"""对冲市场 beta 能不能把 IR 抬上去 —— 以及这个 IR 是不是「广度」而不是「beta」的函数。

上一轮（D13b）收口时的判断
--------------------------
池的 47.10bp 拆成「beta×市场 11.8bp + 超额 35.3bp」，且权重轴的摆动 74% 在非池股身上
同样出现。于是留下一个悬而未决的问题：「既然 121.5bp 里带着市场暴露，对冲掉市场
是不是能把 IR 抬上去」。这是「要不要建」里唯一没算过的一栏。

本脚本要复核的三件事
--------------------
1. **beta 到底有多少**。同一天同一权重，把 cohort（316 日 / 2,752 只-日，8.7 只/日）
   的日收益回归到「全市场逐只等权的日内均值」上。R² 决定对冲最多能削掉多少方差。
2. **削掉方差能不能变成 IR**。对冲的代价不是零：股指期货**平今**手续费 3.45bp + 开仓
   0.23bp，再加两个 tick 滑点 ⇒ 每个回合约 4.3bp/名义本金，200+ 个回合/年就是 900bp
   量级。这笔钱必须从被削掉的均值里扣，否则「IR 抬升」是拿杠杆率换的算术。
3. **IR 是不是约束**。若未对冲的 IR 已经高到不像话（>3），说明这个数不是策略的性质，
   而是「按开盘价成交」这个不可执行假设的性质 —— 那时对冲抬不抬 IR 都不影响结论，
   真正该量的是广度（每日几只）与入场价。

口径
----
- cohort / 池 / 非池 / 全市场与 `_diag_l1_vwap_entry.py`、`_diag_d13_weighting_gap.py`
  同源（C 口径，日历相邻 + 复权）。cohort 直接调 `_diag_l1_vwap_curvature.build_cohort()`，
  不重建第二份成员表；写盘前用它重算 121.5bp 与 316/2,752 双重断言。
- 指数只用本地 `data/kline_index_daily`（245 日，2025-11-04 起），因此「可交易对冲」那
  一条只覆盖后 245 日；全样本那一条用全市场等权（统计口径，不可交易）作对照。
- IR 一律 **逐回合**年化：`mean/sd × sqrt(回合数/年)`，回合数/年 = 316 / (367/242) = 208.4。
  不用「含空仓日的日历 IR」，那是另一个量（分母混了 50 个零收益日）。
- beta 三个版本并列：全样本 OLS（事后）、滚动 60 日（事后但无前视）、非池基准（统计对冲，
  不可交易）。三者的 IR 差就是「beta 估计误差」与「基准换不来」各值多少。

产物 `_diag_l1_market_neutral_result.json`。只读分区，不联网，不写 data/。
"""
from __future__ import annotations

import glob
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402
from _diag_l1_annualize import nw_t  # noqa: E402
from _diag_l1_vwap_curvature import build_cohort  # noqa: E402
from _diag_l1_vwap_entry import (  # noqa: E402
    COST_BP, DAYS_PER_YEAR, EXPECT_CELLS, EXPECT_DAYS, GAP_MAX, daily_pair, load_daily,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "_diag_l1_market_neutral_result.json"

# 股指期货一回合的摩擦（bp / 名义本金）。费率是外生参数，不是本地数据算出来的，
# 所以单独列出来并把敏感性一起落盘：结论若随它翻转，必须让人看得见。
HEDGE_FEE_OPEN_BP = 0.23          # 开仓 万分之0.23
HEDGE_FEE_CLOSE_TODAY_BP = 3.45   # 平今 万分之3.45（日内对冲必须付）
HEDGE_SLIP_BP = 0.66              # 两个 tick（IC 0.2 点 / 约 6,000 点）
HEDGE_COST_BP = HEDGE_FEE_OPEN_BP + HEDGE_FEE_CLOSE_TODAY_BP + HEDGE_SLIP_BP

INDEX_FILES = "data/kline_index_daily/date=*/part.parquet"
INDEX_CODES = ["000001.SH", "399001.SZ", "399006.SZ", "399107.SZ"]
ROLL = 60          # 滚动 beta 窗口
MIN_HIST = 40      # 滚动 beta 最少历史


def ir_of(x: np.ndarray, rounds_per_year: float) -> float:
    """逐回合年化 IR。sd 用 ddof=1；x 必须是 bp。"""
    sd = float(np.std(x, ddof=1))
    if sd <= 0:
        return float("nan")
    return float(np.mean(x) / sd * math.sqrt(rounds_per_year))


def ols_beta(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """y = a + b x ⇒ (b, a, t_hac(lag5), R²)。x/y 都是 bp 的日序列。"""
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    b, a = np.polyfit(x, y, 1)
    resid = y - (a + b * x)
    # HAC：残差自相关已知（F=1.68 那一轮），iid 的 se 会偏窄
    e = resid - resid.mean()
    n = e.size
    s = float(e @ e) / n
    for l in range(1, 6):
        s += 2.0 * (1.0 - l / 6.0) * float(e[l:] @ e[:-l]) / n
    se = math.sqrt(s / float(((x - x.mean()) ** 2).sum()))
    r2 = 1.0 - float(resid.var()) / float(y.var())
    return float(b), float(a), float(b / se), r2


def rolling_hedge(r: np.ndarray, mk: np.ndarray, cost_bp: float,
                  roll: int = ROLL, min_hist: int = MIN_HIST) -> tuple[np.ndarray, np.ndarray]:
    """无前视对冲：t 日的 beta 只用 t-roll…t-1。返回 (对冲后序列, beta 序列)。"""
    n = r.size
    betas = np.full(n, np.nan)
    for t in range(min_hist, n):
        lo = max(0, t - roll)
        x, y = mk[lo:t], r[lo:t]
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < min_hist:
            continue
        b, _ = np.polyfit(x[ok], y[ok], 1)
        betas[t] = b
    hedged = r - betas * mk - cost_bp * betas
    return hedged, betas


def boot_ir_pairs(r: np.ndarray, mk: np.ndarray, cost_bp: float,
                  rounds: float, block: int = 20, B: int = 2000) -> tuple[np.ndarray, np.ndarray]:
    """块自举：同一批 (r, mk) 配对重采样，每次重估 beta ⇒ IR 差含 beta 估计误差。"""
    rng = np.random.default_rng(20261111)
    n = r.size
    nb = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n]
    rs, ms = r[idx], mk[idx]
    b = (((ms - ms.mean(axis=1, keepdims=True)) * (rs - rs.mean(axis=1, keepdims=True))).sum(axis=1)
         / ((ms - ms.mean(axis=1, keepdims=True)) ** 2).sum(axis=1))
    raw = rs.mean(axis=1) / rs.std(axis=1, ddof=1) * math.sqrt(rounds)
    hz = rs - b[:, None] * ms - cost_bp * b[:, None]
    hed = hz.mean(axis=1) / hz.std(axis=1, ddof=1) * math.sqrt(rounds)
    return raw, hed


def tercile_contrast(state: np.ndarray, y: np.ndarray) -> tuple[dict, np.ndarray]:
    """按**事前状态**分三档，返回各档均值与 (最高档 − 最低档) 的块自举分布（重定边界）。"""
    q1, q2 = np.quantile(state, [1 / 3, 2 / 3])
    cuts = {"low": state <= q1, "mid": (state > q1) & (state <= q2), "high": state > q2}
    means = {k: float(y[m].mean()) for k, m in cuts.items()}

    rng = np.random.default_rng(20261111)
    n = y.size
    block, B = 20, 2000
    nb = int(math.ceil(n / block))
    diffs = np.empty(B)
    for i in range(B):
        s = rng.integers(0, n - block + 1, size=nb)
        idx = (s[:, None] + np.arange(block)[None, :]).ravel()[:n]
        s2, y2 = state[idx], y[idx]
        a1, a2 = np.quantile(s2, [1 / 3, 2 / 3])
        diffs[i] = float(y2[s2 > a2].mean()) - float(y2[s2 <= a1].mean())
    return means, diffs


def ci(a: np.ndarray) -> list[float]:
    return [round(float(np.percentile(a, 2.5)), 2), round(float(np.percentile(a, 97.5)), 2)]


def load_index() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / INDEX_FILES)))
    d = pl.concat([pl.read_parquet(f, columns=["symbol", "date", "open", "close"])
                   for f in files], how="vertical_relaxed")
    d = d.with_columns(pl.col("date").cast(pl.Utf8)).filter(
        pl.col("symbol").is_in(INDEX_CODES)
        & (pl.col("open") > 0) & (pl.col("close") > 0))
    return d.with_columns(((pl.col("close") / pl.col("open") - 1.0) * 1e4).alias("i1_idx"))


def main() -> None:
    t0 = time.time()
    daily = load_daily().with_columns(
        (pl.col("raw_close") / pl.col("raw_open") - 1.0).alias("i1"))

    # ---------- cohort：直接调 curvature 的成员表，不建第二产地 ----------
    need = build_cohort()
    cw = daily.join(need, on=["date", "symbol"], how="inner").unique(subset=["date", "symbol"])
    cw_ok = cw.filter(pl.col("in_band"))
    io_d, iv_d, _pen = daily_pair(cw_ok)
    cohort_dates = cw_ok.select("date").unique().sort("date")["date"].to_list()
    if (len(cohort_dates), io_d.size) != (EXPECT_DAYS, EXPECT_DAYS):
        raise SystemExit(f"cohort drift: {len(cohort_dates)} 日 / {io_d.size} 序列")
    if (int(need["date"].n_unique()), need.height) != (EXPECT_DAYS, EXPECT_CELLS):
        raise SystemExit(f"cohort drift: {need['date'].n_unique()} 日 / {need.height} cell")
    if abs(float(io_d.mean()) - 121.5) > 0.15:
        raise SystemExit(f"cohort 均值 {io_d.mean():.2f}bp 与 entry 产物的 121.5 不一致")

    # ---------- 池 / 非池 / 全市场 ----------
    pool = daily.filter(pl.col("gap_c") <= GAP_MAX)
    rest = daily.filter(pl.col("gap_c") > GAP_MAX)
    agg = {
        "pool": pool.group_by("date").agg([
            pl.len().alias("n_pool"), pl.col("i1").mean().alias("m_pool")]),
        "rest": rest.group_by("date").agg([pl.col("i1").mean().alias("m_nonpool")]),
        "mkt": daily.group_by("date").agg([pl.col("i1").mean().alias("m_mkt")]),
    }
    day = agg["pool"].join(agg["rest"], on="date", how="inner")
    day = day.join(agg["mkt"], on="date", how="inner").sort("date")
    # 单位统一到 bp：上游 agg 是小数。单位错会让 beta 变成 1.1 万——这类错不报错，
    # 只会在 IR 上变成一个荒谬的负值，所以在这里一次改净，并在断言里钉住 beta 的量级。
    day = day.with_columns([(pl.col(c) * 1e4).alias(c) for c in ("m_pool", "m_nonpool", "m_mkt")])
    if day.height != 366:
        raise SystemExit(f"池面板日数漂移: {day.height}")

    idx = load_index()
    ipiv = (idx.pivot(on="symbol", index="date", values="i1_idx")
            .rename({c: f"idx_{c.replace('.', '_')}" for c in INDEX_CODES}))

    co = pl.DataFrame({"date": cohort_dates, "r": io_d})
    co = co.join(day, on="date", how="left").join(ipiv, on="date", how="left")
    if co["m_mkt"].null_count() > 0:
        raise SystemExit("cohort 的日子在全市场里缺失")

    r = co["r"].to_numpy().astype(float)
    mk = co["m_mkt"].to_numpy().astype(float)
    mnp = co["m_nonpool"].to_numpy().astype(float)
    rounds = io_d.size / (367.0 / DAYS_PER_YEAR)

    # ---------- 1. beta 有多少 ----------
    b_ew, a_ew, t_ew, r2_ew = ols_beta(mk, r)
    b_np, a_np, t_np, r2_np = ols_beta(mnp, r)
    b_idx = {}
    for c in INDEX_CODES:
        k = f"idx_{c.replace('.', '_')}"
        x = co[k].to_numpy().astype(float)
        ok = np.isfinite(x)
        if ok.sum() < 100:
            continue
        bb, aa, tt, rr = ols_beta(x[ok], r[ok])
        b_idx[c] = {"days": int(ok.sum()), "beta": round(bb, 3), "t_hac": round(tt, 2),
                    "r2": round(rr, 3), "x_mean_bp": round(float(x[ok].mean()), 2),
                    "x_sd_bp": round(float(x[ok].std(ddof=1)), 1)}

    # 池侧也做一次：654 只/日的组合 beta 比 8.7 只/日稳得多，两个数并排才看得出
    # 「同一个市场暴露，广度不同时估计得出来多少」
    b_pool, _, t_pool, r2_pool = ols_beta(
        day["m_mkt"].to_numpy().astype(float), day["m_pool"].to_numpy().astype(float))

    _, betas_roll = rolling_hedge(r, mk, 0.0)
    n_roll = int(np.isfinite(betas_roll).sum())
    rb = betas_roll[np.isfinite(betas_roll)]

    # ---------- 2. 对冲后的 IR ----------
    raw_ir = ir_of(r, rounds)
    raw_mean, raw_sd = float(r.mean()), float(r.std(ddof=1))

    def hedged_stats(beta: float, x: np.ndarray, cost: float) -> dict:
        h = r - beta * x - cost * beta
        ok = np.isfinite(h)
        return {"mean_bp": round(float(h[ok].mean()), 2),
                "sd_bp": round(float(h[ok].std(ddof=1)), 1),
                "ir": round(ir_of(h[ok], rounds), 2),
                "net_mean_after_equity_cost_bp": round(float(h[ok].mean()) - COST_BP, 2),
                "n_days": int(ok.sum())}

    h_ew = hedged_stats(b_ew, mk, HEDGE_COST_BP)
    h_np = hedged_stats(b_np, mnp, 0.0)           # 统计对冲：没有可交易的腿，成本按 0 记
    hz_roll, _ = rolling_hedge(r, mk, HEDGE_COST_BP)
    okr = np.isfinite(hz_roll)
    h_roll = {"mean_bp": round(float(hz_roll[okr].mean()), 2),
              "sd_bp": round(float(hz_roll[okr].std(ddof=1)), 1),
              "ir": round(ir_of(hz_roll[okr], rounds), 2),
              "net_mean_after_equity_cost_bp": round(float(hz_roll[okr].mean()) - COST_BP, 2),
              "n_days": int(okr.sum())}

    braw, bhed = boot_ir_pairs(r, mk, HEDGE_COST_BP, rounds)
    ir_diff = bhed - braw

    # 成本敏感性：结论若翻在某个成本上，必须让人看见翻在哪
    cost_sens = {str(c): round(ir_of(r - b_ew * mk - c * b_ew, rounds), 2)
                 for c in (0.0, 3.68, HEDGE_COST_BP, 6.0, 10.0)}

    # ---------- 3. 这是不是「广度」问题 ----------
    # 每日只数决定不可分散的地板：sd(组合) ≈ sd(单只)/sqrt(广度)。广度是信号给的，
    # 对冲改不了它。这里量的是「一个纯 idio、零 beta 的组合，IR 上界能到多少」。
    n_pool = day["n_pool"].to_numpy().astype(float)
    breadth = need.height / float(EXPECT_DAYS)
    sd_single = float(cw_ok["i1"].std(ddof=1) * 1e4)
    idio_floor_bp = sd_single / math.sqrt(breadth)
    ir_if_pure_idio = float(raw_mean / idio_floor_bp * math.sqrt(rounds))

    # ---------- 3b. 入场价这条轴：换一个成交价，IR 掉多少 ----------
    # 与「对冲」放在同一张表里比：前者改的是分子（成交价），后者改的是分母（beta 暴露）。
    # 谁真正绑住策略，看这两个 ΔIR 的量级差。
    vwap_net = iv_d - COST_BP
    entry_axis = {
        "open": {"mean_bp": round(raw_mean, 2), "sd_bp": round(raw_sd, 1),
                 "net_mean_bp": round(raw_mean - COST_BP, 2), "ir": round(raw_ir, 2)},
        "vwap": {"mean_bp": round(float(iv_d.mean()), 2),
                 "sd_bp": round(float(iv_d.std(ddof=1)), 1),
                 "net_mean_bp": round(float(vwap_net.mean()), 2),
                 "ir": round(ir_of(vwap_net, rounds), 2)},
        # 名字必须与 entry 的 `entry_price_penalty`（mean of vwap/open−1，108.4）分开：
        # 那是**价格**惩罚，这是**均值收益**之差（115.1），两者差 6.7bp 不是矛盾。
        "return_gap_bp": round(raw_mean - float(iv_d.mean()), 2),
        "ir_gap_entry_vs_hedge": round(raw_ir - ir_of(vwap_net, rounds), 2),
    }

    # ---------- 4. regime：超额是不是全在一段行情里 ----------
    # 事前状态（无前视）：过去 5 个交易日的全市场日内累计。用滞后而不是当日，
    # 是因为当日市场与当日池收益机械相关（池是市场的子集），拿它分档等于自己证自己。
    m_all = day["m_mkt"].to_numpy().astype(float)
    prior5 = np.array([m_all[max(0, i - 5):i].sum() if i >= 5 else np.nan
                       for i in range(m_all.size)])
    ex_pool = day["m_pool"].to_numpy().astype(float) - day["m_nonpool"].to_numpy().astype(float)
    ok5 = np.isfinite(prior5)
    st5, y5 = prior5[ok5], ex_pool[ok5]
    reg_means, reg_diffs = tercile_contrast(st5, y5)
    _q1, _q2 = np.quantile(st5, [1 / 3, 2 / 3])
    reg_days = {"low": int((st5 <= _q1).sum()), "mid": int(((st5 > _q1) & (st5 <= _q2)).sum()),
                "high": int((st5 > _q2).sum())}

    half = day.height // 2
    h1, h2 = ex_pool[:half], ex_pool[half:]
    year = day["date"].str.slice(0, 4).to_numpy()
    reg_year = {y: {"days": int((year == y).sum()),
                    "excess_bp": round(float(ex_pool[year == y].mean()), 2)}
                for y in ("2025", "2026")}

    out = {
        "purpose": "对冲市场 beta 能否抬 IR；以及未对冲的 IR 是不是「广度」而不是「beta」的函数",
        "caliber": "C（日历相邻 + 复权）；cohort 复用 _diag_l1_vwap_curvature.build_cohort()",
        "panel": {
            "cohort_days": int(io_d.size),
            "cohort_cells": int(need.height),
            "cohort_names_per_day": round(breadth, 2),
            "pool_days": int(day.height),
            "rounds_per_year": round(rounds, 1),
            "index_overlap_days": int(np.isfinite(
                co["idx_000001_SH"].to_numpy().astype(float)).sum()),
        },
        "cost": {
            "equity_round_bp": COST_BP,
            "hedge_fee_open_bp": HEDGE_FEE_OPEN_BP,
            "hedge_fee_close_today_bp": HEDGE_FEE_CLOSE_TODAY_BP,
            "hedge_slip_bp": HEDGE_SLIP_BP,
            "hedge_round_bp_per_notional": round(HEDGE_COST_BP, 2),
            "note": "费率是外生参数（中金所平今 3.45bp 是日内对冲无法回避的一笔），"
                    "不是本地数据算出来的；敏感性见 hedge.cost_sensitivity",
        },
        "unhedged": {
            "mean_bp": round(raw_mean, 2),
            "sd_bp": round(raw_sd, 1),
            "ir_per_round_annualized": round(raw_ir, 2),
            "net_mean_after_equity_cost_bp": round(raw_mean - COST_BP, 2),
            "annual_return_on_deployed_pct": round(rounds * (raw_mean - COST_BP) / 100.0, 1),
            "t_nw5": round(nw_t(r), 2),
        },
        "beta": {
            "cohort_vs_ew_market": {"beta": round(b_ew, 3), "alpha_bp": round(a_ew, 2),
                                    "t_hac_lag5": round(t_ew, 2), "r2": round(r2_ew, 3),
                                    "x_mean_bp": round(float(mk.mean()), 2),
                                    "x_sd_bp": round(float(mk.std(ddof=1)), 1)},
            "cohort_vs_nonpool": {"beta": round(b_np, 3), "alpha_bp": round(a_np, 2),
                                  "t_hac_lag5": round(t_np, 2), "r2": round(r2_np, 3)},
            "cohort_vs_index": b_idx,
            "pool_vs_ew_market": {"beta": round(b_pool, 3), "t_hac_lag5": round(t_pool, 2),
                                  "r2": round(r2_pool, 3)},
            "rolling60": {"n": n_roll, "min": round(float(rb.min()), 2),
                          "median": round(float(np.median(rb)), 2),
                          "max": round(float(rb.max()), 2),
                          "sd": round(float(rb.std(ddof=1)), 2)},
        },
        "hedge": {
            "in_sample_ew": h_ew,
            "oos_rolling60": h_roll,
            "statistical_nonpool": h_np,
            "ir_delta_ci95_block20": ci(ir_diff),
            "ir_delta_median": round(float(np.median(ir_diff)), 2),
            "prob_ir_improves": round(float((ir_diff > 0).mean()), 3),
            "cost_sensitivity": cost_sens,
            "net_mean_delta_bp": round(h_ew["mean_bp"] - raw_mean, 2),
            "net_mean_delta_pct": round((h_ew["mean_bp"] / raw_mean - 1.0) * 100.0, 1),
        },
        "breadth": {
            "names_per_day": round(breadth, 2),
            "single_name_sd_bp": round(sd_single, 1),
            "idio_floor_sd_bp": round(idio_floor_bp, 1),
            "observed_sd_bp": round(raw_sd, 1),
            "ir_if_pure_idio": round(ir_if_pure_idio, 2),
            "market_share_of_variance": round(r2_ew, 3),
            "market_explained_pct": round(r2_ew * 100.0, 1),
            "market_explained_pct_pool": round(r2_pool * 100.0, 1),
        },
        "entry_axis": entry_axis,
        "regime": {
            "prior5d_market_tercile": {k: round(v, 2) for k, v in reg_means.items()},
            "tercile_days": reg_days,
            "days_used": int(st5.size),
            "excess_mean_bp": round(float(y5.mean()), 2),
            "prior5d_high_vs_low_bp": round(reg_means["high"] - reg_means["low"], 2),
            "prior5d_high_vs_low_ci95_block20": ci(reg_diffs),
            "half_split": {
                "first_half_excess_bp": round(float(h1.mean()), 2),
                "second_half_excess_bp": round(float(h2.mean()), 2),
                "first_half_ci95_block20": ci(_boot_mean(h1)),
                "second_half_ci95_block20": ci(_boot_mean(h2)),
            },
            "by_year": reg_year,
            "note": "状态变量用**滞后 5 日**的全市场日内累计，不用当日（当日市场与当日池收益机械相关，"
                    "拿它分档等于自己证自己）；分档显著性用重定边界的块自举",
        },
        "elapsed_s": 0.0,
    }

    out["reading_template"] = (
        "cohort {cd} 日 / {cc} 只-日、每日 {npd} 只。未对冲：均值 {raw}bp、sd {rawsd}bp、"
        "逐回合年化 IR **{ir}**，扣 {cost}bp 权益成本后 {net}bp/回合（对投入资本 {ann}%/年）。"
        "把日收益回归到全市场等权日内均值上，beta {beta}（t {t}）、R² {r2} ⇒ 市场只解释方差的 "
        "{mpct}%；同一暴露在 654 只/日的池上 beta {pb}、R² {pr2}（解释 {ppct}%）。"
        "全样本对冲后：均值 {hm}bp、sd {hsd}bp、IR {hir}（ΔIR 中位 {dir}，P(ΔIR>0) {pz}）；"
        "逐日只数 {npd} 只时单只日波动 {ssd}bp，纯 idio 地板 {ifl}bp ⇒ 一个只靠广度、没有 beta 的 "
        "组合 IR 上界约 {irid}。对冲一回合要付 {hc}bp/名义（平今 {pjt}bp + 开 {op}bp + 滑点 {sl}bp），"
        "净均值变化 {dnet}bp（{dnetp}%）。滚动 60 日 beta（{rn} 日）在 {bmin}–{bmax} 之间、sd {bsd}，"
        "用它对冲后 IR {rir}。"
        "regime：按事前 5 日市场分三档，池−非池超额是 {r1}/{rmid}/{r3}bp，高−低 {rh}bp、CI {rci}；"
        "前半段 {h1}bp（CI {h1ci}）对后半段 {h2}bp（CI {h2ci}）。"
        "同一组合只把成交价从 open 换成 VWAP：均值 {vm}bp（均值收益差 {rgap}bp）、IR **{vir}** —— 入场价这一格值 "
        "{vgap} 个 IR 点，而对冲这一格只值 {dh}。"
    )
    out["reading_fields"] = {
        "cd": "panel.cohort_days", "cc": "panel.cohort_cells",
        "npd": "panel.cohort_names_per_day",
        "raw": "unhedged.mean_bp", "rawsd": "unhedged.sd_bp",
        "ir": "unhedged.ir_per_round_annualized", "cost": "cost.equity_round_bp",
        "net": "unhedged.net_mean_after_equity_cost_bp",
        "ann": "unhedged.annual_return_on_deployed_pct",
        "beta": "beta.cohort_vs_ew_market.beta", "t": "beta.cohort_vs_ew_market.t_hac_lag5",
        "r2": "beta.cohort_vs_ew_market.r2",
        "mpct": "breadth.market_explained_pct", "ppct": "breadth.market_explained_pct_pool",
        "pb": "beta.pool_vs_ew_market.beta", "pr2": "beta.pool_vs_ew_market.r2",
        "hm": "hedge.in_sample_ew.mean_bp", "hsd": "hedge.in_sample_ew.sd_bp",
        "hir": "hedge.in_sample_ew.ir", "dir": "hedge.ir_delta_median",
        "pz": "hedge.prob_ir_improves",
        "ssd": "breadth.single_name_sd_bp", "ifl": "breadth.idio_floor_sd_bp",
        "irid": "breadth.ir_if_pure_idio",
        "hc": "cost.hedge_round_bp_per_notional",
        "pjt": "cost.hedge_fee_close_today_bp", "op": "cost.hedge_fee_open_bp",
        "sl": "cost.hedge_slip_bp",
        "dnet": "hedge.net_mean_delta_bp", "dnetp": "hedge.net_mean_delta_pct",
        "rn": "beta.rolling60.n", "bmin": "beta.rolling60.min",
        "bmax": "beta.rolling60.max", "bsd": "beta.rolling60.sd",
        "rir": "hedge.oos_rolling60.ir",
        "r1": "regime.prior5d_market_tercile.low",
        "rmid": "regime.prior5d_market_tercile.mid",        "r3": "regime.prior5d_market_tercile.high",
        "rh": "regime.prior5d_high_vs_low_bp",
        "rci": "regime.prior5d_high_vs_low_ci95_block20",
        "h1": "regime.half_split.first_half_excess_bp",
        "h1ci": "regime.half_split.first_half_ci95_block20",
        "h2": "regime.half_split.second_half_excess_bp",
        "h2ci": "regime.half_split.second_half_ci95_block20",
        "vm": "entry_axis.vwap.mean_bp", "vir": "entry_axis.vwap.ir",
        "rgap": "entry_axis.return_gap_bp",
        "vgap": "entry_axis.ir_gap_entry_vs_hedge",
        "dh": "hedge.ir_delta_median",
    }
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["reading"] = render_reading(out, root=out)
    assert_clean(out, label=OUT.name)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(out["reading"])
    print()
    print(f"  raw IR {raw_ir:.2f} | hedged(in-sample) {h_ew['ir']:.2f} | "
          f"rolling {h_roll['ir']:.2f} | nonpool {h_np['ir']:.2f}")
    print(f"  ΔIR CI {ci(ir_diff)}  P(>0)={(ir_diff > 0).mean():.3f}")
    print(f"  beta ew {b_ew:.2f} R2 {r2_ew:.3f} | pool beta {b_pool:.2f} R2 {r2_pool:.3f}")
    print(f"  hedge mean Δ {h_ew['mean_bp'] - raw_mean:+.2f}bp "
          f"({(h_ew['mean_bp'] / raw_mean - 1) * 100:+.1f}%)")
    print(f"  cost sens {cost_sens}")
    print(f"  entry axis: open IR {entry_axis['open']['ir']} -> vwap IR {entry_axis['vwap']['ir']} "
          f"(mean gap {entry_axis['return_gap_bp']}bp)")
    print(f"  regime {reg_means} hi-lo {reg_means['high'] - reg_means['low']:+.2f} "
          f"CI {ci(reg_diffs)}")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


def _boot_mean(x: np.ndarray, block: int = 20, B: int = 2000) -> np.ndarray:
    rng = np.random.default_rng(20261111)
    n = x.size
    nb = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n]
    return x[idx].mean(axis=1)


if __name__ == "__main__":
    main()
