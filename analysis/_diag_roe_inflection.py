"""ROE 拐点能不能提前观测 —— 用 T 期财报内部的前瞻科目预测 T+1..T+4 的 ROE 变化。

三个必须先解决的口径问题（否则结论全是假的）:

1. **announce_date 的年份是错的**。数据源把同一「月日」的所有年份都填成最新一年
   （000001.SZ 2025-06-30 的报告标注 2026-08-15 披露）。直接用 = 前视偏差。
   本脚本用「月日 + 报告期规则」重建，落在不合理窗时退回法定截止日（更保守）。

2. **财报是累计值**（Q1 / H1 / 9M / FY），必须差分出单季。差分要求相邻期严格 3 个月，
   否则会跨期相减（某公司漏披露 Q1，H1−Q1 就变成了 H1−去年年报）。

3. **重叠样本**。d_roe_fwd 用未来 4 季，相邻季度重叠 3 季 ⇒ 逐季 t 值虚高约 2 倍
   （sqrt(4)）。主口径只用 Q4 观察（不重叠），全季度口径只作对照。

运行:
    python analysis/_diag_roe_inflection.py           # 只读
    python analysis/_diag_roe_inflection.py --write   # 落结果 json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
BF = ROOT / "data" / "backfill_20261117"
OUT = ROOT / "analysis" / "_diag_roe_inflection_result.json"
IND = ROOT / "data" / "ext_data" / "ext_hy_ths" / "part.parquet"

# 法定披露截止日 (保守口径): 报告期月份 → (年份偏移, 截止月, 截止日)
DEADLINE = {3: (0, 4, 30), 6: (0, 8, 31), 9: (0, 10, 31), 12: (1, 4, 30)}
# 披露月日的合理窗 (超出视为异常/拖延, 退回截止日)
SANE = {3: (4, 6), 6: (7, 9), 9: (10, 12), 12: (1, 6)}

FEATURES = {
    "d_roe": "ROE 一年变化 (纯均值回归项, 基准)",
    "roe": "ROE 水平",
    "d_gm": "毛利率变化 (TTM)",
    "accrual": "应计: 应收增速 − 收入增速",
    "inv_gap": "存货增速 − 收入增速",
    "cash_conv": "经营现金流 / 归母净利 (TTM)",
    "capex_int": "资本开支 / 收入 (TTM)",
    "fa_turn_chg": "固定资产周转率变化",
    "d_debt": "总负债增速",
    "d_eq": "净资产增速 (股本扩张镜像)",
}


# ---------------------------------------------------------------- PIT 重建
def pit_expr() -> pl.Expr:
    """重建 PIT 可知日: 月日可信(97%)、年份不可信 ⇒ 用 period_end 年份推。

    月日落在合理窗外（年报拖到 7 月等）⇒ 退回法定截止日。
    宁可晚知不可早知: 前视偏差会让所有 IC 虚高, 晚知只损失一点统计功效。
    """
    m = pl.col("period_end").dt.month()
    y = pl.col("period_end").dt.year()
    am = pl.col("announce_date").dt.month()
    ad = pl.col("announce_date").dt.day()
    # 2/29 在平年不存在, pl.date 会抛错 ⇒ clamp 到 2/28 (前后差 1 天, 不影响季度级检验)
    ad = pl.when((am == 2) & (ad > 28)).then(28).otherwise(ad)

    parts = []
    for pm, (lo, hi) in SANE.items():
        oy, dm, dd = DEADLINE[pm]
        base_year = (y + oy) if oy else y
        parts.append(
            pl.when(m == pm)
            .then(pl.when((am >= lo) & (am <= hi))
                  .then(pl.date(base_year, am, ad))
                  .otherwise(pl.date(base_year, pl.lit(dm), pl.lit(dd))))
        )
    return pl.coalesce(parts)


def load_fin() -> pl.DataFrame:
    """单季化 + TTM + 领先特征。每 (symbol, qidx) 一行。"""
    inc = pl.read_parquet(
        BF / "fin_income.parquet",
        columns=["symbol", "period_end", "announce_date", "revenue",
                 "operating_cost", "net_income_attributable"],
    )
    bal = pl.read_parquet(
        BF / "fin_balance.parquet",
        columns=["symbol", "period_end", "total_assets", "total_liabilities",
                 "total_equity", "accounts_receivable", "inventory", "fixed_asset"],
    )
    cf = pl.read_parquet(
        BF / "fin_cashflow.parquet",
        columns=["symbol", "period_end", "net_operating_cash_flow", "capex"],
    )

    df = (
        inc.join(bal, on=["symbol", "period_end"], how="left", coalesce=True)
        .join(cf, on=["symbol", "period_end"], how="left", coalesce=True)
    )
    df = (
        df.with_columns(pl.col("period_end").dt.month().alias("m"))
        .with_columns((pl.col("period_end").dt.year() * 4
                       + (pl.col("m") // 3 - 1)).alias("qidx"))
        .with_columns(pit_expr().alias("pit_date"))
        .filter(pl.col("pit_date").is_not_null())
        .sort(["symbol", "qidx"])
    )

    # 记录 1..4 期前的 qidx, 用于「严格相邻」判定
    for k in (1, 2, 3, 4):
        df = df.with_columns(pl.col("qidx").shift(k).over("symbol").alias(f"q_{-k}"))

    CUM = ["revenue", "operating_cost", "net_income_attributable",
           "net_operating_cash_flow", "capex"]
    # 累计值的 1..3 期滞后 (用于单季差分)
    for c in CUM:
        for k in (1, 2, 3):
            df = df.with_columns(pl.col(c).shift(k).over("symbol").alias(f"{c}__l{k}"))

    adjacent = pl.col("qidx") - pl.col("q_-1") == 1
    # 单季 = 累计 − 上期累计; Q1 直接取。要求严格相邻, 否则留置 null。
    singles = {
        "rev_q": "revenue", "cogs_q": "operating_cost", "ni_q": "net_income_attributable",
        "ocf_q": "net_operating_cash_flow", "capex_q": "capex",
    }
    for out, src in singles.items():
        df = df.with_columns(
            pl.when(pl.col("m") == 3).then(pl.col(src))
            .when(adjacent).then(pl.col(src) - pl.col(f"{src}__l1"))
            .otherwise(None).alias(out)
        )

    # 单季值的 1..3 期滞后, 拼 TTM (要求 4 期严格连续: qidx − q_{-3} == 3)
    for c in singles:
        for k in (1, 2, 3):
            df = df.with_columns(pl.col(c).shift(k).over("symbol").alias(f"{c}__s{k}"))

    ok4 = pl.col("qidx") - pl.col("q_-3") == 3
    for c in singles:
        df = df.with_columns(
            pl.when(ok4).then(pl.col(c) + pl.col(f"{c}__s1") + pl.col(f"{c}__s2")
                              + pl.col(f"{c}__s3")).otherwise(None).alias(f"{c}_ttm")
        )

    # 平均净资产 = (EQ_t + EQ_{t-4}) / 2, 要求 qidx 差 4
    df = df.with_columns(pl.col("total_equity").shift(4).over("symbol").alias("eq_l4"))
    ok5 = pl.col("qidx") - pl.col("q_-4") == 4
    df = df.with_columns(
        pl.when(ok5 & (pl.col("total_equity") > 0) & (pl.col("eq_l4") > 0))
        .then((pl.col("total_equity") + pl.col("eq_l4")) / 2)
        .otherwise(None).alias("eq_avg")
    )
    # 同比基数 (4 期前)
    for c in ["rev_q_ttm", "accounts_receivable", "inventory",
              "total_liabilities", "fixed_asset"]:
        df = df.with_columns(pl.col(c).shift(4).over("symbol").alias(f"{c}__y0"))

    df = df.with_columns([
        (pl.col("ni_q_ttm") / pl.col("eq_avg") * 100).alias("roe"),
        ((pl.col("rev_q_ttm") - pl.col("cogs_q_ttm")) / pl.col("rev_q_ttm") * 100).alias("gm"),
        (pl.col("ocf_q_ttm") / pl.col("ni_q_ttm")).alias("cash_conv"),
        (pl.col("capex_q_ttm") / pl.col("rev_q_ttm") * 100).alias("capex_int"),
        (pl.col("rev_q_ttm") / pl.col("fixed_asset")).alias("fa_turn"),
    ])
    df = df.with_columns([
        (pl.col("rev_q_ttm") / pl.col("rev_q_ttm__y0").abs() - 1).alias("rev_g"),
        (pl.col("accounts_receivable") / pl.col("accounts_receivable__y0").abs() - 1).alias("ar_g"),
        (pl.col("inventory") / pl.col("inventory__y0").abs() - 1).alias("inv_g"),
        (pl.col("total_liabilities") / pl.col("total_liabilities__y0").abs() - 1).alias("d_debt"),
        (pl.col("total_equity") / pl.col("eq_l4") - 1).alias("d_eq"),
        (pl.col("gm") - pl.col("gm").shift(4).over("symbol")).alias("d_gm"),
        (pl.col("fa_turn") / pl.col("fa_turn").shift(4).over("symbol") - 1).alias("fa_turn_chg"),
        (pl.col("roe") - pl.col("roe").shift(4).over("symbol")).alias("d_roe"),
    ])
    df = df.with_columns([
        (pl.col("ar_g") - pl.col("rev_g")).alias("accrual"),
        (pl.col("inv_g") - pl.col("rev_g")).alias("inv_gap"),
    ])

    # 目标: 未来一年 ROE 变化 (TTM vs TTM), 要求 qidx 差 4
    df = df.with_columns([
        pl.col("roe").shift(-4).over("symbol").alias("roe_fwd"),
        pl.col("qidx").shift(-4).over("symbol").alias("q_fwd"),
    ])
    df = df.with_columns(
        pl.when(pl.col("q_fwd") - pl.col("qidx") == 4)
        .then(pl.col("roe_fwd") - pl.col("roe"))
        .otherwise(None).alias("d_roe_fwd")
    )

    # ---- 分子/分母分解 ----
    # ROE = NI/EQ ⇒ ΔROE 里有相当一部分只是「分母变大」的算术, 不是经营恶化。
    # 必须把目标拆成: 利润增速 (分子) / 净资产增速 (分母) / ROA (换分母)
    df = df.with_columns([
        pl.col("ni_q_ttm").shift(-4).over("symbol").alias("ni_fwd"),
        pl.col("total_equity").shift(-4).over("symbol").alias("eq_fwd"),
        pl.col("total_assets").shift(-4).over("symbol").alias("ta_fwd"),
        (pl.col("ni_q_ttm") / pl.col("total_assets") * 100).alias("roa"),
    ])
    df = df.with_columns([
        # 只有「基期与末期都盈利」时增速才有意义。用 abs() 会让负利润公司的
        # 符号错乱 (亏损扩大变成正增长), 均值被拉到 -36% 那种量级。
        pl.when((pl.col("ni_q_ttm") > 0) & (pl.col("ni_fwd") > 0))
        .then(pl.col("ni_fwd") / pl.col("ni_q_ttm") - 1).otherwise(None).alias("g_ni_fwd"),
        (pl.col("eq_fwd") / pl.col("total_equity") - 1).alias("g_eq_fwd"),
        (pl.col("ni_fwd") / pl.col("ta_fwd") * 100 - pl.col("roa")).alias("d_roa_fwd"),
        (pl.col("roa") - pl.col("roa").shift(4).over("symbol")).alias("d_roa"),
        # 净外部融资 (NEF): 净资产增加里超出「留存利润」的部分 = 增发/配股/可转债
        ((pl.col("total_equity") - pl.col("eq_l4") - pl.col("ni_q_ttm"))
         / pl.col("eq_l4")).alias("nef_ratio"),
    ])
    return df


# ---------------------------------------------------------------- 统计工具
def _rank(a: np.ndarray) -> np.ndarray:
    """秩变换 (scipy 不在 venv)。"""
    o = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), dtype=float)
    r[o] = np.arange(len(a))
    return r


def ic_by_period(df: pl.DataFrame, feat: str, tgt: str = "d_roe_fwd",
                 by: str = "period_end", min_n: int = 30) -> np.ndarray:
    """按期截面算 Spearman IC。"""
    ics = []
    for _, sub in df.group_by(by):
        s = sub.select([feat, tgt]).drop_nulls()
        if s.height < min_n:
            continue
        x = _rank(s[feat].to_numpy().astype(float))
        y = _rank(s[tgt].to_numpy().astype(float))
        if x.std() == 0 or y.std() == 0:
            continue
        ics.append(float(np.corrcoef(x, y)[0, 1]))
    return np.array(ics)


def ic_stats(ic: np.ndarray) -> dict:
    if ic.size < 5:
        return {"n": int(ic.size)}
    m, sd = float(ic.mean()), float(ic.std(ddof=1))
    return {"n": int(ic.size), "ic": round(m, 4), "sd": round(sd, 4),
            "t": round(m / (sd / np.sqrt(ic.size)), 2),
            "pos": round(float((ic > 0).mean()), 3)}


def _winsor(a: np.ndarray, lo: float = 0.01, hi: float = 0.99) -> np.ndarray:
    x = np.asarray(a, dtype=float)
    m = np.isfinite(x)
    if m.sum() < 10:
        return x
    a0, a1 = np.quantile(x[m], [lo, hi])
    return np.clip(x, a0, a1)


def resid_on(df: pl.DataFrame, feat: str, ctrl: list[str], tgt: str = "d_roe_fwd",
             by: str = "period_end") -> pl.DataFrame:
    """截面内把 feat 与 tgt 同时对 ctrl 回归取残差 (秩空间), 输出残差列。"""
    # feat 自己不能同时当控制变量 (会 select 出重复列)
    ctrl = [c for c in ctrl if c != feat]
    rows = []
    # partition_by(as_dict=True) 保留分组键; group_by 会把键从子表里丢掉
    for key, sub in df.partition_by(by, as_dict=True).items():
        s = sub.select([feat, tgt] + ctrl).drop_nulls()
        if s.height < 40:
            continue
        y = _rank(s[tgt].to_numpy())
        x = _rank(s[feat].to_numpy())
        Z = np.column_stack([_rank(s[c].to_numpy()) for c in ctrl])
        A = np.column_stack([np.ones(len(y)), Z])
        by_, *_ = np.linalg.lstsq(A, y, rcond=None)
        bx, *_ = np.linalg.lstsq(A, x, rcond=None)
        sub2 = s.with_columns([
            pl.Series("_rx", x - A @ bx), pl.Series("_ry", y - A @ by_)
        ])
        rows.append(sub2.select(["_rx", "_ry"]).with_columns(
            pl.lit(key[0] if isinstance(key, tuple) else key).alias(by)))
    if not rows:
        return pl.DataFrame()
    return pl.concat(rows)


def resid_ic(df: pl.DataFrame, feat: str, ctrl: list[str]) -> dict:
    """控制 ctrl 后的截面秩相关 (逐期), 用于判断「增量」。"""
    r = resid_on(df, feat, ctrl)
    if r.height == 0:
        return {"n": 0}
    ics = []
    for _, sub in r.group_by("period_end"):
        if sub.height < 30:
            continue
        x, y = sub["_rx"].to_numpy(), sub["_ry"].to_numpy()
        if x.std() == 0 or y.std() == 0:
            continue
        ics.append(float(np.corrcoef(x, y)[0, 1]))
    return ic_stats(np.array(ics))


# ---------------------------------------------------------------- 主流程
def prep(df: pl.DataFrame, require_fwd: bool = True) -> pl.DataFrame:
    """清洗: 只保留 2000 年后、极端值截尾、剔除资不抵债。

    require_fwd=False 时保留最后 4 期 (它们没有「未来一年」, 但最新状态要用它)。
    """
    d = df.filter(
        (pl.col("period_end") >= pl.lit("2000-03-31").str.to_date())
        & (pl.col("d_roe_fwd").is_not_null() if require_fwd else pl.lit(True))
        & pl.col("eq_avg").is_not_null()
        & (pl.col("eq_avg") > 0)
        & pl.col("roe").is_not_null()
    )
    # 5 组极端值: |d_roe| > 80pct 基本是重组/借壳/一次性损益, 不是经营
    if require_fwd:
        d = d.filter(pl.col("d_roe_fwd").abs() < 80)
    else:
        d = d.filter(pl.col("d_roe").is_null() | (pl.col("d_roe").abs() < 80))
    d = d.with_columns([
        pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c)
        for c in FEATURES
    ])
    return d


def part_a(df: pl.DataFrame) -> dict:
    """A. 基线: ROE 有多强的均值回归? 这是所有「预测力」的比较基准。"""
    out = {}
    for lab, sub in (("Q4 不重叠", df.filter(pl.col("m") == 12)),
                     ("全季度", df)):
        ic = ic_by_period(sub, "d_roe")
        ic_lv = ic_by_period(sub, "roe")
        out[lab] = {"n_rows": sub.height,
                    "ic_d_roe": ic_stats(ic), "ic_roe": ic_stats(ic_lv),
                    "d_roe_fwd_mean": round(float(sub["d_roe_fwd"].mean()), 3),
                    "d_roe_fwd_med": round(float(sub["d_roe_fwd"].median()), 3),
                    "share_up": round(float((sub["d_roe_fwd"] > 0).mean()), 3)}
    return out


def part_b(df: pl.DataFrame, q4_only: bool = True) -> dict:
    """B. 单变量 IC + 控制当期 ROE、d_roe 后的增量。"""
    sub = df.filter(pl.col("m") == 12) if q4_only else df
    out = {}
    for f in FEATURES:
        raw = ic_stats(ic_by_period(sub, f))
        inc1 = resid_ic(sub, f, ["roe", "d_roe"])
        out[f] = {"raw": raw, "incr_vs_roe_droe": inc1}
    return out


def part_c(df: pl.DataFrame) -> dict:
    """C. 分层: 按特征分 5 组, 看未来 ΔROE 组间差 (Q5−Q1), 逐期算 t。"""
    sub = df.filter(pl.col("m") == 12)
    out = {}
    for f in FEATURES:
        spreads = []
        for _, g in sub.group_by("period_end"):
            s = g.select([f, "d_roe_fwd"]).drop_nulls()
            if s.height < 100:
                continue
            x = s[f].to_numpy().astype(float)
            y = s["d_roe_fwd"].to_numpy().astype(float)
            try:
                q = np.quantile(x, [0.2, 0.8])
            except Exception:
                continue
            hi, lo = y[x >= q[1]], y[x <= q[0]]
            if len(hi) < 15 or len(lo) < 15:
                continue
            spreads.append(float(_winsor(hi).mean() - _winsor(lo).mean()))
        if len(spreads) >= 5:
            a = np.array(spreads)
            out[f] = {"n_periods": len(a), "spread": round(float(a.mean()), 3),
                      "t": round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 2),
                      "pos": round(float((a > 0).mean()), 3)}
    return out


def part_d_placebo(df: pl.DataFrame) -> dict:
    """D1 时间错配负控: 把特征滞后 4 期再用。

    若「同期特征 → 未来 ROE」是因果链 (领先), 滞后一拍应显著衰减;
    若衰减很少, 说明两边都是同一个慢变量的投影, 不是领先关系。
    """
    sub = df.filter(pl.col("m") == 12).sort(["symbol", "qidx"])
    out = {}
    for f in ("d_gm", "accrual", "cash_conv", "capex_int", "d_eq", "d_debt"):
        lagged = sub.with_columns(
            pl.col(f).shift(4).over("symbol").alias("_lag")
        ).filter(pl.col("_lag").is_not_null())
        a = ic_stats(ic_by_period(sub, f))
        b = ic_stats(ic_by_period(lagged, "_lag"))
        out[f] = {"same_period": a, "lag4": b}
    return out


def part_e_split(df: pl.DataFrame) -> dict:
    """D2 行业间 vs 行业内: 未来 ΔROE 里有多少是行业共同成分?

    做法: 每期算行业均值, 拆成 (行业均值) + (个股 − 行业均值)。分别做 IC。
    若特征是「预测行业」而不是「预测个股」, 它对个股偏离项的 IC 会归零。
    """
    ind = pl.read_parquet(IND).select(["symbol", "所属同花顺行业"]).rename(
        {"所属同花顺行业": "ind_l2"})
    ind = ind.with_columns(pl.col("ind_l2").str.split("-").list.first().alias("ind_l1"))
    d = df.join(ind, on="symbol", how="left")
    d = d.filter(pl.col("ind_l1").is_not_null())
    d = d.with_columns(
        pl.col("d_roe_fwd").mean().over(["period_end", "ind_l1"]).alias("ind_fwd"),
        pl.col("d_roe_fwd").mean().over("period_end").alias("mkt_fwd"),
    )
    d = d.with_columns([
        (pl.col("ind_fwd") - pl.col("mkt_fwd")).alias("tgt_ind_dev"),
        (pl.col("d_roe_fwd") - pl.col("ind_fwd")).alias("tgt_firm_dev"),
    ])
    sub = d.filter(pl.col("m") == 12)
    out = {"n_ind": d["ind_l1"].n_unique(), "feat": {}}
    for f in ("d_gm", "accrual", "cash_conv", "capex_int", "d_eq", "d_debt", "d_roe", "roe"):
        out["feat"][f] = {
            "vs_total": ic_stats(ic_by_period(sub, f)),
            "vs_ind_dev": ic_stats(ic_by_period(sub, f, tgt="tgt_ind_dev")),
            "vs_firm_dev": ic_stats(ic_by_period(sub, f, tgt="tgt_firm_dev")),
        }
    return out


def part_f_oos(df: pl.DataFrame) -> dict:
    """E. 样本外: 前 60% vs 后 40% 期数, 方向是否稳定。"""
    sub = df.filter(pl.col("m") == 12)
    periods = sorted(sub["period_end"].unique().to_list())
    cut = periods[int(len(periods) * 0.6)]
    out = {"cut": str(cut), "n_all": len(periods), "feat": {}}
    for span, cond in ((f"前段 <{cut}", pl.col("period_end") < pl.lit(cut)),
                       (f"后段 >={cut}", pl.col("period_end") >= pl.lit(cut))):
        s = sub.filter(cond)
        out["feat"][span] = {f: ic_stats(ic_by_period(s, f))
                             for f in ("d_roe", "roe", "d_gm", "accrual",
                                       "cash_conv", "capex_int", "d_eq")}
    return out


def part_g_hit(df: pl.DataFrame) -> dict:
    """F. 拐点命中率: 预测符号对不对 (而不是相关性)。

    拐点 = 未来一年 ΔROE > 0。三个预测器:
      base    : 只做均值回归 (预测 = −d_roe > 0 即「上期在跌 ⇒ 下期反弹」)
      gm      : 毛利率改善 (d_gm > 0)
      combo   : 均值回归 + 毛利率 + 现金含量 三项同向
    """
    sub = df.filter(pl.col("m") == 12)
    out = {}
    up = sub["d_roe_fwd"] > 0
    base_rate = float(up.mean())
    for name, expr in (
        ("base 均值回归", (pl.col("d_roe") < 0)),
        ("毛利率改善", (pl.col("d_gm") > 0)),
        ("现金含量>1", (pl.col("cash_conv") > 1)),
        ("同向 >=2 项", ((pl.col("d_roe") < 0).cast(pl.Int8) + (pl.col("d_gm") > 0).cast(pl.Int8)
                         + (pl.col("cash_conv") > 1).cast(pl.Int8)) >= 2),
    ):
        s = sub.with_columns(expr.fill_null(False).alias("_p"))
        n = int(s["_p"].sum())
        if n < 50:
            out[name] = {"n": n}
            continue
        hit = float(s.filter(pl.col("_p"))["d_roe_fwd"].gt(0).mean())
        out[name] = {"n": n, "hit": round(hit, 4),
                     "lift_pct": round((hit - base_rate) * 100, 2)}
    out["_base_rate"] = round(base_rate, 4)
    out["_n"] = sub.height
    return out


def part_h_industry(df: pl.DataFrame) -> dict:
    """G. 行业层面: 用户原命题是「融资帮助行业发展」, 主体是行业不是个股。

    行业 ROE = Σ归母净利(TTM) / Σ平均净资产。行业特征用**加总后**计算,
    个股特征用成员中位数 (避免巨头主导)。
    """
    ind = pl.read_parquet(IND).select(["symbol", "所属同花顺行业"]).rename(
        {"所属同花顺行业": "ind_l2"})
    ind = ind.with_columns(pl.col("ind_l2").str.split("-").list.first().alias("ind_l1"))
    d = df.join(ind, on="symbol", how="left").filter(pl.col("ind_l1").is_not_null())

    agg = (
        d.group_by(["period_end", "qidx", "ind_l1"])
        .agg([
            pl.col("ni_q_ttm").sum().alias("_ni"), pl.col("eq_avg").sum().alias("_eq"),
            pl.col("rev_q_ttm").sum().alias("_rev"),
            pl.col("cogs_q_ttm").sum().alias("_cogs"),
            pl.col("ocf_q_ttm").sum().alias("_ocf"),
            pl.col("capex_q_ttm").sum().alias("_capex"),
            pl.len().alias("_n"),
            pl.col("d_gm").median().alias("d_gm"),
            pl.col("accrual").median().alias("accrual"),
            pl.col("d_eq").median().alias("d_eq"),
            pl.col("d_debt").median().alias("d_debt"),
        ])
        .filter(pl.col("_n") >= 5)          # 行业样本太小的期剔除
        .sort(["ind_l1", "qidx"])
    )
    agg = agg.with_columns([
        (pl.col("_ni") / pl.col("_eq") * 100).alias("roe"),
        ((pl.col("_rev") - pl.col("_cogs")) / pl.col("_rev") * 100).alias("gm"),
        (pl.col("_ocf") / pl.col("_ni")).alias("cash_conv"),
        # 行业层面用 OCF/营收: NI→0 时 OCF/NI 会爆炸 (钢铁曾算出 148)
        (pl.col("_ocf") / pl.col("_rev") * 100).alias("ocf_margin"),
        (pl.col("_capex") / pl.col("_rev") * 100).alias("capex_int"),
    ])
    agg = agg.with_columns([
        (pl.col("roe") - pl.col("roe").shift(4).over("ind_l1")).alias("d_roe"),
        (pl.col("gm") - pl.col("gm").shift(4).over("ind_l1")).alias("d_gm_agg"),
        (pl.col("qidx").shift(-4).over("ind_l1") - pl.col("qidx")).alias("_gapf"),
        (pl.col("roe").shift(-4).over("ind_l1") - pl.col("roe")).alias("_dfwd"),
    ])
    agg = agg.with_columns(
        pl.when(pl.col("_gapf") == 4).then(pl.col("_dfwd")).otherwise(None).alias("d_roe_fwd")
    )
    agg_full = agg                        # 全期 (最新一期还没有 fwd, 但 snapshot 要它)
    agg = agg.filter(pl.col("d_roe_fwd").is_not_null() & (pl.col("d_roe_fwd").abs() < 80))

    out = {"n_rows": agg.height, "n_ind": agg["ind_l1"].n_unique(), "feat": {}}
    both = {"q4": agg.filter(pl.col("period_end").dt.month() == 12), "all": agg}
    for lab, sub in both.items():
        out["feat"][lab] = {
            f: ic_stats(ic_by_period(sub, f, min_n=8))
            for f in ("d_roe", "roe", "cash_conv", "d_gm_agg", "d_gm", "d_eq", "d_debt", "accrual", "capex_int")
        }
    # 最新一期快照: 哪些行业的领先指标已经在改善
    out["snapshot"] = latest_snapshot(agg_full)
    out["_agg"] = agg_full
    return out


def latest_snapshot(agg: pl.DataFrame) -> dict:
    """最新一期: 各行业领先指标在**当期 90 个行业中的截面分位**。

    不用「自身历史分位」: 全行业现金含量在同一期一起抬升 (2026-09 全部 83-100),
    那是时间效应不是行业差异。选行业要的是横向比较。
    行业现金含量用 OCF/营收, 不用 OCF/净利 (后者 NI→0 时爆炸, 钢铁算出 148)。
    """
    last = agg["period_end"].max()
    cur = agg.filter(pl.col("period_end") == last)
    out = {"period_end": str(last), "rows": []}
    if cur.height < 8:
        return out
    for f in ("d_gm_agg", "ocf_margin", "d_eq", "roe"):
        cur = cur.with_columns(
            (pl.col(f).rank() / pl.col(f).count() * 100).alias(f"pct_{f}"))
    cur = cur.with_columns(
        (pl.col("pct_d_gm_agg").fill_null(50) + pl.col("pct_ocf_margin").fill_null(50)
         - pl.col("pct_d_eq").fill_null(50)).alias("score"))
    keep = ["ind_l1", "roe", "d_roe", "d_gm_agg", "ocf_margin", "d_eq",
            "pct_d_gm_agg", "pct_ocf_margin", "pct_d_eq", "score"]
    for r in cur.select([c for c in keep if c in cur.columns]).sort(
            "score", descending=True).to_dicts():
        out["rows"].append({k: (round(v, 2) if isinstance(v, float) else v)
                            for k, v in r.items()})
    return out


def part_i_decompose(df: pl.DataFrame) -> dict:
    """I. 核心判据: d_eq 的负 IC 是「分母变大」的算术, 还是「融资没用」?

    ROE = NI/EQ ⇒ ΔROE ≈ ROE·(g_NI − g_EQ)。
    若 d_eq 只预测 g_EQ (分母) 而不预测 g_NI (分子), 那它对 ΔROE 的预测力
    是恒等式, 没有任何经济含义。真正的检验是看分子。
    """
    sub = df.filter(pl.col("m") == 12)
    v = sub.select(["d_roe_fwd", "roe", "g_ni_fwd", "g_eq_fwd"]).drop_nulls()
    ident = v.with_columns(
        (pl.col("roe") * (pl.col("g_ni_fwd") - pl.col("g_eq_fwd"))).alias("_apx")
    ).filter(pl.col("_apx").is_between(-200, 200) & pl.col("d_roe_fwd").is_between(-80, 80))
    ta = ident["_apx"].to_numpy()
    tb = ident["d_roe_fwd"].to_numpy()
    ident_corr = float(np.corrcoef(_winsor(ta), _winsor(tb))[0, 1])

    tgts = {"d_roe_fwd": "ΔROE", "g_ni_fwd": "利润增速",
            "g_eq_fwd": "净资产增速", "d_roa_fwd": "ΔROA"}
    feats = ["d_eq", "nef_ratio", "accrual", "cash_conv", "capex_int", "d_gm", "d_debt"]
    out = {"ident_corr": round(ident_corr, 4), "n_ident": int(ident.height), "ic": {}}
    for f in feats:
        out["ic"][f] = {}
        for t in tgts:
            s2 = sub.filter(pl.col(t).is_not_null() & pl.col(t).is_between(-200, 200))
            out["ic"][f][t] = ic_stats(ic_by_period(s2, f, tgt=t))
    cc = sub.select(["nef_ratio", "d_eq"]).drop_nulls()
    out["corr_nef_eq"] = round(float(np.corrcoef(
        _rank(cc["nef_ratio"].to_numpy()), _rank(cc["d_eq"].to_numpy()))[0, 1]), 3)
    out["race_nef_ctrl_d_eq"] = resid_ic(
        sub.filter(pl.col("d_roe_fwd").is_not_null()), "nef_ratio", ["d_eq"])
    out["race_d_eq_ctrl_nef"] = resid_ic(
        sub.filter(pl.col("d_roe_fwd").is_not_null()), "d_eq", ["nef_ratio"])

    buckets = ((-9.0, -0.05, "净回购/分红 <-5%"), (-0.05, 0.01, "-5% ~ 0"),
               (0.01, 0.10, "1-10%"), (0.10, 0.30, "10-30%"), (0.30, 9.0, ">30%"))
    rows = []
    for lo, hi, lab in buckets:
        g = sub.filter((pl.col("nef_ratio") >= lo) & (pl.col("nef_ratio") < hi))
        g = g.select(["d_roe_fwd", "g_ni_fwd", "g_eq_fwd", "d_roa_fwd", "roe"]).drop_nulls()
        if g.height < 100:
            continue
        rows.append({
            "bucket": lab, "n": int(g.height),
            "roe": round(float(g["roe"].mean()), 2),
            "g_ni_fwd": round(float(_winsor(g["g_ni_fwd"].to_numpy(), 0.02, 0.98).mean()) * 100, 2),
            "g_eq_fwd": round(float(g["g_eq_fwd"].mean()) * 100, 2),
            "d_roa_fwd": round(float(_winsor(g["d_roa_fwd"].to_numpy(), 0.02, 0.98).mean()), 2),
            "d_roe_fwd": round(float(_winsor(g["d_roe_fwd"].to_numpy(), 0.02, 0.98).mean()), 2),
        })
    out["by_nef"] = rows
    return out


def part_k_lead(ev_df: pl.DataFrame, feats: list[str] | None = None,
                evcol: str = "d_roe") -> dict:
    """K. 特征领先 ROE 拐点几个季度?

    事件 = ΔROE 由 ≤0 转为 >0 (由降转升)。把每只股票的事件对齐到 t=0,
    看各特征在 t−8..t+4 的**同期横截面分位**均值。
    若某特征在 t=−2 就已经偏离 50 分位, 说明它能提前两个季度观测到拐点。
    """
    feats = feats or ["d_gm", "accrual", "cash_conv", "capex_int", "d_eq", "d_debt"]
    d = ev_df
    base = d.select(["symbol", "qidx", "period_end", evcol] + feats)
    base = base.with_columns(pl.int_range(0, pl.len()).over("symbol").alias("rn"))
    # **必须在全体截面上排名**: 若先在事件子集内排名, 均值必然 = 50 (循环论证,
    # 首版就打出 13 期清一色 50.2 的假表)。
    pcols = []
    for f in feats:
        pcols.append((pl.col(f).rank().over("period_end")
                      / pl.col(f).count().over("period_end") * 100).alias(f"p_{f}"))
    base = base.with_columns(pcols)
    b = base.with_columns(pl.col(evcol).shift(1).over("symbol").alias("_prev"))
    ev = b.filter((pl.col(evcol) > 0) & (pl.col("_prev") <= 0)
                  & pl.col("_prev").is_not_null()).select(["symbol", "rn"])
    out = {"n_events": int(ev.height), "rel": {}, "evcol": evcol}
    if ev.height < 200:
        return out
    keep = ["symbol", "rn", "period_end"] + [f"p_{f}" for f in feats]

    def walk(events: pl.DataFrame, tag: str) -> dict:
        got = {}
        for rel in range(-8, 5):
            j = (events.with_columns((pl.col("rn") + rel).alias("rn"))
                 .join(base.select(keep), on=["symbol", "rn"], how="inner"))
            if j.height < 100:
                continue
            row = {f: round(float(j[f"p_{f}"].mean()), 1) for f in feats
                   if j[f"p_{f}"].is_not_null().sum() >= 100}
            row["n"] = int(j.height)
            got[rel] = row
        return got

    out["rel"] = walk(ev, "real")

    # 安慰剂: 随机抽同样多的「非拐点」行。若形状相同 ⇒ 那 42.8→57.9 只是
    # 「任意时点前后」的普遍形态, 与拐点无关 (上一轮美联储研究踩过同样的坑:
    # 「低点第 13 日」在安慰剂期也是第 10-13 日)。
    rng = np.random.default_rng(11)
    pool = b.filter(~((pl.col(evcol) > 0) & (pl.col("_prev") <= 0))
                    ).filter(pl.col("_prev").is_not_null()).select(["symbol", "rn"])
    idx = rng.choice(pool.height, size=min(len(ev), pool.height), replace=False)
    idx.sort()
    ph = pool[np.asarray(idx)].with_columns(
        pl.int_range(0, pl.len()).alias("rn"))
    ph = ph.select(["symbol", pl.col("rn").alias("rn")])
    # 保留原始 rn
    ph = pool.gather(np.asarray(idx))
    out["placebo"] = walk(ph, "placebo")
    return out
    for rel in range(-8, 5):
        j = (ev.with_columns((pl.col("rn") + rel).alias("rn"))
             .join(base.select(["symbol", "rn", "period_end"] + feats),
                   on=["symbol", "rn"], how="inner"))
        if j.height < 100:
            continue
        row = {}
        for f in feats:
            jj = j.filter(pl.col(f).is_not_null() & pl.col("period_end").is_not_null())
            if jj.height < 100:
                continue
            jj = jj.with_columns(
                (pl.col(f).rank().over("period_end")
                 / pl.col(f).count().over("period_end") * 100).alias("_pct"))
            row[f] = round(float(jj["_pct"].mean()), 1)
        row["n"] = int(j.height)
        out["rel"][rel] = row
    return out


def part_j3(ev: pl.DataFrame, agg: pl.DataFrame) -> dict:
    """J3. 行业层面: 行业领先指标能否选出「明年涨得多的行业」。"""
    ind = pl.read_parquet(IND).select(["symbol", "所属同花顺行业"]).rename(
        {"所属同花顺行业": "ind_l1"})
    ind = ind.with_columns(pl.col("ind_l1").str.split("-").list.first())
    e = ev.join(ind.select(["symbol", "ind_l1"]), on="symbol", how="inner")
    # 行业组合收益 = 成员等权
    hr = e.group_by(["period_end", "ind_l1"]).agg(
        pl.col("ret").mean().alias("ind_ret"), pl.len().alias("_n")
    ).filter(pl.col("_n") >= 5)
    feats = ["d_roe", "roe", "cash_conv", "d_gm_agg", "d_eq", "d_debt", "accrual"]
    a = agg.select(["period_end", "ind_l1"] + [f for f in feats if f in agg.columns]
                   ).with_columns(pl.col("period_end").dt.truncate("1y").alias("_y"))
    hr2 = hr.with_columns(pl.col("period_end").dt.truncate("1y").alias("_y"))
    m = hr2.join(a.drop("period_end"), on=["ind_l1", "_y"], how="inner")
    out = {"n_rows": int(m.height), "n_ind": int(hr["ind_l1"].n_unique()), "ic": {}}
    if m.height < 200:
        return out
    for f in feats:
        if f not in m.columns:
            continue
        ics = []
        for key, sub in m.partition_by("_y", as_dict=True).items():
            s2 = sub.select([f, "ind_ret"]).drop_nulls()
            if s2.height < 10:
                continue
            x = _rank(s2[f].to_numpy().astype(float))
            y = _rank(s2["ind_ret"].to_numpy().astype(float))
            if x.std() == 0 or y.std() == 0:
                continue
            ics.append(float(np.corrcoef(x, y)[0, 1]))
        out["ic"][f] = ic_stats(np.array(ics))
    return out


def _spread_by(ev: pl.DataFrame, col: str, by: list[str] | None = None,
               lo_q: float = 0.2, hi_q: float = 0.8) -> dict:
    """逐期 Q(hi)−Q(lo) 组合收益差; by 给定时在每个子样本内各自算。"""
    keys = ["period_end"] + (by or [])
    e = ev.filter(pl.col(col).is_not_null())
    if e.height < 300:
        return {"n": 0}
    e = e.with_columns(
        pl.col(col).rank().over(keys).alias("_rk"),
        pl.col(col).count().over(keys).alias("_n"))
    e = e.with_columns((pl.col("_rk") / pl.col("_n")).alias("_pct"))
    per = e.group_by(keys).agg([
        pl.col("ret").filter(pl.col("_pct") > hi_q).mean().alias("_hi"),
        pl.col("ret").filter(pl.col("_pct") <= lo_q).mean().alias("_lo"),
        pl.len().alias("_n"),
    ]).filter(pl.col("_n") >= 40)
    per = per.with_columns((pl.col("_hi") - pl.col("_lo")).alias("_sp"))
    sp = per["_sp"].to_numpy()
    sp = sp[np.isfinite(sp)]
    if len(sp) < 5:
        return {"n": int(len(sp))}
    return {"mean": round(float(sp.mean()) * 100, 2),
            "t": round(float(sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp)))), 2),
            "pos": round(float((sp > 0).mean()), 3), "n": int(len(sp))}


def part_j2(ev: pl.DataFrame) -> dict:
    """J2. 单变量多空 + 市值三档内多空 (排除「只是小盘」)。"""
    if ev.height < 500:
        return {}
    e = ev.filter(pl.col("size").is_not_null() & (pl.col("size") > 0))
    e = e.with_columns(
        pl.col("size").rank().over("period_end").alias("_sr"),
        pl.col("size").count().over("period_end").alias("_sn"))
    e = e.with_columns(
        pl.when(pl.col("_sr") / pl.col("_sn") < 1 / 3).then(pl.lit("小"))
        .when(pl.col("_sr") / pl.col("_sn") < 2 / 3).then(pl.lit("中"))
        .otherwise(pl.lit("大")).alias("size_grp"))
    out = {"feat": {}, "by_size": {}}
    for f in ("score", "cash_conv", "d_gm", "accrual", "capex_int", "z_roe"):
        out["feat"][f] = _spread_by(e, f)
        out["by_size"][f] = {g: _spread_by(e.filter(pl.col("size_grp") == g), f,
                                           by=["size_grp"])
                             for g in ("小", "中", "大")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    raw = load_fin()
    df = prep(raw)
    df_all = prep(raw, require_fwd=False)      # 含最新 4 期, 供「当前状态」用
    print(f"面板: {df.height:,} 行 / {df['symbol'].n_unique():,} 只 / "
          f"{df['period_end'].min()} ~ {df['period_end'].max()}")
    print(f"Q4 观测: {df.filter(pl.col('m')==12).height:,} 行 | "
          f"PIT 滞后中位 {df.select((pl.col('pit_date')-pl.col('period_end')).dt.total_days().median()).item():.0f} 天")

    res: dict = {}

    print("\n== A 基线: ROE 的均值回归有多强 (所有预测力的比较基准) ==")
    ra = part_a(df)
    for lab, v in ra.items():
        print(f"  {lab:<10} n={v['n_rows']:>7,} | IC(d_roe)={v['ic_d_roe'].get('ic')} "
              f"t={v['ic_d_roe'].get('t')}  IC(roe)={v['ic_roe'].get('ic')} t={v['ic_roe'].get('t')}")
        print(f"  {'':<10} 未来一年 ΔROE 均值 {v['d_roe_fwd_mean']:+.2f} pct, 中位 {v['d_roe_fwd_med']:+.2f}, "
              f"上升占比 {v['share_up']:.1%}")
    res["A"] = ra

    print("\n== B 单变量 IC (Q4 不重叠) 与控制当期 ROE/d_roe 后的增量 ==")
    print(f"  {'特征':<14}{'IC':>8}{'t':>7}{'正期占比':>9}   |{'增量IC':>8}{'t':>7}")
    rb = part_b(df)
    inv = {v: k for k, v in FEATURES.items()}
    for f, v in rb.items():
        a = v["raw"]
        b = v["incr_vs_roe_droe"]
        print(f"  {f:<14}{a.get('ic', 0):>8.4f}{a.get('t', 0):>7.2f}{a.get('pos', 0):>9.0%}   |"
              f"{b.get('ic', 0):>8.4f}{b.get('t', 0):>7.2f}")
    res["B"] = rb

    print("\n== C 分层 Q5−Q1 (未来 ΔROE, pct) ==")
    rc = part_c(df)
    for f, v in rc.items():
        print(f"  {f:<14} 期数 {v['n_periods']:>3}  差 {v['spread']:>+7.3f}  t={v['t']:>6.2f}  正期占比 {v['pos']:.0%}")
    res["C"] = rc

    print("\n== D1 时间错配负控 (滞后 4 期再用) ==")
    rd = part_d_placebo(df)
    for f, v in rd.items():
        s_, l_ = v["same_period"], v["lag4"]
        print(f"  {f:<12} 同期 IC={s_.get('ic', 0):>+7.4f}(t{s_.get('t', 0):>5.2f})  "
              f"滞后4期 IC={l_.get('ic', 0):>+7.4f}(t{l_.get('t', 0):>5.2f})  "
              f"保留 {l_.get('ic', 0)/s_.get('ic', 1e-9):>5.0%}" if s_.get('ic') else f"  {f:<12} 数据不足")
    res["D1"] = rd

    print("\n== D2 行业间 vs 行业内 (未来 ΔROE 的成分拆解) ==")
    re_ = part_e_split(df)
    print(f"  一级行业数 {re_['n_ind']}")
    print(f"  {'特征':<12}{'vs 总ΔROE':>12}{'vs 行业偏离':>14}{'vs 个股偏离':>14}")
    for f, v in re_["feat"].items():
        print(f"  {f:<12}{v['vs_total'].get('ic', 0):>12.4f}{v['vs_ind_dev'].get('ic', 0):>14.4f}"
              f"{v['vs_firm_dev'].get('ic', 0):>14.4f}")
    res["D2"] = re_

    print("\n== E 样本外 ==")
    rf = part_f_oos(df)
    spans = list(rf["feat"].keys())
    print(f"  切点 {rf['cut']}  ({rf['n_all']} 个 Q4 观测)")
    print(f"  {'特征':<12}" + "".join(f"{s:>22}" for s in spans))
    for f in rf["feat"][spans[0]]:
        line = f"  {f:<12}"
        for s in spans:
            v = rf["feat"][s][f]
            line += f"{v.get('ic', 0):>+12.4f}(t{v.get('t', 0):>5.2f})"
        print(line)
    res["E"] = rf

    print("\n== F 拐点命中率 (预测符号) ==")
    rg = part_g_hit(df)
    print(f"  基准率 (未来一年 ROE 上升占比) {rg['_base_rate']:.1%}, n={rg['_n']:,}")
    for k, v in rg.items():
        if k.startswith("_"):
            continue
        if "hit" in v:
            print(f"  {k:<14} n={v['n']:>7,}  命中 {v['hit']:.1%}  相对基准 {v['lift_pct']:+.1f} pct")
    res["F"] = rg

    print("\n== I 分子/分母分解 (d_eq 的负 IC 是不是纯算术?) ==")
    ri = part_i_decompose(df)
    print(f"  恒等式 ΔROE ≈ ROE·(g_NI − g_EQ) 与真实 ΔROE 相关 {ri['ident_corr']:.4f} "
          f"(n={ri['n_ident']:,})")
    print(f"  {'特征':<12}" + "".join(f"{v:>20}" for v in
          ("ΔROE", "利润增速", "净资产增速", "ΔROA")))
    for f, d in ri["ic"].items():
        line = f"  {f:<12}"
        for t in ("d_roe_fwd", "g_ni_fwd", "g_eq_fwd", "d_roa_fwd"):
            v = d[t]
            line += f"{v.get('ic', 0):>+10.4f}(t{v.get('t', 0):>5.1f})"
        print(line)
    print(f"  共线性: rank corr(nef, d_eq) = {ri['corr_nef_eq']} "
          f"⇒ 偏 IC 符号翻转不可单独解读")
    print(f"  horse race: nef|控制d_eq 增量 IC={ri['race_nef_ctrl_d_eq'].get('ic')} "
          f"(t={ri['race_nef_ctrl_d_eq'].get('t')})  |  "
          f"d_eq|控制nef 增量 IC={ri['race_d_eq_ctrl_nef'].get('ic')} "
          f"(t={ri['race_d_eq_ctrl_nef'].get('t')})")
    print("\n  按净外部融资强度分组 (%, 未来一年):")
    print(f"  {'档位':<16}{'n':>8}{'当前ROE':>9}{'利润增速':>10}{'净资产增速':>11}{'ΔROA':>8}{'ΔROE':>8}")
    for r in ri["by_nef"]:
        print(f"  {r['bucket']:<16}{r['n']:>8,}{r['roe']:>9.2f}{r['g_ni_fwd']:>10.2f}"
              f"{r['g_eq_fwd']:>11.2f}{r['d_roa_fwd']:>8.2f}{r['d_roe_fwd']:>8.2f}")
    res["I"] = ri

    print("\n== K 特征领先 ROE 拐点几个季度 (事件对齐, 横截面分位均值, 50=中位) ==")
    rk = part_k_lead(df_all)
    print(f"  拐点事件 {rk['n_events']:,} 个")
    if rk["rel"]:
        feats = [f for f in ("d_gm", "accrual", "cash_conv", "capex_int", "d_eq") if f in rk["rel"][0]]
        print(f"  {'相对期':<7}" + "".join(f"{f:>12}" for f in feats) + f"{'n':>9}")
        pl_ = rk.get("placebo", {})
        print("  (括号内为安慰剂: 随机非拐点日对齐)")
        for rel in sorted(rk["rel"]):
            row = rk["rel"][rel]
            prow = pl_.get(rel, {})
            cells = ""
            for f in feats:
                a, bq = row.get(f), prow.get(f)
                cells += f"{a:>7.1f}" + (f"({bq:.1f})" if bq is not None else "") + "  "
            print(f"  {rel:>+3}{'':<3}" + cells.ljust(60) + f"{row['n']:>8,}")
    res["K"] = rk

    print("\n== K2 分子端对照: 事件改为 ΔROA 由降转升 (分母换总资产, 削弱 d_eq 的算术耦合) ==")
    rk2 = part_k_lead(df_all, evcol="d_roa")
    print(f"  事件 {rk2['n_events']:,} 个")
    if rk2["rel"]:
        feats2 = [f for f in ("d_gm", "accrual", "cash_conv", "capex_int", "d_eq")
                  if f in rk2["rel"][0]]
        pl2 = rk2.get("placebo", {})
        print(f"  {'相对期':<6}" + "".join(f"{f:>14}" for f in feats2))
        for rel in sorted(rk2["rel"]):
            row, prow = rk2["rel"][rel], pl2.get(rel, {})
            cells = "".join(
                f"{row.get(f, 0):>8.1f}" + (f"({prow[f]:.1f})" if f in prow else "") + "  "
                for f in feats2)
            print(f"  {rel:>+3}{'':<3}" + cells)
    res["K2"] = rk2

    print("\n== G 行业层面 ==")
    rh = part_h_industry(df_all)
    print(f"  行业-期 {rh['n_rows']:,} 行 / {rh['n_ind']} 个一级行业")
    print(f"  {'特征':<12}{'Q4 IC':>9}{'t':>7}{'全季IC':>9}{'t':>7}")
    for f, v in rh["feat"]["q4"].items():
        a, b = v, rh["feat"]["all"][f]
        print(f"  {f:<12}{a.get('ic', 0):>9.4f}{a.get('t', 0):>7.2f}{b.get('ic', 0):>9.4f}{b.get('t', 0):>7.2f}")
    print(f"\n  -- 最新一期 ({rh['snapshot']['period_end']}) 各行业 --")
    print(f"  排序 = 分位(Δ毛利率) + 分位(现金含量) − 分位(净资产增速), 满分 300")
    print(f"  {'行业':<14}{'分数':>7}{'ROE':>7}{'ΔROE':>8}{'Δ毛利率':>9}{'OCF/营收':>9}"
          f"{'pct毛利':>9}{'pct现金':>9}{'pct净资产':>10}")
    for r in rh["snapshot"]["rows"][:18]:
        print(f"  {r['ind_l1']:<14}{r.get('score') or 0:>7.0f}{r.get('roe') or 0:>7.2f}"
              f"{(r.get('d_roe') or 0):>8.2f}{(r.get('d_gm_agg') or 0):>9.2f}"
              f"{(r.get('ocf_margin') or 0):>9.2f}{(r.get('pct_d_gm_agg') or 0):>9.0f}"
              f"{(r.get('pct_ocf_margin') or 0):>9.0f}{(r.get('pct_d_eq') or 0):>10.0f}")
    res["G"] = rh

    print("\n== J 合成分数 → 未来 250 交易日收益 ==")
    print("  分数 = +z(现金含量) + z(Δ毛利率) − z(应计) − z(资本开支强度); 只用 Q4, 不重叠")
    try:
        px = load_px()
        print(f"  前复权行情 {px.height:,} 行 / {px['symbol'].n_unique():,} 只 "
              f"({px['date'].min()} ~ {px['date'].max()})")
        rj = part_j_returns(df, px)
        print(f"  事件 {rj['n_events']:,} 个 / {rj['n_periods']} 期, "
              f"样本丢失 {rj['coverage']['drop_rate']:.1%} (退市/停牌)")
        if rj["mono"]:
            print(f"  {'组':<5}{'n':>8}{'平均收益':>10}{'胜期占比':>10}")
            for m in rj["mono"]:
                print(f"  {m['q']:<5}{m['n']:>8,}{m['ret']:>9.2f}%{m['win']:>10.0%}")
            print(f"  全市场基准 {rj['base']['all_ret']:+.2f}% (胜期 {rj['base']['win']:.0%})")
            if rj["spread"]:
                print(f"  多空 Q5−Q1 {rj['spread']['mean']:+.2f}%/250日 "
                      f"t={rj['spread']['t']} 正期占比 {rj['spread']['pos']:.0%} n={rj['spread']['n']}")
            c = rj["ctrl_low_roe_spread"]
            print(f"  负控: 只按 ROE 水平分组的多空 {c['mean']:+.2f}% (t={c['t']}) "
                  f"⇒ ROE 低者未来收益未必高")

            rj2 = part_j2(rj["_ev"])
            if rj2:
                print("\n  单变量多空 (Q5−Q1, 250日) 与市值三档内多空:")
                print(f"  {'特征':<12}{'全体':>16}{'小盘':>14}{'中盘':>14}{'大盘':>14}")
                for f, v in rj2["feat"].items():
                    line = f"  {f:<12}"
                    line += (f"{v.get('mean', 0):>+8.2f}(t{v.get('t', 0):>4.1f})"
                             if v.get("n") else f"{'-':>16}")
                    for g in ("小", "中", "大"):
                        w = rj2["by_size"][f][g]
                        line += (f"{w.get('mean', 0):>+8.2f}(t{w.get('t', 0):>4.1f})"
                                 if w.get("n") else f"{'-':>14}")
                    print(line)
                res["J2"] = rj2
            rj3 = part_j3(rj["_ev"], rh["_agg"])
            if rj3.get("ic"):
                print(f"\n  行业层面 (每年截面, {rj3['n_ind']} 个行业): 特征 → 次年行业收益 IC")
                for f, v in rj3["ic"].items():
                    print(f"    {f:<12} IC={v.get('ic', 0):>+7.4f} t={v.get('t', 0):>6.2f} "
                          f"期数={v.get('n')}")
                res["J3"] = rj3
        rj.pop("_ev", None)
        res["J"] = rj
    except Exception as e:
        print(f"  [跳过] 收益检验失败: {type(e).__name__}: {e}")

    if args.write:
        if "G" in res:
            res["G"].pop("_agg", None)      # DataFrame 不可序列化
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n结果已写入 {OUT.relative_to(ROOT)}")




# ---------------------------------------------------------------- J. 收益检验
QFQ = ROOT / "data" / "archive" / "kline_daily_qfq"


def load_px(y0: str = "2000-01-01") -> pl.DataFrame:
    """前复权日线 + 按标的的交易日序号 (用于「第 250 个交易日」而不是「250 天后」)。"""
    d = (
        pl.scan_parquet(QFQ / "**" / "*.parquet")
        .filter(pl.col("date") >= pl.lit(y0).str.to_date())
        .select(["symbol", "date", "close", "amount"])
        .collect()
        .filter(pl.col("close") > 0)
        .sort(["symbol", "date"])
        .with_columns(pl.int_range(0, pl.len()).over("symbol").alias("rn"))
        # 规模代理: 入场前 20 日均成交额 (shift(1) 保证入场时已知)
        .with_columns(
            pl.col("amount").rolling_mean(20).shift(1).over("symbol").alias("amt20")
        )
    )
    return d


def part_j_returns(df: pl.DataFrame, px: pl.DataFrame, hold: int = 250) -> dict:
    """J. 合成分数 → 未来收益。

    分数 = +z(cash_conv) + z(d_gm) − z(accrual) − z(capex_int)
    入场: PIT 可知日之后第一个交易日收盘; 出场: 第 `hold` 个交易日收盘。
    只用 Q4 观察 (不重叠)。
    """
    sub = df.filter(pl.col("m") == 12).select(
        ["symbol", "period_end", "pit_date", "cash_conv", "d_gm", "accrual",
         "capex_int", "roe", "d_eq", "d_roe_fwd"]
    ).drop_nulls(subset=["pit_date", "cash_conv", "d_gm", "accrual", "capex_int"])

    # 截面 rank → z, 再加权求和
    def z(c: str) -> pl.Expr:
        r = pl.col(c).rank().over("period_end")
        n = pl.col(c).count().over("period_end")
        return (r - (n + 1) / 2) / (n / 12 ** 0.5)      # ≈ 标准正态

    sub = sub.with_columns([
        (z("cash_conv") + z("d_gm") - z("accrual") - z("capex_int")).alias("score"),
        z("roe").alias("z_roe"),
    ])

    # 入场: pit_date 之后第一个有价日
    ev = sub.sort(["symbol", "pit_date"])
    p1 = px.select([pl.col("symbol"), pl.col("date").alias("entry_date"),
                    pl.col("close").alias("entry_px"), pl.col("rn").alias("rn_in"),
                    pl.col("amt20").alias("size")])
    ev = ev.join_asof(p1, left_on="pit_date", right_on="entry_date",
                      by="symbol", strategy="forward")
    # 出场: 第 hold 个交易日
    ev = ev.with_columns((pl.col("rn_in") + hold).alias("rn_out"))
    p2 = px.select([pl.col("symbol"), pl.col("rn").alias("rn_out"),
                    pl.col("close").alias("exit_px")])
    ev = ev.join(p2, on=["symbol", "rn_out"], how="left")
    ev = ev.filter(pl.col("entry_px").is_not_null() & pl.col("exit_px").is_not_null())
    ev = ev.with_columns((pl.col("exit_px") / pl.col("entry_px") - 1).alias("ret"))

    out = {"n_events": int(ev.height), "n_periods": int(ev["period_end"].n_unique()),
           "hold": hold, "mono": [], "spread": None, "base": None}
    if ev.height < 500:
        return out

    # 分 5 组 (按期截面), 看单调性
    for q in range(5):
        g = ev.filter(
            (pl.col("score").rank().over("period_end") >=
             pl.col("score").count().over("period_end") * q / 5)
            & (pl.col("score").rank().over("period_end") <
               pl.col("score").count().over("period_end") * (q + 1) / 5)
        )
        # 逐期均值再对期取平均 (避免大期主导)
        per = g.group_by("period_end").agg(pl.col("ret").mean().alias("r")).sort("period_end")
        out["mono"].append({
            "q": f"Q{q+1}", "n": int(g.height),
            "ret": round(float(per["r"].mean()) * 100, 2),
            "win": round(float((per["r"] > 0).mean()), 3),
            "n_periods": int(per.height),
        })
    # 多空: 逐期 Q5−Q1
    per = (
        ev.with_columns(pl.col("score").rank().over("period_end").alias("_rk"),
                        pl.col("score").count().over("period_end").alias("_n"))
        .with_columns((pl.col("_rk") / pl.col("_n")).alias("_pct"))
        .group_by(["period_end"])
        .agg([
            pl.col("ret").filter(pl.col("_pct") > 0.8).mean().alias("_hi"),
            pl.col("ret").filter(pl.col("_pct") <= 0.2).mean().alias("_lo"),
            pl.col("ret").mean().alias("_all"),
            pl.len().alias("_n"),
        ])
        .with_columns((pl.col("_hi") - pl.col("_lo")).alias("_sp"))
        .sort("period_end")
    )
    sp = per["_sp"].to_numpy()
    sp = sp[np.isfinite(sp)]
    out["spread"] = {"mean": round(float(sp.mean()) * 100, 2),
                     "t": round(float(sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp)))), 2),
                     "pos": round(float((sp > 0).mean()), 3), "n": int(len(sp))}
    out["base"] = {"all_ret": round(float(per["_all"].mean()) * 100, 2),
                   "win": round(float((per["_all"] > 0).mean()), 3)}
    # 负控: 只看 ROE 水平 (纯均值回归) 的多空
    per2 = (
        ev.with_columns(pl.col("z_roe").rank().over("period_end").alias("_rk"),
                        pl.col("z_roe").count().over("period_end").alias("_n"))
        .with_columns((pl.col("_rk") / pl.col("_n")).alias("_pct"))
        .group_by("period_end")
        .agg([pl.col("ret").filter(pl.col("_pct") > 0.8).mean().alias("_hi"),
              pl.col("ret").filter(pl.col("_pct") <= 0.2).mean().alias("_lo")])
        .with_columns((pl.col("_hi") - pl.col("_lo")).alias("_sp"))
    )
    sp2 = per2["_sp"].to_numpy()
    sp2 = sp2[np.isfinite(sp2)]
    out["ctrl_low_roe_spread"] = {
        "mean": round(float(sp2.mean()) * 100, 2),
        "t": round(float(sp2.mean() / (sp2.std(ddof=1) / np.sqrt(len(sp2)))), 2)}
    # 覆盖损失 (退市/停牌导致的样本丢失)
    out["coverage"] = {
        "events_all": int(sub.height), "events_priced": int(ev.height),
        "drop_rate": round(1 - ev.height / max(1, sub.height), 3),
    }
    out["_ev"] = ev          # 供 J2 复用, 落 json 前会剔除
    return out


if __name__ == "__main__":
    main()
