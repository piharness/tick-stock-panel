# -*- coding: utf-8 -*-
"""基线复核 B：回封可预测性 —— 严格时点（PIT）下的二分类判定。

复核对象：`_audit_eventdef_findings.md` §5 提出的判据
    「用 EOD 特征（连板高度、成交额、曾开板、封成比）把回封命中率从 59.6% 提到 73.2%」
以及 `_audit_baseline_eventdef.py` 的 G 段（机械打板期望 -82.5bp）。

本脚本要证伪的不是那条判据的算术，是它的**特征口径**：
`曾开板` 与 `封成比` 都是 15:00 才知道的量，而买入发生在盘中；用它们预测
「收盘是否回封」既含未来信息、`封成比` 更是标签本身（收盘封单>0 ⟺ 封住）。
所以这里把特征集按**决策时点**重建：

  决策时点 = T 日 09:30（挂涨停价买单，成交与否由是否触及决定）
  允许的特征 = 只能用到 T-1 收盘 + T 日开盘价（开盘价 09:25 集合竞价后即知）
  标签 = T 日收盘是否封在涨停价

买入侧的口径沿用 A（以涨停价为买价、T+1 开盘卖、扣窗口过滤），
所以 EV 直接是 `r0_lim` 的样本均值，不需要经过 163/-444 的分解（分解只在
条件均值不变的假设下才等价于命中率门槛，这个假设本身要被检验）。

三处对照，缺一不可：
  P0  PIT 特征 + walk-forward     —— 唯一合法的证据
  P1  EOD 特征（分解当日路径）     —— 证明信息真实存在，只是太晚
  P2  `close == 涨停价` 单特征     —— 证明 `封成比` 预测回封是同一句话正反说
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = Path(__file__).resolve().parent / "_audit_reseal_result.json"

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
GEM_PREFIX = ("300", "301")
STAR_PREFIX = ("688", "689")
BJ_PREFIX = ("43", "83", "87", "88", "920")
CAND_ALL = (0.05, 0.10, 0.20, 0.30)
TICK = 0.011
REACH = 0.005
NW_LAG = 5

BASE_HIT = 0.5957     # 26022/43685，A 段实测
R_SEAL, R_BREAK = 162.64, -443.76
COST_BP = 20.0


def breakeven(r_seal, r_break, cost=0.0):
    """p·(r_seal−c) + (1−p)·(r_break−c) = 0 的 p（成本对两腿都收）。"""
    den = r_seal - r_break
    return round((cost - r_break) / den, 4) if abs(den) > 1e-9 else None


HURDLE = breakeven(R_SEAL, R_BREAK)
HURDLE_NET = breakeven(R_SEAL, R_BREAK, COST_BP)


def allowed_L(code):
    if code.startswith(MAIN_PREFIX):
        return (0.05, 0.10)
    if code.startswith(GEM_PREFIX) or code.startswith(STAR_PREFIX):
        return (0.20,)
    if code.startswith(BJ_PREFIX):
        return (0.30,)
    return ()


def nw_t(s, lag=NW_LAG):
    x = pd.Series(s).dropna().to_numpy(float)
    n = len(x)
    if n < 30:
        return None
    m = x.mean()
    u = x - m
    g0 = (u @ u) / n
    var = g0
    for l in range(1, lag + 1):
        var += 2 * (1 - l / (lag + 1)) * ((u[l:] @ u[:-l]) / n)
    se = np.sqrt(max(var, 1e-18) / n)
    return (round(m * 1e4, 2), round(m / se, 2), n)


def auc(y, s):
    y = np.asarray(y).astype(int)
    s = np.asarray(s, float)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0:
        return None
    r = pd.Series(s).rank(method="average").to_numpy()
    return round(float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)), 4)


def fit_logit(X, y, lam=1.0, iters=60):
    """L2 逻辑回归，IRLS/Newton。截距不惩罚。"""
    n, k = X.shape
    Xb = np.column_stack([np.ones(n), X])
    b = np.zeros(k + 1)
    pen = np.ones(k + 1)
    pen[0] = 0.0
    for _ in range(iters):
        z = Xb @ b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))
        w = np.clip(p * (1 - p), 1e-6, None)
        g = Xb.T @ (y - p) - lam * pen * b
        H = (Xb * w[:, None]).T @ Xb + lam * np.diag(pen) + 1e-9 * np.eye(k + 1)
        step = np.linalg.solve(H, g)
        b = b + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return b


def predict(b, X):
    z = np.column_stack([np.ones(len(X)), X]) @ b
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def thresh_for_precision_max(p, y, min_sel=500):
    """在训练集上取使 precision 最大且选中数 >= min_sel 的阈值。"""
    order = np.argsort(-p)
    ys = y[order]
    cum = np.cumsum(ys)
    n = np.arange(1, len(ys) + 1)
    prec = cum / n
    ok = n >= min_sel
    if not ok.any():
        return None, None
    i = int(np.argmax(np.where(ok, prec, -1)))
    return float(p[order][i]), round(float(prec[i]), 4)


def eval_sel(sub, mask, label):
    s = sub[mask]
    if len(s) < 50:
        return {"label": label, "n": int(len(s)), "insufficient": True}
    hit = float(s["y"].mean())
    d = s.groupby("date")["r0_lim"].mean()
    t = nw_t(d)
    rs = float(s.loc[s["y"] == 1, "r0_lim"].mean()) * 1e4 if s["y"].any() else None
    rb = float(s.loc[s["y"] == 0, "r0_lim"].mean()) * 1e4 if (s["y"] == 0).any() else None
    be = breakeven(rs, rb) if (rs is not None and rb is not None) else None
    return {"label": label, "n": int(len(s)), "n_days": int(len(d)),
            "hit": round(hit, 4), "ev_bp": round(float(s["r0_lim"].mean()) * 1e4, 2),
            "nw_t": t[1] if t else None,
            "seal_bp": round(rs, 2) if rs is not None else None,
            "break_bp": round(rb, 2) if rb is not None else None,
            "breakeven_hit": be,
            "gap_pp": round((hit - be) * 100, 2) if be is not None else None}


def main() -> int:
    t0 = time.time()
    fs = sorted((DATA / "kline_daily").glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "high", "low", "close", "amount"]
    df = pd.concat([pq.ParquetFile(f).read(columns=cols).to_pandas() for f in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)

    ex = pd.read_parquet(DATA / "adj_factor" / "all.parquet")[["symbol", "trade_date", "ex_factor"]]
    ex["trade_date"] = pd.to_datetime(ex["trade_date"])

    g = df.groupby("symbol", sort=False)
    df["prev_raw"] = g["close"].shift(1)
    df["nxt_date"] = g["date"].shift(-1)
    df["nxt_open"] = g["open"].shift(-1)
    df["nxt2_date"] = g["date"].shift(-2)
    df["nxt2_open"] = g["open"].shift(-2)

    def exmap(dates):
        v = ex.set_index(["symbol", "trade_date"])["ex_factor"].reindex(
            pd.MultiIndex.from_arrays([df["symbol"], dates])).to_numpy()
        return np.where(np.isfinite(v), v, 1.0)

    exT, ex1, ex2 = exmap(df["date"]), exmap(df["nxt_date"]), exmap(df["nxt2_date"])
    df["adj_prev"] = df["prev_raw"] / exT
    df["ret"] = df["close"] / df["adj_prev"] - 1.0

    ap, hi, lo, cl, op = (df[c].to_numpy() for c in ("adj_prev", "high", "low", "close", "open"))
    code = df["symbol"].str.slice(0, 6)

    n = len(df)
    best_d = np.full(n, np.inf)
    best_L = np.full(n, np.nan)
    best_exp = np.full(n, np.nan)
    for L in CAND_ALL:
        alw = np.array([L in allowed_L(c) for c in code], bool)
        e = np.round(ap * (1 + L), 2)
        d = np.abs(hi - e)
        upd = alw & (d < best_d)
        best_d[upd] = d[upd]
        best_L[upd] = L
        best_exp[upd] = e[upd]
    df["L"], df["lim_px"] = best_L, best_exp
    df["touch"] = np.isfinite(best_exp) & (best_d <= TICK) & (hi >= best_exp - REACH)
    df["lu_px"] = df["touch"] & (np.abs(cl - best_exp) <= TICK)
    df["fill_window"] = df["touch"] & ((df["lim_px"] - lo) > REACH)

    gap1 = (df["nxt_date"] - df["date"]).dt.days
    gap2 = (df["nxt2_date"] - df["nxt_date"]).dt.days
    df["ok_win"] = (gap1 <= 5) & (gap2 <= 5)
    df["r0_lim"] = df["nxt_open"] * ex1 / df["lim_px"] - 1.0

    # 连板高度（收盘封住计数），k_prev = 截至 T-1 的连续封板数
    lu = df["lu_px"].to_numpy()
    sym = df["symbol"].to_numpy()
    cnt = np.zeros(len(df), int)
    run = 0
    for i in range(len(lu)):
        if i and sym[i] == sym[i - 1] and lu[i]:
            run = run + 1 if lu[i - 1] else 1
        elif lu[i]:
            run = 1
        else:
            run = 0
        cnt[i] = run
    df["board_num"] = cnt
    df["k_prev"] = df.groupby("symbol", sort=False)["board_num"].shift(1).fillna(0).astype(int)

    # ---- PIT 特征（<= T 09:30）----
    gb = df.groupby("symbol", sort=False)
    df["open_gap"] = df["open"] / df["adj_prev"] - 1.0
    df["oneword"] = (np.abs(df["open"] - df["lim_px"]) <= TICK).astype(int)
    df["gap_open_lim"] = df["lim_px"] / df["open"] - 1.0
    df["log_amt_prev"] = np.log(gb["amount"].shift(1).clip(lower=1e5))
    df["log_amt_ma20"] = np.log(gb["amount"].shift(1).rolling(20, min_periods=10).mean().clip(lower=1e5))
    df["ret_prev"] = gb["ret"].shift(1)
    df["vol20"] = gb["ret"].shift(1).rolling(20, min_periods=10).std()
    df["ret_5"] = gb["close"].shift(1) / gb["close"].shift(6) - 1.0
    df["log_amt_T"] = np.log(df["amount"].clip(lower=1e5))
    df["amp_T"] = (df["high"] - df["low"]) / df["adj_prev"]
    df["break_depth"] = (df["lim_px"] - df["low"]) / df["lim_px"]
    df["close_is_lim"] = (np.abs(df["close"] - df["lim_px"]) <= TICK).astype(float)

    # ---- 市场情绪状态（T-1 收盘可知）----
    day = (df[df["touch"]].groupby("date")
           .agg(n_touch=("touch", "size"), n_seal=("lu_px", "sum")).reset_index())
    day["break_rate"] = 1.0 - day["n_seal"] / day["n_touch"].clip(lower=1)
    day["prev_break_rate"] = day["break_rate"].shift(1)
    day["prev_n_touch"] = day["n_touch"].shift(1)
    df = df.merge(day[["date", "prev_break_rate", "prev_n_touch"]], on="date", how="left")
    df["log_prev_touch"] = np.log(df["prev_n_touch"].clip(lower=1))

    filt = df["ok_win"] & df["nxt2_open"].notna()
    u = df[filt & df["fill_window"] & df["nxt_open"].notna()].copy()
    u["y"] = u["lu_px"].astype(int)
    u = u.dropna(subset=["open_gap", "ret_prev", "vol20", "ret_5", "prev_break_rate"]).reset_index(drop=True)
    print(f"可成交触及 n={len(u):,} days={u.date.nunique()} 回封率={u.y.mean():.4f} "
          f"{time.time()-t0:.0f}s", flush=True)

    F_PIT = ["k_prev", "open_gap", "oneword", "gap_open_lim", "L", "log_amt_prev",
             "log_amt_ma20", "ret_prev", "vol20", "ret_5", "prev_break_rate", "log_prev_touch"]
    F_EOD = ["break_depth", "amp_T", "log_amt_T"]

    # ---- 替代入场价：T 日收盘买入（封住即涨停价，炸板即折价）----
    df["r0_close"] = df["nxt_open"] * ex1 / df["close"] - 1.0
    tmask = df["touch"] & filt & df["nxt_open"].notna()
    close_entry = {}
    for lab, sel in [("窗口|封住", tmask & df["fill_window"] & df["lu_px"]),
                     ("窗口|炸板", tmask & df["fill_window"] & ~df["lu_px"]),
                     ("窗口|全部", tmask & df["fill_window"])]:
        sub = df[sel]
        if len(sub) < 50:
            continue
        d = sub.groupby("date")["r0_close"].mean()
        tt = nw_t(d)
        close_entry[lab] = {"n": int(len(sub)), "n_days": int(len(d)),
                            "r0_close_bp": round(float(sub["r0_close"].mean()) * 1e4, 2),
                            "nw_t": tt[1] if tt else None}

    # ---- 机制：入场价固定在涨停价，失败代价由同一个特征决定 ----
    u["og_b"] = pd.qcut(u["open_gap"], 6, labels=False, duplicates="drop")
    brk, sl = u[u["y"] == 0], u[u["y"] == 1]
    mech = {"corr_opengap_loss_break": round(float(brk["open_gap"].corr(brk["r0_lim"])), 4),
            "corr_opengap_payoff_seal": round(float(sl["open_gap"].corr(sl["r0_lim"])), 4),
            "sd_bp_break": round(float(brk["r0_lim"].std()) * 1e4, 2),
            "sd_bp_seal": round(float(sl["r0_lim"].std()) * 1e4, 2),
            "by_open_gap": {}}
    for b_, s in u.groupby("og_b"):
        mech["by_open_gap"][f"b{int(b_)+1}"] = {
            "n": int(len(s)), "open_gap_med_pct": round(float(s["open_gap"].median()) * 100, 2),
            "hit": round(float(s["y"].mean()), 4),
            "seal_bp": round(float(s.loc[s["y"] == 1, "r0_lim"].mean()) * 1e4, 2) if s["y"].any() else None,
            "break_bp": round(float(s.loc[s["y"] == 0, "r0_lim"].mean()) * 1e4, 2) if (s["y"] == 0).any() else None,
            "ev_bp": round(float(s["r0_lim"].mean()) * 1e4, 2),
            "breakeven_hit": breakeven(float(s.loc[s["y"] == 1, "r0_lim"].mean()) * 1e4,
                                       float(s.loc[s["y"] == 0, "r0_lim"].mean()) * 1e4)
            if s["y"].any() and (s["y"] == 0).any() else None}

    # ---- 赔率是否时变：机械规则（全窗口、无选择）按季度与情绪分位 ----
    regime, q_ev = {}, {}
    mm = u.copy()
    mm["q"] = mm["date"].dt.to_period("Q").astype(str)
    for q, s in mm.groupby("q"):
        d = s.groupby("date")["r0_lim"].mean()
        t = nw_t(d)
        q_ev[q] = {"n": int(len(s)), "days": int(len(d)),
                   "ev_bp": round(float(s["r0_lim"].mean()) * 1e4, 2),
                   "nw_t": t[1] if t else None, "hit": round(float(s["y"].mean()), 4),
                   "seal_bp": round(float(s.loc[s["y"] == 1, "r0_lim"].mean()) * 1e4, 2) if s["y"].any() else None,
                   "break_bp": round(float(s.loc[s["y"] == 0, "r0_lim"].mean()) * 1e4, 2) if (s["y"] == 0).any() else None}
    for col in ("log_prev_touch", "prev_break_rate"):
        mm["b"] = pd.qcut(mm[col], 5, labels=False, duplicates="drop")
        blk = {}
        for b_, s in mm.groupby("b"):
            d = s.groupby("date")["r0_lim"].mean()
            t = nw_t(d)
            blk[f"q{int(b_) + 1}"] = {"n": int(len(s)), "days": int(len(d)),
                                      "lo": round(float(s[col].min()), 4), "hi": round(float(s[col].max()), 4),
                                      "ev_bp": round(float(s["r0_lim"].mean()) * 1e4, 2),
                                      "nw_t": t[1] if t else None,
                                      "hit": round(float(s["y"].mean()), 4)}
        regime[col] = blk

    # ---- 分季 walk-forward ----
    u["q"] = u["date"].dt.to_period("Q").astype(str)
    qs = sorted(u["q"].unique())
    folds = []
    oos = []
    for q in qs:
        te = u["q"] == q
        tr = u["date"] < u.loc[te, "date"].min()
        if tr.sum() < 4000 or te.sum() < 800 or u.loc[tr, "date"].nunique() < 55:
            folds.append({"q": q, "skipped": True, "n_train": int(tr.sum()), "n_test": int(te.sum())})
            continue
        mu = u.loc[tr, F_PIT].mean()
        sd = u.loc[tr, F_PIT].std().replace(0, 1.0)
        Xtr = ((u.loc[tr, F_PIT] - mu) / sd).to_numpy(float)
        Xte = ((u.loc[te, F_PIT] - mu) / sd).to_numpy(float)
        b = fit_logit(Xtr, u.loc[tr, "y"].to_numpy(int))
        p_tr, p_te = predict(b, Xtr), predict(b, Xte)
        th, prec_tr = thresh_for_precision_max(p_tr, u.loc[tr, "y"].to_numpy(int))
        base_te = float(u.loc[te, "y"].mean())
        rec = {"q": q, "n_train": int(tr.sum()), "n_test": int(te.sum()),
               "days_test": int(u.loc[te, "date"].nunique()),
               "hit_base_test": round(base_te, 4),
               "auc_test": auc(u.loc[te, "y"], p_te),
               "train_prec_max": prec_tr, "train_prec_base": round(float(u.loc[tr, "y"].mean()), 4),
               "theta": round(th, 5) if th is not None else None}
        if th is not None:
            m = p_te >= th
            sub = u.loc[te].assign(p=p_te, sel=m)
            qth = {f"q{int(qq * 100)}": float(np.quantile(p_tr, 1 - qq)) for qq in (0.10, 0.20, 0.30)}
            for k, v in qth.items():
                sub["sel_" + k] = (p_te >= v)
            rec["qth"] = {k: round(v, 5) for k, v in qth.items()}
            e = eval_sel(sub, m, f"{q}-sel")
            rec.update({"hit_sel_test": e.get("hit"), "n_sel_test": e.get("n"),
                        "ev_sel_test": e.get("ev_bp"), "nw_t_sel": e.get("nw_t"),
                        "seal_bp_sel": e.get("seal_bp"), "break_bp_sel": e.get("break_bp"),
                        "breakeven_sel": e.get("breakeven_hit")})
            oos.append(sub)
        folds.append(rec)

    pooled = pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    pool, dec, qrule = {}, {}, {}
    if len(pooled):
        pool = eval_sel(pooled, pooled["sel"].astype(bool).to_numpy(), "walk-forward 选中（PIT）")
        pool["auc_pooled"] = auc(pooled["y"], pooled["p"])
        pool["share_selected"] = round(float(pooled["sel"].mean()), 4)
        pool["fold_hit_sel"] = [f.get("hit_sel_test") for f in folds if f.get("hit_sel_test") is not None]
        # 十分位：门槛是否随筛选移动
        pooled["dec"] = pd.qcut(pooled["p"], 10, labels=False, duplicates="drop")
        for d_, s in pooled.groupby("dec"):
            e = eval_sel(s, np.ones(len(s), bool), f"D{int(d_)+1}")
            dec[f"D{int(d_)+1}"] = {k: e.get(k) for k in
                                    ("n", "n_days", "hit", "ev_bp", "nw_t", "seal_bp", "break_bp",
                                     "breakeven_hit", "gap_pp")}
        for k in ("q10", "q20", "q30"):
            if "sel_" + k in pooled:
                m = pooled["sel_" + k].astype(bool).to_numpy()
                e = eval_sel(pooled, m, k)
                e["share_selected"] = round(float(m.mean()), 4)
                qrule[k] = e

    # ---- P1/P2 泄漏对照（同一 walk-forward，仅换特征）----
    def walk(Xcols, name):
        pred = []
        for q in qs:
            te = u["q"] == q
            tr = u["date"] < u.loc[te, "date"].min()
            if tr.sum() < 4000 or te.sum() < 800 or u.loc[tr, "date"].nunique() < 55:
                continue
            mu = u.loc[tr, Xcols].mean()
            sd = u.loc[tr, Xcols].std().replace(0, 1.0)
            b = fit_logit(((u.loc[tr, Xcols] - mu) / sd).to_numpy(float), u.loc[tr, "y"].to_numpy(int))
            pred.append(pd.DataFrame({"q": q, "y": u.loc[te, "y"].to_numpy(),
                                      "p": predict(b, ((u.loc[te, Xcols] - mu) / sd).to_numpy(float))}))
        if not pred:
            return {"name": name, "insufficient": True}
        z = pd.concat(pred, ignore_index=True)
        return {"name": name, "n": int(len(z)), "auc_oos": auc(z["y"], z["p"]),
                "auc_pit_ref": pool.get("auc_pooled")}

    ctrl = {"P1_eod_noncincular": walk(F_EOD, "EOD 路径特征（break_depth/振幅/成交额）"),
            "P2_label_identity": walk(["close_is_lim"], "close==涨停价 单特征")}

    # ---- 无模型的 PIT 网格：是否存在清过门槛的格子 ----
    u["kb"] = pd.cut(u["k_prev"], [-1, 0, 1, 2, 99], labels=["k0", "k1", "k2", "k3+"])
    grid = {}
    for (kb, ow), s1 in u.groupby(["kb", "oneword"], observed=True):
        for Lv, s2 in s1.groupby("L"):
            grid[f"{kb}|{'一字开' if ow else '低开|高开'}|L={Lv:.2f}"] = eval_sel(
                s2, np.ones(len(s2), bool), f"{kb}|{ow}|{Lv:.2f}")
    grid_n300 = {k: v["ev_bp"] for k, v in grid.items() if not v.get("insufficient") and v["n"] >= 300}
    # 只保留 n>=300 的格子并按 EV 排序
    cand = {k: v for k, v in grid.items() if not v.get("insufficient") and v["n"] >= 300}
    top = dict(sorted(cand.items(), key=lambda kv: -kv[1]["ev_bp"])[:8])

    best = max(cand.values(), key=lambda v: v["hit"]) if cand else None
    gate = {
        "hurdle_hit": round(HURDLE, 4), "hurdle_hit_net20": round(HURDLE_NET, 4),
        "base_hit_in_sample": round(float(u["y"].mean()), 4),
        "oos_hit": pool.get("hit"), "oos_ev_bp": pool.get("ev_bp"), "oos_nw_t": pool.get("nw_t"),
        "oos_n": pool.get("n"), "auc_pooled": pool.get("auc_pooled"),
        "pass_hit": bool(pool.get("hit") and pool["hit"] >= HURDLE),
        "pass_ev": bool(pool.get("ev_bp") is not None and pool["ev_bp"] > 0 and (pool.get("nw_t") or 0) >= 2),
    }
    gate["verdict"] = "PASS" if (gate["pass_hit"] and gate["pass_ev"]) else "REJECT"

    out = {"defs": {"tick": TICK, "reach": REACH, "nw_lag": NW_LAG, "cost_bp": COST_BP,
                    "universe": "touch & fill_window & ok_win", "buy_px": "limit_up", "sell": "T+1 open",
                    "features_pit": F_PIT, "features_eod": F_EOD},
           "universe": {"n": int(len(u)), "days": int(u["date"].nunique()),
                        "hit": round(float(u["y"].mean()), 4),
                        "date_min": str(u.date.min().date()), "date_max": str(u.date.max().date())},
           "folds": folds, "pooled": pool, "controls": ctrl, "deciles": dec,
           "qrule": qrule, "close_entry": close_entry, "mechanism": mech,
           "regime": regime, "quarter_ev": q_ev,
           "grid_n300_ev": grid_n300, "grid_top": top, "grid_best_hit": best,
           "gate": gate}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n== walk-forward (PIT) ==")
    for f in folds:
        if f.get("skipped"):
            print(f"  {f['q']}  跳过 train={f['n_train']} test={f['n_test']}")
        else:
            print(f"  {f['q']}  train={f['n_train']:>6} test={f['n_test']:>5} "
                  f"auc={f['auc_test']}  基准={f['hit_base_test']:.3f}  "
                  f"选中命中={f.get('hit_sel_test')} (n={f.get('n_sel_test')})  "
                  f"EV={f.get('ev_sel_test')}bp  train上限={f['train_prec_max']}")
    print(f"\npooled: {json.dumps(pool, ensure_ascii=False)}")
    print("\n== 十分位（pooled OOS）：命中率 vs 该档自己的盈亏平衡命中率 ==")
    for k, v in dec.items():
        print(f"  {k:<4} n={v['n']:>5} hit={v['hit']:.4f} 平衡点={v['breakeven_hit']} "
              f"gap={v['gap_pp']:>6}pp EV={v['ev_bp']:>8}bp (t={v['nw_t']}) "
              f"封={v['seal_bp']}bp 炸={v['break_bp']}bp")
    print("\n== 固定分位规则 ==")
    for k, v in qrule.items():
        print(f"  {k:<4} 选中占比={v['share_selected']:.3f} n={v['n']:>5} hit={v['hit']:.4f} "
              f"EV={v['ev_bp']:>8}bp (t={v['nw_t']}) 平衡点={v.get('breakeven_hit')}")
    print(f"\n== 收盘买入口径 ==\n  {json.dumps(close_entry, ensure_ascii=False)}")
    print(f"\n== 机制：按 T 日开盘涨幅六分位 ==\n  {json.dumps(mech, ensure_ascii=False)}")
    print(f"\n== 机械规则 EV 按季 ==\n  {json.dumps(q_ev, ensure_ascii=False)}")
    print(f"\n== 机械规则 EV × 情绪分位 ==\n  {json.dumps(regime, ensure_ascii=False)}")
    print(f"\n== 网格 EV 前 8 ==\n  {json.dumps(top, ensure_ascii=False)}")
    print(f"controls: {json.dumps(ctrl, ensure_ascii=False)}")
    print(f"\n筛选: {json.dumps(gate, ensure_ascii=False)}")
    print(f"\nelapsed {time.time()-t0:.0f}s -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
