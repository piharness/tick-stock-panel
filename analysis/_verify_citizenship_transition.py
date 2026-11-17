"""公民等级迁移（升格/降格）的后续超额收益：事前可交易性检验。

背景：前几轮聚焦 silent 池的量价信号，本脚本扩展到**四等级的状态迁移**——
一只票被市场「重新关注」（升格）或「抛弃」（降格）后，未来收益如何？

核心命题（可证伪）：
  - 升格（如 silent→active、background→active）：被重新关注 = 追涨陷阱，还是启动前兆？
  - 降格（如 active→background、core→active）：被抛弃 = 抄底机会，还是下跌延续？

关键纪律：
  - 等级迁移 T 日收盘可知（prev_level = T-1，level = T），T+1 开盘买入。
  - 扣印花税 0.1% + 佣金双向万2.5，剔除 T+1 停牌/低成交额。
  - 基准用「同等级停留者」对照（如 silent→active 对照「一直 active」），
    这样拆出的是「迁移」这个动作的边际，而不是等级本身的效应。

数据：kline_daily_enriched（2005–2026）。
输出：data/.scratch/citizenship_transition.json
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

HORIZONS = (5, 20, 60)
STAMP_TAX = 0.0010
COMMISSION = 0.00025
MIN_AMOUNT = 20_000_000

# 重点迁移类型（升格/降格），以及它们的「停留对照」等级
TRANSITIONS = [
    # (迁移名, prev_level, curr_level, 对照等级=一直待在 curr_level)
    ("silent→active", "silent", "active", "active"),
    ("silent→background", "silent", "background", "background"),
    ("background→active", "background", "active", "active"),
    ("background→core", "background", "core", "core"),
    ("active→core", "active", "core", "core"),
    ("active→background", "active", "background", "background"),
    ("core→active", "core", "active", "active"),
]


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
    cols = ["symbol", "date", "open", "close", "volume", "amount", "turnover_rate"]
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
    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
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
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("open") > 0)
        & (pl.col("volume") > 0)
    )
    print("逐日算等级…", flush=True)
    df = df.group_by("date", maintain_order=True).map_groups(assign_level)
    print("加迁移/收益…", flush=True)
    df = enrich(df)

    tradable = (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )
    df = df.filter(tradable & pl.col("prev_level").is_not_null())

    report = {}
    for name, prev, curr, ctrl_lvl in TRANSITIONS:
        # 迁移事件
        ev = df.filter((pl.col("prev_level") == prev) & (pl.col("level") == curr))
        # 对照：一直待在 curr_level（prev_level == curr_level == curr）
        ctrl = df.filter((pl.col("prev_level") == ctrl_lvl) & (pl.col("level") == ctrl_lvl))

        if ev.height < 50:
            print(f"{name:<20} 迁移 {ev.height:<8,}（样本太少，跳过）", flush=True)
            continue

        print(f"\n=== {name}（迁移 n={ev.height:,} vs 停留 n={ctrl.height:,}）===", flush=True)
        rows = []
        for h in HORIZONS:
            ev_seg = ev.filter(pl.col(f"net{h}").is_not_null())
            ctrl_seg = ctrl.filter(pl.col(f"net{h}").is_not_null())
            if ev_seg.height == 0 or ctrl_seg.height == 0:
                continue
            # 迁移的超额 = 迁移净收益 − 同等级停留者净收益（按日配对）
            ev_daily = ev_seg.group_by("date").agg(pl.col(f"net{h}").mean().alias("e"))
            ctrl_daily = ctrl_seg.group_by("date").agg(pl.col(f"net{h}").mean().alias("c"))
            joined = ev_daily.join(ctrl_daily, on="date", how="inner")
            diff = (joined["e"] - joined["c"]).to_numpy()
            mean_diff = float(diff.mean())
            se = block_se(diff, block=2 * h)
            t = mean_diff / se if se > 0 else float("nan")
            rows.append({
                "horizon": h,
                "ev_net_bp": round(float(ev_seg[f"net{h}"].mean()) * 1e4, 1),
                "ctrl_net_bp": round(float(ctrl_seg[f"net{h}"].mean()) * 1e4, 1),
                "diff_bp": round(mean_diff * 1e4, 1),
                "se_bp": round(se * 1e4, 1),
                "t": round(t, 2),
                "n_ev": int(ev_seg.height),
                "n_days": int(len(diff)),
            })
            print(f"  H={h:>2}  迁移 {rows[-1]['ev_net_bp']:+8.1f}bp  vs  停留 {rows[-1]['ctrl_net_bp']:+8.1f}bp  "
                  f"差 {mean_diff*1e4:+8.1f}bp (t={t:+.2f})  ({len(diff)} 日)", flush=True)
        report[name] = rows

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "citizenship_transition.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/citizenship_transition.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
