"""触点组合的回撤归因与条件分布。

回答三个问题，不给点预测：
  1. 2026-11-13 触点 15 只在 11-13→11-20 跌 22.9%，其中多少是市场 β、市值风格、
     当天追高、触点选择？样本只有 1 期，结论只能是「这一期发生了什么」。
  2. 2026-11-20 触点未来 5/10/20 日会不会继续单边下跌？给同类市场状态下的
     历史条件分布（中位/左尾/胜率），而不是用 11-13 的 15 只外推。
  3. 触点 α 是否逐年衰减（半衰期）？样本量是否足以判定。

口径：
  - 触点 = citizenship.cold_ok 按 heat 降序 top15（与页面同源，parity 已绑）。
  - 收益 = T+1 开盘买入 → T+H 收盘；超额 = 减当日全市场等权/中位。
  - 市场状态 = 全市场日收益等权复利后的 20/60 日累计。
  - t 按日块 bootstrap，块长 2H。
  - 11-13/11-20 名单动态取自 citizenship.compute，不硬编码。

运行：backend/.venv/Scripts/python.exe analysis/_diag_touchpoint_drawdown.py
输出：data/.scratch/touchpoint_drawdown.json
"""
from __future__ import annotations

import json
import sys
from datetime import date as date_cls, timedelta
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
sys.path.insert(0, str(ROOT / "backend"))

from app.services import citizenship as cit  # noqa: E402
from _diag_first_fission import cold_ok_expr, load_panel  # noqa: E402
from _verify_citizenship_signal import block_se  # noqa: E402

TABLE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"
CASE_START = date_cls(2026, 11, 13)
CASE_END = date_cls(2026, 11, 20)
CURRENT = date_cls(2026, 11, 20)
HORIZONS = (5, 10, 20)


def load_bars(d0: date_cls, d1: date_cls) -> pl.DataFrame:
    files = sorted(
        str(p) for p in TABLE.glob("date=*/part.parquet")
        if d0.isoformat() <= p.parent.name[5:] <= d1.isoformat()
    )
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "close", "amount", "turnover_rate"])
        .collect()
        .with_columns(pl.col("date").cast(pl.Utf8))
    )


_snapshot_cache: dict[str, tuple[pl.DataFrame, pl.DataFrame]] = {}


def service_snapshot(as_of: date_cls) -> tuple[pl.DataFrame, pl.DataFrame]:
    """名单与 cold_ok 全池都取自生产服务，不硬编码、不另写一套过滤。"""
    key = as_of.isoformat()
    if key not in _snapshot_cache:
        r = cit.compute(ROOT / "data", as_of)
        picks = pl.DataFrame(r["focus"]["picks"]).select(
            ["symbol", "name", "heat", "ret20", "turn_med", "amount", "mcap"]
        )
        pool = pl.from_dicts(r["rows"], infer_schema_length=None).filter(cold_ok_expr())
        _snapshot_cache[key] = (picks, pool)
    return _snapshot_cache[key]


def case_path(picks: pl.DataFrame) -> dict:
    picks_syms = picks["symbol"].to_list()
    bars = load_bars(CASE_START - timedelta(days=7), CASE_END)
    base = bars.filter(pl.col("date") == CASE_START.isoformat()).select([
        "symbol", pl.col("close").alias("c0"), pl.col("open").alias("o0")
    ])
    prev = bars.filter(pl.col("date") == "2026-11-12").select([
        "symbol", pl.col("close").alias("c_prev")
    ])
    path = (
        bars.filter(pl.col("symbol").is_in(picks_syms))
        .join(base, on="symbol")
        .filter(pl.col("date") >= CASE_START.isoformat())
        .with_columns(((pl.col("close") / pl.col("c0") - 1) * 100).alias("cum"))
        .sort(["symbol", "date"])
    )
    days = sorted(path["date"].unique().to_list())
    detail = picks.join(
        path.group_by("symbol").agg([
            pl.col("cum").filter(pl.col("date") == d).first().alias(d[5:]) for d in days
        ]),
        on="symbol", how="left",
    ).select(["symbol", "name", "heat", "ret20"] + [d[5:] for d in days])

    equal = {d[5:]: float(path.filter(pl.col("date") == d)["cum"].mean()) for d in days}
    # 次日开盘买入（11-14 是周六，最早 11-16 开盘）
    entry = bars.filter(pl.col("date") == "2026-11-16").select([
        "symbol", pl.col("open").alias("o_entry")
    ])
    end = bars.filter(pl.col("date") == CASE_END.isoformat()).select([
        "symbol", pl.col("close").alias("c_end")
    ])
    ex = (
        picks.select(["symbol", "name", "ret20", "mcap"])
        .join(entry, on="symbol").join(end, on="symbol")
        .with_columns((pl.col("c_end") / pl.col("o_entry") - 1).alias("nx"))
        .join(
            path.filter(pl.col("date") == CASE_END.isoformat()).select([
                "symbol", (pl.col("cum") / 100).alias("bh")
            ]),
            on="symbol", how="left",
        )
    )
    # ret20 分组：验证「20 日已深跌的反弹票」是否拖累
    buckets = []
    for lo, hi, label in ((-9.99, -0.10, "ret20<-10%"), (-0.10, 0.0, "-10%~0"), (0.0, 9.99, "ret20>0")):
        s = ex.filter((pl.col("ret20") > lo) & (pl.col("ret20") <= hi))
        if s.height:
            buckets.append({
                "bucket": label, "n": s.height,
                "bh_mean_pct": float(s["bh"].mean() * 100),
                "bh_median_pct": float(s["bh"].median() * 100),
                "nx_mean_pct": float(s["nx"].mean() * 100),
            })
    # 当日涨幅（11-13 vs 11-12）
    day_ret = picks.join(
        base.join(prev, on="symbol").with_columns(
            ((pl.col("c0") / pl.col("c_prev") - 1) * 100).alias("r_day")
        ).select(["symbol", "r_day"]),
        on="symbol", how="left",
    )
    return {
        "picks": detail.to_dicts(),
        "equal_weight_cum_pct": equal,
        "equal_weight_bh_pct": equal.get("11-20"),
        "equal_weight_next_open_pct": float(ex["nx"].mean() * 100) if ex.height else None,
        "by_ret20_bucket": buckets,
        "day_return_pct": day_ret.select(["symbol", "name", "r_day"]).to_dicts(),
    }


def pool_path(pool: pl.DataFrame) -> dict:
    """cold_ok 全池 vs heat 五分位 vs top15：检验「排序取前 15」本身是否负向。"""
    bars = load_bars(CASE_START - timedelta(days=7), CASE_END)
    base = bars.filter(pl.col("date") == CASE_START.isoformat()).select([
        "symbol", pl.col("close").alias("c0")
    ])
    end = bars.filter(pl.col("date") == CASE_END.isoformat()).select([
        "symbol", pl.col("close").alias("c1")
    ])
    entry = bars.filter(pl.col("date") == "2026-11-16").select([
        "symbol", pl.col("open").alias("o1")
    ])
    x = (
        pool.select(["symbol", "heat"])
        .join(base, on="symbol").join(end, on="symbol").join(entry, on="symbol")
        .with_columns([
            (pl.col("c1") / pl.col("c0") - 1).alias("bh"),
            (pl.col("c1") / pl.col("o1") - 1).alias("nx"),
        ])
        .with_columns(
            (pl.col("heat").rank("ordinal") / pl.col("heat").count() * 5)
            .ceil().clip(1, 5).cast(pl.Int32).alias("hbin")
        )
    )
    lv = x.group_by("hbin").agg([
        pl.col("bh").mean().alias("bh_mean"),
        pl.col("bh").median().alias("bh_median"),
        pl.len().alias("n"),
    ]).sort("hbin")
    top15 = x.sort("heat", descending=True).head(15)
    return {
        "n_pool": x.height,
        "pool_bh_mean_pct": float(x["bh"].mean() * 100),
        "pool_bh_median_pct": float(x["bh"].median() * 100),
        "pool_next_open_mean_pct": float(x["nx"].mean() * 100),
        "by_heat_quintile": [{
            "bin": int(r["hbin"]), "n": int(r["n"]),
            "bh_mean_pct": float(r["bh_mean"] * 100),
            "bh_median_pct": float(r["bh_median"] * 100),
        } for r in lv.to_dicts()],
        "top15_bh_mean_pct": float(top15["bh"].mean() * 100),
        "top15_next_open_mean_pct": float(top15["nx"].mean() * 100),
    }


def heat_gradient(panel: pl.DataFrame) -> dict:
    """cold_ok 内按 heat 五分位，看「heat 越高收益越差」是历史规律还是单期噪声。"""
    c = panel.filter(cold_ok_expr()).with_columns([
        pl.col("heat").rank("ordinal").over("date").alias("_hr"),
        pl.col("heat").count().over("date").alias("_hn"),
    ]).with_columns(
        ((pl.col("_hr") / pl.col("_hn")) * 5).ceil().clip(1, 5).cast(pl.Int32).alias("_hb")
    )
    daily = c.group_by(["date", "_hb"]).agg([
        pl.col("fwd5").mean().alias("f5"),
        pl.col("fwd20").mean().alias("f20"),
        pl.len().alias("n"),
    ])
    wide = (
        daily.filter(pl.col("_hb") == 5)
        .select(["date", pl.col("f5").alias("h5_f5"), pl.col("f20").alias("h5_f20")])
        .join(
            daily.filter(pl.col("_hb") == 1)
            .select(["date", pl.col("f5").alias("h1_f5"), pl.col("f20").alias("h1_f20")]),
            on="date",
        )
        .with_columns([
            (pl.col("h5_f5") - pl.col("h1_f5")).alias("d5"),
            (pl.col("h5_f20") - pl.col("h1_f20")).alias("d20"),
        ])
        .drop_nulls(["d5", "d20"]).sort("date")
    )

    def stat(col: str, block: int) -> dict:
        a = wide[col].to_numpy().astype(float)
        se = block_se(a, block=block, seed=29)
        return {
            "mean_pct": float(a.mean() * 100),
            "t": float(a.mean() / se) if se > 0 else None,
            "n_days": int(a.size),
        }

    levels = daily.group_by("_hb").agg([
        pl.col("f5").mean().alias("f5_mean"),
        pl.col("f20").mean().alias("f20_mean"),
        pl.len().alias("rows"),
    ]).sort("_hb").with_columns([
        (pl.col("f5_mean") * 100).alias("f5_mean_pct"),
        (pl.col("f20_mean") * 100).alias("f20_mean_pct"),
    ]).drop(["f5_mean", "f20_mean"])
    yearly = [{
        "year": int(y), "n_days": s.height,
        "d5_mean_pct": float(s["d5"].mean() * 100),
        "d20_mean_pct": float(s["d20"].mean() * 100),
    } for y in sorted(wide["date"].dt.year().unique().to_list())
        for s in [wide.filter(pl.col("date").dt.year() == y)] if s.height >= 30]
    return {
        "by_level": levels.to_dicts(),
        "h5_minus_h1_fwd5": stat("d5", 10),
        "h5_minus_h1_fwd20": stat("d20", 40),
        "by_year": yearly,
        "mcap_double_sort": heat_gradient_by_mcap(panel),
    }


def heat_gradient_by_mcap(panel: pl.DataFrame) -> list[dict]:
    """市值三分组内再看 heat 高低：检验 heat 梯度是不是小市值代理。"""
    c = panel.filter(cold_ok_expr()).with_columns(
        (pl.col("p_scale") * 3).floor().clip(0, 2).cast(pl.Int32).alias("_mb")
    )
    out = []
    for b in range(3):
        s = c.filter(pl.col("_mb") == b).drop_nulls(["fwd20"])
        if s.height < 200:
            continue
        med = float(s["heat"].median())
        hi = s.filter(pl.col("heat") > med)
        lo = s.filter(pl.col("heat") <= med)
        out.append({
            "mcap_tercile": b,
            "n_hi": hi.height, "n_lo": lo.height,
            "hi_f20_mean_pct": float(hi["fwd20"].mean() * 100),
            "lo_f20_mean_pct": float(lo["fwd20"].mean() * 100),
            "diff_f20_pct": float((hi["fwd20"].mean() - lo["fwd20"].mean()) * 100),
            "hi_f5_mean_pct": float(hi["fwd5"].mean() * 100),
            "lo_f5_mean_pct": float(lo["fwd5"].mean() * 100),
        })
    return out


def market_benchmarks(d0: date_cls, d1: date_cls, start: date_cls) -> list[dict]:
    """同期基准：全市场、追高组、市值五格。"""
    bars = load_bars(d0 - timedelta(days=7), d1)
    def day(d, cols):
        return bars.filter(pl.col("date") == d.isoformat()).select(["symbol"] + cols)
    b = (
        day(start, [pl.col("close").alias("c0"), pl.col("amount").alias("a0"),
                    pl.col("turnover_rate").alias("t0")])
        .join(day(start - timedelta(days=1), [pl.col("close").alias("cp")]), on="symbol")
        .join(day(d1, [pl.col("close").alias("c1")]), on="symbol")
        .join(day(date_cls(2026, 11, 16), [pl.col("open").alias("o1")]), on="symbol")
        .filter((pl.col("cp") > 0) & (pl.col("c1") > 0) & (pl.col("o1") > 0)
                & (pl.col("t0") > 0))
        .with_columns([
            (pl.col("c0") / pl.col("cp") - 1).alias("r0"),
            (pl.col("c1") / pl.col("c0") - 1).alias("bh"),
            (pl.col("c1") / pl.col("o1") - 1).alias("nx"),
            (pl.col("a0") / (pl.col("t0") / 100)).alias("fmcap"),
        ])
    )
    q90, q95 = b["r0"].quantile(0.9), b["r0"].quantile(0.95)
    groups = {
        "market": b,
        "r0_top10pct": b.filter(pl.col("r0") >= q90),
        "r0_top5pct": b.filter(pl.col("r0") >= q95),
        "r0_gt9pct": b.filter(pl.col("r0") >= 0.09),
    }
    out = [{
        "group": k, "n": s.height,
        "bh_mean_pct": float(s["bh"].mean() * 100),
        "bh_median_pct": float(s["bh"].median() * 100),
        "next_open_mean_pct": float(s["nx"].mean() * 100),
        "next_open_median_pct": float(s["nx"].median() * 100),
    } for k, s in groups.items()]
    b2 = b.with_columns((pl.col("fmcap").rank("ordinal") / pl.col("fmcap").count()).alias("mq"))
    for i in range(5):
        s = b2.filter((pl.col("mq") > i / 5) & (pl.col("mq") <= (i + 1) / 5))
        out.append({
            "group": f"mcap_Q{i+1}", "n": s.height,
            "bh_mean_pct": float(s["bh"].mean() * 100),
            "bh_median_pct": float(s["bh"].median() * 100),
        })
    return out


def add_forward(panel: pl.DataFrame) -> pl.DataFrame:
    """补 fwd5/fwd10；fwd20/exc20 由 load_panel 给出。"""
    e5 = panel.select(["symbol", (pl.col("didx") - 6).alias("didx"), pl.col("close").alias("_e5")])
    e10 = panel.select(["symbol", (pl.col("didx") - 11).alias("didx"), pl.col("close").alias("_e10")])
    p = panel.join(e5, on=["symbol", "didx"], how="left").join(e10, on=["symbol", "didx"], how="left")
    p = p.with_columns([
        pl.when((pl.col("entry_open") > 0) & (pl.col("_e5") > 0))
        .then(pl.col("_e5") / pl.col("entry_open") - 1).otherwise(None).alias("fwd5"),
        pl.when((pl.col("entry_open") > 0) & (pl.col("_e10") > 0))
        .then(pl.col("_e10") / pl.col("entry_open") - 1).otherwise(None).alias("fwd10"),
    ])
    for h, col in (("5", "fwd5"), ("10", "fwd10")):
        med = p.group_by("date").agg(pl.col(col).median().alias("_m"))
        p = p.join(med, on="date", how="left").with_columns(
            (pl.col(col) - pl.col("_m")).alias(f"exc{h}")
        ).drop("_m")
    return p


def market_state(panel: pl.DataFrame) -> pl.DataFrame:
    m = panel.select(["symbol", "didx", "date", "close"]).sort(["symbol", "didx"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("r1")
    )
    daily = m.group_by("date").agg(pl.col("r1").mean().alias("mkt_r1")).sort("date")
    daily = daily.with_columns(pl.col("mkt_r1").fill_null(0.0).alias("mkt_r1"))
    daily = daily.with_columns((1 + pl.col("mkt_r1")).cum_prod().alias("nav"))
    return daily.with_columns([
        (pl.col("nav") / pl.col("nav").shift(20) - 1).alias("mkt_ret20"),
        (pl.col("nav") / pl.col("nav").shift(60) - 1).alias("mkt_ret60"),
    ])


def daily_touchpoints(panel: pl.DataFrame) -> pl.DataFrame:
    picks = (
        panel.filter(cold_ok_expr())
        .with_columns(pl.col("heat").rank("ordinal", descending=True).over("date").alias("_rk"))
        .filter(pl.col("_rk") <= cit.FOCUS_TOP_N)
    )
    return picks.group_by("date").agg([
        pl.col("fwd5").mean().alias("tp_fwd5"),
        pl.col("fwd10").mean().alias("tp_fwd10"),
        pl.col("fwd20").mean().alias("tp_fwd20"),
        pl.col("exc20").mean().alias("tp_exc20"),
        pl.len().alias("tp_n"),
    ]).sort("date")


def beta_daily(panel: pl.DataFrame) -> pl.DataFrame:
    return panel.group_by("date").agg([
        pl.col("fwd5").mean().alias("mkt_fwd5"),
        pl.col("fwd10").mean().alias("mkt_fwd10"),
        pl.col("fwd20").mean().alias("mkt_fwd20"),
    ]).sort("date")


def describe(a: pl.Series | np.ndarray) -> dict:
    x = np.asarray(a, dtype=float) if not isinstance(a, np.ndarray) else a.astype(float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {"n": 0}
    return {
        "n": int(x.size),
        "mean_pct": float(x.mean() * 100),
        "median_pct": float(np.median(x) * 100),
        "p10_pct": float(np.quantile(x, 0.10) * 100),
        "p25_pct": float(np.quantile(x, 0.25) * 100),
        "p75_pct": float(np.quantile(x, 0.75) * 100),
        "p90_pct": float(np.quantile(x, 0.90) * 100),
        "win_rate": float((x > 0).mean()),
    }


def conditional_distributions(d: pl.DataFrame) -> dict:
    q60 = float(d["mkt_ret60"].drop_nulls().quantile(0.10))
    q60_20 = float(d["mkt_ret60"].drop_nulls().quantile(0.20))
    q20 = float(d["mkt_ret20"].drop_nulls().quantile(0.10))
    conds = {
        "all": (pl.lit(True), None),
        "mkt_ret60_le_p10": (pl.col("mkt_ret60") <= q60, q60),
        "mkt_ret60_le_p20": (pl.col("mkt_ret60") <= q60_20, q60_20),
        "mkt_ret20_le_p10": (pl.col("mkt_ret20") <= q20, q20),
    }
    out = {}
    for name, (expr, cut) in conds.items():
        s = d.filter(expr)
        row = {"n_days": s.height, "cut": cut}
        for h in HORIZONS:
            row[f"tp_fwd{h}"] = describe(s[f"tp_fwd{h}"])
            row[f"mkt_fwd{h}"] = describe(s[f"mkt_fwd{h}"])
            row[f"excess_fwd{h}"] = describe(s[f"tp_fwd{h}"] - s[f"mkt_fwd{h}"])
        out[name] = row
    return out


def yearly_alpha(d: pl.DataFrame) -> list[dict]:
    out = []
    for y in sorted(d["date"].dt.year().unique().to_list()):
        s = d.filter(
            (pl.col("date").dt.year() == y) & pl.col("tp_exc20").is_not_null()
        )
        if s.height < 20:
            continue
        se = block_se(s["tp_exc20"].to_numpy().astype(float), block=40, seed=11)
        mean = float(s["tp_exc20"].mean())
        out.append({
            "year": int(y), "n_days": s.height,
            "tp_fwd20_mean_pct": float(s["tp_fwd20"].mean() * 100),
            "mkt_fwd20_mean_pct": float(s["mkt_fwd20"].mean() * 100),
            "excess20_mean_pct": float(mean * 100),
            "excess20_t": mean / se if se > 0 else None,
        })
    return out


def current_state(d: pl.DataFrame) -> dict:
    m = market_state_cache
    row = m.filter(pl.col("date") == CURRENT)
    if row.is_empty():
        return {}
    r20 = float(row["mkt_ret20"][0]) if row["mkt_ret20"][0] is not None else None
    r60 = float(row["mkt_ret60"][0]) if row["mkt_ret60"][0] is not None else None
    v20 = m["mkt_ret20"].drop_nulls()
    v60 = m["mkt_ret60"].drop_nulls()
    return {
        "date": str(CURRENT),
        "mkt_ret20_pct": r20 * 100 if r20 is not None else None,
        "mkt_ret60_pct": r60 * 100 if r60 is not None else None,
        "mkt_ret20_pctile": float((v20 <= r20).mean()) if r20 is not None else None,
        "mkt_ret60_pctile": float((v60 <= r60).mean()) if r60 is not None else None,
    }


market_state_cache: pl.DataFrame


def main() -> None:
    global market_state_cache
    picks_1113, pool_1113 = service_snapshot(CASE_START)
    picks_1120, _ = service_snapshot(CURRENT)
    print(f"11-13 picks {picks_1113.height}；cold_ok 池 {pool_1113.height}；11-20 picks {picks_1120.height}", flush=True)

    case = case_path(picks_1113)
    case["pool"] = pool_path(pool_1113)
    print(f"cold_ok 全池 BH {case['pool']['pool_bh_mean_pct']:.2f}%；"
          f"heat 五分位 {[(b['bin'], round(b['bh_mean_pct'],2)) for b in case['pool']['by_heat_quintile']]}", flush=True)
    bench = market_benchmarks(CASE_START - timedelta(days=1), CASE_END, CASE_START)
    print(f"11-13 等权 BH {case['equal_weight_bh_pct']:.2f}%；次日开盘 {case['equal_weight_next_open_pct']:.2f}%", flush=True)
    print("基准：" + "; ".join(f"{b['group']}={b['bh_mean_pct']:.2f}%" for b in bench), flush=True)
    print("ret20 分组：" + str(case["by_ret20_bucket"]), flush=True)

    panel, _ = load_panel()
    panel = add_forward(panel)
    market_state_cache = market_state(panel)
    d = (
        daily_touchpoints(panel)
        .join(beta_daily(panel), on="date")
        .join(market_state_cache.select(["date", "mkt_ret20", "mkt_ret60"]), on="date")
    )
    cond = conditional_distributions(d)
    yearly = yearly_alpha(d)
    grad = heat_gradient(panel)
    cur = current_state(d)
    print(f"当前状态：{cur}", flush=True)
    for k, v in cond.items():
        print(
            f"{k}: n={v['n_days']} tp5={v['tp_fwd5'].get('median_pct')}%"
            f" p10={v['tp_fwd5'].get('p10_pct')}% 超额={v['excess_fwd5'].get('median_pct')}%",
            flush=True,
        )
    print("逐年：" + str(yearly), flush=True)
    print(f"heat 梯度 H5-H1 fwd5 {grad['h5_minus_h1_fwd5']}；fwd20 {grad['h5_minus_h1_fwd20']}", flush=True)

    report = {
        "spec": {
            "case": f"{CASE_START}→{CASE_END}",
            "picks": "citizenship.cold_ok heat 降序 top15（服务动态取，不硬编码）",
            "returns": "T+1 开盘→T+H 收盘；超额减当日全市场中位；未扣成本",
            "market_state": "全市场日收益等权复利后的 20/60 日累计",
            "inference": "按日块 bootstrap，块长 2H",
        },
        "case_20261113": {
            "picks": picks_1113.to_dicts(),
            "path": case,
            "benchmarks": bench,
        },
        "current_20261120": {
            "picks": picks_1120.to_dicts(),
            "market_state": cur,
        },
        "conditional_by_market_state": cond,
        "heat_gradient": grad,
        "yearly_alpha": yearly,
        "limitations": [
            "11-13→11-20 是单期 15 只，不能外推为下一期概率；本实验只做归因。",
            "kline_daily_enriched 是当前在册名单回溯面板，存在生存偏差。",
            "收益未扣成本、未剔除 T+1 一字板不可成交；触点容量未做冲击成本。",
            "逐年 α 的 n_days 是日数不是独立样本；同一票多日重复持有会高估显著性。",
            "「被量化针对」没有直接可观测变量，本实验只能检验「触点超额是否显著为负」。",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "touchpoint_drawdown.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=float), encoding="utf-8"
    )
    print(f"写出 {OUT / 'touchpoint_drawdown.json'}", flush=True)


if __name__ == "__main__":
    main()
