"""把单指标结论组装成可交易组合：热度 /  stealth / 流动性 三条腿，长仓、扣费、非重叠。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"

CONTROLS = ["log_mcap", "turn20", "vol20", "ret20", "log_amihud"]


def z(s: pd.Series) -> pd.Series:
    return (s.rank(pct=True) - 0.5) * 2.0


def load() -> pd.DataFrame:
    p = pd.read_parquet(OUT / "panel.parquet")
    p = p[p["ok"] == 1].copy()
    p["date"] = pd.to_datetime(p["date"])
    it = pd.read_parquet(OUT / "intraday_daily.parquet").sort_values(["symbol", "date"])
    it["date"] = pd.to_datetime(it["date"])
    for f in ["sf", "tail_share", "open_share", "vwap_dev", "clv", "hhi", "top_share",
              "up_vol_share", "ret_ac1", "vol_ac1", "corr_rv", "signed_vol_ac1", "vol_cv",
              "big_share", "first30_ret", "last30_ret", "day_range", "illiq", "entropy"]:
        it["i_" + f + "_20"] = it.groupby("symbol", sort=False)[f].transform(
            lambda s: s.rolling(20, min_periods=15).mean()
        )
    p = p.merge(
        it[["symbol", "date"] + [c for c in it.columns if c.startswith("i_")]],
        on=["symbol", "date"], how="left",
    )
    return p


def add_scores(p: pd.DataFrame) -> pd.DataFrame:
    for c in ["turn20", "vol20", "ret20", "limit_up_cnt20", "range20", "turn_chg", "amt_chg",
              "log_amihud", "log_mcap"]:
        p["z_" + c] = p.groupby("date")[c].rank(pct=True) * 2 - 1
    for c in ["i_entropy_20", "i_ret_ac1_20", "i_signed_vol_ac1_20", "i_vol_cv_20", "i_day_range_20"]:
        p["z_" + c] = p.groupby("date")[c].rank(pct=True) * 2 - 1
    p["heat"] = p[["z_turn20", "z_vol20", "z_ret20", "z_limit_up_cnt20", "z_range20",
                   "z_turn_chg", "z_amt_chg"]].mean(axis=1)
    p["stealth"] = (p[["z_i_entropy_20", "z_i_ret_ac1_20", "z_i_signed_vol_ac1_20",
                       "z_i_vol_cv_20", "z_i_day_range_20"]]
                    * np.array([1.0, 1.0, 1.0, -1.0, -1.0])).mean(axis=1)
    p["illiq"] = p["z_log_amihud"]
    p["combo"] = -p["heat"] + p["stealth"] + p["illiq"]
    return p


def residualize(p: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """按日对 size/turn/vol/ret20/amihud + 行业 做残差，返回新列 *_res。"""
    out = []
    for d, sub in p.groupby("date", sort=False):
        n = len(sub)
        if n < 300:
            continue
        X = [np.ones(n)]
        for c in CONTROLS:
            X.append(z(sub[c]).fillna(0).to_numpy())
        ind = sub["industry"].fillna("未分类")
        vc = ind.value_counts()
        ind2 = ind.where(ind.isin(vc[vc >= 20].index), "OTHER")
        D = pd.get_dummies(ind2, drop_first=True).astype(float)
        X.append(D.to_numpy())
        X = np.column_stack(X)
        Y = np.column_stack([z(sub[c]).fillna(0).to_numpy() for c in cols])
        beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
        R = Y - X @ beta
        for j, c in enumerate(cols):
            sub = sub.assign(**{c + "_res": R[:, j]})
        out.append(sub)
    return pd.concat(out, ignore_index=True)


def ic_stats(p: pd.DataFrame, score: str) -> dict:
    vals = []
    for d, sub in p.groupby("date", sort=False):
        m = sub[score].notna() & sub["ex20_mb"].notna()
        if m.sum() < 300:
            continue
        v = sub.loc[m, score].rank()
        e = sub.loc[m, "ex20_mb"].rank()
        vals.append((d, np.corrcoef(v, e)[0, 1]))
    s = pd.Series(dict(vals)) if vals else pd.Series(dtype=float)
    return {"ic": float(s.mean()), "t": float(s.mean() / (s.std() / np.sqrt(len(s)))) if len(s) > 2 else np.nan,
            "hit": float((s > 0).mean()), "n": int(len(s))}


def portfolio(p: pd.DataFrame, score: str, side: str, frac: float = 0.1,
              cost: float = 0.002, size_neutral: bool = False) -> dict:
    """非重叠锚点（20 交易日）等权组合，扣单边+印花+滑点合计 cost。"""
    rows = []
    for d, sub in p[p["di"] % 20 == 0].groupby("date", sort=False):
        sub = sub[sub[score].notna() & sub["fwd20"].notna()]
        if len(sub) < 300:
            continue
        if size_neutral:
            q = pd.qcut(z(sub["log_mcap"]), 5, labels=False, duplicates="drop")
            picks = []
            for _, cell in sub.groupby(q):
                if len(cell) < 50:
                    continue
                k = max(1, int(len(cell) * frac))
                picks.append(cell.nlargest(k, score) if side == "top" else cell.nsmallest(k, score))
            sel = pd.concat(picks)
        else:
            k = max(1, int(len(sub) * frac))
            sel = sub.nlargest(k, score) if side == "top" else sub.nsmallest(k, score)
        ret = float(sel["fwd20"].mean()) - cost
        bench = float(sub["fwd20"].mean())
        rows.append({"date": d, "n": len(sel), "ret": ret, "excess": ret - bench,
                     "amt": float(np.exp(sel["log_amt20"]).median())})
    r = pd.DataFrame(rows)
    if len(r) == 0:
        return {}
    nav = (1 + r["ret"]).cumprod()
    dd = float((nav / nav.cummax() - 1).min())
    return {
        "periods": int(len(r)),
        "ann_ret": float((nav.iloc[-1]) ** (12 / len(r)) - 1),
        "ann_excess": float(r["excess"].mean() * 12),
        "sharpe": float(r["excess"].mean() / r["excess"].std() * np.sqrt(12)),
        "hit": float((r["excess"] > 0).mean()),
        "max_dd": dd,
        "avg_holdings": float(r["n"].mean()),
        "median_amount_yi": float(r["amt"].median() / 1e8),
    }


def main() -> None:
    p = add_scores(load())
    p = residualize(p, ["heat", "stealth", "illiq", "combo"])
    res = {}
    for s in ["heat", "stealth", "illiq", "combo",
              "heat_res", "stealth_res", "illiq_res", "combo_res"]:
        res[s] = ic_stats(p, s)
        res[s]["ls_top"] = portfolio(p, s, "top")
        res[s]["ls_bottom"] = portfolio(p, s, "bottom")
        res[s]["top_size_neutral"] = portfolio(p, s, "top", size_neutral=True)
        res[s]["bottom_size_neutral"] = portfolio(p, s, "bottom", size_neutral=True)
        print(s, res[s]["ic"], res[s]["t"], "| top", res[s]["ls_top"].get("ann_excess"),
              "| bottom", res[s]["ls_bottom"].get("ann_excess"))
    # 年度净值（combo 多头、非重叠）
    rows = []
    for d, sub in p[(p["di"] % 20 == 0)].groupby("date", sort=False):
        sub = sub[sub["combo"].notna() & sub["fwd20"].notna()]
        if len(sub) < 300:
            continue
        k = max(1, int(len(sub) * 0.1))
        sel = sub.nlargest(k, "combo")
        rows.append({"date": d, "ret": float(sel["fwd20"].mean()) - 0.002,
                     "bench": float(sub["fwd20"].mean())})
    r = pd.DataFrame(rows)
    r["year"] = pd.to_datetime(r["date"]).dt.year
    r["ex"] = r["ret"] - r["bench"]
    yearly = r.groupby("year").agg(
        ret=("ret", lambda s: (1 + s).prod() - 1),
        excess=("ex", lambda s: (1 + s).prod() - 1),
        n=("ret", "size"),
    )
    res["yearly_combo"] = yearly.reset_index().to_dict("records")
    json.dump(res, open(OUT / "composite.json", "w"), ensure_ascii=False, indent=1, default=float)
    print("saved")


if __name__ == "__main__":
    main()
