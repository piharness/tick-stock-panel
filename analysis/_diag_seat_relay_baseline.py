"""席位接力：市场基准校准 + 控制变量检验。

## 为什么必须做

上一版输出的是**绝对**前向收益，全部为负。但若样本期（2025-11~2026-11）市场本身
在下跌，则「负收益」不含任何关于接力信号的信息。必须先减掉同口径市场收益。

基准口径必须与个股完全一致：同样是 **T+1 开盘 → T+1+n 收盘**，
对全市场当日所有可交易股票取等权均值（同一交易日、同一持有窗口）。

## 第二个检验

若 relay 有效，应在控制「当日涨幅 / 换手率 / streak」后仍有增量。
用同日横截面回归 + 分桶双重排序。
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"
KLINE = ROOT / "data" / "kline_daily_enriched"
HORIZONS = (1, 3, 5, 10, 20)


def load_kline() -> pd.DataFrame:
    files = sorted(glob.glob(str(KLINE / "date=*/part.parquet")))
    files = [f for f in files if "date=2025-1" in f or "date=2026" in f]
    k = pd.concat(
        [pd.read_parquet(f, columns=["symbol", "date", "open", "close", "turnover_rate", "amount"])
         for f in files], ignore_index=True)
    k["date"] = pd.to_datetime(k.date.astype(str))
    return k.sort_values(["symbol", "date"]).reset_index(drop=True)


def build_market(k: pd.DataFrame) -> pd.DataFrame:
    """每个交易日、每个 horizon 的全市场等权前向收益（T+1 开盘 → T+1+n 收盘）。"""
    k = k.copy()
    k["i"] = k.groupby("symbol").cumcount()
    # 只保留有足够后续的股票日
    rows = []
    kmap = {s: g.reset_index(drop=True) for s, g in k.groupby("symbol")}
    for s, g in kmap.items():
        o = g.open.values
        c = g.close.values
        for j in range(len(g) - 1):
            rec = {"date": g.date.iloc[j], "symbol": s}
            for n in HORIZONS:
                jj = j + 1 + n - 1
                rec[f"m{n}"] = (c[jj] / o[j + 1] - 1) if jj < len(g) else np.nan
            rows.append(rec)
    mk = pd.DataFrame(rows)
    return mk.groupby("date")[[f"m{n}" for n in HORIZONS]].mean().reset_index()


def main() -> None:
    fwd = pd.read_parquet(OUT / "seat_relay_fwd.parquet")
    fwd["date"] = pd.to_datetime(fwd.date)
    k = load_kline()
    mk = build_market(k)
    mk.to_parquet(OUT / "market_fwd_equalweight.parquet", index=False)

    d = fwd.merge(mk, on="date", how="left")
    d = d[d.buyable].copy()
    for n in HORIZONS:
        d[f"ex{n}"] = (d[f"f{n}"] - d[f"m{n}"]) * 1e4

    print("=" * 88)
    print("0. 市场基准（全市场等权，T+1 开盘→T+1+n 收盘，bp）")
    print("=" * 88)
    print(mk[[f"m{n}" for n in HORIZONS]].describe().loc[["mean", "std"]].mul(1e4).round(0).to_string())

    print("\n" + "=" * 88)
    print("A. 超额收益（bp，已减同日等权基准）  relay5 分桶  streak>=3")
    print("=" * 88)
    sub = d[d.streak >= 3].assign(bucket=pd.cut(d[d.streak >= 3].relay5,
                                                [-.01, .2, .4, .6, .8, 1.01]))
    agg = sub.groupby("bucket", observed=True).agg(
        n=("ex1", "size"),
        **{f"ex{n}": (f"ex{n}", "mean") for n in HORIZONS},
        t_ret=("t_ret", "mean"), t_turn=("t_turn", "mean"))
    print(agg.round(1).to_string())

    print("\n" + "=" * 88)
    print("B. 全体 vs 二分（超额 bp）")
    print("=" * 88)
    groups = [("全体 streak>=3", sub),
              ("全换人 relay>=0.8", sub[sub.relay5 >= 0.8]),
              ("老面孔 relay<=0.4", sub[sub.relay5 <= 0.4]),
              ("全换人 & streak3-5", sub[(sub.relay5 >= 0.8) & (sub.streak <= 5)])]
    for lab, g in groups:
        line = f"{lab:<24} n={len(g):>5}"
        for n in HORIZONS:
            line += f"  ex{n}={g[f'ex{n}'].mean():>7.0f}"
        print(line)

    print("\n" + "=" * 88)
    print("C. t 统计量（按事件，relay>=0.8 vs 全体，ex5 / ex20）")
    print("=" * 88)
    for n in (1, 5, 20):
        a = sub[sub.relay5 >= 0.8][f"ex{n}"].dropna()
        b = sub[f"ex{n}"].dropna()
        se = a.std() / np.sqrt(len(a))
        print(f"  relay>=0.8 n={len(a):>4}  ex{n}={a.mean():>7.0f}  t={a.mean() / se:>5.2f}   "
              f"全体 ex{n}={b.mean():>7.0f}  差={a.mean() - b.mean():>7.0f}")

    print("\n" + "=" * 88)
    print("D. 双重排序：先按 streak 分层，再看 relay 高低的 ex5 差")
    print("=" * 88)
    sub = sub.assign(sb=pd.cut(sub.streak, [2, 3, 5, 8, 100], labels=["3", "4-5", "6-8", "9+"]))
    for sb, g in sub.groupby("sb", observed=True):
        hi = g[g.relay5 >= 0.7]
        lo = g[g.relay5 <= 0.3]
        if len(hi) > 20 and len(lo) > 20:
            print(f"  streak={sb:<5} hi(n={len(hi):>4}) ex5={hi.ex5.mean():>6.0f}  "
                  f"lo(n={len(lo):>4}) ex5={lo.ex5.mean():>6.0f}  差={hi.ex5.mean() - lo.ex5.mean():>6.0f}")

    print("\n" + "=" * 88)
    print("E. 同日横截面回归：ex1 ~ relay5 + t_ret + t_turn + streak（按日 FM 均值）")
    print("=" * 88)
    sub = d[d.streak >= 3].dropna(subset=["ex1", "relay5", "t_ret", "t_turn"])
    coefs = []
    for dt, g in sub.groupby("date"):
        if len(g) < 15:
            continue
        X = np.column_stack([np.ones(len(g)), g.relay5, g.t_ret, g.t_turn / 100,
                             np.log1p(g.streak)])
        for n in (1, 5):
            y = g[f"ex{n}"].values
            if np.isnan(y).any():
                continue
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            coefs.append({"date": dt, "h": n, "relay": beta[1], "t_ret": beta[2],
                          "t_turn": beta[3], "streak": beta[4]})
    cf = pd.DataFrame(coefs)
    for n in (1, 5):
        c = cf[cf.h == n]
        print(f"  h={n}: N日={len(c)}")
        for col in ["relay", "t_ret", "t_turn", "streak"]:
            m = c[col].mean()
            t = m / (c[col].std() / np.sqrt(len(c))) if len(c) > 1 else np.nan
            print(f"      {col:<8} {m:>8.1f}  t={t:>5.2f}")


if __name__ == "__main__":
    main()
