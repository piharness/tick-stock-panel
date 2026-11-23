#!/usr/bin/env python3
"""_diag_state_conditional_audit.py — 对 `_diag_state_conditional.py` 的口径审计 + 重算。

前一份脚本给出的结论是「不要等横盘，横盘是最差落点，2026 下行之后 82% 上涨」。
本脚本不复述结论，只回答：那些数字在什么口径下成立、在什么口径下会翻转。

审计项（每项都先写"如果为真会看到什么"）：
  A1 等权指数的日收益是否日历相邻。原脚本用 gap 1..10 天，停牌复牌把 10 天
     跌幅记成 1 天 => 放大日波动 => 加深回撤 => 多判 down。改为 gap==1 重算。
  A2 `state_stats` 是否落盘。原脚本 for 循环变量 `st` 覆盖同名结果变量，JSON 里
     写的是字符串 "up" => 磁盘上的产物无法复现正文表格。
  A3 「下行平均持续 28 天」是哪个统计量。原脚本取 state_len 的**中位数**且按
     **日**加权（inspection paradox），不是"完成态持续期的均值"。改为按 spell 重建。
  A4 n_eff。「2026 down 33 天 / flat 131 天」的 f20 是 20 日重叠窗口，
     非重叠子样本还剩几个观测？重算非重叠 + 块自助置信区间。
  A5 阈值敏感性。-5% 换成 -2/-3.5/-8/-12% 时，2026 的 down/flat 标签与中位数是否翻转。
  A6 「逢跌买 vs 任意日买」是否为同日可比的对照。原对照组 ret1>-2% 含全部上涨日，
     且两组按日期聚集（全市场跌日里所有股票都进实验组）=> 差异被市场择时污染。
     改为**同日横截面**配对：同一天内 dip 组减非 dip 组，再对日期序列取 t。
  A7 fwd1 是 open(t+1)→close(t+1) 的**日内**收益，T+1 下不可执行；
     "fwd20 是 fwd1 的 1.4 倍"不是"持有更久更划算"，是拿 20 天比 0 个隔夜。
  A8 策略层：空仓/只在上行持有/全程持有，扣 20bp 单边，比 CAGR/MDD/Sharpe。

输出：analysis/_diag_state_conditional_audit_result.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kline_daily_enriched"
CACHE = ROOT / "data" / "_cache" / "_audit_state_panel.parquet"
OUT = Path(__file__).with_name("_diag_state_conditional_audit_result.json")
START = "2016-01-01"
COST = 0.0020
HORIZONS = (1, 5, 10, 20, 60)


# ───────────────────────── 数据 ─────────────────────────
def load_panel() -> pl.DataFrame:
    if CACHE.exists():
        return pl.read_parquet(CACHE)
    files = []
    for e in os.scandir(DATA):
        if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= START:
            files.append(str(Path(e.path) / "part.parquet"))
    files.sort()
    df = pl.read_parquet(
        files,
        columns=["symbol", "date", "open", "high", "low", "close", "amount", "turnover_rate"],
    ).filter((pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0))
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(CACHE)
    return df


def with_calendar(df: pl.DataFrame) -> pl.DataFrame:
    """给每行附上市场日历序号 rn（0..），用交易日距离而不是自然日距离判相邻。"""
    cal = pl.DataFrame({"date": sorted(df["date"].unique().to_list())}).with_row_index("rn")
    return df.join(cal, on="date", how="left")


def build_index(df: pl.DataFrame, strict_gap: bool) -> pl.DataFrame:
    """全市场等权指数。

    strict_gap=True：只认**交易日**相邻（rn 差 1）。自然日 1..10 会把停牌复牌的
    多日跌幅算成单日收益（放大日波动、加深回撤），这是本项目已记录的复发缺陷。
    """
    d = with_calendar(df).sort(["symbol", "date"]).with_columns(
        [
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("r"),
            (pl.col("rn") - pl.col("rn").shift(1).over("symbol")).alias("g"),
        ]
    )
    cond = pl.col("g") == 1 if strict_gap else pl.col("g").is_between(1, 10)
    d = d.with_columns(pl.when(cond).then(pl.col("r")).otherwise(None).alias("r"))
    daily = (
        d.group_by("date")
        .agg([pl.col("r").mean().alias("ew_ret"), pl.col("r").count().alias("n")])
        .sort("date")
    )
    return daily.with_columns(
        [
            (1.0 + pl.col("ew_ret")).cum_prod().alias("ew_idx"),
            pl.col("ew_ret").std().alias("_sd"),
        ]
    ).drop("_sd")


RET60_CUTS = (-0.02, -0.035, -0.05, -0.08, -0.12)


def add_state(daily: pl.DataFrame, down_cut: float = -0.05, up_cut: float = 0.05) -> pl.DataFrame:
    d = daily.with_columns(
        [
            pl.col("ew_idx").rolling_mean(20).alias("ma20"),
            pl.col("ew_idx").rolling_mean(60).alias("ma60"),
            pl.col("ew_idx").pct_change(60).alias("ret60"),
        ]
    )
    d = d.with_columns(
        pl.when(
            (pl.col("ew_idx") < pl.col("ma60"))
            & (pl.col("ma20") < pl.col("ma60"))
            & (pl.col("ret60") < down_cut)
        )
        .then(pl.lit("down"))
        .when(
            (pl.col("ew_idx") > pl.col("ma60"))
            & (pl.col("ma20") > pl.col("ma60"))
            & (pl.col("ret60") > up_cut)
        )
        .then(pl.lit("up"))
        .otherwise(pl.lit("flat"))
        .alias("state")
    )
    for k in HORIZONS:
        d = d.with_columns(
            pl.col("ew_idx").shift(-k).truediv(pl.col("ew_idx")).sub(1.0).alias(f"f{k}")
        )
    d = d.with_columns(pl.col("state").shift(1).alias("state_prev"))
    d = d.with_columns((pl.col("state") != pl.col("state").shift(1)).cum_sum().alias("grp"))
    d = d.with_columns(pl.col("grp").len().over("grp").alias("state_len"))
    # 段内**已持续天数**（1,2,3...）。state_len 是整段总长（事后才知道），
    # 拿它做「满 N 日才走」的门槛 = 前视：t 日就知道这段会持续 14 天。
    d = d.with_columns(pl.int_range(1, pl.len() + 1).over("grp").alias("state_age"))
    return d


def med(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def pos(v: list[float]) -> float:
    return sum(1 for x in v if x > 0) / len(v) * 100


def block_boot(v: list[float], block: int = 20, iters: int = 400, seed: int = 7) -> tuple[float, float]:
    """块自助：20 日重叠窗口的自相关由 block≈horizon 吸收。返回 (中位数 2.5%, 97.5%)。"""
    import random

    rnd = random.Random(seed)
    n = len(v)
    if n < block * 2:
        return (float("nan"), float("nan"))
    nb = max(2, n // block)
    out = []
    for _ in range(iters):
        acc: list[float] = []
        while len(acc) < n:
            i = rnd.randrange(0, n - block + 1)
            acc.extend(v[i : i + block])
        out.append(med(acc[:n]))
    out.sort()
    return (out[int(0.025 * iters)], out[int(0.975 * iters)])


# ───────────────────────── A1 ─────────────────────────
def audit_calendar_gap(df: pl.DataFrame) -> dict:
    lax = build_index(df, strict_gap=False)
    strict = build_index(df, strict_gap=True)
    res = {}
    for name, d in (("lax_gap_1_10", lax), ("strict_gap_1", strict)):
        s = add_state(d)
        y26 = s.filter((pl.col("date").dt.year() == 2026) & pl.col("ew_idx").is_not_null())
        res[name] = {
            "ew_daily_sd_pct": round(float(d["ew_ret"].std()) * 100, 3),
            "ew_idx_last": round(float(s["ew_idx"][-1]), 3),
            "y2026_ew_pct": round(float(y26["ew_idx"][-1] / y26["ew_idx"][0] - 1) * 100, 2),
            "n_down": int(s.filter(pl.col("state") == "down").height),
            "n_flat": int(s.filter(pl.col("state") == "flat").height),
            "n_up": int(s.filter(pl.col("state") == "up").height),
            "last_state": s["state"][-1],
            "y2026_down_days": int(
                y26.filter(pl.col("state_prev") == "down").height
            ),
            "y2026_flat_days": int(
                y26.filter(pl.col("state_prev") == "flat").height
            ),
        }
    return res


# ───────────────────────── A2 ─────────────────────────
def audit_artifact_overwrite() -> dict:
    """静态证据：原脚本 JSON 里 state_stats 是什么类型。"""
    p = Path(__file__).with_name("_diag_state_conditional_result.json")
    j = json.loads(p.read_text(encoding="utf-8"))
    return {
        "state_stats_in_json": j.get("state_stats"),
        "type": type(j.get("state_stats")).__name__,
        "keys": list(j.keys()),
        "verdict": "结果变量被 for 循环变量覆盖 => 正文的 459/1383/772 无法从产物复现",
    }


# ───────────────────────── A3 ─────────────────────────
def spell_stats(d: pl.DataFrame, target: str = "down") -> dict:
    """按 spell 重建持续期；并给出「已走到第 a 天时还剩多久」的条件分布。"""
    s = d.select(["date", "state", "grp", "state_len"]).sort("date")
    spells = (
        s.filter(pl.col("state") == target)
        .group_by("grp")
        .agg(
            pl.col("date").min().alias("start"),
            pl.col("date").max().alias("end"),
            pl.col("state_len").first().alias("len"),
        )
        .sort("start")
    )
    lens = spells["len"].to_list()
    completed = lens[:-1] if spells.height and spells["end"][-1] == s["date"][-1] else lens
    cur = spells.tail(1).to_dicts()[0] if spells.height else {}
    # 已持续 a 天时，剩余天数（<=0 即当天结束）的经验分布 —— 只用完成态
    surv = {}
    for a in (5, 10, 14, 20, 28):
        rem = [L - a for L in completed if L >= a]
        if len(rem) < 5:
            surv[f"age{a}"] = {"n": len(rem)}
            continue
        surv[f"age{a}"] = {
            "n": len(rem),
            "p_still_down_next_day": round(sum(1 for r in rem if r > 0) / len(rem) * 100, 1),
            "p_lasts_to_28": round(sum(1 for L in completed if L >= a and L >= 28) / len(rem) * 100, 1),
            "med_remaining": med(rem),
            "mean_remaining": round(sum(rem) / len(rem), 1),
        }
    return {
        "n_spells": spells.height,
        "n_completed": len(completed),
        "len_med": med(completed) if completed else None,
        "len_mean": round(sum(completed) / len(completed), 1) if completed else None,
        "len_p25_p75": (
            sorted(completed)[len(completed) // 4],
            sorted(completed)[3 * len(completed) // 4],
        )
        if completed
        else None,
        "len_max": max(completed) if completed else None,
        "current_spell": {
            k: str(v) for k, v in cur.items()
        },
        "conditional_remaining": surv,
        "naive_median_state_len_over_days": round(
            float(d.filter(pl.col("state") == target)["state_len"].median() or 0), 1
        ),
    }


# ───────────────────────── A4 ─────────────────────────
def neff_table(d: pl.DataFrame, horizon: int = 20) -> dict:
    """每个 cell：重叠 n、非重叠 n、非重叠中位/胜率、块自助 CI。"""
    out: dict = {}
    d = d.filter(pl.col(f"f{horizon}").is_not_null())
    for y in list(range(2016, 2027)) + ["all"]:
        sub = d if y == "all" else d.filter(pl.col("date").dt.year() == y)
        if sub.height < 30:
            continue
        rec = {}
        for st in ("down", "flat", "up"):
            s = sub.filter(pl.col("state_prev") == st)
            v = s[f"f{horizon}"].to_list()
            if len(v) < 15:
                continue
            nov = v[::horizon]  # 非重叠：每 horizon 个交易日取 1
            lo, hi = block_boot(v, block=horizon)
            rec[st] = {
                "n_overlap": len(v),
                "n_nonoverlap": len(nov),
                "med_all_pct": round(med(v) * 100, 2),
                "pos_all_pct": round(pos(v), 1),
                "med_ci95_pct": [round(lo * 100, 2), round(hi * 100, 2)],
                "med_nonoverlap_pct": round(med(nov) * 100, 2) if nov else None,
                "pos_nonoverlap_pct": round(pos(nov), 1) if nov else None,
            }
        if rec:
            out[str(y)] = rec
    return out


# ───────────────────────── A5 ─────────────────────────
def threshold_sensitivity(daily_strict: pl.DataFrame) -> dict:
    out = {}
    for cut in RET60_CUTS:
        s = add_state(daily_strict, down_cut=cut, up_cut=-cut)
        y26 = s.filter(pl.col("date").dt.year() == 2026)
        rec = {"n_down_2026": int(y26.filter(pl.col("state") == "down").height),
               "n_flat_2026": int(y26.filter(pl.col("state") == "flat").height),
               "last_state": s["state"][-1]}
        for st in ("down", "flat"):
            sub = y26.filter((pl.col("state_prev") == st) & pl.col("f20").is_not_null())
            v = sub["f20"].to_list()
            rec[st] = (
                {"n": len(v), "med_pct": round(med(v) * 100, 2), "pos_pct": round(pos(v), 1)}
                if len(v) >= 10
                else None
            )
        # 全样本
        for st in ("down", "flat"):
            sub = s.filter((pl.col("state_prev") == st) & pl.col("f20").is_not_null())
            v = sub["f20"].to_list()
            rec[f"all_{st}"] = {"n": len(v), "med_pct": round(med(v) * 100, 2), "pos_pct": round(pos(v), 1)}
        out[f"cut_{cut:+.3f}"] = rec
    return out


# ───────────────────────── A6 ─────────────────────────
def build_stock_frame(df: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    """个股层：次日开盘入场的 fwd1/5/20、当日涨跌、换手、流通市值、市场状态。"""
    d = with_calendar(df).sort(["symbol", "date"]).with_columns(
        [
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"),
            (pl.col("rn") - pl.col("rn").shift(1).over("symbol")).alias("g1"),
        ]
    )
    d = d.with_columns(
        pl.when(pl.col("g1") == 1).then(pl.col("ret1")).otherwise(None).alias("ret1")
    )
    d = d.with_columns(
        [
            (pl.col("amount") / (pl.col("turnover_rate") / 100)).alias("fmcap"),
            pl.col("turnover_rate").alias("turn"),
        ]
    )
    d = d.with_columns(
        [
            (pl.col("amount").rank("ordinal").over("date") - 1).truediv(pl.len().over("date") - 1).alias("pa"),
            (pl.col("fmcap").rank("ordinal").over("date") - 1).truediv(pl.len().over("date") - 1).alias("pm"),
            ((pl.col("turn").rank("ordinal").over("date") - 1) * 5 // pl.len().over("date")).alias("tq"),
            ((pl.col("fmcap").rank("ordinal").over("date") - 1) * 5 // pl.len().over("date")).alias("mq"),
        ]
    )
    for k in (1, 5, 20):
        d = d.with_columns(
            [
                pl.col("open").shift(-1).over("symbol").alias(f"_entry{k}"),
                pl.col("close").shift(-k).over("symbol").alias(f"_c{k}"),
                (pl.col("rn").shift(-k).over("symbol") - pl.col("rn")).alias(f"_d{k}"),
                (pl.col("rn").shift(-1).over("symbol") - pl.col("rn")).alias("_de"),
            ]
        )
        d = d.with_columns(
            pl.when((pl.col("_de") == 1) & (pl.col(f"_d{k}") == k))
            .then(pl.col(f"_c{k}") / pl.col(f"_entry{k}") - 1.0)
            .otherwise(None)
            .alias(f"fwd{k}")
        ).drop([f"_entry{k}", f"_c{k}", f"_d{k}", "_de"])
    st = daily.select(["date", pl.col("state_prev").alias("mkt_state")])
    return d.join(st, on="date", how="left")


def dip_test_within_date(d: pl.DataFrame) -> dict:
    """同日横截面配对：dip(ret1<=-2%) 组的 fwd 减同日非 dip 组。

    这才是"等阴线"的正确检验：日期固定，比较同一天里跌的和没跌的。
    原脚本对照组把全部上涨日也算进去，且两组按日期聚集 => 差异被市场择时污染。
    """
    d = d.filter((pl.col("pa") >= 0.90) & (pl.col("pm") >= 0.85))
    d = d.with_columns((pl.col("ret1") <= -0.02).alias("dip"))

    out = {}
    for st_name in ("down", "flat", "up", "all"):
        base = d if st_name == "all" else d.filter(pl.col("mkt_state") == st_name)
        for k in (1, 20):
            g = (
                base.filter(pl.col(f"fwd{k}").is_not_null())
                .group_by("date")
                .agg(
                    pl.col(f"fwd{k}").filter(pl.col("dip")).mean().alias("mu_dip"),
                    pl.col(f"fwd{k}").filter(~pl.col("dip")).mean().alias("mu_nodip"),
                    pl.col(f"fwd{k}").filter(pl.col("dip")).len().alias("n_dip"),
                )
                .filter(
                    pl.col("mu_dip").is_not_null()
                    & pl.col("mu_nodip").is_not_null()
                    & (pl.col("n_dip") >= 5)
                )
            )
            diff = (g["mu_dip"] - g["mu_nodip"]).to_list()
            nd = len(diff)
            if nd < 20:
                continue
            m = sum(diff) / nd
            sd = (sum((x - m) ** 2 for x in diff) / (nd - 1)) ** 0.5
            t = m / (sd / nd**0.5) if sd > 0 else float("nan")
            out[f"{st_name}_fwd{k}"] = {
                "n_dates": nd,
                "mean_diff_bp": round(m * 1e4, 1),
                "t": round(t, 2),
                "med_diff_bp": round(med(diff) * 1e4, 1),
                "win_pct": round(pos(diff), 1),
            }
    return out


def dip_decile(d: pl.DataFrame) -> dict:
    """同日按当日涨跌幅分 10 档，看 fwd20 是否单调 —— 避免"一个阈值"的口径争议。"""
    d = d.filter(
        (pl.col("pa") >= 0.90) & (pl.col("pm") >= 0.85) & pl.col("ret1").is_not_null()
    )
    q = (
        d.filter(pl.col("fwd20").is_not_null())
        .with_columns(((pl.col("ret1").rank("ordinal").over("date") - 1) * 10 // pl.col("fwd20").count().over("date")).alias("dq"))
        .group_by(["date", "dq"])
        .agg(pl.col("fwd20").mean().alias("mu"))
        .group_by("dq")
        .agg(pl.col("mu").mean().alias("mu_fwd20"), pl.col("mu").len().alias("n_dates"))
        .sort("dq")
    )
    return {
        str(int(r["dq"])): {
            "n_dates": int(r["n_dates"]),
            "mean_fwd20_bp": round(r["mu_fwd20"] * 1e4, 1),
        }
        for r in q.to_dicts()
    }


def tail_by_axis(d: pl.DataFrame) -> dict:
    """同一张表上比两个轴：市场状态 vs 组合构成（换手×市值）。

    因变量是**左尾**P(fwd20 < -20%) 和中位 fwd20，2026 年，只看已实现的 20 日。
    """
    y = d.filter((pl.col("date").dt.year() == 2026) & pl.col("fwd20").is_not_null())

    def agg(frame: pl.DataFrame, key: str) -> dict:
        g = frame.group_by(key).agg(
            pl.col("fwd20").len().alias("n"),
            (pl.col("fwd20") < -0.20).mean().alias("p_tail"),
            pl.col("fwd20").median().alias("med"),
        )
        return {
            str(int(r[key]) if isinstance(r[key], (int, float)) else r[key]): {
                "n": int(r["n"]),
                "p_lt_-20pct": round(r["p_tail"] * 100, 1),
                "med_pct": round(r["med"] * 100, 2),
            }
            for r in g.to_dicts()
        }

    out = {
        "by_state_2026": {
            str(r["mkt_state"]): {
                "n": int(r["n"]),
                "p_lt_-20pct": round(r["p_tail"] * 100, 1),
                "med_pct": round(r["med"] * 100, 2),
            }
            for r in y.group_by("mkt_state")
            .agg(
                pl.col("fwd20").len().alias("n"),
                (pl.col("fwd20") < -0.20).mean().alias("p_tail"),
                pl.col("fwd20").median().alias("med"),
            )
            .to_dicts()
        },
        "by_turnover_q_2026": agg(y, "tq"),
        "by_mcap_q_2026": agg(y, "mq"),
    }
    # 换手×市值的 5×5 网格（左尾）
    grid = (
        y.group_by(["tq", "mq"])
        .agg(
            pl.col("fwd20").len().alias("n"),
            (pl.col("fwd20") < -0.20).mean().alias("p_tail"),
        )
        .sort(["mq", "tq"])
    )
    out["grid_turnover_x_mcap_2026"] = {
        f"t{int(r['tq'])}_m{int(r['mq'])}": {
            "n": int(r["n"]),
            "p_lt_-20pct": round(r["p_tail"] * 100, 1),
        }
        for r in grid.to_dicts()
    }
    return out


# ───────────────────────── A8 ─────────────────────────
def _eval_weights(dd: pl.DataFrame, w: pl.Series) -> dict:
    w = dd.with_columns(w.alias("w"))
    w = w.with_columns(pl.col("w").shift(1).fill_null(0.0).alias("w_prev"))
    w = w.with_columns(
        (pl.col("w") * pl.col("ew_ret") - (pl.col("w") - pl.col("w_prev")).abs() * COST).alias("pnl")
    )
    w = w.with_columns((1.0 + pl.col("pnl")).cum_prod().alias("eq"))
    ret = w["pnl"].to_list()
    eqv = w["eq"].to_list()
    n = len(ret)
    years = n / 244.0
    cagr = (eqv[-1] ** (1 / years) - 1) * 100 if eqv[-1] > 0 else -100
    peak, mdd = -1e9, 0.0
    for x in eqv:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    mu = sum(ret) / n
    sd = (sum((r - mu) ** 2 for r in ret) / (n - 1)) ** 0.5
    return {
        "cagr_pct": round(cagr, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "sharpe": round(mu / sd * (244**0.5), 2) if sd > 0 else None,
        "exposure_pct": round(float(w["w"].mean()) * 100, 1),
        "n_switches": int((w["w"] - w["w_prev"]).abs().sum()),
        "final_eq": round(eqv[-1], 3),
    }


def strategy_level(d: pl.DataFrame) -> dict:
    """可执行配置，扣 20bp 单边（状态切换才交易）。

    三档实现强度，用来排除"你实现得太抖"这个反驳：
      daily       : t-1 状态直接决定 t 日仓位（原脚本口径）
      confirm5    : 状态需已持续 >=5 日才动（抑制 3 日抖动的状态）
      monthly     : 每月首个交易日按当时状态定一个月
    """
    dd = d.filter(pl.col("ew_ret").is_not_null()).select(["date", "ew_ret", "state_prev", "state_age"])
    # state_age（段内已持续天数）也必须是 t-1 可得的：用 shift(1)。
    # 用 state_len（整段总长）会变成前视 —— 那一位等于提前知道这段会持续多久。
    dd = dd.with_columns(pl.col("state_age").shift(1).alias("len_prev"))
    out = {}
    out["always_hold"] = _eval_weights(dd, pl.lit(1.0))
    for name, expr in (
        ("daily_not_down", pl.col("state_prev") != "down"),
        ("daily_only_up", pl.col("state_prev") == "up"),
        ("confirm5_not_down", (pl.col("state_prev") != "down") | (pl.col("len_prev") < 5)),
        ("confirm5_only_up", (pl.col("state_prev") == "up") | (pl.col("len_prev") < 5)),
        ("confirm10_not_down", (pl.col("state_prev") != "down") | (pl.col("len_prev") < 10)),
    ):
        out[name] = _eval_weights(dd, expr.fill_null(True).cast(pl.Float64))
    # 月度
    m = dd.with_columns(
        pl.col("date").dt.strftime("%Y-%m").alias("ym")
    ).with_columns(
        (pl.col("ym") != pl.col("ym").shift(1)).alias("_new")
    )
    month_state = m.filter(pl.col("_new")).select(["ym", "state_prev"])
    m = m.join(month_state, on="ym", how="left").with_columns(
        pl.col("state_prev_right").fill_null("flat").alias("_ms")
    )
    out["monthly_not_down"] = _eval_weights(m, (pl.col("_ms") != "down").cast(pl.Float64))
    out["monthly_only_up"] = _eval_weights(m, (pl.col("_ms") == "up").cast(pl.Float64))
    return out


def strategy_stability(d: pl.DataFrame) -> dict:
    """confirm10 的收益来自哪几天？分段是否都成立？"""
    dd = d.filter(pl.col("ew_ret").is_not_null()).select(["date", "ew_ret", "state_prev", "state_age"])
    dd = dd.with_columns(pl.col("state_age").shift(1).alias("len_prev"))
    in_mkt = ((pl.col("state_prev") != "down") | (pl.col("len_prev") < 10)).fill_null(True)
    dd = dd.with_columns(in_mkt.cast(pl.Float64).alias("w"))
    out = {
        "mean_ret_in_pct": round(float(dd.filter(pl.col("w") > 0)["ew_ret"].mean()) * 100, 4),
        "mean_ret_flat_pct": round(float(dd.filter(pl.col("w") == 0)["ew_ret"].mean()) * 100, 4),
        "n_flat_days": int(dd.filter(pl.col("w") == 0).height),
        "flat_days_by_year": {
            str(r["y"]): int(r["n"])
            for r in dd.filter(pl.col("w") == 0)
            .with_columns(pl.col("date").dt.year().alias("y"))
            .group_by("y")
            .agg(pl.len().alias("n"))
            .sort("y")
            .to_dicts()
        },
    }
    halves = {
        "2016_2021H1": ("2016-01-01", "2021-06-30"),
        "2021H2_2026": ("2021-07-01", "2026-12-31"),
    }
    out["halves"] = {}
    for name, (lo, hi) in halves.items():
        sub = dd.filter(pl.col("date").is_between(pl.lit(lo).str.to_date(), pl.lit(hi).str.to_date()))
        out["halves"][name] = {
            "always_hold": _eval_weights(sub, pl.lit(1.0)),
            "confirm10": _eval_weights(sub, sub["w"]),
        }
    return out


def placebo_blocks(d: pl.DataFrame, iters: int = 400) -> dict:
    """安慰剂：把 confirm10 的空仓段（长度不变）随机放到别的时间上。

    检验的是"这 395 天特别差"还是"任何 395 天都差不多"。若随机放置也能拿到
    同样的 CAGR 改善，说明规则只是暴露度下降的副产品，不是择时。
    """
    import random

    dd = d.filter(pl.col("ew_ret").is_not_null()).select(["date", "ew_ret", "state_prev", "state_age"])
    dd = dd.with_columns(pl.col("state_age").shift(1).alias("len_prev"))
    w = ((pl.col("state_prev") != "down") | (pl.col("len_prev") < 10)).fill_null(True)
    dd = dd.with_columns(w.cast(pl.Float64).alias("w"))
    rets = dd["ew_ret"].to_list()
    wv = [int(x) for x in dd["w"].to_list()]
    n = len(rets)

    # 实际空仓段的长度
    blocks, i = [], 0
    while i < n:
        if wv[i] == 0:
            j = i
            while j < n and wv[j] == 0:
                j += 1
            blocks.append(j - i)
            i = j
        else:
            i += 1

    def perf(flat_mask: list[bool]) -> tuple[float, float]:
        eq, peak, mdd = 1.0, 1.0, 0.0
        prev = 0
        for k in range(n):
            cur = 0 if flat_mask[k] else 1
            eq *= 1.0 + cur * rets[k] - abs(cur - prev) * COST
            prev = cur
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        cagr = (eq ** (244.0 / n) - 1) * 100 if eq > 0 else -100
        return cagr, mdd * 100

    base_cagr, base_mdd = perf([x == 0 for x in wv])
    rnd = random.Random(11)
    cagrs, mdds = [], []
    total = sum(blocks)
    for _ in range(iters):
        mask = [False] * n
        cnt, tries = 0, 0
        while cnt < total and tries < 4000:
            tries += 1
            L = rnd.choice(blocks)
            s = rnd.randrange(0, n - L)
            if not any(mask[s : s + L]):
                for k in range(s, s + L):
                    mask[k] = True
                cnt += L
        c, m = perf(mask)
        cagrs.append(c)
        mdds.append(m)
    cagrs.sort()
    better = sum(1 for c in cagrs if c >= base_cagr)
    return {
        "actual_cagr_pct": round(base_cagr, 2),
        "actual_mdd_pct": round(base_mdd, 2),
        "n_flat_days": total,
        "blocks": blocks,
        "placebo_cagr_med_pct": round(cagrs[len(cagrs) // 2], 2),
        "placebo_cagr_p90_pct": round(cagrs[int(0.9 * iters)], 2),
        "p_value_cagr": round(better / iters, 3),
        "placebo_mdd_med_pct": round(sorted(mdds)[len(mdds) // 2], 2),
    }


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    res: dict = {}

    df = load_panel()
    print(f"[panel] rows={df.height:,} symbols={df['symbol'].n_unique()} "
          f"{df['date'].min()}..{df['date'].max()}")

    lax = add_state(build_index(df, strict_gap=False))
    strict = add_state(build_index(df, strict_gap=True))

    if which in ("all", "a1"):
        res["A1_calendar_gap"] = audit_calendar_gap(df)
        print("\n[A1] 等权指数口径")
        for k, v in res["A1_calendar_gap"].items():
            print(f"   {k:16s} 日波动 {v['ew_daily_sd_pct']:.3f}%  2026 {v['y2026_ew_pct']:+.2f}%  "
                  f"down/flat/up {v['n_down']}/{v['n_flat']}/{v['n_up']}  last={v['last_state']}")

    if which in ("all", "a2"):
        res["A2_artifact"] = audit_artifact_overwrite()
        print(f"\n[A2] 产物 state_stats = {res['A2_artifact']['state_stats_in_json']!r} "
              f"({res['A2_artifact']['type']}) => {res['A2_artifact']['verdict']}")

    if which in ("all", "a3"):
        res["A3_spells"] = spell_stats(strict, "down")
        s = res["A3_spells"]
        print(f"\n[A3] down 段数 {s['n_spells']}（完成 {s['n_completed']}）"
              f" 中位 {s['len_med']} 均值 {s['len_mean']} p25-p75 {s['len_p25_p75']} 最长 {s['len_max']}")
        print(f"     当前段 {s['current_spell']}；原口径 state_len 中位(按日加权)="
              f"{s['naive_median_state_len_over_days']}")
        for k, v in s["conditional_remaining"].items():
            if v.get("n"):
                print(f"     {k}: n={v['n']} 次日仍下行 {v.get('p_still_down_next_day')}% "
                      f"活到28天 {v.get('p_lasts_to_28')}% 剩余中位 {v.get('med_remaining')} 均值 {v.get('mean_remaining')}")

    if which in ("all", "a4"):
        res["A4_neff"] = neff_table(strict, 20)
        print("\n[A4] 状态×年 f20（重叠 vs 非重叠）")
        for y, rec in res["A4_neff"].items():
            parts = []
            for st in ("down", "flat", "up"):
                r = rec.get(st)
                if not r:
                    continue
                parts.append(
                    f"{st}:{r['med_all_pct']:+.1f}%/{r['pos_all_pct']:.0f}%"
                    f"(n{r['n_overlap']}→nov{r['n_nonoverlap']},CI{r['med_ci95_pct']})"
                )
            print(f"   {y:>4s}  " + "  ".join(parts))

    if which in ("all", "a5"):
        res["A5_threshold"] = threshold_sensitivity(strict)
        print("\n[A5] ret60 阈值敏感性（2026）")
        for k, v in res["A5_threshold"].items():
            d26, f26 = v.get("down"), v.get("flat")
            print(f"   {k:12s} n_down={v['n_down_2026']:3d} n_flat={v['n_flat_2026']:3d} "
                  f"down={d26} flat={f26}  last={v['last_state']}")

    if which in ("all", "a6"):
        frame = build_stock_frame(df, strict)
        res["A6_dip_within_date"] = dip_test_within_date(frame)
        print("\n[A6] 逢跌买：同日横截面配对（dip − nodip）")
        for k, v in res["A6_dip_within_date"].items():
            print(f"   {k:12s} n_dates={v['n_dates']:4d} 差 {v['mean_diff_bp']:+7.1f}bp "
                  f"t={v['t']:+5.2f} 中位 {v['med_diff_bp']:+7.1f}bp 胜率 {v['win_pct']:.1f}%")
        res["A6b_dip_decile"] = dip_decile(frame)
        print("\n[A6b] 当日涨跌幅十分位 → fwd20（同日横截面，等权日期）")
        for k, v in res["A6b_dip_decile"].items():
            print(f"   d{k:>2s} n_dates={v['n_dates']:4d}  fwd20 {v['mean_fwd20_bp']:+8.1f}bp")
        res["A7_tail_axis"] = tail_by_axis(frame)
        ta = res["A7_tail_axis"]
        print("\n[A7] 2026 左尾 P(fwd20<-20%)：状态轴 vs 构成轴")
        for k in ("down", "flat", "up"):
            r = ta["by_state_2026"].get(k)
            if r:
                print(f"   状态 {k:5s} n={r['n']:6d}  P(<-20%) {r['p_lt_-20pct']:5.1f}%  中位 {r['med_pct']:+.2f}%")
        for k, r in ta["by_turnover_q_2026"].items():
            print(f"   换手Q{k} n={r['n']:6d}  P(<-20%) {r['p_lt_-20pct']:5.1f}%  中位 {r['med_pct']:+.2f}%")
        for k, r in ta["by_mcap_q_2026"].items():
            print(f"   市值Q{k} n={r['n']:6d}  P(<-20%) {r['p_lt_-20pct']:5.1f}%  中位 {r['med_pct']:+.2f}%")
        g = ta["grid_turnover_x_mcap_2026"]
        vals = [(k, v["p_lt_-20pct"]) for k, v in g.items() if v["n"] >= 200]
        if vals:
            vals.sort(key=lambda x: x[1])
            print(f"   25 格极值：最低 {vals[0][0]} {vals[0][1]}%  …  最高 {vals[-1][0]} {vals[-1][1]}%  "
                  f"({vals[-1][1]/max(vals[0][1],0.1):.1f}×)")

    if which in ("all", "a8"):
        res["A8_strategy"] = strategy_level(strict)
        print("\n[A8] 策略层（等权指数，20bp 单边，按 t-1 状态）")
        for k, v in res["A8_strategy"].items():
            print(f"   {k:24s} CAGR {v['cagr_pct']:+6.2f}%  MDD {v['max_dd_pct']:7.2f}%  "
                  f"Sharpe {v['sharpe']}  暴露 {v['exposure_pct']:.0f}%  切换 {v['n_switches']}")
        res["A8b_stability"] = strategy_stability(strict)
        s = res["A8b_stability"]
        print(f"\n[A8b] confirm10 空仓日 n={s['n_flat_days']}  空仓日均 {s['mean_ret_flat_pct']:+.4f}%  "
              f"在场日均 {s['mean_ret_in_pct']:+.4f}%")
        print(f"      空仓日按年 {s['flat_days_by_year']}")
        for h, v in s["halves"].items():
            print(f"      {h}: always {v['always_hold']['cagr_pct']:+.2f}%/MDD{v['always_hold']['max_dd_pct']:.1f} "
                  f"vs confirm10 {v['confirm10']['cagr_pct']:+.2f}%/MDD{v['confirm10']['max_dd_pct']:.1f}")
        res["A9_placebo"] = placebo_blocks(strict)
        p = res["A9_placebo"]
        print(f"\n[A9] 安慰剂（空仓段长度不变、位置随机 {p['n_flat_days']} 天，段长 {p['blocks']}）")
        print(f"      实际 CAGR {p['actual_cagr_pct']:+.2f}% / MDD {p['actual_mdd_pct']:.2f}%  "
              f"随机放置 中位 {p['placebo_cagr_med_pct']:+.2f}% p90 {p['placebo_cagr_p90_pct']:+.2f}%  "
              f"p={p['p_value_cagr']}")

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n[out] {OUT}")


if __name__ == "__main__":
    main()
