"""诊断二：博弈角色主场分层是否跨期稳定，以及 level 在球场内是否还有增量。

单期回测（2026-10-23 → 11-20）显示 arena 的 p_dd20 从 18.7% 单调升到 57.8%，
但那是暴跌窗口，可能是 regime 特有。纪律：单期结论不可判定。

本脚本跑多个基期（每月一个交易日），检查：
  1. arena 的 p_dd20 / 中位收益排序是否跨期同向；
  2. 同一 arena 内，level 是否还携带增量（若没有，level 就只是标签）。
"""
from __future__ import annotations

import sys

import polars as pl

from _diag_citizenship_arena import ENRICHED, cross_section, load_inst, quadrant

HORIZON = 20


def main() -> int:
    inst = load_inst()
    days = sorted(p.name[5:] for p in ENRICHED.glob("date=*"))
    # 基期：从 2024-01 起每月取第一个交易日；预留 20 个交易日做前瞻
    bases: list[str] = []
    seen: set[str] = set()
    for d in days[: -HORIZON - 1]:
        ym = d[:7]
        if ym not in seen:
            seen.add(ym)
            bases.append(d)
    bases = [b for b in bases if b >= "2024-01"]
    print(f"基期数 {len(bases)}：{bases[0]} .. {bases[-1]}\n")

    rows: list[dict] = []
    lvl_rows: list[dict] = []
    for bi, base in enumerate(bases):
        idx = days.index(base)
        fwd = days[idx + HORIZON]
        cur = quadrant(cross_section(base, inst), 0.50, 0.75)
        if cur.is_empty():
            continue
        f = pl.read_parquet(ENRICHED / f"date={fwd}" / "part.parquet").select(
            ["symbol", "close"]
        ).rename({"close": "close_fwd"})
        res = cur.join(f, on="symbol", how="inner").with_columns(
            (pl.col("close_fwd") / pl.col("close") - 1).alias("ret")
        )
        for (arena,), g in res.group_by(["arena"]):
            rows.append({
                "base": base, "fwd": fwd, "arena": arena, "n": g.height,
                "med": float(g["ret"].median() or 0),
                "dd": float((g["ret"] < -0.2).mean() or 0),
            })
        for (arena, level), g in res.group_by(["arena", "level"]):
            lvl_rows.append({
                "base": base, "arena": arena, "level": level, "n": g.height,
                "med": float(g["ret"].median() or 0),
                "dd": float((g["ret"] < -0.2).mean() or 0),
            })
        print(f"  [{bi+1}/{len(bases)}] {base} → {fwd}  N={res.height}")

    panel = pl.DataFrame(rows)
    print("\n=== 按 arena：跨期 dd 概率与中位收益 ===")
    print(
        panel.group_by("arena").agg([
            pl.len().alias("期数"),
            pl.col("dd").mean().round(4).alias("dd 均值"),
            pl.col("dd").min().round(4).alias("dd 最低"),
            pl.col("dd").max().round(4).alias("dd 最高"),
            (pl.col("med") * 100).mean().round(3).alias("中位收益均值%"),
            ((pl.col("med") > 0).mean()).round(3).alias("正收益期占比"),
        ]).sort("dd 均值")
    )
    # 排序一致性：每期内 4 个 arena 按 dd 升序的秩，是否总相同
    rank = panel.with_columns(
        pl.col("dd").rank("ordinal").over("base").alias("rk")
    ).group_by("arena").agg([
        pl.col("rk").mean().round(2).alias("平均秩(1=最安全)"),
        pl.col("rk").std().round(2).alias("秩标准差"),
    ]).sort("平均秩(1=最安全)")
    print("\n=== 排序稳定性（dd 升序秩，n=4 格）===")
    print(rank)

    lv = pl.DataFrame(lvl_rows)
    print("\n=== 每个 arena 内 level 的增量（跨期 dd 均值）===")
    print(
        lv.group_by(["arena", "level"]).agg([
            pl.len().alias("期数"),
            pl.col("dd").mean().round(3).alias("dd均值"),
            (pl.col("med") * 100).mean().round(2).alias("中位%"),
        ]).sort(["arena", "dd均值"])
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
