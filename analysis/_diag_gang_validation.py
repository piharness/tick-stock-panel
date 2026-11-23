"""验证同伙信号的预测力 —— 「同伙席位联手买入」的票，未来是否更好。

同伙定义：lift>=100 的极强共现边（连通分量），同一 gang 内 >=2 个营业部
在 T 日同一票的买入榜上同时出现 = 同伙联手。

对比：同伙联手 vs 单席位 vs 无席位，未来 3/5 日收益与涨停率。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def build_gangs(net: pl.DataFrame, min_lift: float) -> pl.DataFrame:
    """lift>=min_lift 的边做并查集，返回 dept_code -> gang_id 映射。"""
    edges = net.filter(pl.col("lift") >= min_lift) \
        .select(["dept_code", "dept_code_2"]).to_numpy().tolist()
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    rows = [{"dept_code": c, "gang_id": find(c)} for c in parent]
    return pl.DataFrame(rows)


def main() -> None:
    net = pl.read_parquet(OUT / "youzi_seat_network.parquet")
    gangs = build_gangs(net, min_lift=100.0)
    gang_sizes = gangs.group_by("gang_id").len().sort("len", descending=True)
    print(f"同伙团伙 {gangs['gang_id'].n_unique()} 个，含 {gangs.height} 个营业部", flush=True)
    print("最大团伙规模：", gang_sizes.head(3).to_dicts())

    seats = pl.read_parquet(OUT / "youzi_seats_all.parquet")
    buy = seats.filter((pl.col("side") == "buy") & (pl.col("buy") > 0)).select([
        "symbol", "date", "dept_code", "dept_name", "buy",
    ]).with_columns(pl.col("date").str.to_date())

    # join gang，看每个 (symbol,date) 买入榜里各 gang 的席位数量
    buy = buy.join(gangs, on="dept_code", how="left")
    buy = buy.with_columns(pl.col("gang_id").fill_null(pl.col("dept_code")))  # 无 gang 的各自独立

    # 每个 (symbol,date)：买入榜的席位总数、最大 gang 的席位数量
    ev = buy.group_by(["symbol", "date"]).agg([
        pl.len().alias("n_seats"),
        pl.col("gang_id").n_unique().alias("n_gangs"),
    ])
    # 最大 gang 席位数的计算：先按 gang 聚合，再取 max
    per_gang = buy.group_by(["symbol", "date", "gang_id"]).agg(pl.len().alias("cnt"))
    max_gang = per_gang.group_by(["symbol", "date"]).agg(pl.col("cnt").max().alias("max_gang_size"))
    ev = ev.join(max_gang, on=["symbol", "date"], how="left")
    ev = ev.with_columns(pl.col("max_gang_size").fill_null(1))

    # 同伙联手 = 存在 gang 有 >=2 个席位
    ev = ev.with_columns((pl.col("max_gang_size") >= 2).alias("gang_together"))

    # join 未来收益
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2025-10"]
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "consecutive_limit_ups"])
        .collect().sort(["symbol", "date"])
    )
    kl = kl.with_columns([
        pl.col("close").shift(-3).over("symbol").alias("c3"),
        pl.col("close").shift(-5).over("symbol").alias("c5"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(5).over("symbol") >= 1).alias("f_lu5"),
    ])
    ev = ev.join(kl.select(["symbol", "date", "close", "c3", "c5", "f_lu5"]),
                 on=["symbol", "date"], how="left")
    ev = ev.with_columns([
        (pl.col("c3") / pl.col("close") - 1).alias("fwd3"),
        (pl.col("c5") / pl.col("close") - 1).alias("fwd5"),
    ]).drop_nulls(["fwd3"])

    print(f"\n上榜事件 {ev.height:,}，其中同伙联手 {int(ev['gang_together'].sum()):,} 个", flush=True)

    g = ev.group_by("gang_together").agg([
        pl.len().alias("n"),
        (pl.col("fwd3").mean() * 1e4).alias("fwd3_bp"),
        (pl.col("fwd5").mean() * 1e4).alias("fwd5_bp"),
        (pl.col("f_lu5").mean() * 1e4).alias("lu5_bp"),
    ]).sort("gang_together", descending=True)
    print("\n=== 同伙联手 vs 单席位（未来表现）===")
    print(g)

    # 细分：同伙联手 + 席位数量分档
    ev = ev.with_columns(
        pl.when(pl.col("max_gang_size") >= 3).then(pl.lit("≥3席位同伙"))
        .when(pl.col("max_gang_size") == 2).then(pl.lit("2席位同伙"))
        .otherwise(pl.lit("单席位")).alias("grp")
    )
    g2 = ev.group_by("grp").agg([
        pl.len().alias("n"),
        (pl.col("fwd3").mean() * 1e4).alias("fwd3_bp"),
        (pl.col("fwd5").mean() * 1e4).alias("fwd5_bp"),
        (pl.col("f_lu5").mean() * 1e4).alias("lu5_bp"),
    ]).sort("fwd3_bp", descending=True)
    print("\n=== 同伙规模分档 ===")
    print(g2)

    # 显著性：同伙联手 vs 单席位 fwd3
    import numpy as np
    t = ev.filter(pl.col("gang_together"))
    s = ev.filter(~pl.col("gang_together"))
    if t.height and s.height:
        d = float(t["fwd3"].mean() - s["fwd3"].mean()) * 1e4
        p1, p2 = float(t["fwd3"].mean()), float(s["fwd3"].mean())
        n1, n2 = t.height, s.height
        ph = (n1 * p1 + n2 * p2) / (n1 + n2)
        se = (ph * (1 - ph) * (1 / n1 + 1 / n2)) ** 0.5 if 0 < ph < 1 else None
        z = (p1 - p2) / se if se else float("nan")
        print(f"\n同伙联手 vs 单席位：fwd3 差 {d:+.1f}bp，z={z:+.2f}")

    # 落盘 gang 映射（供接入复用）
    gangs.write_parquet(OUT / "youzi_seat_gangs.parquet")
    ev.write_parquet(OUT / "youzi_gang_validation.parquet")
    print(f"\n写出 {OUT / 'youzi_seat_gangs.parquet'} 和 {OUT / 'youzi_gang_validation.parquet'}")


if __name__ == "__main__":
    main()
