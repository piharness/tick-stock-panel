"""「首阴战法」事前可交易性检验：强势股首次收阴后，次日买入的胜率与收益。

首阴战法的核心假设（A 股短线经典）：
  连续上涨（尤其连板/大涨）后的第一根阴线是「洗盘」而非「见顶」，
  次日低开高走或反弹，是低吸买点。

命题（可证伪）：
  强势股（连板或短期大涨）首阴后，次日开盘买入，持有 N 日，
  是否有显著正收益 / 高胜率？「首阴」到底是上车点还是见顶信号？

两个信号定义（覆盖两种主流「首阴」理解）：
  A. 连板首阴：T 日收阴，且 T-1 日 consecutive_limit_ups >= 1（断板首阴）
  B. 大涨首阴：T 日收阴，且 T-1 日前 3 日累计涨幅 >= 20%（高位首阴）

关键检验维度：
  1. 首阴后次日（T+1）低开买入的短期收益（首阴战法赌的是低开反弹）
  2. 缩量阴线 vs 放量阴线（洗盘 vs 出货的分辨）
  3. 阴线幅度（小阴 vs 大阴）对后续的影响

纪律（防前视、可交易）：
  - 首阴信号 T 日收盘可知，T+1 开盘买入（open 已是前复权价）
  - 扣印花税 0.1% + 佣金双向万2.5
  - 剔除 T+1 停牌/一字跌停（买不进/卖不出）

数据：kline_daily_enriched（2005–2026）。
输出：data/.scratch/first_negative_line.json
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

HORIZONS = (1, 3, 5, 10)
STAMP_TAX = 0.0010
COMMISSION = 0.00025
MIN_AMOUNT = 30_000_000  # 首阴战法可放宽到 3000 万（强势股流动性通常够）


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
    cols = ["symbol", "date", "open", "close", "volume", "amount",
            "turnover_rate", "consecutive_limit_ups"]
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )


def enrich(df: pl.DataFrame) -> pl.DataFrame:
    """日收益、阴线标记、量比、停牌标记、未来收益。"""
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
    # 阴线 = 收盘 < 前收
    df = df.with_columns((pl.col("ret1") < 0).alias("is_red_down"))
    # 3 日累计涨幅（截至 T-1，用于「大涨首阴」）
    df = df.with_columns(
        (pl.col("close").shift(1).over("symbol")
         / pl.col("close").shift(4).over("symbol") - 1.0).alias("cum3d_before")
    )
    # 量比（相对前 5 日均量）
    df = df.with_columns(
        pl.col("volume").shift(1).rolling_mean(window_size=5, min_samples=5)
        .over("symbol").alias("vol_mean_5d")
    )
    df = df.with_columns((pl.col("volume") / pl.col("vol_mean_5d")).alias("vol_ratio"))
    # 停牌标记
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
    # 未来收益：T+1 开盘买入，持有 h 日 T+1+h 收盘卖出
    cost = STAMP_TAX + 2 * COMMISSION
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-(h + 1)).over("symbol")
             / pl.col("open").shift(-1).over("symbol") - 1.0 - cost).alias(f"net{h}")
        )
    return df


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    print(f"  {df.height:,} 行  {df['date'].min()} → {df['date'].max()}", flush=True)
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("open") > 0)
        & (pl.col("volume") > 0)
    )
    print("加特征/收益…", flush=True)
    df = enrich(df)

    tradable = (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )

    # 信号 A：连板首阴（T-1 连板 >=1，T 日收阴）
    sigA = df.filter(
        (pl.col("consecutive_limit_ups").shift(1).over("symbol") >= 1)
        & (pl.col("ret1") < 0)
        & tradable
    )
    # 信号 B：大涨首阴（T-1 前 3 日累计涨幅 >=20%，T 日收阴）
    sigB = df.filter(
        (pl.col("cum3d_before") >= 0.20)
        & (pl.col("ret1") < 0)
        & tradable
    )

    report = {"n_sigA": int(sigA.height), "n_sigB": int(sigB.height)}

    for name, sig in [("A_连板首阴", sigA), ("B_大涨首阴", sigB)]:
        print(f"\n=== 信号 {name}（n={sig.height:,}）===", flush=True)
        rows = []
        for h in HORIZONS:
            seg = sig.filter(pl.col(f"net{h}").is_not_null())
            if seg.height == 0:
                continue
            daily = seg.group_by("date").agg(pl.col(f"net{h}").mean().alias("e"))
            mean = float(daily["e"].mean())
            se = block_se(daily["e"].to_numpy(), block=2 * h)
            t = mean / se if se > 0 else float("nan")
            win = float((seg[f"net{h}"] > 0).mean())
            rows.append({"horizon": h, "net_bp": round(mean * 1e4, 1),
                         "se_bp": round(se * 1e4, 1), "t": round(t, 2),
                         "win_rate": round(win, 4), "n": int(seg.height)})
            print(f"  H={h:>2}  净收益 {mean*1e4:+7.1f}bp(t={t:+.2f})  胜率 {win:.1%}  ({seg.height:,} 笔)", flush=True)
        report[name] = rows

        # 缩量 vs 放量（洗盘 vs 出货）
        if name == "A_连板首阴":
            print(f"  --- 缩量阴线 vs 放量阴线（H=3）---", flush=True)
            for label, cond in [("缩量(量比<1)", pl.col("vol_ratio") < 1.0),
                                ("放量(量比>=2)", pl.col("vol_ratio") >= 2.0)]:
                sub = sig.filter(cond & pl.col("net3").is_not_null())
                if sub.height == 0:
                    continue
                daily = sub.group_by("date").agg(pl.col("net3").mean().alias("e"))
                mean = float(daily["e"].mean())
                se = block_se(daily["e"].to_numpy(), block=6)
                t = mean / se if se > 0 else float("nan")
                win = float((sub["net3"] > 0).mean())
                print(f"    {label:<12} 净收益 {mean*1e4:+7.1f}bp(t={t:+.2f}) 胜率 {win:.1%} ({sub.height:,} 笔)", flush=True)

            # 阴线幅度
            print(f"  --- 阴线幅度（H=3）---", flush=True)
            for label, lo, hi in [("小阴(>-3%)", -0.03, 0.0), ("中阴(-7%~-3%)", -0.07, -0.03), ("大阴(<-7%)", -1.0, -0.07)]:
                sub = sig.filter((pl.col("ret1") > lo) & (pl.col("ret1") <= hi) & pl.col("net3").is_not_null())
                if sub.height == 0:
                    continue
                daily = sub.group_by("date").agg(pl.col("net3").mean().alias("e"))
                mean = float(daily["e"].mean())
                se = block_se(daily["e"].to_numpy(), block=6)
                t = mean / se if se > 0 else float("nan")
                print(f"    {label:<14} 净收益 {mean*1e4:+7.1f}bp(t={t:+.2f}) ({sub.height:,} 笔)", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "first_negative_line.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/first_negative_line.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
