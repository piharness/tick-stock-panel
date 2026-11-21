"""L1 上的可行域实测量：逐票毛收益 vs 逐票一手成本，求「翻正的最小仓位粒度」。

## 与 `_diag_feasible_region.py` 的分工

前者算**成本地板**（纯算术，不碰收益）。本脚本把 L1 的真实成交池接上去，
回答一个此前无法回答的问题：

    已知 L1 的日等权毛收益约 96bp，那么在「每票买 k 手」这个粒度下，
    扣掉真实的 min-commission 之后，还剩多少？

本仓库此前所有 L1 净收益都假定成本 = 常数 19bp（V→∞ 比例口径）。
一旦把 V 显式化，成本变成 c(V)=2·max(r, m/V)+固定项，
而 u（一手金额）的分布右偏 ⇒ **E[c(u)] ≫ c(E[u])**。

用法：
    python analysis/_diag_feasible_region_l1.py
    python analysis/_diag_feasible_region_l1.py --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort as l1  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_feasible_region_l1_result.json"

STAMP, TRANSFER, LAM = 5.0, 0.1, 5.0
FIXED_BP = STAMP + 2 * TRANSFER + 2 * LAM  # 股票往返的非佣金项 = 15.2bp
TIERS = [
    # 用户实际账户：万0.691 全佣，单笔不足 5 元按 5 元收（不免五）。
    # 这是唯一「已确认」的档，其余是谈判情景。
    ("实际_万0.691_五元", 0.0000691, 5.0),
    ("万3_五元", 0.0003, 5.0),
    ("万2.5_五元", 0.00025, 5.0),
    ("万1_五元", 0.0001, 5.0),
    ("万1_免五", 0.0001, 0.0),
    ("万0.5_免五", 0.00005, 0.0),
]
#: 每票买几手
K_GRID = [1, 2, 3, 5, 10, 20, 50, 100]


def load_cells() -> pl.DataFrame:
    cells = l1.cohort_cells(DATA)
    print(f"cohort cells: {cells.height} / {cells['date'].n_unique()} 日", flush=True)
    # 只读命中的入场日分区（全树 15M 行，没必要全扫）
    files = [DATA / "kline_daily_enriched" / f"date={d}" / "part.parquet"
             for d in sorted(cells["date"].unique().to_list())]
    files = [f for f in files if f.exists()]
    px = (
        pl.concat([pl.read_parquet(f, columns=["symbol", "date", "open", "close", "raw_close"])
                   for f in files])
        .with_columns(pl.col("date").cast(pl.Utf8).alias("d"))
    )
    df = cells.join(px, left_on=["date", "symbol"], right_on=["d", "symbol"], how="inner")
    # enriched 无 raw_open：用当日复权比还原原始开盘价
    return df.drop_nulls().with_columns(
        (pl.col("close") / pl.col("open") - 1.0).alias("gross"),
        (pl.col("open") * pl.col("raw_close") / pl.col("close") * 100.0).alias("unit"),
    ).filter((pl.col("unit") > 0) & pl.col("gross").is_finite())


def build() -> dict:
    df = load_cells()
    g = df["gross"].to_numpy()
    u = df["unit"].to_numpy()
    out: dict = {
        "n_cells": int(df.height),
        "n_days": int(df["date"].n_unique()),
        "gross_bp_mean": round(float(g.mean()) * 1e4, 2),
        "gross_bp_median": round(float(np.median(g)) * 1e4, 2),
        "unit_med": round(float(np.median(u)), 0),
        "unit_p90": round(float(np.percentile(u, 90)), 0),
        "note": "gross = T+1 close/open − 1；unit = 一手金额 = 100×raw_open",
    }

    # A. 一手成本与毛收益的联合：是否负相关（低成本票是否恰好低收益）
    c1 = 2 * np.maximum(0.00025 * u, 5.0) / u * 1e4 + FIXED_BP
    out["corr_gross_cost1lot"] = round(float(np.corrcoef(g, c1)[0, 1]), 4)
    out["corr_unit_cost1lot"] = round(float(np.corrcoef(u, c1)[0, 1]), 4)

    # B. 核心表：粒度 k × 费率档 → 净期望
    rows = []
    for k in K_GRID:
        V = k * u
        for name, r, m in TIERS:
            c = 2 * np.maximum(r * V, m) / V * 1e4 + FIXED_BP
            net = g * 1e4 - c
            rows.append({
                "k_lots": k,
                "tier": name,
                "V_med": round(float(np.median(V)), 0),
                "cost_bp_ew": round(float(c.mean()), 1),
                "net_bp_ew": round(float(net.mean()), 1),
                "net_bp_med": round(float(np.median(net)), 1),
                "frac_pos": round(float((net > 0).mean()), 4),
            })
    out["B_grid"] = rows

    # C. 翻正临界粒度（每档）
    thr = {}
    for name, r, m in TIERS:
        hit = [row["k_lots"] for row in rows if row["tier"] == name and row["net_bp_ew"] > 0]
        thr[name] = min(hit) if hit else None
    out["C_k_breakeven"] = thr

    # D. 分价位：低价股一手成本
    qs = np.percentile(u, [0, 25, 50, 75, 100])
    buckets = []
    for lo, hi in zip(qs[:-1], qs[1:]):
        mk = (u >= lo) & (u <= hi)
        if mk.sum() < 50:
            continue
        c = c1[mk]
        buckets.append({
            "unit_range": [round(float(lo)), round(float(hi))],
            "n": int(mk.sum()),
            "cost1lot_bp": round(float(c.mean()), 1),
            "gross_bp": round(float(g[mk].mean()) * 1e4, 1),
            "net1lot_bp": round(float((g[mk] * 1e4 - c).mean()), 1),
        })
    out["D_by_unit_bucket"] = buckets

    # E. 日度组合口径：逐笔池化忽略同日相关性，会严重高估显著性。
    #    正确单位 = 每日组合净收益序列（当日 picks 等权，再对日求均值）。
    dates = df["date"].to_numpy()
    uniq = np.unique(dates)
    day_idx = {d: i for i, d in enumerate(uniq)}
    gi = np.array([day_idx[d] for d in dates])
    n_grid = [1, 2, 5, 10, 20, 50]
    rng = np.random.default_rng(20261121)
    rowsE = []
    for k in n_grid:
        V = k * u
        for name, r, m in TIERS:
            c = 2 * np.maximum(r * V, m) / V * 1e4 + FIXED_BP
            net = g * 1e4 - c
            s = np.bincount(gi, weights=net)
            n = np.bincount(gi)
            daily = s / n
            mu, sd = daily.mean(), daily.std(ddof=1)
            t = mu / (sd / np.sqrt(len(daily)))
            # 日块 bootstrap（按日重采样，保留同日相关性）
            bs = np.array([daily[rng.integers(0, len(daily), len(daily))].mean()
                           for _ in range(400)])
            rowsE.append({
                "k_lots": k, "tier": name,
                "daily_net_bp": round(float(mu), 2),
                "t": round(float(t), 2),
                "ci95": [round(float(np.percentile(bs, 2.5)), 2),
                         round(float(np.percentile(bs, 97.5)), 2)],
                "n_days": int(len(daily)),
            })
    out["E_daily_portfolio"] = rowsE

    # F. 尾部依赖：净期望的绝对分解（不做除法 —— 总和接近 0 时占比无意义）
    V = 1 * u
    c = 2 * np.maximum(0.00025 * V, 5.0) / V * 1e4 + FIXED_BP
    net = g * 1e4 - c
    order = np.argsort(-net)
    n = len(net)
    out["F_tail"] = {
        "ew_net_bp_all": round(float(net.mean()), 2),
        "ew_net_bp_drop_top1pct": round(float(np.delete(net, order[: n // 100]).mean()), 2),
        "ew_net_bp_drop_top5pct": round(float(np.delete(net, order[: n // 20]).mean()), 2),
        "支撑量_top1pct_bp": round(float(net.mean() - np.delete(net, order[: n // 100]).mean()), 2),
        "支撑量_top5pct_bp": round(float(net.mean() - np.delete(net, order[: n // 20]).mean()), 2),
        "口径": "k=1 手、实际万0.691五元；正号部分全部来自右尾，剔除即翻负",
    }
    # F2 剔尾后按 k 重算：成本是每票的常数平移，但排序会随成本的票间差异改变
    f2 = []
    for k in (1, 2, 5, 20, 50):
        V = k * u
        ck = 2 * np.maximum(0.0000691 * V, 5.0) / V * 1e4 + FIXED_BP
        nk = g * 1e4 - ck
        ok = np.argsort(-nk)
        f2.append({
            "k_lots": k,
            "ew_net_bp": round(float(nk.mean()), 2),
            "drop_top1pct": round(float(np.delete(nk, ok[: n // 100]).mean()), 2),
            "drop_top5pct": round(float(np.delete(nk, ok[: n // 20]).mean()), 2),
        })
    out["F2_tail_by_k"] = f2
    # G. 名义费率 vs 有效费率：最低佣金把万0.691 变成多少
    #    「全佣万0.691」只在 V ≥ m/r 时才成立；V* = 5/0.0000691 = 7.24 万元。
    R_REAL, M_REAL = 0.0000691, 5.0
    g_eff = []
    for k in [1, 2, 5, 10, 20, 50]:
        V = k * u
        per_side_bp = np.maximum(R_REAL * V, M_REAL) / V * 1e4
        g_eff.append({
            "k_lots": k,
            "V_med": round(float(np.median(V)), 0),
            "effective_wan_per_side": round(float(np.median(per_side_bp)), 2),
            "nominal_wan": round(R_REAL * 1e4, 3),
            "ratio": round(float(np.median(per_side_bp) / (R_REAL * 1e4)), 1),
        })
    out["G_effective_rate"] = g_eff
    out["G_v_star_yuan"] = round(M_REAL / R_REAL, 0)
    # 让名义费率生效所需 AUM = 每日票数 × V*（每票都做到 V*，不是 × median(u)）
    out["G_aum_to_reach_nominal_wan"] = round(40090 / 4602 * (M_REAL / R_REAL) / 1e4, 1)

    # H. 两个口径的毛收益必须分开报：池化 vs 日度等权
    #    日度等权给每日同权，池化按只-日加权；后者被「picks 多的日子」压低。
    s = np.bincount(gi, weights=g * 1e4)
    n = np.bincount(gi)
    out["H_gross_caliber"] = {
        "pooled_bp": round(float((g * 1e4).mean()), 2),
        "daily_ew_bp": round(float((s / n).mean()), 2),
        "差_bp": round(float((s / n).mean()) - float((g * 1e4).mean()), 2),
        "说明": "池化按只-日加权、日度等权按日加权；净期望表用日度等权，勿与池化毛收益相减",
    }

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    cur = build()
    if a.write:
        OUT.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {OUT}")
    print(json.dumps(cur, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
