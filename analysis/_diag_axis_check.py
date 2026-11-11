"""检验「板数轴 / 排序器轴」的口径与可算性。

四个问题：
 [0] 网格箱是否可按交易日切前/后两半（k_prev 必须全样本先算再切）
 [1] 分半后的 EV/t：8 个网格箱的稳定性，以及 PIT 式的「前半选箱→后半报数」
 [2] 「板数轴 132bp」按样本份额加权后对总 EV 的贡献是多少
 [3] ow=1（T 日一字开）的 489 行到底是「买不到」还是「开了板能买到」

构建路径与 analysis/_audit_intraday_budget.build_universe 相同，只额外保留
best_L 与 k_prev/oneword，不抄任何产物数字。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = Path(__file__).resolve().parent / "_diag_axis_check_20261110.txt"
LINES: list[str] = []

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
GEM_PREFIX = ("300", "301")
STAR_PREFIX = ("688", "689")
BJ_PREFIX = ("43", "83", "87", "88", "920")
CAND_ALL = (0.05, 0.10, 0.20, 0.30)
TICK = 0.011
REACH = 0.005


def p(s: str) -> None:
    print(s, flush=True)
    LINES.append(s)


def allowed_L(code: str):
    if code.startswith(MAIN_PREFIX):
        return (0.05, 0.10)
    if code.startswith(GEM_PREFIX) or code.startswith(STAR_PREFIX):
        return (0.20,)
    if code.startswith(BJ_PREFIX):
        return (0.30,)
    return ()


def nw_t(s, lag=5):
    x = pd.Series(s).dropna().to_numpy(float)
    n = len(x)
    if n < 30:
        return None
    m = x.mean()
    u = x - m
    var = (u @ u) / n
    for l in range(1, lag + 1):
        var += 2 * (1 - l / (lag + 1)) * ((u[l:] @ u[:-l]) / n)
    se = np.sqrt(max(var, 1e-18) / n)
    return round(m * 1e4, 2), round(m / se, 2), n


def seg(sub: pd.DataFrame):
    """一个子集的 EV（bp）、NW t、n、n_days、se。"""
    if len(sub) < 30:
        return None
    d = sub.groupby("date")["r0_lim"].mean()
    t = nw_t(d)
    if t is None:
        return None
    ev = float(sub["r0_lim"].mean()) * 1e4
    return dict(n=int(len(sub)), days=int(len(d)), ev=round(ev, 2),
                t=t[1], se=round(abs(ev) / abs(t[1]), 2) if t[1] else None)


def build():
    t0 = time.time()
    fs = sorted((DATA / "kline_daily").glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "high", "low", "close", "amount"]
    df = pd.concat([pq.ParquetFile(f).read(columns=cols).to_pandas() for f in fs],
                   ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)

    ex = pd.read_parquet(DATA / "adj_factor" / "all.parquet")[
        ["symbol", "trade_date", "ex_factor"]]
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

    exT, ex1 = exmap(df["date"]), exmap(df["nxt_date"])
    df["adj_prev"] = df["prev_raw"] / exT
    df["ret"] = df["close"] / df["adj_prev"] - 1.0

    ap, hi, lo, cl = (df[c].to_numpy() for c in ("adj_prev", "high", "low", "close"))
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

    # k_prev：全样本逐股滚动，切分前必须算完（否则前段起点全被当成 k0）
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

    gb = df.groupby("symbol", sort=False)
    df["open_gap"] = df["open"] / df["adj_prev"] - 1.0
    df["oneword"] = (np.abs(df["open"] - df["lim_px"]) <= TICK).astype(int)
    df["ret_prev"] = gb["ret"].shift(1)
    df["vol20"] = gb["ret"].shift(1).rolling(20, min_periods=10).std()
    df["ret_5"] = gb["close"].shift(1) / gb["close"].shift(6) - 1.0

    day = (df[df["touch"]].groupby("date")
           .agg(n_touch=("touch", "size"), n_seal=("lu_px", "sum")).reset_index())
    day["break_rate"] = 1.0 - day["n_seal"] / day["n_touch"].clip(lower=1)
    day["prev_break_rate"] = day["break_rate"].shift(1)
    df = df.merge(day[["date", "prev_break_rate"]], on="date", how="left")

    filt = df["ok_win"] & df["nxt2_open"].notna()
    u = df[filt & df["fill_window"] & df["nxt_open"].notna()].copy()
    u["y"] = u["lu_px"].astype(int)
    u = u.dropna(subset=["open_gap", "ret_prev", "vol20", "ret_5", "prev_break_rate"])
    u = u.reset_index(drop=True)
    u["kb"] = pd.cut(u["k_prev"], [-1, 0, 1, 2, 99], labels=["k0", "k1", "k2", "k3+"])
    return df, u, time.time() - t0


def main() -> int:
    df, u, el = build()
    p(f"universe: n={len(u):,} days={u.date.nunique()} "
      f"date={u.date.min().date()}→{u.date.max().date()} 构建 {el:.0f}s")

    # ---------- [0] 可算性 ----------
    p("\n[0] 网格箱能否按交易日切分")
    p(f"    u 携带列: date={('date' in u.columns)} kb={('kb' in u.columns)} "
      f"oneword={('oneword' in u.columns)} L={('L' in u.columns)} "
      f"r0_lim={('r0_lim' in u.columns)}")
    dates = np.array(sorted(u["date"].unique()))
    mid = dates[len(dates) // 2]
    p(f"    交易日 {len(dates)} 个；切点 date={pd.Timestamp(mid).date()} "
      f"前半 {len(dates) // 2} 天 / 后半 {len(dates) - len(dates) // 2} 天")
    p("    ⇒ 只需在 u 上加一个 date 掩码；NW(lag=5) 按 date 聚合天然可分段。")
    p("    硬约束：k_prev / prev_break_rate 必须全样本算完再切（本例已如此），"
      "在两半内各自重算会把每半起点全判成 k0。")

    h1 = u[u["date"] < mid]
    h2 = u[u["date"] >= mid]
    p(f"    前半 n={len(h1):,} days={h1.date.nunique()} EV={h1.r0_lim.mean()*1e4:.2f}bp")
    p(f"    后半 n={len(h2):,} days={h2.date.nunique()} EV={h2.r0_lim.mean()*1e4:.2f}bp")

    # ---------- [1] 分半 ----------
    def half_row(sub, tag):
        a, b = seg(sub[sub["date"] < mid]), seg(sub[sub["date"] >= mid])
        if not a or not b:
            return None
        diff = b["ev"] - a["ev"]
        se = float(np.hypot(a["se"], b["se"]))
        return dict(tag=tag, n=len(sub), a=a, b=b, diff=round(diff, 2),
                    dt=round(diff / se, 2) if se else None)

    p("\n[1] 8 个网格箱（n>=300）分半：EV / NW t / n_days")
    rows = []
    for (kb, ow), s1 in u.groupby(["kb", "oneword"], observed=True):
        for Lv, s2 in s1.groupby("L"):
            if len(s2) < 300:
                continue
            tag = f"{kb}|{'一字开' if ow else '低开|高开'}|L={Lv:.2f}"
            r = half_row(s2, tag)
            if r:
                rows.append(r)
    p(f"    {'箱':<24}{'n':>7}{'前EV':>9}{'前t':>7}{'后EV':>9}{'后t':>7}"
      f"{'差':>9}{'差t':>7}")
    for r in sorted(rows, key=lambda x: -x["a"]["ev"]):
        p(f"    {r['tag']:<24}{r['n']:>7}{r['a']['ev']:>9.2f}{r['a']['t']:>7.2f}"
          f"{r['b']['ev']:>9.2f}{r['b']['t']:>7.2f}{r['diff']:>9.2f}{r['dt']:>7.2f}")

    p("\n[1b] PIT：前半按 EV 选 1 个箱 → 后半报该箱（消除「先看全样本再挑」的选择偏差）")
    for topn in (1, 2):
        best = sorted(rows, key=lambda x: -x["a"]["ev"])[:topn]
        sel = [r for r in best]
        n1 = sum(r["a"]["n"] for r in sel)
        n2 = sum(r["b"]["n"] for r in sel)
        e1 = sum(r["a"]["ev"] * r["a"]["n"] for r in sel) / n1
        e2 = sum(r["b"]["ev"] * r["b"]["n"] for r in sel) / n2
        p(f"    top{topn}: 前半选中 {[r['tag'] for r in sel]}  前半 EV={e1:.2f} "
          f"→ 后半 EV={e2:.2f}bp (n={n2:,})")

    # ---------- [2] 加权贡献 ----------
    p("\n[2] 「板数轴」的跨度 vs 按样本份额加权后的实际贡献（固定 L=0.10 非一字）")
    base = u[(u["L"] == 0.10) & (u["oneword"] == 0)]
    p(f"    L=0.10 非一字池: n={len(base):,} days={base.date.nunique()} "
      f"EV={base.r0_lim.mean()*1e4:.2f}bp")
    tot, acc = len(base), 0.0
    for kb, s in base.groupby("kb", observed=True):
        r = seg(s)
        w = len(s) / tot
        acc += w * r["ev"]
        p(f"      {kb:<4} n={len(s):>6,} 份额={w:>6.1%} 每日只数={len(s)/s.date.nunique():>5.1f} "
          f"EV={r['ev']:>8.2f} t={r['t']:>6.2f}")
    p(f"    加权 EV={acc:.2f}bp；若把全部按 k0 处理（−26.11）为 {seg(base[base.kb=='k0'])['ev']:.2f}bp")
    p(f"    ⇒ 132bp 的跨度只作用在 {1-seg(base[base.kb=='k0'])['n']/tot:.1%} 的样本上，"
      f"对池均值的影响 = {acc - seg(base[base.kb=='k0'])['ev']:.2f}bp")

    p("\n[2b] 全池按 L（涨停幅度制度）分解 —— 这是比 k_prev 更大的一根轴")
    for Lv, s in u.groupby("L"):
        r = seg(s)
        p(f"      L={Lv:.2f} n={len(s):>6,} 份额={len(s)/len(u):>6.1%} "
          f"EV={r['ev']:>8.2f} t={r['t']:>6.2f} hit={s.y.mean():.4f} "
          f"break={s.loc[s.y==0,'r0_lim'].mean()*1e4:>8.1f}")

    # ---------- [3] 一字开真相 ----------
    p("\n[3] ow=1（T 日开盘即涨停价）的 489 行到底是什么")
    touch_ok = df[df["ok_win"] & df["nxt_open"].notna() & df["touch"]]
    ct = pd.crosstab(touch_ok["oneword"], touch_ok["fill_window"],
                     rownames=["oneword"], colnames=["fill_window"])
    p("    touch & ok_win 上 oneword × fill_window 交叉表:")
    for idx, row in ct.iterrows():
        p(f"      oneword={idx}: fill_window=False(全天封死)={row.get(False,0):>6,}  "
          f"fill_window=True(开板可买)={row.get(True,0):>6,}")
    dead = touch_ok[(touch_ok["oneword"] == 1) & (~touch_ok["fill_window"])]
    live = touch_ok[(touch_ok["oneword"] == 1) & (touch_ok["fill_window"])]
    r_dead, r_live = seg(dead), seg(live)
    p(f"    一字且全天封死(买不到): n={len(dead):,} EV={r_dead['ev']:.2f}bp t={r_dead['t']:.2f}")
    p(f"    一字但开了板(买得到): n={len(live):,} EV={r_live['ev']:.2f}bp t={r_live['t']:.2f}")
    p(f"    ⇒ 池里的 489 是后者（买得到），不是「纸面买到实际排不上」；"
      f"买不到的样本被 fill_window 排除在 u 之外，其 EV 单独列在上面。")

    # 买不到的一字板占「一字板全部」的比例，与其 EV 对池子的影响
    n_all = len(u)
    p(f"    若把买不到的一字板按比例计入（{len(dead):,}/{n_all:,}={len(dead)/n_all:.2%}），"
      f"对池均值的影响 = {len(dead)/n_all*(r_dead['ev']-u.r0_lim.mean()*1e4):.2f}bp")

    # ---------- [4] 逐点余量/σ ----------
    p("\n[4] 复核「k∈[5,15] 全落在一个 σ 内」：余量/σ 必须逐点算，σ 随 k 单调降")
    tab = [(5, -2.3, 0.262), (6, 2.6, 0.237), (8, 9.8, 0.187), (11, 16.2, 0.147),
           (14, 19.7, 0.134), (18, 21.6, 0.131), (25, 21.2, 0.125), (40, 22.3, 0.123)]
    p(f"    {'k':>3}{'余量':>8}{'σ':>8}{'余量/σ':>9}{'是否含0':>9}")
    for k, mg, sd in tab:
        p(f"    {k:>3}{mg:>7.1f}%{sd:>8.3f}{mg/100/sd:>9.2f}"
          f"{'含0' if abs(mg/100) < sd else '不含0':>9}")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
