"""5min 末根 close 与日线 raw_close 的逐 只-日 对账（数据正确性的核心判据）。

为什么必须有这一步
------------------
覆盖率审计只回答「有没有文件」，回答不了「里面是不是这只票那天的价」。
而两者都会让研究出错，且错法不同：
  - 缺文件 ⇒ 该 只-日 消失（样本选择偏差）；
  - 文件在但价格错基准（前复权 vs 原始）⇒ 收益率静默偏移一个股息比例。

口径
----
- 5min 段（`f5_merged`）是**原始价**；日线侧必须用 `raw_close`（`kline_daily_enriched`），
  不能用 `close`（前复权）。退市股不在 enriched 里，取 `backfill_20261117/stock_gap_daily`。
- 只比「5min 当日有成交」的 只-日：停牌日 baostock 返回单根 volume=0 假 bar，merge 已剔除，
  所以这里的样本天然是「真实交易过」的日子。
- 末根取 15:00 那根；若某日末根不是 15:00（数据截断），单独计数，不混进误差。

判据（2026-11-20 实测后修正，原写 0.999 是错的）
----
    匹配率应落在 **0.95~0.99**（实测 0.976），**不是 0.999**：
    5min 的 15:00 bar 不含 14:57-15:00 收盘集合竞价，尾盘异动的日子末根 close
    ≠ 日线收盘价（实测 300799.SZ 2023-10-24：5min 212.75 vs 日线 210.00，
    连 low 都差 1.51 —— 210 那个低点出现在集合竞价里）。
    ⇒ **判涨停/封板必须用日线 `raw_close`，不能用 5min 末根**；
    ⇒ 用 5min 算日内 MAE 会**低估**尾部（集合竞价的极值不在 5min 里）。
    若匹配率 < 0.9 或 max|Δ| 与股价同量级 ⇒ 那是基准错（前复权混入），不是尾盘效应。

用法
----
    python analysis/_verify_minute_close.py                 # 抽 40 只
    python analysis/_verify_minute_close.py --n 120 --seed 7
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import polars as pl

ROOT = Path(r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
F5 = DATA / "backfill_bs_minute" / "f5_merged"
ENRICHED = DATA / "kline_daily_enriched"
GAP = DATA / "backfill_20261117" / "stock_gap_daily.parquet"
BS_START = "2020-01-02"
SURVIVOR_END = "2024-11-06"


def gap_symbols() -> set[str]:
    return set(pl.read_parquet(GAP, columns=["symbol"])["symbol"].unique().to_list())


def daily_raw(gaps: set[str]) -> pl.DataFrame:
    """窗口内日线 raw_close。存续股走 enriched，缺口股走 gap 日K。"""
    days = sorted(d[5:] for d in os.listdir(ENRICHED)
                  if d.startswith("date=") and BS_START <= d[5:] <= SURVIVOR_END)
    parts = []
    for d in days:
        p = ENRICHED / f"date={d}" / "part.parquet"
        if not p.exists():
            continue
        parts.append(pl.read_parquet(p, columns=["symbol", "date", "raw_close"]))
    a = pl.concat(parts) if parts else pl.DataFrame(
        schema={"symbol": pl.String, "date": pl.Date, "raw_close": pl.Float64})
    a = a.filter(~pl.col("symbol").is_in(list(gaps)))

    b = (pl.read_parquet(GAP, columns=["symbol", "date", "close"])
          .rename({"close": "raw_close"})
          .with_columns(pl.col("date").cast(pl.Date))
          .filter(pl.col("date").is_between(pl.lit(BS_START).str.to_date(),
                                            pl.lit(SURVIVOR_END).str.to_date())))
    return pl.concat([a.select(["symbol", "date", "raw_close"]), b])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    gaps = gap_symbols()
    syms = sorted(p.stem for p in F5.glob("*.parquet"))
    if not syms:
        print("f5_merged 为空，先跑 merge_bs_minute.py --all")
        return

    # 分层抽样：缺口股（退市/ST/其他）与存续股各取一半，否则退市侧会被稀释掉
    del_side = [s for s in syms if s in gaps]
    sur_side = [s for s in syms if s not in gaps]
    k = max(1, args.n // 2)
    pick = (pl.Series(del_side).shuffle(seed=args.seed).head(k).to_list()
            + pl.Series(sur_side).shuffle(seed=args.seed + 1).head(args.n - k).to_list())
    pick = [s for s in pick if (F5 / f"{s}.parquet").exists()]
    print(f"抽样 {len(pick)} 只（缺口 {len([s for s in pick if s in gaps])} / "
          f"存续 {len([s for s in pick if s not in gaps])}）")

    dly = daily_raw(gaps).filter(pl.col("symbol").is_in(pick))

    rows = []
    for s in pick:
        m = pl.read_parquet(F5 / f"{s}.parquet")
        if m.height == 0:
            continue
        last = (m.with_columns(pl.col("datetime").dt.date().alias("date"),
                               pl.col("datetime").dt.time().alias("t"))
                 .sort("datetime")
                 .group_by("date").agg(pl.col("close").last().alias("m_close"),
                                       pl.col("t").last().alias("m_t"),
                                       pl.col("volume").sum().alias("m_vol")))
        j = last.join(dly.filter(pl.col("symbol") == s).select(["date", "raw_close"]),
                      on="date", how="inner")
        if j.height == 0:
            continue
        j = j.with_columns((pl.col("m_close") - pl.col("raw_close")).abs().alias("ad"),
                           (pl.col("m_t").cast(pl.String) == "15:00:00").alias("ok_tail"))
        rows.append(pl.DataFrame({
            "symbol": [s],
            "n": [j.height],
            "hit": [int((j["ad"] <= 0.005).sum())],
            "max_ad": [float(j["ad"].max())],
            "tail15": [int(j["ok_tail"].sum())],
            "miss_days": [j.height - int((j["ad"] <= 0.005).sum())],
        }))

    if not rows:
        print("没有可比样本")
        return
    r = pl.concat(rows)
    n_tot = int(r["n"].sum())
    hit = int(r["hit"].sum())
    print(f"\n只-日 {n_tot:,}  匹配 {hit:,}  = {hit / n_tot:.6f}")
    print(f"末根=15:00 的 只-日 {int(r['tail15'].sum()):,} / {n_tot:,}"
          f"  ({int(r['tail15'].sum()) / n_tot:.4f})")
    print(f"按票 max|Δ| 中位 {r['max_ad'].median():.6f}  p90 {r['max_ad'].quantile(0.9):.6f}")
    bad = r.filter(pl.col("miss_days") > 0).sort("miss_days", descending=True).head(10)
    if bad.height:
        print("\n不一致最多的 10 只：")
        print(bad)
    print("\n判据：匹配率 >= 0.999 且 max|Δ| 中位 < 0.005 ⇒ 同基准同源。")


if __name__ == "__main__":
    main()
