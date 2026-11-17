"""H2：共识拥挤度对「关注度变化方向」信号的调节效应。

命题（可证伪）：
  「关注度上升（升格）本身不是 alpha；上升发生在哪个共识阶段才是。」
  低拥挤（事件前换手率分位低，刚被注意）→ 升格 = 信息扩散，后续跑赢；
  高拥挤（事件前换手率分位高，已经过热）→ 升格 = 拥挤踩踏，后续跑输。

拥挤度代理（我的选型）：
  crowding = 事件前 20 个交易日的换手率分位(p_turnover)均值。
  理由：拥挤的本质是「关注已经多满」，换手率分位是存量且横截面可比；
  取事件前窗口的均值抹平单日噪声，且【严格防前视】——只用事件日之前的信息。

关键纪律（母题：用结果解释结果）：
  拥挤度只允许用事件日 t 之前的交易日计算。事件日当日的换手率分位
  已被迁移定义锁死（升格必然伴随换手率变化），不能进入拥挤度。

数据：kline_daily_enriched（2005–2026）。
输出：data/.scratch/attention_consensus.json
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

HORIZONS = (20, 60)
STAMP_TAX = 0.0010
COMMISSION = 0.00025
MIN_AMOUNT = 20_000_000
CROWD_WINDOW = 20  # 拥挤度回看窗口（交易日）

# 升格 / 降格事件（关注度变化方向）
UP_TRANSITIONS = [
    ("silent→active", "silent", "active"),
    ("silent→background", "silent", "background"),
    ("background→active", "background", "active"),
    ("active→core", "active", "core"),
]
DOWN_TRANSITIONS = [
    ("active→background", "active", "background"),
    ("core→active", "core", "active"),
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


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("open") > 0)
    )
    print("逐日算等级 + 换手率分位…", flush=True)

    def assign_level_and_turnover(d: pl.DataFrame) -> pl.DataFrame:
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
        return d.with_columns([
            level.alias("level"),
            t.alias("p_turnover"),
        ])

    df = df.group_by("date", maintain_order=True).map_groups(assign_level_and_turnover)

    print("算迁移 + 拥挤度(事件前窗口) + 收益…", flush=True)
    # 迁移：prev_level = 昨日等级
    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))

    # 拥挤度：事件前 CROWD_WINDOW 日换手率分位均值。
    # 用 rolling_mean 且【排除当前行】(shift 后再 roll)，保证只用事件日之前的信息。
    df = df.with_columns(
        pl.col("p_turnover")
        .shift(1).over("symbol")  # 先排除当日
        .rolling_mean(window_size=CROWD_WINDOW, min_periods=max(5, CROWD_WINDOW // 2))
        .over("symbol")
        .alias("crowding")
    )

    # 收益：T+1 开盘买，持有 H 日（复刻 _verify_citizenship_transition 口径）
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

    tradable = (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )
    df = df.filter(tradable & pl.col("prev_level").is_not_null())

    report = {"meta": {"crowding_window": CROWD_WINDOW, "proxy": "事件前20日换手率分位均值"},
              "up": {}, "down": {}}

    for name, prev, curr in UP_TRANSITIONS:
        ev = df.filter((pl.col("prev_level") == prev) & (pl.col("level") == curr))
        report["up"][name] = _by_crowding(ev, name, "升格")
    for name, prev, curr in DOWN_TRANSITIONS:
        ev = df.filter((pl.col("prev_level") == prev) & (pl.col("level") == curr))
        report["down"][name] = _by_crowding(ev, name, "降格")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "attention_consensus.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/attention_consensus.json", flush=True)


def _by_crowding(ev: pl.DataFrame, name: str, kind: str) -> dict:
    """按拥挤度三等分，比较各档升格/降格的后续超额。"""
    seg = ev.filter(pl.col("crowding").is_not_null())
    if seg.height < 50:
        print(f"{name:<20} 事件 n={seg.height:<8,}（样本太少，跳过）", flush=True)
        return {"skipped": True, "n": int(seg.height)}

    # 拥挤度三档（低/中/高）
    q1, q2 = seg["crowding"].quantile(0.333), seg["crowding"].quantile(0.667)
    seg = seg.with_columns(
        pl.when(pl.col("crowding") <= q1).then(pl.lit("low"))
        .when(pl.col("crowding") <= q2).then(pl.lit("mid"))
        .otherwise(pl.lit("high")).alias("crowd_bucket")
    )

    print(f"\n=== {kind} {name}（n={seg.height:,}）===", flush=True)
    out = {"n": int(seg.height), "q1": float(q1), "q2": float(q2), "buckets": {}}
    for bucket in ("low", "mid", "high"):
        b = seg.filter(pl.col("crowd_bucket") == bucket)
        rows = []
        for h in HORIZONS:
            bs = b.filter(pl.col(f"net{h}").is_not_null())
            if bs.height < 10:
                continue
            daily = bs.group_by("date").agg(pl.col(f"net{h}").mean().alias("m"))
            arr = daily["m"].to_numpy()
            mean = float(arr.mean())
            se = block_se(arr, block=2 * h)
            t = mean / se if se > 0 else float("nan")
            rows.append({"horizon": h, "net_bp": round(mean * 1e4, 1),
                         "se_bp": round(se * 1e4, 1), "t": round(t, 2),
                         "n_ev": int(bs.height), "n_days": int(len(arr))})
            print(f"  {bucket:>4} H={h:>2}  {mean*1e4:+8.1f}bp (t={t:+.2f}, n={bs.height:,})", flush=True)
        out["buckets"][bucket] = rows

    # 低 vs 高 拥挤的差分（升格：期待 low > high；降格：期待 low > high，即高拥挤降格跌更多）
    low = seg.filter(pl.col("crowd_bucket") == "low")
    high = seg.filter(pl.col("crowd_bucket") == "high")
    out["low_minus_high"] = []
    for h in HORIZONS:
        l = low.filter(pl.col(f"net{h}").is_not_null()).group_by("date").agg(pl.col(f"net{h}").mean().alias("l"))
        hi = high.filter(pl.col(f"net{h}").is_not_null()).group_by("date").agg(pl.col(f"net{h}").mean().alias("h"))
        j = l.join(hi, on="date", how="inner")
        if j.height < 5:
            continue
        diff = (j["l"] - j["h"]).to_numpy()
        m = float(diff.mean())
        se = block_se(diff, block=2 * h)
        t = m / se if se > 0 else float("nan")
        out["low_minus_high"].append({"horizon": h, "diff_bp": round(m * 1e4, 1),
                                      "se_bp": round(se * 1e4, 1), "t": round(t, 2),
                                      "n_days": int(len(diff))})
        print(f"  low−high H={h:>2}  {m*1e4:+8.1f}bp (t={t:+.2f})", flush=True)
    return out


if __name__ == "__main__":
    sys.exit(main())
