"""载体错配诊断：商品期货 ETF 混进行业池（2026-11-28）。

事故：2026-11-27「有色」段首的载体被选成 159980 有色ETF大成（成交额 6.14 亿 >
512400 的 5.53 亿），但 159980 跟踪的是上期所有色金属**期货**价格指数：
行业净值说「有色跌 41%」，它自己只跌 16%，对四条判据连第一条都不过。

本脚本回答三件事，全部用扫描端同一实现（`_diag_industry_etf_asym`）：
1. 有色/化工的成分与权重（谁把权重拿走了）
2. 159980 与 512400 是不是同一个东西（日收益相关性）
3. 剔除后行业回撤变成多少（信号读数是否还成立）

用法：python analysis/_diag_carrier_mismatch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch as W  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

BAN = {"159980.SZ", "159981.SZ", "159985.SZ"}


def industry_dd(d: pd.DataFrame, ind: str) -> pd.Series:
    """行业净值回撤（成交额加权收益 → cumprod → 距 250 日高点），与扫描同口径。"""
    panel = core.aggregate(d[d["industry"] == ind], require_fwd=False)
    if panel.empty:
        return pd.Series(dtype=float)
    nav = core.industry_nav(panel)[ind]
    return nav / nav.rolling(250, min_periods=200).max() - 1


def main() -> None:
    etf, _ = W.fresh_etf()
    # 用「未剔除」的 universe 载入一次，再在内存里切两版，避免重复 dejump
    saved = core.EXCLUDE_SYMBOLS
    core.EXCLUDE_SYMBOLS = set()
    d_all, _ = core.load(etf_raw=etf)
    core.EXCLUDE_SYMBOLS = saved
    d_new = d_all[~d_all["symbol"].isin(BAN)]

    asof = d_all["date"].max()
    print(f"as of {asof.date()}\n")

    for ind in ("有色", "化工"):
        x = d_all[(d_all["industry"] == ind) & (d_all["date"] == asof)]
        x = x.sort_values("amount", ascending=False)
        w = x["amount"] / x["amount"].sum()
        print(f"=== {ind} 成分（按当日成交额）===")
        for i, (_, r) in enumerate(x.iterrows()):
            flag = "  ← 商品期货" if r["symbol"] in BAN else ""
            print(f"  {r['symbol']:10s} {str(r['name'])[:16]:16s} "
                  f"权重 {w.iloc[i]:5.1%}  当日额 {r['amount']/1e8:5.2f}亿  "
                  f"20日均 {r['amt20']/1e8:5.2f}亿  dd52 {r['dd52']*100:6.1f}%{flag}")
        print()

    p = (d_all[d_all["symbol"].isin(["159980.SZ", "512400.SH"])]
         .pivot(index="date", columns="symbol", values="ret").dropna().tail(250))
    print(f"159980 vs 512400 日收益相关（近 {len(p)} 日）：{p['159980.SZ'].corr(p['512400.SH']):.3f}")
    print()

    print("=== 行业回撤：含商品期货 vs 剔除 ===")
    for ind in ("有色", "化工"):
        a, b = industry_dd(d_all, ind), industry_dd(d_new, ind)
        if a.empty or b.empty:
            print(f"  {ind}: 无数据")
            continue
        print(f"  {ind:4s}  含 {a.iloc[-1]*100:6.1f}%   剔除 {b.iloc[-1]*100:6.1f}%   "
              f"差 {(a.iloc[-1]-b.iloc[-1])*100:+.1f}pp")

    print("\n=== 四条判据（若直接对商品期货 ETF 套用）===")
    for s in ("159980.SZ", "512400.SH"):
        r = d_all[(d_all["symbol"] == s) & (d_all["date"] == asof)].iloc[0]
        in_band = -0.50 < r["dd52"] <= -0.35
        print(f"  {s} {str(r['name'])[:12]:12s} dd52 {r['dd52']*100:6.1f}%  "
              f"位置闸 {'过' if in_band else '不过'}")


if __name__ == "__main__":
    main()
