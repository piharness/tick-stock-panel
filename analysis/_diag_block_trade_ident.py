"""大宗折价率到底在标什么价：把「筹码位置」与「接盘方身份」拆开。

问题
----
`block-trade-source.md` 把大宗让价当作熵的市场标价。但同一只股票的同一天，
筹码分布完全相同（熵相同），若同日内不同买方类型的折价仍然差很多，
那这个差就只能归因于「谁在接盘」，而不能归因于「筹码在哪儿」。

设计（三刀，一刀比一刀硬）
--------------------------
1. 方差分解：premium_dod 的总方差拆成 组间(date×symbol) 与 组内(同股票同日不同笔)。
   组内那一块，「筹码位置」是常量 ⇒ 它 100% 由非熵因素产生。
   ⇒ 组内方差占比 = 折价率**不能**被熵解释的下界。

2. 组内回归（within estimator）：只在组内有类型变异的样本上，
   用 buyer/seller 是否机构解释组内折价差。截距与个股固定效应已被 demean 掉。

3. 收敛效度：折价率与事前 20 日振幅/换手/日内宽度 (ivd_lo) 的相关。
   若「折价=熵标价」成立，这一相关应当显著为正。相关若接近 0，
   它标的是制度摩擦，不是筹码宽度。

用法:
  python analysis/_diag_block_trade_ident.py
输出:
  data/.scratch/block_trade_ident.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "data" / "block_trade"
KL = ROOT / "data" / "kline_daily_enriched"
ENT = ROOT / "data" / ".scratch" / "chip_entropy" / "part.parquet"
OUT = ROOT / "data" / ".scratch" / "block_trade_ident.json"
START, END = "2008-01-01", "2017-12-31"


def load_deals() -> pl.DataFrame:
    files = [p for p in sorted(BT.glob("year=*/part.parquet")) if "premium_dod" in
             pl.read_parquet_schema(p)]
    # 各年 amt/vol 类型不一致（Int64 vs Float64），先统一再 concat
    frames = []
    for p in files:
        t = pl.read_parquet(p, columns=["date", "symbol", "premium_dod", "amt", "vol",
                                        "buyer", "seller", "sec_type", "market",
                                        "pre_close"])
        frames.append(t.with_columns(
            pl.col("amt").cast(pl.Float64), pl.col("vol").cast(pl.Float64),
            pl.col("premium_dod").cast(pl.Float64),
            pl.col("pre_close").cast(pl.Float64),
        ))
    d = pl.concat(frames).filter(
        (pl.col("sec_type") == "EQA")
        & (pl.col("market") != "CNSEBJ")
        & pl.col("premium_dod").is_not_null()
        & (pl.col("pre_close") > 0)
        & (pl.col("amt") > 0)
        & (pl.col("premium_dod").abs() <= 0.21)   # 剔出笼子外的盘后定价/脏行
    ).select(
        ["date", "symbol", "premium_dod", "amt", "vol", "buyer", "seller"]
    )
    d = d.with_columns(
        pl.col("date").cast(pl.Date),
        pl.col("buyer").str.contains("机构专用").cast(pl.Int8).alias("bi"),
        pl.col("seller").str.contains("机构专用").cast(pl.Int8).alias("si"),
    ).filter(pl.col("date").is_between(pl.lit(START).str.to_date(),
                                       pl.lit(END).str.to_date()))
    return d


def var_decomp(d: pl.DataFrame) -> dict:
    g = d.group_by(["date", "symbol"]).agg(
        pl.col("premium_dod").mean().alias("gm"),
        pl.col("premium_dod").std().alias("gsd"),
        pl.len().alias("n"),
    )
    tot = d["premium_dod"].std() ** 2
    # 组间方差：按笔数加权（等价于把每组均值重复 n 次）
    gd = g.filter(pl.col("n") > 1)
    between = float(
        ((gd["gm"] - d["premium_dod"].mean()) ** 2 * gd["n"]).sum() / gd["n"].sum()
    )
    within = float(
        ((gd["gsd"].fill_null(0.0) ** 2) * (gd["n"] - 1)).sum()
        / (gd["n"] - 1).sum()
    )
    return {
        "deals": d.height,
        "groups": g.height,
        "groups_gt1": gd.height,
        "share_single": round(1 - gd.height / g.height, 4),
        "var_total": round(tot, 6),
        "var_between": round(between, 6),
        "var_within": round(within, 6),
        "within_share": round(within / (between + within), 4),
    }


def within_reg(d: pl.DataFrame) -> dict:
    g = d.group_by(["date", "symbol"]).agg(
        pl.col("bi").std().alias("bi_sd"), pl.col("si").std().alias("si_sd"),
    )
    ids = g.filter((pl.col("bi_sd") > 0) | (pl.col("si_sd") > 0)).select(
        ["date", "symbol"])
    s = d.join(ids, on=["date", "symbol"], how="inner")
    if s.height < 200:
        return {"n": s.height, "note": "识别样本不足"}
    for c in ["premium_dod", "bi", "si"]:
        s = s.with_columns(
            (pl.col(c) - pl.col(c).mean().over(["date", "symbol"])).alias(c + "_d"))
    X = np.column_stack([s["bi_d"].to_numpy(), s["si_d"].to_numpy()])
    y = s["premium_dod_d"].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - 2, 1)
    s2 = float(resid @ resid) / dof
    cov = s2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    # 组内解释力：R²（已去组均值）
    ss_tot = float(y @ y)
    r2 = 1 - float(resid @ resid) / ss_tot if ss_tot > 0 else None
    return {
        "n": s.height,
        "groups": int(s.select(["date", "symbol"]).unique().height),
        "beta_buyer_inst": round(float(beta[0]), 5),
        "t_buyer_inst": round(float(beta[0] / se[0]), 2),
        "beta_seller_inst": round(float(beta[1]), 5),
        "t_seller_inst": round(float(beta[1] / se[1]), 2),
        "within_r2": round(r2, 4) if r2 is not None else None,
    }


def convergence(d: pl.DataFrame) -> dict:
    """折价率 × 事前宽度/振幅/换手：若折价=熵标价，相关应为正且不弱。"""
    files = [str(p) for p in sorted(KL.glob("date=*/part.parquet"))]
    px = pl.scan_parquet(files, extra_columns="ignore").select(
        ["symbol", "date", "high", "low", "raw_close", "turnover_rate"]
    ).filter(pl.col("date").is_between(pl.lit(START).str.to_date(),
                                       pl.lit(END).str.to_date())).collect()
    px = px.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("raw_close")).alias("amp")
    ).sort(["symbol", "date"]).with_columns(
        pl.col("amp").rolling_mean(20).over("symbol").alias("amp20"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("to20"),
    )
    s = d.group_by(["date", "symbol"]).agg(
        ((pl.col("premium_dod") * pl.col("amt")).sum() / pl.col("amt").sum()).alias("prem")
    ).join(px, on=["symbol", "date"], how="inner").drop_nulls(["amp20", "to20"])
    out = {"n": s.height}
    for c in ["amp20", "to20"]:
        out[f"spearman_{c}"] = round(float(
            pl.DataFrame({"a": s["prem"].rank(), "b": s[c].rank()}).select(
                pl.corr("a", "b")).item()), 4)
    out["prem_median"] = round(float(s["prem"].median()), 4)
    return out


def main() -> None:
    d = load_deals()
    print(f"大宗成交 {d.height:,} 笔  {d['date'].min()} → {d['date'].max()}")
    rep = {
        "period": [str(d["date"].min()), str(d["date"].max())],
        "variance_decomposition": var_decomp(d),
        "within_regression": within_reg(d),
        "convergence_with_entropy_proxies": convergence(d),
    }
    # 四类买方×卖方中位数（复算，确认陷阱三在长样本仍成立）
    d2 = d.with_columns((pl.col("bi").cast(pl.String) + "/" + pl.col("si").cast(pl.String)).alias("cell"))
    rep["cell_median"] = [
        {"cell": r["cell"], "n": r["len"], "med": round(r["med"], 4)}
        for r in d2.group_by("cell").agg(
            pl.len(), pl.col("premium_dod").median().alias("med")).sort("cell").to_dicts()
    ]
    for k, v in rep.items():
        print(f"\n[{k}]")
        print(json.dumps(v, ensure_ascii=False, indent=2))
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
