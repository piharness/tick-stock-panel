"""过路费率的量纲校准：温和 ramp 的「0~3%」是不是 Θ≈1 的投影？

预注册（先写死，再看数；本脚本只做量纲校准，不做收益检验）
----------------------------------------------------------
定义
  S(p)  = Σ_{s} TO_s · N(p; vwap_s, ivd_s²·vwap_s²)      存量筹码场（高斯核，按当日换手为权重）
  band  = [raw_close_{t-5}, raw_close_t]                  一次上穿的走廊
  D0    = ∫_band S_{t0}(p) dp                             穿越起点时，走廊里的存量（占流通盘）
  Π     = Σ_{s=t0+1..t} TO_s · frac_s(band)               穿越期内真正落在走廊里的成交（占流通盘）
  Θ     = Π / D0                                          「这一层被换手了几遍」

H1（付清叙事）：5 日涨幅 0~3% 的样本，中位 Θ ∈ [0.5, 2.0]。
   —— 若成立，现存的「温和 ramp」阈值不是拟合出来的，而是 Θ≈1 的一个粗投影。
杀死的条件
  Θ 中位 < 0.3  ⇒ 温和 ramp 其实也是欠账，「付清」叙事与量纲不符；
  Θ 中位 > 3    ⇒ 走廊里的存量太小，Θ 退化为换手的单调变换，无独立信息；
  |Spearman(Θ, cum5)| > 0.5 ⇒ Θ 只是位移的重命名。

口径
  - 只做上穿（P1 > P0）；只用 raw_close（与 5min vwap 同为未复权价）。
  - 抽样 500 只（按 symbol 抽，保留完整时序），OOS 段 2024-11~2026-11。
  - 剔除 TO 缺失/为 0、ivd 越界（>0.15）、当日 |ret1|>9.5%（封板使核宽机械趋零）。
  - D0 用 t-19..t-5 的核（穿越前已经存在的筹码），Π 用 t-4..t 的核（穿越期成交）。
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np
import polars as pl

_SQRT2 = np.sqrt(2.0)


def _erf(x: np.ndarray) -> np.ndarray:
    """Abramowitz-Stegun 7.1.26，|误差| < 1.5e-7，够做量纲校准。"""
    s = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return s * y


def ndtr(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(np.asarray(x) / _SQRT2))

ROOT = Path(__file__).resolve().parent.parent
ENT = ROOT / "data" / ".scratch" / "chip_entropy_local" / "part.parquet"
KL = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "chip_toll.json"


def load(sample: int, seed: int) -> pl.DataFrame:
    ent = pl.read_parquet(ENT, columns=["symbol", "date", "vwap", "ivd_lo"])
    ent = ent.filter((pl.col("ivd_lo") > 0) & (pl.col("ivd_lo") < 0.15))
    syms = sorted(ent["symbol"].unique().to_list())
    random.seed(seed)
    pick = set(random.sample(syms, min(sample, len(syms))))
    ent = ent.filter(pl.col("symbol").is_in(list(pick)))

    files = [str(p) for p in sorted(KL.glob("date=*/part.parquet"))]
    px = pl.scan_parquet(files, extra_columns="ignore").select(
        ["symbol", "date", "raw_close", "turnover_rate"]
    ).filter(pl.col("symbol").is_in(list(pick))).collect()
    d = ent.join(px, on=["symbol", "date"], how="inner").sort(["symbol", "date"])
    d = d.with_columns(
        (pl.col("raw_close") / pl.col("raw_close").shift(1).over("symbol") - 1).alias("ret1")
    ).filter(
        (pl.col("turnover_rate") > 0) & (pl.col("turnover_rate") < 60)
        & (pl.col("ret1").abs() < 0.095)
    )
    return d.sort(["symbol", "date"])


def calibrate(d: pl.DataFrame) -> pl.DataFrame:
    rows = []
    L = 20  # 需要 lag 0..19
    for sym, g in d.group_by("symbol", maintain_order=True):
        n = g.height
        if n < 80:
            continue
        close = g["raw_close"].to_numpy()
        to = g["turnover_rate"].to_numpy() / 100.0
        mu = g["vwap"].to_numpy()
        sd = g["ivd_lo"].to_numpy() * mu
        ok = np.isfinite(sd) & (sd > 0)
        if ok.mean() < 0.9:
            continue
        for t in range(L, n):
            p0, p1 = close[t - 5], close[t]
            if not (p1 > p0):
                continue
            cum5 = p1 / p0 - 1
            # D0: 穿越前已存在的筹码（lag 5..19）
            sl = slice(t - 19, t - 4)
            d0 = np.sum(to[sl] * (ndtr((p1 - mu[sl]) / sd[sl]) - ndtr((p0 - mu[sl]) / sd[sl])))
            # Π: 穿越期落在走廊里的成交（lag 0..4）
            sl2 = slice(t - 4, t + 1)
            pi = np.sum(to[sl2] * (ndtr((p1 - mu[sl2]) / sd[sl2]) - ndtr((p0 - mu[sl2]) / sd[sl2])))
            if d0 <= 1e-6:
                continue
            # 场心 μ 与缺口 g：15 日换手加权 vwap（卷一口径），u 为 5 日对数漂移/日
            w15 = to[t - 14:t + 1]
            mu_c = float(np.sum(w15 * mu[t - 14:t + 1]) / max(np.sum(w15), 1e-12))
            g = float(np.log(p1 / mu_c))
            u = float(np.log(p1 / p0) / 5.0)
            to15 = float(np.mean(to[t - 14:t + 1]))
            rows.append((sym, float(cum5), float(d0), float(pi), float(pi / d0),
                         float(to[sl].sum()), g, u, to15,
                         float(to15 * g / u) if u > 0 else float("nan")))
    return pl.DataFrame(rows, schema=["symbol", "cum5", "D0", "PI", "THETA", "to_pre",
                                      "G", "U", "TO15", "SIGMA"], orient="row")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20261126)
    a = ap.parse_args()
    d = load(a.sample, a.seed)
    print(f"样本 {d.height:,} 股票-日，{d['symbol'].n_unique()} 只，"
          f"{d['date'].min()} → {d['date'].max()}")
    r = calibrate(d)
    print(f"可算 {r.height:,} 个上穿窗口")

    r = r.filter(pl.col("THETA").is_finite() & (pl.col("THETA") < 50))
    buckets = [(0.0, 0.01), (0.01, 0.03), (0.03, 0.06), (0.06, 0.10),
               (0.10, 0.20), (0.20, 0.50)]
    print("\n cum5 区间         n       Θ中位    Θ_p25    Θ_p75   D0中位%  Π中位%")
    for lo, hi in buckets:
        s = r.filter((pl.col("cum5") >= lo) & (pl.col("cum5") < hi))
        if s.height < 50:
            continue
        print(f" {lo:5.0%}–{hi:<5.0%} {s.height:8,}  {s['THETA'].median():7.2f}"
              f"  {s['THETA'].quantile(0.25):7.2f}  {s['THETA'].quantile(0.75):7.2f}"
              f"  {s['D0'].median()*100:7.2f} {s['PI'].median()*100:7.2f}")

    g = r.filter((pl.col("cum5") >= 0) & (pl.col("cum5") < 0.03))
    print(f"\n【预注册格】cum5 0–3%：n={g.height:,}  Θ中位={g['THETA'].median():.2f}"
          f"  p25={g['THETA'].quantile(0.25):.2f}  p75={g['THETA'].quantile(0.75):.2f}")

    # Spearman（无 scipy.stats 依赖）
    x = r["cum5"].to_numpy(); y = r["THETA"].to_numpy()
    rx = x.argsort().argsort().astype(float); ry = y.argsort().argsort().astype(float)
    rho = float(np.corrcoef(rx, ry)[0, 1])
    print(f"Spearman(Θ, cum5) = {rho:.3f}")

    # Θ 与「拉伸度」Σ = TO·g/u 是不是同一个数
    s = r.filter(pl.col("SIGMA").is_finite() & (pl.col("SIGMA").abs() < 20))
    x = s["THETA"].to_numpy(); y = s["SIGMA"].to_numpy()
    rx = x.argsort().argsort().astype(float); ry = y.argsort().argsort().astype(float)
    print(f"Spearman(Θ, Σ) = {np.corrcoef(rx, ry)[0, 1]:+.3f}   n={s.height:,}")
    print(f"Σ 分位: " + "  ".join(f"p{int(p*100)}={float(s['SIGMA'].quantile(p)):.2f}"
                                 for p in [.1, .25, .5, .75, .9]))
    for c in ["G", "U", "TO15"]:
        x = s[c].to_numpy(); y = s["THETA"].to_numpy()
        rx = x.argsort().argsort().astype(float); ry = y.argsort().argsort().astype(float)
        print(f"Spearman(Θ, {c}) = {np.corrcoef(rx, ry)[0, 1]:+.3f}")

    OUT.write_text(r.write_json(), encoding="utf-8")
    print(f"写出 {OUT}")


if __name__ == "__main__":
    main()
