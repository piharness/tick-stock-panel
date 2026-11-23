"""二手信息的「信息量」在哪里衰减？——用龙虎榜的三层叙事文本做检验

用户的命题：「就算是谣言也是有信息量的」。
要检验的不是真假，而是**信息量在传播链上的损耗率**。

三个可直接测量的叙事维度（全部是公开的二手信息，不需要付费）：
  1. range_days  传播层级：1 = 当日首次上榜；3 = 连续 3 日上榜（故事已被反复报道）
  2. reason_n    故事复杂度：涨停归因文本按「+」切开的段数（「冰雪工程+液冷+股权受让」=3）
  3. concept_n   蹭概念数：concept_list 的标签个数

三条可证伪假设：
  H1 传播衰减：range_days=3 的后续收益 < range_days=1（越晚听到越差）
  H2 故事反指：reason_n 越多，后续越差（「故事越多越假」的 A 股老话）
  H3 概念反指：concept_n 越多，后续越差

口径（沿用 _diag_dragon_tiger_org_vs_hotmoney.py，不另起一套）：
  - 事件日 T 盘后披露 → T+1 开盘买 → T+1+h 收盘卖（A 股 T+1，最早 T+2 卖）
  - 扣 印花税 10bp + 双边佣金 2×2.5bp
  - 超额 = 个股收益 − 同日全市场等权收益（open→close，同口径）
  - t 值按**交易日聚合**（不是按笔），否则同一天几十只票被当成独立样本

输出：data/.scratch/secondhand_decay.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "data" / "kline_daily_enriched"
DT_DIR = ROOT / "data" / "dragon_tiger"
OUT = ROOT / "data" / ".scratch" / "secondhand_decay.json"

HORIZONS = (5, 20)
STAMP_TAX = 0.0010
COMMISSION = 0.00025
COST = STAMP_TAX + 2 * COMMISSION


def load_events() -> pl.DataFrame:
    """解析全部龙虎榜 JSON，抽取三个叙事维度。"""
    rows = []
    for f in sorted(DT_DIR.glob("date=*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        trade_date = d.get("trade_date")
        if not trade_date:
            continue
        for it in d.get("all", {}).get("stock_items") or []:
            reason = (it.get("limit_reason") or "").strip()
            # 「+」「、」「；」都当分隔符；空串记 0
            n_reason = len([x for x in reason.replace("、", "+").replace("；", "+").split("+") if x.strip()])
            concepts = it.get("concept_list") or []
            rows.append({
                "date": trade_date,
                "ticker": str(it.get("ticker") or ""),
                "change": it.get("change"),
                "range_days": it.get("range_days"),
                "reason_n": n_reason,
                "concept_n": len(concepts),
                "net_rate": it.get("net_rate"),
            })
    df = pl.DataFrame(rows)
    return df.with_columns(
        pl.col("ticker").str.replace(r"\..*$", "").alias("symbol"),
        pl.col("date").str.to_date(),
    )


def load_kline() -> pl.DataFrame:
    files = sorted(str(p) for p in TABLE.glob("date=*/part.parquet"))
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "close", "amount", "turnover_rate"])
        .collect()
        .with_columns(pl.col("symbol").str.replace(r"\..*$", "").alias("symbol"))
        .sort(["symbol", "date"])
    )


def build_forward(k: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """给每只票补 T+1 开盘 与 T+1+h 收盘，并返回同日全市场等权基准。"""
    k = k.with_columns([
        pl.col("date").shift(-1).over("symbol").alias("next_date"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
    ])
    for h in HORIZONS:
        k = k.with_columns(pl.col("close").shift(-(h + 1)).over("symbol").alias(f"close_h{h}"))

    # 全市场基准：以 next_date 为入场日，统计同日全体等权 open→close(h) 收益
    base = k.filter(pl.col("next_open") > 0).select(
        pl.col("next_date").alias("date"),
        *[pl.col(f"close_h{h}").truediv(pl.col("next_open")).sub(1.0).alias(f"b{h}") for h in HORIZONS],
    )
    base = base.group_by("date").agg(
        *[pl.col(f"b{h}").mean().alias(f"base_h{h}") for h in HORIZONS],
        pl.len().alias("n_mkt"),
    ).filter(pl.col("n_mkt") > 500)      # 剔除半日/数据缺失日
    return k, base


def agg_t(x: np.ndarray, dates: np.ndarray) -> tuple[float, float]:
    """按交易日聚合后再算 t（同一天的多只票先平均）。"""
    if len(x) < 10:
        return float("nan"), float("nan")
    s = pl.DataFrame({"d": dates, "x": x}).group_by("d").agg(pl.col("x").mean()).sort("d")
    v = s["x"].to_numpy()
    if len(v) < 10 or v.std(ddof=1) == 0:
        return float("nan"), float("nan")
    return float(v.mean() * 1e4), float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))


def report(dt: pl.DataFrame, key: str, title: str) -> list[dict]:
    out = []
    for (lab,), seg in dt.group_by(key, maintain_order=True):
        row = {"bin": str(lab), "n": int(seg.height), "days": int(seg["date"].n_unique())}
        for h in HORIZONS:
            ev, t = agg_t(seg[f"ex{h}"].to_numpy(), seg["date"].to_numpy())
            row[f"ex{h}_bp"] = None if np.isnan(ev) else round(ev, 1)
            row[f"t{h}"] = None if np.isnan(t) else round(t, 2)
            row[f"win{h}"] = round(float((seg[f"ex{h}"] > 0).mean()) * 100, 1)
        out.append(row)
    print(f"\n=== {title} ===")
    hdr = f"{'bin':>6} {'n':>6} {'days':>5}"
    for h in HORIZONS:
        hdr += f" | ex{h} t{h} win{h}"
    print(hdr)
    for r in out:
        line = f"{r['bin']:>6} {r['n']:>6} {r['days']:>5}"
        for h in HORIZONS:
            line += f" | {r[f'ex{h}_bp']} {r[f't{h}']} {r[f'win{h}']}"
        print(line)
    return out


def main() -> None:
    print("读龙虎榜…", flush=True)
    ev = load_events()
    print(f"  原始条目 {ev.height:,}", flush=True)
    ev = ev.sort(["symbol", "date", "range_days"]).unique(subset=["symbol", "date"], keep="first")
    ev = ev.filter(pl.col("change").is_not_null())
    print(f"  去重后 {ev.height:,} / {ev['date'].n_unique()} 个交易日", flush=True)

    print("读日K…", flush=True)
    k, base = build_forward(load_kline())

    dt = ev.join(
        k.select(["symbol", "date", "next_open", *[f"close_h{h}" for h in HORIZONS],
                  "amount", "turnover_rate"]),
        on=["symbol", "date"], how="inner",
    ).join(base, on="date", how="left")

    for h in HORIZONS:
        dt = dt.with_columns(
            (pl.col(f"close_h{h}") / pl.col("next_open") - 1.0 - COST - pl.col(f"base_h{h}")).alias(f"ex{h}")
        )
    dt = dt.filter(pl.col("ex5").is_not_null() & pl.col("ex20").is_not_null())
    print(f"  可算后续收益 {dt.height:,}", flush=True)

    # 规模控制：流通市值代理 = amount / (turnover_rate/100)，只用当日横截面中位数分大小盘。
    # 单位即便是错的也不影响分组（单调变换），但绝不能用绝对阈值。
    dt = dt.with_columns(
        (pl.col("amount") / (pl.col("turnover_rate") / 100.0)).alias("fmv")
    ).filter(pl.col("fmv") > 0)
    dt = dt.with_columns(
        pl.when(pl.col("fmv") >= pl.col("fmv").median().over("date"))
        .then(pl.lit("大盘")).otherwise(pl.lit("小盘")).alias("mv")
    )

    # 分桶（阈值先定，不看结果）
    dt = dt.with_columns([
        pl.when(pl.col("range_days") <= 1).then(pl.lit("1日(首现)"))
        .when(pl.col("range_days") == 3).then(pl.lit("3日(已被反复报道)"))
        .otherwise(pl.lit("其它")).alias("lvl"),
        pl.when(pl.col("reason_n") <= 1).then(pl.lit("1 个理由"))
        .when(pl.col("reason_n") == 2).then(pl.lit("2 个"))
        .otherwise(pl.lit("3+ 个")).alias("rsn"),
        pl.when(pl.col("concept_n") <= 1).then(pl.lit("1 个概念"))
        .when(pl.col("concept_n") == 2).then(pl.lit("2 个"))
        .otherwise(pl.lit("3+ 个")).alias("ccp"),
    ])

    res = {
        "meta": {
            "n_events": int(dt.height), "n_days": int(dt["date"].n_unique()),
            "window": [str(dt["date"].min()), str(dt["date"].max())],
            "cost_bp": round(COST * 1e4, 1), "horizons": list(HORIZONS),
            "note": "超额 = 个股 − 同日全市场等权；扣 15bp 成本；t 按交易日聚合",
        },
        "by_level": report(dt, "lvl", "H1 传播层级（range_days）"),
        "by_reason": report(dt, "rsn", "H2 故事复杂度（涨停归因段数）"),
        "by_concept": report(dt, "ccp", "H3 蹭概念数（concept_list 长度）"),
    }

    # 连续量：reason_n 全谱单调性
    print("\n=== 连续量：reason_n 逐值 ===")
    mono = []
    for (v,), seg in dt.group_by("reason_n", maintain_order=True):
        if seg.height < 50:
            continue
        ev20, t20 = agg_t(seg["ex20"].to_numpy(), seg["date"].to_numpy())
        mono.append({"reason_n": int(v), "n": int(seg.height),
                     "ex20_bp": None if np.isnan(ev20) else round(ev20, 1),
                     "t20": None if np.isnan(t20) else round(t20, 2)})
        print(f"  reason_n={v}: n={seg.height:,} ex20={mono[-1]['ex20_bp']}bp t={mono[-1]['t20']}")
    res["reason_monotone"] = mono

    # 决定性控制：叙事桶 × 规模桶。若「多概念更好」在大盘内消失，它就是规模效应。
    print("\n=== 双排序：叙事桶 × 市值桶（H20 超额 bp）===")
    dual = []
    for key, name in (("lvl", "传播层级"), ("rsn", "故事复杂度"), ("ccp", "概念数")):
        print(f"-- {name} --")
        for (b, m), seg in dt.group_by([key, "mv"], maintain_order=True):
            if seg.height < 80:
                continue
            ev20, t20 = agg_t(seg["ex20"].to_numpy(), seg["date"].to_numpy())
            row = {"dim": name, "bin": str(b), "mv": m, "n": int(seg.height),
                   "ex20_bp": None if np.isnan(ev20) else round(ev20, 1),
                   "t20": None if np.isnan(t20) else round(t20, 2)}
            dual.append(row)
            print(f"  {b:>18} {m} n={seg.height:>5} ex20={row['ex20_bp']:>8} t={row['t20']}")
    res["dual_sort"] = dual

    # 同一维度的组间差（大盘内 / 小盘内），判断叙事是否独立于规模
    print("\n=== 组间差（同规模内，最高桶 − 最低桶，H20 bp）===")
    gaps = []
    for name in ("传播层级", "故事复杂度", "概念数"):
        for m in ("大盘", "小盘"):
            rows = [r for r in dual if r["dim"] == name and r["mv"] == m and r["ex20_bp"] is not None]
            if len(rows) < 2:
                continue
            hi = max(rows, key=lambda r: r["bin"])
            lo = min(rows, key=lambda r: r["bin"])
            g = round(hi["ex20_bp"] - lo["ex20_bp"], 1)
            gaps.append({"dim": name, "mv": m, "hi": hi["bin"], "lo": lo["bin"], "gap_bp": g})
            print(f"  {name:>8} {m} {hi['bin']} − {lo['bin']} = {g:+.1f}bp")
    res["intra_mv_gap"] = gaps

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
