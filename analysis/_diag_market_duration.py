#!/usr/bin/env python3
"""_diag_market_duration.py — 用本地日K检验「久期定价」叙事，并复核结构优势页面的账本

## 被检验的外来叙事（2026-11，非本项目结论）

所有参与方只在做一个选择：久期。
  长钱（险资/社保）只买高股息 → 「高股息有底」
  短钱（量化/游资）只买动量  → 「主题有顶」
  中间地带的主动公募从 5 万亿缩到 3.4-4.06 万亿，按季度-年度基本面定价的力量消失
  ⇒ 无股息、无动量的中小盘被两边同时抛弃（Q1_micro −35.5%、中位 −25.1%、16.4% 正收益）
  ⇒ 横截面离散度 3.02%、成交额集中度 65.5%「双双历史最高」

## 六个判据，每个都写明「什么条件下这条叙事会红」

D1 市场隐含久期 h_w = Σ流通市值 / Σ成交额（天）
   叙事要求：2026 越出 2016-2025 年度区间上界（长钱真的把平均持有期拉长了）
   会红：2026 落在历史区间内 ⇒ 「长钱来了」只是规模叙事，没改变换手速度

D2 中间塌陷：市值 Q × 换手 Q 的 25 格 fwd20 超额
   叙事要求：中间 9 格（Q2-Q4 × Q2-Q4）的 2026 塌陷显著大于边缘 16 格
   会红：中间与边缘的塌陷差值落在历史分布内 ⇒ 是 β/风格，不是「定价者久期」

D3 分布形状：久期分组 fwd20 的 p5 / p50 / p95
   叙事要求：p5 随久期单调（长钱有底），p95 随久期反向单调（短钱有顶）
   会红：下尾不单调 / 各组下尾差不多 ⇒ 「有底」是均值错觉，不是分布事实

D4 动量崩溃：12-1 动量多空月度收益
   叙事要求：2026 出现历史级崩溃（7 月量化多头 −16.94% 的可测形式）
   会红：2026 在历史分布中间 ⇒ 「去拥挤」是普通轮动，不是结构断裂

D5 载体迁移：ETF 成交额 / 股票成交额
   叙事要求：2026 创新高（「所有人退到选篮子」）
   会红：没越界 ⇒ 载体迁移是局部，不是全体

D6 离散度与集中度：横截面日收益 std、成交额 CR20
   叙事要求：双双创历史新高（3.02% / 65.5%）
   会红：本地口径算不出越界 ⇒ 外来的两个「历史最高」无法复核

## 口径与陷阱（沿用项目纪律）

- 流通市值由 `amount / (turnover_rate/100)` 反推，不用股本表（避开「股 vs 万股」两次踩过的坑）。
  校验：2026-11-19 工行 2.44 万亿、茅台 1.81 万亿、全市场成交 1.31 万亿。
- fwd20 用复权 `close`，且要求日历相邻（date 差 24-45 自然日），否则跨停牌假收益。
- 月度信号日（每月最后一个交易日）⇒ 每年 12 个观测，避免 20 日重叠把 t 值放大 sqrt(20) 倍。
- 2026 只有 ~10 个有效月度观测 ⇒ n_eff < 30：方向可读、量级可读、结论不可判定（项目硬纪律）。
- kline_daily_enriched 是「按今天在册名单回溯填充」⇒ 100% 生存偏差，2026 的极端亏损被系统性低估。
  这不影响格间相对比较（各格同一批票），但会让「最差那一格」偏乐观。

输出：analysis/_diag_market_duration_result.json + _findings.md
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kline_daily_enriched"
ETF = ROOT / "data" / "backfill_20261117" / "etf_daily_full.parquet"
OUT_JSON = Path(__file__).with_name("_diag_market_duration_result.json")
OUT_MD = Path(__file__).with_name("_diag_market_duration_findings.md")

START = "2016-01-01"
COLS = ["symbol", "date", "close", "amount", "turnover_rate"]


def partition_files() -> list[str]:
    files = []
    for e in os.scandir(DATA):
        if not e.is_dir() or not e.name.startswith("date="):
            continue
        if e.name[5:] >= START:
            files.append(str(Path(e.path) / "part.parquet"))
    files.sort()
    return files


def load() -> pl.DataFrame:
    files = partition_files()
    print(f"[load] {len(files)} partitions from {START}")
    df = pl.read_parquet(files, columns=COLS)
    df = df.filter(
        (pl.col("close") > 0)
        & (pl.col("amount") > 0)
        & (pl.col("turnover_rate") > 0)
    )
    # 流通市值（元）由成交额/换手率反推；h = 换手一遍所需天数
    df = df.with_columns(
        [
            (pl.col("amount") / (pl.col("turnover_rate") / 100)).alias("fmcap"),
            (100.0 / pl.col("turnover_rate")).alias("h_days"),
            (pl.col("date").dt.year()).alias("year"),
        ]
    )
    print(f"[load] rows={df.height:,}  symbols={df['symbol'].n_unique():,}")
    return df.sort(["symbol", "date"])


def add_fwd(df: pl.DataFrame, n: int = 20) -> pl.DataFrame:
    df = df.with_columns(
        [
            pl.col("close").shift(-n).over("symbol").alias("_c_fwd"),
            pl.col("date").shift(-n).over("symbol").alias("_d_fwd"),
        ]
    )
    df = df.with_columns((pl.col("_d_fwd") - pl.col("date")).dt.total_days().alias("_gap"))
    df = df.with_columns(
        pl.when(pl.col("_gap").is_between(n + 4, n + 25))
        .then(pl.col("_c_fwd") / pl.col("close") - 1.0)
        .otherwise(None)
        .alias(f"fwd{n}")
    )
    return df.drop(["_c_fwd", "_d_fwd", "_gap"])


# ---------------- D1 市场隐含久期 ----------------
def d1_duration(df: pl.DataFrame) -> dict:
    """三个口径并列，互为稳健性检查。

    h_med  : 横截面中位持有期（典型股票），不受极端值影响 —— 主指标
    h_wmed : 成交额加权中位持有期（「钱」的典型持有期）—— 主指标
    h_sum  : Σ流通市值/Σ成交额 —— 仅作参照。逐股 fmcap=amount/turnover 在
             turnover→0 时爆炸，被僵尸股污染，2016 年算出的 180 天不可信（反例）。
    """
    d = df.select(["date", "h_days", "amount"]).sort(["date", "h_days"])
    d = d.with_columns(
        [
            pl.col("amount").cum_sum().over("date").alias("ca"),
            pl.col("amount").sum().over("date").alias("ta"),
        ]
    ).with_columns((pl.col("ca") / pl.col("ta")).alias("cw"))
    wmed = (
        d.filter(pl.col("cw") >= 0.5)
        .group_by("date")
        .agg(pl.col("h_days").min().alias("h_wmed"))
        .sort("date")
    )
    daily = (
        df.group_by("date")
        .agg(
            [
                pl.col("fmcap").sum().alias("mcap_sum"),
                pl.col("amount").sum().alias("amt_sum"),
                pl.col("h_days").median().alias("h_med"),
                pl.len().alias("n"),
            ]
        )
        .with_columns((pl.col("mcap_sum") / pl.col("amt_sum")).alias("h_sum"))
        .sort("date")
        .join(wmed, on="date", how="left")
        .with_columns(pl.col("date").dt.year().alias("year"))
    )
    yearly = (
        daily.group_by("year")
        .agg(
            [
                pl.col("h_med").mean().alias("h_med"),
                pl.col("h_wmed").mean().alias("h_wmed"),
                pl.col("h_sum").mean().alias("h_sum"),
                pl.col("amt_sum").mean().alias("amt_mean"),
                pl.len().alias("n_days"),
            ]
        )
        .sort("year")
    )
    base = yearly.filter(pl.col("year") <= 2025)
    y26 = yearly.filter(pl.col("year") == 2026)
    out = {}
    for k in ("h_med", "h_wmed", "h_sum"):
        lo, hi = float(base[k].min()), float(base[k].max())
        v26 = float(y26[k][0]) if y26.height else float("nan")
        out[k] = {
            "hist_band": [lo, hi],
            "y2026": v26,
            "verdict": (
                "越界(上界外)" if v26 > hi else ("越界(下界外)" if v26 < lo else "落在历史区间内")
            ),
            "pct_of_hist_years_below": (
                round(sum(1 for x in base[k].to_list() if x < v26) / base.height * 100, 1)
                if y26.height and base.height
                else None
            ),
        }
    return {
        "yearly": yearly.to_dicts(),
        "verdicts": out,
        "direction": "2026 持有期更长（换手更慢）" if out["h_wmed"]["y2026"] > out["h_wmed"]["hist_band"][1] else (
            "2026 持有期更短（换手更快）" if out["h_wmed"]["y2026"] < out["h_wmed"]["hist_band"][0] else "2026 在历史区间内"
        ),
        "daily": daily.select(["date", "h_med", "h_wmed", "h_sum", "amt_sum"]).to_dicts(),
    }


# ---------------- D2 中间塌陷 ----------------
def _day_groups(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    return df.with_columns(
        [
            ((pl.col("fmcap").rank(method="ordinal").over("date") - 1)
             / pl.len().over("date") * 5).floor().clip(0, 4).cast(pl.Int8).alias("q_mcap"),
            ((pl.col("turnover_rate").rank(method="ordinal").over("date") - 1)
             / pl.len().over("date") * 5).floor().clip(0, 4).cast(pl.Int8).alias("q_to"),
        ]
    )


def d2_hollow(df: pl.DataFrame) -> dict:
    m = (
        df.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
        .group_by("ym")
        .agg(pl.col("date").max().alias("sig_date"))
    )
    sig = df.join(m.select(pl.col("sig_date").alias("date")), on="date", how="semi")
    sig = sig.filter(pl.col("fwd20").is_not_null())
    sig = _day_groups(sig, [])
    mkt = sig.group_by("date").agg(pl.col("fwd20").mean().alias("mkt")).sort("date")
    sig = sig.join(mkt, on="date")
    sig = sig.with_columns((pl.col("fwd20") - pl.col("mkt")).alias("ex"))
    cell = (
        sig.group_by(["date", "q_mcap", "q_to"])
        .agg([pl.col("ex").mean().alias("ex"), pl.len().alias("n")])
        .with_columns(pl.col("date").dt.year().alias("year"))
    )
    # 中间 9 格 vs 边缘 16 格
    mid = (pl.col("q_mcap").is_between(1, 3)) & (pl.col("q_to").is_between(1, 3))
    per_date = cell.group_by("date").agg(
        [
            pl.col("ex").filter(mid).mean().alias("mid_ex"),
            pl.col("ex").filter(~mid).mean().alias("edge_ex"),
        ]
    ).sort("date")
    per_date = per_date.with_columns(
        [
            (pl.col("mid_ex") - pl.col("edge_ex")).alias("gap_mid_edge"),
            pl.col("date").dt.year().alias("year"),
        ]
    )
    yearly = per_date.group_by("year").agg(
        [
            (pl.col("mid_ex").mean() * 1e4).alias("mid_bp"),
            (pl.col("edge_ex").mean() * 1e4).alias("edge_bp"),
            (pl.col("gap_mid_edge").mean() * 1e4).alias("gap_bp"),
            (pl.col("gap_mid_edge").std() * 1e4).alias("gap_sd_within"),
            pl.len().alias("n_months"),
        ]
    ).sort("year")
    hist = yearly.filter(pl.col("year") <= 2025)
    y26 = yearly.filter(pl.col("year") == 2026)
    gap_hist = float(np_mean(hist["gap_bp"].to_list()))
    gap_hist_sd = float(np_sd(hist["gap_bp"].to_list()))
    gap26 = float(y26["gap_bp"][0]) if y26.height else float("nan")
    # 2026 相对历史分布的位置（z）
    z26 = (gap26 - gap_hist) / gap_hist_sd if gap_hist_sd > 0 else float("nan")
    # 25 格明细：2026 与 2017-2025 均值
    grid_hist = (
        cell.filter(pl.col("year") <= 2025)
        .group_by(["q_mcap", "q_to"])
        .agg((pl.col("ex").mean() * 1e4).alias("bp"))
    )
    grid_26 = (
        cell.filter(pl.col("year") == 2026)
        .group_by(["q_mcap", "q_to"])
        .agg([(pl.col("ex").mean() * 1e4).alias("bp"), pl.len().alias("n_months")])
    )
    joined = grid_hist.join(grid_26, on=["q_mcap", "q_to"], how="full", coalesce=True)
    joined = joined.rename({"bp": "bp_hist", "bp_right": "bp_2026"})
    if "bp_2026" not in joined.columns:
        joined = joined.with_columns(pl.lit(None).alias("bp_2026"))
    joined = joined.with_columns((pl.col("bp_2026") - pl.col("bp_hist")).alias("delta"))
    order = joined.sort(["q_to", "q_mcap"])
    return {
        "yearly": yearly.to_dicts(),
        "gap_hist_mean": gap_hist,
        "gap_hist_sd": gap_hist_sd,
        "gap_2026": gap26,
        "z_2026": z26,
        "grid": order.select(
            ["q_to", "q_mcap", "bp_hist", "bp_2026", "delta", "n_months"]
        ).to_dicts(),
    }


def np_mean(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def np_sd(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


# ---------------- D3 分布形状 ----------------
def d3_shape(df: pl.DataFrame) -> dict:
    m = (
        df.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
        .group_by("ym")
        .agg(pl.col("date").max().alias("sig_date"))
    )
    sig = df.join(m.select(pl.col("sig_date").alias("date")), on="date", how="semi")
    sig = sig.filter(pl.col("fwd20").is_not_null())
    sig = sig.with_columns(
        [
            ((pl.col("h_days").rank(method="ordinal").over("date") - 1)
             / pl.len().over("date") * 5).floor().clip(0, 4).cast(pl.Int8).alias("q_h"),
            ((pl.col("fmcap").rank(method="ordinal").over("date") - 1)
             / pl.len().over("date") * 5).floor().clip(0, 4).cast(pl.Int8).alias("q_mcap"),
        ]
    )
    out = {}
    for label, expr in [
        ("2017_2025", (pl.col("date").dt.year() <= 2025)),
        ("2026", (pl.col("date").dt.year() == 2026)),
    ]:
        sub = sig.filter(expr)
        rows = []
        for q in range(5):
            cellsub = sub.filter(pl.col("q_h") == q)
            s = cellsub["fwd20"].to_list()
            s = [x for x in s if x is not None]
            s.sort()
            n = len(s)
            if n < 50:
                continue

            def pc(p: float) -> float:
                return s[min(n - 1, max(0, int(round(p * (n - 1)))))]

            rows.append(
                {
                    "q": q + 1,
                    "n": n,
                    "p5": pc(0.05) * 100,
                    "p25": pc(0.25) * 100,
                    "p50": pc(0.50) * 100,
                    "p75": pc(0.75) * 100,
                    "p95": pc(0.95) * 100,
                    "mean": np_mean(s) * 100,
                    "p_lt_10": sum(1 for x in s if x < -0.10) / n * 100,
                    "p_lt_20": sum(1 for x in s if x < -0.20) / n * 100,
                    "h_median": float(cellsub["h_days"].median() or 0),
                    "fmcap_median_yi": float(cellsub["fmcap"].median() or 0) / 1e8,
                }
            )
        out[label] = rows
    # 正交检验：控制市值后久期梯度是否还在（P(<−20%)）。若只剩市值梯度 ⇒ 久期是小盘代理。
    cross = {}
    for label, expr in [
        ("2017_2025", (pl.col("date").dt.year() <= 2025)),
        ("2026", (pl.col("date").dt.year() == 2026)),
    ]:
        sub = sig.filter(expr)
        g = (
            sub.group_by(["q_mcap", "q_h"])
            .agg(
                [
                    ((pl.col("fwd20") < -0.20).mean() * 100).alias("p20"),
                    ((pl.col("fwd20") > 0.20).mean() * 100).alias("p20_up"),
                    pl.len().alias("n"),
                ]
            )
            .sort(["q_mcap", "q_h"])
        )
        cross[label] = g.to_dicts()
    return {"dist": out, "cross": cross}


# ---------------- D4 动量崩溃 ----------------
def d4_momentum(df: pl.DataFrame) -> dict:
    # 关键：动量必须在日频全表上 shift，再筛月度信号日。
    # 若先筛信号日（每票只剩 ~130 行）再 shift(250) → 全 null，静默得出一张空表。
    d = df.select(["symbol", "date", "close", "fwd20"]).sort(["symbol", "date"])
    d = d.with_columns(
        [
            pl.col("close").shift(20).over("symbol").alias("c_20"),
            pl.col("close").shift(250).over("symbol").alias("c_250"),
        ]
    ).with_columns((pl.col("c_20") / pl.col("c_250") - 1.0).alias("mom12_1"))
    m = (
        d.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
        .group_by("ym")
        .agg(pl.col("date").max().alias("sig_date"))
    )
    sig = d.join(m.select(pl.col("sig_date").alias("date")), on="date", how="semi")
    sig = sig.filter(pl.col("mom12_1").is_not_null() & pl.col("fwd20").is_not_null())
    sig = sig.with_columns(
        ((pl.col("mom12_1").rank(method="ordinal").over("date") - 1)
         / pl.len().over("date")).alias("r")
    )
    sig = sig.with_columns(
        pl.when(pl.col("r") >= 0.9)
        .then(pl.lit("win"))
        .when(pl.col("r") <= 0.1)
        .then(pl.lit("lose"))
        .otherwise(pl.lit("mid"))
        .alias("leg")
    )
    legs = (
        sig.filter(pl.col("leg") != "mid")
        .group_by(["date", "leg"])
        .agg(pl.col("fwd20").mean().alias("r"))
    )
    w = legs.filter(pl.col("leg") == "win").select(["date", pl.col("r").alias("r_win")])
    l = legs.filter(pl.col("leg") == "lose").select(["date", pl.col("r").alias("r_lose")])
    mm = w.join(l, on="date").sort("date")
    mm = mm.with_columns(
        [
            (pl.col("r_win") - pl.col("r_lose")).alias("ls"),
            pl.col("date").dt.year().alias("year"),
            (pl.col("r_win") * 100).alias("win_bp"),
            (pl.col("r_lose") * 100).alias("lose_bp"),
        ]
    )
    mm = mm.with_columns((pl.col("ls") * 100).alias("ls_bp"))
    yearly = mm.group_by("year").agg(
        [
            pl.col("ls_bp").mean().alias("ls_bp_mean"),
            pl.len().alias("n"),
            pl.col("ls_bp").min().alias("ls_bp_worst"),
        ]
    ).sort("year")
    hist = yearly.filter(pl.col("year") <= 2025)["ls_bp_mean"].to_list()
    y26 = yearly.filter(pl.col("year") == 2026)
    pct_rank = (sum(1 for x in hist if x < float(y26["ls_bp_mean"][0])) / len(hist)) if y26.height else None
    monthly = mm.select(["date", "year", "win_bp", "lose_bp", "ls_bp"]).to_dicts()
    y26_monthly = [r for r in monthly if r["year"] == 2026]
    return {
        "yearly": yearly.to_dicts(),
        "hist_pct_rank_2026": pct_rank,
        "y2026": float(y26["ls_bp_mean"][0]) if y26.height else None,
        "y2026_monthly": [{"date": str(r["date"]), "ls_bp": r["ls_bp"], "win_bp": r["win_bp"], "lose_bp": r["lose_bp"]} for r in y26_monthly],
    }


# ---------------- D5 载体迁移 ----------------
def d5_etf(df: pl.DataFrame) -> dict:
    stock = df.group_by("date").agg((pl.col("amount").sum() / 1e8).alias("stock_yi")).sort("date")
    if not ETF.exists():
        return {"error": "etf parquet missing"}
    etf = (
        pl.read_parquet(ETF, columns=["symbol", "date", "amount"])
        .filter(pl.col("date") >= pl.lit(START).str.to_date())
        .group_by("date")
        .agg((pl.col("amount").sum() / 1e8).alias("etf_yi"))
        .sort("date")
    )
    j = stock.join(etf, on="date", how="inner").with_columns(
        [
            (pl.col("etf_yi") / pl.col("stock_yi") * 100).alias("etf_share_pct"),
            pl.col("date").dt.year().alias("year"),
        ]
    )
    yearly = j.group_by("year").agg(
        [
            pl.col("etf_share_pct").mean().alias("pct_mean"),
            pl.col("etf_share_pct").max().alias("pct_max"),
            pl.len().alias("n"),
        ]
    ).sort("year")
    hist = yearly.filter(pl.col("year") <= 2025)
    y26 = yearly.filter(pl.col("year") == 2026)
    return {
        "yearly": yearly.to_dicts(),
        "hist_max_pct_mean": float(hist["pct_mean"].max()) if hist.height else None,
        "y2026_pct_mean": float(y26["pct_mean"][0]) if y26.height else None,
        "verdict": None,
    }


# ---------------- D6 离散度 / 集中度 ----------------
def d6_dispersion(df: pl.DataFrame) -> dict:
    d1 = df.with_columns(pl.col("close").shift(-1).over("symbol").alias("c1"))
    d1 = d1.with_columns(
        pl.when((pl.col("date").shift(-1).over("symbol") - pl.col("date")).dt.total_days().is_between(1, 12))
        .then(pl.col("c1") / pl.col("close") - 1.0)
        .otherwise(None)
        .alias("ret1")
    ).filter(pl.col("ret1").is_not_null())
    daily = d1.group_by("date").agg(
        [
            (pl.col("ret1").std() * 100).alias("xs_std_pct"),
            pl.len().alias("n"),
        ]
    )
    # 成交额集中度：前 N 名 / 前百分比
    ranked = df.with_columns(
        [
            pl.col("amount").rank(method="ordinal", descending=True).over("date").alias("rk"),
            (pl.col("amount").rank(method="ordinal", descending=True).over("date")
             / pl.len().over("date")).alias("rp"),
        ]
    )
    cr = ranked.group_by("date").agg(
        [
            (pl.col("amount").filter(pl.col("rk") <= 20).sum() / pl.col("amount").sum() * 100).alias("cr20_pct"),
            (pl.col("amount").filter(pl.col("rk") <= 50).sum() / pl.col("amount").sum() * 100).alias("cr50_pct"),
            (pl.col("amount").filter(pl.col("rk") <= 100).sum() / pl.col("amount").sum() * 100).alias("cr100_pct"),
            (pl.col("amount").filter(pl.col("rp") <= 0.05).sum() / pl.col("amount").sum() * 100).alias("top5_pct"),
            (pl.col("amount").filter(pl.col("rp") <= 0.10).sum() / pl.col("amount").sum() * 100).alias("top10_pct"),
        ]
    )
    j = daily.join(cr, on="date").with_columns(pl.col("date").dt.year().alias("year"))
    yearly = j.group_by("year").agg(
        [
            pl.col("xs_std_pct").mean().alias("xs_std_pct_mean"),
            pl.col("xs_std_pct").max().alias("xs_std_pct_max"),
            pl.col("cr20_pct").mean().alias("cr20_mean"),
            pl.col("cr50_pct").mean().alias("cr50_mean"),
            pl.col("cr100_pct").mean().alias("cr100_mean"),
            pl.col("top5_pct").mean().alias("top5_mean"),
            pl.col("top10_pct").mean().alias("top10_mean"),
            pl.len().alias("n"),
        ]
    ).sort("year")
    hist = yearly.filter(pl.col("year") <= 2025)
    y26 = yearly.filter(pl.col("year") == 2026)
    out = {
        "yearly": yearly.to_dicts(),
        "daily_recent": j.sort("date").tail(250).select(["date", "xs_std_pct", "cr20_pct"]).to_dicts(),
    }
    for k in ("xs_std_pct_mean", "cr20_mean", "cr50_mean", "cr100_mean", "top5_mean", "top10_mean"):
        out[f"hist_max_{k}"] = float(hist[k].max()) if hist.height else None
        out[f"y2026_{k}"] = float(y26[k][0]) if y26.height else None
    return out


# ---------------- D7 市值分组的年度收益（核对 Q1_micro vs Q5）----------------
def d7_size_year(df: pl.DataFrame) -> dict:
    """每年等权收益，按年初流通市值分 5 组。

    只保留年内交易日 >= 100 的票：剔除次新股（次年才有市值分位意义），
    同时把「年内退市」的票排除在外 —— 这一侧仍残留生存偏差，2026 的小盘亏损被低估。
    """
    d = df.select(["symbol", "date", "close", "fmcap"]).with_columns(
        pl.col("date").dt.year().alias("year")
    )
    b = d.group_by(["year", "symbol"]).agg(
        [
            pl.col("close").sort_by("date").first().alias("c0"),
            pl.col("close").sort_by("date").last().alias("c1"),
            pl.col("fmcap").sort_by("date").first().alias("m0"),
            pl.len().alias("nd"),
        ]
    ).filter(pl.col("nd") >= 100)
    b = b.with_columns((pl.col("c1") / pl.col("c0") - 1.0).alias("ret"))
    b = b.with_columns(
        ((pl.col("m0").rank(method="ordinal").over("year") - 1)
         / pl.len().over("year") * 5).floor().clip(0, 4).cast(pl.Int8).alias("q")
    )
    g = b.group_by(["year", "q"]).agg(
        [
            (pl.col("ret").mean() * 100).alias("ret_pct"),
            (pl.col("ret").median() * 100).alias("ret_med_pct"),
            ((pl.col("ret") > 0).mean() * 100).alias("win_pct"),
            pl.len().alias("n"),
        ]
    ).sort(["year", "q"])
    rows = {}
    for r in g.to_dicts():
        rows.setdefault(r["year"], {})[f"Q{r['q']+1}"] = r
    return {"by_year": rows, "rows": g.to_dicts()}


def main() -> None:
    df = load()
    print("[D1] duration ...")
    r1 = d1_duration(df)
    print("[D1]", r1["direction"])
    for k, v in r1["verdicts"].items():
        print(f"     {k}: 2026={v['y2026']:.1f}  历史区间={[round(x,1) for x in v['hist_band']]}  {v['verdict']}")
    df = add_fwd(df, 20)
    print("[D2] hollow ...")
    r2 = d2_hollow(df)
    print("[D2] gap2026", round(r2["gap_2026"], 1), "z", round(r2["z_2026"], 2))
    print("[D3] shape ...")
    r3 = d3_shape(df)
    print("[D4] momentum ...")
    r4 = d4_momentum(df)
    print("[D5] etf ...")
    r5 = d5_etf(df)
    print("[D6] dispersion ...")
    r6 = d6_dispersion(df)
    print("[D7] size-year ...")
    r7 = d7_size_year(df)

    res = {
        "generated_at": "2026-11-20",
        "start": START,
        "end": str(df["date"].max()),
        "n_rows": df.height,
        "n_symbols": df["symbol"].n_unique(),
        "D1_duration": {k: v for k, v in r1.items() if k != "daily"},
        "D2_hollow": r2,
        "D3_shape": r3,
        "D4_momentum": r4,
        "D5_etf": r5,
        "D6_dispersion": {k: v for k, v in r6.items() if k != "daily_recent"},
        "D7_size_year": r7,
    }
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"[out] {OUT_JSON}")


if __name__ == "__main__":
    main()
