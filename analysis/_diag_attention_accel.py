"""方向1：关注度的二阶量（加速度）是否独立于价格动量？

命题（可证伪）：
  关注度加速度 Δ²attention = 换手率分位变化(一阶)的再差分/相对加速。
  「人气加速聚集」理论上独立于「价格动量」，因为价格可横盘而人气加速（吸筹），
  或价格冲高而人气减速（见顶）。

必须攻击的假设：
  换手率与价格强相关，故「关注度加速度」可能只是「价格加速度（二阶）」的镜像。
  因此必须同时控制 价格一阶(动量) + 价格二阶(加速度) + 市值 + 换手水平，
  若控制后仍有增量，才承认「关注度加速度」是独立信号。

构造（防前视，全部只用 t 日及之前信息）：
  p_turnover_t          当日换手率分位（横截面，1=最高）
  d_p_turnover_t       = p_turnover_t - p_turnover_{t-1}   （一阶，方向）
  accel_t              = d_p_turnover_t - mean(d_p_turnover_{t-1..t-N})  （二阶，加速度）

  价格一阶 mom_t        = close_t / close_{t-M} - 1
  价格二阶 paccel_t     = ret1_t - mean(ret1_{t-1..t-N})   （日收益的加速度，与关注度同构）

方法：Fama-MacBeth 横截面回归。
  因变量 = H=20 未来净收益（T+1 开盘买）
  核心 X = accel_t（关注度加速度）
  控制 Z = [mom_t（价格一阶）, paccel_t（价格二阶）, ln_mcap, p_turnover_t（换手水平）]

  分「加速升温 accel>0」与「减速 accel<0」两组看方向；核心看 accel 系数在全控制下是否仍显著。

数据：kline_daily_enriched（2005–2026）+ instruments。
输出：data/.scratch/attention_accel.json
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
ACCEL_WINDOW = 10   # 一阶变化的回看窗口（算「平均升温速率」）
MOM_WINDOW = 20     # 价格动量窗口


def _lstsq(x: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    if x.shape[0] < x.shape[1] + 10:
        return None
    try:
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        return beta
    except np.linalg.LinAlgError:
        return None


def nw_se(ts: np.ndarray, lag: int = 5) -> float:
    n = len(ts)
    if n == 0:
        return float("nan")
    m = ts - ts.mean()
    gamma0 = (m ** 2).sum() / n
    total = gamma0
    for l in range(1, min(lag, n - 1) + 1):
        total += 2 * (1 - l / (lag + 1)) * ((m[l:] * m[:-l]).sum() / n)
    return float(np.sqrt(total / n))


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
    # 关注度一阶：换手率分位日差分
    df = df.with_columns(
        (pl.col("p_turnover") - pl.col("p_turnover").shift(1).over("symbol"))
        .alias("d_p_turnover")
    )
    # 关注度二阶（加速度）：今日一阶 vs 过去 N 日一阶均值
    df = df.with_columns(
        (pl.col("d_p_turnover")
         - pl.col("d_p_turnover").shift(1).over("symbol")
           .rolling_mean(window_size=ACCEL_WINDOW, min_samples=3).over("symbol"))
        .alias("accel")
    )
    # 价格日收益
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
    # 价格一阶（动量）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(MOM_WINDOW).over("symbol") - 1.0).alias("mom")
    )
    # 价格二阶（加速度，与关注度同构）：今日收益 vs 过去 N 日收益均值
    df = df.with_columns(
        (pl.col("ret1")
         - pl.col("ret1").shift(1).over("symbol")
           .rolling_mean(window_size=ACCEL_WINDOW, min_samples=3).over("symbol"))
        .alias("paccel")
    )
    # 对数市值
    df = df.with_columns((pl.col("close") * pl.col("float_shares")).log().alias("ln_mcap"))
    # 未来收益
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

    # 只保留有完整二阶量的样本
    df = df.filter(
        pl.col("accel").is_not_null() & pl.col("mom").is_not_null()
        & pl.col("paccel").is_not_null() & pl.col("ln_mcap").is_not_null()
        & pl.col("p_turnover").is_not_null() & pl.col("d_p_turnover").is_not_null()
    )

    report = {"meta": {"accel_window": ACCEL_WINDOW, "mom_window": MOM_WINDOW, "horizon": H},
              "n": int(df.height)}

    # ===== A. 分组检验：加速升温 vs 减速 =====
    print(f"\n总样本 n={df.height:,}，开始分组检验…", flush=True)
    df = df.with_columns(
        pl.when(pl.col("accel") >= 0).then(pl.lit("accel")).otherwise(pl.lit("decel")).alias("accel_grp")
    )
    grp = {}
    for (g,), d in df.group_by("accel_grp"):
        daily = d.group_by("date").agg(pl.col("fwd").mean().alias("m"))
        arr = daily["m"].to_numpy()
        mean = float(arr.mean())
        se = block_se(arr, block=2 * H)
        t = mean / se if se > 0 else float("nan")
        grp[g] = {"net_bp": round(mean * 1e4, 1), "se_bp": round(se * 1e4, 1),
                  "t": round(t, 2), "n": int(d.height), "n_days": int(len(arr))}
        print(f"  {g:>6}  n={d.height:>8,}  H=20 {mean*1e4:+8.1f}bp (t={t:+.2f})", flush=True)
    report["groups"] = grp

    # ===== B. 正交化回归：accel 是否独立于价格一阶/二阶 =====
    print("\n正交化回归…", flush=True)
    df = df.with_columns([
        ((pl.col("accel") - pl.col("accel").mean().over("date"))
         / pl.col("accel").std().over("date")).alias("z_accel"),
        ((pl.col("mom") - pl.col("mom").mean().over("date"))
         / pl.col("mom").std().over("date")).alias("z_mom"),
        ((pl.col("paccel") - pl.col("paccel").mean().over("date"))
         / pl.col("paccel").std().over("date")).alias("z_paccel"),
        ((pl.col("ln_mcap") - pl.col("ln_mcap").mean().over("date"))
         / pl.col("ln_mcap").std().over("date")).alias("z_mcap"),
        ((pl.col("p_turnover") - pl.col("p_turnover").mean().over("date"))
         / pl.col("p_turnover").std().over("date")).alias("z_pturn"),
    ])

    specs = {
        "accel单变量": ["z_accel"],
        "+价格一阶(动量)": ["z_accel", "z_mom"],
        "+价格二阶(加速)": ["z_accel", "z_mom", "z_paccel"],
        "全控制": ["z_accel", "z_mom", "z_paccel", "z_mcap", "z_pturn"],
    }
    reg = {}
    for spec_name, vars_ in specs.items():
        coefs = {v: [] for v in vars_}
        for day, g in df.group_by("date"):
            x = np.column_stack([np.ones(g.height), *[g[v].to_numpy() for v in vars_]])
            y = g["fwd"].to_numpy()
            beta = _lstsq(x, y)
            if beta is None:
                continue
            for i, v in enumerate(vars_):
                coefs[v].append(beta[i + 1])
        row = {"spec": spec_name, "n_days": len(next(iter(coefs.values())))}
        for v in vars_:
            ts = np.array(coefs[v])
            mean = float(ts.mean())
            se = nw_se(ts, lag=5)
            t = mean / se if se > 0 else float("nan")
            row[v] = {"coef_bp": round(mean * 1e4, 2), "t": round(t, 2)}
        reg[spec_name] = row
        line = f"  {spec_name:<18} "
        for v in vars_:
            c = row[v]
            line += f"  {v}={c['coef_bp']:+7.2f}bp(t={c['t']:+.2f})"
        print(line, flush=True)
    report["regression"] = reg

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "attention_accel.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/attention_accel.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
