"""短线雷达观察池 × 「超跌修复 / 连跌自救」：能不能提高含金量。

三件事被拆开分别检验，因为它们不是同一个东西：
  A. 投资手册里 ETF 的「跌破一年高点 35% → 20 日 alpha +347bp」能否迁移到个股；
  B. 「连跌 N 日主力自救」的可观测代理（连跌天数 / 量能 / 跌停 / 止跌确认）有没有超额；
  C. 短线雷达观察池（heat 最高分位 × cold）内部，加一条条件能否改善 fwd20。

口径（与 `industry-etf-asymmetry.md` / `holding-and-exit.md` 对齐）：
  - 信号 T 日收盘可知 → **T+1 收盘进场** → T+h 收盘卖；个股成本双边 12bp（ETF 研究是 6bp）
  - 超额 = 个股 fwd − **同日全市场等权 fwd**
  - t 一律按**自然月聚簇**（fwd20 逐日重叠 20 倍）
  - 剔除长期停牌复牌/重组造成的非涨跌停跳变，要求 [t-250, t+21] 无跳变
  - 多重比较：C 段预先声明 7 组条件 × 3 档 ≈ 21 格，Bonferroni 门槛 |t| > 3.03

用法：python analysis/_diag_stock_dip_repair.py [gradient|event|spell|pool|all]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data/.scratch/stock_dip_panel.parquet"
OUT = ROOT / "analysis/_diag_stock_dip_repair_result.json"

COST = 0.0012
COOL = 60
MIN_AMT = 3e7
T_BONF = 3.03

STATE: dict = {}
BASE_M: float = 0.0   # 全样本按月 EV，用于报「相对基线增量」


# ---------------------------------------------------------------- 基础设施

def load() -> pl.DataFrame:
    df = pl.read_parquet(PANEL)
    # 补 heat 需要的两项：20 日振幅均值、20 日换手中位（面板构建时未存）
    df = df.sort(["symbol", "date"]).with_columns(
        ((pl.col("high") - pl.col("low")) / (pl.col("close").shift(1).over("symbol") + 1e-12))
        .alias("_amp")
    ).with_columns(
        pl.col("_amp").rolling_mean(20, min_samples=10).over("symbol").alias("amp20"),
        pl.col("turnover_rate").rolling_median(20, min_samples=10).over("symbol").alias("turn_med"),
    ).drop("_amp")
    for src, dst in [("amp20", "p_amp"), ("turn_med", "p_turn_med")]:
        df = df.with_columns(
            (pl.col(src).rank("average").over("date") / pl.len().over("date")).alias(dst)
        )
    # 官方 heat 公式，一字不差：(p_amp + p_turn_med + (1-p_scale) + p_amount)/4
    df = df.with_columns(
        ((pl.col("p_amp").fill_null(0) + pl.col("p_turn_med") + (1.0 - pl.col("p_fcap"))
          + pl.col("p_amt")) / 4.0).alias("heat"),
        (pl.col("ret1") >= 0.095).cast(pl.Int8).alias("_lu0"),
    ).with_columns(
        pl.col("_lu0").rolling_sum(20, min_samples=1).over("symbol").alias("lu20")
    ).drop("_lu0")

    jump = (
        (pl.col("ret1").abs() > 0.25)
        & (pl.col("consecutive_limit_ups") == 0)
        & (pl.col("consecutive_limit_downs") == 0)
    ).fill_null(False)
    df = df.with_columns(jump.alias("_jump")).with_columns(
        pl.col("_jump").shift(-21).rolling_sum(22, min_samples=1).over("symbol").alias("_jf"),
        pl.col("_jump").rolling_sum(251, min_samples=1).over("symbol").alias("_jp"),
    ).with_columns(
        (~pl.col("_jump") & (pl.col("_jf") == 0) & (pl.col("_jp") == 0)).alias("clean")
    ).drop("_jump", "_jf", "_jp")
    return df


def base(df: pl.DataFrame) -> pl.DataFrame:
    return df.filter(
        pl.col("clean")
        & (pl.col("amt20") >= MIN_AMT)
        & pl.col("dd52").is_not_null()
        & pl.col("p_fcap").is_not_null()
    )


def summ(d: pl.DataFrame, col: str, label: str, cost: float = COST) -> dict | None:
    x = d.select("date", col).drop_nulls()
    if x.height < 300:
        return None
    v = x[col].to_numpy() - cost
    m = (x.with_columns(pl.col("date").dt.truncate("1mo").alias("ym"))
         .group_by("ym").agg(pl.col(col).mean()))
    mv = m[col].to_numpy() - cost
    t = float(mv.mean() / (mv.std(ddof=1) / np.sqrt(len(mv)))) if len(mv) > 2 else np.nan
    m_ev = float(mv.mean()) * 1e4
    return {
        "label": label, "n": int(len(v)), "months": int(len(mv)),
        "ev_bp": round(float(v.mean()) * 1e4, 1),
        "med_bp": round(float(np.median(v)) * 1e4, 1),
        "p10_bp": round(float(np.percentile(v, 10)) * 1e4, 1),
        "p90_bp": round(float(np.percentile(v, 90)) * 1e4, 1),
        "win": round(float((v > 0).mean()) * 100, 1),
        "m_ev_bp": round(m_ev, 1),
        "d_base": round(m_ev - BASE_M, 1),
        "t_month": round(t, 2),
        "m_pos": f"{int((mv > 0).sum())}/{len(mv)}",
    }


def show(rows: list[dict], title: str) -> None:
    rows = [r for r in rows if r]
    print(f"\n### {title}")
    print(f"{'分层':<28}{'n':>9}{'月':>4}{'逐日EV':>8}{'中位':>8}{'p10':>9}"
          f"{'胜率':>6}{'按月EV':>8}{'Δ基线':>8}{'t':>7}{'正月':>8}")
    for r in rows:
        print(f"{r['label']:<28}{r['n']:>9}{r['months']:>4}{r['ev_bp']:>8.1f}{r['med_bp']:>8.1f}"
              f"{r['p10_bp']:>9.1f}{r['win']:>6.1f}{r['m_ev_bp']:>8.1f}{r['d_base']:>8.1f}"
              f"{r['t_month']:>7.2f}{r['m_pos']:>8}")
    STATE.setdefault("tables", {})[title] = rows


# ---------------------------------------------------------------- A 位置 / 事件

def run_gradient(df: pl.DataFrame) -> None:
    d = base(df).filter(pl.col("exc20").is_not_null()).with_columns(
        pl.when(pl.col("dd52") > -0.03).then(pl.lit("1_近高点 -3~0%"))
        .when(pl.col("dd52") > -0.10).then(pl.lit("2_ -10~-3%"))
        .when(pl.col("dd52") > -0.20).then(pl.lit("3_ -20~-10%"))
        .when(pl.col("dd52") > -0.35).then(pl.lit("4_ -35~-20%"))
        .when(pl.col("dd52") > -0.50).then(pl.lit("5_ -50~-35%"))
        .otherwise(pl.lit("6_ -50%↓")).alias("bk")
    )
    rows = [summ(d.filter(pl.col("bk") == b), "exc20", b) for b in sorted(d["bk"].unique())]
    rows.append(summ(d, "exc20", "全样本基线"))
    show(rows, "A1 个股位置梯度：距一年高点回撤 → 20日超额")

    # 深跌 × 近期动量：超跌修复最合理的可执行版本 = 距高点很远但近期已走平/转强
    deep = d.filter(pl.col("dd52") <= -0.35)
    rows = []
    for lo, hi, tag in [(-9, -0.05, "近20日仍跌>5%"), (-0.05, 0.05, "近20日走平±5%"),
                        (0.05, 0.15, "近20日涨5-15%"), (0.15, 9, "近20日涨>15%")]:
        rows.append(summ(deep.filter((pl.col("ret20") >= lo) & (pl.col("ret20") < hi)),
                         "exc20", f"深跌×{tag}"))
    show(rows, "A1d 深跌(≤-35%) × 近20日动量（超跌修复的可执行版本）")

    yr = (d.filter(pl.col("dd52") <= -0.35)
          .with_columns(pl.col("date").dt.year().alias("y"))
          .group_by("y").agg(pl.col("exc20").mean().alias("v"), pl.len().alias("n")).sort("y"))
    print("\nA1c 深跌逐年超额(bp):", {int(r["y"]): round(r["v"] * 1e4) for r in yr.to_dicts()})
    STATE.setdefault("tables", {})["A1c_year"] = yr.to_dicts()


def run_event(df: pl.DataFrame) -> None:
    d = base(df).sort(["symbol", "date"]).with_columns(
        pl.int_range(pl.len()).over("symbol").alias("_idx"),
        (pl.col("dd52") <= -0.35).alias("hit"),
    ).with_columns(
        pl.col("hit").cast(pl.Int8).diff().over("symbol").fill_null(0).alias("_chg")
    )
    first = d.filter(pl.col("_chg") == 1).with_columns(
        (pl.col("_idx") - pl.col("_idx").shift(1).over("symbol")).alias("_gap")
    ).filter(pl.col("_gap").is_null() | (pl.col("_gap") >= COOL))

    ev = first.filter(pl.col("exc20").is_not_null())
    print(f"\n[A2] 事件 {ev.height}（冷却 {COOL} 日），独立月 "
          f"{ev['date'].dt.truncate('1mo').n_unique()}，标的 {ev['symbol'].n_unique()}")

    rows = [summ(ev, "exc20", "全部事件")]
    for lo, hi, tag in [(0, 0.33, "低波阴跌"), (0.33, 0.66, "中波"), (0.66, 1.01, "高波砸盘")]:
        rows.append(summ(ev.filter((pl.col("p_vol") >= lo) & (pl.col("p_vol") < hi)),
                         "exc20", f"{tag} (n={ev.filter((pl.col('p_vol') >= lo) & (pl.col('p_vol') < hi)).height})"))
    show(rows, "A2 首次跌破-35% × 波动形态（个股，冷却后）")

    by_year = (ev.with_columns(pl.col("date").dt.year().alias("y"))
               .group_by("y").agg(pl.col("exc20").mean().alias("v"), pl.len().alias("n")).sort("y"))
    print("A2b 逐年(bp,n):", {int(r["y"]): (round(r["v"] * 1e4), r["n"]) for r in by_year.to_dicts()})
    STATE.setdefault("tables", {})["A2_year"] = by_year.to_dicts()


# ---------------------------------------------------------------- B 连跌 / 自救

def run_spell(df: pl.DataFrame) -> None:
    d = base(df).filter(pl.col("exc20").is_not_null() & pl.col("exc5").is_not_null())

    rows = []
    for k in range(0, 7):
        rows.append(summ(d.filter(pl.col("consec_down") == k), "exc20", f"连跌{k}日 → 20日"))
    rows.append(summ(d.filter(pl.col("consec_down") >= 7), "exc20", "连跌7日+ → 20日"))
    show(rows, "B1 连跌天数 → 20日超额")

    rows = []
    for k in range(0, 5):
        rows.append(summ(d.filter(pl.col("consec_down") == k), "exc5", f"连跌{k}日 → 5日"))
    rows.append(summ(d.filter(pl.col("consec_down") >= 5), "exc5", "连跌5日+ → 5日"))
    show(rows, "B1b 连跌天数 → 5日超额")

    s = d.filter(pl.col("consec_down") >= 3)
    rows = [summ(s.filter((pl.col("amt_ratio") >= lo) & (pl.col("amt_ratio") < hi)), "exc20", tag)
            for lo, hi, tag in [(0, 0.7, "缩量 <0.7"), (0.7, 1.5, "正常 0.7-1.5"), (1.5, 99, "放量 >1.5")]]
    rows.append(summ(s, "exc20", "连跌≥3 全"))
    show(rows, "B2 连跌≥3 × 当日量比（缩量=主力没走）")

    rows = []
    for k, lab in [(0, "无跌停"), (1, "跌停1天"), (2, "跌停2天")]:
        rows.append(summ(d.filter(pl.col("consecutive_limit_downs") == k), "exc20", lab))
    rows.append(summ(d.filter(pl.col("consecutive_limit_downs") >= 3), "exc20", "跌停3天+"))
    show(rows, "B3 跌停天数 → 20日超额")

    dd = d.sort(["symbol", "date"]).with_columns(
        ((pl.col("ret1") > 0) & (pl.col("consec_down").shift(1).over("symbol") >= 3))
        .fill_null(False).alias("stopfall"))
    ev = dd.filter(pl.col("stopfall"))
    print(f"\n[B4] 止跌确认事件 {ev.height}，独立月 {ev['date'].dt.truncate('1mo').n_unique()}")
    rows = [summ(ev, "exc20", "连跌≥3后首阳 → 20日"), summ(ev, "exc5", "连跌≥3后首阳 → 5日")]
    for lo, hi, tag in [(0, 0.33, "低波"), (0.33, 0.66, "中波"), (0.66, 1.01, "高波")]:
        rows.append(summ(ev.filter((pl.col("p_vol") >= lo) & (pl.col("p_vol") < hi)),
                         "exc20", f"首阳 × {tag}"))
    show(rows, "B4 连跌后首阳（底部确认）")


# ---------------------------------------------------------------- C 观察池

def run_pool(df: pl.DataFrame) -> None:
    """观察池 = heat>=0.75 × cold × 可交易。heat 用 p_amt 代理（缺 amp20/turn_med）。

    cold 用官方口径：20 日内无涨停 且 -20% < ret20 < 15%。
    """
    d = base(df).filter(pl.col("exc20").is_not_null() & pl.col("ret60").is_not_null())
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    hot_q = pl.col("heat") >= 0.75          # 与线上同一定义点（此前误用 p_amt 代理）
    pool = d.filter(cold & (pl.col("ret60") >= -0.10) & hot_q & (pl.col("consecutive_limit_ups") == 0))
    print(f"\n[C] 观察池近似 {pool.height} 行，月 {pool['date'].dt.truncate('1mo').n_unique()}，"
          f"日均 {pool.height/pool['date'].n_unique():.0f} 只")

    rows = [summ(pool, "exc20", "观察池近似"),
            summ(d.filter(hot_q), "exc20", "仅高 heat（≥0.75）"),
            summ(d.filter(pl.col("p_amt") >= 0.75), "exc20", "仅高成交额分位（对照）"),
            summ(d.filter(cold), "exc20", "仅 cold"),
            summ(d, "exc20", "全市场基线")]
    show(rows, "C1 观察池近似：负值来自哪一层")

    # 预先声明的 7 组条件（各 2-3 档），不做暴力搜索
    rows = [summ(pool.filter(pl.col("dd52") <= -0.35), "exc20", "位置: 深跌≤-35%"),
            summ(pool.filter((pl.col("dd52") > -0.35) & (pl.col("dd52") <= -0.15)), "exc20", "位置: -35~-15%"),
            summ(pool.filter(pl.col("dd52") > -0.10), "exc20", "位置: 距高点<10%"),
            summ(pool.filter(pl.col("ret20") < -0.05), "exc20", "动量: 20日跌>5%"),
            summ(pool.filter(pl.col("ret20") >= 0.05), "exc20", "动量: 20日涨>5%"),
            summ(pool.filter(pl.col("p_vol") >= 0.66), "exc20", "波动: 高波"),
            summ(pool.filter(pl.col("p_vol") < 0.33), "exc20", "波动: 低波"),
            summ(pool.filter(pl.col("amt_ratio") > 1.5), "exc20", "量能: 放量>1.5"),
            summ(pool.filter(pl.col("amt_ratio") < 0.7), "exc20", "量能: 缩量<0.7"),
            summ(pool.filter(pl.col("p_fcap") >= 0.66), "exc20", "市值: 大"),
            summ(pool.filter(pl.col("p_fcap") < 0.33), "exc20", "市值: 小"),
            summ(pool.filter(pl.col("consec_down") >= 3), "exc20", "连跌≥3"),
            summ(pool, "exc20", "观察池基线")]
    show(rows, "C2 观察池内单条件（预先声明，Bonferroni |t|>3.03）")

    # 两两叠加（只测机制上讲得通的四组）
    rows = [
        summ(pool.filter((pl.col("dd52") > -0.10) & (pl.col("p_vol") < 0.33)), "exc20", "距高点<10% + 低波"),
        summ(pool.filter((pl.col("dd52") > -0.10) & (pl.col("p_vol") >= 0.66)), "exc20", "距高点<10% + 高波"),
        summ(pool.filter((pl.col("p_vol") < 0.33) & (pl.col("amt_ratio") < 0.7)), "exc20", "低波 + 缩量"),
        summ(pool.filter((pl.col("dd52") > -0.10) & (pl.col("p_fcap") >= 0.66)), "exc20", "距高点<10% + 大市值"),
        summ(pool.filter((pl.col("amt_ratio") < 0.7) & (pl.col("p_fcap") < 0.33)), "exc20", "缩量 + 小市值"),
    ]
    show(rows, "C3 观察池内两条件叠加（注意：heat 要求高换手高振幅，低波在池内近乎空集）")

    # 样本外：前半段选条件，后半段验证
    cut = pl.date(2019, 1, 1)
    for tag, sub in [("2013-2018", pool.filter(pl.col("date") < cut)),
                     ("2019-2026", pool.filter(pl.col("date") >= cut))]:
        rows = [summ(sub, "exc20", f"{tag} 池子基线"),
                summ(sub.filter(pl.col("amt_ratio") < 0.7), "exc20", f"{tag} 缩量<0.7"),
                summ(sub.filter(pl.col("amt_ratio") > 1.5), "exc20", f"{tag} 放量>1.5"),
                summ(sub.filter(pl.col("p_fcap") < 0.33), "exc20", f"{tag} 小市值"),
                summ(sub.filter(pl.col("dd52") > -0.10), "exc20", f"{tag} 距高点<10%"),
                summ(sub.filter(pl.col("dd52") <= -0.35), "exc20", f"{tag} 深跌"),
                summ(sub.filter(pl.col("consec_down") >= 3), "exc20", f"{tag} 连跌≥3")]
        show(rows, f"C4 样本外拆分 {tag}")


def _market_state(df: pl.DataFrame) -> pl.DataFrame:
    """全市场等权指数的状态：60 日线方向 × 市场波动分位（ETF 研究 §检验3 的同构口径）。

    等权指数用每日全市场 ret1 的均值累乘得到（与 exc 的口径同一个市场定义）。
    """
    m = (
        df.filter(pl.col("amt20") >= MIN_AMT)
        .group_by("date")
        .agg(pl.col("ret1").mean().alias("mret"),
             pl.col("ret1").std().alias("mxs"))
        .sort("date")
    )
    m = m.with_columns((1 + pl.col("mret").fill_null(0)).cum_prod().alias("lvl"))
    m = m.with_columns(
        pl.col("lvl").rolling_mean(60, min_samples=40).alias("ma60"),
        pl.col("mxs").rolling_mean(20, min_samples=10).alias("mvol"),
    )
    m = m.with_columns(
        (pl.col("mvol").rank("average") / pl.len()).alias("mvol_pct"),
        (pl.col("lvl") > pl.col("ma60")).fill_null(False).alias("m_up"),
    )
    m = m.with_columns(
        pl.when(pl.col("mvol_pct") >= 0.66).then(pl.lit("高波")).otherwise(pl.lit("低波")).alias("m_vol"),
        pl.when(pl.col("m_up")).then(pl.lit("上行")).otherwise(pl.lit("下行")).alias("m_dir"),
    )
    return m.select("date", "m_dir", "m_vol", "mvol_pct")


def run_regime(df: pl.DataFrame) -> None:
    """用户直觉最可能成立的地方：超跌修复只在市场恐慌时成立（ETF 检验 3 的同构检验）。"""
    d = base(df).filter(pl.col("exc20").is_not_null())
    d = d.join(_market_state(df), on="date", how="left").filter(pl.col("m_dir").is_not_null())

    rows = []
    for md in ["下行", "上行"]:
        for mv in ["高波", "低波"]:
            s = d.filter((pl.col("m_dir") == md) & (pl.col("m_vol") == mv))
            rows.append(summ(s.filter(pl.col("dd52") <= -0.35), "exc20", f"{md}·{mv} × 深跌≤-35%"))
            rows.append(summ(s.filter(pl.col("dd52") > -0.10), "exc20", f"{md}·{mv} × 距高点<10%"))
            rows.append(summ(s, "exc20", f"{md}·{mv} × 该状态全体"))
    show(rows, "D1 市场状态 × 位置（ETF 检验3 的个股版）")

    # 观察池口径下再切一次
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    pool = d.filter(cold & (pl.col("ret60") >= -0.10) & (pl.col("p_amt") >= 0.75)
                    & (pl.col("consecutive_limit_ups") == 0))
    rows = []
    for md in ["下行", "上行"]:
        for mv in ["高波", "低波"]:
            s = pool.filter((pl.col("m_dir") == md) & (pl.col("m_vol") == mv))
            rows.append(summ(s, "exc20", f"池 × {md}·{mv}"))
            rows.append(summ(s.filter(pl.col("dd52") > -0.10), "exc20", f"池 × {md}·{mv} × 距高点<10%"))
    show(rows, "D2 观察池 × 市场状态")

    st = (d.group_by(["m_dir", "m_vol"]).len().sort("m_dir", "m_vol"))
    print("\nD0 状态分布:", st.to_dicts())
    STATE.setdefault("tables", {})["D0_state"] = st.to_dicts()


# ---------------------------------------------------------------- E 排序键对照

def _daily_top(pool: pl.DataFrame, key: pl.Expr, desc: bool) -> dict | None:
    """每日按 key 取前 15 只，等权，算当日这 15 只的 exc20 均值；再按月聚簇。"""
    s = pool.with_columns(key.rank("ordinal", descending=desc).over("date").alias("_rk"))
    s = s.filter(pl.col("_rk") <= 15)
    daily = s.group_by("date").agg(
        pl.col("exc20").mean().alias("v"), pl.len().alias("n")
    ).sort("date")
    if daily.height < 200:
        return None
    m = (daily.with_columns(pl.col("date").dt.truncate("1mo").alias("ym"))
         .group_by("ym").agg(pl.col("v").mean())["v"].to_numpy() - COST)
    return {
        "n_days": int(daily.height), "months": int(len(m)),
        "m_ev_bp": round(float(m.mean()) * 1e4, 1),
        "t": round(float(m.mean() / (m.std(ddof=1) / np.sqrt(len(m)))), 2),
        "pos": f"{int((m > 0).sum())}/{len(m)}",
        "avg_n": round(float(daily["n"].mean()), 1),
        "p10_bp": round(float(np.percentile(m, 10)) * 1e4, 1),
    }


def run_rank(df: pl.DataFrame) -> None:
    """页面每天只显示 15 只 —— 决定「含金量」的不是池子，是排序键。

    池子固定为 heat>=0.75 × cold，只换排序键，看 20 日超额。
    """
    d = base(df).filter(
        pl.col("exc20").is_not_null() & pl.col("ret60").is_not_null()
        & pl.col("heat").is_not_null() & pl.col("p_amp").is_not_null()
    )
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    pool = d.filter(cold & (pl.col("ret60") >= -0.10) & (pl.col("heat") >= 0.75)
                    & (pl.col("consecutive_limit_ups") == 0))
    print(f"\n[E] 排序对照池 {pool.height} 行，{pool['date'].n_unique()} 个交易日，"
          f"日均 {pool.height/pool['date'].n_unique():.0f} 只")

    # 复合键：位置分位 − 波动分位（越大 = 距高点越近且波动越低）
    pool = pool.with_columns(
        (pl.col("dd52").rank("average").over("date") / pl.len().over("date")
         - pl.col("p_vol")).alias("_loc_vol")
    )
    # 伪随机（对照）：必须**每天独立**，否则等于固定一组票
    pool = pool.with_columns(
        ((pl.col("symbol").hash() * 2654435761 + pl.col("date").dt.epoch("d")) % 1000003)
        .alias("_rnd")
    )

    cands = [
        ("heat 降序（线上现状）", pl.col("heat"), True),
        ("│ p_amp 降序（只按振幅）", pl.col("p_amp"), True),
        ("│ p_turn_med 降序（只按换手）", pl.col("p_turn_med"), True),
        ("│ p_fcap 升序（只按小市值）", pl.col("p_fcap"), False),
        ("│ p_amt 降序（只按当日成交额）", pl.col("p_amt"), True),
        ("dd52 降序（距高点最近）", pl.col("dd52"), True),
        ("p_vol 升序（低波优先）", pl.col("p_vol"), False),
        ("位置−波动 复合降序", pl.col("_loc_vol"), True),
        ("amt_ratio 升序（缩量优先）", pl.col("amt_ratio"), False),
        ("连跌天数 降序（越跌越前）", pl.col("consec_down"), True),
        ("随机（对照）", pl.col("_rnd"), True),
    ]
    print(f"{'排序键':<26}{'交易日':>8}{'月':>5}{'日均只':>7}{'20日超额':>9}{'t':>7}{'正月':>9}{'p10':>9}")
    rows = []
    for lab, key, desc in cands:
        r = _daily_top(pool, key, desc)
        if not r:
            print(f"{lab:<26}  样本不足")
            continue
        print(f"{lab:<26}{r['n_days']:>8}{r['months']:>5}{r['avg_n']:>7.1f}"
              f"{r['m_ev_bp']:>9.1f}{r['t']:>7.2f}{r['pos']:>9}{r['p10_bp']:>9.1f}")
        rows.append({"key": lab, **r})
    STATE.setdefault("tables", {})["E_rank"] = rows

    # 样本外：两个排序键只在后半段是否还成立
    cut = pl.date(2019, 1, 1)
    for tag, sub in [("2013-2018", pool.filter(pl.col("date") < cut)),
                     ("2019-2026", pool.filter(pl.col("date") >= cut))]:
        print(f"\n-- {tag}")
        for lab, key, desc in [("heat 降序", pl.col("heat"), True),
                               ("p_fcap 升序(小市值)", pl.col("p_fcap"), False),
                               ("amt_ratio 升序(缩量)", pl.col("amt_ratio"), False),
                               ("位置−波动 复合", pl.col("_loc_vol"), True),
                               ("随机", pl.col("_rnd"), True)]:
            r = _daily_top(sub, key, desc)
            print(f"  {lab:<22}{r['m_ev_bp']:>8.1f}bp  t={r['t']:>5.2f}  {r['pos']}")


def run_reversal(df: pl.DataFrame) -> None:
    """把「超跌」拆成两个不同的东西：距一年高点远（dd52）vs 最近 20 日跌得多（ret20）。

    用户的语感是后者（「连跌多日」），而 ETF 手册讲的是前者（距一年高点 −35%）。
    """
    d = base(df).filter(pl.col("exc20").is_not_null() & pl.col("ret60").is_not_null()
                        & pl.col("heat").is_not_null())
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    pool = d.filter(cold & (pl.col("ret60") >= -0.10) & (pl.col("heat") >= 0.75)
                    & (pl.col("consecutive_limit_ups") == 0))
    cut = pl.date(2019, 1, 1)
    for tag, sub in [("全期", pool), ("2013-2018", pool.filter(pl.col("date") < cut)),
                     ("2019-2026", pool.filter(pl.col("date") >= cut))]:
        base_v = summ(sub, "exc20", f"{tag} 池基线")
        rows = [base_v,
                summ(sub.filter(pl.col("ret20") < -0.10), "exc20", f"{tag} ret20 <−10%"),
                summ(sub.filter((pl.col("ret20") >= -0.10) & (pl.col("ret20") < -0.05)),
                     "exc20", f"{tag} ret20 −10~−5%"),
                summ(sub.filter(pl.col("ret20") > 0.05), "exc20", f"{tag} ret20 >+5%"),
                summ(sub.filter((pl.col("ret20") < -0.05) & (pl.col("amt_ratio") < 0.7)),
                     "exc20", f"{tag} ret20<−5% + 缩量"),
                summ(sub.filter((pl.col("ret20") < -0.05) & (pl.col("p_fcap") < 0.33)),
                     "exc20", f"{tag} ret20<−5% + 小市值")]
        show(rows, f"F 短期反转 vs 长期位置 {tag}")


# ---------------------------------------------------------------- 主入口

def main() -> None:
    global BASE_M
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    df = load()
    b = base(df)
    BASE_M = float((b.filter(pl.col("exc20").is_not_null())
                    .with_columns(pl.col("date").dt.truncate("1mo").alias("ym"))
                    .group_by("ym").agg(pl.col("exc20").mean())["exc20"].mean() - COST)) * 1e4
    print(f"panel {df.shape} clean {df['clean'].sum()} {df['date'].min()}~{df['date'].max()}")
    print(f"全样本按月基线 {BASE_M:.1f}bp（已扣 {COST*1e4:.0f}bp；Δ基线 = 该格 − 这个数）")

    for name, fn in [("gradient", run_gradient), ("event", run_event),
                     ("spell", run_spell), ("pool", run_pool), ("regime", run_regime),
                     ("rank", run_rank), ("reversal", run_reversal)]:
        if which in ("all", name):
            fn(df)
    if which == "all":
        OUT.write_text(json.dumps({**STATE, "base_m_bp": round(BASE_M, 1)},
                                  ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
