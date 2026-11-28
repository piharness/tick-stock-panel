"""质押贴线：2026-09+ 前向信号表（不依赖 f60）。

背景：历史事件表 `pledge_drift_events.parquet` 需要 60 个交易日的未来收益，
所以最新公告不会出现在里面。真前瞻必须单独从 `pledge_raw_v2.parquet` 生成信号。

规则与 `prereg-pledge-line-paper.md` 一致：
  ratio_c = zhesuan / (1 + drift_c) > 0.8
  drift_c = 公告日前最后一个交易日收盘 / 质押起始日收盘 − 1
  入场 = 公告后第一个交易日开盘；出场 = 入场 + 60 个交易日收盘（未到则标记 open）
只写信号与状态，不计算未发生的收益。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
LEV = ROOT / "data" / ".scratch" / "leverage"
PAPER = ROOT / "data" / "paper"
KLINE = ROOT / "data" / "kline_daily_enriched"
START = pd.Timestamp("2026-08-23")
FORWARD_START = pd.Timestamp("2026-11-30")


def main() -> None:
    raw = pd.read_parquet(LEV / "pledge_raw_v2.parquet")
    raw["code"] = raw["SECURITY_CODE"].astype(str).str.zfill(6)
    raw["notice"] = pd.to_datetime(raw["NOTICE_DATE"])
    raw["pstart"] = pd.to_datetime(raw["PF_START_DATE"])
    raw = raw.dropna(subset=["notice", "pstart", "OPENLINE", "CLOSE_FORWARD_ADJPRICE"])
    raw = raw[(raw["OPENLINE"] > 0) & (raw["CLOSE_FORWARD_ADJPRICE"] > 0)]
    raw["zhesuan"] = raw["OPENLINE"] / raw["CLOSE_FORWARD_ADJPRICE"]
    raw = raw[raw["zhesuan"].between(0.3, 0.9)]
    raw = raw[raw["notice"] >= START].copy()

    ins = pd.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")[
        ["code", "symbol", "name", "listing_date", "exchange"]
    ]
    ins["code"] = ins["code"].astype(str).str.zfill(6)
    raw = raw.merge(ins, on="code", how="left")
    raw = raw.dropna(subset=["symbol"])
    raw["is_st"] = raw["name"].fillna("").str.contains("ST", case=False)
    raw["is_bj"] = raw["symbol"].str.endswith(".BJ") | (raw["exchange"] == "BJ")
    raw = raw[(~raw["is_st"]) & (~raw["is_bj"])]

    # 去重到股票-日：同一公告日多个股东，保留累计质押比例最高的一条（信息量最大）
    raw["accum"] = pd.to_numeric(raw["ACCUM_PLEDGE_TSR"], errors="coerce")
    raw = raw.sort_values(["symbol", "notice", "accum"], ascending=[True, True, False])
    raw = raw.drop_duplicates(subset=["symbol", "notice"], keep="first")

    syms = raw["symbol"].unique().tolist()
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "open", "close", "amount"],
        filter=(ds.field("date") >= pd.Timestamp("2016-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    O = px.pivot_table(index="date", columns="symbol", values="open").sort_index()
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    A = px.pivot_table(index="date", columns="symbol", values="amount").sort_index()
    A20 = A.rolling(20).mean()
    di = C.index
    pos = {s: i for i, s in enumerate(C.columns)}

    rows = []
    for _, r in raw.iterrows():
        s = r["symbol"]
        j = pos.get(s)
        if j is None:
            continue
        notice, pstart = r["notice"], r["pstart"]
        pn_last = int(di.searchsorted(notice, side="right") - 1)  # 公告日前最后一个收盘
        pn_entry = int(di.searchsorted(notice, side="right"))     # 公告后第一个交易日
        ps = int(di.searchsorted(pstart, side="left"))
        if pn_last < 0 or ps < 0 or ps >= pn_last:
            continue
        close_sig = C.iat[pn_last, j]
        close_start = C.iat[ps, j]
        if not np.isfinite(close_sig) or not np.isfinite(close_start) or close_start <= 0:
            continue
        drift = close_sig / close_start - 1.0
        if not (-0.9 <= drift <= 3.0):
            continue
        ratio = float(r["zhesuan"]) / (1.0 + drift)
        if ratio <= 0.8:
            continue
        amount_row = min(pn_entry, len(di) - 1)
        amt20 = A20.iat[amount_row, j] if 0 <= amount_row < len(di) else np.nan
        if not np.isfinite(amt20) or amt20 < 1.5e7:
            continue
        listing = pd.to_datetime(r.get("listing_date"), errors="coerce")
        if pd.notna(listing) and (di[pn_entry] if pn_entry < len(di) else notice) - listing < pd.Timedelta(days=365):
            continue
        entry_date = di[pn_entry] if pn_entry < len(di) else pd.NaT
        status = "pending_entry" if pd.isna(entry_date) else "open"
        mark = np.nan
        days_held = np.nan
        if pd.notna(entry_date):
            entry_px = O.iat[pn_entry, j]
            last_px = C.iat[-1, j]
            if np.isfinite(entry_px) and entry_px > 0 and np.isfinite(last_px):
                mark = float(last_px / entry_px - 1.0)
            days_held = int((di[-1] - entry_date).days)
            if pn_entry + 60 <= len(di) - 1:
                status = "closed"
                exit_px = C.iat[pn_entry + 60, j]
                if np.isfinite(entry_px) and entry_px > 0 and np.isfinite(exit_px):
                    mark = float(exit_px / entry_px - 1.0)
        rows.append({
            "symbol": s, "name": r.get("name"), "notice": notice, "pstart": pstart,
            "entry_date": entry_date, "ratio_c": round(ratio, 4), "drift_c": round(drift, 4),
            "zhesuan": round(float(r["zhesuan"]), 4), "amount20": round(float(amt20), 0),
            "window": "gap" if notice < FORWARD_START else "forward",
            "status": status, "mark_return": round(mark, 4) if np.isfinite(mark) else None,
            "days_held": days_held if np.isfinite(days_held) else None,
        })

    df = pd.DataFrame(rows).sort_values(["notice", "symbol"])
    PAPER.mkdir(parents=True, exist_ok=True)

    # 同期 CSI500 基准（只用于缺口期盯市对照；不是最终 60 日超额）
    it = ds.dataset(ROOT / "data" / "kline_index_daily", format="parquet").to_table(
        columns=["symbol", "date", "close"],
        filter=(ds.field("symbol") == "000905.SH") & (ds.field("date") >= pd.Timestamp("2026-08-01").date()),
    )
    idx = it.to_pandas()
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.set_index("date")["close"].sort_index()
    last_idx = idx.index[-1]
    bench, excess = [], []
    for _, r in df.iterrows():
        e = r["entry_date"]
        if pd.isna(e) or pd.isna(r["mark_return"]) or e > last_idx:
            bench.append(np.nan); excess.append(np.nan); continue
        b = idx.loc[pd.Timestamp(e):last_idx]
        if len(b) < 2:
            bench.append(np.nan); excess.append(np.nan); continue
        br = float(b.iloc[-1] / b.iloc[0] - 1.0)
        bench.append(br); excess.append(float(r["mark_return"]) - br)
    df["bench_return"] = bench
    df["excess"] = excess
    df.to_parquet(PAPER / "pledge_line_signals.parquet", index=False)

    gap = df[(df["window"] == "gap") & df["mark_return"].notna()] if len(df) else df
    by_month = []
    if len(gap):
        for p, g in gap.groupby(gap["notice"].dt.to_period("M")):
            by_month.append({
                "month": str(p), "n": int(len(g)),
                "mean": round(float(g["mark_return"].mean()), 4),
                "median": round(float(g["mark_return"].median()), 4),
                "hit": round(float((g["mark_return"] > 0).mean()), 3),
            })

    out = {
        "generated_at": str(pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")),
        "spec": "prereg-pledge-line-paper-2026-11-27",
        "feed_last_event": str(raw["notice"].max().date()),
        "price_last_date": str(di[-1].date()),
        "signals": int(len(df)),
        "gap": int((df["window"] == "gap").sum()) if len(df) else 0,
        "forward": int((df["window"] == "forward").sum()) if len(df) else 0,
        "pending_entry": int((df["status"] == "pending_entry").sum()) if len(df) else 0,
        "open": int((df["status"] == "open").sum()) if len(df) else 0,
        "closed": int((df["status"] == "closed").sum()) if len(df) else 0,
        "gap_mark_n": int(len(gap)),
        "gap_mark_mean": round(float(gap["mark_return"].mean()), 4) if len(gap) else None,
        "gap_mark_median": round(float(gap["mark_return"].median()), 4) if len(gap) else None,
        "gap_mark_hit": round(float((gap["mark_return"] > 0).mean()), 3) if len(gap) else None,
        "gap_bench_mean": round(float(gap["bench_return"].mean()), 4) if len(gap) and gap["bench_return"].notna().any() else None,
        "gap_excess_mean": round(float(gap["excess"].mean()), 4) if len(gap) and gap["excess"].notna().any() else None,
        "gap_excess_median": round(float(gap["excess"].median()), 4) if len(gap) and gap["excess"].notna().any() else None,
        "gap_excess_hit": round(float((gap["excess"] > 0).mean()), 3) if len(gap) and gap["excess"].notna().any() else None,
        "gap_by_month": by_month,
        "gap_closed_mean_return": round(float(df[(df["window"] == "gap") & (df["status"] == "closed")]["mark_return"].mean()), 4) if ((df["window"] == "gap") & (df["status"] == "closed")).sum() else None,
        "gap_closed_n": int(((df["window"] == "gap") & (df["status"] == "closed")).sum()) if len(df) else 0,
        "rows": json.loads(df.assign(
            notice=df["notice"].dt.strftime("%Y-%m-%d"),
            pstart=df["pstart"].dt.strftime("%Y-%m-%d"),
            entry_date=df["entry_date"].dt.strftime("%Y-%m-%d"),
        ).to_json(orient="records", force_ascii=False)) if len(df) else [],
    }
    (PAPER / "pledge_line_forward.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ["generated_at", "feed_last_event", "price_last_date", "signals", "pending_entry", "open", "closed"]}, ensure_ascii=False, indent=2))
    if len(df):
        print(df.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
