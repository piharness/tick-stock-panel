"""H5：网络中心度 → 尾部传染。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
对象
  中心度 = 共同持有人网络的暴露：
    shared_score      = Σ_h (deg_h − 1) · ratio_{h,i}     （该股被多少「到处持仓」的持有人持有）
    n_shared_holders  = 持有度 >1 的持有人个数
    max_holder_degree = 最大持有人持有度
  持有人 = top_float 前十大流通股东，剔除托管/结算/证金/外资经纪类账户（否则网络退化成
  「所有股票都连着香港中央结算」）。

机制
  共同持有人 → 一个持仓亏损 → 组合赎回/止损 → 卖其他持仓。中心度高的股票是系统性持有人的交叉点，
  因此在市场崩溃时应「跌得更一致」——**下行贝塔 > 平均贝塔**（CoVaR 式的尾部共动），
  而不是整体波动更高。

判据（三层，逐层更强）
  L1 第二维度：中心度与久期 D、换手、波动、规模的 |秩相关| 应有一个独立轴。
      若中心度只是规模/换手的代理（|ρ|>0.6），直接杀死。
  L2 尾部共动：tail_excess = β⁻ − β（下行市场贝塔 − 全样本贝塔），
      截面回归 tail_excess ~ z(中心度) + z(规模)+z(波动)+z(换手)+z(D) + 季度固定效应。
      判据：中心度系数 > 0 且 |t| ≥ 2；同时中心度对「平均 β」不显著（否则只是 β）。
  L3 置换：把中心度在同规模十分位内跨股票置换，重跑 L2；真值必须显著高于置换分布。
      另跑网络版 tail_excess_net（对共同持有人组合的下行贝塔），机制检验。

杀死
  L1 中心度只是规模代理；或 L2 不显著；或 L2 只对平均 β 显著（只是 β，不是传染）；或 L3 置换后不变。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h5"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"
HOLDER = ROOT / "data" / ".scratch" / "chip_age" / "holder_panel.parquet"
TOP_FLOAT = ROOT / "data" / "shareholder" / "top_float" / "part.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"

WINDOW = 250
NW_LAG = 6
N_PLACEBO = 40
NOMINEE_PAT = (
    "香港中央结算|HKSCC|中央结算|中国证券金融|中央汇金|证金公司|"
    "高盛|UBS|MORGAN|J.P.|JPMORGAN|BARCLAYS|MERRILL|CITIGROUP|"
    "STATE STREET|BNP|DEUTSCHE|CREDIT SUISSE|UBS AG|"
    "中信证券资产管理(香港)|客户资金"
)


def load_daily(start: str = "2018-01-01"):
    dataset = ds.dataset(SRC, format="parquet")
    table = dataset.to_table(
        columns=["symbol", "date", "close", "amount", "turnover_rate"],
        filter=ds.field("date") >= pd.Timestamp(start).date(),
    )
    df = table.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last").sort_index()
    am = am.reindex(cl.index)
    to = to.reindex(cl.index)
    return cl, am, to


def nw_t(x, lag: int = NW_LAG) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return np.nan
    e = x - x.mean()
    s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def down_beta(r: np.ndarray, m: np.ndarray):
    """r: (T,) 个股收益；m: (T,) 市场收益（原始）。返回 (beta, beta_down)。"""
    rm = m - np.nanmean(m)
    ri = r - np.nanmean(r)
    var = np.nansum(rm * rm)
    beta = np.nansum(ri * rm) / var if var > 0 else np.nan
    d = m < 0
    if d.sum() < 30:
        return beta, np.nan
    md = m[d] - np.nanmean(m[d])
    rd = r[d] - np.nanmean(r[d])
    vard = np.nansum(md * md)
    beta_d = np.nansum(rd * md) / vard if vard > 0 else np.nan
    return beta, beta_d


def main() -> None:
    print("[1/5] load daily ...", flush=True)
    cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(cl_df.index)
    syms = np.asarray(cl_df.columns)
    C = cl_df.to_numpy(dtype=np.float64)
    AM = am_df.to_numpy(dtype=np.float64)
    TO = to_df.to_numpy(dtype=np.float64)
    R = np.full_like(C, np.nan)
    R[1:] = C[1:] / C[:-1] - 1.0
    mkt = np.nanmean(R, axis=1)
    n, k = C.shape
    print(f"      {n} x {k}", flush=True)

    print("[2/5] centrality + age D ...", flush=True)
    hp = pd.read_parquet(HOLDER)
    hp["period_end"] = pd.to_datetime(hp["period_end"]).astype("datetime64[ns]")
    hp["symbol"] = hp["symbol"].astype(str)
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    ag = age[["date", "symbol", "D"]].sort_values("date")
    ag["symbol"] = ag["symbol"].astype(str)

    quarters = sorted(hp["period_end"].unique())
    print(f"      quarters={[q.date() for q in quarters]}", flush=True)

    rows = []
    for Q in quarters:
        # 窗口：Q 前 250 个交易日
        pos = np.searchsorted(dates.values, np.datetime64(Q, "ns"))
        if pos < WINDOW:
            continue
        w = slice(pos - WINDOW, pos)
        Rw = R[w]
        mw = mkt[w]
        # 市场 beta / downside beta
        beta = np.full(k, np.nan)
        bdn = np.full(k, np.nan)
        for j in range(k):
            rj = Rw[:, j]
            if np.isfinite(rj).sum() < 100:
                continue
            beta[j], bdn[j] = down_beta(rj, mw)
        # 控制：规模/波动/换手（窗口内）
        size = np.log(np.nanmean(AM[w], axis=0) + 1.0)
        vol = np.nanstd(Rw, axis=0)
        turn = np.nanmean(TO[w], axis=0)
        # 中心度（asof Q）
        hpq = hp[hp["period_end"] == Q][["symbol", "shared_score", "n_shared_holders", "max_holder_degree"]].copy()
        # D asof Q
        agq = ag[ag["date"] <= Q].sort_values("date").groupby("symbol").tail(1)
        df = pd.DataFrame({"symbol": syms, "beta": beta, "beta_down": bdn,
                           "size": size, "vol": vol, "turn": turn})
        df["symbol"] = df["symbol"].astype(str)
        df = df.merge(hpq, on="symbol", how="inner").merge(agq[["symbol", "D"]], on="symbol", how="left")
        df["tail_excess"] = df["beta_down"] - df["beta"]
        df["Q"] = Q
        rows.append(df)
    allx = pd.concat(rows, ignore_index=True)
    allx = allx.dropna(subset=["tail_excess", "shared_score", "size", "vol", "turn", "D", "beta"])
    allx["Qnum"] = allx["Q"].astype("int64")
    allx.to_parquet(OUT / "panel.parquet", index=False)
    print(f"      panel {len(allx):,} rows, {allx['Q'].nunique()} quarters", flush=True)

    # L1 正交性
    corr = {}
    for b in ["D", "turn", "vol", "size", "n_shared_holders", "max_holder_degree"]:
        x = allx[["shared_score", b]].dropna()
        corr[f"shared_score_vs_{b}"] = round(float(x.corr(method="spearman").iloc[0, 1]), 3)

    # L2 截面回归（季度固定效应）
    def run_reg(d):
        d = d.copy()
        d["qfe"] = d["Qnum"].astype("category")
        dummies = pd.get_dummies(d["qfe"], prefix="q", drop_first=True).astype(float)
        X = np.column_stack([
            zscore(d["shared_score"]), zscore(d["size"]), zscore(d["vol"]), zscore(d["turn"]), zscore(d["D"]),
            dummies.to_numpy(),
        ])
        y = zscore(d["tail_excess"])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        return b[0], b[1], b[2], b[3], b[4]

    b_cent, b_size, b_vol, b_turn, b_D = run_reg(allx)
    # 平均 beta 也跑一遍（kill 检查）
    b_cent_beta, *_rest = run_reg(allx.assign(tail_excess=allx["beta"]))

    # 每季度系数（稳健性）
    qcoef = []
    for Q, g in allx.groupby("Qnum"):
        if len(g) < 300:
            continue
        g = g.copy()
        X = np.column_stack([zscore(g["shared_score"]), zscore(g["size"]), zscore(g["vol"]), zscore(g["turn"]), zscore(g["D"]), np.ones(len(g))])
        y = zscore(g["tail_excess"])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        qcoef.append({"Q": Q, "b_cent": b[0], "n": int(ok.sum())})
    qc = pd.DataFrame(qcoef)

    # L3 置换：同规模十分位内置换 shared_score
    rng = np.random.default_rng(20261126)
    allx["sdec"] = allx.groupby("Qnum")["size"].transform(lambda x: pd.qcut(x.rank(method="first"), 10, labels=False))
    placebo = []
    for _ in range(N_PLACEBO):
        shuf = allx.copy()
        shuf["shared_score"] = shuf.groupby(["Qnum", "sdec"])["shared_score"].transform(
            lambda x: rng.permutation(x.to_numpy()))
        b, *_rest = run_reg(shuf)
        placebo.append(b)
    placebo = np.asarray(placebo)

    summary = {
        "sample": {
            "rows": int(len(allx)), "quarters": int(allx["Qnum"].nunique()),
            "symbols_per_quarter": int(allx.groupby("Qnum")["symbol"].nunique().median()),
            "start": str(allx["Q"].min().date()), "end": str(allx["Q"].max().date()),
        },
        "L1_orthogonality": corr,
        "L2": {
            "b_centrality": round(float(b_cent), 4),
            "t_centrality": round(float(nw_t(qc["b_cent"])) if len(qc) > 1 else np.nan, 2),
            "b_size": round(float(b_size), 4),
            "b_vol": round(float(b_vol), 4),
            "b_turn": round(float(b_turn), 4),
            "b_D": round(float(b_D), 4),
            "b_centrality_on_avg_beta": round(float(b_cent_beta), 4),
            "quarter_coefs": [round(float(x), 3) for x in qc["b_cent"]],
            "quarter_same_sign": int((qc["b_cent"] > 0).sum()),
            "n_quarters": int(len(qc)),
        },
        "L3_placebo": {
            "true": round(float(b_cent), 4),
            "placebo_mean": round(float(placebo.mean()), 4),
            "placebo_sd": round(float(placebo.std()), 4),
            "percentile": round(float((placebo < b_cent).mean() * 100), 1),
        },
    }
    (OUT / "h5_market.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
