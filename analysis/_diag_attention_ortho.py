"""H1：对「拥挤度调节」做正交化 —— 是独立机制，还是动量/市值的镜像？

H2 发现（t 高达 12~18）：
  active→core / core→active 迁移里，「低拥挤」组显著跑赢「高拥挤」组。
  但可疑：高拥挤的 active→core 票，换手率分位 0.77+，是「放量冲顶」形态。

本脚本做致命检验：在横截面上，控制 动量/市值/换手率水平/反转 后，
crowding 对未来收益是否仍有增量预测力？

方法：Fama-MacBeth 逐日横截面回归。
  因变量 y = H=20 净收益（事件日之后）
  核心 X = crowding（事件前20日换手率分位均值）
  控制 Z = [事件前20日动量, 对数市值, 事件前换手率分位(流动性水平)]
  每日跑 OLS，系数取时序均值，标准误用 Newey-West 或 block bootstrap。

判据：crowding 系数在「单变量」和「全控制」下的 t 值衰减。
  若全控制后 |t|<2，则「共识调节」是动量/市值的镜像，非独立机制。

数据：kline_daily_enriched + instruments。
输出：data/.scratch/attention_consensus_ortho.json
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

H = 20
STAMP_TAX = 0.0010
COMMISSION = 0.00025
MIN_AMOUNT = 20_000_000
CROWD_WINDOW = 20
MOM_WINDOW = 20

# 要检验的迁移（升格 + 降格，涉及 core 的）
TARGET_TRANSITIONS = [
    ("active→core", "active", "core"),
    ("core→active", "core", "active"),
]


def _lstsq(x: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    """最小二乘，返回系数向量；失败返回 None。x 形状 (n, k)，已含截距列。"""
    if x.shape[0] < x.shape[1] + 10:
        return None
    try:
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        return beta
    except np.linalg.LinAlgError:
        return None


def nw_se(ts: np.ndarray, lag: int = 5) -> float:
    """Newey-West 标准误（针对时序系数序列）。"""
    n = len(ts)
    if n == 0:
        return float("nan")
    m = ts - ts.mean()
    gamma0 = (m ** 2).sum() / n
    total = gamma0
    for l in range(1, min(lag, n - 1) + 1):
        gamma_l = (m[l:] * m[:-l]).sum() / n
        total += 2 * (1 - l / (lag + 1)) * gamma_l
    var = total / n
    return float(np.sqrt(var))


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

    print("逐日算等级 + 分位…", flush=True)
    def assign(d: pl.DataFrame) -> pl.DataFrame:
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
        return d.with_columns([level.alias("level"), t.alias("p_turnover"),
                               a.alias("p_amount")])

    df = df.group_by("date", maintain_order=True).map_groups(assign)

    print("算迁移/拥挤度/动量/市值/收益…", flush=True)
    df = df.with_columns(pl.col("level").shift(1).over("symbol").alias("prev_level"))
    df = df.with_columns(
        pl.col("p_turnover").shift(1).over("symbol")
        .rolling_mean(window_size=CROWD_WINDOW, min_samples=max(5, CROWD_WINDOW // 2))
        .over("symbol").alias("crowding")
    )
    # 动量 = 事件前 20 日收益（close T / close T-20 - 1）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(MOM_WINDOW).over("symbol") - 1.0)
        .alias("mom")
    )
    df = df.with_columns(
        (pl.col("close") * pl.col("float_shares")).log().alias("ln_mcap")
    )
    # 未来收益（T+1 开盘买，持有 H 日，扣成本）
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
    df = df.filter(tradable & pl.col("prev_level").is_not_null())

    report = {}
    for name, prev, curr in TARGET_TRANSITIONS:
        ev = df.filter((pl.col("prev_level") == prev) & (pl.col("level") == curr))
        ev = ev.filter(
            pl.col("crowding").is_not_null() & pl.col("mom").is_not_null()
            & pl.col("ln_mcap").is_not_null() & pl.col("fwd").is_not_null()
        )
        if ev.height < 1000:
            print(f"{name} 事件太少 n={ev.height}", flush=True)
            continue

        # 标准化自变量（横截面 z-score），便于比较系数量级
        ev = ev.with_columns([
            ((pl.col("crowding") - pl.col("crowding").mean().over("date"))
             / pl.col("crowding").std().over("date")).alias("z_crowd"),
            ((pl.col("mom") - pl.col("mom").mean().over("date"))
             / pl.col("mom").std().over("date")).alias("z_mom"),
            ((pl.col("ln_mcap") - pl.col("ln_mcap").mean().over("date"))
             / pl.col("ln_mcap").std().over("date")).alias("z_mcap"),
            ((pl.col("p_turnover") - pl.col("p_turnover").mean().over("date"))
             / pl.col("p_turnover").std().over("date")).alias("z_pturn"),
        ])

        print(f"\n=== {name}（n={ev.height:,}）===", flush=True)
        # 两个规格：单变量（crowding 对 fwd），全控制（crowding + mom + mcap + pturn）
        specs = {
            "单变量": ["z_crowd"],
            "控制动量": ["z_crowd", "z_mom"],
            "全控制": ["z_crowd", "z_mom", "z_mcap", "z_pturn"],
        }
        out = {"n": int(ev.height)}
        for spec_name, vars_ in specs.items():
            coefs = {v: [] for v in vars_}
            for day, g in ev.group_by("date"):
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
            out[spec_name] = row
            line = f"  {spec_name:<12} "
            for v in vars_:
                c = row[v]
                line += f"  {v}={c['coef_bp']:+7.2f}bp(t={c['t']:+.2f})"
            print(line, flush=True)

        report[name] = out

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "attention_consensus_ortho.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/attention_consensus_ortho.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
