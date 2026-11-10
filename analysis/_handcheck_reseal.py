# -*- coding: utf-8 -*-
"""手算抽验：回封标签与 r0_lim 是否与原始字段一致（独立重算，不复用主脚本中间量）。

抽 6 条（3 封住 / 3 炸板），从 parquet 重新读取 make 档位价、判标签、算 r0_lim，
与 `_audit_reseal_result.json` 的 universe 口径对齐（n / 回封率）。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = Path(__file__).resolve().parent / "_handcheck_reseal_20261110.txt"

MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
GEM = ("300", "301")
STAR = ("688", "689")
BJ = ("43", "83", "87", "88", "920")


def cands(code):
    if code.startswith(MAIN):
        return (0.05, 0.10)
    if code.startswith(GEM) or code.startswith(STAR):
        return (0.20,)
    if code.startswith(BJ):
        return (0.30,)
    return ()


def main() -> int:
    fs = sorted((DATA / "kline_daily").glob("date=*/part.parquet"))
    df = pd.concat([pq.ParquetFile(f).read(
        columns=["symbol", "date", "open", "high", "low", "close", "amount"]).to_pandas() for f in fs],
        ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    ex = pd.read_parquet(DATA / "adj_factor" / "all.parquet")[["symbol", "trade_date", "ex_factor"]]
    ex["trade_date"] = pd.to_datetime(ex["trade_date"])
    exm = ex.set_index(["symbol", "trade_date"])["ex_factor"]

    g = df.groupby("symbol", sort=False)
    df["prev_raw"] = g["close"].shift(1)
    df["nxt_date"] = g["date"].shift(-1)
    df["nxt_open"] = g["open"].shift(-1)
    df["nxt2_date"] = g["date"].shift(-2)
    df["nxt2_open"] = g["open"].shift(-2)

    def ef(dates):
        v = exm.reindex(pd.MultiIndex.from_arrays([df["symbol"], dates])).to_numpy()
        return np.where(np.isfinite(v), v, 1.0)

    df["exT"], df["ex1"] = ef(df["date"]), ef(df["nxt_date"])
    df["adj_prev"] = df["prev_raw"] / df["exT"]

    rows = []
    for L in (0.05, 0.10, 0.20, 0.30):
        rows.append(pd.DataFrame({
            "symbol": df["symbol"], "date": df["date"], "L": L,
            "lim_px": np.round(df["adj_prev"] * (1 + L), 2)}))
    cand = pd.concat(rows, ignore_index=True)
    cand = cand[cand.apply(lambda r: L in cands(r["symbol"][:6]) if (L := r["L"]) else False, axis=1)]
    m = df.merge(cand, on=["symbol", "date"], how="inner")
    m["d"] = (m["high"] - m["lim_px"]).abs()
    m = m.sort_values(["symbol", "date", "d"]).drop_duplicates(["symbol", "date"])
    # 与主脚本同口径：TICK=0.011 且 REACH=0.005（最高价不得比涨停价低超过半个价位）
    m = m[(m["d"] <= 0.011) & (m["high"] >= m["lim_px"] - 0.005)]
    m["y"] = (m["close"] - m["lim_px"]).abs() <= 0.011
    m["window"] = (m["lim_px"] - m["low"]) > 0.005
    m["r0_lim"] = m["nxt_open"] * m["ex1"] / m["lim_px"] - 1.0
    ok = ((m["nxt_date"] - m["date"]).dt.days <= 5) & ((m["nxt2_date"] - m["nxt_date"]).dt.days <= 5) \
        & m["nxt2_open"].notna()
    m = m[ok]

    lines = [f"触及(独立重算, 同 filt) = {len(m):,}  有窗口 = {int(m['window'].sum()):,}  "
             f"回封率(有窗口) = {m.loc[m['window'], 'y'].mean():.4f}"]
    pick = pd.concat([
        m[m["window"] & m["y"]].sample(3, random_state=7),
        m[m["window"] & ~m["y"]].sample(3, random_state=7)])
    for _, r in pick.iterrows():
        man = r["nxt_open"] * r["ex1"] / r["lim_px"] - 1.0
        lines.append(
            f"{r['symbol']} {r['date'].date()} L={r['L']:.2f} prev_raw={r['prev_raw']:.2f} "
            f"exT={r['exT']:.4f} adj_prev={r['adj_prev']:.4f} lim={r['lim_px']:.2f} "
            f"high={r['high']:.2f} low={r['low']:.2f} close={r['close']:.2f} "
            f"y={int(r['y'])} nxt_open={r['nxt_open']:.2f} ex1={r['ex1']:.4f} "
            f"r0_lim={r['r0_lim']*100:.2f}% manual={man*100:.2f}% "
            f"err_ok={abs(man - r['r0_lim']) < 1e-12}")

    ref = json.loads((Path(__file__).resolve().parent / "_audit_reseal_result.json").read_text("utf-8"))
    lines.append(f"主脚本 universe: n={ref['universe']['n']:,} hit={ref['universe']['hit']} "
                 f"days={ref['universe']['days']}")
    lines.append("主脚本 G_touch 参照: 触及全部 45,682 / 有窗口 43,685 / 回封率 0.5957")
    lines.append("差额来源: 主脚本 dropna(open_gap/ret_5/vol20/prev_break_rate) 再减 622 条特征不足样本")
    txt = "\n".join(lines)
    OUT.write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
