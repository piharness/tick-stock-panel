"""证伪「赢家法则」：菲阿里式 1 日区间突破 vs 60 日新高突破。

背景：用户想从「市场赢家」挖价值。日本期货三年三冠的菲阿里公开宣称极简规则：
  只看 昨日最高/最低/收盘 + 今日开盘，突破昨日最高做多、跌破昨日最低做空。
本脚本把这条「赢家法则」转成可回测规则，在 A 股 20 年日K上验证是否有可交易超额。

命题（可证伪）：
  「突破昨日最高做多」在 A 股是否有可交易超额？它是「可复制的赢家 skill」，
  还是「幸存者叙事」（只在特定市场状态/年份有效，全样本不成立）？

对照：现有 60 日新高突破（breakout_new_high_60d），看短窗口(1日)与长窗口(60日)
  突破是否有本质差异。菲阿里用的是 1 日区间，这是与现有策略的关键差异点。

关键纪律：
  - 日K口径：T 日收盘突破昨日最高 → T+1 开盘买入（保守，因为盘中突破无法在日K复盘）。
  - 扣印花税 0.1% + 佣金双向万2.5。
  - 逐年分解，检验「赢家法则」是否近5年衰减（前几轮所有量价信号的共同模式）。
  - 排除停牌/低成交额。

数据：kline_daily_enriched（2005–2026）。
输出：data/.scratch/winner_rule_breakout.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"

HORIZONS = (5, 20)
STAMP_TAX = 0.0010
COMMISSION = 0.00025
COST = STAMP_TAX + 2 * COMMISSION
MIN_AMOUNT = 20_000_000


def block_se(x: np.ndarray, block: int, reps: int = 1000, seed: int = 0) -> float:
    n = len(x)
    if n < block:
        block = max(1, n)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[i] = x[idx.ravel()[:n]].mean()
    return float(means.std(ddof=1))


def load_history() -> pl.DataFrame:
    files = sorted(str(p) for p in TABLE.glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "close", "high", "low", "amount"]
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )


def main() -> None:
    print("读日K…", flush=True)
    df = load_history()
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("high") > 0) & (pl.col("low") > 0)
        & (pl.col("open") > 0) & (pl.col("amount") > 0)
    )

    print("算突破信号…", flush=True)
    # 昨日最高 / 昨日最低（shift 1）
    df = df.with_columns([
        pl.col("high").shift(1).over("symbol").alias("prev_high"),
        pl.col("low").shift(1).over("symbol").alias("prev_low"),
    ])
    # 60日新高（对照，用 rolling max，shift 1 排除当日）
    df = df.with_columns(
        pl.col("high").shift(1).over("symbol")
        .rolling_max(window_size=60, min_samples=30).over("symbol")
        .alias("high_60d")
    )
    # 信号：收盘突破昨日最高 = 菲阿里做多
    df = df.with_columns(
        (pl.col("close") > pl.col("prev_high")).alias("sig_1d"),
        (pl.col("close") > pl.col("high_60d")).alias("sig_60d"),
    )

    # 未来收益（T+1 开盘买入，持有 H 日）
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-(h + 1)).over("symbol")
             / pl.col("open").shift(-1).over("symbol") - 1.0 - COST).alias(f"fwd{h}")
        )

    tradable = (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )
    df = df.filter(tradable)

    report = {"meta": {"horizons": list(HORIZONS)}, "signals": {}}

    for sig_name, sig_col in (("1日区间突破(菲阿里)", "sig_1d"), ("60日新高突破(对照)", "sig_60d")):
        ev = df.filter(pl.col(sig_col))
        print(f"\n=== {sig_name}（n={ev.height:,}）===", flush=True)
        sig_report = {"n": int(ev.height), "horizons": {}, "annual": {}}
        for h in HORIZONS:
            s = ev.filter(pl.col(f"fwd{h}").is_not_null())
            if s.height < 10:
                continue
            arr = s[f"fwd{h}"].to_numpy()
            mean = float(arr.mean())
            se = block_se(arr, block=2 * h)
            t = mean / se if se > 0 else float("nan")
            sig_report["horizons"][f"H{h}"] = {
                "net_bp": round(mean * 1e4, 1), "t": round(t, 2),
                "n_ev": int(s.height),
            }
            print(f"  H={h:>2}  {mean*1e4:+8.1f}bp (t={t:+.2f}, n={s.height:,})", flush=True)

        # 逐年分解（检验近5年衰减）
        print(f"  逐年 H=20 净收益（bp）：", flush=True)
        for year in range(2005, 2027):
            s = ev.filter(
                (pl.col("date").dt.year() == year) & (pl.col("fwd20").is_not_null())
            )
            if s.height < 20:
                continue
            mean = float(s["fwd20"].mean()) * 1e4
            sig_report["annual"][str(year)] = round(mean, 1)
            if year in (2005, 2010, 2015, 2020, 2021, 2022, 2023, 2024, 2025, 2026):
                print(f"    {year}: {mean:+8.1f}bp (n={s.height:,})", flush=True)
        report["signals"][sig_name] = sig_report

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "winner_rule_breakout.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/winner_rule_breakout.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
