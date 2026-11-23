"""龙虎榜营业部共现网络 —— 识别同伙资金。

思路：两个营业部若频繁在同一 (symbol, date) 的买入榜上同时出现，
     且显著超过随机共现的期望（lift 高），则很可能是一伙（协同席位/同一操盘手）。

lift 定义：lift(D1,D2) = C(D1,D2) × N / (n1 × n2)
  C = 共现次数；n1/n2 = 各自买入榜出现次数；N = 去重事件总数（symbol,date）。
  lift≈1 = 随机；lift 高 = 协同。

聚合席位（机构专用/深股通/沪股通/总部）出现次数巨大，会拉低与任何人的 lift，
所以 lift 排序天然把「真同伙」排在「通道共现」前面，但仍显式剔除最脏的聚合名。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(50)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)

# 聚合/通道席位：不是个体游资，剔除（名称含这些关键词）
AGG_KEYWORDS = ["机构专用", "深股通专用", "沪股通专用", "总部", "专用"]

MIN_DEG = 20   # 活跃阈值：买入榜出现 >= 20 次
MIN_LIFT = 4.0      # 展示阈值：lift >= 4 进共现对表
MIN_CLUSTER = 100.0  # 聚类阈值：lift >= 100 才是「极强共现」= 真同伙


def is_agg(name: str) -> bool:
    return any(k in name for k in AGG_KEYWORDS)


def main() -> None:
    s = pl.read_parquet(OUT / "youzi_seats_all.parquet")
    buy = s.filter((pl.col("side") == "buy") & (pl.col("buy") > 0)).select([
        "symbol", "date", "dept_code", "dept_name",
    ])
    # 剔除聚合席位
    buy = buy.with_columns(pl.col("dept_name").map_elements(is_agg, return_dtype=pl.Boolean).alias("_agg"))
    buy = buy.filter(~pl.col("_agg")).drop("_agg")
    print(f"买入事件（剔除聚合后）{buy.height:,} 条", flush=True)

    # 活跃度
    deg = buy.group_by("dept_code").agg([pl.len().alias("n"), pl.col("dept_name").first().alias("name")])
    active = deg.filter(pl.col("n") >= MIN_DEG)
    print(f"活跃营业部（>= {MIN_DEG} 次）{active.height} 个", flush=True)

    # 事件总数 N = 去重 (symbol, date)
    events = buy.select(["symbol", "date"]).unique()
    N = events.height
    print(f"去重事件 N = {N:,}", flush=True)

    # 只保留活跃营业部的买入事件，self-join 得共现对
    b = buy.join(active.select(["dept_code"]), on="dept_code", how="semi")
    pairs = (
        b.join(b, on=["symbol", "date"], how="inner", suffix="_2")
        .filter(pl.col("dept_code") < pl.col("dept_code_2"))
        .select(["dept_code", "dept_code_2", "dept_name", "dept_name_2"])
    )
    co = pairs.group_by(["dept_code", "dept_code_2"]).agg([
        pl.len().alias("c"),
        pl.col("dept_name").first().alias("name1"),
        pl.col("dept_name_2").first().alias("name2"),
    ])

    # join 各自活跃度，算 lift
    co = co.join(
        active.select(["dept_code", pl.col("n").alias("n1")]), on="dept_code", how="left"
    ).join(
        active.select([pl.col("dept_code").alias("dept_code_2"), pl.col("n").alias("n2")]),
        on="dept_code_2", how="left"
    )
    co = co.with_columns((pl.col("c") * N / (pl.col("n1") * pl.col("n2"))).alias("lift"))
    co = co.filter(pl.col("lift") >= MIN_LIFT).sort("lift", descending=True)
    print(f"\n共现对（lift >= {MIN_LIFT}）{co.height} 个", flush=True)

    print("\n=== Top 30 同伙营业部对 ===")
    for r in co.head(30).to_dicts():
        print(f"  {r['lift']:6.1f}x  {r['name1'][:22]:22s} × {r['name2'][:22]:22s}  共现{r['c']:>3}  "
              f"({r['n1']:>4}次 × {r['n2']:>4}次)")

    # 连通分量聚类：lift>=MIN_CLUSTER 的边（极强共现），做并查集
    strong = co.filter(pl.col("lift") >= MIN_CLUSTER)
    print(f"\n极强共现边（lift >= {MIN_CLUSTER}）{strong.height} 个", flush=True)
    edges = strong.select(["dept_code", "dept_code_2"]).to_numpy().tolist()
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        union(a, b)

    groups: dict[str, list[str]] = {}
    for code in set([e for pair in edges for e in pair]):
        groups.setdefault(find(code), []).append(code)

    # 名字映射
    nm = {r["dept_code"]: r["name1"] for r in co.select(["dept_code", "name1"]).to_dicts()}
    nm.update({r["dept_code_2"]: r["name2"] for r in co.select(["dept_code_2", "name2"]).to_dicts()})

    print("\n=== 同伙团伙（连通分量，>=2 个营业部）===")
    gangs = [(len(v), k) for k, v in groups.items() if len(v) >= 2]
    gangs.sort(reverse=True)
    for size, k in gangs:
        names = [nm.get(c, c)[:16] for c in groups[k]]
        print(f"  [{size}人] {', '.join(names)}")

    co.write_parquet(OUT / "youzi_seat_network.parquet")
    print(f"\n写出 {OUT / 'youzi_seat_network.parquet'}")


if __name__ == "__main__":
    main()
