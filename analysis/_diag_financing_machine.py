"""融资市的三条可证伪命题。

用户前提「A 股的目标是融资帮助行业发展」+「降息是必要不充分条件」。把它拆成三个能算的命题:

H1 **稀释命题**: 融资注入的净资产增速 > 利润增速 ⇒ 全市场 ROE 长期下滑 ⇒
   每股盈利增长被吃掉 ⇒ 长期持有只能靠估值波动。
   口径: ROE = 归母净利润 / 期初期末平均净资产。**净资产和利润都不受送股影响**,
   所以这是唯一能绕开「送转 vs 融资无法分离」的口径 (basic_eps 本库全 null, 不可用)。

H2 **内生供给命题**: 股价涨 → 融资加速 → 涨幅被新增供给吸收。
   ⇒ 解释了为什么「利好(降息)只能抬估值, 抬完就被融资打回」, 即必要不充分的机制。
   口径: 年 t 指数收益 vs 年 t+1 股本增速, 同期相关。

H3 **行业效率命题**: 融资多的行业, 后来 ROE 是否改善。
   若融资强度与后续 ROE 变化负相关 ⇒ 「融资帮助行业发展」被证伪。
   口径: 行业 Σ净资产增速 → 后续 3 年 ROE 变化。

口径铁律:
- 一律 same-store (t 与 t-1 都有年报), 否则 IPO 会被算成"增长"。
- 不剔除亏损公司 (全市场就是有亏损), 剔除会高估 ROE。
- 净资产用 `total_equity` (含少数股东), 分子用归母 ⇒ 轻微低估, 趋势不受影响。
- 日线只有 2005 年起 ⇒ 市值/股价相关分析只能用 2005+; ROE 可到 1993。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
BF = DATA / "backfill_20261117"
OUT = Path(__file__).resolve().parent / "_diag_financing_machine_result.json"

INDEX = {"000001.SH": "上证", "000300.SH": "沪深300", "399006.SZ": "创业板"}


def load_annual() -> pl.DataFrame:
    inc = (
        pl.read_parquet(BF / "fin_income.parquet",
                        columns=["symbol", "period_end", "net_income_attributable"])
        .filter(pl.col("period_end").dt.month() == 12)
        .with_columns(pl.col("period_end").dt.year().alias("year"))
        .filter(pl.col("net_income_attributable").is_not_null())
        .unique(subset=["symbol", "year"], keep="last")
        .rename({"net_income_attributable": "E"})
    )
    bal = (
        pl.read_parquet(BF / "fin_balance.parquet", columns=["symbol", "period_end", "total_equity"])
        .filter(pl.col("period_end").dt.month() == 12)
        .with_columns(pl.col("period_end").dt.year().alias("year"))
        .filter(pl.col("total_equity").is_not_null())
        .unique(subset=["symbol", "year"], keep="last")
        .rename({"total_equity": "EQ"})
        .select(["symbol", "year", "EQ"])
    )
    sh = (
        pl.read_parquet(BF / "fin_shares.parquet", columns=["symbol", "end_date", "total_shares"])
        .filter(pl.col("total_shares").is_not_null() & (pl.col("total_shares") > 0))
        .with_columns(pl.col("end_date").dt.year().alias("year"))
        .unique(subset=["symbol", "year"], keep="last")
        .select(["symbol", "year", "total_shares"])
    )
    df = inc.join(bal, on=["symbol", "year"], how="inner").join(sh, on=["symbol", "year"], how="inner")
    ind = (
        pl.read_parquet(DATA / "ext_data" / "ext_hy_ths" / "part.parquet")
        .select(["symbol", "所属同花顺行业"])
        .with_columns(pl.col("所属同花顺行业").str.split("-").list.first().alias("ind"))
        .select(["symbol", "ind"])
        .unique(subset=["symbol"])
    )
    return df.join(ind, on="symbol", how="left")


def h1_roe(df: pl.DataFrame) -> dict:
    """全市场 ROE 序列, 全口径 + 同店口径。"""
    tab = df.filter((pl.col("year") >= 1993) & (pl.col("year") <= 2025))
    allp = (
        tab.group_by("year")
        .agg(pl.len().alias("n"), pl.col("E").sum().alias("E"), pl.col("EQ").sum().alias("EQ"))
        .sort("year")
        .with_columns((pl.col("E") / pl.col("EQ") * 100).alias("roe"))
    )
    # 同店: t 与 t-1 都有
    prev = tab.with_columns((pl.col("year") + 1).alias("year")).rename({"E": "E0", "EQ": "EQ0"})
    pair = tab.join(prev, on=["symbol", "year"], how="inner").filter(pl.col("EQ0") > 0)
    same = (
        pair.group_by("year")
        .agg(pl.len().alias("n"),
             pl.col("E").sum().alias("E"), pl.col("E0").sum().alias("E0"),
             pl.col("EQ").sum().alias("EQ"), pl.col("EQ0").sum().alias("EQ0"))
        .sort("year")
        .with_columns([
            (pl.col("E") / ((pl.col("EQ") + pl.col("EQ0")) / 2) * 100).alias("roe"),
            (pl.col("E0") / pl.col("EQ0") * 100).alias("roe_prev"),
            ((pl.col("EQ") / pl.col("EQ0") - 1) * 100).alias("gEQ"),
            ((pl.col("E") / pl.col("E0") - 1) * 100).alias("gE"),
            ((pl.col("EQ") - pl.col("EQ0")).alias("dEQ")),
            ((pl.col("E") - pl.col("E0")).alias("dE")),
        ])
        .sort("year")
    )
    return {"all": allp.to_dicts(), "same_store": same.to_dicts()}


def h1b_fixed_pool(df: pl.DataFrame) -> dict:
    """完全固定成分: 2010 与 2025 都有年报的公司。排除「新股拉低 ROE」这一解释。"""
    # **严格平衡面板**: 2010-2025 每一年都有年报, 缺一年就剔。
    # 首版只要求「2010 与 2025 都有」, 结果 2025 年公司数从 803 跳到 961,
    # Σ净资产凭空多 77% —— 那是成分变化, 不是增长。
    YRS = list(range(2010, 2026))
    cnt = (df.filter(pl.col("year").is_in(YRS))
             .group_by("symbol").agg(pl.col("year").n_unique().alias("k"))
             .filter(pl.col("k") == len(YRS)))
    pool = cnt["symbol"]
    sub = df.filter(pl.col("symbol").is_in(pool))
    tab = (
        sub.group_by("year")
        .agg(pl.len().alias("n"), pl.col("E").sum().alias("E"), pl.col("EQ").sum().alias("EQ"))
        .sort("year")
        .with_columns((pl.col("E") / pl.col("EQ") * 100).alias("roe"))
    )
    return {"n_pool": int(pool.len()), "series": tab.to_dicts()}


def h2_endogenous(df: pl.DataFrame) -> dict:
    """股价涨 → 次年融资加速? 用同店口径的 gEQ 与指数年收益对齐。"""
    prev = df.with_columns((pl.col("year") + 1).alias("year")).rename({"EQ": "EQ0"})
    pair = df.join(prev, on=["symbol", "year"], how="inner").filter(pl.col("EQ0") > 0)
    g = (
        pair.group_by("year")
        .agg(pl.col("EQ").sum().alias("EQ"), pl.col("EQ0").sum().alias("EQ0"))
        .sort("year")
        .with_columns(((pl.col("EQ") / pl.col("EQ0") - 1) * 100).alias("gEQ"))
        .select(["year", "gEQ"])
    )
    # 指数年收益
    idx = pl.read_parquet(BF / "index_daily_full.parquet",
                          columns=["symbol", "date", "close"]).filter(pl.col("symbol").is_in(list(INDEX)))
    yr = (
        idx.with_columns(pl.col("date").dt.year().alias("year"))
        .group_by(["symbol", "year"])
        .agg(pl.col("close").last().alias("c"))
        .sort(["symbol", "year"])
        .with_columns((pl.col("c") / pl.col("c").shift(1) - 1).mul(100).over("symbol").alias("ret"))
        .select(["symbol", "year", "ret"])
        .pivot(index="year", on="symbol", values="ret")
        .sort("year")
        .rename({k: f"ret_{v}" for k, v in INDEX.items()})
    )
    t = g.join(yr, on="year", how="inner").sort("year")
    # t 年指数收益 → t+1 年净资产增速
    t = t.with_columns([pl.col(c).shift(1).alias(f"{c}_prev") for c in t.columns if c.startswith("ret_")])
    t = t.with_columns(pl.col("gEQ").shift(-1).alias("gEQ_next"))
    out = {}
    for c in [c for c in t.columns if c.startswith("ret_") and not c.endswith("_prev")]:
        a = t.filter(pl.col(f"{c}_prev").is_not_null() & pl.col("gEQ_next").is_not_null())
        if a.height >= 8:
            x = a[f"{c}_prev"].to_numpy().astype(float)
            y = a["gEQ_next"].to_numpy().astype(float)
            out[c] = {
                "n": int(a.height),
                "corr": round(float(np.corrcoef(x, y)[0, 1]), 3),
                "slope": round(float(np.polyfit(x, y, 1)[0]), 4),
                "x_hi_mean_gEQ": round(float(y[x >= np.median(x)].mean()), 2),
                "x_lo_mean_gEQ": round(float(y[x < np.median(x)].mean()), 2),
            }
    return {"table": t.to_dicts(), "corr": out}


def h3_industry(df: pl.DataFrame) -> dict:
    """行业: 净资产增速 → 后续 ROE 变化, 以及每元新增净资产的利润增量。"""
    tab = df.filter((pl.col("year") >= 2005) & (pl.col("year") <= 2025) & pl.col("ind").is_not_null())
    prev = tab.with_columns((pl.col("year") + 1).alias("year")).rename({"E": "E0", "EQ": "EQ0"})
    pair = tab.join(prev, on=["symbol", "year"], how="inner").filter(pl.col("EQ0") > 0)
    g = (
        pair.group_by(["ind", "year"])
        .agg(pl.len().alias("n"),
             pl.col("E").sum().alias("E"), pl.col("E0").sum().alias("E0"),
             pl.col("EQ").sum().alias("EQ"), pl.col("EQ0").sum().alias("EQ0"))
        .with_columns([
            (pl.col("E") / ((pl.col("EQ") + pl.col("EQ0")) / 2) * 100).alias("roe"),
            ((pl.col("EQ") / pl.col("EQ0") - 1) * 100).alias("gEQ"),
            ((pl.col("E") - pl.col("E0")) / (pl.col("EQ") - pl.col("EQ0")) * 100).alias("mroi"),
        ])
        .filter((pl.col("n") >= 3) & ((pl.col("EQ") - pl.col("EQ0")).abs() > 0))
    )
    # 后续 3 年 ROE 变化
    g = g.sort(["ind", "year"]).with_columns(
        (pl.col("roe").shift(-3).over("ind") - pl.col("roe")).alias("d_roe_3y"))
    v = g.filter(pl.col("d_roe_3y").is_not_null() & pl.col("gEQ").is_not_null()
                 & pl.col("roe").is_not_null())
    med = v["gEQ"].median()
    hi = v.filter(pl.col("gEQ") >= med)
    lo = v.filter(pl.col("gEQ") < med)
    summary = {
        "n_obs": int(v.height),
        "n_ind": int(v["ind"].n_unique()),
        "split_at_gEQ_pct": round(float(med), 2),
        "high_fin_gEQ": round(float(hi["gEQ"].mean()), 2),
        "high_fin_d_roe_3y": round(float(hi["d_roe_3y"].mean()), 3),
        "low_fin_gEQ": round(float(lo["gEQ"].mean()), 2),
        "low_fin_d_roe_3y": round(float(lo["d_roe_3y"].mean()), 3),
        "corr_gEQ_dROE": round(float(np.corrcoef(v["gEQ"].to_numpy(), v["d_roe_3y"].to_numpy())[0, 1]), 3),
        "mroi_med": round(float(g["mroi"].median()), 2),
        # 控制初始 ROE: 若相关消失 ⇒ 只是均值回归, 不是融资毁灭价值
        "corr_parts": _partial_corr(v),
        "by_initial_roe": _by_initial_roe(v),
        "mroi_mean": round(float(g["mroi"].mean()), 2),
    }
    # 行业长期: 融资强度 vs 平均 ROE
    byind = (
        g.group_by("ind")
        .agg(pl.len().alias("n_yr"),
             pl.col("gEQ").mean().alias("avg_gEQ"),
             pl.col("roe").mean().alias("avg_roe"),
             pl.col("mroi").median().alias("med_mroi"))
        .filter(pl.col("n_yr") >= 8)
        .sort("avg_gEQ", descending=True)
    )
    return {"summary": summary, "by_industry": byind.to_dicts()}


def _rank(a: np.ndarray) -> np.ndarray:
    """秩变换 (scipy 不在 venv, 手写 argsort(argsort))。"""
    o = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), dtype=float)
    r[o] = np.arange(len(a))
    return r


def _partial_corr(v: pl.DataFrame) -> dict:
    """r(gEQ, ΔROE | roe_t) = (r_xy - r_xz*r_yz) / sqrt((1-r_xz²)(1-r_yz²))"""
    x = v["gEQ"].to_numpy().astype(float)
    y = v["d_roe_3y"].to_numpy().astype(float)
    z = v["roe"].to_numpy().astype(float)      # 初始 ROE
    rxy = np.corrcoef(x, y)[0, 1]
    rxz = np.corrcoef(x, z)[0, 1]
    ryz = np.corrcoef(y, z)[0, 1]
    den = np.sqrt(max(1e-12, (1 - rxz ** 2) * (1 - ryz ** 2)))
    return {"r_xy": round(float(rxy), 3), "r_xz": round(float(rxz), 3),
            "r_yz": round(float(ryz), 3), "partial": round(float((rxy - rxz * ryz) / den), 3)}


def _by_initial_roe(v: pl.DataFrame) -> list[dict]:
    """按初始 ROE 三档分层, 每层内再看 gEQ 高低 → ΔROE。均值回归应让三档都同向。"""
    out = []
    qs = v["roe"].quantile(0.33), v["roe"].quantile(0.67)
    for lab, sub in (("低ROE", v.filter(pl.col("roe") <= qs[0])),
                     ("中ROE", v.filter((pl.col("roe") > qs[0]) & (pl.col("roe") <= qs[1]))),
                     ("高ROE", v.filter(pl.col("roe") > qs[1]))):
        if sub.height < 30:
            continue
        m = sub["gEQ"].median()
        hi = sub.filter(pl.col("gEQ") >= m)
        lo = sub.filter(pl.col("gEQ") < m)
        out.append({"bucket": lab, "n": int(sub.height),
                    "init_roe": round(float(sub["roe"].mean()), 2),
                    "hi_gEQ": round(float(hi["gEQ"].mean()), 2),
                    "hi_dROE": round(float(hi["d_roe_3y"].mean()), 3),
                    "lo_gEQ": round(float(lo["gEQ"].mean()), 2),
                    "lo_dROE": round(float(lo["d_roe_3y"].mean()), 3),
                    "gap": round(float(hi["d_roe_3y"].mean() - lo["d_roe_3y"].mean()), 3)})
    return out


def h4_supply_drag(df: pl.DataFrame) -> dict:
    """净外部融资 = ΔEQ − ΣE, 以及「融资扩张是否压制次年指数收益」。"""
    prev = df.with_columns((pl.col("year") + 1).alias("year")).rename({"EQ": "EQ0"})
    pair = df.join(prev, on=["symbol", "year"], how="inner").filter(pl.col("EQ0") > 0)
    g = (
        pair.group_by("year")
        .agg(pl.col("EQ").sum().alias("EQ"), pl.col("EQ0").sum().alias("EQ0"),
             pl.col("E").sum().alias("E"))
        .sort("year")
        .with_columns([((pl.col("EQ") / pl.col("EQ0") - 1) * 100).alias("gEQ"),
                       (pl.col("EQ") - pl.col("EQ0")).alias("dEQ")])
    )
    idx = pl.read_parquet(BF / "index_daily_full.parquet",
                          columns=["symbol", "date", "close"]).filter(pl.col("symbol").is_in(list(INDEX)))
    yr = (
        idx.with_columns(pl.col("date").dt.year().alias("year"))
        .group_by(["symbol", "year"]).agg(pl.col("close").last().alias("c"))
        .sort(["symbol", "year"])
        .with_columns((pl.col("c") / pl.col("c").shift(1) - 1).mul(100).over("symbol").alias("ret"))
        .select(["symbol", "year", "ret"]).pivot(index="year", on="symbol", values="ret")
        .sort("year").rename({k: f"ret_{v}" for k, v in INDEX.items()})
    )
    t = g.join(yr, on="year", how="inner").sort("year")
    t = t.with_columns([pl.col(c).shift(-1).alias(f"{c}_next") for c in t.columns if c.startswith("ret_")])
    out = {"table": t.to_dicts(), "corr": {}}
    for c in [c for c in t.columns if c.endswith("_next")]:
        a = t.filter(pl.col(c).is_not_null() & pl.col("gEQ").is_not_null())
        if a.height < 8:
            continue
        x, y = a["gEQ"].to_numpy().astype(float), a[c].to_numpy().astype(float)
        med = float(np.median(x))
        # 极端值体检: 2006 年 gEQ 最高却撞上 2007 大牛市, 单点能把 Pearson 从负翻正
        keep = a["year"].to_numpy() != 2006
        pear_ex = float(np.corrcoef(x[keep], y[keep])[0, 1]) if keep.sum() >= 6 else np.nan
        out["corr"][c] = {
            "n": int(a.height), "corr": round(float(np.corrcoef(x, y)[0, 1]), 3),
            "spearman": round(float(np.corrcoef(_rank(x), _rank(y))[0, 1]), 3),
            "pearson_ex2006": round(pear_ex, 3) if np.isfinite(pear_ex) else None,
            "hi_gEQ_ret": round(float(y[x >= med].mean()), 2),
            "lo_gEQ_ret": round(float(y[x < med].mean()), 2),
        }
    # 净外部融资 (2010-2025, 固定 961 只那一批更能说明问题)
    a = df.filter(pl.col("year") == 2010).select("symbol").unique()
    b = df.filter(pl.col("year") == 2025).select("symbol").unique()
    pool = a.join(b, on="symbol", how="inner")["symbol"]
    sub = df.filter(pl.col("symbol").is_in(pool) & (pl.col("year") >= 2011) & (pl.col("year") <= 2025))
    # ΔEQ 必须是**存量差** (2025 年末 − 2010 年末)。首版写成 `sub["EQ"].sum()` 把 15 年
    # 各年净资产横向加总, 得出 150.9 万亿 (真实 23.4 万亿), 高估 6.4 倍 —— 存量/流量混用。
    eq_end = float(df.filter(pl.col("symbol").is_in(pool) & (pl.col("year") == 2025))["EQ"].sum())
    eq_beg = float(df.filter(pl.col("symbol").is_in(pool) & (pl.col("year") == 2010))["EQ"].sum())
    assert sub.filter(pl.col("year").is_in(YRS))["year"].n_unique() == len(YRS)
    dEQ = eq_end - eq_beg
    sumE = float(sub["E"].sum())          # 利润是流量 ⇒ 逐年相加才对
    out["fixed_pool"] = {
        "n_pool": int(pool.len()),
        "eq_beg_yi": round(eq_beg / 1e8, 0),
        "eq_end_yi": round(eq_end / 1e8, 0),
        "dEQ_yi": round(dEQ / 1e8, 0),
        "sum_E_yi": round(sumE / 1e8, 0),
        "net_external_financing_yi": round((dEQ - sumE) / 1e8, 0),
        "note": "净外部融资 = Δ净资产(存量差) − 累计归母净利润(流量和)。未扣分红 ⇒ 为下界",
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    df = load_annual()
    print(f"面板 {df.height} 行 / {df['symbol'].n_unique()} 只 / {df['year'].min()}-{df['year'].max()}",
          flush=True)

    r1 = h1_roe(df)
    print("\n== H1 全市场 ROE (归母净利 / 平均净资产, 单位 %) ==")
    print(f'{"年":>5}{"全口径n":>8}{"ROE":>8}{"同店n":>7}{"同店ROE":>9}{"gEQ":>8}{"gE":>8}{"ΔE/ΔEQ":>9}')
    ss = {r["year"]: r for r in r1["same_store"]}
    for r in r1["all"]:
        y = r["year"]
        s = ss.get(y)
        if s:
            mroi = s["dE"] / s["dEQ"] * 100 if s["dEQ"] else None
            s_roe, s_geq, s_ge = f'{s["roe"]:.2f}', f'{s["gEQ"]:+.1f}', f'{s["gE"]:+.1f}'
            s_n = s["n"]
            s_mroi = f"{mroi:+.1f}" if mroi is not None else "-"
        else:
            s_roe = s_geq = s_ge = s_mroi = "-"
            s_n = 0
        print(f'{y:>5}{r["n"]:>8}{r["roe"]:>8.2f}{s_n:>7}{s_roe:>9}{s_geq:>8}{s_ge:>8}{s_mroi:>9}')

    r1b = h1b_fixed_pool(df)
    print(f'\n== H1b 平衡面板 ({r1b["n_pool"]} 只, 2010-2025 每年都有年报) ==')
    print(f'{"年":>5}{"n":>6}{"ΣE(亿)":>11}{"ΣEQ(亿)":>12}{"ROE%":>8}')
    for r in r1b["series"]:
        if r["year"] < 2010:
            continue
        print(f'{r["year"]:>5}{r["n"]:>6}{r["E"]/1e8:>11.0f}{r["EQ"]/1e8:>12.0f}{r["roe"]:>8.2f}')

    r2 = h2_endogenous(df)
    print("\n== H2 内生供给: t 年指数收益 → t+1 年净资产增速 ==")
    for k, v in r2["corr"].items():
        print(f'  {k:<14} n={v["n"]:>2} corr={v["corr"]:+.3f} slope={v["slope"]:+.4f} '
              f'| 涨年→次年gEQ {v["x_hi_mean_gEQ"]:+.2f}% vs 跌年→{v["x_lo_mean_gEQ"]:+.2f}%')

    r3 = h3_industry(df)
    s = r3["summary"]
    print("\n== H3 行业: 融资强度 vs 后续 ROE ==")
    print(f'  N={s["n_obs"]} 行业-年, {s["n_ind"]} 个行业, 按 gEQ 中位 {s["split_at_gEQ_pct"]}% 分组')
    print(f'  高融资组 gEQ {s["high_fin_gEQ"]:+.2f}% → 3 年后 ROE 变化 {s["high_fin_d_roe_3y"]:+.3f}pct')
    print(f'  低融资组 gEQ {s["low_fin_gEQ"]:+.2f}% → 3 年后 ROE 变化 {s["low_fin_d_roe_3y"]:+.3f}pct')
    print(f'  corr(gEQ, ΔROE_3y) = {s["corr_gEQ_dROE"]:+.3f}')
    print(f'  每元新增净资产的净利润增量 (mROI): 中位 {s["mroi_med"]:+.2f}%  均值 {s["mroi_mean"]:+.2f}%')
    print(f'\n  行业横截面 (按平均净资产增速降序, 前/后 8):')
    print(f'    {"行业":<12}{"年数":>5}{"avg gEQ%":>10}{"avg ROE%":>10}{"med mROI%":>10}')
    bi = r3["by_industry"]
    for r in bi[:8] + bi[-8:]:
        print(f'    {r["ind"]:<12}{r["n_yr"]:>5}{r["avg_gEQ"]:>10.2f}{r["avg_roe"]:>10.2f}{r["med_mroi"]:>10.2f}')

    cv = s["corr_parts"]
    print(f'  控制初始 ROE: r(gEQ,ΔROE)={cv["r_xy"]:+.3f}  r(gEQ,ROE_t)={cv["r_xz"]:+.3f}  '
          f'r(ΔROE,ROE_t)={cv["r_yz"]:+.3f}  **偏相关={cv["partial"]:+.3f}**')
    print("  按初始 ROE 三档分层 (层内再看融资高低):")
    for b in s["by_initial_roe"]:
        print(f'    {b["bucket"]} n={b["n"]:>4} 初始ROE {b["init_roe"]:>5.2f} | '
              f'高融资 gEQ{b["hi_gEQ"]:>6.2f}%→ΔROE{b["hi_dROE"]:>+7.3f} | '
              f'低融资 gEQ{b["lo_gEQ"]:>6.2f}%→ΔROE{b["lo_dROE"]:>+7.3f} | 差 {b["gap"]:>+7.3f}')

    r4 = h4_supply_drag(df)
    print("\n== H4 供给拖累: t 年净资产增速 → t+1 年指数收益 ==")
    for k, v in r4["corr"].items():
        print(f'  {k:<16} n={v["n"]:>2} Pearson={v["corr"]:+.3f} Spearman={v["spearman"]:+.3f} '
              f'剔2006后={v["pearson_ex2006"]} | 扩张年→次年 {v["hi_gEQ_ret"]:+.2f}%  '
              f'收缩年→{v["lo_gEQ_ret"]:+.2f}%')
    print(f'\n  H4 逐年: {"年":>5}{"gEQ%":>8}' + "".join(f'{k:>12}' for k in r4["corr"]))
    for r in r4["table"]:
        if r["year"] < 2006:
            continue
        line = f'{"":>8}{r["year"]:>5}{(r["gEQ"] if r["gEQ"] is not None else 0):>8.1f}'
        for k in r4["corr"]:
            v = r.get(k)
            line += f'{(f"{v:+.1f}" if v is not None else "-"):>12}'
        print(line)
    fp = r4["fixed_pool"]
    print(f'  固定池 {fp["n_pool"]} 只 2011-2025: Δ净资产 {fp["dEQ_yi"]:.0f} 亿, '
          f'累计归母净利 {fp["sum_E_yi"]:.0f} 亿')
    print(f'  净资产 {fp["eq_beg_yi"]:.0f} → {fp["eq_end_yi"]:.0f} 亿 (Δ{fp["dEQ_yi"]:.0f}), '
          f'累计利润 {fp["sum_E_yi"]:.0f} 亿')
    print(f'  ⇒ **净外部融资 ≈ {fp["net_external_financing_yi"]:.0f} 亿** (存量差−流量和, 未扣分红即下界)')

    if args.write:
        OUT.write_text(json.dumps({"H1": r1, "H1b": r1b, "H2": r2, "H3": r3, "H4": r4}, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        print(f"\n[written] {OUT}")


if __name__ == "__main__":
    main()
