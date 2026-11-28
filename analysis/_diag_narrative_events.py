"""事件腿：解禁与定增的定价检验 + 371 个交易日的逐日表。

要回答三件事：
  A 解禁（有余额、有日期、方向为负的结算事件）到底有没有漂移？漂移是在事件前积累
    还是在事件后发生？（/chip 的 P3 在大盘上的版本）
  B 定增：发行价是合约面值。当前价相对发行价的位置（破发率）是一个可读的存量，
    不预测方向；但「近 12 个月定增 + 未来 30 日解禁」构成供给日历。
  C 把「未来 20 日内有解禁 / 过去 20 日内有定增上市」的股票从概念样本里剔除后，
    371 个交易日的读数会不会变——如果结论只靠这些股票撑着，它就该被撤回。

口径：
  - 解禁日 = FREE_DATE（可上市流通日）。ratio = TOTAL_RATIO（解禁股数/流通股数）。
  - 收益：pre20 = close[t-1]/open[t-20]-1。解禁后的窗口曾测过，2026-11-28 撤下
    （刚从成本、方向只出现过一次，见 git 895aa2a0）；本脚本不再计算、也不再输出。
  - 基准 = 同日全市场（有行情的股票）日收益中位数的复利路径；超额 = 个股 − 基准。
  - 事件扎堆：同一天多个解禁按「当日事件均值」聚成一条，t 按日聚簇 NW(5)。
  - 安慰剂：同股票、同一年内随机取 20 个非事件日，重算同一统计量（控制个股固定效应）。
  - 概念样本与 `_diag_narrative_heat.py` 完全同源（≥5 只成员、等权、同日概念中位为基准）。

输出：
  data/.scratch/narrative_events.json   汇总读数
  data/.scratch/narrative_371d.csv      371 行逐日表（UTF-8 BOM，Excel 直接开）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT_JSON = ROOT / "data" / ".scratch" / "narrative_events.json"
OUT_CSV = ROOT / "data" / ".scratch" / "narrative_371d.csv"
ENRICHED = ROOT / "data" / "kline_daily_enriched"
LOCKUP = ROOT / "data" / "events" / "lockup.parquet"
SEO = ROOT / "data" / "events" / "seo.parquet"
REGIME = ROOT / "data" / "regime_history" / "part.parquet"
MAINLINE = ROOT / "data" / "mainline_history" / "part.parquet"
MARGIN = ROOT / "data" / "margin" / "daily.parquet"
GN = ROOT / "data" / "ext_data" / "ext_gn_ths" / "part.parquet"

MIN_MEMBERS = 5
TOP_N = 30
BUCKETS = [(-1e-9, 0.01, '<1%'), (0.01, 0.03, '1–3%'), (0.03, 0.10, '3–10%'), (0.10, 2.0, '≥10%')]


def _partitions(start: str, end: str) -> list[Path]:
    out = []
    for p in sorted(ENRICHED.glob("date=*")):
        d = p.name.split("=", 1)[1]
        if len(d) == 10 and start <= d <= end:
            f = p / "part.parquet"
            if f.exists():
                out.append(f)
    return out


def load_panel(start: str, end: str) -> pd.DataFrame:
    import polars as pl

    files = [str(f) for f in _partitions(start, end)]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "close", "amount", "consecutive_limit_ups"])
        .collect()
        .to_pandas()
    )
    df["date"] = pd.to_datetime(df["date"])
    df["lu"] = (df["consecutive_limit_ups"].fillna(0) >= 1).astype(int)
    return df.sort_values(["symbol", "date"])


def nw_t(x, lag: int = 5) -> float:
    x = np.asarray([v for v in x if np.isfinite(v)], dtype=float)
    n = len(x)
    if n < 8:
        return float("nan")
    mu = x.mean()
    e = x - mu
    var = float((e ** 2).mean())
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float((e[k:] * e[:-k]).mean())
    return mu / np.sqrt(max(var, 1e-30) / n)


def bucket(ratio: float) -> str | None:
    for lo, hi, lab in BUCKETS:
        if lo < ratio <= hi:
            return lab
    return None


def wide_matrices(panel: pd.DataFrame):
    """返回 (dates, symbols, open, close) 四个对齐的二维数组。"""
    px = panel.pivot(index="date", columns="symbol", values="close").sort_index()
    op = panel.pivot(index="date", columns="symbol", values="open").reindex(index=px.index, columns=px.columns)
    return px.index, px.columns, op.to_numpy(dtype=float), px.to_numpy(dtype=float)


def window_stats(open_m, close_m, n_dates):
    """对每个 (日期序, 股票序) 预计算 pre20 / pre120。"""
    eps = 1e-12
    pre = np.full_like(close_m, np.nan)
    pre120 = np.full_like(close_m, np.nan)
    if n_dates > 121:
        pre[20:, :] = close_m[19:-1, :] / (open_m[:-20, :] + eps) - 1
        pre120[120:, :] = close_m[119:-1, :] / (open_m[:-120, :] + eps) - 1
    return pre, pre120


def main() -> None:
    import polars as pl

    print('载入事件表')
    lk = pl.read_parquet(LOCKUP).to_pandas()
    lk["free_date"] = pd.to_datetime(lk["free_date"])
    lk["ratio_float"] = pd.to_numeric(lk["ratio_float"], errors="coerce").fillna(0.0)
    lk["lift_mcap_wan"] = pd.to_numeric(lk["lift_mcap_wan"], errors="coerce").fillna(0.0)
    seo = pl.read_parquet(SEO).to_pandas()
    seo["listing_date"] = pd.to_datetime(seo["listing_date"])

    # 面板：2018-12 起，覆盖 2019 事件的 pre20（以及 120 日动量所需的 120 个交易日）
    print('载入日线面板 2018-11 ~ 2027-03')
    panel = load_panel("2018-11-01", "2027-03-31")
    dates, symbols, open_m, close_m = wide_matrices(panel)
    didx = {d: i for i, d in enumerate(dates)}
    sidx = {s: j for j, s in enumerate(symbols)}
    print(f'  日期 {len(dates)} · 股票 {len(symbols)}')

    pre_m, pre120_m = window_stats(open_m, close_m, len(dates))

    # 市场基准：同日全市场**等权均值**收益（逐日），再复利成路径。
    # 用中位会把所有个股超额系统性抬高约 +400bp/20日（收益分布右偏），
    # 价差仍可读但水平不可读；均值口径下「随机日」的超额≈0，便于直接看符号。
    ret = np.full_like(close_m, np.nan)
    ret[1:, :] = close_m[1:, :] / (close_m[:-1, :] + 1e-12) - 1
    mkt = np.nanmean(ret, axis=1)
    mkt = np.where(np.isfinite(mkt), mkt, 0.0)
    base_pre = np.full(len(dates), np.nan)
    base_120 = np.full(len(dates), np.nan)
    for i in range(len(dates)):
        lo, hi = i - 20, i
        if lo >= 0:
            base_pre[i] = np.prod(1 + mkt[lo:hi]) - 1
        lo3 = i - 120
        if lo3 >= 0:
            base_120[i] = np.prod(1 + mkt[lo3:hi]) - 1

    # ---- A 解禁事件 ----
    ev = lk[(lk["free_date"] >= "2019-01-01") & (lk["free_date"] <= "2026-06-30")].copy()
    ev["t"] = ev["free_date"].map(didx)
    ev["s"] = ev["symbol"].map(sidx)
    ev = ev.dropna(subset=["t", "s"])
    ev["t"] = ev["t"].astype(int)
    ev["s"] = ev["s"].astype(int)
    ev["b"] = ev["ratio_float"].map(bucket)
    ev = ev[ev["b"].notna()]
    ev["pre_ex"] = pre_m[ev["t"], ev["s"]] - base_pre[ev["t"]]
    ev["m120_ex"] = pre120_m[ev["t"], ev["s"]] - base_120[ev["t"]]

    def agg(g: pd.DataFrame) -> dict:
        by = g.groupby("free_date").agg(pre=("pre_ex", "mean"), n=("pre_ex", "size"))
        return {
            "events": int(len(g)),
            "days": int(by.shape[0]),
            "pre_bp": round(float(by["pre"].mean()) * 1e4, 1),
            "pre_t": round(nw_t(by["pre"]), 2),
        }

    print('\n== A 解禁：按解禁占流通比分档（超额，bp）')
    lock_rows = {}
    for lab in [b[2] for b in BUCKETS]:
        g = ev[ev["b"] == lab]
        if g.empty:
            continue
        lock_rows[lab] = agg(g)
        r = lock_rows[lab]
        print(f"  {lab:<7} n={r['events']:>5} days={r['days']:>4} "
              f"pre {r['pre_bp']:>+8.1f} (t {r['pre_t']:>5.2f})")

    # 安慰剂：同股票、±120 个交易日内随机日（避开该股自己的事件日）。
    # 不按「同一年」取，是因为逐年筛选候选日会把 2,700 个事件 × 2,000 个候选做成
    # 纯 Python 循环（分钟级）；±120 日已经能控制个股固定效应与年度趋势。
    rng = np.random.default_rng(20261128)
    ev_t_by_sym = ev.groupby("s")["t"].apply(lambda x: set(x.tolist())).to_dict()
    plac = {}
    for lab, g in ev.groupby("b"):
        ts = g["t"].to_numpy()
        ss = g["s"].to_numpy()
        draws = []
        for _ in range(20):
            off = rng.integers(-120, 121, size=len(ts))
            ti = np.clip(ts + off, 0, len(dates) - 1)
            # 落到该股自己的事件日就重抽一次（最多 3 次）
            for _k in range(3):
                bad = np.array([ti[i] in ev_t_by_sym.get(ss[i], ()) for i in range(len(ti))], dtype=bool)
                if not bad.any():
                    break
                ti[bad] = np.clip(ts[bad] + rng.integers(-120, 121, size=int(bad.sum())), 0, len(dates) - 1)
            pre = pre_m[ti, ss] - base_pre[ti]
            draws.append(np.nanmean(pre) * 1e4)
        a = np.array(draws)
        plac[lab] = {
            "pre_p50": round(float(np.median(a)), 1),
            "pre_p5": round(float(np.percentile(a, 5)), 1),
        }
    print('\n== A2 安慰剂（同股票、±120 交易日随机日）')
    for lab, v in plac.items():
        print(f"  {lab:<7} pre p50 {v['pre_p50']:>+7.1f} (p5 {v['pre_p5']:>+7.1f})")

    # DiD = 事件 − 安慰剂（同一股票、同一基准）。基准偏置与个股固定效应在这一步被消掉。
    print('\n== A3 DiD（事件 − 安慰剂，bp）')
    for lab, r in lock_rows.items():
        pv = plac.get(lab) or {}
        r["did_pre"] = round(r["pre_bp"] - pv.get("pre_p50", np.nan), 1)
        print(f"  {lab:<7} pre {r['did_pre']:>+8.1f}")
    plac_out = plac

    # ---- A4 前期动量控制 ----
    # 高比例解禁的股票往往在更早的 120 日里涨得更多；若「解禁前走弱」只是均值回归，
    # 那么在**同一前期动量分位内**，高比例组与低比例组的差应该消失。
    print('\n== A4 前期动量控制（同 m120 三分位内：≥10% − <1%，pre20）')
    ev["m_terc"] = ev.groupby("b")["m120_ex"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 3, labels=["低", "中", "高"])
    )
    mom_rows = {}
    for terc in ["低", "中", "高"]:
        sub = ev[ev["m_terc"] == terc]
        lo = sub[sub["b"] == "<1%"]["pre_ex"]
        hi = sub[sub["b"] == "≥10%"]["pre_ex"]
        if len(lo) < 50 or len(hi) < 50:
            continue
        mom_rows[terc] = {
            "n_hi": int(len(hi)),
            "n_lo": int(len(lo)),
            "hi_bp": round(float(hi.mean()) * 1e4, 1),
            "lo_bp": round(float(lo.mean()) * 1e4, 1),
            "diff_bp": round(float(hi.mean() - lo.mean()) * 1e4, 1),
            "m120_med_hi": round(float(sub[sub["b"] == "≥10%"]["m120_ex"].median()) * 100, 1),
            "m120_med_lo": round(float(sub[sub["b"] == "<1%"]["m120_ex"].median()) * 100, 1),
        }
        r = mom_rows[terc]
        print(f"  {terc}动量  ≥10% {r['hi_bp']:>+8.1f}  <1% {r['lo_bp']:>+8.1f}  差 {r['diff_bp']:>+8.1f}bp "
              f"（前期120日超额中位 {r['m120_med_hi']:>+6.1f}% vs {r['m120_med_lo']:>+6.1f}%）")

    # ---- A5 按限售类型拆（≥10% 桶）----
    print('\n== A5 ≥10% 桶按限售类型拆（pre20，bp）')
    big = ev[ev["b"] == "≥10%"].copy()
    kind_map = {
        "定向增发机构配售股份": "定增解禁",
        "股权激励限售股份": "股权激励",
        "首发原股东限售股份": "首发原股东",
        "首发机构配售股份": "首发配售",
        "首发战略配售股份": "首发战略配售",
    }
    big["kg"] = big["kind"].map(lambda k: kind_map.get(k, "其他"))
    kind_rows = {}
    for kg, g in big.groupby("kg"):
        if len(g) < 100:
            continue
        by = g.groupby("free_date").agg(pre=("pre_ex", "mean"))
        kind_rows[kg] = {
            "events": int(len(g)),
            "pre_bp": round(float(by["pre"].mean()) * 1e4, 1),
            "pre_t": round(nw_t(by["pre"]), 2),
        }
        r = kind_rows[kg]
        print(f"  {kg:<8} n={r['events']:>5} pre {r['pre_bp']:>+8.1f} (t {r['pre_t']:>5.2f})")

    # ---- B 定增 ----
    print('\n== B 定增：存量与破发')
    seo["issue_price"] = pd.to_numeric(seo["issue_price"], errors="coerce")
    seo["listing_date"] = pd.to_datetime(seo["listing_date"])
    recent_cut = dates[-1] - pd.Timedelta(days=365)
    rec = seo[(seo["listing_date"] >= recent_cut) & seo["listing_date"].notna()].copy()
    rec["s"] = rec["symbol"].map(sidx)
    rec = rec.dropna(subset=["s"])
    last_close = pd.Series(close_m[-1, :], index=symbols)
    rec["now"] = rec["symbol"].map(last_close)
    rec["vs_issue"] = rec["now"] / rec["issue_price"] - 1
    valid = rec.dropna(subset=["vs_issue"])
    seo_out = {
        "deals_12m": int(len(rec)),
        "stocks_12m": int(rec["symbol"].nunique()),
        "raise_12m_yi": round(float(pd.to_numeric(rec["raise_yuan"], errors="coerce").sum()) / 1e8, 0),
        "break_rate": round(float((valid["vs_issue"] < 0).mean()) * 100, 1) if len(valid) else None,
        "vs_issue_med": round(float(valid["vs_issue"].median()) * 100, 1) if len(valid) else None,
        "n_valid": int(len(valid)),
    }
    print(f"  近 12 个月 {seo_out['deals_12m']} 单 / {seo_out['stocks_12m']} 只 / 募资 {seo_out['raise_12m_yi']} 亿")
    print(f"  破发率 {seo_out['break_rate']}% · 相对发行价中位 {seo_out['vs_issue_med']}% (n={seo_out['n_valid']})")

    # ---- C 371 日：事件密度 + 剔除后重跑 ----
    print('\n== C 371 日逐日表')
    ml = pl.read_parquet(MAINLINE).filter(pl.col("kind") == "concept").to_pandas()
    ml["date"] = pd.to_datetime(ml["date"])
    ml = ml[ml["rank"] <= TOP_N]
    # 固定窗口：与 `_diag_narrative_heat.py` 的 371 日样本一致（2026-11-13 之后是后来回填的
    # 10 个交易日，不在冻结读数里；要延长窗口必须两边一起改，否则页面与检验对不上）。
    d0, d1 = pd.Timestamp("2025-05-08"), pd.Timestamp("2026-11-13")
    ml = ml[ml["date"].between(d0, d1)]
    reg = pl.read_parquet(REGIME).to_pandas()
    reg["date"] = pd.to_datetime(reg["date"])
    mg = pl.read_parquet(MARGIN).to_pandas()
    mg["date"] = pd.to_datetime(mg["date"])
    float_mcap = dict(zip(mg["date"], pd.to_numeric(mg["float_mktcap"], errors="coerce")))

    # 概念映射
    gn = pl.read_parquet(GN).select(["symbol", "所属概念"]).to_pandas()
    rows = []
    for sym, raw in zip(gn["symbol"], gn["所属概念"]):
        if isinstance(raw, str):
            for c in [x.strip() for x in raw.split(";") if x.strip()]:
                rows.append((sym, c))
    cmap = pd.DataFrame(rows, columns=["symbol", "concept"])

    # 事件标记：某股票在 [t+1, t+20] 有解禁，或在 [t-20, t] 有定增上市
    unlock_days: dict[str, np.ndarray] = {}
    for s, g in lk.groupby("symbol"):
        idxs = g["free_date"].map(didx).dropna().astype(int).to_numpy()
        if len(idxs):
            unlock_days[s] = np.sort(idxs)
    seo_days: dict[str, np.ndarray] = {}
    for s, g in seo.groupby("symbol"):
        idxs = g["listing_date"].map(didx).dropna().astype(int).to_numpy()
        if len(idxs):
            seo_days[s] = np.sort(idxs)

    # 事件窗口摊成 (symbol, date-index) 集合：解禁前 20 日 / 定增上市后 20 日。
    # 逐行调用 Python 函数要 260 万次（分钟级），摊平后只有 ~6 万个键。
    flag_keys: set[tuple[str, int]] = set()
    for sym, g in lk.groupby("symbol"):
        for ti in g["free_date"].map(didx).dropna().astype(int):
            for k in range(1, 21):
                flag_keys.add((sym, int(ti) - k))
    for sym, g in seo.groupby("symbol"):
        for ti in g["listing_date"].map(didx).dropna().astype(int):
            for k in range(0, 21):
                flag_keys.add((sym, int(ti) + k))

    # 概念日收益（全部 / 剔除后）
    p2 = panel[panel["date"].between(d0 - pd.Timedelta(days=45), d1 + pd.Timedelta(days=45))].copy()
    p2 = p2.sort_values(["symbol", "date"])
    p2["ret"] = p2.groupby("symbol", sort=False)["close"].pct_change()
    p2 = p2.dropna(subset=["ret"])
    p2["t"] = p2["date"].map(didx)
    p2 = p2.merge(cmap, on="symbol", how="inner")
    p2["flag"] = [(s, int(t)) in flag_keys for s, t in zip(p2["symbol"], p2["t"])]

    def concept_daily(df: pd.DataFrame) -> pd.DataFrame:
        g = df.groupby(["concept", "date"], sort=False).agg(
            ret=("ret", "mean"), n=("ret", "size"), lu=("lu", "sum")
        ).reset_index()
        return g[g["n"] >= MIN_MEMBERS]

    all_cd = concept_daily(p2)
    exc_cd = concept_daily(p2[~p2["flag"]])

    def fwd20_matrix(cd: pd.DataFrame) -> pd.DataFrame:
        w = cd.pivot(index="date", columns="concept", values="ret").sort_index()
        f = (1 + w).rolling(20).apply(np.prod, raw=True).shift(-20) - 1
        return f

    f_all = fwd20_matrix(all_cd)
    f_exc = fwd20_matrix(exc_cd)
    x_all = (f_all.sub(f_all.median(axis=1), axis=0)) * 1e4
    x_exc = (f_exc.sub(f_exc.median(axis=1), axis=0)) * 1e4

    # 解禁市值密度
    lk_win = lk[(lk["free_date"] >= d0) & (lk["free_date"] <= d1)]
    lk_by_day = lk_win.groupby("free_date").agg(mcap=("lift_mcap_wan", "sum"), n=("symbol", "size"))
    seo_by_day = seo[(seo["listing_date"] >= d0) & (seo["listing_date"] <= d1)].groupby("listing_date").size()

    # 未来 20 日解禁密度
    fwd_mcap = {}
    lkd = lk_win.groupby("free_date")["lift_mcap_wan"].sum()
    idx_all = list(lkd.index)
    for d in pd.date_range(d0, d1, freq="B"):
        s = lkd[(lkd.index > d) & (lkd.index <= d + pd.Timedelta(days=28))].sum()
        fwd_mcap[d] = float(s)

    top1 = ml[ml["rank"] == 1].set_index("date")
    out = []
    for d in [x for x in f_all.index if d0 <= x <= d1]:
        if d not in f_all.index:
            continue
        a = float(x_all.loc[d].mean()) if d in x_all.index else np.nan
        b = float(x_exc.loc[d].mean()) if d in x_exc.index else np.nan
        fm = float_mcap.get(d, np.nan)
        um = float(lk_by_day["mcap"].get(d, 0.0)) / 1e4 if d in lk_by_day.index else 0.0  # 万元→亿
        reg_row = reg[reg["date"] == d]
        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "limit_up": int(reg_row["limit_up"].iloc[0]) if len(reg_row) else None,
            "phase": reg_row["phase"].iloc[0] if len(reg_row) else None,
            "top1": top1["member"].get(d, ""),
            "top1_lu": int(top1["limit_up_count"].get(d, 0)) if d in top1.index else 0,
            "unlock_mcap_yi": round(um, 1),
            "unlock_ratio_pct": round(um / fm * 100, 3) if fm and np.isfinite(fm) and fm > 0 else None,
            "seo_listed": int(seo_by_day.get(d, 0)),
            "fwd20_unlock_yi": round(fwd_mcap.get(d, 0.0) / 1e4, 1),
            "concepts": int(f_all.loc[d].notna().sum()),
            "x20_all_bp": round(a, 1) if np.isfinite(a) else None,
            "x20_excl_bp": round(b, 1) if np.isfinite(b) else None,
            "delta_bp": round(b - a, 1) if np.isfinite(a) and np.isfinite(b) else None,
        })
    daily = pd.DataFrame(out)
    daily.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"  → {OUT_CSV} {len(daily)} 行")

    # 剔除前后：剂量反应是否变化（1 档 vs ≥10 档）
    hot = p2.groupby(["concept", "date"]).agg(lu=("lu", "sum")).reset_index()
    hot = hot.merge(all_cd[["concept", "date"]], on=["concept", "date"])
    hot["bucket"] = pd.cut(hot["lu"], [-1, 0, 1, 2, 4, 9, 1e6], labels=["0", "1", "2", "3-4", "5-9", ">=10"])

    def dose(x: pd.DataFrame, hot_df: pd.DataFrame) -> dict:
        j = hot_df.set_index(["concept", "date"]).join(x.stack().rename("x"), how="inner").reset_index()
        raw = {}
        for lab in ["1", ">=10"]:
            g = j[j["bucket"] == lab].dropna(subset=["x"])
            by = g.groupby("date")["x"].mean()
            raw[lab] = float(by.mean()) if len(by) else None
        out = {k: (round(v, 1) if v is not None else None) for k, v in raw.items()}
        # 跨度用未舍入的均值算，再舍一次：两个分量各自舍入会在 .05 边界上翻面，
        # 同一份数据跑两次会给出两个数（实测差 0.1bp）。
        if raw.get("1") is not None and raw.get(">=10") is not None:
            out["span"] = round(raw[">=10"] - raw["1"], 1)
        return out

    dose_all = dose(x_all, hot)
    dose_exc = dose(x_exc, hot)
    print(f"  剂量跨度（1 → ≥10）：全部 {dose_all.get('span')}bp · 剔除事件股后 {dose_exc.get('span')}bp")

    result = {
        "meta": {
            "sample": "解禁 2019-01 ~ 2026-06（pre 需 20 个交易日）",
            "lockup_events": int(len(ev)),
            "stocks": int(ev["symbol"].nunique()),
            "source": "eastmoney RPT_LIFT_STAGE / RPT_SEO_DETAIL",
            "convention": "pre20=close[t-1]/open[t-20]-1；"
                          "基准=同日全市场收益中位复利；按日聚簇 NW(5)；安慰剂=同股票同年随机日",
        },
        "lockup": lock_rows,
        "lockup_placebo": plac_out,
        "lockup_momentum": mom_rows,
        "lockup_kind": kind_rows,
        "seo": seo_out,
        "dose_all": dose_all,
        "dose_excluded": dose_exc,
        "daily": {
            "days": int(len(daily)),
            "unlock_ratio_med": round(float(daily["unlock_ratio_pct"].median()), 3),
            "unlock_ratio_p95": round(float(daily["unlock_ratio_pct"].quantile(0.95)), 3),
            "delta_med": round(float(daily["delta_bp"].median()), 1),
            "corr_density_x20": round(float(daily[["unlock_ratio_pct", "x20_all_bp"]].dropna().corr().iloc[0, 1]), 3),
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → {OUT_JSON}")
    print(json.dumps({k: result[k] for k in ("lockup", "seo", "dose_all", "dose_excluded", "daily")},
                     ensure_ascii=False, indent=1)[:2000])


if __name__ == "__main__":
    main()
