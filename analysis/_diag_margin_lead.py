"""两融去杠杆是领先变量还是同期影子？

问题（收敛后）：
    融资余额占流通市值的去杠杆速度，在控制过去 20 日收益之后，
    对未来 20 日收益还有没有增量信息？

为什么必须控制过去收益：
    跌 → 强平/减仓 → 余额降。dL20 与 r20_past 天然同向，不控制就会把
    「同期相关」读成「领先预测」。这是本项目的母题 #2（跨口径相除）在时间轴上的版本。

为什么必须用占比口径并同时报绝对口径：
    fin_pct_float = 融资余额 / 流通市值。暴跌时分母也缩，
    绝对余额降 10%、市值降 10% ⇒ 占比不变。两个口径会给出不同答案，
    所以两者都跑，并做 dlnB = dlnL + dlnM 的分解，不许只报一个。

数据：
    data/margin/daily.parquet        东财 RPTA_RZRQ_LSHJ，4,041 日，全市场
    data/backfill_20261117/index_daily_full.parquet  本地指数日线

产物：data/_diag_margin_lead_result.json
用法：backend/.venv/Scripts/python.exe analysis/_diag_margin_lead.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MARGIN = ROOT / "data" / "margin" / "daily.parquet"
INDEX = ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet"
OUT = ROOT / "data" / "_diag_margin_lead_result.json"

H = 20          # 前瞻/回看窗口
NW_LAG = 20     # Newey-West 滞后阶数，与 H 对齐
N_BOOT = 1000
N_PLACEBO = 500


def ols_nw(y: np.ndarray, X: np.ndarray, lag: int = NW_LAG):
    """OLS + Newey-West 标准误。X 不含常数项。"""
    X = np.column_stack([np.ones(len(X)), X])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ b
    n, k = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)
    Xe = X * e[:, None]
    S = Xe.T @ Xe
    for l in range(1, lag + 1):
        w = 1.0 - l / (lag + 1.0)
        A = Xe[l:].T @ Xe[:-l]
        S += w * (A + A.T)
    V = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(V), 0))
    return b, se, n


def build() -> pd.DataFrame:
    m = pd.read_parquet(MARGIN)
    m["date"] = pd.to_datetime(m["date"])
    m = m.sort_values("date").set_index("date")

    idx = pd.read_parquet(INDEX)
    idx["date"] = pd.to_datetime(idx["date"])

    df = m[["fin_balance", "fin_pct_float", "float_mktcap", "index_level"]].copy()
    df["L"] = df["fin_pct_float"]
    df["dL20"] = df["L"] - df["L"].shift(H)
    df["dlnB20"] = np.log(df["fin_balance"]).diff(H)
    df["dlnM20"] = np.log(df["float_mktcap"]).diff(H)

    for name, code in (("hs300", "000300.SH"), ("csi1000", "000852.SH")):
        s = (idx[idx["symbol"] == code][["date", "close"]]
             .dropna().sort_values("date").set_index("date")["close"])
        p = s.reindex(df.index).ffill(limit=3)
        df[f"px_{name}"] = p
        df[f"r20p_{name}"] = p / p.shift(H) - 1.0
        df[f"r20f_{name}"] = p.shift(-H) / p - 1.0

    return df.dropna(subset=["dL20", "r20p_hs300", "r20f_hs300"])


def core(df: pd.DataFrame, xcol: str, bench: str) -> dict:
    """单变量 → 控制动量后的增量。两个模型都报，缺一不可。"""
    y = df[f"r20f_{bench}"].to_numpy() * 100
    x = df[xcol].to_numpy()
    mom = df[f"r20p_{bench}"].to_numpy() * 100

    b1, se1, n = ols_nw(y, x[:, None])
    b2, se2, _ = ols_nw(y, np.column_stack([x, mom]))
    # 同期相关：dL20 与过去收益
    c_past = float(np.corrcoef(x, mom)[0, 1])
    c_fwd = float(np.corrcoef(x, y)[0, 1])
    return {
        "n": int(n),
        "coef_raw": float(b1[1]), "t_raw": float(b1[1] / se1[1]),
        "coef_ctrl": float(b2[1]), "t_ctrl": float(b2[1] / se2[1]),
        "coef_mom": float(b2[2]), "t_mom": float(b2[2] / se2[2]),
        "corr_with_past": c_past, "corr_with_fwd": c_fwd,
    }


def layers(df: pd.DataFrame, xcol: str, bench: str) -> list[dict]:
    """先按过去 20 日收益五等分（控动量），层内再按 x 三分位。"""
    d = df.copy()
    d["past_q"] = pd.qcut(d[f"r20p_{bench}"], 5, labels=False, duplicates="drop")
    out = []
    for q, g in d.groupby("past_q"):
        try:
            g = g.copy()
            g["x_q"] = pd.qcut(g[xcol], 3, labels=False, duplicates="drop")
        except ValueError:
            continue
        for xq, gg in g.groupby("x_q"):
            out.append({
                "past_q": int(q), "x_q": int(xq), "n": int(len(gg)),
                "fwd_mean_pct": float(gg[f"r20f_{bench}"].mean() * 100),
                "fwd_median_pct": float(gg[f"r20f_{bench}"].median() * 100),
            })
    return out


def block_bootstrap_t(df: pd.DataFrame, xcol: str, bench: str, n_boot: int = N_BOOT) -> dict:
    """块自助：长度 H 的块，保留重叠结构。返回系数分布与 t 的经验 CI。"""
    d = df[[xcol, f"r20f_{bench}", f"r20p_{bench}"]].dropna()
    x = d[xcol].to_numpy()
    y = d[f"r20f_{bench}"].to_numpy() * 100
    mom = d[f"r20p_{bench}"].to_numpy() * 100
    n = len(x)
    blocks = int(np.ceil(n / H))
    rng = np.random.default_rng(20261122)
    coefs = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - H, size=blocks)
        pos = np.concatenate([np.arange(s, s + H) for s in starts])[:n]
        b, _, _ = ols_nw(y[pos], np.column_stack([x[pos], mom[pos]]), lag=H)
        coefs.append(b[1])
    coefs = np.array(coefs)
    b_point, se_point, _ = ols_nw(y, np.column_stack([x, mom]))
    return {
        "coef": float(b_point[1]),
        "boot_mean": float(coefs.mean()),
        "ci_lo": float(np.percentile(coefs, 2.5)),
        "ci_hi": float(np.percentile(coefs, 97.5)),
        "p_two_sided": float(2 * min((coefs <= 0).mean(), (coefs >= 0).mean())),
        "n_boot": n_boot,
    }


def placebo(df: pd.DataFrame, xcol: str, bench: str, n: int = N_PLACEBO) -> dict:
    """安慰剂：把 x 的日期索引整体随机平移（保持序列结构，破坏对齐）。

    真实系数应显著落在安慰剂分布之外；若落在中间，说明测到的是趋势/结构而非对齐信息。
    """
    d = df[[xcol, f"r20f_{bench}", f"r20p_{bench}"]].dropna()
    x = d[xcol].to_numpy()
    y = d[f"r20f_{bench}"].to_numpy() * 100
    mom = d[f"r20p_{bench}"].to_numpy() * 100
    b_real, _, _ = ols_nw(y, np.column_stack([x, mom]))
    rng = np.random.default_rng(777)
    null = []
    N = len(x)
    for _ in range(n):
        shift = int(rng.integers(H * 2, N - H * 2))
        xs = np.roll(x, shift)
        b, _, _ = ols_nw(y, np.column_stack([xs, mom]))
        null.append(b[1])
    null = np.array(null)
    pct = float((null <= b_real[1]).mean())
    return {
        "coef_real": float(b_real[1]),
        "null_mean": float(null.mean()),
        "null_sd": float(null.std()),
        "pct_of_null": pct,
        "n_placebo": n,
    }


def split_half(df: pd.DataFrame, xcol: str, bench: str) -> list[dict]:
    mid = df.index[len(df) // 2]
    out = []
    for label, part in (("first", df[df.index < mid]), ("second", df[df.index >= mid])):
        if len(part) < 400:
            continue
        y = part[f"r20f_{bench}"].to_numpy() * 100
        x = part[xcol].to_numpy()
        mom = part[f"r20p_{bench}"].to_numpy() * 100
        b, se, n = ols_nw(y, np.column_stack([x, mom]))
        out.append({
            "half": label,
            "from": str(part.index.min().date()), "to": str(part.index.max().date()),
            "n": int(n), "coef": float(b[1]), "t": float(b[1] / se[1]),
        })
    return out


def delever_cycles(df: pd.DataFrame, thresh: float = -0.15) -> dict:
    """去杠杆周期计数：dL20 连续低于阈值 → 一次周期。N 是段数，不是天数。"""
    flag = (df["dL20"] < thresh).to_numpy()
    starts = np.where(flag & ~np.r_[False, flag[:-1]])[0]
    ends = np.where(flag & ~np.r_[flag[1:], False])[0]
    lengths = (ends - starts + 1)
    keep = lengths >= 10          # 至少 10 个交易日才算一段
    return {
        "threshold_pct": thresh,
        "cycles": int(keep.sum()),
        "days": int(lengths[keep].sum()),
        "median_len": float(np.median(lengths[keep])) if keep.any() else None,
        "max_len": int(lengths[keep].max()) if keep.any() else None,
    }


def horizon_scan(df: pd.DataFrame, bench: str, horizons=(5, 10, 20, 60)) -> list[dict]:
    """同一口径扫不同窗口。视频讲的是「每天 200 亿、连续 5 日」，
    如果只测 20 日就断言「无信息」，那是对它不公平。"""
    out = []
    for h in horizons:
        d = df[[f"px_{bench}", "L"]].copy()
        d["x"] = d["L"] - d["L"].shift(h)
        d["rp"] = d[f"px_{bench}"] / d[f"px_{bench}"].shift(h) - 1.0
        d["rf"] = d[f"px_{bench}"].shift(-h) / d[f"px_{bench}"] - 1.0
        d = d.dropna()
        y = d["rf"].to_numpy() * 100
        x = d["x"].to_numpy()
        mom = d["rp"].to_numpy() * 100
        b, se, n = ols_nw(y, np.column_stack([x, mom]), lag=h)
        out.append({
            "H": h, "n": int(n),
            "coef_ctrl": float(b[1]), "t_ctrl": float(b[1] / se[1]),
            "coef_mom": float(b[2]), "t_mom": float(b[2] / se[2]),
        })
    return out


def streak_events(df: pd.DataFrame, bench: str, k: int = 5,
                  horizons=(5, 10, 20)) -> dict:
    """视频的第一个信号：融资余额「连续 k 日下降」之后，第一次不再下降。

    事件日 t = 余额变化首次 ≥0，且 t-1 时已连续下降 ≥k 日。
    t 日收盘后才知道 ⇒ t+1 收盘入场，持 h 日（不偷看未来）。
    """
    bal = df["fin_balance"]
    down = bal.diff() < 0
    run = down.groupby((~down).cumsum()).cumsum()
    ev = (run.shift(1) >= k) & (~down)
    pos = np.where(ev.to_numpy())[0]
    px = df[f"px_{bench}"].to_numpy()
    n = len(px)
    rows = []
    for i in pos:
        entry = i + 1                      # t+1 收盘入场
        rec = {"date": str(df.index[i].date()), "prior_down_days": int(run.iloc[i - 1])}
        for h in horizons:
            j = entry + h
            rec[f"fwd{h}_pct"] = (px[j] / px[entry] - 1.0) * 100 if j < n else None
        rows.append(rec)

    base = {}
    for h in horizons:
        r = px[h:] / px[:-h] - 1.0
        base[f"fwd{h}_pct"] = float(np.nanmean(r) * 100)

    # 事件扎堆 ⇒ N 是簇，不是事件：相邻事件间隔 <20 交易日算同一簇
    clusters = 0
    if len(pos):
        clusters = 1 + int(sum(1 for a, b in zip(pos[:-1], pos[1:]) if b - a >= 20))

    return {"k": k, "events": len(rows), "clusters": clusters, "rows": rows, "baseline": base}


def main() -> None:
    df = build()
    print(f"panel: {len(df)} 日  {df.index.min().date()} → {df.index.max().date()}")
    print(f"分解自洽: dlnB = dlnL + dlnM 最大残差 "
          f"{(df['dlnB20'] - (np.log(df['L'] / df['L'].shift(H)) + df['dlnM20'])).abs().max():.4f}")

    result: dict = {"window": [str(df.index.min().date()), str(df.index.max().date())],
                    "n_days": int(len(df)), "H": H}

    for xcol, tag in (("dL20", "占流通市值（百分点）"), ("dlnB20", "绝对余额（对数变化）")):
        result[tag] = {}
        for bench in ("hs300", "csi1000"):
            r = core(df, xcol, bench)
            r["bootstrap"] = block_bootstrap_t(df, xcol, bench, n_boot=300)
            r["placebo"] = placebo(df, xcol, bench, n=200)
            r["split"] = split_half(df, xcol, bench)
            r["layers"] = layers(df, xcol, bench)
            result[tag][bench] = r
            print(f"\n[{tag}] {bench}: n={r['n']}  corr(past)={r['corr_with_past']:+.3f}  corr(fwd)={r['corr_with_fwd']:+.3f}")
            print(f"  单变量: coef={r['coef_raw']:+.3f} t={r['t_raw']:+.2f}")
            print(f"  控动量: coef={r['coef_ctrl']:+.3f} t={r['t_ctrl']:+.2f}  (动量 t={r['t_mom']:+.2f})")
            print(f"  bootstrap: CI95=[{r['bootstrap']['ci_lo']:+.3f},{r['bootstrap']['ci_hi']:+.3f}] p={r['bootstrap']['p_two_sided']:.3f}")
            print(f"  placebo: 真实 {r['placebo']['coef_real']:+.3f} 在零分布第 {r['placebo']['pct_of_null']*100:.0f} 百分位"
                  f"（null sd={r['placebo']['null_sd']:.3f}）")
            for s in r["split"]:
                print(f"  分半 {s['half']}: {s['from']}→{s['to']} coef={s['coef']:+.3f} t={s['t']:+.2f}")

    result["delever_cycles"] = delever_cycles(df)
    print(f"\n去杠杆周期: {result['delever_cycles']}")

    for bench in ("hs300", "csi1000"):
        scan = horizon_scan(df, bench)
        result[f"horizon_scan_{bench}"] = scan
        print(f"\n窗口扫描 · {bench}（占比口径，已控动量）:")
        for r in scan:
            print(f"  H={r['H']:>2} n={r['n']}  coef={r['coef_ctrl']:+.3f} t={r['t_ctrl']:+.2f}"
                  f"  (动量 t={r['t_mom']:+.2f})")

    for bench in ("hs300", "csi1000"):
        for k in (3, 5):
            ev = streak_events(df, bench, k=k)
            result[f"streak_{bench}_k{k}"] = {
                "events": ev["events"], "clusters": ev["clusters"], "baseline": ev["baseline"],
            }
            print(f"\n连续下降≥{k}日后首次回升 · {bench}: 事件 {ev['events']} 个 / 独立簇 {ev['clusters']}")
            if ev["rows"]:
                e = pd.DataFrame(ev["rows"])
                for h in (5, 10, 20):
                    col = f"fwd{h}_pct"
                    s = e[col].dropna()
                    if len(s) > 1:
                        t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))
                        print(f"  {h:>2}日: 均值 {s.mean():+.2f}%  中位 {s.median():+.2f}%"
                              f"  t={t:+.2f}  全样本对照 {ev['baseline'][col]:+.2f}%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {OUT}")


if __name__ == "__main__":
    main()
