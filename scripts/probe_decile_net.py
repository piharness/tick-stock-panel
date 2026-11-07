"""十年面板上的十分位净 alpha 网格：把「gross 有多大」换成「net 能不能过 0」。

动机：前一轮只报了十分位形状（bp/20日）与 D1−D10 的 t，没有报**每一格的 t 与净值**。
而产品形态固定为「仅多、N=20、19bp/边」，那么真正的问题只有一个：
存在任一格，gross > 2×19bp 且 t 站得住吗？H=60 的换手只有 H=20 的三分之一，
是唯一可能藏住正净值的地方，前一轮没算。

口径（刻意与生产 probe_mde_matrix 对齐，便于交叉核对）：
  mom  = exp(cum_t - cum_{t-20}) - 1        复权累计对数收益，自建 ex_factor
  fwd  = exp(cum_{t+1+h} - cum_{t+1}) - 1   入场 t+1 收盘，出场 t+1+h 收盘
  base = 当日合格池等权 fwd 均值（零假设中心）
  十分位 = 当日 mom 的等数量分箱（rank/count×10 取整，与 quantile 分箱等价）
  成交额 = dump 自带 turnover 字段（元）—— 不是 volume×累计对数收益
  t 的块长 = 2×H：H 期重叠会把自相关藏进块内，block<H 会系统性高估 t

输出：data/.scratch/decile_net.json

⚠ 引擎坑：本脚本的窗口函数（shift/cum_sum ... over）**必须用默认引擎 collect()**。
  同一段代码在 collect(engine="streaming") 下不报错但结果错：D1−D10 从 177bp 缩到
  19.5bp（约 1/9），形状仍单调，肉眼看不出来。已用「逐票 numpy 切片」对拍确认默认引擎正确。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "data/cache/fuyao/daily_k__20261104.parquet"
EXF = ROOT / "data/cache/fuyao/adj_factors__20261104.parquet"
OUT = ROOT / "data/.scratch"
PANEL_CACHE = OUT / "_decile_panel.parquet"
LOOKBACK = 20
HORIZONS = (20, 60)
COST_BP = 38.0          # 19bp/边
PORTFOLIO_YI = (1.0, 5.0, 20.0)
TOP_N = 20


def block_se(x: np.ndarray, block: int = 40, reps: int = 1000, seed: int = 0) -> float:
    n = len(x)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[i] = x[idx.ravel()[:n]].mean()
    return float(means.std(ddof=1))


def build() -> pl.DataFrame:
    ex = (
        pl.scan_parquet(EXF)
        .select(["thscode", "ex_date_ms", "dividend_per_share", "per_share_bonus",
                 "allotment_ratio", "allotment_price"])
        .with_columns(
            pl.from_epoch(pl.col("ex_date_ms") + 8 * 3600 * 1000, time_unit="ms")
            .dt.date().alias("date"))
        .select(["thscode", "date", "dividend_per_share", "per_share_bonus",
                 "allotment_ratio", "allotment_price"])
        .collect()
        .lazy()
    )
    lf = (
        pl.scan_parquet(DUMP)
        .select(["thscode", "date_ms", "close_price", "volume", "turnover"])
        .with_columns(
            pl.from_epoch(pl.col("date_ms") + 8 * 3600 * 1000, time_unit="ms")
            .dt.date().alias("date"))
        .filter(pl.col("close_price") > 0)
        .sort(["thscode", "date"])
        .with_columns(pl.col("close_price").shift(1).over("thscode").alias("prev_close"))
        .join(ex, on=["thscode", "date"], how="left")
    )
    ref = ((pl.col("prev_close") - pl.col("dividend_per_share").fill_null(0.0)
            + pl.col("allotment_price").fill_null(0.0) * pl.col("allotment_ratio").fill_null(0.0))
           / (1.0 + pl.col("per_share_bonus").fill_null(0.0)
              + pl.col("allotment_ratio").fill_null(0.0)))
    lf = (
        lf.with_columns(
            pl.when(pl.col("dividend_per_share").is_not_null() & (ref > 0))
            .then(pl.col("prev_close") / ref).otherwise(1.0).alias("ex_factor"))
        .with_columns(
            (pl.col("close_price") / pl.col("prev_close") * pl.col("ex_factor"))
            .log().fill_null(0.0).alias("log_ret"))
        .with_columns(pl.col("log_ret").cum_sum().over("thscode").alias("cum"))
        .with_columns(
            ((pl.col("cum") - pl.col("cum").shift(LOOKBACK).over("thscode")).exp() - 1.0).alias("mom"))
    )
    for h in HORIZONS:
        lf = lf.with_columns(
            ((pl.col("cum").shift(-(h + 1)).over("thscode")
              - pl.col("cum").shift(-1).over("thscode")).exp() - 1.0).alias(f"fwd{h}"))
    return lf.select(["thscode", "date", "mom"] + [f"fwd{h}" for h in HORIZONS]
                     + ["turnover", "volume"]).collect()


def load_panel() -> pl.DataFrame:
    """构建并缓存带 mom/fwd/turnover 的面板。缓存只是为了迭代快，口径不变。"""
    if PANEL_CACHE.exists():
        return pl.read_parquet(PANEL_CACHE)
    df = build()
    OUT.mkdir(parents=True, exist_ok=True)
    df.write_parquet(PANEL_CACHE)
    return df


def deciles(df: pl.DataFrame, h: int) -> pl.DataFrame:
    fwd = f"fwd{h}"
    d = df.filter(
        pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
        & (pl.col("turnover") > 0)
    )
    return (
        d.with_columns(
            ((pl.col("mom").rank("ordinal").over("date") - 1)
             / pl.col("mom").count().over("date") * 10.0)
            .floor().clip(0, 9).cast(pl.Int8).alias("dec"))
        .group_by(["date", "dec"])
        .agg(
            pl.col(fwd).mean().alias("mu"),
            pl.col(fwd).len().alias("n"),
            pl.col("turnover").median().alias("amt"),
        )
        .sort(["date", "dec"])
    )


def main() -> None:
    print("构建面板（自建复权，默认引擎）…", flush=True)
    df = load_panel()
    print(f"  行数 {df.height:,}  日期 {df['date'].min()} → {df['date'].max()}", flush=True)

    report: dict = {"cost_bp": COST_BP, "lookback": LOOKBACK, "rows": df.height}
    for h in HORIZONS:
        g = deciles(df, h)
        # 当日全池等权基准 = 各十分位按 n 加权平均
        base = g.group_by("date").agg(
            (pl.col("mu") * pl.col("n")).sum().alias("tw"),
            pl.col("n").sum().alias("tn")).with_columns(
            (pl.col("tw") / pl.col("tn")).alias("base")).select(["date", "base"])
        g = g.join(base, on="date").with_columns((pl.col("mu") - pl.col("base")).alias("exc"))

        dates = g["date"].unique().sort().to_list()
        rows = []
        for dec in range(10):
            seg = g.filter(pl.col("dec") == dec).sort("date")
            x = seg["exc"].to_numpy()
            mu_bp = 1e4 * x.mean()
            se_40 = block_se(x, block=40)
            se_2h = block_se(x, block=2 * h)
            t = mu_bp / (1e4 * se_40) if x.size > 30 else float("nan")
            t2 = mu_bp / (1e4 * se_2h) if x.size > 30 else float("nan")
            rows.append({
                "decile": dec + 1,
                "n_days": int(x.size),
                "members_med": float(seg["n"].median()),
                "gross_bp": float(mu_bp),
                "t": float(t),
                "t_block2h": float(t2),
                "net_bp": float(mu_bp - COST_BP),
                "net_19bp": float(mu_bp - COST_BP / 2),
                "amt_median_yi": float(seg["amt"].median() / 1e8),
                "hit": float((x > 0).mean()),
            })
        report[f"H{h}"] = rows
        report[f"H{h}_days"] = len(dates)

        print(f"\n=== H={h}  （{len(dates)} 个决策日，成本 {COST_BP:.0f}bp/期）===")
        print("  D   毛超额  t(b40) t(b2H)  净(38)  净(19)  中位成交额  胜率")
        for r in rows:
            print(f"  {r['decile']:2d} {r['gross_bp']:+7.1f} {r['t']:+6.2f} {r['t_block2h']:+6.2f} "
                  f"{r['net_bp']:+7.1f} {r['net_19bp']:+7.1f}  {r['amt_median_yi']:8.2f}亿 "
                  f"{r['hit']:.2f}")

        # 容量：N=20 等权，portfolio 总额 S，单票下单额 S/20
        cap = []
        d1 = rows[0]
        for s in PORTFOLIO_YI:
            order = s / TOP_N * 1e8
            adv = d1["amt_median_yi"] * 1e8
            cap.append({"portfolio_yi": s, "pct_adv_d1": float(order / adv * 100)})
        report[f"H{h}_capacity"] = cap
        print("  D1 容量（N=20 等权）: " + ", ".join(
            f"{c['portfolio_yi']:.0f}亿→{c['pct_adv_d1']:.1f}%ADV" for c in cap))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "decile_net.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    print("\n写出 data/.scratch/decile_net.json")


if __name__ == "__main__":
    sys.exit(main())
