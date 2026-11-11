"""把 3.55% 从「点估计之比」升级成「区间之比」：8864bp 的分母有没有 CI。

`docs/asymmetry-summary-audit.md` §3 用界替掉了 t=0.12：

    前瞻 20 日超额 95% 上界 +315bp <= 选择窗内超额 8864bp 的 3.6%

这句话只有分子有区间（`arms.mom_top20.pooled.S8.bb` = [-277.40, +314.78]），
分母 +8864bp 是 `round4:253` 表里的**点值**，`_diag_gaprev_result.json` 的
`mom20_cross_section.reconciliation` 也只存了四个标量。缺一次对它自己的重抽样。

这个脚本补的只有那一件事，但先要证明它算的是同一个估计量：

    [G] 里的 88.64% 不是「逐 cohort 超额」的均值，而是
        mean(mom20) - nanmean(matched_mom20)     （两个均值相减）

所以逐 cohort 差 d_i = mom20_i - matched_mom20_i 的均值**恰好**等于 88.64%
（两者同 n、match 向量无 NaN 时逐项相减可交换）。这条恒等式是本脚本的
第一个闸：它不成立，后面的 CI 就不是 88.64% 的 CI。

预注册（写于运行之前）：

    R-P1 复现闸。 必须先复现 reconciliation 的五个字段到两位小数
          （96.81 / 96.81 / 1.0 / 8.17 / 88.64）。任一不符 → 退出码 2，
          不写任何 CI。CI 的合法性只建立在「与点值同一个估计量」上。

    R-P2 块长阶梯。 block ∈ {1,5,10,20,40,60}，B=2000，seed 20261109。
          主读数取 block=20（与 S8 同一个估计器，同 20 日重叠）。阶梯用来
          判断宽度是否已上平台：block>=20 之后相邻两档的半宽相对变化
          <=15% 记为有平台；没有平台则 block=20 的读数不可采纳。

    R-P3 比值口径。 点估计之比 = 314.78 / den_point。**合法上界** =
          num_ci95 / den_ci5（分子取上界、分母取下界，两边都朝同一方向
          放宽）。这条定义了一个能红的阈值：
              den_ci5 <= 629.6bp  ⇒ 上界 >= 50%，「收益全在选择窗内」
              退化为不可判定。
          629.6bp 是 8864bp 的 7.1%，即分母要缩到 1/14 才翻。

    R-P4 MC 误差。 5 个 seed 的 CI 端点极差必须 < 块长阶梯半宽跨度的 1/3，
          否则 B=2000 不足，读数标 ±。

对拍（R-P2 之前跑）：本脚本的循环块实现与 `_diag_regime2.block_boot` 在
同一数组、同一 block=20 上必须给同一组端点（同 seed 下逐位相同）。

ASCII 输出（GBK 控制台）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import _diag_gaprev as gp  # noqa: E402
import _diag_regime2 as rg  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
N_BOOT = 2000
BLOCK_MAIN = 20
BLOCKS = (1, 5, 10, 20, 40, 60)
SEEDS = (20261109, 20261110, 20261111, 20261112, 20261113)
N_DECILE = rg.N_DECILE if hasattr(rg, "N_DECILE") else 10
K_MAX = 10
PICK_FIELD = "mom20"
ASCENDING = False  # mom_top20

#: S8 的前瞻 20 日超额 95% 区间（bp），来自 arms.mom_top20.pooled.S8.bb
NUM_CI95_BP = 314.78
#: R-P3 的翻盘阈值：上界比 >= 50% 时的分母
BREAK_DEN_BP = NUM_CI95_BP / 0.50


def circ_block_mean_idx(n: int, block: int, n_boot: int, seed: int) -> np.ndarray:
    """circular moving-block 重抽样的下标矩阵，[n_boot, n]。"""
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    off = np.arange(block)
    starts = rng.integers(0, n, size=(n_boot, nb))
    idx = (starts[:, :, None] + off[None, None, :]) % n
    return idx.reshape(n_boot, -1)[:, :n]


def boot_mean(x: np.ndarray, idx: np.ndarray) -> np.ndarray:
    return x[idx].mean(axis=1)


def ci_of(x: np.ndarray, block: int, n_boot: int, seed: int) -> dict:
    idx = circ_block_mean_idx(x.size, block, n_boot, seed)
    out = boot_mean(x, idx)
    return {
        "block": block,
        "n": int(x.size),
        "n_boot": n_boot,
        "seed": seed,
        "mean": float(x.mean()),
        "ci5": float(np.percentile(out, 2.5)),
        "ci95": float(np.percentile(out, 97.5)),
        "half_width": float((np.percentile(out, 97.5) - np.percentile(out, 2.5)) / 2.0),
        "p_le0": round(float((out <= 0).mean()), 4),
    }


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    pool_base = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    to20 = ac.grid_of(df, "to20", dates, symbols)
    mom_grid = ac.grid_of(df, PICK_FIELD, dates, symbols)
    G = gp.session_grids(mats)
    print(f"panel dates={T} symbols={S}")

    picks_rows = ap.pick_daily(df, PICK_FIELD, ASCENDING).select("date", "symbols").to_dicts()
    setups = []
    for r in picks_rows:
        i0 = date_index[str(r["date"])]
        if i0 + K_MAX >= T:
            continue
        st = gp.cohort_setup(i0, np.array([s_map[x] for x in r["symbols"]], dtype=np.int64),
                             pool_base, to20, S)
        if st is None:
            continue
        setups.append((i0, st))
    print(f"cohorts set up = {len(setups)}")

    # ---- 逐 cohort 重算 [G]，并保留序列 ----
    lhs, rhs, mat_mom, cidx = [], [], [], []
    for i0, st in setups:
        if i0 - 20 < 1:
            continue
        sel = st["sel"]
        ok = np.ones(sel.size, dtype=bool)
        leg = np.ones(sel.size)
        for s in range(i0 - 19, i0 + 1):
            v = G["c2c1"][s][sel]
            ok &= np.isfinite(v)
            leg = leg * np.where(np.isfinite(v), 1.0 + v, 1.0)
        if ok.sum() < 5:
            continue
        lhs.append(float(np.mean(leg[ok] - 1.0)))
        rhs.append(float(np.mean(mom_grid[i0][sel][ok])))
        mv = mom_grid[i0][st["pool"]]
        dmean = np.array([
            float(np.nanmean(mv[st["dec"] == k])) if (st["dec"] == k).any() else np.nan
            for k in range(N_DECILE)
        ])
        mat_mom.append(float(np.nansum(st["w"] * dmean)))
        cidx.append(i0)
    lhs, rhs, mat_mom = np.array(lhs), np.array(rhs), np.array(mat_mom)
    cidx = np.array(cidx)

    # ---- R-P1 复现闸 ----
    rec_now = {
        "n_cohorts": int(lhs.size),
        "compounded_20d_legs_pct": round(float(lhs.mean()) * 100, 2),
        "mom20_pct": round(float(rhs.mean()) * 100, 2),
        "ratio": round(float(lhs.mean() / rhs.mean()), 6) if rhs.mean() else None,
        "mom20_median_pct": round(float(np.median(rhs)) * 100, 2),
        "matched_mom20_pct": round(float(np.nanmean(mat_mom)) * 100, 2),
        "mom20_excess_over_matched_pct": round(
            float(rhs.mean() * 100 - np.nanmean(mat_mom) * 100), 2),
    }
    stored = json.loads((Path("analysis") / "_diag_gaprev_result.json")
                        .read_text(encoding="utf-8"))
    # 路径是 arms.mom_top20.reconciliation —— 不是 mom20_cross_section.reconciliation。
    # `mom20_cross_section` 只有 {60,150,250,330,pooled} 五个键，`pooled` 里只有
    # p999_pct / frac_above_90pct；[G] 写在每个 arm 内部。这是与 C1 同一类的错
    # （丢掉 `arms.<label>` 这一层），而它在审计文档自己的复现清单第一行上。
    ref = stored["arms"]["mom_top20"]["reconciliation"]
    print(f"[R-P1] recomputed: legs {rec_now['compounded_20d_legs_pct']:+.2f}% "
          f"mom20 {rec_now['mom20_pct']:+.2f}% ratio {rec_now['ratio']:.6f} "
          f"matched {rec_now['matched_mom20_pct']:+.2f}% excess "
          f"{rec_now['mom20_excess_over_matched_pct']:+.2f}%")

    guard_keys = ("n_cohorts", "compounded_20d_legs_pct", "mom20_pct", "ratio",
                  "mom20_median_pct", "matched_mom20_pct",
                  "mom20_excess_over_matched_pct")
    mismatch = {k: {"now": rec_now[k], "stored": ref.get(k)}
                for k in guard_keys if rec_now[k] != ref.get(k)}
    if mismatch:
        print("[R-P1] FAIL -- estimator drift, no CI is written:")
        for k, v in mismatch.items():
            print(f"    {k}: now={v['now']} stored={v['stored']}")
        sys.exit(2)
    print("[R-P1] PASS -- same estimator as the stored point value")

    # 两个均值相减 == 逐项差的均值（同 n、match 向量有限）
    finite_match = bool(np.isfinite(mat_mom).all())
    d = rhs - mat_mom
    identity_gap = float(abs(d.mean() * 100 - rec_now["mom20_excess_over_matched_pct"]))
    print(f"[R-P1b] d_i = mom20_i - matched_i : mean {d.mean()*100:+.4f}% | "
          f"identity gap {identity_gap:.2e}pp | matched finite {finite_match}")
    if identity_gap > 0.005:
        print("[R-P1b] FAIL -- mean(rhs-mat) != mean(rhs)-mean(mat); bootstrap target is "
              "not the stored 88.64%")
        sys.exit(2)

    # ---- 对拍：自己的实现 vs _diag_regime2.block_boot（同 seed 首调用） ----
    rng_backup = rg.RNG
    rg.RNG = np.random.default_rng(SEED)
    par = rg.block_boot(d, block=BLOCK_MAIN, n_boot=N_BOOT)
    rg.RNG = rng_backup
    mine = ci_of(d, BLOCK_MAIN, N_BOOT, SEED)
    same = (abs(par["ci5"] - mine["ci5"]) < 1e-12) and (abs(par["ci95"] - mine["ci95"]) < 1e-12)
    print(f"[parity] regime2.block_boot ci=[{par['ci5']*100:+.2f}%, {par['ci95']*100:+.2f}%] "
          f"| mine ci=[{mine['ci5']*100:+.2f}%, {mine['ci95']*100:+.2f}%] | same={same}")
    if not same:
        print("[parity] FAIL -- the two block implementations disagree on the same array")
        sys.exit(2)

    # ---- R-P2 块长阶梯 ----
    ladder = [ci_of(d, b, N_BOOT, SEED) for b in BLOCKS]
    print("\n[R-P2] block ladder (bootstrap of mean per-cohort excess, %):")
    for c in ladder:
        print(f"    block={c['block']:3d}  mean {c['mean']*100:+9.4f}  "
              f"ci [{c['ci5']*100:+8.4f}, {c['ci95']*100:+8.4f}]  "
              f"half {c['half_width']*100:7.4f}  P(<=0) {c['p_le0']:.4f}")
    big = [c for c in ladder if c["block"] >= BLOCK_MAIN]
    rel = [abs(big[i + 1]["half_width"] / big[i]["half_width"] - 1.0)
           for i in range(len(big) - 1)]
    plateau = bool(rel and max(rel) <= 0.15)
    span_big = (max(c["half_width"] for c in big) / min(c["half_width"] for c in big)
                if big else float("nan"))
    print(f"    adjacent relative change (block>=20): "
          f"{', '.join(f'{r*100:.1f}%' for r in rel)} | plateau={plateau} | "
          f"half-width span x{span_big:.2f}")

    # ---- R-P4 MC 误差 ----
    mc = [ci_of(d, BLOCK_MAIN, N_BOOT, s) for s in SEEDS]
    mc_lo = [c["ci5"] for c in mc]
    mc_hi = [c["ci95"] for c in mc]
    mc_range_lo = max(mc_lo) - min(mc_lo)
    mc_range_hi = max(mc_hi) - min(mc_hi)
    ladder_span = (max(c["half_width"] for c in ladder)
                   - min(c["half_width"] for c in ladder))
    mc_ok = bool(max(mc_range_lo, mc_range_hi) < ladder_span / 3.0)
    print(f"\n[R-P4] MC across {len(SEEDS)} seeds at block={BLOCK_MAIN}: "
          f"ci5 range {mc_range_lo*100:.4f}pp, ci95 range {mc_range_hi*100:.4f}pp "
          f"| ladder half-width span {ladder_span*100:.4f}pp | ok={mc_ok}")

    # ---- R-P3 比值 ----
    den_point_bp = rec_now["mom20_excess_over_matched_pct"] * 100.0
    # ci5/ci95 是**比例**（0.8423 = 84.23%），换成 bp 要 x1e4，不是 x100。
    den_ci5_bp = mine["ci5"] * 1e4
    den_ci95_bp = mine["ci95"] * 1e4
    ratio_point = NUM_CI95_BP / den_point_bp * 100.0
    ratio_bound = NUM_CI95_BP / den_ci5_bp * 100.0
    print(f"\n[R-P3] denominator: point {den_point_bp:+.2f}bp  "
          f"ci [{den_ci5_bp:+.2f}, {den_ci95_bp:+.2f}]bp  "
          f"(n_cohorts {rec_now['n_cohorts']})")
    print(f"       numerator  (S8 95% upper): {NUM_CI95_BP:+.2f}bp")
    print(f"       ratio point (num/den_point) = {ratio_point:.3f}%")
    print(f"       ratio BOUND (num_ci95/den_ci5) = {ratio_bound:.3f}%")
    print(f"       flips to >=50% only if denominator <= {BREAK_DEN_BP:.1f}bp "
          f"({BREAK_DEN_BP / den_point_bp * 100:.2f}% of point; x{den_point_bp/BREAK_DEN_BP:.2f} smaller)")
    print(f"       denominator CI width = x{den_ci95_bp/den_ci5_bp:.2f} of its lower bound")

    out = {
        "seed": SEED,
        "pre_registration": {
            "R-P1": "reproduce the five reconciliation fields to 2dp or exit 2",
            "R-P2": f"block ladder {BLOCKS}, B={N_BOOT}, main block={BLOCK_MAIN}, "
                    "plateau = adjacent half-width change <=15% for block>=20",
            "R-P3": "ratio bound = num_ci95 / den_ci5; red if ratio_bound >= 50%",
            "R-P4": "max seed-to-seed CI endpoint range < ladder half-width span / 3",
        },
        "guard": {
            "recomputed": rec_now,
            "stored": ref,
            "mismatch": mismatch,
            "identity_gap_pp": identity_gap,
            "matched_finite": finite_match,
            "passed": True,
        },
        "parity_vs_regime2_block_boot": {
            "regime2": {k: par[k] for k in ("n", "mean", "ci5", "ci95", "p_le0")},
            "this_script": {k: mine[k] for k in ("n_boot", "seed", "block", "mean", "ci5", "ci95", "p_le0")},
            "identical": same,
        },
        "series": {
            "n_cohorts": int(d.size),
            "mean_pct": round(float(d.mean()) * 100, 4),
            "sd_pct": round(float(d.std(ddof=1)) * 100, 4),
            "median_pct": round(float(np.median(d)) * 100, 4),
            "frac_pos": round(float((d > 0).mean()), 4),
            "first_date_index": int(cidx[0]),
            "last_date_index": int(cidx[-1]),
        },
        "denominator_ci_bp": {
            "point": round(den_point_bp, 2),
            "ci5": round(den_ci5_bp, 2),
            "ci95": round(den_ci95_bp, 2),
            "width_ratio": round(den_ci95_bp / den_ci5_bp, 4),
        },
        "block_ladder": ladder,
        "plateau": {"holds": plateau, "adjacent_rel_change": [round(r, 4) for r in rel],
                    "half_width_span_x": round(span_big, 4)},
        "mc_error": {"seeds": list(SEEDS), "ci5_range_pp": round(mc_range_lo * 100, 4),
                     "ci95_range_pp": round(mc_range_hi * 100, 4), "ok": mc_ok,
                     "per_seed": [{k: c[k] for k in ("seed", "ci5", "ci95")} for c in mc]},
        "ratio": {
            "numerator_bp": NUM_CI95_BP,
            "point_pct": round(ratio_point, 4),
            "bound_pct": round(ratio_bound, 4),
            "break_even_denominator_bp": round(BREAK_DEN_BP, 2),
            "break_even_as_share_of_point": round(BREAK_DEN_BP / den_point_bp, 4),
        },
    }
    body = json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True)
    p = Path("analysis") / "_diag_premom_ci_result.json"
    p.write_text(body, encoding="utf-8")
    print(f"\nwrote {p}  sha256={hashlib.sha256(body.encode()).hexdigest()[:16]}  "
          f"elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
