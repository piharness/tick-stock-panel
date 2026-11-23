"""cold_ok 内部的「首次分歧萌芽」对照实验。

命题（事前固定，不调参）：
  T 日换手率或日振幅进入全市场前 20%，且过去 60 个市场交易日没有同类记录，
  T 日仍未涨停。只比较页面同一套 cold_ok 池内的股票。

结果：
  1. T+1..T+5 是否出现纯涨停（不是“涨停且龙虎榜游资净买>0”）。
  2. T+1 开盘买入、T+21 收盘退出的 20 交易日收益；超额 = 减当日全市场中位数。

统计纪律：
  - 所有滚动窗口按 enriched 分区给出的市场交易日索引，不按个股行数。
  - 收益用日历索引 self-join，禁止跨停牌拼接出假收益。
  - t 按日聚合后做块 bootstrap，块长为 2*horizon。
  - 市值控制用日期 × 全市场市值分位五格内配对，不把 pooled t 当独立样本。
  - 最新交易日与 citizenship.compute() 对账；不一致就 fail closed。

运行：backend/.venv/Scripts/python.exe analysis/_diag_first_fission.py
自检：backend/.venv/Scripts/python.exe analysis/_diag_first_fission.py --self-test
输出：data/.scratch/first_fission.json、first_fission_cold_samples.parquet
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
sys.path.insert(0, str(ROOT / "backend"))

from app.services import citizenship as cit  # noqa: E402
from _verify_citizenship_signal import block_se  # noqa: E402

TABLE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"
READ_START = date(2019, 1, 1)
SAMPLE_START = date(2020, 1, 2)
FISSION_QUANTILE = 0.80
FISSION_LOOKBACK = 60
START_HORIZON = 5
RETURN_HORIZON = 20
MCAP_BINS = 5
MIN_SIGNAL_N = 100
MIN_SIGNAL_DAYS = 30


def cold_ok_expr() -> pl.Expr:
    """镜像 _focus 的 cold_ok；由最新交易日的真实服务对账绑定到唯一定义点。"""
    mature = pl.col("listing_days").is_null() | (
        pl.col("listing_days") >= cit.FOCUS_MIN_LISTING_DAYS
    )
    not_falling = pl.col("ret60").is_null() | (
        pl.col("ret60") >= cit.FOCUS_MIN_RET60
    )
    return (
        (pl.col("heat") >= cit.HEAT_CUT)
        & (pl.col("stage") == "cold")
        & ~cit._is_st_name()
        & ~cit._is_bse()
        & (pl.col("amount") >= cit.FOCUS_MIN_AMOUNT)
        & mature
        & not_falling
        & pl.col("ret20").is_not_null()
    )


def mark_first_fission(df: pl.DataFrame) -> pl.DataFrame:
    """过去 60 个市场交易日无同类事件；61i 含今天，减去今天后恰好是前 60 日。"""
    out = df.sort(["symbol", "didx"]).with_columns([
        pl.col("fission_today").cast(pl.Int32).alias("_fission_int"),
        pl.col("didx").min().over("symbol").alias("_first_didx"),
    ])
    out = out.with_columns(
        pl.col("_fission_int")
        .rolling_sum_by("didx", f"{FISSION_LOOKBACK + 1}i", min_samples=1)
        .over("symbol")
        .alias("_fission_61_incl_today")
    ).with_columns([
        (pl.col("_fission_61_incl_today") - pl.col("_fission_int")).alias("fission_prior60"),
        ((pl.col("didx") - pl.col("_first_didx")) >= FISSION_LOOKBACK).alias("fission_history_ok"),
    ]).with_columns([
        (
            pl.col("fission_today")
            & (pl.col("fission_prior60") == 0)
            & pl.col("fission_history_ok")
        ).alias("first_fission"),
        (pl.col("fission_today") & (pl.col("fission_prior60") > 0)).alias("repeat_fission"),
        (~pl.col("fission_today")).alias("quiet_cold"),
    ])
    return out.drop(["_fission_int", "_first_didx", "_fission_61_incl_today"])


def add_future_limit(df: pl.DataFrame, horizon: int = START_HORIZON) -> pl.DataFrame:
    """反向时间轴滚动，严格排除 T；缺失交易日不会因 shift 被拉进窗口。"""
    rev = (
        df.select(["symbol", "didx", "_lu"])
        .sort(["symbol", "didx"], descending=[False, True])
        .with_columns((-pl.col("didx")).alias("_rdidx"))
        .with_columns(
            pl.col("_lu")
            .shift(1)
            .over("symbol")
            .rolling_max_by("_rdidx", f"{horizon}i", min_samples=1)
            .over("symbol")
            .alias("f_lu5_raw")
        )
    )
    return df.join(
        rev.select(["symbol", "didx", "f_lu5_raw"]), on=["symbol", "didx"], how="left"
    )


def load_panel() -> tuple[pl.DataFrame, int]:
    files = sorted(
        str(p) for p in TABLE.glob("date=*/part.parquet")
        if p.parent.name[5:] >= READ_START.isoformat()
    )
    if not files:
        raise RuntimeError(f"没有 {READ_START} 起的 enriched 日分区")
    days = [date.fromisoformat(Path(f).parent.name[5:]) for f in files]
    calendar = pl.DataFrame({"date": days}).with_row_index("didx").with_columns(
        pl.col("didx").cast(pl.Int64)
    )
    max_didx = len(days) - 1
    print(f"加载 {len(files)} 个分区：{days[0]} → {days[-1]}", flush=True)
    cols = [
        "symbol", "date", "open", "high", "low", "close", "amount",
        "turnover_rate", "consecutive_limit_ups",
    ]
    bars = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .join(calendar, on="date", how="inner")
    )
    inst = pl.read_parquet(
        ROOT / "data" / "instruments" / "instruments.parquet",
        columns=["symbol", "name", "float_shares", "listing_date"],
    )
    if bars.unique(["symbol", "didx"]).height != bars.height:
        raise RuntimeError("日K存在 symbol×didx 重复，停止计算")

    # _struct_stats 的结构窗口只要求 close/turnover/float_shares 有效，不过滤 amount。
    stats = (
        bars.join(inst, on="symbol", how="left")
        .filter(
            (pl.col("close") > 0)
            & (pl.col("turnover_rate") > 0)
            & (pl.col("float_shares") > 0)
        )
        .with_columns([
            ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("amp"),
            (pl.col("close") * pl.col("float_shares")).alias("_mcap"),
            (pl.col("consecutive_limit_ups") >= 1).cast(pl.Int32).alias("_lu"),
        ])
        .sort(["symbol", "didx"])
    )
    stats = stats.with_columns([
        pl.col("turnover_rate").rolling_median_by("didx", "20i", min_samples=1)
        .over("symbol").alias("turn_med"),
        pl.col("_mcap").rolling_median_by("didx", "20i", min_samples=1)
        .over("symbol").alias("mcap_med"),
        pl.col("amp").rolling_mean_by("didx", "20i", min_samples=1)
        .over("symbol").alias("amp20"),
        pl.col("_lu").rolling_sum_by("didx", "20i", min_samples=1)
        .over("symbol").alias("lu20"),
        pl.col("close").is_not_null().cast(pl.Int32).rolling_sum_by("didx", "60i", min_samples=1)
        .over("symbol").alias("_n60"),
    ]).with_columns(
        # 服务的 ret20 只看当前 60 市场日窗口；窗口内不足 21 个观测时必须是 null。
        pl.when(pl.col("_n60") > 20)
        .then(pl.col("close") / pl.col("close").shift(20).over("symbol") - 1)
        .otherwise(None).alias("ret20")
    )
    close60 = stats.select([
        "symbol", (pl.col("didx") + 60).alias("didx"), pl.col("close").alias("close60")
    ])
    stats = stats.join(close60, on=["symbol", "didx"], how="left").with_columns(
        (pl.col("close") / pl.col("close60") - 1).alias("ret60")
    )

    # 页面当日横截面才额外要求 amount>0。
    day = stats.filter(pl.col("amount") > 0).with_columns([
        (pl.col("close") * pl.col("float_shares")).alias("mcap"),
        pl.when(pl.col("listing_date").is_not_null())
        .then((pl.col("date") - pl.col("listing_date").str.to_date()).dt.total_days())
        .otherwise(None).cast(pl.Int64).alias("listing_days"),
    ])
    day = day.with_columns([
        cit._quantile_rank("mcap_med").alias("p_scale"),
        cit._quantile_rank("turn_med").alias("p_turn_med"),
        cit._quantile_rank("turnover_rate").alias("p_turnover"),
        cit._quantile_rank("amount").alias("p_amount"),
        cit._quantile_rank("amp20").alias("p_amp"),
        cit._quantile_rank("amp").alias("p_amp_day"),
    ]).with_columns([
        ((pl.col("p_amp").fill_null(0) + pl.col("p_turn_med")
          + (1.0 - pl.col("p_scale")) + pl.col("p_amount")) / 4.0).alias("heat"),
        cit._assign_stage(),
        ((pl.col("p_turnover") >= FISSION_QUANTILE)
         | (pl.col("p_amp_day") >= FISSION_QUANTILE)).fill_null(False).alias("fission_today"),
    ])
    day = mark_first_fission(day)
    day = add_future_limit(day)

    # 日历完整时，未来无任何有效bar表示没有涨停；不能把整周停牌排除而抬高启动率。
    day = day.with_columns(
        pl.when(pl.col("didx") <= max_didx - START_HORIZON)
        .then(pl.col("f_lu5_raw").fill_null(0).cast(pl.Int8))
        .otherwise(None).cast(pl.Int8)
        .alias("f_lu5")
    ).drop("f_lu5_raw")

    # T+1 开盘 → T+21 收盘；self-join 强制真实日历相邻，停牌日不会被跳过。
    entry = day.select([
        "symbol", (pl.col("didx") - 1).alias("didx"), pl.col("open").alias("entry_open")
    ])
    exit_ = day.select([
        "symbol", (pl.col("didx") - (RETURN_HORIZON + 1)).alias("didx"),
        pl.col("close").alias("exit_close"),
    ])
    day = day.join(entry, on=["symbol", "didx"], how="left").join(
        exit_, on=["symbol", "didx"], how="left"
    ).with_columns(
        pl.when((pl.col("entry_open") > 0) & (pl.col("exit_close") > 0))
        .then(pl.col("exit_close") / pl.col("entry_open") - 1.0)
        .otherwise(None).alias("fwd20")
    )
    market_med = day.group_by("date").agg(pl.col("fwd20").median().alias("_market_fwd20"))
    day = day.join(market_med, on="date", how="left").with_columns(
        (pl.col("fwd20") - pl.col("_market_fwd20")).alias("exc20")
    ).drop("_market_fwd20")
    print(f"面板 {day.height:,} 只-日；最新市场交易日索引 {max_didx}", flush=True)
    return day, max_didx


def parity_check(panel: pl.DataFrame) -> dict:
    """用生产服务真实输出约束回测镜像，防止第二套 cold_ok 静默漂移。"""
    latest = panel["date"].max()
    local_day = panel.filter(pl.col("date") == latest)
    local_cold = local_day.filter(cold_ok_expr())
    actual = cit.compute(ROOT / "data", latest)
    budget = actual["focus"]["budget"]
    local_picks = local_cold.sort("heat", descending=True).head(cit.FOCUS_TOP_N)["symbol"].to_list()
    actual_picks = [r["symbol"] for r in actual["focus"]["picks"]]
    got = {
        "market": local_day.height,
        "struct": local_day.filter(pl.col("heat") >= cit.HEAT_CUT).height,
        "cold": local_cold.height,
        "picks": local_picks,
    }
    expected = {k: budget[k] for k in ("market", "struct", "cold")} | {"picks": actual_picks}
    checks = {k: got[k] == expected[k] for k in got}
    if not all(checks.values()):
        raise RuntimeError(f"cold_ok 与生产服务不一致：{checks}; local={got}; service={expected}")
    return {"date": str(latest), "checks": checks, "budget": {k: got[k] for k in ("market", "struct", "cold")}}


def audit_future_limit(panel: pl.DataFrame, cold: pl.DataFrame) -> dict:
    """用五次显式日历 join 独立重算标签，不复用反向滚动实现。"""
    eligible = cold.filter(pl.col("f_lu5").is_not_null()).select(["symbol", "didx", "f_lu5"])
    if eligible.is_empty():
        return {"available": False, "n": 0, "mismatches": 0}
    probe = eligible.sample(n=min(500, eligible.height), seed=71)
    for k in range(1, START_HORIZON + 1):
        future = panel.select([
            "symbol", (pl.col("didx") - k).alias("didx"), pl.col("_lu").alias(f"_future_{k}")
        ])
        probe = probe.join(future, on=["symbol", "didx"], how="left")
    expected = pl.max_horizontal([pl.col(f"_future_{k}").fill_null(0) for k in range(1, START_HORIZON + 1)])
    checked = probe.with_columns(expected.cast(pl.Int8).alias("_expected"))
    mismatches = checked.filter(pl.col("f_lu5") != pl.col("_expected")).height
    if mismatches:
        raise RuntimeError(f"未来5日标签独立对账失败：{mismatches}/{checked.height}")
    return {"available": True, "n": checked.height, "mismatches": mismatches, "method": "5次显式didx+k join"}


def fission_funnel(panel: pl.DataFrame) -> list[dict]:
    """证明检测器在全市场能触发，并定位 cold_ok 的首次事件在哪一层被排空。"""
    base = panel.filter(pl.col("date") >= SAMPLE_START)
    layers = [
        ("market", pl.lit(True)),
        ("heat_struct", pl.col("heat") >= cit.HEAT_CUT),
        ("stage_cold", pl.col("stage") == "cold"),
        ("heat_and_cold", (pl.col("heat") >= cit.HEAT_CUT) & (pl.col("stage") == "cold")),
        ("cold_ok", cold_ok_expr()),
    ]
    out = []
    for name, expr in layers:
        seg = base.filter(expr)
        out.append({
            "layer": name,
            "rows": seg.height,
            "fission_today": int(seg["fission_today"].sum()),
            "first_fission": int(seg["first_fission"].sum()),
            "first_fission_non_limit": int((
                seg["first_fission"] & (seg["consecutive_limit_ups"] < 1)
            ).sum()),
        })
    return out


def structural_diagnosis(panel: pl.DataFrame, cold: pl.DataFrame) -> dict:
    """定位「首次分歧」在哪一层被排空，并给出机制证据，而不止报 N=0。

    判据要求 prior60==0；若 cold_ok 内所有当天高分歧票 prior60>=1，
    就证明空集来自 heat 的 20 日中位/均值分项与「60 日无记录」互斥，
    而不是检测器坏、窗口算错或样本被误删。
    """
    base = panel.filter(pl.col("date") >= SAMPLE_START)
    heat_first = base.filter(
        (pl.col("heat") >= cit.HEAT_CUT) & pl.col("first_fission")
    )
    stage_dist = {
        s: int(heat_first.filter(pl.col("stage") == s).height)
        for s in cit.STAGE_ORDER
    }
    prior = cold.filter(pl.col("fission_today"))["fission_prior60"]
    k_curve = [
        {"max_prior60": k, "n_signal": int((prior <= k).sum())}
        for k in (0, 1, 2, 3, 5, 10, 20, 40)
    ]
    parts = ["p_amp", "p_turn_med", "p_scale", "p_amount"]
    comp = {
        name: {p: float(seg[p].mean()) for p in parts}
        for name, seg in (("cold_ok", cold), ("market", base))
    }
    return {
        "heat_struct_first_fission_n": heat_first.height,
        "heat_struct_first_fission_by_stage": stage_dist,
        "cold_ok_fission_today_prior60": {
            "n": prior.len(),
            "min": int(prior.min()),
            "median": float(prior.median()),
            "p90": float(prior.quantile(0.9)),
            "max": int(prior.max()),
            "n_zero": int((prior == 0).sum()),
        },
        "signal_n_by_allowed_prior60": k_curve,
        "heat_components_mean": comp,
    }


def _summary_stats(
    df: pl.DataFrame, outcome: str, horizon: int, signal: pl.Expr, control: pl.Expr
) -> dict:
    sub = df.with_columns([
        signal.alias("_sig"), control.alias("_ctrl")
    ]).filter((pl.col("_sig") | pl.col("_ctrl")) & pl.col(outcome).is_not_null())
    sig = sub.filter(pl.col("_sig"))
    ctrl = sub.filter(pl.col("_ctrl"))
    if sig.is_empty() or ctrl.is_empty():
        return {"available": False, "n_signal": sig.height, "n_control": ctrl.height}
    daily = sub.group_by("date").agg([
        pl.col(outcome).filter(pl.col("_sig")).mean().alias("s"),
        pl.col(outcome).filter(pl.col("_ctrl")).mean().alias("c"),
    ]).drop_nulls(["s", "c"]).sort("date")
    diff = (daily["s"] - daily["c"]).to_numpy().astype(float)
    se = block_se(diff, block=2 * horizon, seed=17)
    diff_mean = float(diff.mean())
    out = {
        "available": True,
        "n_signal": sig.height,
        "n_control": ctrl.height,
        "signal_mean": float(sig[outcome].mean()),
        "control_mean": float(ctrl[outcome].mean()),
        "n_days": daily.height,
        "daily_diff": diff_mean,
        "daily_diff_se": se,
        "diff_t": diff_mean / se if se > 0 else None,
    }
    if outcome == "f_lu5":
        out.update({
            "signal_hits": int(sig[outcome].sum()),
            "control_hits": int(ctrl[outcome].sum()),
            "lift": out["signal_mean"] / out["control_mean"] if out["control_mean"] > 0 else None,
        })
    else:
        alpha_rows = {}
        for name, mask in (("signal", pl.col("_sig")), ("control", pl.col("_ctrl"))):
            a = sub.filter(mask).group_by("date").agg(pl.col("exc20").mean().alias("x")).sort("date")
            arr = a["x"].to_numpy().astype(float)
            ase = block_se(arr, block=2 * horizon, seed=23)
            amean = float(arr.mean())
            alpha_rows[f"{name}_excess_mean"] = amean
            alpha_rows[f"{name}_excess_t"] = amean / ase if ase > 0 else None
            alpha_rows[f"{name}_excess_days"] = a.height
        out.update(alpha_rows)
    return out


def compare_groups(df: pl.DataFrame, signal: pl.Expr, control: pl.Expr) -> dict:
    return {
        "start5": _summary_stats(df, "f_lu5", START_HORIZON, signal, control),
        "return20": _summary_stats(df, "exc20", RETURN_HORIZON, signal, control),
    }


def mcap_controlled(
    df: pl.DataFrame, outcome: str, horizon: int, signal: pl.Expr, control: pl.Expr
) -> dict:
    """日期×市值五格内比较；每日按信号格样本数加权，t 仍按日块 bootstrap。"""
    sub = df.with_columns([
        signal.alias("_sig"), control.alias("_ctrl"),
        (pl.col("p_scale") * MCAP_BINS).floor().cast(pl.Int32)
        .clip(0, MCAP_BINS - 1).alias("_mcap_bin"),
    ]).filter((pl.col("_sig") | pl.col("_ctrl")) & pl.col(outcome).is_not_null())
    total_signal_n = sub.filter(pl.col("_sig")).height
    cells = sub.group_by(["date", "_mcap_bin"]).agg([
        pl.col("_sig").sum().alias("sn"),
        pl.col("_ctrl").sum().alias("cn"),
        pl.col(outcome).filter(pl.col("_sig")).mean().alias("s"),
        pl.col(outcome).filter(pl.col("_ctrl")).mean().alias("c"),
    ]).filter((pl.col("sn") > 0) & (pl.col("cn") > 0))
    if cells.is_empty():
        return {"available": False, "matched_signal_n": 0, "total_signal_n": total_signal_n}
    daily = cells.with_columns([
        ((pl.col("s") - pl.col("c")) * pl.col("sn")).alias("dw"),
        (pl.col("s") * pl.col("sn")).alias("sw"),
        (pl.col("c") * pl.col("sn")).alias("cw"),
    ]).group_by("date").agg([
        pl.col("sn").sum().alias("n"), pl.col("dw").sum().alias("dw"),
        pl.col("sw").sum().alias("sw"), pl.col("cw").sum().alias("cw"),
    ]).sort("date").with_columns([
        (pl.col("dw") / pl.col("n")).alias("diff"),
        (pl.col("sw") / pl.col("n")).alias("s"),
        (pl.col("cw") / pl.col("n")).alias("c"),
    ])
    arr = daily["diff"].to_numpy().astype(float)
    se = block_se(arr, block=2 * horizon, seed=31)
    matched_n = int(daily["n"].sum())
    smean = float(daily["sw"].sum() / matched_n)
    cmean = float(daily["cw"].sum() / matched_n)
    mean = float(arr.mean())
    out = {
        "available": True,
        "matched_signal_n": matched_n,
        "total_signal_n": total_signal_n,
        "signal_coverage": matched_n / total_signal_n if total_signal_n else None,
        "n_days": daily.height,
        "signal_mean": smean,
        "control_mean": cmean,
        "daily_diff": mean,
        "daily_diff_se": se,
        "diff_t": mean / se if se > 0 else None,
    }
    if outcome == "f_lu5":
        out["lift"] = smean / cmean if cmean > 0 else None
    return out


def add_placebo(df: pl.DataFrame) -> pl.DataFrame:
    """同日期×市值格内保留完全相同的事件数，检验评价管线是否制造假效应。"""
    counts = df.with_columns(
        (pl.col("p_scale") * MCAP_BINS).floor().cast(pl.Int32)
        .clip(0, MCAP_BINS - 1).alias("_mcap_bin")
    ).group_by(["date", "_mcap_bin"]).agg(pl.col("first_fission").sum().alias("_placebo_n"))
    return df.with_columns([
        (pl.col("p_scale") * MCAP_BINS).floor().cast(pl.Int32)
        .clip(0, MCAP_BINS - 1).alias("_mcap_bin"),
        pl.concat_str([pl.col("symbol"), pl.col("date").cast(pl.Utf8)])
        .hash(seed=42).alias("_random_hash"),
    ]).join(counts, on=["date", "_mcap_bin"], how="left").with_columns(
        (pl.col("_random_hash").rank("ordinal").over(["date", "_mcap_bin"])
         <= pl.col("_placebo_n")).alias("placebo_fission")
    )


def _rounded(value):
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, float):
        return None if not np.isfinite(value) else round(value, 8)
    if isinstance(value, np.generic):
        return value.item()
    return value


def verdict(primary: dict, controlled_start: dict, controlled_return: dict) -> dict:
    s, r = primary["start5"], primary["return20"]
    enough = (
        s.get("n_signal", 0) >= MIN_SIGNAL_N
        and s.get("n_days", 0) >= MIN_SIGNAL_DAYS
        and r.get("n_days", 0) >= MIN_SIGNAL_DAYS
    )
    raw_start = bool(s.get("lift") is not None and s["lift"] >= 1.5)
    raw_return = bool(
        r.get("diff_t") is not None and r["diff_t"] >= 2
        and r.get("signal_excess_t") is not None and r["signal_excess_t"] >= 2
    )
    retention = None
    if controlled_return.get("daily_diff") is not None and r.get("daily_diff") not in (None, 0):
        retention = controlled_return["daily_diff"] / r["daily_diff"]
    controlled_ok = bool(
        controlled_start.get("lift") is not None and controlled_start["lift"] >= 1.5
        and controlled_return.get("diff_t") is not None and controlled_return["diff_t"] >= 2
        and retention is not None and retention >= 0.50
    )
    passed = bool(enough and raw_start and raw_return and controlled_ok)
    n_signal = s.get("n_signal", 0)
    if n_signal == 0:
        status = "NOT_ESTIMABLE_EMPTY_SIGNAL"
        reason = (
            "严格定义（前60市场交易日无同类事件）在 cold_ok 内为空集："
            "既不能判为通过，也不能判为『启动前兆已被证伪』。"
            "事前判据的 lift/t 检验没有可估计对象，结论是不可执行。"
        )
    elif passed:
        status = "PASSED"
        reason = "全部事前判据满足，首次分歧对今日触点有可估计增量。"
    else:
        status = "FALSIFIED"
        reason = "信号非空但未达到事前 lift/t 与市值控制判据。"
    return {
        "status": status,
        "reason": reason,
        "enough_sample": enough,
        "raw_start_lift_ge_1_5": raw_start,
        "raw_return_diff_and_excess_t_ge_2": raw_return,
        "mcap_controlled_no_decay": controlled_ok,
        "mcap_return_effect_retention": retention,
        "passed": passed,
        "rule": (
            "lift>=1.5；信号20日超额t>=2且相对cold对照差t>=2；"
            "市值格内lift仍>=1.5、差t>=2且效应保留>=50%；信号>=100且>=30日"
        ),
    }


def run_self_tests() -> None:
    a = pl.DataFrame({
        "symbol": ["a"] * 61 + ["b"] * 61,
        "didx": list(range(1, 62)) * 2,
        "fission_today": [False] * 60 + [True] + [True] + [False] * 59 + [True],
    })
    marked = mark_first_fission(a)
    a61 = marked.filter((pl.col("symbol") == "a") & (pl.col("didx") == 61)).row(0, named=True)
    b61 = marked.filter((pl.col("symbol") == "b") & (pl.col("didx") == 61)).row(0, named=True)
    assert a61["first_fission"] and not a61["repeat_fission"]
    assert not b61["first_fission"] and b61["repeat_fission"] and b61["fission_prior60"] == 1

    f = pl.DataFrame({
        "symbol": ["a"] * 7 + ["b"] * 7 + ["c"] * 7,
        "didx": list(range(1, 8)) * 3,
        "_lu": [1, 0, 1, 0, 0, 0, 0] + [0] * 7 + [0, 0, 0, 0, 0, 0, 1],
    })
    fl = add_future_limit(f)
    assert fl.filter((pl.col("symbol") == "a") & (pl.col("didx") == 1))["f_lu5_raw"][0] == 1
    assert fl.filter((pl.col("symbol") == "a") & (pl.col("didx") == 3))["f_lu5_raw"][0] == 0
    # b 全期为 0：若 a 的涨停漏进 b，就是跨股票污染，而不是市场规律。
    assert fl.filter((pl.col("symbol") == "b") & (pl.col("didx") <= 2))["f_lu5_raw"].sum() == 0
    # c 仅在第7日涨停：对T=1是T+6不得计入，对T=2是T+5必须计入。
    assert fl.filter((pl.col("symbol") == "c") & (pl.col("didx") == 1))["f_lu5_raw"][0] == 0
    assert fl.filter((pl.col("symbol") == "c") & (pl.col("didx") == 2))["f_lu5_raw"][0] == 1
    print("自检通过：60 日边界、重复分歧、未来 5 日排除 T/T+6 且不跨股票", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="只运行合成边界自检")
    args = parser.parse_args()
    run_self_tests()
    if args.self_test:
        return

    panel, _ = load_panel()
    parity = parity_check(panel)
    print(f"生产同源对账通过：{parity}", flush=True)
    cold = panel.filter(
        (pl.col("date") >= SAMPLE_START) & cold_ok_expr()
    )
    if cold.filter(pl.col("consecutive_limit_ups") >= 1).height:
        raise RuntimeError("cold_ok 内出现当日涨停，阶段定义或标签已错位")
    cold = add_placebo(cold)
    label_audit = audit_future_limit(panel, cold)
    funnel = fission_funnel(panel)
    structural = structural_diagnosis(panel, cold)
    print(
        f"cold_ok {cold.height:,} 只-日；first={int(cold['first_fission'].sum()):,}；"
        f"repeat={int(cold['repeat_fission'].sum()):,}；quiet={int(cold['quiet_cold'].sum()):,}",
        flush=True,
    )
    print(f"未来标签独立对账：{label_audit}", flush=True)
    print(f"首次分歧漏斗：{funnel}", flush=True)
    print(f"结构性诊断：{structural}", flush=True)

    primary = compare_groups(cold, pl.col("first_fission"), ~pl.col("first_fission"))
    controlled_start = mcap_controlled(
        cold, "f_lu5", START_HORIZON, pl.col("first_fission"), ~pl.col("first_fission")
    )
    controlled_return = mcap_controlled(
        cold, "exc20", RETURN_HORIZON, pl.col("first_fission"), ~pl.col("first_fission")
    )
    first_vs_quiet = compare_groups(cold, pl.col("first_fission"), pl.col("quiet_cold"))
    repeat_vs_quiet = compare_groups(cold, pl.col("repeat_fission"), pl.col("quiet_cold"))
    placebo = compare_groups(cold, pl.col("placebo_fission"), ~pl.col("placebo_fission"))

    years = {}
    for y in range(SAMPLE_START.year, panel["date"].max().year + 1):
        seg = cold.filter(pl.col("date").dt.year() == y)
        if seg.is_empty():
            continue
        years[str(y)] = compare_groups(seg, pl.col("first_fission"), ~pl.col("first_fission"))

    report = {
        "spec": {
            "read_start": str(READ_START), "sample_start": str(SAMPLE_START),
            "data_end": str(panel["date"].max()),
            "fission": "T日 turnover_rate 或 (high-low)/close 的全市场分位>=0.80；前60市场交易日同类事件=0；T未涨停",
            "pool": "citizenship.cold_ok（含heat/stage/ST/BSE/成交额/上市天数/ret60/ret20全部过滤）",
            "start_label": "T+1..T+5 consecutive_limit_ups>=1，纯涨停",
            "return_label": "T+1前复权open→T+21前复权close；exc20减当日全市场中位数；未扣成本",
            "inference": "按日聚合，circular block bootstrap 1000次，块长2H",
            "mcap_control": "日期×20日中位市值的全市场分位五格内配对，按信号格样本数加权",
        },
        "parity": parity,
        "future_label_audit": label_audit,
        "fission_funnel": funnel,
        "structural_diagnosis": structural,
        "counts": {
            "cold_rows": cold.height,
            "first_fission": int(cold["first_fission"].sum()),
            "repeat_fission": int(cold["repeat_fission"].sum()),
            "quiet_cold": int(cold["quiet_cold"].sum()),
        },
        "primary_first_vs_all_other_cold": primary,
        "mcap_controlled": {"start5": controlled_start, "return20": controlled_return},
        "first_vs_quiet": first_vs_quiet,
        "repeat_vs_quiet": repeat_vs_quiet,
        "random_placebo_same_date_mcap_cells": placebo,
        "by_year": years,
        "verdict": verdict(primary, controlled_start, controlled_return),
        "limitations": [
            "kline_daily_enriched 是当前在册名单回溯面板，存在生存偏差；本实验不是全历史退市并集。",
            "ST 与上市日期来自当前 instruments 维表，不是完整历史名称快照。",
            "收益用于信号增量检验，不是成交回测：未扣费用，未剔除T+1一字涨停不可成交。",
            "时间分段是稳健性检查，不是真正未接触样本外；阈值在本次运行前已固定。",
            "N=0 是『不可估计』，不是负效应：本实验不能证明启动前兆无效，只能证明该严格定义在触点池内无对象可测。",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "first_fission.json").write_text(
        json.dumps(_rounded(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cold.select([
        "symbol", "name", "date", "heat", "p_scale", "p_turnover", "p_amp_day",
        "fission_today", "fission_prior60", "first_fission", "repeat_fission",
        "quiet_cold", "placebo_fission", "f_lu5", "fwd20", "exc20",
    ]).write_parquet(OUT / "first_fission_cold_samples.parquet")
    print(json.dumps(_rounded(report["verdict"]), ensure_ascii=False, indent=2), flush=True)
    print(f"写出 {OUT / 'first_fission.json'}", flush=True)


if __name__ == "__main__":
    main()
