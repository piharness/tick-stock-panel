"""验证：龙虎榜「机构 vs 游资」是否携带量价之外的信息？

命题（可证伪）：
  龙虎榜上榜当日，是「机构净买」还是「游资净买」，对未来收益有独立于
  「当日涨跌幅」的预测力吗？「谁在买」这个维度是否高于「买了多少」？

必须攻击的假设（前几轮教训）：
  上榜是「结果」（因大涨/大跌/异动才上榜），不是原因。因此「机构净买」
  很可能只是「当天涨得不多、机构在低位接」的镜像，而「游资净买」是
  「当天爆炒」的镜像。若不控制当日涨跌幅 change，会重蹈「复合代理」覆辙。

方法：
  - 数据：data/dragon_tiger（243 日 JSON，2025-11~2026-11）+ kline_daily_enriched。
  - 事件：all 榜条目（去重 ticker+date），按 org_net_value 与 hot_money_net_value
    的符号/占比分成「机构净买」「游资净买」「双净卖」等组。
  - 后续收益：T+1 开盘买入，H=5/20，扣印花税+佣金，防前视。
  - 关键对照：按当日涨跌幅 change 分桶（控制「上榜是涨是跌」），再看
    机构 vs 游资在【同涨跌幅桶内】是否有差异。

数据契约（实测）：
  all 榜有 org_net_value(11410条)/hot_money_net_value(16307条)，但无 org_net_rate。
  org 榜是「有机构参与」子集，含 org_net_rate/org_buy_num。org_net_rate 需自算或从 org 榜取。

输出：data/.scratch/dragon_tiger_org_vs_hotmoney.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "data" / "kline_daily_enriched"
DT_DIR = ROOT / "data" / "dragon_tiger"
OUT = ROOT / "data" / ".scratch"

HORIZONS = (5, 20)
STAMP_TAX = 0.0010
COMMISSION = 0.00025
COST = STAMP_TAX + 2 * COMMISSION


def block_se(x: np.ndarray, block: int, reps: int = 1000, seed: int = 0) -> float:
    n = len(x)
    if n < block:
        block = max(1, n)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[i] = x[idx.ravel()[:n]].mean()
    return float(means.std(ddof=1))


def load_dragon_tiger() -> pl.DataFrame:
    """解析全部龙虎榜 JSON，得到 (date, ticker, change, org_net_value, hot_money_net_value, range_days)。"""
    rows = []
    for f in sorted(DT_DIR.glob("date=*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        trade_date = d.get("trade_date")
        if not trade_date:
            continue
        items = d.get("all", {}).get("stock_items") or []
        for it in items:
            rows.append({
                "date": trade_date,
                "ticker": str(it.get("ticker") or ""),
                "change": it.get("change"),
                "org_net": it.get("org_net_value"),
                "hm_net": it.get("hot_money_net_value"),
                "range_days": it.get("range_days"),
            })
    df = pl.DataFrame(rows)
    # 去掉 ticker 的市场后缀（如 002086.SZ → 002086），与 enriched 的 symbol 对齐
    df = df.with_columns(
        pl.col("ticker").str.replace(r"\..*$", "").alias("symbol"),
        pl.col("date").str.to_date().alias("date"),
    )
    return df


def load_kline() -> pl.DataFrame:
    """读 enriched，取 symbol/date/open/close/amount，用于算后续收益。"""
    files = sorted(str(p) for p in TABLE.glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "close", "amount"]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )
    # symbol 去掉市场后缀（000001.SZ → 000001），与龙虎榜裸代码对齐
    df = df.with_columns(pl.col("symbol").str.replace(r"\..*$", "").alias("symbol"))
    return df


def main() -> None:
    print("读龙虎榜…", flush=True)
    dt = load_dragon_tiger()
    print(f"  龙虎榜原始条目: {dt.height:,}", flush=True)

    # 去重：同一 (symbol, date) 可能因 1日榜+3日榜 出现多条，取 range_days 最小的（优先当日榜）
    dt = dt.sort(["symbol", "date", "range_days"]).unique(
        subset=["symbol", "date"], keep="first")
    print(f"  去重后事件: {dt.height:,}", flush=True)

    # 过滤无效 change
    dt = dt.filter(pl.col("change").is_not_null())
    print(f"  有 change 的事件: {dt.height:,}", flush=True)

    print("读日K…", flush=True)
    k = load_kline()

    # 计算后续收益：T+1 开盘买，持有 H 日。用 join 到 enriched 面板。
    # 对每个事件 (symbol, T)，找到 T 之后的下一个交易日作为入场，再持有 H 日。
    k = k.with_columns([
        (pl.col("close") / pl.col("open") - 1.0).alias("ret_open_close"),  # 当日（从开盘到收盘）
    ])

    # 为每个 symbol 生成「未来第 h 日」的收盘价序列
    k = k.sort(["symbol", "date"])
    k = k.with_columns([
        pl.col("date").shift(-1).over("symbol").alias("next_date"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
    ])
    for h in HORIZONS:
        k = k.with_columns(
            pl.col("close").shift(-(h + 1)).over("symbol").alias(f"close_h{h}")
        )

    # 事件日 T 的收益基准：T+1 开盘 → T+1+h 收盘
    dt2 = dt.join(
        k.select(["symbol", "date", "next_open", *[f"close_h{h}" for h in HORIZONS]]),
        on=["symbol", "date"], how="inner",
    )
    # 注意：事件日 date 是龙虎榜的 trade_date（字符串），enriched 的 date 也是字符串，直接对齐。
    for h in HORIZONS:
        dt2 = dt2.with_columns(
            ((pl.col(f"close_h{h}") / pl.col("next_open") - 1.0 - COST).alias(f"fwd{h}"))
        )
    dt2 = dt2.filter(pl.col(f"fwd{HORIZONS[0]}").is_not_null())
    print(f"  成功对接后续收益的事件: {dt2.height:,}", flush=True)

    # 分组：机构净买 vs 游资净买
    # 机构净买：org_net > 0 且 显著（相对成交额）。游资净买：hm_net > 0 且 org_net <= 0。
    # 用绝对金额 + 相对当日涨跌分桶来分离。
    dt2 = dt2.with_columns([
        pl.when((pl.col("org_net") > 0) & (pl.col("hm_net") <= 0))
        .then(pl.lit("机构净买(游资不参与)"))
        .when((pl.col("hm_net") > 0) & (pl.col("org_net") <= 0))
        .then(pl.lit("游资净买(机构不参与)"))
        .when((pl.col("org_net") > 0) & (pl.col("hm_net") > 0))
        .then(pl.lit("机构+游资同买"))
        .otherwise(pl.lit("双净卖/无"))
        .alias("buyer_type")
    ])

    report = {"meta": {"n_events": int(dt2.height)}, "buyer_types": {}}

    print("\n=== 各 buyer_type 的样本量与后续收益 ===", flush=True)
    for (bt,), seg in dt2.group_by("buyer_type"):
        row = {"n": int(seg.height)}
        line = f"  {bt}: n={seg.height:,}"
        for h in HORIZONS:
            s = seg.filter(pl.col(f"fwd{h}").is_not_null())
            if s.height < 10:
                continue
            arr = s[f"fwd{h}"].to_numpy()
            mean = float(arr.mean())
            se = block_se(arr, block=2 * h)
            t = mean / se if se > 0 else float("nan")
            row[f"H{h}"] = {"net_bp": round(mean * 1e4, 1), "t": round(t, 2)}
            line += f"  H{h}={mean*1e4:+7.1f}bp(t={t:+.2f})"
        report["buyer_types"][bt] = row
        print(line, flush=True)

    # ===== 关键：控制当日涨跌幅 change =====
    # 按 change 分 3 桶（跌/平/涨），在每桶内比较 机构 vs 游资
    print("\n=== 控制当日涨跌幅 change（分桶后 机构 vs 游资 差异）===", flush=True)
    dt2 = dt2.with_columns(
        pl.when(pl.col("change") < -0.03).then(pl.lit("大跌<-3%"))
        .when(pl.col("change") < 0.03).then(pl.lit("平(-3%~3%)"))
        .otherwise(pl.lit("大涨>3%")).alias("chg_bucket")
    )
    control = {}
    inst = dt2.filter(pl.col("buyer_type") == "机构净买(游资不参与)")
    hot = dt2.filter(pl.col("buyer_type") == "游资净买(机构不参与)")
    for bucket in ("大跌<-3%", "平(-3%~3%)", "大涨>3%"):
        i = inst.filter(pl.col("chg_bucket") == bucket)
        hh = hot.filter(pl.col("chg_bucket") == bucket)
        row = {"n_inst": int(i.height), "n_hot": int(hh.height)}
        line = f"  {bucket}: 机构n={i.height:,} 游资n={hh.height:,}"
        for h in HORIZONS:
            ii = i.filter(pl.col(f"fwd{h}").is_not_null())
            hh2 = hh.filter(pl.col(f"fwd{h}").is_not_null())
            if ii.height < 5 or hh2.height < 5:
                continue
            # 按日配对做差分
            i_daily = ii.group_by("date").agg(pl.col(f"fwd{h}").mean().alias("i"))
            h_daily = hh2.group_by("date").agg(pl.col(f"fwd{h}").mean().alias("hh"))
            j = i_daily.join(h_daily, on="date", how="inner")
            if j.height < 5:
                continue
            diff = (j["i"] - j["hh"]).to_numpy()
            m = float(diff.mean())
            se = block_se(diff, block=2 * h)
            t = m / se if se > 0 else float("nan")
            row[f"H{h}_inst_minus_hot_bp"] = round(m * 1e4, 1)
            row[f"H{h}_t"] = round(t, 2)
            line += f"  [H{h} 机构-游资={m*1e4:+7.1f}bp t={t:+.2f}]"
        control[bucket] = row
        print(line, flush=True)
    report["control_by_change"] = control

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "dragon_tiger_org_vs_hotmoney.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/dragon_tiger_org_vs_hotmoney.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
