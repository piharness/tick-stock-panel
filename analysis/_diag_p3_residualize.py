"""第 3 阶段 · 第一步：归零检验（把温和 ramp 的超额对项目已有指标面板正交化）。

判据（执行前签字，不得事后改）：
  残差超额 < +20bp  或  t < 1.5      ⇒ 执行 A4，关闭整条线
  残差超额 ≥ +35bp  且  t ≥ 2.5      ⇒ 才值得占用一次预注册
  之间                                ⇒ 仍不部署，但保留为"未定"

口径：
  信号 = 第 2 阶段冻结规则（含 main board，18,9xx 条）
  超额 = 个股 ex15（d+1 开盘 → d+15 收盘）− 同日 basic_ok 等权同窗口
  控制 = 27 个盘前指标（4 静态 + 6 流动性/换手 + 7 波动 + 4 动量 + 6 微结构），
         全部只用 t 日及之前的数据，逐日 z 标准化（1/99 缩尾）
  估计 = 逐日横截面 OLS：ex15 ~ 1 + 控制 + signal_dummy，Fama-MacBeth 平均，
         NW(15) t。系数对控制变量的仿射变换不变，故 z 化不改变 alpha。

  stage=indicators  生成全主板日频指标面板（2017 起，用于 250 日回看）
  stage=table       与 basic_ok / ex15 合并，落盘回归表
  stage=regress     分块累积正交化 + 行业中性 + 分年

复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_residualize.py --stage all
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / ".scratch" / "p3"
ENR = DATA / "kline_daily_enriched" / "*" / "part.parquet"
SHARES = DATA / "backfill_20261117" / "fin_shares.parquet"
INSTR = DATA / "instruments" / "instruments.parquet"
INDUSTRY = DATA / "ext_data" / "ext_hy_ths" / "part.parquet"
REPLAY = DATA / ".scratch" / "footprint20d" / "replay_daily.parquet"
SIGNALS = DATA / ".scratch" / "p2audit" / "signals_enriched.parquet"

MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
W = 20
H = 15

STATIC = ["log_mcap", "log_price", "age_years", "is_sh"]
LIQ = ["turn20", "turn_chg", "turn_cv20", "log_amt20", "log_amihud", "amt_chg"]
VOL = ["vol20", "skew20", "max_ret20", "ar1_20", "range20", "range_vol_ratio", "idio_vol60"]
MOM = ["ret5", "ret20", "dist_high250", "beta60"]
MICRO = ["limit_up_cnt20", "quiet_cnt20", "corr_pv", "upvol_ratio", "overnight20", "intraday20"]
BLOCKS = [("static", STATIC), ("liq", LIQ), ("vol", VOL), ("mom", MOM), ("micro", MICRO)]
ALLCTRL = STATIC + LIQ + VOL + MOM + MICRO


def nw_t(x, lag=15):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 10:
        return float("nan"), float("nan")
    mu = x.mean()
    e = x - mu
    v = e @ e / n
    for l in range(1, lag + 1):
        v += 2 * (1 - l / (lag + 1)) * (e[l:] @ e[:-l] / n)
    return float(mu), float(mu / np.sqrt(max(v, 0) / n))


def stage_indicators() -> None:
    lf = pl.scan_parquet(ENR).filter(pl.col("date") >= pl.date(2016, 6, 1))
    df = (
        lf.select(
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "raw_close", "turnover_rate", "consecutive_limit_ups",
        )
        .filter(pl.col("symbol").str.slice(0, 3).is_in(MAIN))
        .filter(pl.col("amount").is_not_null() & (pl.col("amount") > 0))
        .collect()
        .sort(["symbol", "date"])
    )
    sh = (
        pl.read_parquet(SHARES)
        .with_columns(pl.col("end_date").cast(pl.Date))
        .with_columns(pl.coalesce(pl.col("listed_a_shares"), pl.col("unlimited_shares"),
                                  pl.col("total_shares")).alias("float_shares"))
        .filter(pl.col("float_shares") > 0)
        .select("symbol", "end_date", "float_shares")
        .sort(["symbol", "end_date"])
    )
    df = df.sort(["symbol", "date"]).join_asof(
        sh, left_on="date", right_on="end_date", by="symbol", strategy="backward")

    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret"),
        (pl.col("close") / pl.col("open") - 1).alias("intraday"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("range"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int8).alias("is_limit_up"),
    )
    df = df.with_columns(
        pl.col("ret").abs().alias("absret"),
        (pl.col("ret").abs() < 0.01).cast(pl.Int8).alias("is_quiet"),
        pl.when(pl.col("ret").abs() > 0.25).then(None).otherwise(pl.col("ret")).alias("ret_clean"),
    )
    mkt = df.group_by("date").agg(pl.col("ret_clean").mean().alias("mkt_ret"))
    df = df.join(mkt, on="date", how="left").sort(["symbol", "date"])

    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("ret5"),
        pl.col("ret_clean").rolling_std(W, min_samples=15).over("symbol").alias("vol20"),
        pl.col("ret_clean").rolling_skew(W, min_samples=15).over("symbol").alias("skew20"),
        pl.col("ret_clean").rolling_max(W, min_samples=15).over("symbol").alias("max_ret20"),
        pl.rolling_corr(pl.col("ret_clean"), pl.col("ret_clean").shift(1),
                        window_size=W, min_samples=15).over("symbol").alias("ar1_20"),
        pl.col("turnover_rate").rolling_mean(W, min_samples=15).over("symbol").alias("turn20"),
        (pl.col("turnover_rate").rolling_mean(5, min_samples=4).over("symbol")
         / pl.col("turnover_rate").rolling_mean(W, min_samples=15).over("symbol") - 1).alias("turn_chg"),
        pl.col("turnover_rate").rolling_std(W, min_samples=15).over("symbol").alias("turn_std20"),
        pl.col("amount").rolling_mean(W, min_samples=15).over("symbol").alias("amt20"),
        (pl.col("amount").rolling_mean(5, min_samples=4).over("symbol")
         / pl.col("amount").rolling_mean(W, min_samples=15).over("symbol") - 1).alias("amt_chg"),
        (pl.col("absret") / (pl.col("amount") / 1e8)).rolling_mean(W, min_samples=15).over("symbol").alias("amihud"),
        (pl.when(pl.col("ret_clean") > 0).then(pl.col("volume")).otherwise(0))
        .rolling_sum(W, min_samples=15).over("symbol").alias("up_vol"),
        (pl.when(pl.col("ret_clean") < 0).then(pl.col("volume")).otherwise(0))
        .rolling_sum(W, min_samples=15).over("symbol").alias("dn_vol"),
        pl.rolling_corr(pl.col("ret_clean"), pl.col("amount").pct_change(),
                        window_size=W, min_samples=15).over("symbol").alias("corr_pv"),
        pl.col("range").rolling_mean(W, min_samples=15).over("symbol").alias("range20"),
        pl.col("is_limit_up").rolling_sum(W, min_samples=15).over("symbol").alias("limit_up_cnt20"),
        pl.col("is_quiet").rolling_sum(W, min_samples=15).over("symbol").alias("quiet_cnt20"),
        pl.col("intraday").rolling_mean(W, min_samples=15).over("symbol").alias("intraday20"),
        (pl.col("close") / pl.col("high").rolling_max(250, min_samples=200).over("symbol") - 1).alias("dist_high250"),
        (pl.rolling_corr(pl.col("ret_clean"), pl.col("mkt_ret"), window_size=60, min_samples=40).over("symbol")
         * pl.col("ret_clean").rolling_std(60, min_samples=40).over("symbol")
         / pl.col("mkt_ret").rolling_std(60, min_samples=40).over("symbol")).alias("beta60"),
    )
    df = df.with_columns(
        pl.col("ret_clean").rolling_std(60, min_samples=40).over("symbol").alias("vol60"),
    ).with_columns(
        (pl.col("vol60") ** 2 - (pl.col("beta60") ** 2)
         * pl.col("mkt_ret").rolling_var(60, min_samples=40).over("symbol")).clip(lower_bound=0).sqrt().alias("idio_vol60"),
        (pl.col("up_vol") / pl.col("dn_vol").clip(lower_bound=1)).log().alias("upvol_ratio"),
        pl.col("amihud").log1p().alias("log_amihud"),
        pl.col("amt20").log().alias("log_amt20"),
        (pl.col("raw_close") * pl.col("float_shares")).log().alias("log_mcap"),
        pl.col("raw_close").log().alias("log_price"),
        (pl.col("turn_std20") / pl.col("turn20")).alias("turn_cv20"),
        (pl.col("range20") / (pl.col("vol20") + 1e-9)).alias("range_vol_ratio"),
        (pl.col("ret") - pl.col("ret_clean")).alias("overnight_placeholder"),
    )

    ins = (pl.read_parquet(INSTR).select("symbol", "listing_date", "name")
           .with_columns(pl.col("listing_date").cast(pl.Date)))
    df = df.join(ins, on="symbol", how="left").with_columns(
        ((pl.col("date") - pl.col("listing_date")).dt.total_days() / 365.25).alias("age_years"),
        pl.col("symbol").str.ends_with(".SH").cast(pl.Int8).alias("is_sh"),
    )
    hy = (pl.read_parquet(INDUSTRY)
          .select("symbol", pl.col("所属同花顺行业").str.split("-").list.first().alias("industry"))
          .unique(subset=["symbol"]))
    df = df.join(hy, on="symbol", how="left").with_columns(pl.col("industry").fill_null("未分类"))

    # overnight: 隔夜跳空均值（用 open/prev close）
    df = df.with_columns(
        (pl.col("open") / pl.col("close").shift(1).over("symbol") - 1).alias("gap_raw")
    ).with_columns(
        pl.when(pl.col("gap_raw").abs() > 0.25).then(None).otherwise(pl.col("gap_raw")).alias("gap_clean")
    ).with_columns(
        pl.col("gap_clean").rolling_mean(W, min_samples=15).over("symbol").alias("overnight20")
    )

    keep = ["symbol", "date", "industry"] + ALLCTRL
    out = df.filter(pl.col("date") >= pl.date(2018, 1, 1)).select(
        [pl.col(c).cast(pl.Float32) if c in ALLCTRL else pl.col(c) for c in keep]
    )
    out.write_parquet(OUT / "indicators_all.parquet")
    print(f"indicators: {out.height:,} rows, {len(ALLCTRL)} controls")


def stage_table() -> None:
    rd = pl.read_parquet(REPLAY, columns=["symbol", "date", "open", "close", "basic_ok"])
    cal = rd.select("date").unique().sort("date").with_row_index("di")
    cal = cal.with_columns((pl.col("di") + 1).alias("enter_di"), (pl.col("di") + H).alias("exit_di"))
    # 键必须是原始 di：enter_di/exit_di 的值是目标行的 di
    e = cal.select(pl.col("di").alias("enter_di"), pl.col("date").alias("entry_date"))
    x = cal.select(pl.col("di").alias("exit_di"), pl.col("date").alias("exit_date"))
    cal = cal.join(e, on="enter_di", how="left").join(x, on="exit_di", how="left")
    rd = rd.join(cal.select("date", "entry_date", "exit_date"), on="date", how="left")

    px = rd.select("symbol", "date", "open", "close")
    rd = rd.join(px.rename({"date": "entry_date", "open": "entry_open"}),
                 on=["symbol", "entry_date"], how="left")
    rd = rd.join(px.rename({"date": "exit_date", "close": "exit_close"}),
                 on=["symbol", "exit_date"], how="left")
    rd = rd.with_columns((pl.col("exit_close") / pl.col("entry_open") - 1).alias("fwd"))

    # 基准：同日 basic_ok 等权
    bench = (rd.filter(pl.col("basic_ok") == 1)
             .group_by("date").agg(pl.col("fwd").mean().alias("bench")))
    rd = rd.join(bench, on="date", how="left").with_columns((pl.col("fwd") - pl.col("bench")).alias("ex15"))

    sig = (pl.read_parquet(SIGNALS, columns=["date", "symbol"]).unique()
           .with_columns(pl.col("date").cast(pl.Date)))
    sig = sig.with_columns(pl.lit(1).cast(pl.Int8).alias("sig"))

    ind = pl.read_parquet(OUT / "indicators_all.parquet")
    tab = (rd.filter(pl.col("basic_ok") == 1)
           .join(sig, on=["date", "symbol"], how="left")
           .with_columns(pl.col("sig").fill_null(0))
           .join(ind, on=["date", "symbol"], how="left")
           .filter(pl.col("ex15").is_finite()))
    tab.write_parquet(OUT / "resid_table.parquet")
    n_sig = tab.filter(pl.col("sig") == 1).height
    print(f"table: {tab.height:,} rows, signals {n_sig:,}, dates {tab.select('date').n_unique():,}")


def _arrays(tab: pl.DataFrame, extra=None):
    """把回归表转成 numpy：日期码、y、signal 哑变量、控制矩阵、行业码。"""
    cols = ["date", "sig", "industry", "ex15"] + ALLCTRL
    pdf = tab.select(cols).to_pandas()
    y = pdf["ex15"].to_numpy(float)
    sig = pdf["sig"].to_numpy(float)
    dates = pdf["date"].to_numpy()
    Z = pdf[ALLCTRL].to_numpy(float)
    ind = pd.factorize(pdf["industry"].astype(str))[0]
    order = np.argsort(dates, kind="stable")
    bounds = np.flatnonzero(np.r_[True, dates[order][1:] != dates[order][:-1]])
    groups = np.split(order, bounds[1:])
    return {"y": y, "sig": sig, "Z": Z, "ind": ind, "groups": groups, "dates": dates}


def _fm(dat: dict, ctrls: list[str]) -> dict:
    n_ctrl = len(ctrls)
    pos = [ALLCTRL.index(c) for c in ctrls]
    alphas, ns = [], []
    for gi in dat["groups"]:
        yy = dat["y"][gi]
        ss = dat["sig"][gi]
        if ss.sum() < 1 or (1 - ss).sum() < 100:
            continue
        cols = [np.ones(gi.size), ss]
        if n_ctrl:
            Z = dat["Z"][gi][:, pos]
            mu = np.nanmean(Z, axis=0)
            sd = np.nanstd(Z, axis=0)
            sd = np.where(sd > 1e-12, sd, 1.0)
            Z = np.clip((Z - mu) / sd, -3, 3)
            Z = np.where(np.isfinite(Z), Z, 0.0)
            cols.append(Z)
        X = np.column_stack(cols)
        beta, *_ = np.linalg.lstsq(X, yy, rcond=None)
        alphas.append(beta[1])
        ns.append(int(ss.sum()))
    a = np.array(alphas)
    mu, t = nw_t(a)
    return {"bp": mu * 1e4, "t": t, "days": int(a.size), "signals": int(sum(ns)),
            "per_signal_bp": float(np.average(a, weights=ns) * 1e4)}


def stage_regress() -> None:
    tab = pl.read_parquet(OUT / "resid_table.parquet")
    tab = tab.filter(pl.col("date") >= pl.date(2018, 9, 1))
    res = {"n_rows": tab.height, "n_sig": int(tab.filter(pl.col("sig") == 1).height),
           "dates": int(tab.select("date").n_unique()), "blocks": []}

    dat = _arrays(tab)
    base = _fm(dat, [])
    res["blocks"].append({"spec": "no_control", **base})
    print(f"{'no_control':>10s} (     total  0): {base['bp']:8.1f}bp  t={base['t']:5.2f}  days={base['days']}")
    acc: list[str] = []
    for name, cols in BLOCKS:
        acc = acc + cols
        r = _fm(dat, acc)
        r["spec"] = name
        r["n_controls"] = len(acc)
        res["blocks"].append(r)
        print(f"{name:>10s} (+{len(cols):2d}, total {len(acc):2d}): "
              f"{r['bp']:8.1f}bp  t={r['t']:5.2f}  days={r['days']}")

    # 单控制逐项：哪一个特征单独就把超额吃掉
    single = []
    for c in ALLCTRL:
        r = _fm(dat, [c])
        r["control"] = c
        single.append(r)
    res["single_control"] = sorted(single, key=lambda d: d["bp"])
    print("\n单控制（按残差从低到高，前 8）：")
    print(pd.DataFrame(res["single_control"][:8])[["control", "bp", "t"]].round(2).to_string(index=False))

    # 行业中性：同日同行业去均值后再跑无控制版本
    g = (tab.with_columns(pl.col("ex15").mean().over(["date", "industry"]).alias("ind_mean"))
         .with_columns((pl.col("ex15") - pl.col("ind_mean")).alias("ex15")))
    dat_ind = _arrays(g)
    res["industry_neutral"] = _fm(dat_ind, [])
    print("industry_neutral:", {k: round(v, 2) if isinstance(v, float) else v
                                for k, v in res["industry_neutral"].items()})

    # 分年（全控制）
    yearly = []
    for yr in range(2018, 2027):
        sub = tab.filter(pl.col("date").dt.year() == yr)
        if sub.height < 1000:
            continue
        r = _fm(_arrays(sub), ALLCTRL)
        r["year"] = yr
        yearly.append(r)
    res["yearly_full_control"] = yearly
    print(pd.DataFrame(yearly).round(1).to_string(index=False))

    # 缺失率（诊断）
    res["nan_share"] = {c: float(tab.select(pl.col(c).is_null().mean()).item()) for c in ALLCTRL}
    json.dump(res, open(OUT / "residualize.json", "w"), ensure_ascii=False, indent=1)
    print("\n" + json.dumps({k: v for k, v in res.items() if k != "blocks"}, ensure_ascii=False)[:600])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["indicators", "table", "regress", "all"])
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.stage in ("indicators", "all"):
        stage_indicators()
    if a.stage in ("table", "all"):
        stage_table()
    if a.stage in ("regress", "all"):
        stage_regress()


if __name__ == "__main__":
    main()
