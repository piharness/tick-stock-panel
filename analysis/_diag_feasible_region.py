"""净期望 > 0 的可行域 —— 在 {标的类型 × 时段 × 信号周期 × 费率档 × 仓位粒度} 上求交。

## 为什么要单开一个脚本

`app/backtest/engine.py::MatcherConfig` 的成本只有比例项：
`buy_cost_pct = commission + slippage`，**与下单金额 V 无关**。
这意味着本仓库所有 `net` 数字都是 V→∞ 的极限值，而「仓位粒度」在系统里
根本不是一个变量。本脚本把 V 显式化，把五维空间压到两个可算的边界上。

## 模型（全部闭式，无随机数）

单笔往返成本（以名义额 V 归一，单位 bp）：

    c(V) = 1e4 · [ 2·max(r·V, m) + s·V + 2·t·V + 2·λ·V ] / V
         = 2·max(r, m/V)·1e4 + s_bp + 2·t_bp + 2·λ_bp

    r 佣金率, m 单笔最低佣金(元), s 卖出印花税, t 单边过户费, λ 单边滑点/价差

可成交的 V 不是连续的：V ∈ {n·u}，u = 一手金额 = lot × price。
（主板/创业板 100 股整数倍；科创板 200 股起、1 股递增；ETF 100 份；
可转债 10 张、每张面值 100 元）

可行判据（三个约束同时成立）：

    (1) 成本约束   g > c(V)                      ← 费率档/粒度/标的
    (2) 粒度约束   V ≥ u 且 V ∈ 格点              ← 价格 × 板块
    (3) 冲击约束   V ≤ V_max = ADV·[(g − c_prop)/(2Yσ)]²   ← 流动性

空集分三类：
    E1 结构性空  g ≤ c_prop（比例地板），任何 V 都救不了 → 费率档再低也无效
    E2 规模性空  g > c_prop 但 V_min > V_max 或 V_min > u 不可达
    E3 容量性空  V_min < V_max 但 AUM < N·V_min（分散度买不起）

用法：
    python analysis/_diag_feasible_region.py            # 打印
    python analysis/_diag_feasible_region.py --write    # 写 _result.json
    python analysis/_diag_feasible_region.py --check    # 漂移 >5% 退出 1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_feasible_region_result.json"
ANCHOR = "2026-11-20"

# ── 制度参数（硬约束，不可协商）─────────────────────────────────
# 印花税：2023-08-28 起卖出单边 0.05%；ETF/可转债免征。
STAMP_BP = {"stock": 5.0, "etf": 0.0, "cb": 0.0}
# 过户费：沪深 A 股 0.001%/边（中国结算，双向）；ETF/可转债免征。
TRANSFER_BP = {"stock": 0.1, "etf": 0.0, "cb": 0.0}

# ── 费率档（可协商，作为轴而非事实）────────────────────────────
# (标签, 佣金率, 单笔最低佣金元)
TIERS = [
    # 用户实际账户：万0.691 全佣、单笔不足 5 元按 5 元收。V* = 72,359 元。
    ("实际_万0.691_五元", 0.0000691, 5.0),
    ("万3_五元", 0.0003, 5.0),
    ("万2.5_五元", 0.00025, 5.0),
    ("万1.5_五元", 0.00015, 5.0),
    ("万1_五元", 0.0001, 5.0),
    ("万1_免五", 0.0001, 0.0),
    ("万0.854_免五", 0.0000854, 0.0),  # 规费底线：经手费万0.487+证管费万0.054+过户
    ("万0.5_免五", 0.00005, 0.0),
]

LAMBDA_BP = [0.0, 2.0, 5.0, 10.0]  # 单边滑点/价差，5bp 为本仓库预注册值
G_BP = [0.0, 10.0, 20.0, 40.0, 96.0]  # 单笔毛期望，96bp = L1 实测日等权

IMPACT_Y = 1.0  # 平方根冲击律系数（模型假设，非实测）


def board_of(sym: str) -> str:
    if sym.startswith(("688", "689")):
        return "STAR"
    if sym.startswith(("8", "4")):
        return "BSE"
    if sym.startswith(("300", "301")):
        return "GEM"
    return "MAIN"


def lot_of(board: str, instr: str) -> tuple[int, int]:
    """(一手股数/份数/张数, 量化步长)。科创板 200 起 1 股递增。"""
    if instr == "cb":
        return 10, 10
    if board == "STAR":
        return 200, 1
    if board == "BSE":
        return 100, 1
    return 100, 100


def cost_bp(V: float, r: float, m: float, s_bp: float, t_bp: float, lam_bp: float) -> float:
    comm = 2.0 * max(r * V, m)
    return 1e4 * (comm + (s_bp + 2.0 * t_bp + 2.0 * lam_bp) * 1e-4 * V) / V


def prop_floor_bp(r: float, s_bp: float, t_bp: float, lam_bp: float) -> float:
    """V→∞ 的比例地板。"""
    return 2.0 * r * 1e4 + s_bp + 2.0 * t_bp + 2.0 * lam_bp


def v_min(g_bp: float, r: float, m: float, s_bp: float, t_bp: float, lam_bp: float) -> float:
    """满足 g > c(V) 的最小 V。可达 iff g > prop_floor。"""
    denom_bp = g_bp - (s_bp + 2.0 * t_bp + 2.0 * lam_bp + 2.0 * r * 1e4)
    if denom_bp <= 0:
        return math.inf
    return 2.0 * m * 1e4 / denom_bp


def v_max(g_bp: float, r: float, s_bp: float, t_bp: float, lam_bp: float,
          adv: float, sigma_d: float, y: float = IMPACT_Y) -> float:
    """冲击约束下的最大 V：2Yσ√(V/ADV) ≤ g − c_prop。"""
    if sigma_d <= 0 or not np.isfinite(adv) or adv <= 0:
        return math.inf
    room = (g_bp - prop_floor_bp(r, s_bp, t_bp, lam_bp)) * 1e-4
    if room <= 0:
        return 0.0
    return adv * (room / (2.0 * y * sigma_d)) ** 2


# ── 数据 ────────────────────────────────────────────────────────
def load_cross_section() -> pl.DataFrame:
    """最新交易日横截面 + 60 日已实现波动 + 20 日均成交额。"""
    dates = sorted(p.name for p in (DATA / "kline_daily_enriched").glob("date=*"))
    use = dates[-61:]
    frames = []
    for d in use:
        f = DATA / "kline_daily_enriched" / d / "part.parquet"
        if f.exists():
            frames.append(pl.read_parquet(f, columns=["symbol", "date", "raw_close", "amount"]))
    panel = pl.concat(frames).drop_nulls().sort(["symbol", "date"])
    stats = (
        panel.group_by("symbol")
        .agg(
            pl.col("raw_close").last().alias("px"),
            pl.col("amount").tail(20).mean().alias("adv20"),
            pl.col("raw_close").pct_change().tail(60).std().alias("sigma_d"),
            pl.len().alias("n_obs"),
        )
        .filter((pl.col("n_obs") >= 40) & (pl.col("px") > 0))
    )
    stats = stats.with_columns(
        pl.col("symbol").map_elements(board_of, return_dtype=pl.Utf8).alias("board")
    )
    stats = stats.with_columns(
        pl.struct(["board"]).map_elements(
            lambda s: lot_of(s["board"], "stock")[0], return_dtype=pl.Int64
        ).alias("lot")
    )
    return stats.with_columns((pl.col("lot") * pl.col("px")).alias("unit"))


def load_etf_cb() -> dict:
    e = pl.read_parquet(DATA / "kline_etf_daily" / f"date={ANCHOR}" / "part.parquet")
    e = e.select(pl.col("close").alias("px"), pl.col("amount").alias("adv20")).filter(pl.col("px") > 0)
    cb = (
        pl.scan_parquet(DATA / "cb_5min_sina" / "cb_5min.parquet")
        .group_by("symbol")
        .agg(pl.col("close").last().alias("px_100"), pl.col("amount").sum().alias("adv20"))
        .collect()
    )
    return {
        "etf": {"px_med": float(e["px"].median()), "adv_med": float(e["adv20"].median()), "n": e.height},
        "cb": {
            # 可转债报价是「每 100 元面值」的价格，一手 = 10 张 = 10×100 面值
            "px_med": float(cb["px_100"].median()) * 10.0 / 100.0,
            "adv_med": float(cb["adv20"].median()),
            "n": cb.height,
        },
    }


# ── 主计算 ──────────────────────────────────────────────────────
def build() -> dict:
    cs = load_cross_section()
    unit = cs["unit"].to_numpy()
    px = cs["px"].to_numpy()
    adv = cs["adv20"].to_numpy()
    sig = cs["sigma_d"].to_numpy()
    out: dict = {"anchor": ANCHOR, "universe": int(cs.height)}

    # T0 价格与一手金额分布
    qs = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    out["price_pct"] = {str(q): round(float(np.percentile(px, q)), 2) for q in qs}
    out["unit_pct"] = {str(q): round(float(np.percentile(unit, q)), 0) for q in qs}
    out["unit_mean"] = round(float(unit.mean()), 0)
    out["unit_sum"] = round(float(unit.sum()), 0)
    out["sigma_pct"] = {str(q): round(float(np.percentile(sig, q) * 100), 2) for q in qs}
    out["adv_pct"] = {str(q): round(float(np.percentile(adv, q)), 0) for q in qs}

    # T1 一手成本地板：V = 一手金额时，往返成本（bp）。
    # 注意 E[c] ≠ c(E[u])：c 对 1/V 凸，1e4·m/u 被低价股主导，必须逐票算再平均。
    t1 = []
    for name, r, m in TIERS:
        row = {"tier": name}
        for instr in ("stock", "etf", "cb"):
            per = np.array([cost_bp(u, r, m, STAMP_BP[instr], TRANSFER_BP[instr], 5.0) for u in unit])
            row[instr] = round(float(per.mean()), 1)
            row[instr + "_med"] = round(float(np.median(per)), 1)
        t1.append(row)
    out["t1_one_lot_cost_bp"] = t1
    out["t1_note"] = "mean/med 均为逐票成本再聚合；stock 列含印花 5bp+过户 0.2bp+滑点 10bp"

    # T2 临界单笔额 V* = m/r（最低佣金不再绑定的位置）
    out["t2_v_star"] = {name: (round(m / r, 0) if r > 0 else 0.0) for name, r, m in TIERS}

    # T3 比例地板（V→∞）
    out["t3_prop_floor_bp"] = {
        name: {instr: round(prop_floor_bp(r, STAMP_BP[instr], TRANSFER_BP[instr], 5.0), 1)
               for instr in ("stock", "etf", "cb")}
        for name, r, m in TIERS
    }

    # T4 V_min（元）随毛期望变化：股票；免五档不受佣金约束 ⇒ 只有整手下限
    t4 = []
    for g in G_BP:
        row = {"g_bp": g}
        for name, r, m in TIERS:
            v = v_min(g, r, m, STAMP_BP["stock"], TRANSFER_BP["stock"], 5.0)
            if m == 0 and math.isinf(v) is False and v == 0.0:
                row[name] = "lot_bound"
            else:
                row[name] = None if math.isinf(v) else round(v, 0)
        t4.append(row)
    out["t4_v_min_stock"] = t4

    # T5 关键：一手买得起吗？—— V=一手金额时的净期望（股票，λ=5），逐票算再等权
    t5 = []
    for g in G_BP:
        row = {"g_bp": g}
        for name, r, m in TIERS:
            per = g - np.array([cost_bp(u, r, m, 5.0, 0.1, 5.0) for u in unit])
            row[name] = {
                "ew_net_bp": round(float(per.mean()), 1),
                "med_net_bp": round(float(np.median(per)), 1),
                "frac_positive": round(float(np.mean(per > 0)), 4),
            }
        t5.append(row)
    out["t5_one_lot_net"] = t5

    # T5b V 扫描：费率档何时才开始起作用（V* 的负控）
    t5b = []
    for V in (1_000, 5_000, 20_000, 50_000, 100_000, 1_000_000):
        row = {"V_yuan": V}
        for name, r, m in TIERS:
            row[name] = round(cost_bp(V, r, m, 5.0, 0.1, 5.0), 1)
        t5b.append(row)
    out["t5b_cost_vs_V_bp"] = t5b

    # T6 分格：g × 费率档 → 达标标的分位占比（股票，λ=5，一手起）
    t6 = []
    for g in (10.0, 20.0, 40.0, 96.0):
        for name, r, m in TIERS:
            costs = np.array([cost_bp(u, r, m, 5.0, 0.1, 5.0) for u in unit])
            t6.append({
                "g_bp": g, "tier": name,
                "frac_ok": round(float(np.mean(g - costs > 0)), 4),
                "median_net_bp": round(float(np.median(g - costs)), 1),
            })
    out["t6_frac_universe_ok"] = t6

    # T7 AUM 下限：每股至少买 ceil(V_min/u) 手 ⇒ AUM ≥ Σ max(u, ⌈V_min/u⌉·u)
    t7 = []
    for g in (20.0, 40.0, 96.0):
        for name, r, m in TIERS:
            vm = v_min(g, r, m, 5.0, 0.1, 5.0)
            if math.isinf(vm):
                t7.append({"g_bp": g, "tier": name, "aum_min_yi": None, "note": "E1 结构性空"})
                continue
            need = np.maximum(unit, np.ceil(vm / unit) * unit)
            # 该 AUM 下逐票成本（用实际下单额 need）
            per = g - np.array([cost_bp(v, r, m, 5.0, 0.1, 5.0) for v in need])
            t7.append({
                "g_bp": g, "tier": name,
                "aum_min_yi": round(float(need.sum()) / 1e8, 4),
                "ew_net_bp": round(float(per.mean()), 1),
                "n_lots_med": round(float(np.median(np.ceil(vm / unit))), 1) if vm > 0 else 1.0,
            })
    out["t7_aum_floor_full_universe"] = t7
    out["t7_notes"] = {
        "universe_n": int(cs.height),
        "sum_one_lot_yi": round(float(unit.sum()) / 1e8, 4),
        "说明": "全市场等权建仓所需最小资金；N 只组合的 AUM 下限 ≈ (N/n)*该值，非线性",
    }

    # T10 环形可行域：V_min 与 V_max 是否相交（股票，λ=5，σ 取中位）
    t10 = []
    sig_med = float(np.percentile(sig, 50))
    for g in (20.0, 40.0, 96.0):
        for band, sl in (("p10", 10), ("p50", 50), ("p90", 90)):
            a = float(np.percentile(adv, sl))
            for name, r, m in TIERS:
                lo = v_min(g, r, m, 5.0, 0.1, 5.0)
                hi = v_max(g, r, 5.0, 0.1, 5.0, a, sig_med)
                if math.isinf(lo):
                    verdict = "E1_structural"
                elif hi <= 0:
                    verdict = "E1_structural"
                elif lo >= hi:
                    verdict = "E2_no_window"
                else:
                    verdict = "feasible"
                t10.append({
                    "g_bp": g, "adv_band": band, "tier": name,
                    "V_min": None if math.isinf(lo) else round(lo, 0),
                    "V_max": round(hi, 0),
                    "width_x": None if math.isinf(lo) or lo == 0 else round(hi / lo, 2),
                    "verdict": verdict,
                })
    out["t10_annulus"] = t10

    # T8 冲击上界：V_max（万元）按 ADV 与 σ 分位
    t8 = []
    for band, sl in (("ADV_p10", 10), ("ADV_p25", 25), ("ADV_p50", 50), ("ADV_p75", 75), ("ADV_p90", 90)):
        a = float(np.percentile(adv, sl))
        s = float(np.percentile(sig, 50))
        row = {"adv_band": band, "adv_yi": round(a / 1e8, 3), "sigma_d_pct": round(s * 100, 2)}
        for g in (20.0, 40.0, 96.0):
            vm = v_max(g, 0.0001, 5.0, 0.1, 5.0, a, s)
            row[f"vmax_g{int(g)}_wan"] = round(vm / 1e4, 1) if math.isfinite(vm) else None
        t8.append(row)
    out["t8_v_max"] = t8

    # T11 真·非空测度：逐票同时满足「成本约束 + 冲击约束」的占比
    t11 = []
    for g in G_BP:
        for name, r, m in TIERS:
            ok = 0
            for u, a, s in zip(unit, adv, sig):
                c = cost_bp(u, r, m, 5.0, 0.1, 5.0)
                if g - c <= 0:
                    continue
                hi = v_max(g, r, 5.0, 0.1, 5.0, a, s)
                if u <= hi:
                    ok += 1
            t11.append({"g_bp": g, "tier": name, "frac_names_feasible": round(ok / len(unit), 4)})
    out["t11_joint_frac"] = t11

    # T9 空集三分类：由 AUM 下限直接读出
    by_key = {(r["g_bp"], r["tier"]): r for r in t7}
    cls = []
    for (g, name), r in by_key.items():
        if r.get("aum_min_yi") is None:
            kind = "E1_structural"
        elif r["aum_min_yi"] > float(unit.sum()) / 1e8 * 1.001:
            kind = "E3_capacity"  # 一手不够，必须多手 ⇒ 需要更多资本
        else:
            kind = "feasible_at_one_lot"
        cls.append({"g_bp": g, "tier": name, "kind": kind, "aum_min_yi": r.get("aum_min_yi")})
    out["t9_class"] = cls

    out["_sources"] = {
        "cross_section": "data/kline_daily_enriched/date=2026-11-20 + 近60日",
        "etf": "data/kline_etf_daily/date=2026-11-20",
        "cb": "data/cb_5min_sina/cb_5min.parquet（100只，近22日）",
        "fee_params": "印花税 0.05% 卖出（2023-08-28起）；过户费 0.001%/边；佣金率作轴",
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    cur = build()

    if a.check:
        if not OUT.exists():
            print("基线不存在，先 --write")
            return 1
        base = json.loads(OUT.read_text(encoding="utf-8"))
        bad = []
        for k in ("universe", "unit_mean", "unit_sum"):
            if abs(cur[k] - base[k]) / max(abs(base[k]), 1e-9) > 0.05:
                bad.append((k, base[k], cur[k]))
        for t in ("t2_v_star", "t3_prop_floor_bp"):
            for kk, vv in cur[t].items():
                bv = base[t][kk]
                if isinstance(vv, dict):
                    for k2, v2 in vv.items():
                        if abs(v2 - bv[k2]) > max(0.05 * abs(bv[k2]), 0.5):
                            bad.append((f"{t}.{kk}.{k2}", bv[k2], v2))
                elif abs(vv - bv) > max(0.05 * abs(bv), 0.5):
                    bad.append((f"{t}.{kk}", bv, vv))
        if bad:
            print("漂移超阈：", bad)
            return 1
        print("check ok")
        return 0

    if a.write:
        OUT.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {OUT}")

    print(json.dumps(cur, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
