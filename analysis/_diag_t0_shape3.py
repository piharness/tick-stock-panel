"""收尾核验：唯一越过成本线的格是真信号还是尾部彩票；多变量相对单变量有无增量

## 要回答的两个问题

Q1 `_diag_t0_shape2.py` 的 A 表里唯一 **净为正** 的格是
   「T-1 振幅 Q5 + 负T + d=0.03」：per_day 毛 +14.7bp / t=1.38
   它是 4 个 d x 5 个分位的 20 格里挑出来的最大值。必须回答：
   - 分年/分半年是否同号（C3 式稳定性）
   - 是不是靠少数几天（按日 trimmed mean、去掉最大 1% 日）
   - 胜率与均值是否同号（赢在概率还是赢在右尾）
   若三项任一不成立 ⇒ 记为 `NOT_ESTABLISHED`，不作为策略依据。

Q2 「变量太少」的反驳是否成立：多变量 D10 与单变量 Q5 必须在**同一测试窗口**比。
   若单变量已到 11bp、多变量 10.8bp ⇒ 增量 ≈ 0 ⇒ 瓶颈不是变量数，是成本。

## 口径
沿用 `_diag_t0_shape.py`：r 已扣 12bp；per_day = 未触发计 0；t = 按日 bootstrap。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
B = 1000
COST_BP = 12.0


def day_g(df: pl.DataFrame, col: str) -> pl.DataFrame:
    return (df.filter(pl.col(col).is_finite())
              .group_by("date").agg([pl.col(col).sum().alias("s"), pl.len().alias("n")])
              .sort("date"))


def stat(g: pl.DataFrame, seed: int = 11) -> dict:
    s, n = g["s"].to_numpy(), g["n"].to_numpy()
    v = s.sum() / n.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(s), (B, len(s)))
    bs = s[idx].sum(1) / n[idx].sum(1)
    return {"bp": float(v * 1e4), "t": float(v / bs.std()), "days": int(len(s)),
            "n": int(n.sum())}


def main() -> None:
    sh = pl.read_parquet(OUT / "shape.parquet")
    sh = sh.sort(["symbol", "date"]).with_columns(
        pl.col("ampl_o").shift(1).over("symbol").alias("L_ampl"))
    tr = pl.read_parquet(OUT / "trades.parquet",
                         columns=["symbol", "date", "d", "r1", "r2", "f1", "f2"])
    df = sh.join(tr, on=["symbol", "date"], how="inner").sort(["symbol", "date"])
    df = df.with_columns([
        pl.when(pl.col("f1").fill_null(False)).then(pl.col("r1")).otherwise(0.0).alias("p1"),
        pl.when(pl.col("f2").fill_null(False)).then(pl.col("r2")).otherwise(0.0).alias("p2"),
    ])
    out: dict = {"cost_bp": COST_BP}

    # ---------------- Q1 唯一正格 ---------------- #
    print("=" * 92)
    print("[Q1] 唯一净为正的格：T-1 振幅 Q5 + 负T + d=0.03")
    s = df.filter(pl.col("d") == 0.03, pl.col("L_ampl").is_finite())
    s = s.with_columns(pl.col("L_ampl").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                                             allow_duplicates=True).over("date").alias("aq"))
    cell = s.filter(pl.col("aq") == "Q5")
    g = day_g(cell, "p2")
    st = stat(g)
    print(f"  全样本 per_day 毛 {st['bp'] + COST_BP:+.1f}bp 净 {st['bp']:+.1f}bp "
          f"t={st['t']:+.2f}  days={st['days']} n={st['n']}")
    rets = cell.filter(pl.col("f2").fill_null(False))["r2"].drop_nulls().to_numpy()
    print(f"  触发笔数 {len(rets):,}  fill={cell['f2'].mean():.3f}  "
          f"胜率 {(rets > 0).mean():.3f}  中位 {np.median(rets) * 1e4:+.1f}bp  "
          f"均值 {rets.mean() * 1e4:+.1f}bp")
    print(f"  均值-中位差 {(rets.mean() - np.median(rets)) * 1e4:+.1f}bp  "
          f"p1 {np.percentile(rets, 1) * 1e4:+.0f}  p99 {np.percentile(rets, 99) * 1e4:+.0f}")

    # 去尾部
    q = cell.filter(pl.col("f2").fill_null(False))
    lo, hi = np.percentile(q["r2"].to_numpy(), [1, 99])
    w = cell.with_columns(
        pl.when(pl.col("f2").is_null() | ~pl.col("f2")).then(0.0)
        .otherwise(pl.col("r2").clip(lo, hi)).alias("w2"))
    st2 = stat(day_g(w, "w2"))
    print(f"  1/99 缩尾 per_day 毛 {st2['bp'] + COST_BP:+.1f}bp  t={st2['t']:+.2f}")

    # 分年 / 分半年
    print(f"  {'区间':<18}{'毛bp':>9}{'t':>7}{'days':>7}{'fill':>7}")
    parts = {}
    cell = cell.with_columns(pl.col("date").str.slice(0, 4).alias("yr"))
    years = sorted(cell["yr"].unique().to_list())
    halves = [("前半", None, 0.5), ("后半", 0.5, None)]
    ds = sorted(cell["date"].unique().to_list())
    for nm, a, b in halves:
        i0 = 0 if a is None else int(len(ds) * a)
        i1 = len(ds) if b is None else int(len(ds) * b)
        sub = cell.filter(pl.col("date").is_in(ds[i0:i1]))
        stx = stat(day_g(sub, "p2"))
        f = float(sub["f2"].mean())
        print(f"  {nm:<18}{stx['bp'] + COST_BP:>+9.1f}{stx['t']:>7.2f}"
              f"{stx['days']:>7}{f:>7.3f}")
        parts[nm] = {"gross": round(stx["bp"] + COST_BP, 1), "t": round(stx["t"], 2),
                     "fill": round(f, 3)}
    for y in years:
        sub = cell.filter(pl.col("yr") == y)
        if sub.height < 2000:
            continue
        stx = stat(day_g(sub, "p2"))
        f = float(sub["f2"].mean())
        print(f"  {y:<18}{stx['bp'] + COST_BP:>+9.1f}{stx['t']:>7.2f}"
              f"{stx['days']:>7}{f:>7.3f}")
        parts[y] = {"gross": round(stx["bp"] + COST_BP, 1), "t": round(stx["t"], 2),
                    "fill": round(f, 3)}
    pos = sum(1 for k, v in parts.items() if k in ("前半", "后半", *years)
              and v["gross"] > 0)
    print(f"  同号为正的区间 {pos}/{len(parts)}   ⇒ "
          f"{'通过稳定性' if pos == len(parts) else '不通过稳定性'}")
    out["q1"] = {"full": {k: round(v, 2) for k, v in st.items()},
                 "hit": round(float((rets > 0).mean()), 3),
                 "median_bp": round(float(np.median(rets) * 1e4), 1),
                 "mean_bp": round(float(rets.mean() * 1e4), 1),
                 "winsor_bp": round(st2["bp"] + COST_BP, 1),
                 "parts": parts, "n_pos_parts": pos, "n_parts": len(parts)}

    # ---------------- Q2 多变量 vs 单变量同窗口 ---------------- #
    print("\n" + "=" * 92)
    print("[Q2] 同一测试窗口（后 60% 交易日）：单变量 Q5 vs 多变量 D10")
    all_days = sorted(df["date"].unique().to_list())
    test_days = all_days[int(len(all_days) * 0.4):]
    t1 = df.filter(pl.col("d") == 0.01, pl.col("date").is_in(test_days),
                   pl.col("L_ampl").is_finite())
    t1 = t1.with_columns(pl.col("L_ampl").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                                               allow_duplicates=True).over("date").alias("aq"))
    print(f"    {'方案':<26}{'正T毛/d':>10}{'t':>7}{'份额':>8}")
    for q in ("Q1", "Q3", "Q5"):
        sub = t1.filter(pl.col("aq") == q)
        stx = stat(day_g(sub, "p1"))
        print(f"    {'单变量 振幅 ' + q:<26}{stx['bp'] + COST_BP:>+10.1f}"
              f"{stx['t']:>7.2f}{sub.height / t1.height:>8.1%}")
        if q == "Q5":
            out["q2_single"] = {"gross": round(stx["bp"] + COST_BP, 1),
                                "t": round(stx["t"], 2)}
    print(f"    {'多变量 D10（上一脚本）':<26}{10.8:>+10.1f}{-0.19:>7.2f}{0.1:>8.1%}")
    print("    ⇒ 增量 = 多变量 D10 − 单变量 Q5")

    p = ROOT / "analysis" / "_diag_t0_shape3.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
