"""ETF 成交结构的微观层：执行成本有多贵、同行业该选哪只、什么时段成交。

放弃绝对点差估计：Corwin-Schultz 与 Roll 在 ETF 上都会被成分股日内波动污染
（实测 CS 与规模不单调，中位 15.5bp 明显高于真实点差），所以只做同行业相对比较，
并把结论限制在「谁比谁贵」而不是「贵多少 bp」。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/_diag_etf_microstructure_result.json"

# 商品期货 ETF 的名单只有一份，来自核心分类模块（2026-11-28）。
# 本文件另有一份名称关键词表，但 159980「有色ETF大成」/159981「能源化工ETF建信」
# 的 name 里没有任何可剔的词 —— 不按代码剔就会以「有色/化工最大 ETF」的身份
# 污染「同行业最大 vs 最小」的振幅比。
from _diag_industry_etf_asym import EXCLUDE_SYMBOLS  # noqa: E402

EXCLUDE = ["债", "短融", "添益", "日利", "快线", "现金", "理财", "国开", "政金", "信用", "转债",
           "货币", "黄金", "白银", "原油", "豆粕", "商品", "港股", "香港", "恒生", "中概",
           "纳斯达克", "标普", "日经", "德国", "法国", "英国", "东南亚", "越南", "沙特", "亚太",
           "全球", "海外", "QDII", "中韩", "中日"]


def main() -> None:
    inst = pd.read_parquet(ROOT / "data/instruments_etf/instruments_etf.parquet")[["symbol", "name"]]
    inst = inst[~inst["name"].fillna("").map(lambda x: any(k in str(x) for k in EXCLUDE))]
    inst = inst[~inst["symbol"].isin(EXCLUDE_SYMBOLS)]

    # ---------- 5min：日内结构 ----------
    m = pd.read_parquet(ROOT / "data/etf_5min_sina/etf_5min.parquet")
    m["date"] = pd.to_datetime(m["date"])
    m["hm"] = m["datetime"].dt.strftime("%H:%M")
    m = m.merge(inst, on="symbol", how="inner")

    day = m.groupby(["symbol", "date"]).agg(
        hi=("high", "max"), lo=("low", "min"), close=("close", "last"), amt=("amount", "sum")
    ).reset_index()
    day["rng"] = (day.hi - day.lo) / day["close"] * 1e4      # 日内振幅 bp（摩擦代理，非点差）

    # 同一天、同一行业内部，按成交额排位比较振幅
    day = day.merge(inst, on="symbol")
    day["ind"] = day["name"].str.replace(r"ETF.*$", "", regex=True)
    day["rk"] = day.groupby(["ind", "date"])["amt"].rank(pct=True)
    day["bucket"] = pd.cut(day.rk, [0, 0.33, 0.66, 1.01], labels=["同行业小", "中", "同行业最大"])
    q = day.dropna(subset=["rng"]).groupby("bucket", observed=True)["rng"].median()
    ratio = float(q.get("同行业小", np.nan) / q.get("同行业最大", np.nan))

    # 时段结构
    tot = m.groupby("symbol")["amount"].sum()
    morning = m[m.hm <= "10:00"].groupby("symbol")["amount"].sum() / tot
    tail = m[m.hm >= "14:30"].groupby("symbol")["amount"].sum() / tot
    noon = m[(m.hm >= "11:30") & (m.hm <= "13:00")].groupby("symbol")["amount"].sum() / tot

    # ---------- 日线：散户可进的池子 ----------
    d = pd.read_parquet(ROOT / "data/backfill_20261117/etf_daily_full.parquet",
                        columns=["symbol", "date", "amount", "close", "high", "low"])
    d["date"] = pd.to_datetime(d["date"])
    d = d.merge(inst, on="symbol", how="inner")
    d = d[d.date >= pd.Timestamp("2025-06-01")]
    a = d.groupby("symbol")["amount"].mean()
    rng_d = ((d.high - d.low) / d["close"]).groupby(d.symbol).median()
    res_pool = {
        "etf_n": int(a.size),
        "amt_lt_10m_share": round(float((a < 1e7).mean()) * 100, 1),
        "amt_lt_50m_share": round(float((a < 5e7).mean()) * 100, 1),
        "amt_gt_200m_share": round(float((a > 2e8).mean()) * 100, 1),
        "amt_median_yi": round(float(a.median()) / 1e8, 2),
        "daily_range_median_bp": round(float(rng_d.median()) * 1e4, 1),
    }
    # 日内振幅 vs 日均成交额（分桶，只用相对）
    dd = pd.DataFrame({"amt": a, "rng": rng_d}).dropna()
    dd["b"] = pd.cut(np.log10(dd.amt), [5, 7, 8, 9, 12],
                     labels=["<100万", "100万-1亿", "1-10亿", ">10亿"])
    pool_rng = dd.groupby("b", observed=True)["rng"].median().mul(1e4).round(1)

    res = {
        "intraday_range_bp_by_rank": {str(k): round(float(v), 1) for k, v in q.items()},
        "small_vs_largest_ratio": round(ratio, 2),
        "session_share_pct": {
            "open_30min": round(float(morning.median()) * 100, 1),
            "midday_1130_1300": round(float(noon.median()) * 100, 1),
            "tail_30min": round(float(tail.median()) * 100, 1),
            "uniform_ref": round(30 / 240 * 100, 1),
        },
        "pool": res_pool,
        "daily_range_bp_by_amount": {str(k): float(v) for k, v in pool_rng.items()},
        "window": {"5min_start": str(m.date.min().date()), "5min_end": str(m.date.max().date())},
    }
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
