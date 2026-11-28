"""H13c：贴线组跑赢的是「深跌股」，还是「所有深跌股」？——全市场同日对照

H13/H13b 只在质押事件内部比较，样本里没有「同样跌了很多、但没有质押」的股票。
本脚本把对照扩到全市场：每个质押公告日，按当日全市场横截面把股票分成
ret250（或 ret60）五分位，比较 near 事件的 f60 与「同一分位的全市场平均 f60」。

如果 near 的 f60 减去同分位均值 ≈ 0，那么「贴线」只是「深跌」的另一个标签；
如果仍显著为正，才说明质押合约本身有增量。

口径：
  f60 = close[t+60] / open[t+1] − 1（与事件表一致）
  分位 = 公告日当日全市场 ret250 / ret60 五分位（rank，含事件股）
  超额 = 事件 f60 − 同日同分位全市场均值；按月聚簇 NW t

输出：data/.scratch/leverage/h13c_market.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_audit_fix import load, nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
KLINE = ROOT / "data" / "kline_daily_enriched"


def main() -> None:
    ev = load()
    syms = set(ev["symbol"].unique().tolist())
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "open", "close", "turnover_rate"],
        filter=(ds.field("date") >= pd.Timestamp("2016-01-01").date()) & ds.field("symbol").isin(list(syms)),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    O = px.pivot_table(index="date", columns="symbol", values="open").sort_index()
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    T = px.pivot_table(index="date", columns="symbol", values="turnover_rate").sort_index()
    R1 = C.pct_change(fill_method=None)
    ret60 = C / C.shift(60) - 1
    ret250 = C / C.shift(250) - 1
    vol20 = R1.rolling(20).std()
    turn20 = T.rolling(20).mean()
    f60 = C.shift(-60) / O.shift(-1) - 1

    # 事件日 → 交易日索引（公告日或其后第一个交易日）
    di = C.index
    idx = di.searchsorted(pd.to_datetime(ev["notice"]).values)
    ok = (idx < len(di)) & (idx >= 0)
    ev = ev[ok].reset_index(drop=True)
    idx = idx[ok]

    # 预先算好每个交易日的分位阈值，避免逐事件 rank 全行
    q250 = ret250.rank(axis=1, pct=True)
    q60 = ret60.rank(axis=1, pct=True)
    qvol = vol20.rank(axis=1, pct=True)
    qturn = turn20.rank(axis=1, pct=True)

    rows = []
    for i, r in ev.iterrows():
        row = idx[i]
        dt = di[row]
        sym = r["symbol"]
        if sym not in C.columns:
            continue
        f = f60.iat[row, C.columns.get_indexer([sym])[0]]
        if not np.isfinite(f):
            continue
        rec = {"ym": r["ym"], "f60": f, "ratio_c": r["ratio_c"]}
        for name, Q in [("ret250", q250), ("ret60", q60), ("vol20", qvol), ("turn20", qturn)]:
            q = Q.iat[row, Q.columns.get_indexer([sym])[0]]
            if not np.isfinite(q):
                rec[name] = np.nan
                continue
            k = min(int(q * 5), 4)
            same = (Q.iloc[row] > k / 5) & (Q.iloc[row] <= (k + 1) / 5)
            vals = f60.iloc[row][same]
            rec[name] = float(vals.mean()) if vals.notna().any() else np.nan
        rows.append(rec)

    x = pd.DataFrame(rows).dropna(subset=["f60", "ret250", "ret60"])
    near = x[x["ratio_c"] > 0.8]
    far = x[x["ratio_c"] < 0.7]

    out: dict = {"events": int(len(x)), "near": int(len(near)), "months": int(x["ym"].nunique())}

    def agg(sub: pd.DataFrame, bench: str) -> dict:
        ex = (sub["f60"] - sub[bench]).dropna()
        g = ex.groupby(sub.loc[ex.index, "ym"]).mean()
        return {"bp": round(float(ex.mean() * 1e4), 1), "t": round(nw_t(g.to_numpy()), 2), "n": int(len(ex)), "months": int(g.size)}

    out["near"] = {b: agg(near, b) for b in ["ret250", "ret60", "vol20", "turn20"]}
    out["far"] = {b: agg(far, b) for b in ["ret250", "ret60", "vol20", "turn20"]}
    out["near_minus_far_same_bench"] = {
        b: round(out["near"][b]["bp"] - out["far"][b]["bp"], 1) for b in ["ret250", "ret60"]
    }

    # 分年：near 相对同 ret250 五分位的超额
    near = near.copy()
    near["ex"] = near["f60"] - near["ret250"]
    near["year"] = pd.to_datetime(near["ym"]).dt.year
    out["near_by_year"] = [
        {"year": int(y), "bp": round(float(g["ex"].mean() * 1e4), 1), "n": int(len(g))}
        for y, g in near.groupby("year") if len(g) >= 20
    ]

    (OUT / "h13c_market.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
