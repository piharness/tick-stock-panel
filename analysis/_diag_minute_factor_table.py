"""诊断: 分钟「反前复权比例」是否等于 adj_factor 表里「下一个除权日的 ex_factor」。

假设 H: 通达信分钟是前复权且基准为*下一个*除权日 ⇒ 对 T 日
    ratio_T = raw_daily_close(T) / adj_minute_close(T) = ex_factor(next event > T)
若 H 成立, 则校准是**静态查表**, 不需要快照价、不需要日线两两相除, 也不可能
被盘中 8 分钟行情污染 —— 这直接决定 P0 该怎么修。

同时验 "复权是纯乘法": 整日 OHLC 应被同一个 ratio 缩放 ⇒
    max(high)/日线high - 1 ≈ 0, min(low)/日线low - 1 ≈ 0
只在**完整日**(末根 15:00)上做, 否则末根不是收盘价, 比值里混进盘中漂移。

用法: backend/.venv/Scripts/python.exe analysis/_diag_minute_factor_table.py
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def q(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))]


def main() -> None:
    ev = pl.read_parquet(DATA / "adj_factor" / "all.parquet").select(
        ["symbol", "trade_date", "ex_factor", "dividend", "prev_close"]
    )
    print(f"事件表: {ev.height} 行 / {ev['symbol'].n_unique()} 只 / "
          f"{ev['trade_date'].min()} ~ {ev['trade_date'].max()}")

    rows: list[dict] = []
    for part in sorted((DATA / "kline_minute").glob("date=*")):
        day = part.name.split("=")[1]
        m = pl.read_parquet(part / "part.parquet")
        if m.is_empty():
            continue
        dp = DATA / "kline_daily" / f"date={day}" / "part.parquet"
        if not dp.exists():
            continue
        d = pl.read_parquet(dp, columns=["symbol", "close", "high", "low"]).rename(
            {"close": "raw_close", "high": "raw_high", "low": "raw_low"}
        )
        agg = m.group_by("symbol").agg(
            pl.col("datetime").max().alias("t_last"),
            pl.len().alias("n"),
            pl.col("close").last().alias("adj_close"),
            pl.col("high").max().alias("adj_high"),
            pl.col("low").min().alias("adj_low"),
        )
        # 完整日 = 末根 15:00 且 240 根; 否则末根不是收盘价
        agg = agg.filter((pl.col("t_last").dt.hour() == 15) & (pl.col("t_last").dt.minute() == 0))
        j = agg.join(d, on="symbol", how="inner")
        for r in j.iter_rows(named=True):
            rows.append({"day": day, **r})

    print(f"完整日样本: {len(rows)} 个 symbol-day")
    if not rows:
        return

    # 每个 symbol 的事件日期升序, 便于取「下一个除权日」
    nxt: dict[str, list[tuple[object, float]]] = {}
    for r in ev.sort("trade_date").iter_rows(named=True):
        nxt.setdefault(r["symbol"], []).append((r["trade_date"], float(r["ex_factor"])))

    errs: list[float] = []
    env_h: list[float] = []
    env_l: list[float] = []
    ex: list[tuple[str, str, float, float]] = []
    for r in rows:
        day = r["day"]
        d_iso = __import__("datetime").date.fromisoformat(day)
        tab = 1.0
        for td, f in nxt.get(r["symbol"], []):
            if td > d_iso:
                tab = f
                break
        meas = r["raw_close"] / r["adj_close"]
        errs.append(meas / tab - 1.0)
        env_h.append(r["adj_high"] / r["raw_high"] - 1.0)
        env_l.append(r["adj_low"] / r["raw_low"] - 1.0)
        if abs(meas / tab - 1.0) > 1e-6 and len(ex) < 8:
            ex.append((day, r["symbol"], meas, tab))

    ae = [abs(x) for x in errs]
    print(f"\nH: ratio_T == ex_factor(next event)")
    print(f"  |err| 中位={q(ae,0.5):.3e}  p95={q(ae,0.95):.3e}  max={max(ae):.3e}")
    for thr in (1e-9, 1e-6, 1e-4, 1e-3):
        k = sum(1 for a in ae if a > thr)
        print(f"  |err|>{thr:.0e}: {k}/{len(ae)} ({k/len(ae)*100:.1f}%)")
    if ex:
        print("  超 1e-6 的样例 (day, symbol, 实测ratio, 表ex_factor):")
        for t in ex:
            print("   ", t)

    ah = [abs(x) for x in env_h]
    al = [abs(x) for x in env_l]
    print(f"\n纯乘法 (整日同一 ratio):")
    print(f"  |max(high)/日线high-1| 中位={q(ah,0.5):.3e} p95={q(ah,0.95):.3e} max={max(ah):.3e}")
    print(f"  |min(low)/日线low-1|   中位={q(al,0.5):.3e} p95={q(al,0.95):.3e} max={max(al):.3e}")
    bad = [r for r, h, l in zip(rows, ah, al) if h > 1e-4 or l > 1e-4]
    print(f"  越界 (high 或 low 偏 >1e-4) 的 symbol-day: {len(bad)}/{len(rows)}")


if __name__ == "__main__":
    main()
