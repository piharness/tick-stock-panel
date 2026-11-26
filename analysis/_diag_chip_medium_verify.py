"""卷二审计：dens 的联合控制 / tc_close / 固定核宽 / 递推存量 / Theta 增量。

预注册（先写死，再看数）
------------------------
卷一唯一活着的变量是 dens（现价处 15 日换手加权密度）。卷二把它解释成「介质」。
此前所有「控制」都是一次切一个变量的双排序——本仓库已经吃过这个亏
（`footprint-20d-significance`：回归残差 +7.8bp，匹配孪生 +0.2bp(t0.01)）。
所以本脚本只做能杀死它的四件事：

A 联合控制：10 个已知量同时进横截面回归，看 dens 的增量系数；再把 dens 对
  全部控制回归后的残差重新排序。一次切一个变量的「存活」不算。
B tc_close：dens 与尾盘成交占比的相关，以及控制 tc15 后残差 dens 的排序。
C 核宽伪影：ivd 换成全样本常数，切断 dens 与 1/ivd 的乘积关系。
D 递推存量：S_t=(1-TO)S_{t-1}+TO*K_t，重跑市值分层（P1）。

E 附带：全市场重算 Theta，检验「与涨幅、换手正交」是否只是同尺度相除的算术，
  以及 P2（同位移内 Theta 单调）在 240 万观测上是否成立。

口径：fwd20 = close[t+21]/open[t+1]-1（前复权）；超额 = 减当日池内等权均值；
t 一律按日聚合（先算每日截面差，再对日序列求 t）。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENT = DATA / ".scratch" / "chip_entropy_local" / "part.parquet"
SHARES = DATA / "backfill_20261117" / "fin_shares.parquet"
INSTR = DATA / "instruments" / "instruments.parquet"
DAILY = DATA / "kline_daily_enriched"
OUTDIR = DATA / ".scratch" / "chip_medium"
PANEL = OUTDIR / "panel.parquet"
RESULT = OUTDIR / "verify.json"

START = "2024-11-07"
END = "2026-11-20"
_SQRT2 = np.sqrt(2.0)


def _erf(x: np.ndarray) -> np.ndarray:
    s = np.sign(x); a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return s * y


def Phi(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(np.asarray(x) / _SQRT2))


def sh(A: np.ndarray, k: int) -> np.ndarray:
    """滞后 k 期（axis=0），不足补 nan。"""
    if k == 0:
        return A
    out = np.full_like(A, np.nan)
    out[k:] = A[:-k]
    return out


def fw(A: np.ndarray, k: int) -> np.ndarray:
    """超前 k 期（axis=0）。"""
    out = np.full_like(A, np.nan)
    out[:-k] = A[k:]
    return out


def nan0(A):
    return np.nan_to_num(A, nan=0.0)


# ---------------------------------------------------------------- 载入
def load_long() -> tuple[pl.DataFrame, dict, list, list]:
    ent = pl.read_parquet(ENT, columns=["symbol", "date", "vwap", "ivd_lo", "tc_close",
                                        "h_time", "nbar", "amt", "vol"])
    dates_f = sorted(p.name[5:] for p in DAILY.glob("date=*") if START <= p.name[5:] <= END)
    files = [str(DAILY / f"date={d}" / "part.parquet") for d in dates_f]
    px = pl.scan_parquet(files, extra_columns="ignore").select(
        ["symbol", "date", "open", "close", "raw_close", "turnover_rate", "amount",
         "consecutive_limit_ups"]).collect()

    d = ent.join(px, on=["symbol", "date"], how="inner")

    sh_ = (pl.read_parquet(SHARES)
           .with_columns(pl.col("end_date").cast(pl.Date))
           .with_columns(pl.coalesce(pl.col("listed_a_shares"), pl.col("unlimited_shares"),
                                     pl.col("total_shares")).alias("float_shares"))
           .filter(pl.col("float_shares") > 0)
           .select("symbol", "end_date", "float_shares").sort(["symbol", "end_date"]))
    d = d.sort(["symbol", "date"]).join_asof(
        sh_, left_on="date", right_on="end_date", by="symbol", strategy="backward")

    ins = (pl.read_parquet(INSTR).select(["symbol", "listing_date"])
           .with_columns(pl.col("listing_date").str.slice(0, 10)
                         .str.to_date("%Y-%m-%d", strict=False)))
    d = d.join(ins, on="symbol", how="left")

    d = d.with_columns(
        (pl.col("close") / pl.col("raw_close")).alias("adjf"),
    ).with_columns(
        (pl.col("vwap") * pl.col("adjf")).alias("vwap_a"),
        pl.col("close").alias("p"),
        pl.col("open").alias("o"),
        (pl.col("turnover_rate") / 100.0).alias("to"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Float64).alias("limup"),
    ).filter(
        (pl.col("to") > 0) & (pl.col("to") < 0.6)
        & (pl.col("ivd_lo") > 0) & (pl.col("ivd_lo") < 0.15)
        & (pl.col("p") > 0) & pl.col("p").is_not_null()
    ).sort(["symbol", "date"])

    syms = sorted(d["symbol"].unique().to_list())
    dates = sorted(d["date"].unique().to_list())
    si = {s: i for i, s in enumerate(syms)}
    di = {x: i for i, x in enumerate(dates)}
    T, N = len(dates), len(syms)
    cols = {}
    for c in ["p", "o", "to", "vwap_a", "ivd_lo", "tc_close", "h_time",
              "float_shares", "amt", "limup"]:
        A = np.full((T, N), np.nan)
        A[d["date"].replace_strict(di, return_dtype=pl.Int32).to_numpy(),
          d["symbol"].replace_strict(si, return_dtype=pl.Int32).to_numpy()] = \
            d[c].cast(pl.Float64).to_numpy()
        cols[c] = A
    return d, cols, syms, dates


# ---------------------------------------------------------------- 特征
def build_features(cols: dict[str, np.ndarray], dates: list, syms: list,
                   ldmap: dict) -> dict[str, np.ndarray]:
    p, o, to = cols["p"], cols["o"], cols["to"]
    vw, iv, tcc, amt = cols["vwap_a"], cols["ivd_lo"], cols["tc_close"], cols["amt"]
    lnp, lnv = np.log(p), np.log(vw)
    T, N = p.shape

    dens = np.zeros((T, N)); dens_k = np.zeros((T, N))
    dens_up = np.zeros((T, N)); dens_dn = np.zeros((T, N))
    for k in range(15):
        lv, sd, tw = lnv, iv, to
        lv = sh(lv, k); sd = sh(sd, k); tw = nan0(sh(tw, k))
        z = (lnp - lv) / sd
        dens += nan0(tw * (Phi(z + 2) - Phi(z - 2)))
        dens_k += nan0(tw * np.exp(-0.5 * z * z) / (sd * math.sqrt(2 * math.pi)))
        dens_up += nan0(tw * (Phi((lnp + math.log(1.10) - lv) / sd) - Phi(z)))
        dens_dn += nan0(tw * (Phi(z) - Phi((lnp + math.log(0.90) - lv) / sd)))

    iv_med = float(np.nanmedian(iv))
    dens_fix = np.zeros((T, N))
    for k in range(15):
        lv = sh(lnv, k); tw = nan0(sh(to, k))
        z = (lnp - lv) / iv_med
        dens_fix += nan0(tw * (Phi(z + 2) - Phi(z - 2)))

    to15 = np.zeros((T, N)); to5 = np.zeros((T, N)); cnt15 = np.zeros((T, N))
    wvw = np.zeros((T, N)); iv15 = np.zeros((T, N)); tc15 = np.zeros((T, N))
    for k in range(15):
        t_ = sh(to, k)
        to15 += nan0(t_)
        cnt15 += (~np.isnan(t_)).astype(float)
        if k < 5:
            to5 += nan0(t_)
        wvw += nan0(t_ * sh(vw, k))
        iv15 += nan0(sh(iv, k))
        tc15 += nan0(sh(tcc, k))
    mu = wvw / np.maximum(to15, 1e-9)
    g = np.log(p / mu)
    q15 = dens / np.maximum(to15, 1e-9)

    ret1 = p / sh(p, 1) - 1
    ret5 = p / sh(p, 5) - 1
    ret20 = p / sh(p, 20) - 1
    fwd20 = fw(p, 21) / fw(o, 1) - 1

    def roll_std(A, w):
        s = np.zeros_like(A); s2 = np.zeros_like(A); c = np.zeros_like(A)
        for k in range(w):
            a = sh(A, k)
            s += nan0(a); s2 += nan0(a * a); c += (~np.isnan(a)).astype(float)
        m = s / np.maximum(c, 1)
        return np.sqrt(np.maximum(s2 / np.maximum(c, 1) - m * m, 0))

    vol20 = roll_std(ret1, 20)
    maxret20 = np.full_like(p, -np.inf)
    for k in range(20):
        maxret20 = np.fmax(maxret20, sh(ret1, k))
    hi15 = np.full_like(p, -np.inf); lo15 = np.full_like(p, np.inf)
    for k in range(15):
        hi15 = np.fmax(hi15, sh(p, k)); lo15 = np.fmin(lo15, sh(p, k))
    range15 = hi15 / lo15 - 1

    amihud = np.zeros_like(p)
    for k in range(20):
        a = sh(amt, k); r = sh(np.abs(ret1), k)
        amihud += nan0(r / np.maximum(nan0(a) / 1e8, 1e-6))
    amihud /= 20.0

    mcap = p * cols["float_shares"]
    dens_rec = recursive_dens(lnp, lnv, iv, to)

    # ---- 过路费率 Theta（与 _diag_chip_toll.py 同定义，全市场向量化）
    p5 = sh(p, 5)
    lnp5 = np.log(p5)
    D0 = np.zeros((T, N)); PI = np.zeros((T, N))
    for k in range(5, 20):
        lv = sh(lnv, k); sd = sh(iv, k); tw = nan0(sh(to, k))
        D0 += nan0(tw * (Phi((lnp - lv) / sd) - Phi((lnp5 - lv) / sd)))
    for k in range(0, 5):
        lv = sh(lnv, k); sd = sh(iv, k); tw = nan0(sh(to, k))
        PI += nan0(tw * (Phi((lnp - lv) / sd) - Phi((lnp5 - lv) / sd)))
    cum5 = p / p5 - 1
    with np.errstate(divide="ignore", invalid="ignore"):
        THETA = np.where((D0 > 1e-6) & (cum5 > 0), PI / D0, np.nan)

    # 上市天数
    d64 = np.array(dates, dtype="datetime64[D]")
    l64 = np.array([np.datetime64(ldmap.get(s), "D") if ldmap.get(s) else np.datetime64("1900-01-01", "D")
                    for s in syms], dtype="datetime64[D]")
    age = (d64[:, None] - l64[None, :]) / np.timedelta64(1, "D")

    return {
        "p": p, "o": o, "to": to, "to15": to15, "to5": to5, "cnt15": cnt15,
        "ivd": iv, "ivd15": iv15 / np.maximum(cnt15, 1), "tc15": tc15 / np.maximum(cnt15, 1),
        "tc_close": tcc, "dens": dens, "dens_k": dens_k, "dens_fix": dens_fix,
        "dens_up": dens_up, "dens_dn": dens_dn, "q15": q15,
        "g": g, "absg": np.abs(g), "ret1": ret1, "ret5": ret5, "ret20": ret20,
        "vol20": vol20, "maxret20": maxret20, "range15": range15, "amihud": amihud,
        "mcap": mcap, "fwd20": fwd20, "dens_rec": dens_rec, "age_days": age,
        "is_limit_up": cols["limup"], "THETA": THETA, "D0": D0, "PI": PI,
        "cum5": cum5, "ln_mcap": np.log(np.maximum(mcap, 1.0)),
    }


def recursive_dens(lnp, lnv, iv, to, grid_n: int = 161) -> np.ndarray:
    T, N = lnp.shape
    out = np.full((T, N), np.nan)
    for j in range(N):
        pj, vj, sj, tj = lnp[:, j], lnv[:, j], iv[:, j], to[:, j]
        ok = np.isfinite(pj) & np.isfinite(tj) & np.isfinite(vj) & np.isfinite(sj)
        if ok.sum() < 60:
            continue
        idx = np.flatnonzero(ok)
        sdmax = np.nanmax(sj[idx])
        lo = np.nanmin(vj[idx]) - 5 * sdmax
        hi = np.nanmax(vj[idx]) + 5 * sdmax
        grid = np.linspace(lo, hi, grid_n)
        step = grid[1] - grid[0]
        S = None
        for i in idx:
            kk = np.exp(-0.5 * ((grid - vj[i]) / sj[i]) ** 2) / (sj[i] * math.sqrt(2 * math.pi))
            S = kk if S is None else (1 - tj[i]) * S + tj[i] * kk
            x = (pj[i] - lo) / step
            m = int(math.floor(x))
            if m < 0 or m >= grid_n - 1:
                continue
            w = x - m
            out[i, j] = (1 - w) * S[m] + w * S[m + 1]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default="all", choices=["build", "stats", "all"])
    a = ap.parse_args()
    OUTDIR.mkdir(parents=True, exist_ok=True)

    if a.step in ("build", "all"):
        d, cols, syms, dates = load_long()
        print(f"long {d.height:,} rows, {len(syms)} syms, {len(dates)} dates")
        ld = d.group_by("symbol").agg(pl.col("listing_date").first()).to_dict(as_series=False)
        ldmap = dict(zip(ld["symbol"], ld["listing_date"]))
        feat = build_features(cols, dates, syms, ldmap)
        print("features done")
        feat["exc20"] = feat["fwd20"] - np.nanmean(feat["fwd20"], axis=1, keepdims=True)

        keep = ["p", "o", "to", "to15", "to5", "ivd", "ivd15", "tc15", "tc_close",
                "dens", "dens_k", "dens_fix", "dens_up", "dens_dn", "q15", "g", "absg",
                "ret1", "ret5", "ret20", "vol20", "maxret20", "range15", "amihud",
                "mcap", "ln_mcap", "fwd20", "exc20", "dens_rec", "age_days", "is_limit_up",
                "THETA", "D0", "PI", "cum5"]
        ii, jj = np.nonzero(np.isfinite(feat["p"]))
        out = pl.DataFrame({"date": [dates[i] for i in ii], "symbol": [syms[j] for j in jj]})
        for k in keep:
            out = out.with_columns(pl.Series(k, feat[k][ii, jj]))
        out.write_parquet(PANEL)
        print(f"panel {out.height:,} -> {PANEL}")
        if a.step == "build":
            return
    stats()


def theta_panel(feat, dates) -> pl.DataFrame:
    p = feat["p"]; to = feat["to"]; vw = feat["ivd"] * 0  # placeholder
    # 用未经复权调整的原始价更接近 _diag_chip_toll.py 的口径；这里 p 已复权，
    # 但 band 与核都在同一空间，比值不变。
    lnp = np.log(p); lnv = np.log(feat["p"] * 0 + np.nan)  # 占位，稍后替换
    raise NotImplementedError


def stats() -> None:
    d = pl.read_parquet(PANEL)
    print(f"panel {d.height:,}  {d['symbol'].n_unique()} 只  {d['date'].min()}~{d['date'].max()}")
    d = d.filter((pl.col("age_days") >= 365) & pl.col("exc20").is_not_null()
                 & (pl.col("ret1").abs() < 0.095) & (pl.col("is_limit_up").fill_null(0) == 0))
    print(f"可算 {d.height:,}  {d['date'].min()}~{d['date'].max()}")

    out = {}

    def rep(name, tab):
        out[name] = tab
        print(f"\n== {name} ==")
        if tab.get("buckets"):
            print("  " + "  ".join(f"Q{b['q']}={b['mean_bp']:+.0f}(t{b['t']:.2f})"
                                   for b in tab["buckets"] if b))
        print(f"  Q1-Q{tab['k']} = {tab['q1_minus_qk_bp']:+.0f}bp  t={tab['t']:.2f}  "
              f"n={tab['n']:,} days={tab['n_day']}")

    dates = d["date"].unique().sort().to_list()
    syms = d["symbol"].unique().sort().to_list()
    di = {x: i for i, x in enumerate(dates)}; si = {s: i for i, s in enumerate(syms)}
    T, N = len(dates), len(syms)
    F = {}
    need = ["dens", "dens_k", "dens_fix", "dens_up", "dens_dn", "q15", "to15", "to5",
            "ivd15", "tc15", "tc_close", "g", "absg", "ret1", "ret5", "ret20", "vol20",
            "maxret20", "range15", "amihud", "mcap", "exc20", "fwd20", "dens_rec"]
    ii = d["date"].replace_strict(di, return_dtype=pl.Int32).to_numpy()
    jj = d["symbol"].replace_strict(si, return_dtype=pl.Int32).to_numpy()
    for c in need:
        A = np.full((T, N), np.nan)
        A[ii, jj] = d[c].cast(pl.Float64).to_numpy()
        F[c] = A
    exc = F["exc20"]; mask = np.isfinite(exc)

    rep("dens 单变量（复现）", sort_table(F, "dens", exc, mask))
    rep("dens_k 核版（卷二的数）", sort_table(F, "dens_k", exc, mask))
    rep("dens_fix 固定核宽", sort_table(F, "dens_fix", exc, mask))
    rep("q15 走廊内份额", sort_table(F, "q15", exc, mask))
    rep("to15", sort_table(F, "to15", exc, mask))
    rep("absg", sort_table(F, "absg", exc, mask))
    rep("dens_rec 递推", sort_table(F, "dens_rec", exc, mask))

    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)
    print(f"\n写出 {RESULT}")


def qcut(A: np.ndarray, k: int = 5) -> np.ndarray:
    T, N = A.shape
    out = np.full((T, N), -1, dtype=np.int8)
    for i in range(T):
        row = A[i]; m = np.isfinite(row); n = m.sum()
        if n < 50:
            continue
        r = np.argsort(np.argsort(row[m], kind="stable"), kind="stable")
        out[i, m] = np.minimum((r * k // n).astype(np.int8), k - 1)
    return out


def daily_t(x: np.ndarray) -> tuple[float, float, int]:
    x = x[np.isfinite(x)]; n = len(x)
    if n < 10:
        return float("nan"), float("nan"), n
    m = x.mean(); se = x.std(ddof=1) / math.sqrt(n)
    return float(m), float(m / se) if se > 0 else float("nan"), n


def sort_table(F: dict, var: str, exc: np.ndarray, mask: np.ndarray, k: int = 5) -> dict:
    A = np.where(mask, F[var], np.nan)
    q = qcut(A, k)
    res = {"var": var, "k": k, "buckets": [], "n": int((q >= 0).sum())}
    for b in range(k):
        sel = q == b
        cnt = sel.sum(axis=1).astype(float)
        s = np.where(sel, exc, 0.0).sum(axis=1)
        dd = np.where(cnt > 20, s / np.maximum(cnt, 1), np.nan)
        m, t, nd = daily_t(dd)
        res["buckets"].append({"q": b + 1, "mean_bp": m * 1e4, "t": t,
                               "n_stockday": int(sel.sum()), "n_day": nd})
    d = []
    for i in range(A.shape[0]):
        a, b = q[i] == 0, q[i] == k - 1
        if a.sum() > 20 and b.sum() > 20:
            d.append(exc[i, a].mean() - exc[i, b].mean())
    m, t, nd = daily_t(np.array(d))
    res["q1_minus_qk_bp"] = m * 1e4; res["t"] = t; res["n_day"] = nd
    return res


if __name__ == "__main__":
    main()
