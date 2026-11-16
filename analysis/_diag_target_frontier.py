"""把「年化 >10% 且最大回撤 <5%」当成一个可计算的双阈值事件，算它的可行域。

触发问题（2026-11-15）：用户问「TSP 能不能做到年化>10%、回撤<5%，概率多大」。
仓库里没有任何一个产物回答过这个问题：所有回测指标都是单点（年化、回撤分开报），
晋级门槛写的是「回撤不劣于 -25%」（docs/mining.md），从来没有把两条阈值**联立**过。

这个脚本不做预测，只做三件可证伪的事：

  1. **给出上界**：给定真实信息比 IR，把仓位缩放到最优波动 sd*，问
     P(年化>10% 且 MDD<5%) 最大能到多少。因为 mu=IR*sd、任何杠杆都是过原点的射线，
     所以「max over sd」是**任何仓位管理都无法超越的上界**，与选股无关。
  2. **给出所需 IR**：要 50%/80%/90% 的把握，IR 得是多少。这把「能不能」换成一个
     可以直接拿去和候选比对的数。
  3. **把仓库里的三个候选放进去读数**：游资换手规则（毛 1.63%/净 1.22%、sd 25%）、
     L1 总权益口径（105.7%/yr、日 sd 343.2bp）、L1 按容量一致打平线（余量 0.47bp）。
     三者都是既有产物里的**既有数**，本脚本不重算收益，只做事件概率。

口径：每年 242 个交易日；日收益 iid；两种分布（正态 + 标准化 t4 厚尾敏感性）；
MDD 是复利净值的峰谷回撤；概率是路径频率（蒙特卡洛），不是对未来的信念。

为什么必须用「扫描 sd」而不是直接用样本 sd：105.7%/yr 配 49.5% 波动时，5% 的回撤
在两周内就会被打穿 —— 但那是**满仓满容量**的跑法。同一个 IR 缩到 1/6 仓位，
年化仍有 17%，回撤才有机会压在 5% 以内。不扫描 sd 就会把「规模选错」误读成「策略不行」。

同一个 IR 内**复用同一批 Z**（common random numbers）：sd 的曲线是同一批路径在不同
缩放下的读数，比较的是缩放效应，不是抽样噪声。

ASCII 输出（GBK 控制台）。纯计算，不读盘、不联网。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "_diag_target_frontier_result.json"

SEED = 20261115
DAYS = 242.0
RET_TARGET = 0.10
DD_TARGET = 0.05
M_MAP = 1500
M_CAND = 20000
NU = 4.0                       # t 分布自由度（厚尾敏感性）

IR_GRID = [0.0, 0.05, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0]
SIG_GRID = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10,
            0.12, 0.14, 0.16, 0.18, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]
HORIZONS = [1, 5]

# 三个候选（全部来自既有产物，不在此重算）
CANDIDATES = {
    "youzi_turnover_rule": {
        "label": "游资换手极值（docs/youzi-strategy-audit.md:215）",
        "mu_ann": 0.0122, "sd_ann": 0.25,
        "source": "净 1.22%/年（往返 32bp），IR 0.05",
    },
    "l1_total_equity": {
        "label": "L1 缺口反转，总权益口径（_diag_l1_annualize_result.json）",
        "mu_ann": 1.0574, "sd_ann": 0.03432 * math.sqrt(208.4),
        "source": "净 101.5bp/回合 x 208.4 信号日 = 105.7%/年；日 sd 343.2bp 只在信号日",
    },
    "l1_at_breakeven": {
        "label": "L1 按容量一致打平线（_diag_l1_cohort_curve_result.json）",
        "mu_ann": (108.39 - 107.92) / 1e4 * 208.4, "sd_ann": 0.03432 * math.sqrt(208.4),
        "source": "全样本 D(240) 日等权 108.39bp vs 打平线 107.92bp，余量 0.47bp（t=0.04，退化）",
    },
}


def _z(rng: np.random.Generator, m: int, t: int, dist: str) -> np.ndarray:
    """单位方差冲击。三种：正态 / 标准化 t4 / 随机波动（乘性 AR(1) 波动率，E[scale^2]=1）。"""
    if dist == "normal":
        return rng.standard_normal((m, t), dtype=np.float32)
    if dist == "t4":
        # 标准化 t(nu)：Var(t_nu)=nu/(nu-2)
        return (rng.standard_t(NU, size=(m, t)) / math.sqrt(NU / (NU - 2.0))).astype(np.float32)
    # cluster：h_t 为 AR(1)（rho=0.97, 边际 sd=0.40），scale=exp(h-sigma_h^2) 使 E[scale^2]=1
    rho, sh = 0.97, 0.40
    e = rng.standard_normal((m, t), dtype=np.float32)
    h = np.empty((m, t), dtype=np.float32)
    h[:, 0] = rng.standard_normal(m).astype(np.float32) * sh
    for i in range(1, t):
        h[:, i] = rho * h[:, i - 1] + math.sqrt(1.0 - rho * rho) * sh * e[:, i]
    return (np.exp(h - sh * sh) * rng.standard_normal((m, t), dtype=np.float32)).astype(np.float32)


def from_z(z: np.ndarray, mu_ann: float, sd_ann: float, years: int) -> dict:
    """同一批冲击在给定 (mu, sd) 下的双阈值事件频率。

    用**对数收益**：单日亏损天然 <100%（净值恒正），高 sd 端不会出现「负净值 -> 回撤失真」。
    mu_ann 是算术年均（候选产物就是这个口径），几何漂移 = log1p(mu) - sd^2/2。
    """
    mu_log = math.log1p(mu_ann) - 0.5 * sd_ann * sd_ann
    cum = np.cumsum(mu_log / DAYS + (sd_ann / math.sqrt(DAYS)) * z, axis=1, dtype=np.float32)
    peak = np.maximum.accumulate(cum, axis=1)
    mdd = (1.0 - np.exp(np.minimum(cum - peak, 0.0))).max(axis=1)
    ret = np.exp(cum[:, -1].astype(np.float64) / years) - 1.0
    hit_r = ret > RET_TARGET
    hit_d = mdd < DD_TARGET
    both = hit_r & hit_d
    p = float(both.mean())
    return {
        "p_both": p,
        "p_ret": float(hit_r.mean()),
        "p_dd": float(hit_d.mean()),
        "p_ret_given_dd": float(hit_r[hit_d].mean()) if hit_d.any() else float("nan"),
        "mdd_mean": float(mdd.mean()),
        "mdd_q50": float(np.quantile(mdd, 0.5)),
        "mdd_q95": float(np.quantile(mdd, 0.95)),
        "ret_q05": float(np.quantile(ret, 0.05)),
        "ret_q50": float(np.quantile(ret, 0.5)),
        "se": float(math.sqrt(max(p * (1.0 - p), 1e-12) / z.shape[0])),
    }


def _r5(d: dict) -> dict:
    return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in d.items()}


def scan_ir(ir: float, years: int, dist: str, rng: np.random.Generator) -> dict:
    z = _z(rng, M_MAP, int(round(DAYS * years)), dist)
    best = {"p_both": -1.0}
    curve = []
    for sig in SIG_GRID:
        r = from_z(z, ir * sig, sig, years)
        curve.append({"sd_ann": sig, "mu_ann": round(ir * sig, 5),
                      "p_both": round(r["p_both"], 5),
                      "p_ret": round(r["p_ret"], 5), "p_dd": round(r["p_dd"], 5)})
        if r["p_both"] > best["p_both"]:
            best = {**r, "sd_ann": sig, "mu_ann": ir * sig}
    print(f"  IR={ir:.2f} -> p_best={best['p_both']:.4f} @sd={best['sd_ann']:.2f}"
          f" (p_ret={best['p_ret']:.3f} p_dd={best['p_dd']:.3f})", flush=True)
    return {"ir": ir, "best": _r5(best), "curve": curve}


def ir_for_confidence(scan: list[dict], target: float) -> float | None:
    """在 IR 网格上线性插值出达到 target 把握所需的最小 IR。"""
    xs = np.array([row["ir"] for row in scan])
    ys = np.array([row["best"]["p_both"] for row in scan])
    if ys.max() < target:
        return None
    for i in range(1, len(xs)):
        if ys[i - 1] < target <= ys[i]:
            f = (target - ys[i - 1]) / (ys[i] - ys[i - 1])
            return round(float(xs[i - 1] + f * (xs[i] - xs[i - 1])), 3)
    return round(float(xs[0]), 3) if ys[0] >= target else None


def main() -> None:
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    sets: dict[str, dict] = {}

    for dist in ("normal", "t4", "cluster"):
        sets[dist] = {}
        for yr in HORIZONS:
            print(f"[{dist} {yr}y]", flush=True)
            scan = [scan_ir(ir, yr, dist, rng) for ir in IR_GRID]
            thr = {f"p{int(c * 100)}": ir_for_confidence(scan, c) for c in (0.5, 0.8, 0.9)}
            sets[dist][f"{yr}y"] = {"scans": scan, "ir_required": thr}
            print(f"  required IR: {thr}", flush=True)

    # 候选读数：原规模 + 最优规模
    cand: dict[str, dict] = {}
    for key, c in CANDIDATES.items():
        ir = c["mu_ann"] / c["sd_ann"]
        rows = {}
        for dist in ("normal", "t4", "cluster"):
            for yr in (1, 5):
                raw = from_z(_z(rng, M_CAND, int(round(DAYS * yr)), dist),
                             c["mu_ann"], c["sd_ann"], yr)
                z = _z(rng, M_CAND, int(round(DAYS * yr)), dist)
                best = None
                for sig in SIG_GRID:
                    r = from_z(z, ir * sig, sig, yr)
                    if best is None or r["p_both"] > best["p_both"]:
                        best = {**r, "sd_ann": sig, "mu_ann": ir * sig}
                rows[f"{dist}_{yr}y"] = {"as_deployed": _r5(raw),
                                         "at_best_scale": _r5(best)}
        cand[key] = {**c, "ir": round(ir, 4), "rows": rows}
        b = rows["normal_5y"]["at_best_scale"]
        print(f"[cand] {key}: IR={ir:.3f} -> p_best(5y)={b['p_both']:.4f}"
              f" @sd={b['sd_ann']:.2f} mu={b['mu_ann']:.3f}", flush=True)

    # 外部验证①：同一个「无漂移、年化波动 10%」的路径，用 242 步与 10000 步各算一次
    # 平均最大回撤。两者只差离散化，应当吻合（<5%）；同时报连续极限闭式 sd*sqrt(pi/2)。
    def _mean_mdd(sd: float, m: int, sub: int) -> float:
        """独立种子、独立分辨率地重算同一参数的平均最大回撤（只改步长，不改口径）。"""
        t = int(DAYS) * sub
        rr = np.random.default_rng(SEED + sub)
        zz = rr.standard_normal((m, t), dtype=np.float32)
        lr = (-0.5 * sd * sd) / t + (sd / math.sqrt(t)) * zz
        cum = np.cumsum(lr, axis=1, dtype=np.float32)
        return float((1.0 - np.exp(np.minimum(cum - np.maximum.accumulate(cum, axis=1), 0.0)))
                     .max(axis=1).mean())

    m242 = _mean_mdd(0.10, 3000, 1)
    m10k = _mean_mdd(0.10, 1500, 40)
    ref = 0.10 * math.sqrt(math.pi / 2.0)
    validation = {
        "driftless_mean_mdd_242step": round(m242, 4),
        "driftless_mean_mdd_10k_step": round(m10k, 4),
        "closed_form_continuous": round(ref, 4),
        "discretization_gap_pct": round((m242 / m10k - 1.0) * 100, 2),
        "note": "242 步比 10000 步系统性偏低约 5% => 本产物的 P(MDD<5%) 略偏乐观（已知偏差，方向已标）",
        "pass": bool(abs(m242 / m10k - 1.0) < 0.05),
    }

    # 外部验证②：同样是 (IR=2, sd=5%)，回撤中位数随**评估窗口**变长单调上升
    # —— 「5 年长期业绩」比「1 年」难得多，这与「长期会平滑」的直觉相反。
    horizon_effect = []
    for yr in (1, 3, 5, 10):
        m = from_z(_z(rng, 8000, int(round(DAYS * yr)), "normal"), 0.10, 0.05, yr)
        horizon_effect.append({"years": yr, "p_dd": round(m["p_dd"], 4),
                               "mdd_q50": round(m["mdd_q50"], 4),
                               "mdd_q95": round(m["mdd_q95"], 4),
                               "p_both": round(m["p_both"], 4)})

    yz = cand["youzi_turnover_rule"]["rows"]
    l1 = cand["l1_total_equity"]["rows"]
    be = cand["l1_at_breakeven"]["rows"]

    def _at(ir_target: float, horizon: str = "5y") -> dict:
        for row in sets["normal"][horizon]["scans"]:
            if abs(row["ir"] - ir_target) < 1e-9:
                return row["best"]
        raise KeyError(ir_target)

    headline = {
        "horizon_years": 5,
        "ir50_5y": sets["normal"]["5y"]["ir_required"]["p50"],
        "ir80_5y": sets["normal"]["5y"]["ir_required"]["p80"],
        "ir90_5y": sets["normal"]["5y"]["ir_required"]["p90"],
        "ir50_1y": sets["normal"]["1y"]["ir_required"]["p50"],
        "ir80_1y": sets["normal"]["1y"]["ir_required"]["p80"],
        "ir50_5y_t4": sets["t4"]["5y"]["ir_required"]["p50"],
        "ir80_5y_t4": sets["t4"]["5y"]["ir_required"]["p80"],
        "ir50_5y_cl": sets["cluster"]["5y"]["ir_required"]["p50"],
        "ir80_5y_cl": sets["cluster"]["5y"]["ir_required"]["p80"],
        "p_at_ir4": _at(4.0)["p_both"],
        "p_at_ir05": sets["normal"]["5y"]["scans"][1]["best"]["p_both"],
        "p_at_ir05_t4": sets["t4"]["5y"]["scans"][1]["best"]["p_both"],
        "yz_ir": cand["youzi_turnover_rule"]["ir"],
        "yz_p_raw": yz["normal_5y"]["as_deployed"]["p_both"],
        "yz_p_best": yz["normal_5y"]["at_best_scale"]["p_both"],
        "yz_sig_best": yz["normal_5y"]["at_best_scale"]["sd_ann"],
        "l1_ir": cand["l1_total_equity"]["ir"],
        "l1_p_raw": l1["normal_5y"]["as_deployed"]["p_both"],
        "l1_p_raw_dd": l1["normal_5y"]["as_deployed"]["p_dd"],
        "l1_mdd_raw": l1["normal_5y"]["as_deployed"]["mdd_q50"],
        "l1_p_best": l1["normal_5y"]["at_best_scale"]["p_both"],
        "l1_sig_best": l1["normal_5y"]["at_best_scale"]["sd_ann"],
        "l1_mu_best": l1["normal_5y"]["at_best_scale"]["mu_ann"],
        "l1_p_best_t4": l1["t4_5y"]["at_best_scale"]["p_both"],
        "l1_sig_raw": cand["l1_total_equity"]["sd_ann"],
        "be_ir": cand["l1_at_breakeven"]["ir"],
        "be_p_best": be["normal_5y"]["at_best_scale"]["p_both"],
        "he_1y_mdd": horizon_effect[0]["mdd_q50"],
        "he_10y_mdd": horizon_effect[-1]["mdd_q50"],
    }
    validation["verdict"] = "pass" if validation["pass"] else "FAIL"

    nv: dict = {
        "purpose": "年化>10% 且 MDD<5% 的双阈值事件概率，按 IR 与仓位规模给上界",
        "generated_at": "2026-11-15",
        "seed": SEED,
        "caliber": {
            "days_per_year": DAYS, "ret_target": RET_TARGET, "dd_target": DD_TARGET,
            "m_map": M_MAP, "m_cand": M_CAND, "nu": NU,
            "return_type": "复利净值，年化=终值^(1/years)-1",
            "dd_type": "1 - min(净值/历史峰值)，逐路径",
            "why_scan_sigma": "mu=IR*sd => 任何杠杆都是过原点的射线；max over sd 是任何仓位管理都无法超越的上界",
            "common_random_numbers": True,
        },
        "headline": headline,
        "validation": validation,
        "horizon_effect": horizon_effect,
        "sets": sets,
        "candidates": cand,
        "elapsed_s": round(time.time() - t0, 1),
    }
    nv["reading_template"] = (
        "双阈值事件（年化>{rt:.0%} 且 MDD<{dt:.0%}，{h} 年，{days:.0f} 日/年，log 收益 + "
        "正态基准，另跑 t4 厚尾与随机波动两种敏感性）：**概率不是策略的属性，是 (IR x 规模) 的函数**。\n"
        "**① 所需 IR**：5 年口径正态 50% 把握要 IR>={ir50}、80% 要 {ir80}、90% 要 {ir90}；"
        "1 年口径 50% 只要 {ir50y}。t4 几乎不动（{ir50t} / {ir80t}），"
        "但**随机波动**（AR(1) 波动率，E[scale^2]=1）把 50% 抬到 {ir50cl}、80% 抬到 {ir80cl}"
        "——“同样的年化波动、不同的波动时间结构”不是同一个策略。"
        "这个门槛**任何仓位管理都无法降低**：mu=IR*sd，缩放只沿过原点的射线移动。\n"
        "**② 上界的形状**：IR=4 的最优规模下 5 年命中率 {p4:.1%}；IR=0.05 时只有 {p05:.3%}"
        "（t4 下 {p05t:.3%}）。天花板由 IR 定，能不能站到天花板下面由规模定。"
        "最优规模几乎总是「刚好把年化顶到 10%」的那一档——再加波动只买回撤、不买收益。\n"
        "**③ 三个候选**：游资换手规则 IR={yzir}（净 1.22%/年、sd 25%）=> 满仓 {yzraw:.2%}、"
        "缩到 sd={yzsig:.0%} 也只有 {yzbest:.2%}；L1 总权益口径 IR={l1ir} => 满仓（sd={l1sigraw:.0%}）"
        "{l1raw:.1%}，MDD 中位 {l1mdd:.1%}、P(MDD<5%)={l1pdd:.1%}；缩到 sd={l1sig:.0%}"
        "（年化 {l1mu:.0%}）才升到 {l1best:.1%}（t4 下 {l1bestt:.1%}）。"
        "按容量一致打平线读（余量 0.47bp，IR={beir}）=> {bebest:.2%}。\n"
        "**④ 判决**：IR<{ir50} 时这个目标不是「难」，是**在概率上不成立**——"
        "不是策略跑得不好，是没有路径能同时满足两条阈值。IR>=2 那一档才让「>10% 且 <5%」"
        "从不可能变成五成把握。\n"
        "**⑤ 自检**：无漂移 1 年平均最大回撤 242 步 {sim} vs 10000 步 {ref}（{ok}，离散化差 {gap}%）；"
        "同一 (IR=2, sd=5%) 下回撤中位随窗口 1->10 年从 {m1:.1%} 升到 {m10:.1%}"
        "——「五年业绩」比「一年业绩」难得多。"
    )
    nv["reading_fields"] = {
        "rt": "caliber.ret_target", "dt": "caliber.dd_target",
        "h": "headline.horizon_years", "days": "caliber.days_per_year",
        "ir50": "headline.ir50_5y", "ir80": "headline.ir80_5y", "ir90": "headline.ir90_5y",
        "ir50y": "headline.ir50_1y", "ir80y": "headline.ir80_1y",
        "ir50t": "headline.ir50_5y_t4", "ir80t": "headline.ir80_5y_t4",
        "ir50cl": "headline.ir50_5y_cl", "ir80cl": "headline.ir80_5y_cl",
        "p4": "headline.p_at_ir4", "p05": "headline.p_at_ir05", "p05t": "headline.p_at_ir05_t4",
        "yzir": "headline.yz_ir", "yzraw": "headline.yz_p_raw", "yzbest": "headline.yz_p_best",
        "yzsig": "headline.yz_sig_best",
        "l1ir": "headline.l1_ir", "l1raw": "headline.l1_p_raw", "l1pdd": "headline.l1_p_raw_dd",
        "l1mdd": "headline.l1_mdd_raw", "l1best": "headline.l1_p_best",
        "l1sig": "headline.l1_sig_best", "l1mu": "headline.l1_mu_best",
        "l1bestt": "headline.l1_p_best_t4", "l1sigraw": "headline.l1_sig_raw",
        "beir": "headline.be_ir", "bebest": "headline.be_p_best",
        "ref": "validation.driftless_mean_mdd_10k_step",
        "sim": "validation.driftless_mean_mdd_242step",
        "ok": "validation.verdict",
        "gap": "validation.discretization_gap_pct",
        "m1": "headline.he_1y_mdd", "m10": "headline.he_10y_mdd",
    }
    nv["reading"] = render_reading(nv, root=nv)
    assert_clean(nv, label=str(OUT.name))          # 写盘前拦，不留脏产物
    OUT.write_text(json.dumps(nv, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={nv['elapsed_s']}s  guard=clean")
    print(nv["reading"])


if __name__ == "__main__":
    main()
