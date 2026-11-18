"""市场门 (regime gate) 判据检验。

判据要回答的不是「加了门收益变好吗」, 而是「**门的信号有没有预测力**」。
等权市场本身是负期望 (10 年 −13.3%), 所以任何降低仓位的做法都会改善收益 ——
那是降杠杆, 不是择时。三个对照用来把这两者分开:

  1. 恒定仓位对照: 平均暴露相同但每天都不择时。门若只赢在降杠杆, 打不过它。
  2. 随机空仓对照: 同样的空仓天数与切换次数, 但信号被随机打乱。门若没有预测力,
     会被随机分布的同一分位数轻松覆盖。
  3. 成本敏感度: 每次切换按单边 5/10/15bp 计成本。10 年 200+ 次切换会吃掉多少。

口径与生产一致: 等权日收益按**日历相邻**计算 (跨停牌不算一日), 剔除 |ret|>=11.5%。

用法:
    backend/.venv/Scripts/python.exe analysis/_diag_regime_gate.py --days 2400 --trials 300
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kline_daily_enriched"
TRADING_DAYS = 244


def load_daily(days: int) -> pl.DataFrame:
    parts = sorted(DATA.glob("date=*/part.parquet"))
    if len(parts) < days + 2:
        raise SystemExit(f"分区不足: 只有 {len(parts)} 个, 需要 {days + 2}")
    frames = [
        pl.read_parquet(p, columns=["symbol", "date", "close"]) for p in parts[-(days + 2):]
    ]
    df = pl.concat(frames).sort(["symbol", "date"])
    calendar = sorted(df["date"].unique().to_list())
    index = {day: i for i, day in enumerate(calendar)}
    df = df.with_columns(
        pl.col("date").replace_strict(index, return_dtype=pl.Int32).alias("ti"),
        pl.col("close").shift(1).over("symbol").alias("prev_close"),
    )
    df = df.with_columns(pl.col("ti").shift(1).over("symbol").alias("prev_ti"))
    df = df.filter(pl.col("ti") - pl.col("prev_ti") == 1)
    df = df.with_columns((pl.col("close") / pl.col("prev_close") - 1).alias("ret"))
    df = df.filter(pl.col("ret").abs() < 0.115)
    return (
        df.group_by("date")
        .agg(
            pl.col("ret").mean().alias("eq_ret"),
            (pl.col("ret") > 0).mean().alias("breadth"),
        )
        .sort("date")
    )


def nav_stats(rets: list[float], cost_bp: float = 0.0, pos: list[float] | None = None) -> dict:
    nav = peak = 1.0
    mdd = 0.0
    switches = 0
    prev = 1.0
    for i, r in enumerate(rets):
        p = 1.0 if pos is None else pos[i]
        if pos is not None and p != prev:
            switches += 1
        # 切换成本按「仓位变化量」计, 进出都收费
        cost = abs(p - prev) * cost_bp / 1e4
        prev = p
        nav *= 1 + p * r - cost
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
    n = len(rets)
    cagr = nav ** (TRADING_DAYS / n) - 1 if nav > 0 else -1.0
    mean = sum(r * (1.0 if pos is None else pos[i]) for i, r in enumerate(rets)) / n
    var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
    vol = var ** 0.5 * (TRADING_DAYS ** 0.5)
    return {
        "total_pct": round((nav - 1) * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "mdd_pct": round(mdd * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "calmar": round(cagr / abs(mdd), 2) if mdd < 0 else None,
        "switches": switches,
    }


def build_pos(gate: list[bool]) -> list[float]:
    """T+1 生效: 第 i 天用第 i-1 天的信号。"""
    return [0.0 if (i > 0 and gate[i - 1]) else 1.0 for i in range(len(gate))]


def scan_thresholds(ma: list[float], br: list[float], rets: list[float]) -> None:
    """阈值稳健性: 判据必须是一片高地, 不是一根尖峰。

    若只有某一个阈值好看, 那是数据挖掘; 若邻近阈值都能改善基准,
    说明捕捉到的是真实的结构 (广度萎缩 = 等权市场的坏时段)。
    """
    bench = nav_stats(rets)["total_pct"]
    print(f"阈值扫描 (基准 {bench:+.1f}%):")
    print(f"{'br20<':>6} {'空仓%':>6} {'累计%':>9} {'年化%':>7} {'回撤%':>7} {'Calmar':>7} {'切换':>5}")
    for th in [x / 100 for x in range(40, 61, 2)]:
        gate = [b < th for b in br]
        pos = build_pos(gate)
        st = nav_stats(rets, pos=pos)
        flat = sum(1 for p in pos if p == 0) / len(pos) * 100
        print(
            f"{th * 100:>5.0f}% {flat:>6.0f} {st['total_pct']:>9.1f} {st['cagr_pct']:>7.1f} "
            f"{st['mdd_pct']:>7.1f} {str(st['calmar']):>7} {st['switches']:>5}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2400)
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    daily = load_daily(args.days)
    daily = daily.with_columns(
        pl.col("eq_ret").rolling_mean(20).alias("ma20"),
        pl.col("breadth").rolling_mean(20).alias("br20"),
    ).drop_nulls(["ma20", "br20"])

    rets = daily["eq_ret"].to_list()
    dates = [str(d) for d in daily["date"].to_list()]
    ma = daily["ma20"].to_list()
    br = daily["br20"].to_list()
    n = len(rets)

    print(f"样本 {dates[0]} → {dates[-1]}  ({n} 个交易日)")
    print(f"当前 ma20={ma[-1] * 1e4:+.1f}bp   br20={br[-1] * 100:.1f}%")
    bench = nav_stats(rets)
    print(
        f"一直持有: 累计 {bench['total_pct']:+.1f}%  年化 {bench['cagr_pct']:+.1f}%  "
        f"回撤 {bench['mdd_pct']:.1f}%  Calmar {bench['calmar']}"
    )

    if args.scan:
        scan_thresholds(ma, br, rets)
        return 0

    # 候选门: 以广度为主判据 (先前 500 日样本里 ma20 越严越差, 说明跌幅条件是噪声)
    candidates = [
        ("br20<50%", lambda m, b: b < 0.50),
        ("br20<48%", lambda m, b: b < 0.48),
        ("br20<45%", lambda m, b: b < 0.45),
        ("ma20<0 & br20<50%", lambda m, b: m < 0 and b < 0.50),
        ("ma20<0 & br20<45%", lambda m, b: m < 0 and b < 0.45),
        ("ma20<-20bp & br20<45%", lambda m, b: m < -0.0020 and b < 0.45),
    ]

    print("\n门 vs 对照 (无成本):")
    print(f"{'门':>22} {'空仓%':>6} {'累计%':>8} {'年化%':>7} {'回撤%':>7} "
          f"{'Calmar':>7} {'切换':>5} {'恒定仓位累计%':>13} {'随机同暴露中位%':>15}")
    results = []
    for label, fn in candidates:
        gate = [fn(m, b) for m, b in zip(ma, br)]
        pos = build_pos(gate)
        st = nav_stats(rets, pos=pos)
        flat = sum(1 for p in pos if p == 0) / n
        # 对照 1: 恒定仓位 = 平均暴露
        const_pos = [1 - flat] * n
        const_st = nav_stats(rets, pos=const_pos)
        # 对照 2: 随机打乱, 保持同样的空仓天数
        rng = random.Random(20261118)
        sims = []
        for _ in range(args.trials):
            p = [0.0 if rng.random() < flat else 1.0 for _ in range(n)]
            sims.append(nav_stats(rets, pos=p)["total_pct"])
        sims.sort()
        med = sims[len(sims) // 2]
        p90 = sims[int(len(sims) * 0.90)]
        st["flat_pct"] = round(flat * 100, 1)
        st["const_total"] = const_st["total_pct"]
        st["rand_med"] = med
        st["rand_p90"] = p90
        st["label"] = label
        results.append(st)
        print(
            f"{label:>22} {flat * 100:>5.0f}% {st['total_pct']:>8.1f} {st['cagr_pct']:>7.1f} "
            f"{st['mdd_pct']:>7.1f} {str(st['calmar']):>7} {st['switches']:>5} "
            f"{const_st['total_pct']:>13.1f} {med:>15.1f}"
        )

    best = max(results, key=lambda r: r["total_pct"])
    print(
        f"\n最强门 = {best['label']}: 累计 {best['total_pct']:+.1f}%  "
        f"vs 恒定同暴露 {best['const_total']:+.1f}%  "
        f"vs 随机同暴露中位 {best['rand_med']:+.1f}% (p90 {best['rand_p90']:+.1f}%)"
    )
    if best["total_pct"] <= best["rand_p90"]:
        print("  警告: 门的收益没有超过随机同暴露的 90 分位 ⇒ 择时预测力不成立")
    else:
        print("  门的收益超过随机同暴露 p90 ⇒ 信号含增量信息")

    # 成本敏感度
    label, fn = next((l, f) for l, f in candidates if l == best["label"])
    gate = [fn(m, b) for m, b in zip(ma, br)]
    pos = build_pos(gate)
    print(f"\n成本敏感度 ({label}, 切换 {best['switches']} 次, 按仓位变化量计费):")
    print(f"{'单边bp':>7} {'累计%':>9} {'年化%':>8} {'回撤%':>8} {'Calmar':>7}")
    cost_rows = []
    for bp in (0, 5, 10, 15, 25):
        st = nav_stats(rets, cost_bp=bp, pos=pos)
        cost_rows.append({"cost_bp": bp, **st})
        print(
            f"{bp:>7} {st['total_pct']:>9.1f} {st['cagr_pct']:>8.1f} "
            f"{st['mdd_pct']:>8.1f} {str(st['calmar']):>7}"
        )

    # 分年度: 门在牛市里是否只是拖累
    print(f"\n分年度 ({label}: 策略 vs 一直持有):")
    by_year: dict[str, list[int]] = {}
    for i, d in enumerate(dates):
        by_year.setdefault(d[:4], []).append(i)
    print(f"{'年':>5} {'交易日':>6} {'空仓%':>6} {'策略%':>8} {'持有%':>8} {'差%':>8}")
    for y in sorted(by_year):
        idxs = by_year[y]
        s_acc = 1.0
        b_acc = 1.0
        flat = 0
        for i in idxs:
            s_acc *= 1 + pos[i] * rets[i]
            b_acc *= 1 + rets[i]
            if pos[i] == 0:
                flat += 1
        print(
            f"{y:>5} {len(idxs):>6} {flat / len(idxs) * 100:>5.0f}% "
            f"{(s_acc - 1) * 100:>8.1f} {(b_acc - 1) * 100:>8.1f} "
            f"{(s_acc - b_acc) * 100:>8.1f}"
        )

    if args.out:
        Path(args.out).write_text(
            json.dumps({"gates": results, "cost": cost_rows}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
