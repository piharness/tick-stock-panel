"""一次性读数：511010（5年期国债ETF）与现金腿的风险收益结构 + 204014 口径对照。

目的：回答「60 万全仓 511010 是否与『可忍受 10% 回撤、否则要稳定收益』一致」。
不改任何规则、不产出结论表，只把 local 数据的实际分布打出来。

运行：python analysis/_probe_511010_duration.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch_dividend as D  # noqa: E402
import etf_watch_leg2_rule as LR  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402


def bond_tr() -> pd.Series:
    f = D.fetch("511010")
    nav, ac = f["nav"], f["ac"]
    nav_s = nav / nav.iloc[0]
    dv = D.dividends_from_ac(nav_s, ac)
    assert dv is not None, "511010 恒等式不成立"
    return D.tr_from_nav_div(nav_s, dv)


def dd_episodes(eq: pd.Series, floor: float = -0.015) -> pd.DataFrame:
    dd = eq / eq.cummax() - 1
    rows = []
    in_dd = False
    start = trough = None
    for dt, v in dd.items():
        if not in_dd and v < 0:
            in_dd, start, trough, low = True, dt, dt, v
        elif in_dd:
            if v < low:
                low, trough = v, dt
            if v >= -1e-9:
                rows.append({"start": start.date(), "trough": trough.date(), "depth": low,
                             "recover_days": len(dd.loc[start:dt]) - 1})
                in_dd = False
    if in_dd:
        rows.append({"start": start.date(), "trough": trough.date(), "depth": low,
                     "recover_days": None})
    df = pd.DataFrame(rows)
    return df[df.depth <= floor] if len(df) else df


def main() -> None:
    tr = bond_tr()
    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    lvl = tr.reindex(cal).ffill().dropna()
    print(f"511010 净值序列 {lvl.index[0].date()} → {lvl.index[-1].date()}  N={len(lvl)}")

    for name, start in (("全史 2013-03起", "2013-03-05"), ("近3年", "2023-11-20"),
                        ("近1年", "2025-11-20")):
        w = lvl.loc[lvl.index >= pd.Timestamp(start)]
        r = w.pct_change().dropna()
        p = LR.perf(r, name)
        print(f"\n[{name}] 年化 {p['ann']:+.2%} | 年化波动 {p['vol']:.2%} | MDD {p['mdd']:.2%} "
              f"| 最差20日 {p['worst20']:.2%} | 总收益 {p['total']:+.2%}")
        yrs = pd.Series(1 + r).groupby(r.index.year).prod() - 1
        print("  逐年: " + "  ".join(f"{y}:{v:+.2%}" for y, v in yrs.items()))

    # 现金腿
    r_all = lvl.pct_change().dropna()
    cash = LR.cash_series(r_all.index)
    print(f"\n511990 现金腿（同期）年化 {LR.perf(cash, 'cash')['ann']:+.2%} "
          f"MDD {LR.perf(cash, 'cash')['mdd']:.3%}")

    # 回撤事件：>1.5%
    eq = lvl / lvl.iloc[0]
    ep = dd_episodes(eq, floor=-0.015)
    print(f"\n≥1.5% 的回撤事件 {len(ep)} 次：")
    print(ep.to_string(index=False))
    print(f"  平均深度 {ep.depth.mean():.2%} | 最深 {ep.depth.min():.2%} | "
          f"平均修复 {ep.recover_days.dropna().mean():.0f} 交易日 | 最长修复 {ep.recover_days.max():.0f}")

    # 滚动 1 年
    w252 = lvl.pct_change(252).dropna()
    ex = (lvl.pct_change(252) - (1 + cash).cumprod().pct_change(252).reindex(lvl.index)).dropna()
    for nm, s in (("滚动1年总收益", w252), ("滚动1年超额(vs现金)", ex)):
        q = s.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        print(f"\n{nm}  N={len(s)}  最差 {s.min():+.2%}  P(<0) {(s < 0).mean():.1%}")
        print("  分位: " + "  ".join(f"p{int(k*100)}:{v:+.2%}" for k, v in q.items()))
        print(f"  均值 {s.mean():+.2%}")

    # 日收益尾部
    r3 = lvl.pct_change().dropna().loc["2019-12-02":]
    print(f"\n日收益(2019-12起) N={len(r3)}  sd {r3.std()*100:.3f}%  "
          f"P(日≤-0.5%) {(r3<=-0.005).mean():.2%}  P(日≤-1%) {(r3<=-0.01).mean():.2%}  "
          f"最差日 {r3.min():.2%} ({r3.idxmin().date()})")

    # 需要的利率冲击
    print("\n久期算术（修正久期 D≈4.3）：")
    for shock in (25, 50, 100, 150, 200, 250):
        print(f"  收益率上行 {shock:>3}bp → 价格约 {-4.3*shock/100:+.2f}%")

    # 流动性
    k = pd.read_parquet(ROOT / "data/backfill_20261117/etf_daily_full.parquet",
                        columns=["symbol", "date", "amount", "close"])
    k = k[k.symbol.isin(["511010.SH", "511990.SH", "511260.SH"])]
    k = k.sort_values("date")
    g = k.groupby("symbol").tail(250).groupby("symbol")["amount"]
    print("\n近250日日均成交额（元，原始 amount 口径）:")
    print((g.mean() / 1e8).round(1).to_string())
    print(k.groupby("symbol").tail(1)[["symbol", "date", "close"]].to_string(index=False))
    close_now = float(k[k.symbol == "511010.SH"].iloc[-1]["close"])
    print(f"511010 最新价 {close_now:.3f} → 60万可买 {int(600000/close_now/100)*100} 份"
          f"（单张合约 {close_now*100:,.0f} 元，整手约束）")


if __name__ == "__main__":
    main()
