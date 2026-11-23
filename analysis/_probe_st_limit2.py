"""探针 2：主板 ST 的 5% 指纹在横截面上是否成立。

单票（600340）出现 -10.23% / +9.94% 的日波动，与「戴帽后 5% 限制」矛盾。
一个分位数不是分布，所以改看横截面：新规前最后一个月，主板当前 ST 股的
日涨跌幅极值分布。
"""
from __future__ import annotations

import glob

import polars as pl

QFQ = "data/archive/kline_daily_qfq"


def load_month(ym: str) -> pl.DataFrame:
    fs = glob.glob(f"{QFQ}/month={ym}/*.parquet")
    if not fs:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in fs], how="diagonal_relaxed").sort(["symbol", "date"])


def main() -> None:
    inst = pl.read_parquet("data/instruments/instruments.parquet")
    st = inst.filter(pl.col("name").str.contains("ST"))
    # 主板：60/000/001/002/003 vs 创业板 300/301、科创 688、北交所 8xx/4xx/920
    main_board = st.filter(
        pl.col("code").str.starts_with("60")
        | pl.col("code").str.starts_with("000")
        | pl.col("code").str.starts_with("001")
        | pl.col("code").str.starts_with("002")
        | pl.col("code").str.starts_with("003")
    )["symbol"].to_list()
    print(f"当前 ST {st.height} 只，其中主板 {len(main_board)} 只")

    for ym, label in (("2025-06", "2025-06 新规前"), ("2026-06", "2026-06 新规前"), ("2026-09", "2026-09 新规后")):
        df = load_month(ym)
        if df.is_empty():
            print(f"{label}: 无数据")
            continue
        df = df.with_columns([
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("r"),
        ])
        agg = (
            df.group_by("symbol")
            .agg([pl.len().alias("n"), pl.col("r").max().alias("rmax"), pl.col("r").abs().max().alias("absmax")])
            .filter(pl.col("n") >= 10)
        )
        s = agg.filter(pl.col("symbol").is_in(main_board))
        o = agg.filter(~pl.col("symbol").is_in(main_board))
        if s.is_empty():
            print(f"{label}: 主板 ST 无样本")
            continue
        q = [round(s["absmax"].quantile(x), 4) for x in (0.1, 0.25, 0.5, 0.75, 0.9)]
        print(f"{label}: 主板ST n={s.height} absmax分位={q} <=5.5%占比={round((s['absmax']<=0.055).mean(),3)}")
        print(f"    主板非ST n={o.height} absmax中位={round(o['absmax'].median(),4)}")


if __name__ == "__main__":
    main()
