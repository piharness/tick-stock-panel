"""最小检验：大宗让价（premium_dod）的横截面分位 → fwd20 / fwd120 超额。

只做一件事，且故意不做控制变量：先看单变量有没有信号。
没有 ⇒ 直接关闭，不必再花钱做匹配。
有   ⇒ 才值得进入「同日×市值×买卖方类型」的配对。

口径：
  - 单位 = 股票-日，金额加权 premium_dod；
  - entry = T+1 复权开盘，exit = T+21 / T+121 复权收盘（按全局交易日索引 didx，不按行数 shift）；
  - 超额 = 组均值 − 当日全样本均值（未扣成本）；
  - t = 按日聚合后日序列的 t。

用法:
  python analysis/_diag_block_trade_min.py
输出:
  data/.scratch/block_trade_min.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "data" / "block_trade"
KL = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "block_trade_min.json"
START = "2008-01-01"
HORIZONS = [20, 120]


def load_events() -> pl.DataFrame:
    files = [str(p) for p in sorted(BT.glob("year=*/part.parquet"))]
    df = pl.scan_parquet(files).filter(
        (pl.col("sec_type") == "EQA")
        & (pl.col("market") != "CNSEBJ")
        & pl.col("premium_dod").is_not_null()
        & (pl.col("pre_close") > 0)
        & pl.col("amt").is_not_null()
    ).collect()
    return (
        df.group_by(["date", "symbol"])
        .agg(
            ((pl.col("premium_dod") * pl.col("amt")).sum() / pl.col("amt").sum()).alias("prem"),
            pl.col("amt").sum().alias("bt_amt"),
            pl.len().alias("deals"),
            pl.col("buyer").str.contains("机构专用").max().alias("buy_inst"),
            pl.col("seller").str.contains("机构专用").max().alias("sell_inst"),
        )
        .with_columns(pl.col("date").cast(pl.Date))
    )


def load_panel() -> tuple[pl.DataFrame, pl.DataFrame]:
    files = [str(p) for p in sorted(KL.glob("date=*/part.parquet"))]
    px = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "close"])
        .filter(pl.col("date") >= pl.lit(START).str.to_date())
        .collect()
    )
    cal = px.select("date").unique().sort("date").with_row_index("didx")
    px = px.join(cal, on="date", how="left")
    return px, cal


def add_forward(ev: pl.DataFrame, px: pl.DataFrame, cal: pl.DataFrame) -> pl.DataFrame:
    ev = ev.join(cal, on="date", how="inner")
    ent = (
        px.select(["symbol", "didx", "open"])
        .with_columns((pl.col("didx") + 1).alias("didx"))
        .rename({"open": "entry_px"})
    )
    ev = ev.join(ent, on=["symbol", "didx"], how="left")
    for h in HORIZONS:
        ex = (
            px.select(["symbol", "didx", "close"])
            .with_columns((pl.col("didx") - h).alias("didx"))
            .rename({"close": f"c{h}"})
        )
        ev = ev.join(ex, on=["symbol", "didx"], how="left")
        ev = ev.with_columns((pl.col(f"c{h}") / pl.col("entry_px") - 1).alias(f"fwd{h}"))
    return ev


def add_group(d: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """按 keys 分组，用 ordinal rank 分五桶（Q1 = 折价最深）。"""
    r = pl.col("prem").rank(method="ordinal").over(keys)
    n = pl.col("prem").count().over(keys)
    return d.with_columns(
        (((r - 1) * 5 / n).floor().clip(0, 4) + 1).cast(pl.Int32).alias("g")
    )


def nw_t(x: np.ndarray, lag: int) -> float:
    """Newey-West t：日序列的 H 日收益高度重叠，必须用 lag=H-1 修正。"""
    x = x[~np.isnan(x)]
    n = x.size
    if n < 30:
        return float("nan")
    mu = x.mean()
    d = x - mu
    var = float((d * d).mean())
    s = var
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        s += 2.0 * w * float((d[k:] * d[:-k]).mean())
    s = max(s, 1e-12)
    return float(mu / np.sqrt(s / n))


def bucket_stats(ev: pl.DataFrame, h: int, by: str | None = None) -> list[dict]:
    col = f"fwd{h}"
    d = ev.filter(pl.col(col).is_not_null() & pl.col("prem").is_not_null())
    if d.height < 1000:
        return []
    d = d.with_columns((pl.col(col) - pl.col(col).mean().over("date")).alias("exc"))
    keys = ["date"] if by is None else ["date", by]
    d = add_group(d, keys)
    daily = d.group_by(["date", "g"]).agg(pl.col("exc").mean(), pl.len().alias("n")).sort(["g", "date"])
    res = []
    for g in sorted(daily["g"].unique().to_list()):
        s = daily.filter(pl.col("g") == g)["exc"]
        n = s.len()
        if n < 30:
            continue
        mean = float(s.mean())
        sd = float(s.std())
        res.append({
            "group": f"Q{g}", "n_days": n, "exc_bp": round(mean * 1e4, 1),
            "t_naive": round(mean / (sd / n ** 0.5), 2) if sd > 0 else None,
            "t_nw": round(nw_t(s.to_numpy(), lag=h - 1), 2),
        })
    return res


def yearly(ev: pl.DataFrame, h: int) -> list[dict]:
    col = f"fwd{h}"
    d = ev.filter(pl.col(col).is_not_null()).with_columns(
        (pl.col(col) - pl.col(col).mean().over("date")).alias("exc"),
        pl.col("date").dt.year().alias("y"),
    )
    d = add_group(d, ["date"])
    dd = (
        d.filter(pl.col("g").is_in([1, 5]))
        .group_by(["y", "g"]).agg(pl.col("exc").mean().alias("m")).sort(["y", "g"])
    )
    piv = dd.pivot(values="m", index="y", on="g")
    if "1" not in piv.columns or "5" not in piv.columns:
        return []
    piv = piv.with_columns(((pl.col("1") - pl.col("5")) * 1e4).alias("q1_minus_q5_bp"))
    return piv.select(["y", "q1_minus_q5_bp"]).drop_nulls().to_dicts()


def main() -> None:
    ev = load_events()
    px, cal = load_panel()
    ev = add_forward(ev, px, cal)
    print(f"股票-日事件: {ev.height}，{ev['date'].min()} → {ev['date'].max()}")

    rep: dict = {
        "events": ev.height,
        "date_min": str(ev["date"].min()),
        "date_max": str(ev["date"].max()),
        "prem_quantiles": {
            str(q): round(float(ev["prem"].quantile(q)), 4)
            for q in [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
        },
    }
    for h in HORIZONS:
        rep[f"fwd{h}_all"] = bucket_stats(ev, h)
        rep[f"fwd{h}_by_buyinst"] = bucket_stats(ev, h, by="buy_inst")
        rep[f"fwd{h}_yearly"] = yearly(ev, h)
        print(f"\n=== fwd{h} 五分位（Q1=最深折价，Q5=溢价）===")
        for r in rep[f"fwd{h}_all"]:
            print(f"  {r['group']}: {r['exc_bp']:+.1f}bp  t_naive={r['t_naive']}  t_nw={r['t_nw']}  days={r['n_days']}")
        print(f"  分年 Q1−Q5(bp): {[(r['y'], round(r['q1_minus_q5_bp'])) for r in rep[f'fwd{h}_yearly']]}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
