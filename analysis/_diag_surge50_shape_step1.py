"""10 日累计涨幅 >=50% 的事件盘点 + 事后路径（step1）。

用户问：抓住「10 日累计偏离 >50%（上涨）」的股票，最佳形状是什么。

第一步只做事实层面的事，不猜形状：
1. 事件有多少、分布在哪些年/板块/连板结构上 —— 先确认样本是否够研究；
2. 确认日 T（收盘已涨 50%）之后，T+1 开盘买入的各持有期收益与全市场等权超额 ——
   先确认「抓住」之后钱是否还在，否则形状研究是空转。

口径：
- close 为前复权序列（最新交易日 close==raw_close），收益一律同序列相除。
- 相邻交易日用日历序号差判断，不用 shift(N) 数行（停牌会造出假收益）。
- 事件去重：同一只票 20 个交易日内只保留首次（一波行情只算一个事件）。
- 生存偏差：本地日K 只有「今天仍在册」的 5581 只，退市股不在样本内，事后收益偏乐观。
- 基准：全市场等权指数，分别构造收盘口径 E_close 与开盘口径 E_open，用于对齐开盘入场。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
K = str(DATA / "kline_daily_enriched" / "*" / "part.parquet").replace("\\", "/")
OUT = DATA / "_diag_surge50_shape_step1.json"

con = duckdb.connect()
con.execute("PRAGMA threads=6")

con.execute(
    f"""
    CREATE TABLE k AS
    SELECT symbol, date, open, high, low, close, raw_close, volume, amount,
           turnover_rate, consecutive_limit_ups, consecutive_limit_downs,
           row_number() OVER (PARTITION BY symbol ORDER BY date) AS rn
    FROM read_parquet('{K}')
    WHERE close > 0 AND open > 0
    """
)

cal = con.execute("SELECT DISTINCT date FROM k ORDER BY date").df()
cal["seq"] = np.arange(len(cal))
print(f"交易日 {len(cal)}  {cal.date.iloc[0]} .. {cal.date.iloc[-1]}", flush=True)
con.execute("CREATE TABLE cal AS SELECT * FROM cal")

# 10 日 / 20 日累计涨幅：要求日历 seq 差正好 N
con.execute(
    """
    CREATE TABLE f AS
    SELECT a.symbol, a.date, c.seq, a.rn, a.open, a.high, a.low, a.close, a.raw_close,
           a.volume, a.amount, a.turnover_rate, a.consecutive_limit_ups,
           CASE WHEN cb.date IS NULL THEN NULL ELSE a.close / b.close - 1.0 END AS r10,
           CASE WHEN cd.date IS NULL THEN NULL ELSE a.close / d.close - 1.0 END AS r20
    FROM k a
    JOIN cal c ON a.date = c.date
    LEFT JOIN k b ON b.symbol = a.symbol AND b.rn = a.rn - 10
    LEFT JOIN cal cb ON cb.date = b.date AND c.seq - cb.seq = 10
    LEFT JOIN k d ON d.symbol = a.symbol AND d.rn = a.rn - 20
    LEFT JOIN cal cd ON cd.date = d.date AND c.seq - cd.seq = 20
    """
)

ev = con.execute(
    """
    SELECT symbol, date, seq, rn, open, high, low, close, raw_close, volume, amount,
           turnover_rate, consecutive_limit_ups, r10, r20
    FROM f WHERE r10 >= 0.50 ORDER BY symbol, date
    """
).df()
ev["date"] = pd.to_datetime(ev["date"])
print(f"原始命中 {len(ev)} 行  涉及 {ev.symbol.nunique()} 只", flush=True)


def board(sym: str) -> str:
    if sym.startswith(("688", "689")):
        return "科创板20%"
    if sym.startswith(("300", "301", "302")):
        return "创业板20%"
    if sym.startswith(("8", "4", "920")):
        return "北交所30%"
    return "主板10%"


# 去重：同票 20 个交易日内只保留首次
ev = ev.sort_values(["symbol", "seq"]).reset_index(drop=True)
keep, last_seq = [], {}
for i, row in enumerate(ev.itertuples(index=False)):
    s = row.symbol
    if s not in last_seq or row.seq - last_seq[s] >= 20:
        keep.append(i)
        last_seq[s] = row.seq
ev = ev.iloc[keep].reset_index(drop=True)
ev["board"] = ev["symbol"].map(board)
ev["year"] = ev["date"].dt.year
print(f"去重后独立事件 {len(ev)}  涉及 {ev.symbol.nunique()} 只", flush=True)

# ---- 全市场等权指数：open / close 两个口径 ----
bmk = con.execute(
    """
    WITH r AS (
      SELECT a.date, a.open / b.close AS g_open, a.close / a.open AS g_in
      FROM k a JOIN k b ON b.symbol = a.symbol AND b.rn = a.rn - 1
      JOIN cal c ON c.date = a.date JOIN cal cb ON cb.date = b.date
      WHERE c.seq - cb.seq = 1
    )
    SELECT date, avg(g_open) AS g_open, avg(g_in) AS g_in FROM r GROUP BY date ORDER BY date
    """
).df()
bmk["date"] = pd.to_datetime(bmk["date"])
bmk = bmk.sort_values("date").reset_index(drop=True)
bmk["e_close"] = (bmk["g_open"] * bmk["g_in"]).cumprod()
bmk["e_prev"] = bmk["e_close"].shift(1).fillna(1.0)
bmk["e_open"] = bmk["e_prev"] * bmk["g_open"]
E_CLOSE = dict(zip(bmk["date"], bmk["e_close"]))
E_OPEN = dict(zip(bmk["date"], bmk["e_open"]))

# ---- 事后路径 ----
path = con.execute(
    """
    SELECT e.symbol, e.date AS ev_date, e.seq AS ev_seq, e.r10, e.high AS ev_high, e.low AS ev_low,
           k.rn - e.rn AS fwd_rn, k.date AS fwd_date, k.open AS f_open, k.high AS f_high,
           k.close AS f_close, c.seq AS f_seq
    FROM (SELECT * FROM ev) e
    JOIN k ON k.symbol = e.symbol AND k.rn BETWEEN e.rn + 1 AND e.rn + 22
    JOIN cal c ON c.date = k.date
    """
).df()
path["fwd_date"] = pd.to_datetime(path["fwd_date"])
path["gap"] = path["f_seq"] - path["ev_seq"]

recs = []
for (sym, ev_date), g in path.groupby(["symbol", "ev_date"], sort=False):
    g = g.sort_values("fwd_rn")
    e1 = g[g.fwd_rn == 1]
    if e1.empty or int(e1["gap"].iloc[0]) != 1:
        continue
    entry = float(e1["f_open"].iloc[0])
    b_open_entry = E_OPEN.get(pd.Timestamp(e1["fwd_date"].iloc[0]))
    rec = dict(symbol=sym, ev_date=ev_date, entry=entry, b_open_entry=b_open_entry)
    for h in (1, 3, 5, 10, 20):
        sub = g[g.fwd_rn == h]
        if sub.empty:
            continue
        r0 = sub.iloc[0]
        if int(r0["gap"]) != h:
            continue
        exit_px = float(r0["f_close"])
        r = exit_px / entry - 1.0
        b1 = E_CLOSE.get(pd.Timestamp(r0["fwd_date"]))
        excess = (1 + r) / (b1 / b_open_entry) - 1.0 if b1 and b_open_entry else np.nan
        # 期间最大回撤（用收盘）
        seg = g[(g.fwd_rn >= 1) & (g.fwd_rn <= h)]
        mdd = float((seg["f_close"] / seg["f_close"].cummax() - 1).min())
        rec.update({f"ret{h}": r, f"exc{h}": excess, f"mdd{h}": mdd})
    recs.append(rec)

res = pd.DataFrame(recs)
res["board"] = res["symbol"].map(board)
print(f"可执行事件（T+1 有开盘） {len(res)}", flush=True)


def summarize(col: str) -> dict:
    s = res[col].dropna()
    if s.empty:
        return {}
    return {
        "n": int(len(s)),
        "mean": round(float(s.mean()) * 100, 3),
        "median": round(float(s.median()) * 100, 3),
        "win": round(float((s > 0).mean()) * 100, 2),
        "p10": round(float(s.quantile(0.10)) * 100, 2),
        "p90": round(float(s.quantile(0.90)) * 100, 2),
    }


summary = {
    "n_raw_hits": int(len(ev)),
    "n_events": int(len(ev)),
    "n_symbols": int(ev.symbol.nunique()),
    "by_board": {k: int(v) for k, v in ev.groupby("board").size().items()},
    "by_year": {int(k): int(v) for k, v in ev.groupby("year").size().items()},
    "board_share": {k: round(float(v) * 100, 2) for k, v in ev.groupby("board").size().div(len(ev)).items()},
    "r10_median": round(float(ev.r10.median()) * 100, 2),
    "turnover_median_T": round(float(ev.turnover_rate.median()), 3),
    "limitup_T_share": round(float((ev.consecutive_limit_ups >= 1).mean()) * 100, 2),
    "limitup_count_dist": {int(k): int(v) for k, v in ev.groupby("consecutive_limit_ups").size().items()},
    "post_all": {c: summarize(c) for c in ("ret1", "ret3", "ret5", "ret10", "ret20")},
    "post_excess": {c: summarize(c) for c in ("exc1", "exc3", "exc5", "exc10", "exc20")},
    "post_mdd": {c: summarize(c) for c in ("mdd5", "mdd10", "mdd20")},
}
for b, sub in res.groupby("board"):
    summary[f"post_board_{b}"] = {
        c: {
            "n": int(sub[c].notna().sum()),
            "mean": round(float(sub[c].mean()) * 100, 3),
            "win": round(float((sub[c].dropna() > 0).mean()) * 100, 2),
        }
        for c in ("ret5", "ret10", "ret20", "exc20")
        if sub[c].notna().any()
    }

Path(OUT).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
ev.to_parquet(DATA / "_diag_surge50_events.parquet", index=False)
res.to_parquet(DATA / "_diag_surge50_post.parquet", index=False)
print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
print("saved", OUT, flush=True)
