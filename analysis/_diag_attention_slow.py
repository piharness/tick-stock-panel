"""方向：慢变量漂移（slow attention）—— 关注度的「长期趋势」是否独立于价格动量？

背景：快变量（关注度的一阶/二阶，日级差分）已证伪——它们是价格动量的延迟镜像。
本脚本测「慢变量」：换手率分位 p_turnover 在长窗口上的趋势（斜率）。

直觉：一只票可以「长期缓慢升温」（持续被资金关注）但「今天没异动」；反之亦然。
慢变量（月级）与价格动量的日级变化正交，理论上更可能独立。

构造（防前视，只用 t 日及之前信息）：
  p_turnover_t                    当日换手率分位（横截面 1=最高）
  slow_t = p_turnover_t - p_turnover_{t-SLOW_WINDOW}   （长窗口水平变化，代表长期趋势）
  即「当前换手率分位」相对「S 个交易日前的分位」抬升了多少。

证伪判据：slow 在控制 价格动量(mom) + 对数市值 + 换手水平 后是否仍有增量。
  若归零 → 慢关注也是价格趋势的影子；若独立 → 真正的新轴。

对照：同时跑 paccel（价格加速度，上一轮暴露的倒U形），确认它是不是标准动量反转。
  paccel 用「动量绝对值 |mom|」控制，检验倒U是否=「极端动量反转」。

数据：kline_daily_enriched + instruments。
输出：data/.scratch/attention_slow.json
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
SLOW_WINDOW = 60     # 慢变量窗口（交易日，约一个季度）
MOM_WINDOW = 20      # 价格动量窗口
ACCEL_WINDOW = 10    # 价格加速度窗口


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
    total = (m ** 2).sum() / n
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

    print("算慢变量/动量/价格加速度/收益…", flush=True)
    df = df.sort(["symbol", "date"])
    # 慢变量：长窗口换手率分位变化
    df = df.with_columns(
        (pl.col("p_turnover") - pl.col("p_turnover").shift(SLOW_WINDOW).over("symbol"))
        .alias("slow")
    )
    # 价格动量
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(MOM_WINDOW).over("symbol") - 1.0).alias("mom")
    )
    # 价格日收益 + 价格加速度（对照）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
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
    df = df.filter(
        pl.col("slow").is_not_null() & pl.col("mom").is_not_null()
        & pl.col("paccel").is_not_null() & pl.col("ln_mcap").is_not_null()
    )

    report = {"meta": {"slow_window": SLOW_WINDOW, "mom_window": MOM_WINDOW,
                       "horizon": H}, "n": int(df.height)}

    # ===== A. 分组：慢变量五等分 =====
    print(f"\n总样本 n={df.height:,}，慢变量五等分…", flush=True)
    df = df.with_columns(
        ((pl.col("slow").rank("ordinal").over("date") - 1) * 5
         // pl.col("slow").count().over("date")).clip(0, 4).alias("slow_bin")
    )
    grp = {}
    for j in range(5):
        seg = df.filter(pl.col("slow_bin") == j)
        daily = seg.group_by("date").agg(pl.col("fwd").mean().alias("m"))
        arr = daily["m"].to_numpy()
        mean = float(arr.mean())
        se = block_se(arr, block=2 * H)
        t = mean / se if se > 0 else float("nan")
        grp[f"slow_bin{j}"] = {"net_bp": round(mean * 1e4, 1), "se_bp": round(se * 1e4, 1),
                               "t": round(t, 2), "n": int(seg.height)}
        print(f"  slow_{j}(低→高)  n={seg.height:>8,}  H=20 {mean*1e4:+8.1f}bp (t={t:+.2f})", flush=True)
    report["slow_groups"] = grp

    # ===== B. 正交化：slow 是否独立于动量/市值/换手水平 =====
    print("\n正交化回归…", flush=True)
    df = df.with_columns([
        ((pl.col("slow") - pl.col("slow").mean().over("date"))
         / pl.col("slow").std().over("date")).alias("z_slow"),
        ((pl.col("mom") - pl.col("mom").mean().over("date"))
         / pl.col("mom").std().over("date")).alias("z_mom"),
        ((pl.col("paccel") - pl.col("paccel").mean().over("date"))
         / pl.col("paccel").std().over("date")).alias("z_paccel"),
        ((pl.col("ln_mcap") - pl.col("ln_mcap").mean().over("date"))
         / pl.col("ln_mcap").std().over("date")).alias("z_mcap"),
        ((pl.col("p_turnover") - pl.col("p_turnover").mean().over("date"))
         / pl.col("p_turnover").std().over("date")).alias("z_pturn"),
        pl.col("mom").abs().alias("abs_mom"),
    ])
    df = df.with_columns(
        ((pl.col("abs_mom") - pl.col("abs_mom").mean().over("date"))
         / pl.col("abs_mom").std().over("date")).alias("z_abs_mom")
    )

    specs = {
        "slow单变量": ["z_slow"],
        "+动量": ["z_slow", "z_mom"],
        "+市值+换手水平": ["z_slow", "z_mom", "z_mcap", "z_pturn"],
        "+动量绝对值(倒U检验)": ["z_slow", "z_mom", "z_abs_mom", "z_mcap", "z_pturn"],
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
        line = f"  {spec_name:<24} "
        for v in vars_:
            c = row[v]
            line += f"  {v}={c['coef_bp']:+7.2f}bp(t={c['t']:+.2f})"
        print(line, flush=True)
    report["regression"] = reg

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "attention_slow.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/attention_slow.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
