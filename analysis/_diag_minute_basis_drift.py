"""诊断: 盘中 `_calibrate_today` 是否在落盘数据里留下可见的基准误差。

机制假设 (待证伪): 快照一次拉完, 分钟逐只拉 (≈98ms/只, 全市场 ≈8.5min)。
第 i 只票的 ratio = raw(T_round_start) / adj(t_fetch_i), 于是整日 OHLC 被乘上
    raw(T_round_start) / raw(t_fetch_i) ≈ 1 - return(Δ_i),  Δ_i ∝ 序号 i
⇒ 落盘后的「当日最后一根 close / 原始日线 close」应随 universe 序号单调漂移;
⇒ 若收盘补拉已覆盖该日 (keep="last" 新行胜出), 误差应恒 ≈0。

判据 (不看代码看数据):
  A. |err| 的分布 —— 中位/ p95 / max;
  B. err 与「按 symbol 排序的序号」的秩相关 (scipy 不在 venv, 手写 argsort(argsort));
  C. 每只票的根数 / 首末根时间 (区分「缺头缺尾」与「因子错」)。

用法: backend/.venv/Scripts/python.exe analysis/_diag_minute_basis_drift.py [YYYY-MM-DD ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def spearman(a: list[float], b: list[float]) -> float:
    """秩相关 (scipy 不在 venv: 排名用 argsort(argsort) 手写, 平均秩处理并列)。"""
    def rank(xs: list[float]) -> list[float]:
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        out = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = rank(a), rank(b)
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * db) if da and db else float("nan")


def q(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


def one(day: str) -> None:
    mpath = DATA / "kline_minute" / f"date={day}" / "part.parquet"
    dpath = DATA / "kline_daily" / f"date={day}" / "part.parquet"
    print(f"\n=== {day} ===")
    if not mpath.exists():
        print("  无分钟分区")
        return
    m = pl.read_parquet(mpath)
    print(f"  分钟: {m.height} 行 / {m['symbol'].n_unique()} 只")
    if not dpath.exists():
        print("  无日线分区 (无法对账: 该日可能是当日未收盘或日线未落)")
        return
    d = pl.read_parquet(dpath, columns=["symbol", "close"]).rename({"close": "raw_close"})

    # 每只票的根数 / 首末根
    agg = m.group_by("symbol").agg(
        pl.len().alias("n"),
        pl.col("datetime").min().alias("t0"),
        pl.col("datetime").max().alias("t1"),
    )
    print("  根数分布: " + ", ".join(
        f"{k}={v}" for k, v in sorted(
            ((str(k), v) for k, v in agg["n"].value_counts().sort("n").iter_rows()), key=lambda x: x[0]
        )
    ))
    tail = agg.group_by("t1").agg(pl.len().alias("c")).sort("c", descending=True).head(5)
    print("  末根时间 top5: " + ", ".join(f"{str(r[0])[11:]}={r[1]}只" for r in tail.iter_rows()))

    last = agg.rename({"t1": "datetime"}).join(m, on=["symbol", "datetime"], how="left").select(
        ["symbol", "n", "datetime", "close"]
    ).rename({"close": "stored_last"})
    j = last.join(d, on="symbol", how="inner").with_columns(
        (pl.col("stored_last") / pl.col("raw_close") - 1.0).alias("err")
    )
    if j.is_empty():
        print("  分钟与日线 symbol 无交集")
        return
    errs = [float(x) for x in j["err"].to_list()]
    abses = [abs(x) for x in errs]
    print(f"  可比 {j.height} 只; err = 末根close/原始日线close - 1")
    print(f"    median={q(errs,0.5):+.6f}  median|e|={q(abses,0.5):.6f}  "
          f"p95|e|={q(abses,0.95):.6f}  max|e|={max(abses):.6f}")
    for thr in (1e-4, 1e-3, 1e-2):
        k = sum(1 for a in abses if a > thr)
        print(f"    |e|>{thr:g}: {k} 只 ({k/len(abses)*100:.1f}%)")

    # 序号 = 按 symbol 字符串排序的位置 (生产 _universe() 即 unique().sort())
    syms = sorted(j["symbol"].to_list())
    rank = {s: i for i, s in enumerate(syms)}
    idx = [float(rank[s]) for s in j["symbol"].to_list()]
    rho = spearman(idx, errs)
    print(f"  秩相关 rho(序号, err) = {rho:+.3f}   "
          f"(机制假设: 快照一次拉完 ⇒ 序号越靠后 drift 越大 ⇒ rho ≈ -1)")

    # 分桶看 |err| 是否随序号放大
    buckets = 5
    n = len(syms)
    rows = []
    for b in range(buckets):
        lo, hi = b * n // buckets, (b + 1) * n // buckets
        sub = [abs(e) for e, i in zip(errs, idx) if lo <= i < hi]
        rows.append(f"Q{b+1}({lo}-{hi}): median|e|={q(sub,0.5):.6f} max={max(sub):.6f}")
    print("  按序号五分位的 |err|:\n    " + "\n    ".join(rows))


def main() -> None:
    days = sys.argv[1:]
    if not days:
        days = sorted(p.name.split("=")[1] for p in (DATA / "kline_minute").glob("date=*"))[-3:]
    for day in days:
        one(day)


if __name__ == "__main__":
    main()
