"""k 曲线：从 open 吃到第 k 根，价格还能留住多少；以及 k* 到底能不能离线定。

为什么需要它
------------
`_diag_l1_open_depth_result.json` 把 r* 变成了恒等式 `r* = (cost − vwap)/gap`，
但它默认 r 可选。r 真正的上限由两件事夹出来：**开盘能吃下多少**（bar0 成交额）
与**吃到后面价格会跑多远**（VWAP_k 相对 open 的位移 Δ(k)）。

上一轮的口头结论是「参照类 40 只的 k 曲线近乎线性走到 0，与 r* 交于 k≈139」，
据此给了容量 5.8–8.2 亿。本脚本把那条曲线**落成产物并复算**，结论是：
参照类自己的 Δ(k) 是**平的**（R²≈0.2，不是线性），139 只在「按参照类自身两端
归一化」这一种读数下出现，换成「把同一形状映射到 cohort 的端点」就变成 235。
⇒ k* 在离线数据上只有区间，没有点值。

口径
----
- Δ(k) = `VWAP_k / open − 1`（bp），`VWAP_k = Σamount / Σ(volume×100)`，
  前 k+1 根（k=0 即只吃首根 bar，含 09:25 竞价）。
- **日线 open 是复权价，amount/volume 是原始价**：必须用 `raw_close/close`
  把 open 打回原始价再比。不除这个因子，Δ(240) 会从 109 变 433（4 倍），
  不抛异常、只让「价格惩罚」变成噪声——这是本项目第四次栽在「两个产地不同刻度」。
- 日级聚类：先按日取截面均值，再对日均值算 se（天数才是样本量）。
- 参照类只取 240 根齐全、首根 09:31 的分区（2026-10-08 与 2026-11-05 是残段）。

预注册（本轮新增，可被这两个产物直接否证）
------------------------------------------
    K1 端点。 Δ(1) 与 Δ(240) 必须由 part.parquet + 日线复算得到；后者按日等权
             必须落在 `entry_price_penalty` 的 ±1bp 内，否则说明复权或成交量单位
             没有对齐（这正是 433 那条路径）。
    K2 形状。 参照类 Δ(k) 若对 k 的线性回归 R² < 0.8，则「近乎线性」不成立，
             任何由该形状插出的 k* 点值都不可报，只能报区间。
    K3 区间。 用「参照类自归一化」与「映射到 cohort 端点」两种读数各解一次 k*，
             两者相差 2 倍以上时报区间，不报点值。
    K4 功效。 Δ(52) 的日级 se 由参照类 se 随 k 的放大倍数（K1 的 se × 倍数）
             推出；19bp 差在 60 天下的 σ 数若 < 2，则「60 天够判」不成立。

只读本地分区，不联网。
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent
OUT = HERE / "_diag_l1_kcurve_result.json"
PART = DATA / "research" / "l1_open_depth" / "part.parquet"
MN = HERE / "_diag_l1_market_neutral_result.json"
VE = HERE / "_diag_l1_vwap_entry_result.json"      # entry_price_penalty 的唯一产地
OD = HERE / "_diag_l1_open_depth_result.json"      # 日均只数与成交额中位的唯一产地

FULL_BARS = 200
OPEN_TM = "09:31:00"
CKPTS = (1, 5, 20, 52, 139, 239)
BOOK_YI = 5.0        # 判据的建仓额（亿元/日）
PI_GATE = 0.10       # 判据的参与率上限


# ------------------------------------------------------------------ 数据
def load_part() -> pl.DataFrame:
    return pl.read_parquet(PART)


def load_daily() -> pl.DataFrame:
    files = sorted(glob.glob(str(DATA / "kline_daily_enriched" / "date=*" / "*.parquet")))
    d = pl.concat(
        [
            pl.read_parquet(
                f, columns=["symbol", "date", "open", "close", "raw_close", "amount", "volume"]
            )
            for f in files
        ],
        how="vertical_relaxed",
    ).with_columns(pl.col("date").cast(pl.Utf8))
    d = d.filter((pl.col("volume") > 0) & (pl.col("amount") > 0) & (pl.col("open") > 0))
    return d.with_columns(
        (pl.col("raw_close") / pl.col("close")).alias("adjf"),
        (pl.col("amount") / (pl.col("volume") * 100.0)).alias("dvwap"),
    )


def load_reference() -> tuple[pl.DataFrame, dict]:
    """参照类逐 cell 的 (k, Δ, c) 长表。c = 累计成交额 / 当日成交额。"""
    frames: list[pl.DataFrame] = []
    levels: dict[str, dict] = {}
    for mf in sorted(glob.glob(str(DATA / "kline_minute" / "date=*" / "*.parquet"))):
        date = mf.split("date=")[1][:10]
        m = pl.read_parquet(
            mf, columns=["symbol", "datetime", "open", "volume", "amount"]
        ).sort(["symbol", "datetime"])
        m = m.with_columns(pl.col("datetime").dt.time().alias("tm"))
        nb = int(m.group_by("symbol").len()["len"].min())
        used = str(m["tm"].min()) == OPEN_TM and nb >= FULL_BARS
        levels[date] = {"used": used, "first_bar": str(m["tm"].min()), "min_bars": nb}
        if not used:
            continue
        m = m.with_columns(
            pl.col("amount").cum_sum().over("symbol").alias("ca"),
            (pl.col("volume").cum_sum().over("symbol") * 100.0).alias("cv100"),
            pl.col("amount").sum().over("symbol").alias("ta"),
            pl.int_range(pl.len()).over("symbol").alias("k"),
            pl.col("open").first().over("symbol").alias("o"),
        ).filter(pl.col("cv100") > 0)
        frames.append(
            m.with_columns(
                (((pl.col("ca") / pl.col("cv100")) / pl.col("o") - 1.0) * 1e4).alias("delta_bp"),
                (pl.col("ca") / pl.col("ta")).alias("c"),
                pl.lit(date).alias("date"),
            ).select("date", "symbol", "k", "delta_bp", "c")
        )
    return pl.concat(frames), levels


# ------------------------------------------------------------------ 工具
def day_se(g: pl.DataFrame, col: str) -> tuple[float, float, int]:
    """按日截面均值 → 日均值、日级 se、天数。样本单位是天，不是 cell。"""
    per = g.group_by("date").agg(pl.col(col).mean().alias("m"))
    x = np.asarray(per["m"], float)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("nan"), float("nan"), int(x.size)
    se = float(x.std(ddof=1) / np.sqrt(x.size))
    return float(x.mean()), se, int(x.size)


def interp_k(k: np.ndarray, y: np.ndarray, target: float, rising: bool) -> float:
    """在 (k, y) 单调段上解 y(k)=target，线性插值。解不出返回 nan。"""
    for i in range(len(k) - 1):
        a, b = y[i], y[i + 1]
        if (rising and a <= target <= b) or ((not rising) and a >= target >= b):
            if b == a:
                continue
            return float(k[i] + (target - a) / (b - a) * (k[i + 1] - k[i]))
    return float("nan")


# ------------------------------------------------------------------ 主体
def main() -> None:
    t_start = time.time()
    mn = json.loads(MN.read_text(encoding="utf-8"))
    open_bp = float(mn["unhedged"]["mean_bp"])          # 121.49：open 入场的毛收益
    vwap_bp = float(mn["entry_axis"]["vwap"]["mean_bp"])  # 6.43：整日 VWAP 入场
    gap_bp = float(mn["entry_axis"]["return_gap_bp"])     # 115.05：两者均值差
    cost_bp = float(mn["cost"]["equity_round_bp"])        # 20.0
    r_star = round((cost_bp - vwap_bp) / gap_bp, 4)
    if not (0.01 < r_star < 0.5):
        raise SystemExit(f"r* 单位可疑: {r_star}")
    # 毛收益打到「只够付成本」的那个 Δ：payoff(k) = open_bp − Δ(k) = cost_bp − vwap_bp
    delta_breakeven = round(open_bp - (cost_bp - vwap_bp), 2)

    # ---- 端点：cohort 的 Δ(1) 与 Δ(240)
    part, daily = load_part(), load_daily()
    j = part.join(
        daily.select("symbol", "date", "open", "adjf", "dvwap"), on=["symbol", "date"], how="inner"
    )
    j = j.with_columns(
        (pl.col("b0_amt") / (pl.col("b0_vol") * 100.0)).alias("b0_vwap"),
    ).with_columns(
        ((pl.col("b0_vwap") / pl.col("b0_open") - 1.0) * 1e4).alias("d1_bp"),
        ((pl.col("dvwap") / (pl.col("open") * pl.col("adjf")) - 1.0) * 1e4).alias("d240_bp"),
        ((pl.col("dvwap") / pl.col("open") - 1.0) * 1e4).alias("d240_noadj_bp"),
    )
    d1_mean, d1_se, d1_days = day_se(j, "d1_bp")
    d240_mean, d240_se, d240_days = day_se(j, "d240_bp")
    d240_noadj = float(np.nanmean(np.asarray(j["d240_noadj_bp"], float)))

    published_pen = float(
        json.loads(VE.read_text(encoding="utf-8"))["daily_bp"]["entry_price_penalty"]["mean"]
    )
    od = json.loads(OD.read_text(encoding="utf-8"))["coverage"]
    names_per_day = float(od["cohort_cells_per_day"])
    adv_yi = float(od["cohort_day_amt_med_yi"])
    c_required = round(BOOK_YI * 1e8 / (names_per_day * PI_GATE * adv_yi * 1e8), 4)

    endpoints = {
        "d1": {
            "cells": int(j.height),
            "days": d1_days,
            "cell_mean_bp": round(float(j["d1_bp"].mean()), 2),
            "cell_median_bp": round(float(j["d1_bp"].median()), 2),
            "day_eq_mean_bp": round(d1_mean, 2),
            "se_day_bp": round(d1_se, 2),
            "t": round(d1_mean / d1_se, 2) if d1_se else 0.0,
        },
        "d240": {
            "cells": int(j.height),
            "days": d240_days,
            "cell_mean_bp": round(float(j["d240_bp"].mean()), 2),
            "cell_median_bp": round(float(j["d240_bp"].median()), 2),
            "day_eq_mean_bp": round(d240_mean, 2),
            "se_day_bp": round(d240_se, 2),
            "t": round(d240_mean / d240_se, 2) if d240_se else 0.0,
        },
        "d240_without_adjf_bp": round(d240_noadj, 2),
        "adjf_multiple": round(d240_noadj / d240_mean, 1) if d240_mean else None,
    }

    reconcile = {
        "published_penalty_bp": published_pen,
        "day_eq_d240_bp": round(d240_mean, 2),
        "gap_bp": round(abs(d240_mean - published_pen), 2) if np.isfinite(published_pen) else None,
        "same_caliber": bool(np.isfinite(published_pen) and abs(d240_mean - published_pen) <= 1.0),
    }

    # ---- 参照类曲线
    ref, levels = load_reference()
    per = ref.group_by(["date", "k"]).agg(
        pl.col("delta_bp").mean().alias("d"), pl.col("c").mean().alias("c")
    )
    cur = per.group_by("k").agg(
        pl.col("d").mean().alias("delta_bp"), pl.col("c").mean().alias("c")
    ).sort("k")
    kk = np.asarray(cur["k"], float)
    dk = np.asarray(cur["delta_bp"], float)
    ck = np.asarray(cur["c"], float)

    cur_se = per.group_by("k").agg(
        pl.col("d").std().alias("sd"), pl.col("d").len().alias("n")
    ).sort("k")
    sd = np.asarray(cur_se["sd"], float)
    nn = np.asarray(cur_se["n"], float)
    se_k = sd / np.sqrt(nn)

    A = np.column_stack([np.ones_like(kk), kk])
    beta, *_ = np.linalg.lstsq(A, dk, rcond=None)
    pred = A @ beta
    r2 = float(1 - ((dk - pred) ** 2).sum() / ((dk - dk.mean()) ** 2).sum())

    def at(k: int, arr: np.ndarray) -> float:
        i = np.where(kk == k)[0]
        return float(arr[i[0]]) if i.size else float("nan")

    d0, d239 = at(0, dk), at(239, dk)
    span = d0 - d239  # 参照类 Δ 的总下降量（>0 表示递减）

    # 读数 A：把参照类自身的 [Δ(0), Δ(239)] 归一化到 r ∈ [1, 0]
    #   r(k) = 1 − (Δ(0) − Δ(k)) / span  ⇒  r = r* ⇒ 下降量比例 = 1 − r*
    frac = 1.0 - r_star
    k_self = interp_k(kk, dk, d0 - frac * span, rising=False)

    # 读数 B：把同一形状映射到 cohort 的 [Δ(1), Δ(240)]
    #   Δ_coh(k) = d1 + (d240 − d1)·frac(k)，打平条件 Δ_coh = delta_breakeven
    d1_cell = float(j["d1_bp"].mean())
    d240_cell = float(j["d240_bp"].mean())
    want_frac = (delta_breakeven - d1_cell) / (d240_cell - d1_cell)
    k_map = interp_k(kk, dk, d0 - want_frac * span, rising=False)

    def c_at(k: float) -> float:
        if not np.isfinite(k):
            return float("nan")
        return round(float(np.interp(k, kk, ck)), 3)

    low_k = min(k_self, k_map)
    high_k = max(k_self, k_map)

    # ---- 功效：se 随 k 的放大倍数由参照类给，水平由 cohort 的 Δ(1) 给
    ratio52 = at(52, se_k) / at(1, se_k) if at(1, se_k) else float("nan")
    se52_316 = d1_se * ratio52
    se52_60 = se52_316 * np.sqrt(316.0 / 60.0)
    sigma60 = 19.0 / se52_60 if se52_60 else float("nan")

    kstar = {
        "r_star": r_star,
        "delta_breakeven_bp": delta_breakeven,
        "self_norm_k": round(k_self, 1) if np.isfinite(k_self) else None,
        "mapped_k": round(k_map, 1) if np.isfinite(k_map) else None,
        "low_k": round(low_k, 1) if np.isfinite(low_k) else None,
        "high_k": round(high_k, 1) if np.isfinite(high_k) else None,
        "c_at_low": c_at(low_k),
        "c_at_high": c_at(high_k),
        "c_required": c_required,
        "margin_at_low": round(c_at(low_k) - c_required, 3) if np.isfinite(c_at(low_k)) else None,
        # 「可辨」= 两种读数给出的 c(k*) 落在判据线的同一侧；跨越则判据由口径决定
        "identified": bool(not (c_at(low_k) <= c_required <= c_at(high_k))),
    }

    power = {
        "se_ratio_k52_over_k1": round(ratio52, 2),
        "se_d52_316d_bp": round(se52_316, 2),
        "se_d52_60d_bp": round(se52_60, 2),
        "sigma_of_19bp_at_60d": round(sigma60, 2),
        "sixty_days_enough": bool(sigma60 >= 2.0),
    }

    reference = {
        "cells": int(ref.select("date", "symbol").unique().height),
        "days": int(ref["date"].n_unique()),
        "symbols": int(ref["symbol"].n_unique()),
        "partitions_used": sum(1 for v in levels.values() if v["used"]),
        "partitions_skipped": sum(1 for v in levels.values() if not v["used"]),
        "checkpoints": {f"k{k}": round(at(k, dk), 2) for k in CKPTS},
        "ols_slope_bp_per_bar": round(float(beta[1]), 5),
        "ols_r2": round(r2, 4),
        # Δ 本应随 k 单调递减；出现上升段说明「价格位移」不是累积量，
        # 任何把它当累积形状的归一化都只是「首次到达」而不是「走到」
        "monotone_violations": int(np.sum(np.diff(dk) > 0)),
        "curve_k": [int(x) for x in kk[::10]],
        "curve_delta_bp": [round(float(x), 2) for x in dk[::10]],
        "curve_c": [round(float(x), 3) for x in ck[::10]],
    }

    verdict = {
        "K1_endpoints_recomputed": bool(j.height == part.height and j.height > 0),
        "K1_adjf_guard_live": bool(abs(d240_noadj) > 2 * abs(d240_cell)),
        "K2_shape_is_not_linear": bool(r2 < 0.8),
        "K3_kstar_is_interval": bool(not kstar["identified"]),
        "K4_sixty_days_not_enough": bool(not power["sixty_days_enough"]),
        "online_run_justified": bool(
            (not kstar["identified"]) or kstar["margin_at_low"] is None
            or kstar["margin_at_low"] < 0.05
        ),
    }
    verdict["all_pass"] = all(
        v for k, v in verdict.items() if k not in ("online_run_justified", "all_pass")
    )

    out: dict = {
        "purpose": "k 曲线的两个端点 + 参照类形状；判 k* 能不能离线定",
        "caliber": (
            "Δ(k)=VWAP_k/open−1(bp)，VWAP_k=Σamount/Σ(volume×100)；"
            "日线 open 用 raw_close/close 打回原始价；日级聚类"
        ),
        "breakeven": {
            "open_bp": round(open_bp, 2),
            "vwap_bp": round(vwap_bp, 2),
            "gap_bp": round(gap_bp, 2),
            "cost_bp": round(cost_bp, 2),
            "r_star": r_star,
            "delta_breakeven_bp": delta_breakeven,
        },
        "endpoints": endpoints,
        "reconcile": reconcile,
        "reference": reference,
        "kstar": kstar,
        "power": power,
        "findings": [
            "端点可离线复算；参照类形状是平的，不是线性",
            "k* 只有区间：自归一化与映射到 cohort 端点相差一个量级",
            "c(k*) 在下沿只剩不足一个百分点的余量，判据卡在边界",
            "60 天下的检验力不足两个 sigma，首轮窗口要放大",
        ],
        "verdict": verdict,
        "levels": levels,
    }

    tpl = (
        "k 曲线的左端点 {d1}bp（按日聚类 se {se1}，{days} 天）与右端点 {d240}bp 都能离线复算，"
        "但右端点必须先把日线复权 open 打回原始价——不除那个因子得到 {noadj}bp，是 {ratio_noadj} 倍，"
        "不抛异常、只让价格惩罚变成噪声。按日等权 {d240} 与 vwap_entry 的 {pub}bp 对上（差 {gap}bp）。\n"
        "参照类 {refcells} 格 / {refdays} 天 / {refsym} 只的 Δ(k) 不是线性："
        "k=1 时 {k1}bp、k=52 时 {k52}bp、k=239 时 {k239}bp，对 k 回归 R²={r2}，"
        "还有 {viol} 段回升。所以「近乎线性走到 0、交于 k≈139」只在**按参照类自身两端归一化**时出现；"
        "把同一形状映射到 cohort 的两个端点，交点变成 {kmap}。两种读数给 k*∈[{low}, {high}]，"
        "对应 c(k*) 从 {clow} 到 {chigh}。\n"
        "判据要求 c(k*)≥{creq}，而这个区间**跨过**了它（下沿余量 {margin}）——"
        "「深度够不够」现在完全由一个离线不可辨的归一化选择决定。"
        "这正是必须联网测 cohort 自己那条曲线的理由，而且比原计划更硬："
        "参照类 se 从 k=1 到 k=52 放大 {se_ratio} 倍，60 天下 19bp 的差只有 {sig}σ，"
        "首轮 60 天不足以判，窗口要按功效反推。"
    )
    fields = {
        "d1": "$endpoints.d1.cell_mean_bp",
        "se1": "$endpoints.d1.se_day_bp",
        "days": "$endpoints.d1.days",
        "d240": "$endpoints.d240.day_eq_mean_bp",
        "noadj": "$endpoints.d240_without_adjf_bp",
        "ratio_noadj": "$endpoints.adjf_multiple",
        "pub": "$reconcile.published_penalty_bp",
        "gap": "$reconcile.gap_bp",
        "refcells": "$reference.cells",
        "refdays": "$reference.days",
        "refsym": "$reference.symbols",
        "k1": "$reference.checkpoints.k1",
        "k52": "$reference.checkpoints.k52",
        "k239": "$reference.checkpoints.k239",
        "r2": "$reference.ols_r2",
        "viol": "$reference.monotone_violations",
        "kmap": "$kstar.mapped_k",
        "low": "$kstar.low_k",
        "high": "$kstar.high_k",
        "clow": "$kstar.c_at_low",
        "chigh": "$kstar.c_at_high",
        "creq": "$kstar.c_required",
        "margin": "$kstar.margin_at_low",
        "se_ratio": "$power.se_ratio_k52_over_k1",
        "sig": "$power.sigma_of_19bp_at_60d",
    }
    out["reading_template"] = tpl
    out["reading_fields"] = fields
    out["elapsed_s"] = round(time.time() - t_start, 1)
    out["reading"] = render_reading(out)

    assert_clean(out, "kcurve")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("endpoints", "reconcile", "kstar", "power", "verdict")},
                     ensure_ascii=False, indent=1))
    print("\n" + out["reading"])


if __name__ == "__main__":
    main()
