#!/usr/bin/env python3
"""_diag_index_rebalance.py — 指数调样「被动资金冲击」事件研究（第一版）

背景（docs/structural-edge/forced-counterparty-ledger.md 第 1 类被迫流）：
  指数定期调样（6/12 月半年度）→ 指数基金被契约锁死，生效日必须买入纳入股、卖出剔除股。
  ⇒ 纳入股：异常换手 + 当日涨（被迫买入推高）→ 之后应回落（价格压力反转）
  ⇒ 剔除股：异常换手 + 当日跌（被迫卖出砸坑）→ 之后应回补

数据现实（诚实边界）：本地**没有**历史指数成分名单（`instruments_index` 只有指数代码、
`index_daily_full` 是指数点位，均无成分股历史）。故第一版用「被动资金冲击签名」近似事件：

  事件 = 半年调样窗口内的股票日，其换手率 ≥ 3× 自身过去 20 日换手中位数（= 异常换手）。
  方向 = 当日涨跌（涨=疑似调入被动买入，跌=疑似调出被动卖出）。
  结果 = 事件后 20 日 forward 收益（日历相邻、raw_close 比值），超额 = 减当日全市场中位数。

要精确到「具体调入/调出名单」，需中证指数公司历史成分表（付费数据），本地无此数据。

产出：analysis/_diag_index_rebalance_result.json + _diag_index_rebalance_findings.md
      data/_diag_index_rebalance_events.parquet（事件级明细，供复现）

运行：backend/.venv/Scripts/python.exe analysis/_diag_index_rebalance.py
"""
from __future__ import annotations

import calendar
import json
from bisect import bisect_left
from datetime import date
from pathlib import Path

import polars as pl

KROOT = Path("data/kline_daily_enriched")
OUT_JSON = Path(__file__).with_name("_diag_index_rebalance_result.json")
OUT_MD = Path(__file__).with_name("_diag_index_rebalance_findings.md")
OUT_EV = Path("data/_diag_index_rebalance_events.parquet")

ATR_MIN = 3.0      # 主阈值：异常换手倍数
ATR_STRICT = 5.0   # 严格阈值（敏感性）
WIN_HALF = 10      # 调样锚点 ±10 交易日


def second_friday(year: int, month: int) -> date:
    fridays = [w[calendar.FRIDAY] for w in calendar.monthcalendar(year, month) if w[calendar.FRIDAY]]
    return date(year, month, fridays[1])


def build_window_mask(days: list[date], anchors: list[date]) -> set[date]:
    in_win: set[date] = set()
    for a in anchors:
        idx = bisect_left(days, a)  # 生效日 ≈ 第二个周五之后最近交易日
        for i in range(idx - WIN_HALF, idx + WIN_HALF + 1):
            if 0 <= i < len(days):
                in_win.add(days[i])
    return in_win


def summarize(g: pl.DataFrame, label: str) -> dict | None:
    n = g.height
    if n == 0:
        return None
    m = g["ret_fwd"].mean()
    med = g["ret_fwd"].median()
    s = g["ret_fwd"].std()
    t = m / (s / n**0.5) if s and s > 0 else None
    ex = g["excess_fwd"].mean()
    ex_med = g["excess_fwd"].median()
    ex_t = None
    if g["excess_fwd"].std() and g["excess_fwd"].std() > 0:
        ex_t = ex / (g["excess_fwd"].std() / n**0.5)
    return {
        "group": label, "n": n,
        "ret_fwd_mean_bp": round(m * 1e4, 1),
        "ret_fwd_median_bp": round(med * 1e4, 1),
        "t": round(t, 2) if t is not None else None,
        "win_pct": round(float((g["ret_fwd"] > 0).mean() * 100), 1),
        "excess_fwd_mean_bp": round(ex * 1e4, 1),
        "excess_fwd_median_bp": round(ex_med * 1e4, 1),
        "excess_t": round(ex_t, 2) if ex_t is not None else None,
    }


def main() -> None:
    print("读取 enriched ...")
    lf = pl.scan_parquet(str(KROOT / "**" / "*.parquet"), extra_columns="ignore")
    df = lf.select(["symbol", "date", "close", "turnover_rate"]).collect()
    # 月分区可能与日分区重叠 → 同 (symbol,date) 去重（实测当前 0 重复，保留防御）
    df = df.unique(subset=["symbol", "date"], keep="last")
    # 关键：unique() 不保序（并行实现会打乱），sort 必须放在所有窗口/shift 之前
    df = df.sort(["symbol", "date"])
    print(f"raw rows={df.height}")

    # 换手基线 = 过去 20 日换手中位数（不含当日）
    df = df.with_columns(pl.col("turnover_rate").shift(1).over("symbol").alias("turn_prev"))
    df = df.with_columns(
        pl.col("turn_prev").rolling_median(window_size=20, min_samples=10).over("symbol").alias("baseline_turn")
    )
    df = df.with_columns((pl.col("turnover_rate") / pl.col("baseline_turn")).alias("atr"))

    # 收益：ret_1d = 当日涨跌（T-1→T），ret_next = 事件次日反应（T→T+1），
    # ret_fwd = forward 20 日（T+1→T+20）。用 close（前复权，跨除权连续），不用 raw_close。
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret_1d"),
        (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1).alias("ret_next"),
        (pl.col("close").shift(-20).over("symbol") / pl.col("close").shift(-1).over("symbol") - 1).alias("ret_fwd"),
    )

    # 日历相邻守卫：前后行间隔 >4 天（跨停牌）则丢弃，避免单日 -90% 假收益
    df = df.with_columns(pl.col("date").cast(pl.Int32).alias("dint"))
    df = df.with_columns(
        (pl.col("dint") - pl.col("dint").shift(1).over("symbol")).alias("date_gap"),
        (pl.col("dint").shift(-1).over("symbol") - pl.col("dint")).alias("next_gap"),
    )

    # 调样窗口
    days = sorted(df["date"].unique().to_list())
    anchors = [second_friday(y, m) for y in range(2005, 2027) for m in (6, 12)]
    in_win = build_window_mask(days, anchors)
    df = df.with_columns(pl.col("date").is_in(list(in_win)).alias("in_win"))
    print(f"交易天数={len(days)}  调样窗口天数={len(in_win)}")

    # 市场基准：每日 forward/次日 的中位数（等权）
    mkt = df.group_by("date").agg(
        pl.col("ret_fwd").median().alias("mkt_fwd"),
        pl.col("ret_next").median().alias("mkt_next"),
    )

    events = df.filter(
        (pl.col("atr") >= ATR_MIN)
        & (pl.col("baseline_turn").is_not_null())
        & (pl.col("baseline_turn") > 0)
        & (pl.col("turnover_rate") > 0)
        & (pl.col("ret_fwd").is_not_null())
        & (pl.col("date_gap") <= 4)
        & (pl.col("next_gap") <= 4)
    ).join(mkt, on="date", how="left")
    events = events.with_columns(
        (pl.col("ret_fwd") - pl.col("mkt_fwd")).alias("excess_fwd"),
        pl.when(pl.col("ret_1d") > 0).then(pl.lit("up")).otherwise(pl.lit("down")).alias("direction"),
    )

    results = []
    for thr, name in [(ATR_MIN, "atr3"), (ATR_STRICT, "atr5")]:
        ev = events.filter(pl.col("atr") >= thr)
        for win, wname in [(True, "in"), (False, "out")]:
            for d, dname in [("up", "up"), ("down", "down")]:
                g = ev.filter((pl.col("in_win") == win) & (pl.col("direction") == d))
                r = summarize(g, f"{name}-{wname}-{dname}")
                if r:
                    results.append(r)

    # 事件级明细落盘（slim）
    events.select(
        ["symbol", "date", "turnover_rate", "baseline_turn", "atr", "ret_1d", "ret_next", "ret_fwd", "excess_fwd", "in_win", "direction"]
    ).write_parquet(OUT_EV)

    OUT_JSON.write_text(json.dumps({"threshold_main": ATR_MIN, "threshold_strict": ATR_STRICT,
                                    "window_half_days": WIN_HALF, "n_days": len(days),
                                    "n_events": events.height, "results": results},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 100)
    print("指数调样「被动资金冲击」事件研究（第一版）— forward 20 日收益（bp）")
    print("=" * 100)
    hdr = f"{'组':<14}{'n':>9}{'均值':>10}{'中位':>10}{'t':>7}{'胜率%':>8}{'超额均值':>10}{'超额t':>7}"
    print(hdr)
    for r in results:
        print(f"{r['group']:<14}{r['n']:>9}{r['ret_fwd_mean_bp']:>10}{r['ret_fwd_median_bp']:>10}"
              f"{str(r['t']):>7}{r['win_pct']:>8}{r['excess_fwd_mean_bp']:>10}{str(r['excess_t']):>7}")

    # 核心对比（用于判读）
    def get(gname):
        return next((r for r in results if r["group"] == gname), None)

    print("\n判读要点：")
    down_in = get("atr3-in-down"); down_out = get("atr3-out-down")
    up_in = get("atr3-in-up"); up_out = get("atr3-out-up")
    if down_in and down_out:
        print(f"  调出（down）窗口内 vs 窗口外 超额: {down_in['excess_fwd_mean_bp']} vs {down_out['excess_fwd_mean_bp']} bp"
              f"（正=剔除股被砸坑后回补，若窗口内更正则支持「被迫卖压→回补」）")
    if up_in and up_out:
        print(f"  调入（up）窗口内 vs 窗口外 超额: {up_in['excess_fwd_mean_bp']} vs {up_out['excess_fwd_mean_bp']} bp"
              f"（负=纳入股被抬价后回落，若窗口内更负则支持「被迫买压→回落」）")
    print(f"\n事件明细: {OUT_EV}  ({events.height} 行)")
    print(f"JSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
