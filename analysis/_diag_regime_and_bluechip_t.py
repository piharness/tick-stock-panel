#!/usr/bin/env python3
"""_diag_regime_and_bluechip_t.py — 两个问题，一套可复现判据

Q1 现在是横盘、下行还是上行？
Q2 核心公民里的白马/蓝筹，逢低买入做 T 有正期望吗？

## Q1 设计
不猜方向，只报三条同步读数（均线结构 / 分段收益 / 距高点回撤），并明确规则。
判据本身是**同步指标**，不做闸门（既有结论：`br20<50%` 当开关阈值是尖峰，见 MEMORY）。
会红：若三条读数彼此矛盾（如 close>MA60 但 ret60<-5%），必须报「矛盾」而不是硬塞进三分。

## Q2 设计（关键是把「做T」拆成可执行与不可执行两层）

A 股 T+1：当日买入不能当日卖出。「做 T」实际有三种形态，期望完全不同：
  T-a 底仓日内回转：先卖昨仓 → 当日低点买回。需要日内择时 + 已有底仓。
  T-b T+1 波段：当日逢跌买 → 次日/数日后卖。日线可测，无需底仓。
  T-c 无底仓日内：不可执行，直接排除。

本脚本测 T-a 的**天花板**与 T-b 的**实际期望**：
  天花板 = (当日 close / 当日 low - 1)，即「每次都买在最低点、收盘卖出」的收益上限。
          若天花板扣掉双边成本后不显著为正，任何日内择时都不可能有正期望 —— 这是必要条件。
  实际   = 次日 open 入场（可执行、不偷价），持有 1/3/5/10/20 日，扣双边 20bp。

池子（沿用 `backend/app/services/citizenship.py` 的既有定义，不新造）：
  core      = 当日成交额分位 ≥ 0.90（流动性中心；**不要求高换手**，因为大盘蓝筹换手天然低）
  blue      = core 且 流通市值分位 ≥ 0.85（= PRICING_RULES 的 weight，被动/长钱定价）
  对照组 A  = 市值前 15% 但成交额不在前 10%（「没人看的蓝筹」）
  对照组 B  = 高换手 + 市值 < 85%（momentum 定价，交易盘）

事件：当日个股收益 ≤ −2%，并按跌幅分档；同时给「相对跌幅」（个股 − 当日池均值）分档，
      把「全池一起跌的 β」与「个股单独超跌」分开。

输出：analysis/_diag_regime_and_bluechip_t_result.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kline_daily_enriched"
IDX = ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet"
OUT = Path(__file__).with_name("_diag_regime_and_bluechip_t_result.json")

START = "2016-01-01"
COST = 0.0020  # 双边 20bp（印花税 10bp + 佣金 5bp×2）


# ---------------- Q1 市场状态 ----------------
def q1_regime() -> dict:
    idx = pl.read_parquet(IDX, columns=["symbol", "date", "close"]).filter(
        pl.col("date") >= pl.lit(START).str.to_date()
    )
    names = {
        "000001.SH": "上证指数",
        "000300.SH": "沪深300",
        "399006.SZ": "创业板指",
        "000905.SH": "中证500",
        "000852.SH": "中证1000",
        "000016.SH": "上证50",
    }
    out = {}
    for code, nm in names.items():
        s = idx.filter(pl.col("symbol") == code).sort("date")
        if s.height < 260:
            continue
        c = s["close"].to_list()
        d = s["date"].to_list()
        n = len(c)

        def ret(k: int) -> float:
            return (c[-1] / c[-1 - k] - 1) * 100

        def ma(k: int) -> float:
            return sum(c[-k:]) / k

        hi250 = max(c[-250:])
        out[nm] = {
            "code": code,
            "last_date": str(d[-1]),
            "last": round(c[-1], 2),
            "ret5": round(ret(5), 2),
            "ret20": round(ret(20), 2),
            "ret60": round(ret(60), 2),
            "ret120": round(ret(120), 2),
            "vs_ma20": round((c[-1] / ma(20) - 1) * 100, 2),
            "vs_ma60": round((c[-1] / ma(60) - 1) * 100, 2),
            "vs_ma120": round((c[-1] / ma(120) - 1) * 100, 2),
            "vs_ma250": round((c[-1] / ma(250) - 1) * 100, 2),
            "dd_from_250d_high": round((c[-1] / hi250 - 1) * 100, 2),
            "ma20_above_ma60": ma(20) > ma(60),
            "ma60_above_ma120": ma(60) > ma(120),
        }
        # 三分规则（同步、可复现）
        r = out[nm]
        if r["vs_ma60"] < 0 and not r["ma20_above_ma60"] and r["ret60"] < -5:
            r["label"] = "下行"
        elif r["vs_ma60"] > 0 and r["ma20_above_ma60"] and r["ret60"] > 5:
            r["label"] = "上行"
        else:
            r["label"] = "横盘/纠缠"
    return out


# ---------------- Q2 蓝筹逢跌买入 ----------------
def q2_bluechip() -> dict:
    files = []
    for e in os.scandir(DATA):
        if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= START:
            files.append(str(Path(e.path) / "part.parquet"))
    files.sort()
    df = pl.read_parquet(
        files, columns=["symbol", "date", "open", "high", "low", "close", "amount", "turnover_rate"]
    ).filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
    )
    df = df.sort(["symbol", "date"])
    df = df.with_columns(
        [
            (pl.col("amount") / (pl.col("turnover_rate") / 100)).alias("fmcap"),
            # 当日收益（用前一日 close）
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"),
            # 日历相邻检查
            (pl.col("date") - pl.col("date").shift(1).over("symbol")).dt.total_days().alias("gap1"),
        ]
    )
    df = df.with_columns(
        pl.when(pl.col("gap1").is_between(1, 10)).then(pl.col("ret1")).otherwise(None).alias("ret1")
    )
    # 分位：成交额 / 流通市值（当日横截面）
    df = df.with_columns(
        [
            (pl.col("amount").rank("ordinal").over("date") - 1)
            .truediv(pl.len().over("date") - 1).alias("pa"),
            (pl.col("fmcap").rank("ordinal").over("date") - 1)
            .truediv(pl.len().over("date") - 1).alias("pm"),
            (pl.col("turnover_rate").rank("ordinal").over("date") - 1)
            .truediv(pl.len().over("date") - 1).alias("pt"),
        ]
    )
    df = df.with_columns(
        [
            (pl.col("pa") >= 0.90).alias("is_core"),
            ((pl.col("pa") >= 0.90) & (pl.col("pm") >= 0.85)).alias("is_blue"),
            ((pl.col("pm") >= 0.85) & (pl.col("pa") < 0.90)).alias("is_blue_unwatched"),
            ((pl.col("pt") >= 0.90) & (pl.col("pm") < 0.85)).alias("is_momentum"),
            pl.col("date").dt.year().alias("year"),
        ]
    )
    # 池内均值（用于相对跌幅）
    pool_mean = (
        df.filter(pl.col("is_blue"))
        .group_by("date")
        .agg(pl.col("ret1").mean().alias("blue_mean"))
    )
    df = df.join(pool_mean, on="date", how="left")
    df = df.with_columns((pl.col("ret1") - pl.col("blue_mean")).alias("rel_ret"))

    # 前瞻收益：次日 open 入场（可执行）
    for k in (1, 3, 5, 10, 20):
        df = df.with_columns(
            [
                pl.col("open").shift(-1).over("symbol").alias("entry"),
                pl.col("close").shift(-k).over("symbol").alias(f"_c{k}"),
                (pl.col("date").shift(-k).over("symbol") - pl.col("date")).dt.total_days().alias(f"_g{k}"),
            ]
        )
        df = df.with_columns(
            pl.when(pl.col(f"_g{k}").is_between(k + 2, k + 22))
            .then(pl.col(f"_c{k}") / pl.col("entry") - 1.0)
            .otherwise(None)
            .alias(f"fwd{k}")
        ).drop([f"_c{k}", f"_g{k}"])
    # 次日日内（做 T 的 T+1 版：开盘买、次日收盘卖）+ 当日完美做 T 天花板
    df = df.with_columns(
        [
            (pl.col("close") / pl.col("low") - 1.0).alias("ceiling_intraday"),
            (pl.col("open") / pl.col("close").shift(1).over("symbol") - 1.0).alias("gap_open"),
        ]
    )

    def agg_event(sub: pl.DataFrame, label: str) -> dict:
        rec = {"label": label, "n": sub.height}
        for k in (1, 3, 5, 10, 20):
            col = sub[f"fwd{k}"].drop_nulls()
            if col.len() < 100:
                rec[f"fwd{k}"] = None
                continue
            vals = col.to_list()
            m = sum(vals) / len(vals)
            rec[f"fwd{k}_bp"] = round(m * 1e4, 1)
            rec[f"fwd{k}_win"] = round(sum(1 for x in vals if x > 0) / len(vals) * 100, 1)
            rec[f"fwd{k}_net_bp"] = round((m - COST) * 1e4, 1)
        cap = sub["ceiling_intraday"].drop_nulls()
        if cap.len() > 100:
            v = cap.to_list()
            v.sort()
            rec["ceiling_med_pct"] = round(v[len(v) // 2] * 100, 2)
            rec["ceiling_mean_pct"] = round(sum(v) / len(v) * 100, 2)
            rec["ceiling_net_mean_bp"] = round((sum(v) / len(v) - COST) * 1e4, 1)
            rec["ceiling_win_pct"] = round(sum(1 for x in v if x > COST) / len(v) * 100, 1)
        return rec

    res: dict = {"pools": {}}
    buckets = [
        (-1.0, -0.08), (-0.08, -0.05), (-0.05, -0.03), (-0.03, -0.02),
        (0.02, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 1.0),
    ]
    for pool_name, pool_expr in [
        ("blue_core", pl.col("is_blue")),
        ("blue_unwatched", pl.col("is_blue_unwatched")),
        ("momentum", pl.col("is_momentum")),
    ]:
        sub = df.filter(pool_expr)
        entry: dict = {"n_total": sub.height}
        rows = []
        for lo, hi in buckets:
            ev = sub.filter((pl.col("ret1") > lo) & (pl.col("ret1") <= hi))
            rows.append(agg_event(ev, f"当日收益 {lo*100:.0f}~{hi*100:.0f}%"))
        # 相对跌幅分档（剔除全池齐跌的 β），只看下跌侧
        for lo, hi in buckets[:4]:
            ev = sub.filter((pl.col("rel_ret") > lo) & (pl.col("rel_ret") <= hi))
            rows.append(agg_event(ev, f"相对池均值 {lo*100:.0f}~{hi*100:.0f}%"))
        entry["buckets"] = rows
        # 分年
        byyear = []
        for y in range(2016, 2027):
            e = agg_event(sub.filter((pl.col("year") == y) & (pl.col("ret1") <= -0.02)), f"{y}")
            byyear.append(e)
        entry["by_year_ret_le_-2pct"] = byyear
        res["pools"][pool_name] = entry
    # 全样本基准：所有 blue 的 fwd（不择时）
    base = df.filter(pl.col("is_blue"))
    res["baseline_blue_all_days"] = agg_event(base, "blue 全样本（不择时）")
    res["cost_assumption"] = f"双边 {COST*1e4:.0f}bp"
    res["n_rows"] = df.height
    res["last_date"] = str(df["date"].max())
    return res


def q3_timing_precision() -> dict:
    """做 T 的真正约束：日内择时需要多准才能覆盖成本。

    模型：买入价 = high − X·(high−low)，X=1 表示买在当日最低点；卖出价 = 当日收盘。
    净收益 = close/buy − 1 − COST ≥ 0  ⇒  X ≥ (high − close/(1+COST)) / (high − low) = X*

    X* 的含义：必须在日内区间的下方 (1−X*) 之内买入才不亏。
      X* 中位 0.2 → 只要不在最上面 20% 追高就行（容易）
      X* 中位 0.7 → 必须抓到最低 30% 的价位（很难）
      X* ≤ 0     → 收盘价已低于盈亏平衡价，当日无论怎么买都亏
      X* > 1     → 买在最低点也不够覆盖成本
    """
    r = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    df = _load_panel()
    sub = df.filter(pl.col("is_blue") & (pl.col("ret1") <= -0.02))
    sub = sub.filter((pl.col("high") - pl.col("low")) > 1e-9)
    sub = sub.with_columns(
        [
            ((pl.col("high") - pl.col("close") / (1 + COST)) / (pl.col("high") - pl.col("low"))).alias("x_star"),
            ((pl.col("high") - pl.col("low")) / pl.col("close") * 100).alias("amp_pct"),
            ((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low"))).alias("close_pos"),
        ]
    )
    out = {}
    for y in [None] + list(range(2016, 2027)):
        s = sub if y is None else sub.filter(pl.col("year") == y)
        if s.height < 500:
            continue
        xs = sorted(x for x in s["x_star"].drop_nulls().to_list())
        n = len(xs)
        if n < 500:
            continue
        out["all" if y is None else str(y)] = {
            "n": n,
            "x_star_p25": round(xs[int(0.25 * n)] * 100, 1),
            "x_star_med": round(xs[n // 2] * 100, 1),
            "x_star_p75": round(xs[int(0.75 * n)] * 100, 1),
            "share_x_le_0": round(sum(1 for x in xs if x <= 0) / n * 100, 1),
            "share_x_gt_1": round(sum(1 for x in xs if x > 1) / n * 100, 1),
            "amp_med_pct": round(float(s["amp_pct"].median() or 0), 2),
            "close_pos_med": round(float(s["close_pos"].median() or 0), 3),
        }
    r.setdefault("Q2_bluechip", {})["timing_precision"] = out
    OUT.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


def _load_panel() -> pl.DataFrame:
    """重算最小面板（供 q3 复用），只保留必要列。"""
    files = []
    for e in os.scandir(DATA):
        if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= START:
            files.append(str(Path(e.path) / "part.parquet"))
    files.sort()
    df = pl.read_parquet(
        files, columns=["symbol", "date", "high", "low", "close", "amount", "turnover_rate"]
    ).filter((pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0))
    df = df.sort(["symbol", "date"])
    df = df.with_columns(
        [
            (pl.col("amount") / (pl.col("turnover_rate") / 100)).alias("fmcap"),
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"),
            (pl.col("date") - pl.col("date").shift(1).over("symbol")).dt.total_days().alias("gap1"),
            pl.col("date").dt.year().alias("year"),
        ]
    )
    df = df.with_columns(
        pl.when(pl.col("gap1").is_between(1, 10)).then(pl.col("ret1")).otherwise(None).alias("ret1")
    )
    df = df.with_columns(
        [
            (pl.col("amount").rank("ordinal").over("date") - 1).truediv(pl.len().over("date") - 1).alias("pa"),
            (pl.col("fmcap").rank("ordinal").over("date") - 1).truediv(pl.len().over("date") - 1).alias("pm"),
        ]
    )
    return df.with_columns(((pl.col("pa") >= 0.90) & (pl.col("pm") >= 0.85)).alias("is_blue"))


def main() -> None:
    print("[Q1] regime ...")
    r1 = q1_regime()
    for k, v in r1.items():
        print(f"  {k:8s} {v['label']:6s} last={v['last']} ret20={v['ret20']:+.1f}% ret60={v['ret60']:+.1f}% "
              f"vsMA60={v['vs_ma60']:+.1f}% vsMA250={v['vs_ma250']:+.1f}% dd250={v['dd_from_250d_high']:+.1f}%")
    print("[Q2] bluechip T ...")
    r2 = q2_bluechip()
    print(f"  rows={r2['n_rows']:,} last={r2['last_date']}")
    for p in ("blue_core", "blue_unwatched", "momentum"):
        print(f"  --- {p} (n={r2['pools'][p]['n_total']:,})")
        for b in r2["pools"][p]["buckets"][:4]:
            print(f"    {b['label']:22s} n={b['n']:6d} fwd1={b.get('fwd1_bp')} fwd5={b.get('fwd5_bp')} "
                  f"fwd10={b.get('fwd10_bp')} fwd20={b.get('fwd20_bp')} cap={b.get('ceiling_med_pct')}")
    OUT.write_text(json.dumps({"Q1_regime": r1, "Q2_bluechip": r2}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("[Q3] timing precision ...")
    r3 = q3_timing_precision()
    for k, v in r3.items():
        print(f"  {k:5s} n={v['n']:6d} X*中位={v['x_star_med']:6.1f}%  "
              f"必亏比例={v['share_x_le_0']:5.1f}%  买最低也不够={v['share_x_gt_1']:5.1f}%  "
              f"振幅中位={v['amp_med_pct']:.2f}%  收盘位置={v['close_pos_med']:.2f}")
    print(f"[out] {OUT}")


if __name__ == "__main__":
    main()
