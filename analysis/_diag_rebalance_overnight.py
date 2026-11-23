#!/usr/bin/env python3
"""_diag_rebalance_overnight.py — 指数调样的隔夜/日内拆分

背景：`_diag_index_rebalance_real.py` 的 `ret_0 = T→T+1` 是收盘到收盘，
把「隔夜」与「日内」合并成一段。而换手板数据（docs/hotmoney-quant-game.md）
显示：涨停次日溢价 100% 在隔夜产生（2026 +1.45%），日内段 2018 后恒负（−0.18%）。
若被迫卖压同样在生效日收盘定价、修复在次日开盘，则 ret_0 会把两段相互抵消，
读成「无 edge」。

口径：
  overnight = open[T+1] / close[T]   − 1      （生效日收盘 → 次日开盘）
  intraday  = close[T+1] / open[T+1] − 1      （次日开盘 → 次日收盘）
  ret_0     = close[T+1] / close[T]  − 1      （两段之和，用于核对与旧口径一致）
  超额 = 个股 − 同日全市场（同口径）中位数

聚合：按事件日等权成一条组合收益。4 个生效日 ⇒ n=4，
**只能看方向与符号，不能给 t 值当结论**（n_eff<30）。

运行：backend/.venv/Scripts/python.exe analysis/_diag_rebalance_overnight.py
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

EV = Path("data/_csindex/rebalance_events.parquet")
ENR = Path("data/kline_daily_enriched")
OUT = Path(__file__).with_name("_diag_rebalance_overnight_result.json")


def main() -> None:
    ev = pl.read_parquet(EV).select(["index", "action", "symbol", "eff_date"]).unique()
    eff_dates = sorted(ev["eff_date"].unique().to_list())
    print(f"事件 {ev.height} 条 / 生效日 {len(eff_dates)} 个: {eff_dates}")

    parts = []
    for d in eff_dates:
        start = d.toordinal()
        for off in range(0, 6):
            day = __import__("datetime").date.fromordinal(start + off)
            p = ENR / f"date={day.isoformat()}" / "part.parquet"
            if not p.exists():
                p = ENR / f"date={day.isoformat()}"
                if p.is_dir():
                    fs = sorted(p.glob("*.parquet"))
                    if fs:
                        parts.append(pl.read_parquet(fs[0]).select(
                            ["symbol", "date", "open", "close"]))
                continue
            parts.append(pl.read_parquet(p).select(["symbol", "date", "open", "close"]))

    if not parts:
        raise SystemExit("未读到日线分区，检查路径")

    px = pl.concat(parts).unique(subset=["symbol", "date"], keep="first")
    px = px.sort(["symbol", "date"]).with_columns(
        nxt_open=pl.col("open").shift(-1).over("symbol"),
        nxt_close=pl.col("close").shift(-1).over("symbol"),
        nxt_date=pl.col("date").shift(-1).over("symbol"),
    )
    print(f"日线 {px.height} 行 / {px['symbol'].n_unique()} 只")

    rows = []
    for d in eff_dates:
        t = px.filter(pl.col("date") == d)
        if t.height == 0:
            print(f"  跳过 {d}：无该日分区")
            continue
        t = t.with_columns(
            overnight=pl.col("nxt_open") / pl.col("close") - 1,
            intraday=pl.col("nxt_close") / pl.col("nxt_open") - 1,
            ret0=pl.col("nxt_close") / pl.col("close") - 1,
        ).filter(pl.col("overnight").is_not_null() & pl.col("intraday").is_not_null())

        bench = {k: t[k].median() for k in ("overnight", "intraday", "ret0")}
        for act in ("调出", "调入"):
            g = t.join(ev.filter((pl.col("eff_date") == d) & (pl.col("action") == act))
                       .select(["symbol"]), on="symbol", how="inner")
            if g.height == 0:
                continue
            rows.append({
                "eff_date": str(d),
                "action": act,
                "n": g.height,
                "ex_overnight": g["overnight"].mean() - bench["overnight"],
                "ex_intraday": g["intraday"].mean() - bench["intraday"],
                "ex_ret0": g["ret0"].mean() - bench["ret0"],
                "bench_overnight": bench["overnight"],
                "bench_intraday": bench["intraday"],
            })

    r = pl.DataFrame(rows)
    print("\n=== 按事件日 ===")
    print(r)

    print("\n=== 按 action 跨事件等权（bp，n=事件数）===")
    agg = r.group_by("action").agg(
        n_events=pl.len(),
        ex_overnight=pl.col("ex_overnight").mean() * 1e4,
        ex_intraday=pl.col("ex_intraday").mean() * 1e4,
        ex_ret0=pl.col("ex_ret0").mean() * 1e4,
        overnight_pos=(pl.col("ex_overnight") > 0).sum(),
    ).sort("action")
    print(agg)

    OUT.write_text(json.dumps({
        "note": "4 个生效日 ⇒ n=4，仅方向/符号可读，不给 t 值",
        "by_event": r.to_dicts(),
        "by_action_bp": agg.to_dicts(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
