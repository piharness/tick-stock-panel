"""对照：同样「跌破一年高点 35%」，买个股 vs 买行业 ETF，左尾差多少。

这是「凭什么是我」的关键一环：如果 ETF 的左尾显著比个股薄，那散户的优势不是信号，
而是载体——ETF 用分散把单票黑天鹅砍掉，代价是右尾也被砍掉一部分。
声明：本地个股池只含现存股票（生存偏差），会低估个股左尾 ⇒ 对 ETF 优势的估计偏保守。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/_diag_etf_vs_stock_result.json"
SAMPLE = 3          # 每 3 只抽 1 只，控制内存
START = pd.Timestamp("2021-01-01")


def load_stocks() -> pd.DataFrame:
    files = sorted((ROOT / "data/kline_daily_enriched").glob("date=*"))
    files = [f for f in files if pd.Timestamp(f.name.split("=")[1]) >= START]
    parts = []
    for f in files:
        p = f / "part.parquet"
        if not p.exists():
            continue
        x = pd.read_parquet(p, columns=["symbol", "date", "close", "amount"])
        parts.append(x[x.symbol.str[-6:-3].astype(int) % SAMPLE == 0])
    d = pd.concat(parts, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["symbol", "date"])
    g = d.groupby("symbol", sort=False)
    d["hi250"] = g["close"].transform(lambda s: s.rolling(250, min_periods=200).max())
    d["dd52"] = d["close"] / d["hi250"] - 1
    d["fwd20"] = g["close"].shift(-21) / g["close"].shift(-1) - 1
    d["amt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    return d


def stat(v: np.ndarray, cost=0.0006) -> dict:
    v = v[~np.isnan(v)] - cost
    if len(v) < 100:
        return {}
    return {"n": int(len(v)), "ev_bp": round(float(v.mean()) * 1e4, 1),
            "med_bp": round(float(np.median(v)) * 1e4, 1),
            "p10_bp": round(float(np.percentile(v, 10)) * 1e4, 1),
            "p90_bp": round(float(np.percentile(v, 90)) * 1e4, 1),
            "win": round(float((v > 0).mean()) * 100, 1),
            # 上下不对称比：正尾均值 / |负尾均值|。ETF 与个股必须同一算法，
            # 否则「ETF 砍左尾」这句话会变成两套口径的差（2026-11-28 补）
            "updn": round(float(v[v > 0].mean() / abs(v[v < 0].mean())), 2),
            "skew": round(float(pd.Series(v).skew()), 2)}


def main() -> None:
    s = load_stocks()
    s = s[(s.amt20 > 3e7) & s.dd52.notna() & s.fwd20.notna()]
    out = {}
    for name, m in {"个股 低位−35~−50%": (s.dd52 <= -0.35) & (s.dd52 > -0.50),
                    "个股 深跌−50%↓": s.dd52 <= -0.50}.items():
        out[name] = stat(s.loc[m, "fwd20"].to_numpy())
    out["_meta"] = {"sample_rule": f"1/{SAMPLE} symbols", "start": str(START.date()),
                    "rows": int(len(s)), "symbols": int(s.symbol.nunique())}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
