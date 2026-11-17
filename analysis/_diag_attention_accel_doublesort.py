"""裁决：关注度加速度的「越控制越强」是真发现，还是抑制变量假象？

上一轮回归出现危险模式：accel 单变量 t=1.16（不显著）→ 全控制 t=14.5（爆强）。
「系数随控制变量增加而变强」可能是：(1) 真发现（accel 与小盘负共线，控制后被释放）；
(2) 抑制变量陷阱（强共线性把 accel 系数吹虚高）。

线性回归无法分辨这两者，故用【独立双变量排序 double sort】裁决：
  按 市值分位(5) × 关注度加速度分位(5) 独立分组，共 25 格。
  若在【每个市值桶内】accel 从低到高单调提升收益 → 真发现；
  若 accel 效应只在「控制小盘后」才显现、同市值桶内无单调 → 抑制假象。

另外做价格加速度 paccel 的同款 double sort 作对照：若 accel 的效应只是 paccel 的镜像，
两者的 double sort 形态应高度相似。

数据：kline_daily_enriched + instruments。
输出：data/.scratch/attention_accel_doublesort.json
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

H = 20
STAMP_TAX = 0.0010
COMMISSION = 0.00025
MIN_AMOUNT = 20_000_000
ACCEL_WINDOW = 10
MOM_WINDOW = 20
N_BINS = 5


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


def load_mcap() -> pl.DataFrame:
    inst = ROOT / "data" / "instruments" / "instruments.parquet"
    if not inst.exists():
        return pl.DataFrame(schema={"symbol": pl.Utf8, "float_shares": pl.Float64})
    cols = pl.read_parquet_schema(inst)
    keep = [c for c in ("symbol", "float_shares") if c in cols]
    return pl.read_parquet(inst, columns=keep)


def _qcut_rank(col: str, n: int) -> pl.Expr:
    """按日分组，把列切成 n 等分（0..n-1），返回秩（0=最低）。"""
    r = pl.col(col).rank("ordinal").over("date")
    cnt = pl.col(col).count().over("date")
    return ((r - 1) * n // cnt).clip(0, n - 1).alias(col + "_bin")


def main() -> None:
    print("读面板 + 股本…", flush=True)
    df = load_history()
    inst = load_mcap()
    df = df.join(inst, on="symbol", how="left")
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("open") > 0)
        & (pl.col("float_shares") > 0)
    )

    print("逐日算换手率分位…", flush=True)
    def add_pturn(d: pl.DataFrame) -> pl.DataFrame:
        n = d.height
        if n < 2:
            return d.with_columns(pl.lit(0.5).alias("p_turnover"))
        t = (pl.col("turnover_rate").rank("ordinal") - 1) / (n - 1)
        return d.with_columns(t.alias("p_turnover"))

    df = df.group_by("date", maintain_order=True).map_groups(add_pturn)

    print("算 一阶/二阶/动量/价格加速度/收益…", flush=True)
    df = df.sort(["symbol", "date"])
    df = df.with_columns(
        (pl.col("p_turnover") - pl.col("p_turnover").shift(1).over("symbol")).alias("d_p_turnover")
    )
    df = df.with_columns(
        (pl.col("d_p_turnover")
         - pl.col("d_p_turnover").shift(1).over("symbol")
           .rolling_mean(window_size=ACCEL_WINDOW, min_samples=3).over("symbol"))
        .alias("accel")
    )
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(MOM_WINDOW).over("symbol") - 1.0).alias("mom")
    )
    df = df.with_columns(
        (pl.col("ret1")
         - pl.col("ret1").shift(1).over("symbol")
           .rolling_mean(window_size=ACCEL_WINDOW, min_samples=3).over("symbol"))
        .alias("paccel")
    )
    df = df.with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
    cost = STAMP_TAX + 2 * COMMISSION
    df = df.with_columns(
        (pl.col("close").shift(-(H + 1)).over("symbol")
         / pl.col("open").shift(-1).over("symbol") - 1.0 - cost).alias("fwd")
    )
    tradable = (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )
    df = df.filter(tradable & pl.col("fwd").is_not_null())
    df = df.filter(
        pl.col("accel").is_not_null() & pl.col("paccel").is_not_null()
        & pl.col("mcap").is_not_null()
    )

    # 独立分组（各自按日分位）
    df = df.with_columns([
        _qcut_rank("mcap", N_BINS),
        _qcut_rank("accel", N_BINS),
        _qcut_rank("paccel", N_BINS),
    ])

    def double_sort(x_bin: str) -> dict:
        out = {}
        for i in range(N_BINS):
            seg = df.filter(pl.col("mcap_bin") == i)
            row = []
            for j in range(N_BINS):
                cell = seg.filter(pl.col(x_bin) == j)
                daily = cell.group_by("date").agg(pl.col("fwd").mean().alias("m"))
                arr = daily["m"].to_numpy()
                if len(arr) < 10:
                    row.append(None)
                    continue
                mean = float(arr.mean())
                se = block_se(arr, block=2 * H)
                t = mean / se if se > 0 else float("nan")
                row.append({"net_bp": round(mean * 1e4, 1), "t": round(t, 2),
                            "n": int(cell.height)})
            out[f"mcap_bin{i}"] = row
        return out

    report = {"meta": {"n_bins": N_BINS, "horizon": H,
                       "mcap_bin_desc": "mcap_bin0=最小市值 ... bin4=最大市值"},
              "accel_x_mcap": double_sort("accel_bin"),
              "paccel_x_mcap": double_sort("paccel_bin")}

    # 打印
    print("\n=== 关注度加速度 accel × 市值（每行=市值桶，每列=accel 分位 0低→4高）===", flush=True)
    for i in range(N_BINS):
        cells = report["accel_x_mcap"][f"mcap_bin{i}"]
        line = f"  市值{i}: "
        for c in cells:
            if c is None:
                line += "  .  "
            else:
                line += f"{c['net_bp']:+7.1f}  "
        print(line, flush=True)

    print("\n=== 对照：价格加速度 paccel × 市值 ===", flush=True)
    for i in range(N_BINS):
        cells = report["paccel_x_mcap"][f"mcap_bin{i}"]
        line = f"  市值{i}: "
        for c in cells:
            if c is None:
                line += "  .  "
            else:
                line += f"{c['net_bp']:+7.1f}  "
        print(line, flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "attention_accel_doublesort.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/attention_accel_doublesort.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
