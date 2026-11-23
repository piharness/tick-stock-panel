"""探针：主板 ST 的 5% 涨跌幅指纹是否可用于反推历史 ST 区间。

背景（真实规则，2026-07-06 起生效）：
  沪深主板风险警示股涨跌幅 5% -> 10%，与普通股一致；创业板/科创板 ST 仍 20%，
  北交所 *ST 仍 30%。
因此「日涨跌幅上限 <= 5.2%」只在 2001-2026.07 的**主板**上成立，是一条
可以用于重建历史 ST 面板的价格指纹；2026-07 之后该指纹消失。

本脚本只验证指纹本身，不改任何生产数据。
"""
from __future__ import annotations

import glob
from datetime import date

import polars as pl

QFQ = "data/archive/kline_daily_qfq"


def load_symbol(symbol: str, start: date, end: date) -> pl.DataFrame:
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{QFQ}/month={y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    parts = []
    for d in months:
        for p in glob.glob(d + "/*.parquet"):
            try:
                sub = pl.read_parquet(p)
            except Exception:
                continue
            sub = sub.filter(pl.col("symbol") == symbol)
            if not sub.is_empty():
                parts.append(sub)
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="diagonal_relaxed").sort("date").unique(subset=["date"], keep="last")


def seg_stats(d: pl.DataFrame, a: date, b: date, label: str) -> None:
    s = d.filter((pl.col("date") >= a) & (pl.col("date") <= b))
    if s.is_empty():
        print(f"{label}: 空")
        return
    over = s.filter(pl.col("r") > 0.052)
    print(
        f"{label}: n={s.height} rmax={s['r'].max():.4f} rmin={s['r'].min():.4f} "
        f">5.2% 天数={over.height}"
    )
    if not over.is_empty():
        print("   ", over.select(["date", "close", "r"]).rows()[:5])


def main() -> None:
    d = load_symbol("600340.SH", date(2025, 10, 1), date(2026, 11, 16))
    print("rows", d.height, d["date"].min() if not d.is_empty() else None)
    d = d.with_columns((pl.col("close") / pl.col("close").shift(1) - 1).alias("r"))
    seg_stats(d, date(2025, 10, 1), date(2026, 1, 13), "戴帽前(应 10%)")
    seg_stats(d, date(2026, 1, 14), date(2026, 7, 3), "戴帽后·新规前(应 5%)")
    seg_stats(d, date(2026, 7, 6), date(2026, 11, 16), "新规后(应 10%)")


if __name__ == "__main__":
    main()
