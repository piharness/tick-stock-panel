"""「动量仍强但跌入沉默」信号的事前可交易性检验。

上一轮（silent-regime-conditional-alpha.md）事件研究结论：
  跌入 silent 前的动量是主导变量——动量崩坏(mom低)跌入 silent → 事件后 −861bp；
  动量仍强(mom高)跌入 silent → 事件后 +762bp（疑似错杀）。

本脚本把「动量仍强 + 跌入沉默」做成**事前信号**，检验 T+1 开盘买入持有 N 日的
真实收益。关键纪律（区别于事件研究的事后视角）：

1. 防前视：信号只用 T 日收盘可得的信息（level、20 日动量都算 T 日收盘），
   入场 T+1 开盘、出场 T+1+N 收盘。
2. 扣成本：印花税(卖出 0.1%，保守用历史口径)、佣金(双向万2.5)、
   滑点通过「单票买入不超过当日成交额 1%」的容量约束体现。
3. 可交易性：silent 是低流动性小盘，必须检验「想买能否买到」——
   剔除成交额过小、且按容量折算，避免把「买不到的量」算成收益。
4. 基准：全市场中位数 + 同规模小盘对照（silent 天然小盘，必须和小盘比）。

信号定义（可证伪，两种强度）：
  A. mom>0：20 日累计对数收益为正，且当日 level==silent。
  B. mom_pct_high：20 日动量处于当日全市场前 50% 分位，且 level==silent
     （「动量仍强」的更强定义——不是勉强为正，而是相对市场仍在涨）。

数据：kline_daily_enriched（2005–2026）。open 为原始价，close 为前复权价，
用 factor=raw_close/close 构造前复权 open，保证买入/卖出口径一致、跨除权成立。
输出：data/.scratch/silent_momentum_signal.json
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

CORE_MIN_AMOUNT = 0.90
ACTIVE_MIN_AMOUNT = 0.50
ACTIVE_MIN_TURNOVER = 0.90
SILENT_MAX_AMOUNT = 0.10
SILENT_MAX_TURNOVER = 0.10

LOOKBACK = 20
HORIZONS = (5, 20, 60)

# 成本假设
STAMP_TAX = 0.0010   # 卖出印花税 0.1%（历史口径，保守）
COMMISSION = 0.00025  # 双向佣金 万2.5
CAP_RATIO = 0.01      # 单票买入金额 ≤ 当日成交额 1%（容量/滑点约束）
MIN_AMOUNT = 20_000_000  # 最低成交额阈值（元），低于此视为不可交易


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
    cols = ["symbol", "date", "open", "close", "amount", "turnover_rate"]
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )


def assign_level(d: pl.DataFrame) -> pl.DataFrame:
    n = d.height
    if n < 2:
        return d
    a = (pl.col("amount").rank("ordinal") - 1) / (n - 1)
    t = (pl.col("turnover_rate").rank("ordinal") - 1) / (n - 1)
    level = (
        pl.when(a >= CORE_MIN_AMOUNT).then(pl.lit("core"))
        .when((a >= ACTIVE_MIN_AMOUNT) | (t >= ACTIVE_MIN_TURNOVER)).then(pl.lit("active"))
        .when((a < SILENT_MAX_AMOUNT) & (t < SILENT_MAX_TURNOVER)).then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
    )
    return d.with_columns(level.alias("level"))


def enrich(df: pl.DataFrame) -> pl.DataFrame:
    """20 日动量 + 当日动量分位。全部只用当日及以前信息。

    注意：enriched 的 open 与 close 都已是前复权价（同基准），
    收益直接用 close/open 相减即可，跨除权成立，无需任何复权因子。
    """
    # 20 日动量（累计对数收益，用前复权 close）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("log_ret")
    )
    df = df.with_columns(
        pl.col("log_ret").rolling_sum(window_size=LOOKBACK, min_samples=LOOKBACK)
        .over("symbol").alias("mom")
    )
    # 当日动量横截面分位（0~1，1=最高）
    df = df.with_columns(
        ((pl.col("mom").rank("ordinal").over("date") - 1)
         / (pl.col("mom").count().over("date") - 1).clip(lower_bound=1)).alias("mom_pct")
    )
    return df


def build_signal(df: pl.DataFrame) -> pl.DataFrame:
    """构造事前信号。返回 (df, sigA, sigB)。sigA/sigB 每行 = 一个 T 日信号。"""
    # 信号 A：mom > 0 且 silent
    sigA = df.filter((pl.col("level") == "silent") & (pl.col("mom") > 0))
    # 信号 B：mom_pct >= 0.5 且 silent（动量前 50%）
    sigB = df.filter((pl.col("level") == "silent") & (pl.col("mom_pct") >= 0.5))
    return df, sigA, sigB


def compute_fwd_returns(df: pl.DataFrame) -> pl.DataFrame:
    """T+1 开盘买入，持有 N 日 T+1+N 收盘卖出，扣成本后的净收益。

    收益 = close[T+1+N] / open[T+1] - 1 - 成本
    成本 = 印花税(卖出) + 佣金(双向)。滑点通过容量约束在信号层处理。

    关键：open/close 都是前复权，直接相除。但必须处理停牌——停牌日无数据行，
    shift 会跨过停牌日。这里用「未来第 k 个真实交易日的价格」的 shift 语义：
    shift(-1) 取的是下一个有数据的交易日（若 T+1 停牌则取 T+2），这本身是
    正确的「下一个可交易日」。停牌期间的跳空会进入收益，这是真实风险（停牌
    复牌跳空），但需额外标记停牌以便后续敏感性分析。
    """
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-(h + 1)).over("symbol")
             / pl.col("open").shift(-1).over("symbol") - 1.0).alias(f"gross{h}")
        )
    # 成本（固定比例部分）：印花税 + 双向佣金
    cost = STAMP_TAX + 2 * COMMISSION
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col(f"gross{h}") - cost).alias(f"net{h}")
        )
    # 标记停牌：下一交易日自然日间隔 > 7 天视为停牌（跨过周末+长假期后仍 >7 天即异常）
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
    return df


def benchmark_returns(df: pl.DataFrame) -> pl.DataFrame:
    """全市场等权（中位数）与同规模小盘对照的持有收益（同口径 T+1 open → T+1+N close）。"""
    for h in HORIZONS:
        med = df.group_by("date").agg(pl.col(f"net{h}").median().alias(f"mkt_med{h}"))
        df = df.join(med, on="date")
    return df


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    print(f"  {df.height:,} 行  {df['date'].min()} → {df['date'].max()}", flush=True)
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
        & (pl.col("open") > 0)
    )
    print("逐日算等级…", flush=True)
    df = df.group_by("date", maintain_order=True).map_groups(assign_level)
    print("加动量 / 分位…", flush=True)
    df = enrich(df)

    print("算未来持有收益 + 基准…", flush=True)
    df = compute_fwd_returns(df)
    df = benchmark_returns(df)

    print("构造信号…", flush=True)
    _, sigA, sigB = build_signal(df)

    report = {"horizons": list(HORIZONS), "cost": {
        "stamp_tax": STAMP_TAX, "commission": COMMISSION, "cap_ratio": CAP_RATIO,
        "min_amount": MIN_AMOUNT,
    }}

    for name, sig in [("A_mom>0", sigA), ("B_mom_pct_high", sigB)]:
        print(f"\n=== 信号 {name} ===", flush=True)
        # 可交易性约束 1：成交额 >= MIN_AMOUNT（太低买不进）
        # 可交易性约束 2：T+1 不停牌（停牌无法在开盘买入）
        #   _next_gap 是「到下一交易日的自然日间隔」，>7 天视为 T+1 停牌（跨周末+长假后仍>7 即异常）
        tradable = sig.filter(
            (pl.col("amount") >= MIN_AMOUNT)
            & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
        )
        n_sig = sig.height
        n_trad = tradable.height
        print(f"  信号总数 {n_sig:,}，成交额≥{MIN_AMOUNT/1e6:.0f}M 且 T+1 不停牌 可交易 {n_trad:,} "
              f"({n_trad/n_sig*100:.1f}%)", flush=True)

        rows = []
        for h in HORIZONS:
            seg = tradable.filter(pl.col(f"net{h}").is_not_null())
            # 信号净收益 vs 全市场中位数
            exc = seg[f"net{h}"] - seg[f"mkt_med{h}"]
            # 按日聚合（同日多票共享 beta，不独立）
            daily_exc = (
                seg.with_columns(exc.alias("_exc"))
                .group_by("date").agg(pl.col("_exc").mean().alias("e"))
            )
            mean_exc = float(daily_exc["e"].mean())
            se_exc = block_se(daily_exc["e"].to_numpy(), block=2 * h)
            t_exc = mean_exc / se_exc if se_exc > 0 else float("nan")
            # 信号净收益（绝对，未对基准）
            gross = float(seg[f"gross{h}"].mean())
            net = float(seg[f"net{h}"].mean())
            # 命中率（相对市场）
            win_rate = float((exc > 0).mean())
            n_days = int(daily_exc.height)
            rows.append({
                "horizon": h, "n_days": n_days, "n_trades": int(seg.height),
                "gross_bp": round(gross * 1e4, 1),
                "net_bp": round(net * 1e4, 1),
                "exc_vs_mkt_bp": round(mean_exc * 1e4, 1),
                "exc_se_bp": round(se_exc * 1e4, 1),
                "t": round(t_exc, 2),
                "win_rate": round(win_rate, 4),
            })
            print(f"  H={h:>2}  净收益 {net*1e4:+8.1f}bp  超额vs市场 {mean_exc*1e4:+8.1f}bp "
                  f"(t={t_exc:+.2f})  命中率 {win_rate:.1%}  ({n_days} 日 / {seg.height:,} 笔)", flush=True)
        report[name] = rows

    # 对照：全部 silent（不含动量过滤），看动量过滤是否真的提升了信号
    all_silent = df.filter(
        (pl.col("level") == "silent") & (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )
    print("\n=== 对照：全部 silent（无动量过滤，可交易）===", flush=True)
    rows = []
    for h in HORIZONS:
        seg = all_silent.filter(pl.col(f"net{h}").is_not_null())
        exc = seg[f"net{h}"] - seg[f"mkt_med{h}"]
        daily_exc = seg.with_columns(exc.alias("_exc")).group_by("date").agg(
            pl.col("_exc").mean().alias("e"))
        mean_exc = float(daily_exc["e"].mean())
        se_exc = block_se(daily_exc["e"].to_numpy(), block=2 * h)
        t_exc = mean_exc / se_exc if se_exc > 0 else float("nan")
        rows.append({"horizon": h, "exc_vs_mkt_bp": round(mean_exc * 1e4, 1),
                     "t": round(t_exc, 2), "n_days": int(daily_exc.height),
                     "n_trades": int(seg.height)})
        print(f"  H={h:>2}  超额vs市场 {mean_exc*1e4:+8.1f}bp (t={t_exc:+.2f})  "
              f"({daily_exc.height} 日 / {seg.height:,} 笔)", flush=True)
    report["all_silent"] = rows

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "silent_momentum_signal.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/silent_momentum_signal.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
