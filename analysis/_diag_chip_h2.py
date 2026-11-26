"""H2：密度 × 伽马符号 —— 把「密度」和「谁持有」一起拆。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
对象
  月度快照 2025-04 ~ 2026-09（gamma 公告日 as-of，防前视）。
  dens = chip_medium 面板的 15 日换手加权密度（旧页对象）。
  gamma_holder = z(fund+insurance+social+qfii share − individual share)，真实 top10、剔托管/法人。
     高 = 机构长钱（正伽马）；低 = 个人/短线（负伽马）。
  gamma_flow = −z(HOLDER_NUM_RATIO)，户数下降=集中=正伽马。
  gamma_comp = 0.5*(z_holder + z_flow)，仅当两者同向；否则退化为 gamma_holder。
  次代理：gamma_block = z(block premium)（折价越小=越正）；gamma_hot = −z(20日龙虎榜净买/市值)。

主判据（收益）
  fwd20_bp ~ dens_z + gamma_z + dens_z×gamma_z + controls
  交互 b_int ≥ +100bp（正伽马削弱密度的负效应），NW(3) |t| ≥ 2；
  且 gamma 低组 dens Q5−Q1 < 0、gamma 高组 ≥ 0（符号相反）。

次判据（脆弱性）
  fragility ~ 同一组；交互 ≤ −0.10 z，|t| ≥ 2。
  fragility = 月度截面 z(−mdd20)、z(−skew20)、z(−worst5)、z(+dvol20) 的均值。

OOS
  2025-04~2025-12 vs 2026-01~2026-09，交互同号。

杀死
  交互为零/同号；或控制 dens×to15 后消失；或只有单一 gamma 代理成立；
  或 gamma 高组与低组的密度斜率差 < 100bp。

方法
  不用 dens/gamma 比值；只拆主效应 + 交互。dens 用 log 后 z；收益 1%/99% winsorize。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h2"
OUT.mkdir(parents=True, exist_ok=True)
CHIP = ROOT / "data" / ".scratch" / "chip_medium" / "panel.parquet"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"
HOLDER = ROOT / "data" / ".scratch" / "chip_age" / "holder_panel.parquet"
TOPF = ROOT / "data" / "shareholder" / "top_float" / "part.parquet"
HN = ROOT / "data" / "shareholder" / "holder_num" / "part.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
BLOCK = ROOT / "data" / "block_trade"
DRAGON = ROOT / "data" / "dragon_tiger_seats"

NW_LAG = 3
N_PLACEBO = 200
HORIZON = 20


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


def z(x) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def skewness(a: np.ndarray) -> np.ndarray:
    mu = np.nanmean(a, axis=0, keepdims=True)
    d = a - mu
    m2 = np.nanmean(d ** 2, axis=0)
    m3 = np.nanmean(d ** 3, axis=0)
    return m3 / np.maximum(m2, 1e-18) ** 1.5


def load_chip():
    cols = ["date", "symbol", "p", "o", "dens", "dens_rec", "fwd20", "to15", "vol20", "ln_mcap", "ret20"]
    p = pd.read_parquet(CHIP, columns=cols)
    p["date"] = pd.to_datetime(p["date"]).astype("datetime64[ns]")
    p["symbol"] = p["symbol"].astype(str)
    p["ym"] = p["date"].dt.to_period("M")
    return p


def build_gamma():
    h = pd.read_parquet(HOLDER)
    h["period_end"] = pd.to_datetime(h["period_end"]).astype("datetime64[ns]")
    tf = pd.read_parquet(TOPF, columns=["code", "period_end", "notice_date"])
    tf["period_end"] = pd.to_datetime(tf["period_end"]).astype("datetime64[ns]")
    tf["notice_date"] = pd.to_datetime(tf["notice_date"]).astype("datetime64[ns]")
    nd = tf.groupby(["period_end", "code"])["notice_date"].max().reset_index()
    h = h.merge(nd, on=["period_end", "code"], how="left")
    h["gamma_holder"] = h[["fund_share", "insurance_share", "social_share", "qfii_share"]].sum(axis=1) - h["individual_share"]
    h = h.dropna(subset=["notice_date"]).sort_values(["symbol", "notice_date"])
    return h[["symbol", "period_end", "notice_date", "gamma_holder",
              "fund_share", "insurance_share", "social_share", "qfii_share",
              "individual_share", "corporate_share", "top1_ratio", "hhi", "real_top10_ratio"]]


def build_flow():
    hn = pd.read_parquet(HN)
    hn["period_end"] = pd.to_datetime(hn["period_end"]).astype("datetime64[ns]")
    hn["notice_date"] = pd.to_datetime(hn["notice_date"]).astype("datetime64[ns]")
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    hn["code"] = hn["code"].astype(str).str.zfill(6)
    hn = hn.merge(inst, on="code", how="left")
    hn["gamma_flow"] = -pd.to_numeric(hn["HOLDER_NUM_RATIO"], errors="coerce").clip(-50, 100)
    hn = hn.dropna(subset=["notice_date", "symbol"]).sort_values(["symbol", "notice_date"])
    return hn[["symbol", "period_end", "notice_date", "gamma_flow", "HOLDER_NUM_RATIO"]]


def build_block():
    frames = []
    for y in (2024, 2025, 2026):
        f = BLOCK / f"year={y}" / "part.parquet"
        if f.exists():
            d = pd.read_parquet(f, columns=["TRADE_DATE", "symbol", "premium_doc", "amt", "buyer", "FREE_SHARES_RATIO"])
            frames.append(d)
    if not frames:
        return None
    b = pd.concat(frames, ignore_index=True)
    b["TRADE_DATE"] = pd.to_datetime(b["TRADE_DATE"]).astype("datetime64[ns]")
    b["period_end"] = b["TRADE_DATE"].dt.to_period("Q").dt.end_time.dt.normalize().astype("datetime64[ns]")
    b["is_inst_buy"] = b["buyer"].astype(str).str.contains("机构专用", na=False)
    b["inst_amt"] = np.where(b["is_inst_buy"], b["amt"], 0.0)
    g = b.groupby(["symbol", "period_end"]).apply(
        lambda x: pd.Series({
            "block_amt": x["amt"].sum(),
            "block_premium": np.average(x["premium_doc"], weights=np.maximum(x["amt"], 1)),
            "block_ratio": x["FREE_SHARES_RATIO"].sum(),
            "block_inst_share": x["inst_amt"].sum() / max(x["amt"].sum(), 1),
        }), include_groups=False
    ).reset_index()
    return g.sort_values(["symbol", "period_end"])


def build_dragon(panel: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame | None:
    files = sorted(DRAGON.glob("date=*"))
    if not files:
        return None
    frames = []
    for f in files:
        d = pd.read_parquet(f, columns=["symbol", "date", "net"])
        frames.append(d)
    d = pd.concat(frames, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"]).astype("datetime64[ns]")
    d["symbol"] = d["symbol"].astype(str)
    daily = d.groupby(["date", "symbol"])["net"].sum().unstack("symbol").reindex(dates)
    # 20 日净买 / 市值
    roll = daily.rolling(20, min_periods=10).sum()
    mcap = panel.pivot_table(index="date", columns="symbol", values="ln_mcap", aggfunc="last").reindex(dates)
    ratio = roll / np.exp(mcap)
    return ratio


def future_metrics(panel: pd.DataFrame, snap: pd.DataFrame) -> pd.DataFrame:
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    p = panel.pivot_table(index="date", columns="symbol", values="p", aggfunc="last").reindex(dates)
    o = panel.pivot_table(index="date", columns="symbol", values="o", aggfunc="last").reindex(dates)
    syms = np.asarray(p.columns)
    rows = []
    for dt in sorted(snap["date"].unique()):
        ti = int(dates.get_loc(dt))
        if ti + HORIZON + 2 > len(dates):
            continue
        p0 = o.iloc[ti + 1].to_numpy(dtype=float)
        path = p.iloc[ti + 1:ti + HORIZON + 2].to_numpy(dtype=float)  # 21 x N
        if path.shape[0] < HORIZON + 1:
            continue
        r1 = path[0] / p0 - 1.0
        r = np.vstack([r1, path[1:] / path[:-1] - 1.0])
        r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
        fwd = path[-1] / p0 - 1.0
        pp = np.vstack([p0, path])
        peak = np.maximum.accumulate(pp, axis=0)
        mdd = (pp / peak - 1.0).min(axis=0)
        sk = skewness(r)
        sw = np.lib.stride_tricks.sliding_window_view(r, 5, axis=0)
        worst5 = (np.prod(1.0 + sw, axis=-1) - 1.0).min(axis=0)
        dvol = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2, axis=0))
        rows.append(pd.DataFrame({
            "date": dt, "symbol": syms,
            "fwd_calc": fwd, "mdd20": mdd, "skew20": sk, "worst5": worst5, "dvol20": dvol,
        }))
    return pd.concat(rows, ignore_index=True)


def main() -> None:
    print("[1/6] load chip panel ...", flush=True)
    panel = load_chip()
    month_ends = panel.groupby("ym")["date"].max()
    snap = panel[panel["date"].isin(month_ends)].copy()
    snap = snap[(snap["date"] >= "2025-04-01") & (snap["date"] <= "2026-09-30")].copy()
    print(f"      snap={len(snap):,} dates={snap['date'].nunique()}", flush=True)

    print("[2/6] build gamma ...", flush=True)
    g = build_gamma()
    f = build_flow()
    b = build_block()
    snap = snap.sort_values("date").astype({"symbol": str})
    snap = pd.merge_asof(
        snap, g.sort_values("notice_date"), left_on="date", right_on="notice_date",
        by="symbol", direction="backward", suffixes=("", "_g"),
    )
    snap = pd.merge_asof(
        snap.sort_values("date"), f.sort_values("notice_date"), left_on="date", right_on="notice_date",
        by="symbol", direction="backward", suffixes=("", "_f"),
    )
    if b is not None:
        snap = pd.merge_asof(
            snap.sort_values("date"), b.sort_values("period_end"), left_on="date", right_on="period_end",
            by="symbol", direction="backward", suffixes=("", "_b"),
        )
    print(f"      gamma nonnull={snap['gamma_holder'].notna().mean():.3f} flow={snap['gamma_flow'].notna().mean():.3f}", flush=True)

    print("[3/6] future metrics ...", flush=True)
    fut = future_metrics(panel, snap)
    snap = snap.merge(fut, on=["date", "symbol"], how="left")
    # age panel D/H
    age = pd.read_parquet(AGE, columns=["date", "symbol", "age_mean", "age_entropy", "old_share", "young20"])
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    snap = pd.merge_asof(
        snap.sort_values("date"), age.sort_values("date"), on="date", by="symbol",
        direction="backward", suffixes=("", "_a"),
    )
    snap["D"] = snap["age_mean"].astype(float) + snap["old_share"].astype(float) * 500.0
    snap["dens"] = pd.to_numeric(snap["dens"], errors="coerce")
    snap["dens_rec"] = pd.to_numeric(snap["dens_rec"], errors="coerce")
    snap["fwd20"] = pd.to_numeric(snap["fwd20"], errors="coerce")
    snap["dens"] = snap["dens"].clip(lower=1e-6)
    snap["dens_rec"] = snap["dens_rec"].clip(lower=1e-6)
    snap = snap.dropna(subset=["dens", "fwd20", "fwd_calc", "gamma_holder", "D", "to15", "vol20", "ln_mcap", "ret20"])
    print(f"      merged={len(snap):,} dates={snap['date'].nunique()} symbols={snap['symbol'].nunique()}", flush=True)

    # 月度截面 z
    zp = []
    for dt, x in snap.groupby("date"):
        d = pd.DataFrame(index=x.index)
        d["dens_z"] = z(np.log(x["dens"]))
        d["dens_rec_z"] = z(np.log(x["dens_rec"]))
        d["gamma_z"] = z(x["gamma_holder"])
        d["gamma_flow_z"] = z(x["gamma_flow"])
        d["gamma_comp_z"] = z(0.5 * z(x["gamma_holder"]) + 0.5 * z(x["gamma_flow"]))
        d["to_z"] = z(x["to15"])
        d["vol_z"] = z(x["vol20"])
        d["cap_z"] = z(x["ln_mcap"])
        d["ret_z"] = z(x["ret20"])
        d["D_z"] = z(np.log(x["D"].clip(lower=1)))
        d["H_z"] = z(x["age_entropy"])
        d["dens_x_gamma"] = d["dens_z"] * d["gamma_z"]
        d["dens_x_to"] = d["dens_z"] * d["to_z"]
        d["fwd_bp"] = x["fwd20"] * 10000.0
        d["z_mdd"] = z(-x["mdd20"])
        d["z_skew"] = z(-x["skew20"])
        d["z_worst5"] = z(-x["worst5"])
        d["z_dvol"] = z(x["dvol20"])
        d["fragility"] = d[["z_mdd", "z_skew", "z_worst5", "z_dvol"]].mean(axis=1)
        zp.append(d)
    snap = snap.join(pd.concat(zp))
    # 收益 winsorize 1%/99% by month
    snap["fwd_bp"] = snap.groupby("date")["fwd_bp"].transform(
        lambda s: s.clip(s.quantile(0.01), s.quantile(0.99))
    )
    snap.to_parquet(OUT / "panel.parquet", index=False)

    print("[4/6] regressions ...", flush=True)
    def run_reg(ycol: str, xint: str = "dens_x_gamma", extra: list[str] | None = None):
        rows = []
        for dt, x in snap.groupby("date"):
            if len(x) < 200:
                continue
            cols = ["dens_z", "gamma_z", xint, "to_z", "vol_z", "cap_z", "ret_z", "D_z", "H_z"]
            if extra:
                cols += extra
            X = np.column_stack([x[c].to_numpy(dtype=float) for c in cols] + [np.ones(len(x))])
            y = x[ycol].to_numpy(dtype=float)
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 200:
                continue
            beta, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            row = {"date": dt, "b_dens": beta[0], "b_gamma": beta[1], "b_int": beta[2],
                   "b_to": beta[3], "b_vol": beta[4], "b_cap": beta[5], "b_ret": beta[6],
                   "b_D": beta[7], "b_H": beta[8], "n": int(ok.sum())}
            if extra:
                for j, name in enumerate(extra):
                    row[f"b_{name}"] = beta[9 + j]
            rows.append(row)
        return pd.DataFrame(rows)

    reg_fwd = run_reg("fwd_bp")
    reg_frag = run_reg("fragility")
    reg_fwd_ctrl = run_reg("fwd_bp", extra=["dens_x_to"])
    reg_frag_ctrl = run_reg("fragility", extra=["dens_x_to"])

    def summ(reg, col):
        return {"mean": round(float(reg[col].mean()), 4), "nw_t": round(float(nw_t(reg[col])), 2), "n": int(len(reg))}

    out = {
        "sample": {"rows": int(len(snap)), "dates": int(snap["date"].nunique()), "symbols": int(snap["symbol"].nunique()),
                   "start": str(snap["date"].min().date()), "end": str(snap["date"].max().date())},
        "coverage": {
            "gamma_holder": round(float(snap["gamma_holder"].notna().mean()), 3),
            "gamma_flow": round(float(snap["gamma_flow"].notna().mean()), 3),
            "gamma_block": round(float(snap["block_premium"].notna().mean()), 3) if "block_premium" in snap else None,
        },
        "corr": {
            "dens_to": round(float(snap[["dens", "to15"]].corr(method="spearman").iloc[0, 1]), 3),
            "dens_gamma": round(float(snap[["dens", "gamma_holder"]].corr(method="spearman").iloc[0, 1]), 3),
            "gamma_to": round(float(snap[["gamma_holder", "to15"]].corr(method="spearman").iloc[0, 1]), 3),
            "gamma_vol": round(float(snap[["gamma_holder", "vol20"]].corr(method="spearman").iloc[0, 1]), 3),
            "gamma_D": round(float(snap[["gamma_holder", "D"]].corr(method="spearman").iloc[0, 1]), 3),
        },
        "fwd": {
            "main": {"dens": summ(reg_fwd, "b_dens"), "gamma": summ(reg_fwd, "b_gamma"), "interaction": summ(reg_fwd, "b_int")},
            "with_dens_x_to": {"dens": summ(reg_fwd_ctrl, "b_dens"), "gamma": summ(reg_fwd_ctrl, "b_gamma"),
                               "interaction": summ(reg_fwd_ctrl, "b_int"), "dens_x_to": summ(reg_fwd_ctrl, "b_dens_x_to")},
        },
        "fragility": {
            "main": {"dens": summ(reg_frag, "b_dens"), "gamma": summ(reg_frag, "b_gamma"), "interaction": summ(reg_frag, "b_int")},
            "with_dens_x_to": {"dens": summ(reg_frag_ctrl, "b_dens"), "gamma": summ(reg_frag_ctrl, "b_gamma"),
                               "interaction": summ(reg_frag_ctrl, "b_int"), "dens_x_to": summ(reg_frag_ctrl, "b_dens_x_to")},
        },
        "reg_fwd": [{**r, "date": str(pd.Timestamp(r["date"]).date())} for r in reg_fwd.to_dict(orient="records")],
        "reg_frag": [{**r, "date": str(pd.Timestamp(r["date"]).date())} for r in reg_frag.to_dict(orient="records")],
    }

    print("[5/6] slopes + 2x2 ...", flush=True)
    slope_rows = []
    for dt, x in snap.groupby("date"):
        if len(x) < 300:
            continue
        x = x.copy()
        try:
            x["gq"] = pd.qcut(x["gamma_z"], 3, labels=False, duplicates="drop")
            x["dq"] = pd.qcut(x["dens_z"], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        for gq in sorted(x["gq"].dropna().unique()):
            y = x[x["gq"] == gq]
            q = y.groupby("dq").agg(fwd=("fwd_bp", "mean"), frag=("fragility", "mean"), n=("symbol", "size"))
            if 0 in q.index and 4 in q.index:
                slope_rows.append({
                    "date": dt, "gq": int(gq),
                    "fwd_q1": q.loc[0, "fwd"], "fwd_q5": q.loc[4, "fwd"],
                    "fwd_spread": q.loc[4, "fwd"] - q.loc[0, "fwd"],
                    "frag_q1": q.loc[0, "frag"], "frag_q5": q.loc[4, "frag"],
                    "frag_spread": q.loc[4, "frag"] - q.loc[0, "frag"],
                    "n": int(q["n"].sum()),
                })
    slopes = pd.DataFrame(slope_rows)
    slopes.to_parquet(OUT / "slopes.parquet", index=False)
    out["slopes"] = {}
    for gq, name in ((0, "gamma_low"), (1, "gamma_mid"), (2, "gamma_high")):
        x = slopes[slopes["gq"] == gq]
        out["slopes"][name] = {
            "fwd_spread": round(float(x["fwd_spread"].mean()), 1),
            "fwd_t": round(float(nw_t(x["fwd_spread"])), 2),
            "frag_spread": round(float(x["frag_spread"].mean()), 3),
            "frag_t": round(float(nw_t(x["frag_spread"])), 2),
            "months": int(len(x)),
        }

    # 2x2
    cell_rows = []
    for dt, x in snap.groupby("date"):
        if len(x) < 300:
            continue
        dm = x["dens_z"] > x["dens_z"].median()
        gm = x["gamma_z"] > x["gamma_z"].median()
        for dn, dmask in (("dlo", ~dm), ("dhi", dm)):
            for gn, gmask in (("glo", ~gm), ("ghi", gm)):
                c = x[dmask & gmask]
                cell_rows.append({"date": dt, "cell": f"{dn}_{gn}", "fwd": c["fwd_bp"].mean(),
                                  "frag": c["fragility"].mean(), "n": len(c)})
    cells = pd.DataFrame(cell_rows)
    cells.to_parquet(OUT / "cells.parquet", index=False)
    out["two_by_two"] = {}
    for cell, name in (("dlo_glo", "dens_low_gamma_low"), ("dhi_glo", "dens_high_gamma_low"),
                       ("dlo_ghi", "dens_low_gamma_high"), ("dhi_ghi", "dens_high_gamma_high")):
        x = cells[cells["cell"] == cell]
        out["two_by_two"][name] = {"fwd": round(float(x["fwd"].mean()), 1), "frag": round(float(x["frag"].mean()), 3)}
    # DiD
    def did(frame, col):
        p = frame.pivot_table(index="date", columns="cell", values=col, aggfunc="mean")
        if not {"dhi_ghi", "dlo_ghi", "dhi_glo", "dlo_glo"}.issubset(p.columns):
            return None
        d = (p["dhi_ghi"] - p["dlo_ghi"]) - (p["dhi_glo"] - p["dlo_glo"])
        return {"mean": round(float(d.mean()), 4), "nw_t": round(float(nw_t(d)), 2)}
    out["two_by_two"]["did_fwd"] = did(cells, "fwd")
    out["two_by_two"]["did_frag"] = did(cells, "frag")

    print("[6/6] OOS + placebo ...", flush=True)
    # OOS
    for name, mask in (("2025", snap["date"] < "2026-01-01"), ("2026", snap["date"] >= "2026-01-01")):
        sub = snap[mask]
        rf = reg_fwd[reg_fwd["date"].isin(sub["date"].unique())]
        rg = reg_frag[reg_frag["date"].isin(sub["date"].unique())]
        out.setdefault("oos", {})[name] = {
            "fwd_interaction": round(float(rf["b_int"].mean()), 1), "fwd_t": round(float(nw_t(rf["b_int"])), 2),
            "frag_interaction": round(float(rg["b_int"].mean()), 3), "frag_t": round(float(nw_t(rg["b_int"])), 2),
            "months": int(len(rf)),
        }

    # placebo：月内打乱 gamma
    rng = np.random.default_rng(20261126)
    true_int = float(reg_fwd["b_int"].mean())
    placebo = []
    for _ in range(N_PLACEBO):
        vals = []
        for dt, x in snap.groupby("date"):
            if len(x) < 200:
                continue
            x = x.copy()
            x["gamma_z"] = rng.permutation(x["gamma_z"].to_numpy())
            x["dens_x_gamma"] = x["dens_z"] * x["gamma_z"]
            X = np.column_stack([x[c].to_numpy(dtype=float) for c in
                                 ["dens_z", "gamma_z", "dens_x_gamma", "to_z", "vol_z", "cap_z", "ret_z", "D_z", "H_z"]]
                                + [np.ones(len(x))])
            y = x["fwd_bp"].to_numpy(dtype=float)
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 200:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            vals.append(b[2])
        placebo.append(float(np.nanmean(vals)))
    placebo = np.asarray(placebo)
    out["placebo"] = {"true_int": round(true_int, 1), "mean": round(float(placebo.mean()), 1),
                      "sd": round(float(placebo.std()), 1), "percentile": round(float((placebo < true_int).mean() * 100), 1)}

    # 判据
    fwd_int = out["fwd"]["main"]["interaction"]
    frag_int = out["fragility"]["main"]["interaction"]
    lo = out["slopes"]["gamma_low"]
    hi = out["slopes"]["gamma_high"]
    slopes_opposite = lo["fwd_spread"] < 0 and hi["fwd_spread"] >= 0
    oos_same = (out["oos"]["2025"]["fwd_interaction"] * out["oos"]["2026"]["fwd_interaction"] > 0)
    pass_fwd = fwd_int["mean"] >= 100 and fwd_int["nw_t"] >= 2 and slopes_opposite and oos_same
    pass_frag = frag_int["mean"] <= -0.10 and frag_int["nw_t"] <= -2
    out["verdict"] = {
        "pass_fwd": bool(pass_fwd),
        "pass_frag": bool(pass_frag),
        "slopes_opposite": bool(slopes_opposite),
        "oos_same": bool(oos_same),
        "status": "PASS" if (pass_fwd and pass_frag) else ("PARTIAL" if (pass_fwd or pass_frag) else "FAIL"),
    }
    (OUT / "h2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("sample", "coverage", "corr", "fwd", "fragility", "slopes", "two_by_two", "oos", "placebo", "verdict")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
