"""预注册 surge-all：全市场放量事件的锁仓分层（2026-11-25）。

判据与规则冻结在 `docs/structural-edge/prereg-surge-all.md`。本脚本是唯一实现点。

与 `lock_surge.py`（v1，已关闭）的区别：
  v1 比「锁仓带内 放量 vs 安静」，事件 3 只/日，6 个月 MDE≈977bp，不可检验；
  v2 比「全市场放量事件，最低换手档 vs 最高换手档」，事件 ~300-500 只/日，
  分层用当日横截面 rank 五分位（消除绝对换手带随 regime 漂移）。

跑法：
    backend/.venv/Scripts/python.exe analysis/_diag_surge_allmarket.py --run

窗口（写死，不调）：
    design  2010-01-01 .. 2019-12-31   （描述，不参与裁决）
    holdout 2020-01-01 .. 2026-10-31   （pseudo-OOS，只作先验）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"

DESIGN = ("2010-01-01", "2019-12-31")
HOLDOUT = ("2020-01-01", "2026-10-31")

TR_CUT = 2.0
FWD = 20
COST_RT = 0.0012
GATE_DIFF_BP = 80.0
GATE_T = 1.65
GATE_POS_MONTHS = 0.60
GATE_MIN_Q1 = 10.0
MIN_AMT60 = 1.5e7
MIN_LIST_DAYS = 365          # 自然日；≈250 交易日（页面用交易日，这里用日历日近似）


def _days() -> list[str]:
    return [p.name[5:] for p in sorted(ENRICHED.glob("date=*"))]


def _load(lo: int, hi: int) -> pl.DataFrame:
    files = sorted(ENRICHED.glob("date=*"))[lo:hi]
    frames = []
    for p in files:
        f = p / "part.parquet"
        if not f.exists():
            continue
        frames.append(pl.read_parquet(
            f, columns=["symbol", "date", "open", "high", "low", "close",
                        "turnover_rate", "amount", "consecutive_limit_ups"]))
    return pl.concat(frames, how="vertical_relaxed").sort(["symbol", "date"])


def _features(panel: pl.DataFrame) -> pl.DataFrame:
    panel = panel.with_columns([
        pl.col("turnover_rate").rolling_mean(60, min_samples=55).over("symbol").alias("turn60"),
        pl.col("amount").rolling_mean(60, min_samples=55).over("symbol").alias("amt60"),
        pl.col("open").shift(-1).over("symbol").alias("_o1"),
        pl.col("high").shift(-1).over("symbol").alias("_h1"),
        pl.col("low").shift(-1).over("symbol").alias("_l1"),
        pl.col("open").shift(-(FWD + 1)).over("symbol").alias("_o21"),
        pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol").alias("_lu"),
    ])
    return panel.with_columns([
        pl.when(pl.col("_o1") > 0).then(pl.col("_o21") / pl.col("_o1") - 1).alias("fwd20"),
        (pl.col("_lu").fill_null(0) >= 1).alias("lu_fwd20"),
        # 买不进的一字：看**入场日 t+1**，不是信号日 t
        ((pl.col("_o1") == pl.col("_h1")) & (pl.col("_o1") == pl.col("_l1"))).alias("_ow1"),
    ])


def _year_rows(year: int, all_days: list[str]) -> list[dict]:
    """算一年、按 (date, quintile) 聚合；前后各留 70/25 个交易日。"""
    lo = next(i for i, d in enumerate(all_days) if d >= f"{year}-01-01")
    hi = max(i for i, d in enumerate(all_days) if d <= f"{year}-12-31")
    panel = _load(max(0, lo - 70), min(len(all_days), hi + 26))
    panel = _features(panel)

    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet",
                           columns=["symbol", "name", "listing_date", "float_shares"])
    panel = panel.join(inst, on="symbol", how="left")
    panel = panel.with_columns(pl.col("listing_date").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False))

    # 全市场等权基准：所有「三个价格都在」的票，先于任何筛选
    base = panel.filter(pl.col("fwd20").is_not_null())
    mkt = base.group_by("date").agg(pl.col("fwd20").mean().alias("mkt"))
    panel = panel.join(mkt, on="date", how="left").with_columns(
        (pl.col("fwd20") - pl.col("mkt")).alias("exc20")
    )

    # 股票池：非 ST / 非北交所 / 上市 ≥365 自然日 / 60 日均额 ≥1500 万
    name = pl.col("name").fill_null("")
    sym = pl.col("symbol")
    panel = panel.filter(
        ~name.str.contains("ST")
        & ~(sym.str.ends_with(".BJ") | sym.str.starts_with("920"))
        & (pl.col("listing_date").is_not_null())
        & ((pl.col("date") - pl.col("listing_date")).dt.total_days() >= MIN_LIST_DAYS)
        & (pl.col("amt60") >= MIN_AMT60)
        & (pl.col("turn60").is_not_null())
        & (pl.col("turn60") > 0)
        & (pl.col("float_shares") > 0)
        & pl.col("exc20").is_not_null()
    )

    # 当日横截面 rank 分位：换手五分位 q（1 = 最低）+ 市值五分位 mq（规模控制用）
    panel = panel.with_columns([
        pl.col("turn60").rank("ordinal").over("date").alias("_r"),
        (pl.col("close") * pl.col("float_shares")).alias("mcap"),
        pl.len().over("date").alias("_n"),
        (pl.col("turnover_rate") / pl.col("turn60")).alias("tr"),
    ]).with_columns([
        (((pl.col("_r") - 1) * 5 / pl.col("_n")).floor().clip(0, 4) + 1).cast(pl.Int8).alias("q"),
        pl.col("mcap").rank("ordinal").over("date").alias("_rm"),
    ]    ).with_columns(
        (((pl.col("_rm") - 1) * 5 / pl.col("_n")).floor().clip(0, 4) + 1).cast(pl.Int8).alias("mq")
    ).filter((pl.col("date").dt.year() == year) & pl.col("mq").is_not_null())

    surge = panel.filter(pl.col("tr") >= TR_CUT)
    rows = []
    for (d, q, mq), g in surge.group_by(["date", "q", "mq"]):
        rows.append({
            "date": str(d), "q": int(q), "mq": int(mq), "n_surge": g.height,
            "mean_ex": float(g["exc20"].mean()),
            "med_ex": float(g["exc20"].median()),
            "hit": float((g["exc20"] > 0).mean()),
            "oneword": float(g["_ow1"].mean() or 0.0),
        })
    return rows


def _monthly(rows: list[dict]) -> list[dict]:
    """按月聚簇。同时给出「全体」与「市值五分位内」两个层级。"""
    by: dict[str, dict[int, dict[int, list[dict]]]] = {}
    for r in rows:
        by.setdefault(r["date"][:7], {}).setdefault(r["q"], {}).setdefault(r["mq"], []).append(r)
    out = []
    for m in sorted(by):
        rec: dict = {"month": m, "by_mq": {}}
        for q in range(1, 6):
            rs = [x for mq in by[m].get(q, {}) for x in by[m][q][mq]]
            if not rs:
                continue
            ns = [x["n_surge"] for x in rs]
            rec[f"q{q}_ex"] = _wmean([x["mean_ex"] for x in rs], ns)
            rec[f"q{q}_n"] = sum(ns) / len({x["date"] for x in rs})
            rec[f"q{q}_hit"] = _wmean([x["hit"] for x in rs], ns)
            rec[f"q{q}_ow"] = _wmean([x["oneword"] for x in rs], ns)
        for mq in range(1, 6):
            d = {}
            for q in (1, 5):
                rs = by[m].get(q, {}).get(mq, [])
                if rs:
                    ns = [x["n_surge"] for x in rs]
                    d[f"q{q}_ex"] = _wmean([x["mean_ex"] for x in rs], ns)
                    d[f"q{q}_n"] = sum(ns)
            if len(d) == 4:
                d["diff"] = d["q1_ex"] - d["q5_ex"]
            rec["by_mq"][mq] = d
        if "q1_ex" in rec and "q5_ex" in rec:
            rec["diff"] = rec["q1_ex"] - rec["q5_ex"]
        out.append(rec)
    return out


def _wmean(xs: list[float], ws: list[int]) -> float:
    tot = sum(ws)
    return sum(x * w for x, w in zip(xs, ws)) / tot if tot else 0.0


def _t(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else float("nan")


def _report(name: str, rows: list[dict]) -> dict:
    monthly = _monthly(rows)
    diffs = [m["diff"] for m in monthly if "diff" in m]
    q1 = [m["q1_ex"] for m in monthly if "q1_ex" in m]
    q5 = [m["q5_ex"] for m in monthly if "q5_ex" in m]
    hit1 = [m["q1_hit"] for m in monthly if "q1_hit" in m]
    hit5 = [m["q5_hit"] for m in monthly if "q5_hit" in m]
    ow1 = [m["q1_ow"] for m in monthly if "q1_ow" in m]
    ow5 = [m["q5_ow"] for m in monthly if "q5_ow" in m]
    q1n = [m["q1_n"] for m in monthly if "q1_n" in m]
    mono = sum(1 for m in monthly if m.get("q1_ex", 0) > m.get("q5_ex", 0))
    n_month = len(diffs)
    res = {
        "window": name,
        "months": n_month,
        "q1_ex_bp": round(sum(q1) / len(q1) * 1e4, 1) if q1 else None,
        "q5_ex_bp": round(sum(q5) / len(q5) * 1e4, 1) if q5 else None,
        "diff_bp": round(sum(diffs) / len(diffs) * 1e4, 1) if diffs else None,
        "diff_t": round(_t(diffs), 2) if diffs else None,
        "q1_net_bp": round((sum(q1) / len(q1) - COST_RT) * 1e4, 1) if q1 else None,
        "hit1": round(sum(hit1) / len(hit1) * 100, 1) if hit1 else None,
        "hit5": round(sum(hit5) / len(hit5) * 100, 1) if hit5 else None,
        "oneword1": round(sum(ow1) / len(ow1) * 100, 2) if ow1 else None,
        "oneword5": round(sum(ow5) / len(ow5) * 100, 2) if ow5 else None,
        "pos_months": round(sum(1 for d in diffs if d > 0) / len(diffs), 3) if diffs else None,
        "mono_months": round(mono / n_month, 3) if n_month else None,
        "q1_n": round(sum(q1n) / len(q1n), 1) if q1n else None,
        "monthly": [
            {k: (round(v * 1e4, 1) if isinstance(v, float) and (k.endswith("_ex") or k == "diff") else v)
             for k, v in m.items() if k != "by_mq"}
            for m in monthly
        ],
    }
    print(f"\n== {name} ==  月数 {res['months']}")
    if not res["months"]:
        print("  窗口内没有数据")
        return res
    print(f"  Q1 放量 {res['q1_ex_bp']:+.1f}bp / Q5 放量 {res['q5_ex_bp']:+.1f}bp"
          f" → 差 {res['diff_bp']:+.1f}bp (t {res['diff_t']:+.2f})")
    print(f"  Q1 净超额 {res['q1_net_bp']:+.1f}bp | 胜率 Q1 {res['hit1']}% vs Q5 {res['hit5']}%"
          f" | 正月份 {res['pos_months']*100:.0f}% | 单调月份 {res['mono_months']*100:.0f}%"
          f" | Q1 日均 {res['q1_n']:.1f} 只 | 一字 Q1 {res['oneword1']}% / Q5 {res['oneword5']}%")

    # 规模控制：每个市值五分位内部的 Q1−Q5（若五档都为正，说明不是「低换手=大市值」）
    size = {}
    for mq in range(1, 6):
        ds = [m["by_mq"][mq]["diff"] for m in monthly if "diff" in m["by_mq"].get(mq, {})]
        if ds:
            size[mq] = {"diff_bp": round(sum(ds) / len(ds) * 1e4, 1), "t": round(_t(ds), 2), "months": len(ds)}
    res["size_control"] = size
    if size:
        print("  市值分位内（1=最小）："
              + " | ".join(f"mq{k} {v['diff_bp']:+.0f}bp(t{v['t']:+.2f})" for k, v in size.items()))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--years", default="", help="调试用：逗号分隔的年份，默认 2010-2026")
    args = ap.parse_args()
    if not args.run:
        print(__doc__)
        return 0

    all_days = _days()
    years = [int(y) for y in args.years.split(",")] if args.years else list(range(2010, 2027))
    rows: list[dict] = []
    for y in years:
        got = _year_rows(y, all_days)
        rows.extend(got)
        print(f"  {y}: {len(got)} 个 (date,q) 组", flush=True)

    def slice_rows(win):
        lo, hi = win
        return [r for r in rows if lo <= r["date"] <= hi]

    design = _report("DESIGN 2010-2019（描述）", slice_rows(DESIGN))
    hold = _report("HOLDOUT 2020-2026（pseudo-OOS）", slice_rows(HOLDOUT))

    out = {
        "prereg_id": "surge-all-2026-11-25",
        "rule": f"全市场 tr>= {TR_CUT}；按当日 turn60 rank 五分位；T+1 开盘买 → T+{FWD+1} 开盘卖",
        "gate": {"diff_bp": GATE_DIFF_BP, "t": GATE_T, "pos_months": GATE_POS_MONTHS,
                 "min_q1": GATE_MIN_Q1, "cost_rt": COST_RT},
        "design": design,
        "holdout": hold,
    }
    f = DATA / "_diag_surge_allmarket.json"
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物：{f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
