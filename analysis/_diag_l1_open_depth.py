"""开盘那一下到底能吃下多少：把 r* 从恒等式变成一条能撑多大规模的线。

为什么需要它
------------
`_diag_l1_market_neutral_result.json` 把决策变量从 IR 换成了一个配比式
`净 bp = vwap + gap·r − cost`，r 是「仓位在开盘价成交的比例」，令净 = 0 得
`r* = (cost − vwap)/gap`。它顺手写了一句「只要开盘吃掉 11.8% 就打平」。

这句话里有一个**没测过的前提**：r 是可以选的。实际上 r 被两件事夹出来——
开盘价上最多能换手多少（首根 bar 的成交额），和我们要买多少。本脚本只测前者
能撑起多大的后者，不产生新的策略结论。

两条路线，先测再迁移
--------------------
- **实测**：`scripts/fetch_l1_open_depth.py` 用 tdx 的 `get_minute` 补 cohort 自己那
  2,752 个 (date, symbol) 的 09:31 bar，缓存到 `data/research/l1_open_depth/part.parquet`。
- **迁移**（备用/对拍）：`data/kline_minute` 那 38–40 只（全 000xxx.SZ）与 cohort 的
  交集为 0，只能按 |gap| 把份额迁过去；留一日误差一并落盘。

口径
----
- 「bar0」= 时间戳 09:31:00 的那根分钟 bar。它**含 09:25 集合竞价**（与
  `_diag_l1_vwap_curvature.py` 同一约定），因此 竞价成交额 ≤ bar0 成交额；
  所有以 bar0 为分母的参与率都是**下界**。
- 份额按**成交额**：`b0_amt / kline_daily_enriched.amount`，两者都是原始人民币金额，
  不受复权影响。成交量口径只用来与 `_diag_l1_open_probe_result.json` 对账。
- 参照类只用全日分区（240 根、首根 09:31）；2026-10-08 与 2026-11-05 是残段，弃用。
- gap = open/prev_close − 1（复权、日历相邻）。

预注册
------
    R-D1 覆盖。 若 cohort 与参照类交集为 0，则参照类份额不可直接用于 cohort，
          必须迁移并报留一日误差；实测覆盖 < 80% 时，主线仍判「不可判」。
    R-D2 迁移方向。 若参照类 share 在 gap ≤ −1% 区间随缺口加深而上升，
          则用「全样本中位」迁移是保守的；若实测高于迁移，方向得到验证。
    R-D3 判据。 π* = 建仓额 × r* / bar0成交额，即「打平所需的 bar0 参与率」。
          π* ≤ 10% ⇒ 深度这一格通过；10% < π* ≤ 30% ⇒ 不可判；π* > 30% ⇒ 关闭。
          阈值理由：10% 的 bar0 约等于 0.3–0.7% 日成交额，是正常参与率；
          30% 已经接近「一个人就是那根 bar」。
    R-D4 单位守卫。 r* 是小数、gap 是 bp。把 gap 当小数会让 r* 落到 1e-2 量级的
          倒数上，不报错、只让结论反向。断言 0.01 < r* < 0.5。

只读分区，不联网（补数在 fetch 脚本里，本脚本只读缓存）。
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
from _diag_l1_vwap_curvature import build_cohort  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent
OUT = HERE / "_diag_l1_open_depth_result.json"
MN = HERE / "_diag_l1_market_neutral_result.json"
PROBE = HERE / "_diag_l1_open_probe_result.json"
DIRECT = DATA / "research" / "l1_open_depth" / "part.parquet"

FULL_BARS = 200
OPEN_TM = "09:31:00"
REF_P10, REF_P90 = 0.0154, 0.0749
PI_GRID = (0.02, 0.05, 0.10, 0.20, 0.50)
BOOK_GRID_YI = (1.0, 2.0, 5.0, 10.0)
MIN_COVER = 0.80


# ------------------------------------------------------------------ 数据
def load_reference() -> tuple[pl.DataFrame, list[dict]]:
    frames: list[pl.DataFrame] = []
    levels: list[dict] = []
    for mf in sorted(glob.glob(str(DATA / "kline_minute" / "date=*" / "*.parquet"))):
        date = mf.split("date=")[1][:10]
        m = pl.read_parquet(
            mf, columns=["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
        ).with_columns(pl.col("datetime").dt.time().alias("tm"))
        t0 = m["tm"].min()
        nb = int(m.group_by("symbol").len()["len"].min())
        used = str(t0) == OPEN_TM and nb >= FULL_BARS
        levels.append({"date": date, "used": used, "first_bar": str(t0), "min_bars": nb})
        if not used:
            continue
        b0 = m.filter(pl.col("tm") == t0).select(
            "symbol",
            pl.col("open").alias("b0_open"),
            pl.col("high").alias("b0_high"),
            pl.col("low").alias("b0_low"),
            pl.col("close").alias("b0_close"),
            pl.col("amount").alias("b0_amt"),
            pl.col("volume").alias("b0_vol"),
        )
        day = m.group_by("symbol").agg(
            pl.col("amount").sum().alias("day_amt"),
            pl.col("volume").sum().alias("day_vol"),
            pl.len().alias("nbars"),
        )
        frames.append(b0.join(day, on="symbol").with_columns(pl.lit(date).alias("date")))
    df = pl.concat(frames).with_columns(
        (pl.col("b0_amt") / pl.col("day_amt")).alias("share_amt"),
        (pl.col("b0_vol") / pl.col("day_vol")).alias("share_vol"),
        ((pl.col("b0_close") / pl.col("b0_open") - 1.0) * 1e4).alias("b0_ret_bp"),
        ((pl.col("b0_high") - pl.col("b0_low")) / pl.col("b0_open") * 1e4).alias("b0_range_bp"),
        (pl.col("b0_high") == pl.col("b0_low")).alias("b0_single_price"),
    )
    return df, levels


def load_daily() -> pl.DataFrame:
    files = sorted(glob.glob(str(DATA / "kline_daily_enriched" / "date=*" / "*.parquet")))
    d = pl.concat(
        [
            pl.read_parquet(f, columns=["symbol", "date", "open", "close", "amount", "turnover_rate"])
            for f in files
        ],
        how="vertical_relaxed",
    ).with_columns(pl.col("date").cast(pl.Utf8))
    d = d.sort(["symbol", "date"]).with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
    d = d.with_columns(((pl.col("open") / pl.col("prev_close") - 1.0) * 100.0).alias("gap_pct"))
    return d.select("date", "symbol", "amount", "turnover_rate", "gap_pct", "open", "close")


def load_direct() -> pl.DataFrame:
    if not DIRECT.exists():
        return pl.DataFrame()
    return pl.read_parquet(DIRECT)


def open_consistency(dd: pl.DataFrame) -> dict:
    """tdx 的 bar0.open 与 `kline_daily_enriched.open` 是不是同一个价。

    两条上游（tdx 分钟 / 日线分区）互不相干；若逐位相同，说明「日线 open 就是首根 bar
    的开盘价」，也就是 09:25 竞价的成交价，bar0 从竞价那一瞬开始。
    """
    empty = {"n": 0, "exact_share": 0.0, "med_bp": 0.0, "p95_abs_bp": 0.0}
    if dd.is_empty():
        return empty
    dates = set(dd["date"].unique().to_list())
    frames = []
    for f in sorted(glob.glob(str(DATA / "kline_daily_enriched" / "date=*" / "*.parquet"))):
        if f.split("date=")[1][:10] not in dates:
            continue
        try:
            frames.append(
                pl.read_parquet(f, columns=["symbol", "date", "open", "close", "raw_close"])
                .with_columns(pl.col("date").cast(pl.Utf8))
            )
        except Exception:  # noqa: BLE001  分区 schema 不齐就跳过，n 会报出来
            continue
    if not frames:
        return empty
    dy = pl.concat(frames, how="vertical_relaxed").with_columns(
        (pl.col("raw_close") / pl.col("close")).alias("adjf")
    )
    j = dd.join(dy.select("symbol", "date", "open", "adjf"), on=["symbol", "date"], how="inner")
    if j.height == 0:
        return empty
    r = j["b0_open"].to_numpy() / (j["open"].to_numpy() * j["adjf"].to_numpy())
    dev = (r - 1.0) * 1e4
    return {
        "n": int(r.size),
        "exact_share": round(float(np.mean(np.abs(r - 1.0) <= 1e-9)), 4),
        "med_bp": round(float(np.median(dev)), 3),
        "p95_abs_bp": round(pct(np.abs(dev), 95), 3),
    }


# ------------------------------------------------------------------ 工具
def pct(x: np.ndarray, q: float) -> float:
    return float(np.percentile(x, q)) if x.size else float("nan")


def describe(x: np.ndarray, digits: int = 4) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {"n": 0}
    return {
        "n": int(x.size),
        "med": round(float(np.median(x)), digits),
        "mean": round(float(x.mean()), digits),
        "p10": round(pct(x, 10), digits),
        "p25": round(pct(x, 25), digits),
        "p75": round(pct(x, 75), digits),
        "p90": round(pct(x, 90), digits),
    }


GAP_EDGES = [
    (-math.inf, -3.0, "le_m3"),
    (-3.0, -2.0, "m3_m2"),
    (-2.0, -1.0, "m2_m1"),
    (-1.0, 0.0, "m1_0"),
    (0.0, 1.0, "0_1"),
    (1.0, math.inf, "gt_1"),
]


def gap_bins(df: pl.DataFrame, share_col: str) -> dict[str, dict]:
    """互斥分桶看 share。返回 dict：`_artifact_guard._resolve` 只走 dict。"""
    rows: dict[str, dict] = {}
    for lo, hi, name in GAP_EDGES:
        s = df.filter((pl.col("gap_pct") > lo) & (pl.col("gap_pct") <= hi))
        if s.height == 0:
            continue
        rows[name] = {
            "n": int(s.height),
            "share_med": round(float(s[share_col].median()), 4),
            "share_p25": round(pct(s[share_col].to_numpy(), 25), 4),
            "share_p75": round(pct(s[share_col].to_numpy(), 75), 4),
            "day_amt_med_yi": round(float(s["amount"].median()) / 1e8, 3),
        }
    return rows


def ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def fit_share_model(ref: pl.DataFrame) -> dict:
    g = np.abs(ref["gap_pct"].to_numpy())
    la = np.log10(ref["day_amt"].to_numpy())
    y = ref["share_amt"].to_numpy()
    ok = np.isfinite(g) & np.isfinite(la) & np.isfinite(y)
    g, la, y, dates = g[ok], la[ok], y[ok], ref["date"].to_numpy()[ok]
    X = np.column_stack([np.ones_like(g), g, la])
    beta = ols(X, y)
    pred = X @ beta
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(((y - pred) ** 2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    errs = []
    for d in np.unique(dates):
        tr, te = dates != d, dates == d
        if tr.sum() < 50 or te.sum() == 0:
            continue
        errs.append(np.abs(y[te] - X[te] @ ols(X[tr], y[tr])))
    mae = float(np.concatenate(errs).mean()) if errs else float("nan")
    return {
        "beta": [round(float(v), 6) for v in beta],
        "r2": round(float(r2), 4),
        "lodo_mae_share": round(mae, 4),
        "n": int(y.size),
    }


def predict_share(model: dict, gap_pct: np.ndarray, day_amt: np.ndarray) -> np.ndarray:
    b = model["beta"]
    x = b[0] + b[1] * np.abs(gap_pct) + b[2] * np.log10(day_amt)
    return np.clip(x, REF_P10, REF_P90)


def bucket_transfer(ref: pl.DataFrame, gap_pct: np.ndarray) -> np.ndarray:
    """非参迁移：按 |gap| 的四个嵌套档取参照类中位，深档外延不外推。"""
    ag = np.abs(ref["gap_pct"].to_numpy())
    sh = ref["share_amt"].to_numpy()
    ok = np.isfinite(ag) & np.isfinite(sh)
    ag, sh = ag[ok], sh[ok]
    cuts = [1.0, 2.0, 3.0]
    meds = []
    lo = 0.0
    for c in cuts:
        sel = (ag > lo) & (ag <= c)
        meds.append(float(np.median(sh[sel])) if sel.sum() >= 15 else float(np.median(sh)))
        lo = c
    meds.append(float(np.median(sh[ag > cuts[-1]])) if (ag > cuts[-1]).sum() >= 15
                else meds[-1])
    out = np.full(gap_pct.size, float(np.median(sh)))
    prev = 0.0
    for c, m in zip(cuts, meds[:3]):
        out[(np.abs(gap_pct) > prev) & (np.abs(gap_pct) <= c)] = m
        prev = c
    out[np.abs(gap_pct) > cuts[-1]] = meds[3]
    return np.clip(out, REF_P10, REF_P90)


def book_budget(pool: np.ndarray, cells_per_day: float, r_star: float) -> dict:
    """给定每 cell 的 bar0 成交额，反解「打平所需参与率」与「该参与率能撑的日建仓额」。"""
    pool = pool[np.isfinite(pool) & (pool > 0)]
    med, mean, p25 = pct(pool, 50), float(pool.mean()), pct(pool, 25)
    pi_req = {}
    for book in BOOK_GRID_YI:
        b = book * 1e8
        pi_req[f"{book:g}yi"] = {
            "med": round(b * r_star / (cells_per_day * med), 4),
            "mean": round(b * r_star / (cells_per_day * mean), 4),
            "p25": round(b * r_star / (cells_per_day * p25), 4),
        }
    cap = {}
    for pi in PI_GRID:
        cap[f"p{int(round(pi * 100)):02d}"] = {
            "med_yi": round(cells_per_day * pi / r_star * med / 1e8, 3),
            "mean_yi": round(cells_per_day * pi / r_star * mean / 1e8, 3),
            "p25_yi": round(cells_per_day * pi / r_star * p25 / 1e8, 3),
        }
    return {
        "cells": int(pool.size),
        "pool_med_yi": round(med / 1e8, 4),
        "pool_mean_yi": round(mean / 1e8, 4),
        "pool_p25_yi": round(p25 / 1e8, 4),
        "pi_req": pi_req,
        "capacity": cap,
        "pi_at_5e8_med": round(pi_req["5yi"]["med"], 4),
        "cap_at_pi10_med_yi": round(cap["p10"]["med_yi"], 3),
    }


# ------------------------------------------------------------------ 主流程
def main() -> None:  # noqa: C901
    t0 = time.time()
    ref_raw, levels = load_reference()
    daily = load_daily()
    ref = ref_raw.join(
        daily.select("date", "symbol", "amount", "gap_pct"), on=["date", "symbol"], how="inner"
    ).filter(pl.col("gap_pct").is_finite())
    need = build_cohort()
    cohort = need.join(
        daily.select("date", "symbol", "amount", "turnover_rate", "gap_pct"),
        on=["date", "symbol"], how="inner",
    )
    if cohort.height != 2752 or cohort["date"].n_unique() != 316:
        raise SystemExit(f"cohort 口径漂移: {cohort.height} / {cohort['date'].n_unique()}")
    cells_per_day = cohort.height / cohort["date"].n_unique()

    # ---- 实测路线
    direct = load_direct()
    direct_cov = 0.0
    if direct.height:
        dd = direct.join(
            cohort.select("date", "symbol", "amount", "gap_pct"), on=["date", "symbol"], how="inner"
        )
        dd = dd.filter((pl.col("nbars") >= FULL_BARS) & (pl.col("b0_amt") > 0) & (pl.col("amount") > 0))
        dd = dd.with_columns(
            (pl.col("b0_amt") / pl.col("amount")).alias("share_amt"),
            ((pl.col("b0_close") / pl.col("b0_open") - 1.0) * 1e4).alias("b0_ret_bp"),
            ((pl.col("b0_high") - pl.col("b0_low")) / pl.col("b0_open") * 1e4).alias("b0_range_bp"),
            (pl.col("b0_high") == pl.col("b0_low")).alias("b0_single_price"),
        ).filter(pl.col("gap_pct").is_finite())
        direct_cov = dd.height / cohort.height if cohort.height else 0.0
    else:
        dd = pl.DataFrame()

    # ---- 覆盖
    coh_keys = set(zip(cohort["date"].to_list(), cohort["symbol"].to_list()))
    ref_keys = set(zip(ref["date"].to_list(), ref["symbol"].to_list()))
    coverage = {
        "partitions": len(levels),
        "partitions_used": sum(1 for r in levels if r["used"]),
        "partitions_skipped": sum(1 for r in levels if not r["used"]),
        "reference_cells": int(ref.height),
        "reference_symbols": int(ref["symbol"].n_unique()),
        "cohort_cells": int(cohort.height),
        "cohort_days": int(cohort["date"].n_unique()),
        "cohort_cells_per_day": round(cells_per_day, 3),
        "cohort_minute_overlap": len(coh_keys & ref_keys),
        "direct_cells": int(dd.height),
        "direct_days": int(dd["date"].n_unique()) if dd.height else 0,
        "direct_coverage": round(direct_cov, 4),        "direct_covers_enough": bool(direct_cov >= MIN_COVER),
        "cohort_day_amt_med_yi": round(float(cohort["amount"].median()) / 1e8, 3),
        "cohort_day_amt_mean_yi": round(float(cohort["amount"].mean()) / 1e8, 3),
        "reference_day_amt_med_yi": round(float(ref["day_amt"].median()) / 1e8, 3),
        "cohort_gap_pct_med": round(float(cohort["gap_pct"].median()), 3),
    }

    # ---- 参照类分布（对拍 + 迁移）
    probe = json.loads(PROBE.read_text(encoding="utf-8"))
    probe_vol = float(probe["pooled"]["median_vol_share"])
    share_amt = describe(ref["share_amt"].to_numpy())
    share_vol = describe(ref["share_vol"].to_numpy())
    ref_bins = gap_bins(ref, "share_amt")
    model = fit_share_model(ref)
    m1 = predict_share(model, cohort["gap_pct"].to_numpy(), cohort["amount"].to_numpy())
    m2 = bucket_transfer(ref, cohort["gap_pct"].to_numpy())
    s_hat = np.minimum(m1, m2)
    pool_transfer = s_hat * cohort["amount"].to_numpy()

    # ---- r*：唯一产地是 market_neutral，这里只复算恒等式
    mn = json.loads(MN.read_text(encoding="utf-8"))
    vwap_bp = float(mn["entry_axis"]["vwap"]["mean_bp"])
    gap_bp = float(mn["entry_axis"]["return_gap_bp"])
    open_bp = float(mn["unhedged"]["mean_bp"])
    cost_bp = float(mn["cost"]["equity_round_bp"])
    r_star = (cost_bp - vwap_bp) / gap_bp
    if not (0.01 < r_star < 0.5):
        raise SystemExit(f"r* 单位可疑: {r_star}（gap 是否被当成小数？）")

    b_direct = book_budget(dd["b0_amt"].to_numpy(), cells_per_day, r_star) if dd.height else {}
    b_transfer = book_budget(pool_transfer, cells_per_day, r_star)

    # 主线：实测够覆盖就用实测，否则迁移（并明写判据落在误差里）
    primary = b_direct if coverage["direct_covers_enough"] else b_transfer
    route = "direct" if coverage["direct_covers_enough"] else "transfer"
    pi5 = primary["pi_at_5e8_med"]
    pi5_110 = round(5.0 * 1e8 * r_star / (110 * primary["pool_med_yi"] * 1e8), 4)
    adv_pct = round(5.0e8 / cells_per_day / (coverage["cohort_day_amt_med_yi"] * 1e8) * 100, 2)

    dd_share = describe(dd["share_amt"].to_numpy(), 4) if dd.height else {
        "n": 0, "med": 0.0, "mean": 0.0, "p10": 0.0, "p25": 0.0, "p75": 0.0, "p90": 0.0
    }
    dd_bins = gap_bins(dd, "share_amt") if dd.height else {}
    for _k in ("le_m3", "m3_m2", "m2_m1"):
        dd_bins.setdefault(_k, {"n": 0, "share_med": 0.0, "share_p25": 0.0,
                                "share_p75": 0.0, "day_amt_med_yi": 0.0})
    dd_ret = describe(dd["b0_ret_bp"].to_numpy(), 2) if dd.height else {"n": 0, "med": 0.0}
    path = {
        "route": route,
        "b0_ret_bp": {k: v for k, v in dd_ret.items() if k != "n"} if dd.height else
        {"med": 0.0, "mean": 0.0, "p10": 0.0, "p25": 0.0, "p75": 0.0, "p90": 0.0},
        "b0_range_bp_med": round(float(dd["b0_range_bp"].median()), 2) if dd.height else 0.0,
        "b0_single_price_share": round(float(dd["b0_single_price"].mean()), 4) if dd.height else 0.0,
    }
    vs_transfer_pp = round((dd_share["med"] - float(np.median(s_hat))) * 100, 2) if dd.height else 0.0

    verdict = {
        "R-D1_overlap_zero": bool(coverage["cohort_minute_overlap"] == 0),
        "R-D1_reading": (
            "参照类与 cohort 交集 0 -> 份额只能实测或迁移"
            if coverage["cohort_minute_overlap"] == 0
            else f"交集 {coverage['cohort_minute_overlap']} -> 可直接测"
        ),
        "R-D2_direct_vs_transfer_pp": vs_transfer_pp,
        "R-D2_reading": (
            "实测量到的份额高于迁移值 -> 用全样本中位迁移是保守的"
            if vs_transfer_pp > 0 else
            "实测量到的份额低于迁移值 -> 迁移偏乐观，主线必须用实测"
        ),
        "R-D3_route": route,
        "R-D3_pi_star_at_5e8_med": round(pi5, 4),
        "R-D3_verdict": (
            "pass" if pi5 <= 0.10 else ("undecidable" if pi5 <= 0.30 else "close")
        ),
        "R-D3_verdict_zh": (
            "通过" if pi5 <= 0.10 else ("不可判" if pi5 <= 0.30 else "关闭")
        ),
        "R-D4_unit_ok": bool(0.01 < r_star < 0.5),
        "binding_constraint": "impact" if pi5 <= 0.10 else "depth",
    }

    out: dict = {
        "caliber": (
            "bar0 = 09:31:00 那一根（含 09:25 竞价）；份额按成交额；"
            "gap = open/prev_close−1 复权；cohort 复用 build_cohort()；"
            "实测来自 tdx get_minute 缓存，迁移来自 19 个全日分区"
        ),
        "coverage": coverage,
        "levels": levels,
        "reference": {
            "share_amt": share_amt,
            "share_vol": share_vol,
            "open_probe_vol_med": probe_vol,
            "reconcile_with_open_probe_bp": round(abs(share_vol["med"] - probe_vol) * 1e4, 3),
            "by_gap_bin": ref_bins,
        },
        "direct": {
            "share_amt": dd_share,
            "by_gap_bin": dd_bins,
            "day_amt_med_yi": round(float(dd["amount"].median()) / 1e8, 3) if dd.height else 0.0,
            "open_vs_daily": open_consistency(dd),
        },
        "transfer": {
            "model": model,
            "m1_med": round(float(np.median(m1)), 4),
            "m2_med": round(float(np.median(m2)), 4),
            "used_med": round(float(np.median(s_hat)), 4),
            "clip": [REF_P10, REF_P90],
            "vs_direct_pp": vs_transfer_pp,
        },
        "budget": {
            "r_star": round(r_star, 4),
            "open_bp": round(open_bp, 2),
            "vwap_bp": round(vwap_bp, 2),
            "gap_bp": round(gap_bp, 2),
            "cost_bp": round(cost_bp, 2),
            "impact_budget_bp": round(open_bp - cost_bp, 2),
            "book_yi": 5.0,
            "names_per_day": 110,
            "pi_at_5e8_med_110names": pi5_110,
            "adv_pct_at_5e8": adv_pct,
            "direct": b_direct,
            "transfer": b_transfer,
            "primary_route": route,
        },
        "path": path,
        "verdict": verdict,
    }
    b = out["budget"]
    pf = f"budget.{route}"
    out["reading_template"] = (
        "cohort {ccells} 只-日 / {cdays} 日、日均 {cpd:.2f} 只；参照类（{pused}/{ptot} 个全日分区、"
        "{rsyms} 只）与它的 (date,symbol) 交集是 {ov}，所以份额不能从参照类直接借。"
        "实测补到 {dcells} 只-日（{dcov:.0%}、{ddays} 日）：bar0 成交额份额中位 {dmed:.2%}"
        "（p25 {dp25:.2%}、p75 {dp75:.2%}），中位日成交额 {damt:.2f}亿，bar0 成交额中位 "
        "{pmed:.3f}亿（均值 {pmean:.3f}亿、p25 {pp25:.3f}亿）。cohort 自己的三个缺口档："
        "{b3:.2%}（gap≤−3%，n {n3}）/ {b32:.2%}（{n32}）/ {b21:.2%}（{n21}）—— 缺口越深，"
        "首根 bar 占当日成交额的份额越高。迁移那条路（留一日误差 {lmae:.2%}）给的是 "
        "{tmed:.2%}，实测减迁移 {diff:+.2f}pp ⇒ {rd2}。"
        "r* = (cost {cost:.2f}bp − vwap {vwap:.2f}bp)/gap {gap:.2f}bp = **{rstar:.2%}**"
        "（恒等式，唯一产地在 market_neutral）。反解：{book:g}亿日建仓额、日均 {cpd:.2f} 只，"
        "需要吃掉 bar0 的 **{pi5:.2%}**（p25 流动性下 {pi5p25:.2%}；单票相当于日成交额的 "
        "{adv:.2f}%），判 **{verd}**；同口径下 10% 的参与率能撑 {c10:.2f}亿/日"
        "（中位流动性）、{c10m:.2f}亿（均值）。注意分母：bar0 **含** 09:25 竞价，所以真正需要的"
        "「竞价参与率」≥ {pi5:.2%}——要再除上竞价占首根 bar 的份额 θ，而 θ 本库测不出"
        "（没有 09:25 快照）；θ 越小这一格越紧，θ=0.4 时就是它的 2.5 倍。"
        "把同样 5 亿摊薄到 110 只/日（放弃 gap 条件，"
        "只作为分母敏感性）打平只需 {pi110:.2%}。"
        "首根 bar 内部：开盘→收盘中位 {ret:.1f}bp、振幅 {rng:.0f}bp、单一定价 {sing:.1%} —— "
        "开盘价只在一瞬成立，那一瞬就是竞价（bar0 的 open 与日线 open 在 {oexact:.0%} 的 "
        "cell 上逐位相同，两条上游互不相干）。自带冲击的预算 {ibud:.1f}bp 本库测不出来"
        "（无委托队列），所以这一格能给的最强判据是「打平需要多少参与率」，不是「净收益是多少」。"
    )
    out["reading_fields"] = {
        "ccells": "coverage.cohort_cells", "cdays": "coverage.cohort_days",
        "cpd": "coverage.cohort_cells_per_day",
        "pused": "coverage.partitions_used", "ptot": "coverage.partitions",
        "rsyms": "coverage.reference_symbols",
        "ov": "coverage.cohort_minute_overlap",
        "dcells": "coverage.direct_cells", "ddays": "coverage.direct_days",
        "dcov": "coverage.direct_coverage",
        "dmed": "direct.share_amt.med", "dp25": "direct.share_amt.p25",
        "dp75": "direct.share_amt.p75",
        "b3": "direct.by_gap_bin.le_m3.share_med",
        "n3": "direct.by_gap_bin.le_m3.n",
        "b32": "direct.by_gap_bin.m3_m2.share_med",
        "n32": "direct.by_gap_bin.m3_m2.n",
        "b21": "direct.by_gap_bin.m2_m1.share_med",
        "n21": "direct.by_gap_bin.m2_m1.n",
        "damt": "direct.day_amt_med_yi",
        "pmed": f"{pf}.pool_med_yi", "pmean": f"{pf}.pool_mean_yi",
        "pp25": f"{pf}.pool_p25_yi",
        "lmae": "transfer.model.lodo_mae_share", "tmed": "transfer.used_med",
        "diff": "transfer.vs_direct_pp", "rd2": "verdict.R-D2_reading",
        "cost": "budget.cost_bp", "vwap": "budget.vwap_bp", "gap": "budget.gap_bp",
        "rstar": "budget.r_star", "book": "budget.book_yi",
        "pi5": f"{pf}.pi_at_5e8_med", "pi5p25": f"{pf}.pi_req.5yi.p25",
        "pi110": "budget.pi_at_5e8_med_110names",
        "adv": "budget.adv_pct_at_5e8",
        "verd": "verdict.R-D3_verdict_zh",
        "c10": f"{pf}.cap_at_pi10_med_yi", "c10m": f"{pf}.capacity.p10.mean_yi",
        "ret": "path.b0_ret_bp.med", "rng": "path.b0_range_bp_med",
        "sing": "path.b0_single_price_share",
        "oexact": "direct.open_vs_daily.exact_share",
        "ibud": "budget.impact_budget_bp",
    }
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["reading"] = render_reading(out, root=out)
    assert_clean(out, label=OUT.name)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"coverage: ref {coverage['reference_cells']} cells / {coverage['partitions_used']}d, "
          f"cohort {coverage['cohort_cells']} cells, overlap {coverage['cohort_minute_overlap']}, "
          f"direct {coverage['direct_cells']} cells ({direct_cov*100:.1f}%)")
    print(f"ref share amt med {share_amt['med']} | vol {share_vol['med']} vs probe {probe_vol}")
    if dd.height:
        print(f"direct share med {dd_share['med']} p25 {dd_share['p25']} p75 {dd_share['p75']}")
        print(f"direct bins: {[(k, v['share_med'], v['n']) for k, v in dd_bins.items()]}")
    print(f"transfer m1 {out['transfer']['m1_med']} m2 {out['transfer']['m2_med']} "
          f"used {out['transfer']['used_med']} | LODO mae {model['lodo_mae_share']}")
    print(f"pool {route}: med {primary['pool_med_yi']}yi mean {primary['pool_mean_yi']}yi "
          f"p25 {primary['pool_p25_yi']}yi")
    print(f"r* {r_star:.4f} | pi@5yi med {pi5:.4f} p25 {primary['pi_req']['5yi']['p25']:.4f} "
          f"-> {verdict['R-D3_verdict']}")
    print(f"cap@pi10 med {primary['cap_at_pi10_med_yi']}yi "
          f"mean {primary['capacity']['p10']['mean_yi']}yi")
    print(f"path: ret {path['b0_ret_bp'].get('med')}bp range {path['b0_range_bp_med']}bp "
          f"single {path['b0_single_price_share']}")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


if __name__ == "__main__":
    main()
