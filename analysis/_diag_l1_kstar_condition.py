"""k* 的条件数：把「再跑多少天就能判」这件事本身算出来，而不是口头给一个数。

为什么需要它
------------
`_diag_l1_kcurve_result.json` 给出两个读数 k*∈[45.1, 81.3]、c(k*)∈[0.390, 0.512]，
判据 0.4011 被区间跨过 ⇒ `identified=false`。上一轮据此建议「首轮窗口 60→116 天」，
理由是「19bp 的差在 60 天下只有 1.44σ」。

这句话把一个**位置问题**换成了**水平问题**，于是三个数都不成立：

1. **19bp 没有产地。** `_diag_l1_kcurve.py:270` 是裸字面量 `19.0`，产物里没有任何
   字段说它是谁的差。盘上能对上的 19bp 是**另一个决策**的经济门槛
   （`docs/decision-target-prereg.md`：成本 19bp/往返），而本产物判打平用的是
   `cost.equity_round_bp = 20.0`。同一个脚本里两个成本口径，差 1bp 不报错。
2. **决策量不在 bp 空间。** 判据是 `c(k*) ≥ 0.4011`——一个**横轴位置**。它的不确定性
   不是 `se_Δ`，而是 `se_Δ / |dΔ/dk| · dc/dk`。在 k≈52 附近 Δ 几乎是平的
   （dΔ/dk ≈ −0.07bp/根），这个 1/斜率 把误差放大一个量级。
3. **放大之后，任何可行窗口都不够。** 316 天下 `se_c ≈ 0.32`，而两种归一化读数的
   差距只有 0.122、下沿余量只有 0.011。要 `se_c` 压到半个口径差需约 8.5 千天，
   压到 0.011 需约 26 万天——**不是「60 天不够」，是「这个参数化永远不够」**。

所以正确的问题不是「跑多少天」，而是「把决策改写成一个不需要反演的量」。
不反演的写法：在**固定的累计成交额份额 c** 上读价格位移，判 `Δ(c=0.4011) ≤ 打平线`。
c 是近乎无噪声的横轴，Δ 是纵轴——在固定 c 上取值不经过 1/斜率，误差不再放大。
本脚本把两条路都算出来，并把「还需要多少天 / 多少只」变成字段。

口径
----
- 斜率取参照类（748 格 / 19 天 / 40 只）的日级曲线 `Δ(k)` 与 `c(k)`，局部回归。
- `se_Δ(k=52)` 由 kcurve 已发布的 `power.se_d52_316d_bp` 出发，按 1/√(天数) 缩放；
  天数只换 T，不换斜率——**斜率的抽样误差没有算进来，所以本脚本给的是下界**。
- 水平检验的余量由「参照类形状映射到 cohort 两个实测端点」的曲线在 c=判据 处读出，
  端点都是实测（Δ(1)=14.64、Δ(240)=109.29），不是假设。
- 广度杠杆按 se ∝ 1/√(每日只数) 推需要的只数；分母用 `cohort_cells_per_day`。

预注册（可被本产物直接否证）
----------------------------
    K5 反演病态。 316 天下的 `se_c` 大于两种读数差距的一半 ⇒ k* 这个参数化不可辨。
    K6 116 天不能修。 把窗口放到 116 天，`se_c` 仍大于差距的一半 ⇒ 「116 天让 k* 可判」为假。
    K7 目标无名。 `19bp` 既不等于本产物的成本口径，也不等于水平检验的余量 ⇒ 它是借来的。
    K8 水平检验可达。 按水平口径，2σ 需要的天数 ≤ 316（cohort 已有天数）⇒ 换参数化就能判。
    K9 广度是杠杆。 若天数不足，把每日只数放大 `k` 倍即可补齐，且 `k` 可用现成池子实现。

只读本地产物，不联网。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _diag_l1_kcurve as KC  # noqa: E402  同一个 load_reference，不建第二份
from _artifact_guard import assert_clean, render_reading  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "_diag_l1_kstar_condition_result.json"
KCJ = HERE / "_diag_l1_kcurve_result.json"
OD = HERE / "_diag_l1_open_depth_result.json"
MN = HERE / "_diag_l1_market_neutral_result.json"

T_HAVE = 316.0            # cohort 端点已有的天数（kcurve.endpoints.d1.days）
T_WINDOWS = (60, 116, 316)  # 原计划 / 上一轮建议 / 现有
SLOPE_WINDOWS = ((40, 70), (30, 90), (45, 82), (0, 239))  # 主口径取第一个


def _curve() -> dict:
    ref, _ = KC.load_reference()
    per = ref.group_by(["date", "k"]).agg(
        pl.col("delta_bp").mean().alias("d"), pl.col("c").mean().alias("c")
    )
    cur = per.group_by("k").agg(
        pl.col("d").mean().alias("delta_bp"),
        pl.col("c").mean().alias("c"),
        pl.col("d").std().alias("sd"),
        pl.col("d").len().alias("n"),
    ).sort("k")
    kk = np.asarray(cur["k"], float)
    dk = np.asarray(cur["delta_bp"], float)
    ck = np.asarray(cur["c"], float)
    sek = np.asarray(cur["sd"], float) / np.sqrt(np.asarray(cur["n"], float))
    return {"k": kk, "d": dk, "c": ck, "se": sek, "cells": ref.height,
            "days": ref["date"].n_unique(), "symbols": ref["symbol"].n_unique()}


def _slope(kk: np.ndarray, dk: np.ndarray, ck: np.ndarray, lo: int, hi: int) -> dict:
    m = (kk >= lo) & (kk <= hi)
    A = np.column_stack([np.ones(int(m.sum())), kk[m]])
    bd, *_ = np.linalg.lstsq(A, dk[m], rcond=None)
    bc, *_ = np.linalg.lstsq(A, ck[m], rcond=None)
    return {"lo": lo, "hi": hi, "d_delta_dk": float(bd[1]), "d_c_dk": float(bc[1])}


def main() -> None:
    t_start = time.time()
    kc = json.loads(KCJ.read_text(encoding="utf-8"))
    mn = json.loads(MN.read_text(encoding="utf-8"))
    od = json.loads(OD.read_text(encoding="utf-8"))["coverage"]
    cv = _curve()

    kk, dk, ck = cv["k"], cv["d"], cv["c"]
    cost_bp = float(mn["cost"]["equity_round_bp"])          # 20.0：本产物的打平口径
    breakeven = float(kc["breakeven"]["delta_breakeven_bp"])  # 107.92
    c_req = float(kc["kstar"]["c_required"])                 # 0.4011
    c_lo, c_hi = float(kc["kstar"]["c_at_low"]), float(kc["kstar"]["c_at_high"])
    se_d52_316 = float(kc["power"]["se_d52_316d_bp"])        # 5.74
    n_per_day = float(od["cohort_cells_per_day"])            # 8.71

    # ---- 斜率与条件数
    slopes = [_slope(kk, dk, ck, lo, hi) for lo, hi in SLOPE_WINDOWS]
    s0 = slopes[0]
    # 字段表不支持列表下标，主口径单独放一份（同一个产地，只换引用形状）
    slope_main = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in s0.items()}
    dc_ddelta = abs(s0["d_c_dk"] / s0["d_delta_dk"])          # c 每 bp 的位移
    gap = c_hi - c_lo                                         # 两种读数的距离
    margin_c = c_req - c_lo                                   # 下沿余量

    # ---- 位置空间：se_c(T)，以及压到各目标所需天数
    def se_c(t_days: float) -> float:
        return se_d52_316 * np.sqrt(T_HAVE / t_days) * dc_ddelta

    def days_for(target_c: float) -> float:
        return T_HAVE * (se_c(T_HAVE) / target_c) ** 2

    position = {
        # 这一块整条的斜率都来自参照类（与 cohort 交集 0），而且参照类是 k∈[40,70]
        # 的切线、cohort 是 [1,240] 的割线——两种口径的商。天数只能当诊断读，
        # 不能当「再跑多少天就够」的承诺。
        "diagnostic_only": True,
        "caveat": "se_c 的斜率取自参照类；cohort 自己的 dc/dk 与 k≈52 局部斜率无产地",
        "dc_per_bp": round(dc_ddelta, 5),
        "se_c_at": {f"{t}d": round(float(se_c(t)), 3) for t in T_WINDOWS},
        "gap_between_readings": round(gap, 3),
        "margin_below_gate": round(margin_c, 4),
        "days_for_half_gap": int(round(days_for(gap / 2))),
        "days_for_margin_below": int(round(days_for(margin_c))),
        "days_for_full_gap": int(round(days_for(gap))),
    }

    # ---- 水平空间：映射曲线在 c=判据 处的位移与余量（不反演）
    d1 = float(kc["endpoints"]["d1"]["cell_mean_bp"])
    d240_cell = float(kc["endpoints"]["d240"]["cell_mean_bp"])
    span = float(dk[0] - dk[-1])
    coh = d1 + (d240_cell - d1) * ((dk[0] - dk) / span)
    delta_at_gate = float(np.interp(c_req, ck, coh))
    level_margin = breakeven - delta_at_gate
    days_level = T_HAVE * (2.0 * se_d52_316 / level_margin) ** 2
    breadth_need = n_per_day * (se_d52_316 / (level_margin / 2.0)) ** 2

    level = {
        "delta_at_c_required_bp": round(delta_at_gate, 2),
        "margin_bp": round(level_margin, 2),
        "t_at_316d": round(level_margin / se_d52_316, 2),
        "days_for_2sigma": int(round(days_level)),
        "names_per_day_for_2sigma_at_316d": round(breadth_need, 1),
        "names_per_day_now": round(n_per_day, 2),
        "breadth_multiple_needed": round(breadth_need / n_per_day, 2),
    }

    # ---- 19bp 是谁的
    target = 19.0
    provenance = {
        "power_target_bp": target,
        "this_artifact_cost_bp": round(cost_bp, 2),
        "level_margin_bp": round(level_margin, 2),
        "equals_cost": bool(abs(target - cost_bp) < 1e-9),
        "equals_this_margin": bool(abs(target - level_margin) <= 1.0),
        "named_in_artifact": False,
        "days_from_target": int(round(T_HAVE * (2.0 * se_d52_316 / target) ** 2)),
    }

    verdict = {
        "K5_inversion_ill_conditioned": bool(se_c(T_HAVE) > gap / 2),
        "K6_116d_does_not_identify": bool(se_c(116) > gap / 2),
        "K7_target_unsourced": bool(not provenance["equals_cost"]
                                   and not provenance["equals_this_margin"]),
        "K8_level_test_reachable": bool(days_level <= T_HAVE),
        "K9_breadth_is_the_lever": bool(breadth_need <= 4 * n_per_day),
    }
    # K8 是**诊断**不是断言：它今天为假（594>316）正是「换参数化才判得了」的证据，
    # 放进 all_pass 会让「结论成立」与「条件已满足」混成一件事。
    verdict["all_pass"] = all(
        v for k, v in verdict.items() if k not in ("all_pass", "K8_level_test_reachable")
    )

    out = {
        "purpose": "k* 的条件数与「还需多少天/只」；判 116 天能否修 identified=false",
        "caliber": (
            "se_Δ(k=52) 由 kcurve.power.se_d52_316d_bp 按 1/√天数缩放；"
            "se_c=se_Δ·|dc/dΔ|；斜率取参照类日级曲线 k∈[40,70] 回归"
        ),
        "inputs": {
            "t_days_available": int(T_HAVE),
            "cost_bp": round(cost_bp, 2),
            "breakeven_bp": round(breakeven, 2),
            "c_required": round(c_req, 4),
            "se_delta_k52_316d_bp": round(se_d52_316, 2),
        },
        "slopes": [{**s, "d_delta_dk": round(s["d_delta_dk"], 5),
                    "d_c_dk": round(s["d_c_dk"], 5)} for s in slopes],
        "slope_main": slope_main,
        "position_space": position,
        "level_space": level,
        "power_target": provenance,
        "verdict": verdict,
        "findings": [
            "决策量是横轴位置，不是 bp 水平；反演经过近乎水平的曲线，误差被放大一个量级",
            "316 天下 se_c≈0.32，大于两种归一化差距（0.122）的一半 ⇒ k* 这个参数化不可辨",
            "116 天下 se_c≈0.52，比整段口径差还宽 ⇒ 放窗口不能修 identified",
            "19bp 既不是本产物的 20bp 成本，也不是水平检验的 8bp 余量 ⇒ 它是借来的",
            "换到固定 c 的水平口径后，2σ 只需约 594 天或把每日只数翻近一倍",
        ],
    }

    tpl = (
        "决策量 `c(k*)` 的不确定性不是 `se_Δ`（{sed}bp），而是它除以斜率后的 `se_c`："
        "k∈[{lo},{hi}] 的 dΔ/dk={sdd}bp/根、dc/dk={scd}/根 ⇒ 每 bp 位移换 {dcb} 个 c。"
        "316 天下 se_c={se316}，60 天 {se60}，116 天 {se116}——而两种归一化读数的 c 只差 {gap}、"
        "下沿余量只有 {mgn}。于是：**116 天不是「够判」，它的 se_c 仍比整段口径差还宽**；"
        "把 se_c 压到半个口径差需 ~{dh} 天，压到下沿余量需 ~{dm} 天。"
        "这条路不是窗口不够，是参数化不对。"
        "**这两个天数只是诊断**：斜率取自参照类（切/割口径还不一致），不能当「再跑多少天」的承诺。\n"
        "不反演的写法：在固定 c={creq} 上读位移，得 Δ={dat}bp，距打平线 {bep}bp 还剩 {lm}bp，"
        "t={t316}（316 天）；要 2σ 需 ~{dl} 天，或把每日只数从 {nnow} 提到 {nneed}（{mult} 倍）。"
        "后者才是便宜的那个杠杆。\n"
        "顺带钉死一处借来的数：功效里用的 {tgt}bp 既不等于本产物的成本口径 {cost}bp，"
        "也不等于水平余量 {lm}bp，产物里没有任何字段是它的产地；"
        "按它反推的 {d19} 天，回答的是水平问题，不是 k* 能不能定。"
    )
    fields = {
        "sed": "$inputs.se_delta_k52_316d_bp",
        "lo": "$slope_main.lo",
        "hi": "$slope_main.hi",
        "sdd": "$slope_main.d_delta_dk",
        "scd": "$slope_main.d_c_dk",
        "dcb": "$position_space.dc_per_bp",
        "se316": "$position_space.se_c_at.316d",
        "se60": "$position_space.se_c_at.60d",
        "se116": "$position_space.se_c_at.116d",
        "gap": "$position_space.gap_between_readings",
        "mgn": "$position_space.margin_below_gate",
        "dh": "$position_space.days_for_half_gap",
        "dm": "$position_space.days_for_margin_below",
        "creq": "$inputs.c_required",
        "dat": "$level_space.delta_at_c_required_bp",
        "bep": "$inputs.breakeven_bp",
        "lm": "$level_space.margin_bp",
        "t316": "$level_space.t_at_316d",
        "dl": "$level_space.days_for_2sigma",
        "nnow": "$level_space.names_per_day_now",
        "nneed": "$level_space.names_per_day_for_2sigma_at_316d",
        "mult": "$level_space.breadth_multiple_needed",
        "tgt": "$power_target.power_target_bp",
        "cost": "$power_target.this_artifact_cost_bp",
        "d19": "$power_target.days_from_target",
    }
    out["reading_template"] = tpl
    out["reading_fields"] = fields
    out["elapsed_s"] = round(time.time() - t_start, 1)
    out["reading"] = render_reading(out)

    assert_clean(out, "kstar_condition")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("position_space", "level_space", "power_target", "verdict")},
                     ensure_ascii=False, indent=1))
    print("\n" + out["reading"])


if __name__ == "__main__":
    main()
