"""基准中心的校准检验：零能力组合在各中心下被判成 ECON+ 的频率。

## 为什么不能只报一个安慰剂的点估计

§9.32 的安慰剂报的是「随机 20 只 ×20 次」的**日平均**，得到减中位数 +194bp、
t=5.75，于是「§4 的 PASS 可以零能力达成」。这个构造有一个问题：**20 次抽签
的日平均不是一只组合**，它的抽样方差被 √20 压掉了，t 因此虚高；MDE 也随之
被压到 84，恰好过了 200 的门槛。一只真实的随机组合每 20 天只抽一次，它带着
完整的选股噪声。

所以本脚本问的不是「偏移有多大」（那是 §9.32 的算术，已确定是 195.7bp），
而是更该问的那个：

    **零能力的组合，被 §4 判成 ECON+ 的频率是多少？**

若中位数口径把这个频率从名义 5% 抬到 30%，那才是「§4 判的不是能力」的证据；
若它仍是 5%，那 §4 的判定没坏，坏的是**点估计的刻度**——两件事的处置不同。

## 三个中心，一个判定函数

`evaluate_series` 从 `probe_net_diff.py` 导入，与策略表**同一条代码路径**。
本脚本不重写判定，只换输入。

    median  全样本中位数。随机组合的期望 = 均值 ≠ 中位数 ⇒ 零假设不成立。
    mean    全样本等权均值。随机组合的期望 = 均值 ⇒ E[exc|无能力] = 0。
    mcap    流通市值加权均值。随机组合（等权，小盘偏多）相对它有规模溢价。

## 读法

- `偏移` = 当日「等权均值 − 该中心」的平均。它必须等于安慰剂均值：
  随机组合的期望就是这个常数，与信号无关。
- `ECON+ 率` = 区间条件单独成立的频率。能区分中心的首先是这个数。
- `PASS* 率` = ECON+ 且 MDE≤200（§4 的数值条件）。**Holm 无法对安慰剂族
  定义**（它们不是四个竞争假设），所以这一列只能读作「数值条件」。
- 名义值：单侧 5%，且还要过 19bp 成本，所以应**低于** 5%。

## 一个反直觉的读数

安慰剂的 `se_块` 比四个策略都小（15–58 vs 81–237bp），`MDE` 也比策略小
（37–144 vs 202–588）。原因不是安慰剂更"干净"，而是**它每天重抽**：
随机组合的日序列近似独立，而策略的 picks 高度持续（低换手股明天还是低换手），
有效样本小得多。所以 `MDE≤200` 这一条**拦不住零能力**，它只拦住了策略。

用法：
    python scripts/probe_basis_calibration.py [--nmax=325] [--draws=200]
                                              [--reps=400] [--seed=0]
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TABLE = "kline_daily_enriched"

HORIZON, LOOKBACK, TOP_N = 20, 20, 20
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)
DRAWS = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
              if a.startswith("--draws=")), 200)
REPS = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--reps=")), 400)
SEED = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--seed=")), 0)
BASES = ("median", "mean", "mcap")

SIGNALS = {
    "momentum_20": ("mom", True),
    "reversal_20": ("mom", False),
    "low_turnover": ("turn", False),
    "high_turnover": ("turn", True),
}


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 判定与策略表共用同一实现；换中心不许换判定。
pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
evaluate_series = pnd.evaluate_series
COST_BP = pnd.COST_BP
#: 中心的定义只有一处（`probe_window.center_of`），本脚本不重写它。
pwin = _load("probe_window", "scripts/probe_window.py")
center_of = pwin.center_of
#: 校准也读原始行情，因此也要过闸门：打印一个确认段的率同样是「读了一次」。
GATE = _load("decision_gate", "scripts/decision_gate.py")
INCLUDE_CONFIRM = "--include-confirmation" in sys.argv


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
        ["symbol", "close", "raw_close", "turnover_rate"]).collect()


def shares() -> pl.DataFrame:
    df = pl.read_parquet(DATA / "instruments" / "instruments.parquet")
    return (df.select(["symbol", "float_shares"])
            .filter(pl.col("float_shares") > 0)
            .with_columns(pl.col("float_shares").cast(pl.Float64)))


def main() -> None:
    cal = sessions()
    if not INCLUDE_CONFIRM:
        cal = GATE.cap_calendar(cal)
    else:
        GATE.require_exploration(GATE.decision_days(cal),
                                 "probe_basis_calibration", sys.argv, allow=True)
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    sh = shares()
    cached = {b: json.loads(p.read_text(encoding="utf-8"))
              for b, p in pnd._CACHE.items() if p.exists()}

    rng = np.random.default_rng(SEED)
    keys = list(SIGNALS) + [f"placebo#{k:03d}" for k in range(DRAWS)]
    ser: dict[str, dict[str, list[float]]] = {k: {b: [] for b in BASES} for k in keys}
    days_used: list[str] = []
    offsets = {b: [] for b in BASES}

    for i in range(LOOKBACK, len(cal) - HORIZON):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("raw_close"),
                                    pl.col("turnover_rate").alias("turn")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0))
        )
        if base.height < 200:
            continue
        base = (base.join(sh, on="symbol", how="left")
                .with_columns((pl.col("raw_close") * pl.col("float_shares"))
                              .fill_null(0.0).alias("mv")))
        base = base.with_columns(
            (pl.col("c1") / pl.col("c0") - 1.0).alias("mom"),
            (pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"),
        )
        fwd = base["fwd"].to_numpy()
        cen = {k: center_of(base, k) for k in BASES}
        for k in BASES:
            offsets[k].append((cen["mean"] - cen[k]) * 1e4)

        # 策略：与 probe_window 同一套排序，逐日读数取 picks 的均值。
        for name, (col, desc) in SIGNALS.items():
            picked = base.drop_nulls(col).sort(col, descending=desc).head(TOP_N)
            if picked.height != TOP_N:
                continue
            m = float(picked["fwd"].mean())
            for k in BASES:
                ser[name][k].append((m - cen[k]) * 1e4)

        #: 每只安慰剂每日**只抽一次**：这才是「一只组合」，不是「20 只的平均」。
        idx = rng.integers(0, base.height, size=(DRAWS, TOP_N))
        draw = fwd[idx].mean(axis=1)
        for j in range(DRAWS):
            for k in BASES:
                ser[f"placebo#{j:03d}"][k].append(float(draw[j] - cen[k]) * 1e4)
        days_used.append(t)
        if NMAX and len(days_used) >= NMAX:
            break

    n = len(days_used)
    print(f"{n} 个决策日（{days_used[0]} … {days_used[-1]}）；"
          f"{DRAWS} 只随机组合（每只每日抽一次，不是日平均）；"
          f"reps={REPS}；seed={SEED}")
    print("§4 的核心（均值、40 天块重抽 5% 分位、MDE、p、判定）由 "
          "probe_net_diff.evaluate_series 提供，与策略表同一条代码路径。")
    print(f"成本 {COST_BP:.0f}bp/往返；名义单侧 5%。\n")

    print("零、复现校验：本脚本的策略序列 vs 缓存（逐日最大偏差，bp）")
    for name in SIGNALS:
        parts = []
        for k in BASES:
            if k not in cached:
                continue
            got = cached[k].get(name, {})
            d = [x for x in days_used if x in got]
            if len(d) < n:
                parts.append(f"{k}:缺 {n - len(d)} 天")
                continue
            mx = max(abs(ser[name][k][j] - sum(got[x]) / len(got[x]) * 1e4)
                     for j, x in enumerate(days_used))
            parts.append(f"{k}:{mx:.4f}")
        print(f"  {name:<15}" + "  ".join(parts))

    print("\n一、四个策略 × 三个中心（§4 判定）")
    print(f"{'策略':<15}{'中心':<8}{'均值':>8}{'se_块':>8}{'MDE':>8}"
          f"{'净LCB':>9}{'p':>8}{'Holm':>7}  判定")
    res = {k: {name: evaluate_series(ser[name][k], reps=REPS, seed=SEED)
               for name in SIGNALS} for k in BASES}
    #: Holm 的族是**同一中心下的四个策略**（策略竞争同一份样本）；
    #: 不是同一策略的三个中心（那是同一假设的三种测法，不该进校正）。
    adj = {k: {name: a for name, a in zip(
        SIGNALS, pnd.holm([res[k][n]["p"] for n in SIGNALS]))} for k in BASES}
    for name in SIGNALS:
        for j, k in enumerate(BASES):
            r = res[k][name]
            print(f"{name if j == 0 else '':<15}{k:<8}{r['mu']:>8.1f}"
                  f"{r['sd_b']:>8.1f}{r['mde']:>8.1f}{r['net_lo']:>9.1f}"
                  f"{r['p']:>8.3f}{adj[k][name]:>7.3f}  {r['verdict']}")
        print()

    print("二、安慰剂校准：零能力组合被判成 ECON+ 的频率")
    hdr = (f"{'中心':<8}{'偏移bp':>9}{'组合均值bp':>11}{'跨组合sd':>10}"
           f"{'ECON+率':>9}{'名义':>6}{'MDE中位':>9}{'PASS*率':>8}")
    print(hdr)
    calib = {}
    for k in BASES:
        res = [evaluate_series(ser[f"placebo#{j:03d}"][k], reps=REPS, seed=SEED)
               for j in range(DRAWS)]
        mus = [r["mu"] for r in res]
        econ = sum(r["verdict"] == "ECON+" for r in res) / DRAWS
        ok = sum(r["verdict"] == "ECON+" and r["mde"] <= 200 for r in res) / DRAWS
        mde = sorted(r["mde"] for r in res)[DRAWS // 2]
        calib[k] = {"econ": econ, "pass": ok, "mu": sum(mus) / DRAWS, "mde": mde}
        print(f"{k:<8}{sum(offsets[k]) / n:>+9.1f}{calib[k]['mu']:>11.1f}"
              f"{float(np.std(mus, ddof=1)):>10.1f}{econ:>9.1%}{'5%':>6}"
              f"{mde:>9.0f}{ok:>8.1%}")
    print("\n偏移 = 当日「等权均值 − 该中心」的平均；随机组合的期望就是这个数，"
          "与信号无关。")
    print("ECON+ 率与偏移同向；PASS* 是 ECON+ 且 MDE≤200（Holm 对安慰剂族无定义）。")

    print("\n三、判定")
    nom, sx = 0.05, calib["median"]
    my = calib["mean"]
    if sx["econ"] <= nom * 1.6 and my["econ"] <= nom * 1.6:
        print(f"两个中心都接近名义值（median {sx['econ']:.1%} / "
              f"mean {my['econ']:.1%}）⇒ §4 的判定没被中心带偏，"
              "错的只是点估计刻度。")
        print("处置：换中心报数，不动阈值。")
    else:
        print(f"中位数口径把零能力的 ECON+ 率抬到 {sx['econ']:.1%}"
              f"（名义 {nom:.0%}；均值口径 {my['econ']:.1%}）"
              " ⇒ §4 在中位数口径下判的不是能力。")
    print(f"\nMDE 中位：median {sx['mde']:.0f} / mean {my['mde']:.0f} / "
          f"mcap {calib['mcap']['mde']:.0f}；策略的 MDE 是 202–588。"
          "\n零能力的 MDE 比策略**更小**——它每天重抽，序列近似独立；"
          "策略的 picks 持续，有效样本小。")
    print("所以 MDE≤200 这一条拦不住零能力，它只拦住了策略。"
          "\n§4 在中位数口径下实际检验的是「你这条多头序列够不够平稳」，"
          "不是「你有没有选股能力」。")


if __name__ == "__main__":
    main()
