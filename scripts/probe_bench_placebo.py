"""安慰剂：随机选 20 只，在「中位数基准」和「均值基准」下各报多少超额。

## 为什么必须做这个

预注册 §2 把估计量写成

    exc_i = fwd_i − median_j(fwd_j)

并且 §4 的 PASS 条件是 `net = mean_i(exc_i) − 19bp > 0`。这两件事放在一起
有一个隐含假设：**减掉中位数之后剩下的就是「选股能力」**。

但如果 `fwd` 的横截面右偏（均值 − 中位数 ≈ +200bp），那么任何**多头**组合
减中位数都会得到一个正的偏移：随机选 20 只，`mean(fwd_picks) ≈ 截面均值`，
减去截面中位数就得到「均值 − 中位数」。**这个偏移与选股能力无关**，它只是
偏度的读数。也就是说，中位数基准下的 `net > 0` 是**构造性成立**的，不是
发现。

本脚本用随机组合把这个偏移量出来：

- 每天从可交易池里**均匀随机**抽 20 只（无任何信号），重复 `R` 次；
- 同时算 `mean_picks − median(全截面)` 与 `mean_picks − mean(全截面)`；
- 报两个口径的均值、块重抽 se、t。

预期（若上面的推理成立）：

    中位数基准：+200bp 左右，t 很大，且与信号无关
    均值基准：  0 附近，t 在 ±2 内

若中位数基准的安慰剂读数不显著异于 0，则本推理被证伪，预注册的基准没问题。

用法：
    python scripts/probe_bench_placebo.py [--nmax=325] [--draws=20] [--seed=0]

⚠️ 本脚本给的是**点估计**，且每日取 `draws` 次抽签的平均——那是「随机组合的
期望」，不是「一只随机组合」。频率与 MDE 见 `probe_basis_calibration.py`
（200 只单只组合，中位数口径 ECON+ 率 100%、均值口径 0%，§9.33）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TABLE = "kline_daily_enriched"

HORIZON, LOOKBACK, TOP_N = 20, 20, 20
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)
DRAWS = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
              if a.startswith("--draws=")), 20)
SEED = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--seed=")), 0)

_spec = importlib.util.spec_from_file_location(
    "probe_paired_diff", ROOT / "scripts" / "probe_paired_diff.py")
_ppd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ppd)
se_block = _ppd.se_block


def sessions() -> list[str]:
    base = DATA / TABLE
    return sorted(c.name.split("=", 1)[1] for c in base.iterdir()
                  if c.is_dir() and c.name.startswith("date="))


def day_frame(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "turnover_rate", "amount"]).collect()


def main() -> None:
    cal = sessions()
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}

    import random
    random.seed(SEED)

    exc_med: list[float] = []
    exc_mean: list[float] = []
    gaps: list[float] = []
    used = 0

    for i in range(LOOKBACK, len(cal) - HORIZON):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("amount")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0)
                    & pl.col("amount").is_not_null())
        )
        if base.height < 200:
            continue
        base = base.with_columns((pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"))
        fwd = base["fwd"]
        med = float(fwd.median())
        mu = float(fwd.mean())
        # 浮点 NaN 不是缺失，但也要挡掉
        vals = [v for v in fwd.to_list() if v == v]
        if len(vals) < 200:
            continue
        used += 1
        gaps.append((mu - med) * 1e4)
        # 每日 R 次独立抽取，先按日汇总：等价于「一个随机组合」的 R 次重复，
        # 其均值就是「随机组合」的日读数，方差由日序列而不是抽签噪声决定。
        acc_m, acc_u = 0.0, 0.0
        for _ in range(DRAWS):
            picks = random.sample(vals, TOP_N)
            m = sum(picks) / TOP_N
            acc_m += m - med
            acc_u += m - mu
        exc_med.append(acc_m / DRAWS * 1e4)
        exc_mean.append(acc_u / DRAWS * 1e4)
        if NMAX and used >= NMAX:
            break

    def report(name: str, v: list[float]) -> None:
        mu = sum(v) / len(v)
        se = se_block(v)
        t = mu / se if se > 0 else float("nan")
        print(f"{name:<22}{mu:>9.1f}{se:>8.1f}{t:>8.2f}")

    print(f"{used} 个决策日；每日随机抽 {TOP_N} 只 × {DRAWS} 次；"
          f"seed={SEED}；H={HORIZON}")
    print(f"（安慰剂：没有任何信号，超额应当为 0）\n")
    print(f"{'口径':<22}{'均值bp':>9}{'se':>8}{'t':>8}")
    report("减截面中位数", exc_med)
    report("减截面均值", exc_mean)
    gap = sum(gaps) / len(gaps)
    print(f"\n截面均值 − 中位数：{gap:+.1f}bp/日"
          f"（这是中位数基准送给每个多头组合的固定偏移）")

    print("\n判定：中位数基准的 |t| 很大而均值基准的 |t| 小 ⇒ 预注册 §2 的基准"
          "\n中心选错了：它量的是横截面偏度，不是选股能力。"
          "\n两者都接近 0 ⇒ 上面的推理被证伪，基准没问题。")


if __name__ == "__main__":
    main()
