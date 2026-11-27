"""合约簿覆盖率：和「中登全口径」比，公告明细到底盖住了多少。

不能只问「补完之后零还在不在」，还要问「补完之后离全口径还差多远」——
否则「存量零」会被误读成「所有质押都被测过了」。

三个口径，同一批日期：
  raw   逐行求和（v2 的做法）
  dedup 合约指纹去重后求和（去掉重复披露的同一份合约）
  snap  每份合约只取最近一期未解押余额（时点快照）
参照：中登 RPT_CSDC_LIST 个股质押比例（占主要股东/全部质押，口径更宽）。

产物：data/.scratch/leverage/book_coverage.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
from _diag_chip_constraint_book_v3 import adjust_factor, prep_contracts  # noqa: E402

OUT = ROOT / "data" / ".scratch" / "leverage"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"


def main():
    off = pd.read_parquet(OUT / "csdc_pledge_ratio.parquet")
    dates = pd.DatetimeIndex(sorted(off["date"].unique()))

    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "close"],
                   filter=ds.field("date") >= pd.Timestamp(dates[0]).date()).to_pandas()
    t["date"] = pd.to_datetime(t["date"]); t["symbol"] = t["symbol"].astype(str)
    cl = t.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    cal = pd.DatetimeIndex(cl.index)
    C = cl.to_numpy(np.float32)
    sym2idx = {s: i for i, s in enumerate(cl.columns)}
    print(f"      {len(cal)} 交易日 × {len(sym2idx)} 只；中登 {len(dates)} 个日期", flush=True)

    p = prep_contracts(cal)
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6); inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)].copy()
    p["idx"] = p["symbol"].map(sym2idx)
    p["fac"] = adjust_factor(pd.DataFrame({"symbol": p["symbol"].to_numpy(), "date": p["PF_START_DATE"].to_numpy()}))
    p["fac"] = np.clip(np.where(np.isfinite(p["fac"]) & (p["fac"] > 0), p["fac"], 1.0), 0.05, 5.0)
    p["line_adj"] = p["OPENLINE"] * p["fac"]
    p["ck"] = p["code"] + "|" + p["HOLDER_NAME"].astype(str) + "|" + p["PF_START_DATE"].astype(str)
    print(f"      合约 {len(p):,} / {p['symbol'].nunique():,} 只", flush=True)

    rows = []
    for dt in dates:
        m = int(cal.searchsorted(dt, side="right") - 1)
        if m < 0 or m >= len(cal):
            continue
        px = C[m]
        q0 = p[p["ni"] <= m]
        live = q0["open_state"] | (q0["UNFREEZE_DATE"].notna() & (q0["UNFREEZE_DATE"] > cal[m]))
        q = q0[live].copy(); q["px"] = px[q["idx"].to_numpy()]
        q = q[np.isfinite(q["px"]) & (q["px"] > 0)]
        q["ratio"] = q["line_adj"] / q["px"]
        raw = q.groupby("symbol").PF_TSR.sum()
        raw_near = q[(q["ratio"] >= 0.8) & (q["ratio"] <= 1.2)].groupby("symbol").PF_TSR.sum()
        ded = q.sort_values("NOTICE_DATE").drop_duplicates(["ck"], keep="first")
        ded_b = ded.groupby("symbol").PF_TSR.sum()
        ded_near = ded[(ded["ratio"] >= 0.8) & (ded["ratio"] <= 1.2)].groupby("symbol").PF_TSR.sum()
        s = q0.copy(); s["lastn"] = s.groupby("ck")["NOTICE_DATE"].transform("max")
        s = s[(s["NOTICE_DATE"] == s["lastn"]) & s["open_state"]].copy()
        s["px"] = px[s["idx"].to_numpy()]
        s = s[np.isfinite(s["px"]) & (s["px"] > 0)]
        s["ratio"] = s["line_adj"] / s["px"]
        snap = s.groupby("symbol").PF_TSR.sum()
        snap_near = s[(s["ratio"] >= 0.8) & (s["ratio"] <= 1.2)].groupby("symbol").PF_TSR.sum()
        o = off[off["date"] == dt].set_index("code")
        for tag, b in [("raw", raw), ("dedup", ded_b), ("snap", snap)]:
            sym = pd.Series(b.index)
            codes = inst.drop_duplicates("symbol").set_index("symbol")["code"]
            mm = pd.DataFrame({"symbol": sym}).assign(code=sym.map(codes))
            mm["book"] = b.to_numpy()
            mm = mm.merge(o[["ratio"]], left_on="code", right_index=True, how="inner")
            mm = mm[mm["ratio"] > 0]
            rows.append({"date": str(dt.date()), "spec": tag, "matched": int(len(mm)),
                         "official_n": int((o["ratio"] > 0).sum()),
                         "book_sum": round(float(mm["book"].sum()), 1),
                         "official_sum": round(float(mm["ratio"].sum()), 1),
                         "agg_ratio": round(float(mm["book"].sum() / max(mm["ratio"].sum(), 1e-9)), 3),
                         "med_ratio": round(float((mm["book"] / mm["ratio"]).median()), 3)})
        print(f"      {str(dt.date())} raw {rows[-3]['agg_ratio']} dedup {rows[-2]['agg_ratio']} "
              f"snap {rows[-1]['agg_ratio']} matched {rows[-1]['matched']}/{rows[-1]['official_n']}", flush=True)

    # near_share 的三口径（在合约簿内部为分母）
    out = {"rows": rows,
           "note": "agg_ratio = Σbook / Σ中登质押比例（都只在匹配到的股票上）；med_ratio = 逐股比值中位。"
                   "中登口径含全部质押人，公告明细只含重要股东，所以 1.0 不可达。"}
    (OUT / "book_coverage.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rows[-3:], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
