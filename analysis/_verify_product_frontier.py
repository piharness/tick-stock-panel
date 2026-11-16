"""产品目标前沿的复现入口：把 docs/product-frontier.md 里的每个数绑到产物上。

用法：
    python analysis/_verify_product_frontier.py --write   # 从三个 result.json 抽取基准
    python analysis/_verify_product_frontier.py --check   # 与基准比对，漂移 >1% 退出 1

为什么需要它（MEMORY 第 42 条）：文档里的常数一旦没有重算入口，就会在下一轮
被当成新证据引用。本脚本让 docs/product-frontier.json 成为唯一产地，
文档只引用它的键，`--check` 负责在产物变动后变红。

三个来源：
  analysis/_diag_ir_lower_frontier_result.json  判据与所需天数
  analysis/_diag_product_nav_result.json        S1/S2/S0 口径、三角、成本网格、逐年
  analysis/_diag_entry_timing_result.json       买点分组、当前读数、波动管理
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENCH = ROOT.parent / "docs" / "product-frontier.json"
SRC = {
    "frontier": ROOT / "_diag_ir_lower_frontier_result.json",
    "nav": ROOT / "_diag_product_nav_result.json",
    "timing": ROOT / "_diag_entry_timing_result.json",
}
TOL = 0.01


A_STAR = 21_938_325.0


def _profit_identity(tri: dict, s1: dict) -> list[dict]:
    """年利润 = A_star x 日收益 x 回合数，与 sigma 无关。逐档重算，供文档引用。"""
    ir = s1["ir_net20"]
    sd_nat = s1["sigma_nat_pct"] / 100.0
    a_star = tri["aum_cap_cny"] * tri["leverage"]
    rows = []
    for sig in (0.03, 0.05, 0.07, 0.0935, 0.12):
        lev = sig / sd_nat
        aum = a_star / lev
        mu = ir * sig
        rows.append({
            "sigma_pct": round(sig * 100, 2),
            "leverage": round(lev, 4),
            "aum_cap_cny": round(aum, 0),
            "mu_pct": round(mu * 100, 2),
            "ann_profit_cny": round(aum * mu, 0),
        })
    return rows
def _astar_sensitivity(tri: dict, s1: dict) -> list[dict]:
    """A_star 放大只改「能做多大」，不改 mu —— 因为 mu = IR x sigma，不含 A_star。

    参与率 alpha 与 A_star 成正比（A_star = 21.94M 对应 alpha=5%）。
    这里假设成本不随参与率变化（上界情形）；成本一涨，利润按 (gross-cost) 缩。
    判据：mu_pct 必须逐行相同；ann_profit 必须随 alpha 线性。
    """
    ir = s1["ir_net20"]
    sd_nat = s1["sigma_nat_pct"] / 100.0
    a_star = tri["aum_cap_cny"] * tri["leverage"]
    rows = []
    for alpha in (0.05, 0.10, 0.15, 0.20, 0.30):
        a = a_star * alpha / 0.05
        lev = 0.05 / sd_nat
        aum = a / lev
        mu = ir * 0.05
        rows.append({
            "participation_pct": round(alpha * 100, 1),
            "a_star_round_cny": round(a, 0),
            "aum_cap_cny": round(aum, 0),
            "mu_pct": round(mu * 100, 2),
            "ann_profit_cny": round(aum * mu, 0),
            "cost_assumption": "held at 20bp (upper bound; impact not measured)",
        })
    return rows


def _mechanism(nav: dict) -> dict:
    """底仓为什么吃掉 IR：方差占比、收益占比、以及算术加总与复利的交互项。

    交互项 = mean(r_base) - mean(gap) - mean(i1) = mean(gap*i1)。恒等式对每只票成立，
    对均值也成立（线性）。**不要拿「选中票的 gap 均值 x i1 均值」代替它** —— 那是子集。
    """
    a = nav["anatomy"]
    s2 = next(r for r in nav["shapes"] if r["shape"] == "S2_base1d_only")["mean_gross_bp"]
    s1 = next(r for r in nav["shapes"] if r["shape"] == "S1_base1d_overlay")["mean_gross_bp"]
    dec = nav["sigma_decomposition"]
    g, i = a["gap_mean_all_top20_bp"], a["i1_mean_all_top20_bp"]
    return {
        "share_variance_base": dec["share_variance_base"],
        "share_income_base_pct": round(s2 / s1 * 100, 1),
        "corr_base_overlay": dec["corr"],
        "gap_mean_all_bp": g,
        "i1_mean_all_bp": i,
        "arith_sum_bp": round(g + i, 2),
        "base_compound_bp": s2,
        "interaction_bp": round(s2 - (g + i), 2),
    }


def _annual_summary(yrs: dict) -> dict:
    v = [x for x in yrs.values() if x is not None]
    srt = sorted(v)
    n = len(srt)
    med = (srt[n // 2 - 1] + srt[n // 2]) / 2.0 if n % 2 == 0 else srt[n // 2]
    return {
        "n_years": n,
        "n_gt_2": sum(1 for x in v if x > 2.0),
        "n_negative": sum(1 for x in v if x < 0),
        "n_abs_lt_0p1": sum(1 for x in v if abs(x) < 0.1),
        "median": round(med, 2),
        "min": min(v), "max": max(v),
    }


def _load() -> dict:
    out = {}
    for k, p in SRC.items():
        if not p.exists():
            raise SystemExit(f"missing artifact: {p}")
        out[k] = json.loads(p.read_text(encoding="utf-8"))
    return out


def build() -> dict:
    a = _load()
    nav, tim, fr = a["nav"], a["timing"], a["frontier"]
    s1 = next(r for r in nav["shapes"] if r["shape"] == "S1_base1d_overlay")
    s0 = next(r for r in nav["shapes"] if r["shape"] == "S0_overlay_only")
    s2 = next(r for r in nav["shapes"] if r["shape"] == "S2_base1d_only")
    tri = nav["sigma_triangle_S1"]
    rec = nav["recent_subsample"]["S1"]
    yrs = {r["year"]: r["s1_ir_net20"] for r in nav["by_year"]}
    dd = tim["group_by_drawdown"]["rows"]
    cur = tim["current_reading"]
    vm = tim["vol_management"]
    win = tim["target_window_history"]

    return {
        "generated_from": {k: str(v.name) for k, v in SRC.items()},
        "s1": {k: s1[k] for k in ("n_days", "mean_gross_bp", "sd_bp", "ir_gross",
                                  "ir_net20", "sigma_nat_pct")},
        "s0_unreachable": {k: s0[k] for k in ("mean_gross_bp", "sd_bp", "ir_gross", "ir_net20")},
        "s2_base_only": {k: s2[k] for k in ("mean_gross_bp", "sd_bp", "ir_gross", "ir_net20")},
        "triangle": {k: tri[k] for k in ("leverage", "aum_cap_cny", "ann_profit_cny",
                                         "mu_pct", "mu_over_floor")},
        "cost_grid": [{"cost_bp": r["cost_bp"], "ir_net": r["ir_net"],
                       "mu_at_sigma5_pct": r["mu_at_sigma5_pct"]} for r in nav["cost_grid"]],
        "profit_identity": _profit_identity(tri, s1),
        "annual_summary": _annual_summary(yrs),
        "mechanism": _mechanism(nav),
        "astar_sensitivity": _astar_sensitivity(tri, s1),
        "sigma_needed_for_mu10": nav["sigma_needed_for_mu10"],
        "gate": {"ir_net": nav["gate"]["ir_net"],
                 "days_lower_gt0": nav["gate"]["days_lower_gt0"],
                 "days_lower_gt2": nav["gate"]["days_lower_gt2"]},
        "analytic_gate_table": [{"n_days": r["n_days"], "ir_needed_5pct": r["ir_needed_5pct"]}
                                for r in fr["gate"]["table"]],
        "recent_332d": {"n_days": rec["n_days"], "ir_net20": rec["ir_net20"],
                        "mu_at_sigma5_pct": round(rec["ir_net20"] * 5.0, 2)},
        "annual": {"2025": yrs.get("2025"), "2026": yrs.get("2026")},
        "entry_dd_groups": [{"group": r["group"], "n": r["n"],
                             "fwd_ir": r.get("fwd_ir_mean"),
                             "fwd_sd_bp": r.get("fwd_sd_bp"),
                             "lo": r.get("fwd_ir_ci95_block_year", [None, None])[0],
                             "hi": r.get("fwd_ir_ci95_block_year", [None, None])[1]}
                            for r in dd],
        "season": {str(m["month"]): {"mean_gross_bp": m["mean_gross_bp"], "t": m["t"]}
                   for m in tim["seasonality_by_month"] if m["month"] in (1, 2)},
        "entry_dd_groups_oos": [{"group": r["group"], "n": r["n"],
                                 "fwd_ir": r.get("fwd_ir_mean")}
                                for r in tim["group_by_drawdown_oos"]["rows"]],
        "current": {"as_of": cur["as_of"], "dd250_pct": cur["dd250_pct"],
                    "dd250_percentile": cur["dd250_percentile_in_sample"],
                    "v20_pct": cur["v20_pct_annualized"],
                    "v20_percentile": cur["v20_percentile_in_sample"]},
        "vol_management": {"sigma_nat_pct": vm["volmanaged_sigma_nat_pct"],
                           "ir_gross": vm["volmanaged_ir_gross"],
                           "ir_net20": vm["volmanaged_ir_net20"],
                           "raw_ir_net20": vm["raw_ir_net20"]},
        "target_window": {"ir_gross": win["ir_gross"], "ir_net20": win["ir_net20"],
                          "vs_all_ir_gross": win["vs_all_ir_gross"]},
    }


def compare(a, b, path: str = "") -> list[str]:
    bad = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k not in b:
                bad.append(f"{path}.{k}: missing in bench")
                continue
            bad += compare(a[k], b[k], f"{path}.{k}")
        return bad
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} vs {len(b)}"]
        for i, (x, y) in enumerate(zip(a, b)):
            bad += compare(x, y, f"{path}[{i}]")
        return bad
    if a is None or b is None:
        return [] if a == b else [f"{path}: {a} vs {b}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == 0:
            return [] if b == 0 else [f"{path}: 0 vs {b}"]
        if abs(b - a) / abs(a) > TOL:
            bad.append(f"{path}: {a} -> {b} ({(b - a) / abs(a) * 100:+.2f}%)")
    return bad


def main() -> None:
    fresh = build()
    if "--write" in sys.argv:
        BENCH.write_text(json.dumps(fresh, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[written] {BENCH}")
        return
    if not BENCH.exists():
        raise SystemExit("bench missing; run --write first")
    old = json.loads(BENCH.read_text(encoding="utf-8"))
    bad = compare(old, fresh)
    if bad:
        print(f"[DRIFT] {len(bad)} field(s) moved >{TOL:.0%}:")
        for b in bad:
            print("  ", b)
        raise SystemExit(1)
    n = len(json.dumps(old))
    print(f"[OK] {BENCH.name} clean ({n} bytes, tol {TOL:.0%})")


if __name__ == "__main__":
    main()
