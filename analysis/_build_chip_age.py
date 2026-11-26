"""筹码年龄面板：把「筹码」从价格上的密度，扩成「价格 × 年龄」的期限结构。

这不是判据脚本，不产生任何 alpha 结论。它只做一件事：
对每个股票、每个月的最后一个交易日，用日频换手率递推筹码年龄分布

    S_t(a) = TO_{t-a} * Π_{u=t-a+1..t} (1 - TO_u)

并输出五个描述量：久期 D、年龄熵 H、年龄分位 q50/q80/q90、新钱占比 young20、
以及 500 日以外的「老钱」占比 old_share。

口径：
  - 数据：data/kline_daily_enriched（日线，turnover_rate 为百分数）
  - 样本：2005-01-04 ~ 2026-11-26；输出 2019-01 起的月末快照
  - 缺失交易日按 TO=0 处理（停牌不换手，筹码继续变老）
  - A=500 个交易日；old_share = 1 - Σ_{a<500} S(a)
  - 年龄分位在 mass 内归一；q=500 表示「500 日仍未覆盖到该分位」

产物：
  data/.scratch/chip_age/age_panel.parquet   股票 × 月末
  data/.scratch/chip_age/market_age.json     年度中位数 / 最新截面 / 说明
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "chip_age"
OUT.mkdir(parents=True, exist_ok=True)


def load_panel(start_year: int) -> pd.DataFrame:
    dataset = ds.dataset(SRC, format="parquet")
    table = dataset.to_table(
        columns=["symbol", "date", "close", "turnover_rate"],
        filter=ds.field("date") >= pd.Timestamp(f"{start_year}-01-01").date(),
    )
    df = table.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[df["close"] > 0]
    # turnover_rate 是百分数；异常值截断，避免 log(0)
    df["to"] = (df["turnover_rate"].fillna(0.0) / 100.0).clip(0.0, 0.99)
    return df


def month_end_dates(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(dates, index=dates)
    return pd.DatetimeIndex(s.groupby([dates.year, dates.month]).max().values).sort_values()


def build(start_year: int = 2005, max_age: int = 500, out_start: str = "2019-01-01") -> None:
    print(f"[1/4] load {start_year}-2026 ...", flush=True)
    df = load_panel(start_year)
    print(f"      rows={len(df):,} symbols={df['symbol'].nunique():,} "
          f"dates={df['date'].nunique():,}", flush=True)

    print("[2/4] pivot date x symbol ...", flush=True)
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last")
    close = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    to = to.sort_index()
    close = close.reindex(to.index)
    to = to.fillna(0.0).astype(np.float32)
    close = close.astype(np.float32)
    dates = pd.DatetimeIndex(to.index)
    syms = np.asarray(to.columns)

    print(f"      matrix={to.shape[0]:,} x {to.shape[1]:,}", flush=True)

    # log(1-TO) 的累积和，用 float64 避免长序列下溢
    logc = np.log1p(-to.to_numpy(dtype=np.float64)).cumsum(axis=0)
    tov = to.to_numpy(dtype=np.float32)

    targets = month_end_dates(dates)
    targets = targets[targets >= pd.Timestamp(out_start)]
    print(f"[3/4] age recursion on {len(targets)} month-ends, A={max_age} ...", flush=True)

    rows: list[dict] = []
    for k, dt in enumerate(targets):
        ti = int(dates.get_loc(dt))
        n = to.shape[1]
        ages = np.zeros((max_age + 1, n), dtype=np.float32)

        base_lc = logc[ti]
        for a in range(0, max_age + 1):
            j = ti - a
            if j < 0:
                break
            if a == 0:
                s = tov[j].astype(np.float64)
            else:
                s = tov[j].astype(np.float64) * np.exp(base_lc - logc[j])
            ages[a] = np.nan_to_num(s, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

        mass = ages.sum(axis=0, dtype=np.float64)
        good = mass > 1e-8
        idx = np.arange(max_age + 1, dtype=np.float32)[:, None]
        d_sum = (ages * idx).sum(axis=0, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            h_sum = -np.where(ages > 0, ages * np.log(ages), 0.0).sum(axis=0, dtype=np.float64)
        young20 = ages[:20].sum(axis=0, dtype=np.float64)
        new5 = ages[:5].sum(axis=0, dtype=np.float64)

        cum = np.cumsum(ages, axis=0, dtype=np.float64) / np.maximum(mass, 1e-12)
        qs = {}
        for q in (0.5, 0.8, 0.9):
            hit = cum >= q
            qv = np.where(hit.any(axis=0), hit.argmax(axis=0), np.nan)
            qs[q] = np.where(good, qv, np.nan).astype(np.float32)

        age_mean = np.where(good, d_sum / np.maximum(mass, 1e-12), np.nan)
        # 归一化分布的熵：-Σ p log p = -Σ s log s / mass + log(mass)
        with np.errstate(divide="ignore", invalid="ignore"):
            age_entropy = np.where(
                good, h_sum / np.maximum(mass, 1e-12) + np.log(np.maximum(mass, 1e-12)), np.nan
            )
        young20_share = np.where(good, young20 / np.maximum(mass, 1e-12), np.nan)
        new5_share = np.where(good, new5 / np.maximum(mass, 1e-12), np.nan)
        old_share = np.where(good, np.clip(1.0 - mass, 0.0, 1.0), np.nan)

        cl = close.iloc[ti].to_numpy(dtype=np.float32)
        rows.append(pd.DataFrame({
            "date": dt,
            "symbol": syms,
            "close": cl,
            "age_mean": age_mean.astype(np.float32),
            "age_entropy": age_entropy.astype(np.float32),
            "q50": qs[0.5],
            "q80": qs[0.8],
            "q90": qs[0.9],
            "young20": young20_share.astype(np.float32),
            "new5": new5_share.astype(np.float32),
            "old_share": old_share.astype(np.float32),
            "mass": mass.astype(np.float32),
        }))
        if (k + 1) % 12 == 0:
            print(f"      {k+1}/{len(targets)} {dt.date()}", flush=True)

    panel = pd.concat(rows, ignore_index=True)
    panel = panel[panel["mass"] > 0.05].copy()
    panel.to_parquet(OUT / "age_panel.parquet", index=False)
    print(f"[4/4] saved {len(panel):,} rows -> {OUT / 'age_panel.parquet'}", flush=True)

    # 年度中位数（按月末截面先取中位，再按年取中位）
    panel["year"] = panel["date"].dt.year
    by_year = (
        panel.groupby("year")
        .agg(
            n=("symbol", "nunique"),
            age_median=("age_mean", "median"),
            age_p25=("age_mean", lambda x: x.quantile(0.25)),
            age_p75=("age_mean", lambda x: x.quantile(0.75)),
            entropy_median=("age_entropy", "median"),
            young20_median=("young20", "median"),
            new5_median=("new5", "median"),
            old_share_median=("old_share", "median"),
            q50_median=("q50", "median"),
            q90_median=("q90", "median"),
        )
        .reset_index()
    )
    latest = panel[panel["date"] == panel["date"].max()]
    summary = {
        "as_of": str(panel["date"].max().date()),
        "rows": int(len(panel)),
        "symbols": int(panel["symbol"].nunique()),
        "dates": int(panel["date"].nunique()),
        "start": str(panel["date"].min().date()),
        "max_age": max_age,
        "yearly": by_year.round(4).to_dict(orient="records"),
        "latest": {
            "n": int(len(latest)),
            "age_median": round(float(latest["age_mean"].median()), 2),
            "age_p25": round(float(latest["age_mean"].quantile(0.25)), 2),
            "age_p75": round(float(latest["age_mean"].quantile(0.75)), 2),
            "entropy_median": round(float(latest["age_entropy"].median()), 3),
            "young20_median": round(float(latest["young20"].median()), 3),
            "new5_median": round(float(latest["new5"].median()), 3),
            "old_share_median": round(float(latest["old_share"].median()), 3),
            "q50_median": round(float(latest["q50"].median()), 1),
            "q90_median": round(float(latest["q90"].median()), 1),
        },
    }
    (OUT / "market_age.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary["latest"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2005)
    ap.add_argument("--max-age", type=int, default=500)
    ap.add_argument("--out-start", default="2019-01-01")
    args = ap.parse_args()
    build(args.start_year, args.max_age, args.out_start)
