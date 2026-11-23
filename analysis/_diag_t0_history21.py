"""把「做T」的结论推到本地全部日K历史（2005-01-04 ~ 2026-11-18）。

## 这个脚本能做什么、不能做什么

日K 里没有日内路径，**无法**回测「先买后卖」的顺序。所以它不是回测，
是把做T 的收益拆成三个**可在日K上直接测量**的因子，看 2024-11~2026-11
这段样本是不是一个特殊时期：

  F1 空间  A_y   当日振幅中位 = median((high-low)/close)       —— 能赚多大
  F2 机会  hit_y 日内跌破昨收 d 的股票占比                      —— 触发多频繁
  F3 趋势  mom_y corr(隔夜跳空, 开盘->收盘)                     —— 反转还是延续
  F4 成本  c_y   当年「免佣零滑点」印花税地板                    —— 门槛多高

映射（写死，不许偷改）：
  gross_y ≈ gross_base × (A_y / A_base)
  A_base = 3.39%（在 5 分钟面板上用**同一公式**算出）
  gross_base 取 5 分钟实测毛收益（net + 12bp 成本还原）。

**这是外推，不是实测**。捕获率随振幅线性是这里最强的假设，方向偏向
高估做T（振幅大的年份往往也是趋势年，反转捕获率更低，F3 用来提示这一点）。
所以：若连外推都过不了成本线，结论就稳；若过了，只说明「那一年值得用
1 分钟数据去实测」，不构成做T可行的证据。

## 印花税（round-trip 合计，买卖两边）
  2005-01-24 ~ 2007-05-29   0.1% 双边 = 20bp
  2007-05-30 ~ 2008-04-23   0.3% 双边 = 60bp
  2008-04-24 ~ 2008-09-18   0.1% 双边 = 20bp
  2008-09-19 ~ 2023-08-27   0.1% 单边 = 10bp
  2023-08-28 ~              0.05% 单边 =  5bp
佣金按 2bp/边 = 4bp（地板，实际早年是 10~30bp）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_probe_t0"
OUT.mkdir(parents=True, exist_ok=True)

DIP = 0.01           # 与 5 分钟回测同一档 d=1%
A_BASE = 0.0339      # 5 分钟面板中位振幅（相对收盘）
GROSS_BASE = {"core": 8.1, "silent": 6.6, "background": 2.8, "active": 1.4}

STAMP = [
    ("2005-01-24", "2007-05-29", 20.0),
    ("2007-05-30", "2008-04-23", 60.0),
    ("2008-04-24", "2008-09-18", 20.0),
    ("2008-09-19", "2023-08-27", 10.0),
    ("2023-08-28", "2099-12-31", 5.0),
]


def stamp_bp(d: str) -> float:
    for a, b, v in STAMP:
        if a <= d <= b:
            return v
    return 20.0


def main() -> None:
    days = sorted(
        x.split("=", 1)[1] for x in os.listdir(DATA / "kline_daily_enriched")
        if x.startswith("date=")
    )
    print(f"[scan] {len(days)} 个交易日 {days[0]} .. {days[-1]}", flush=True)

    prev_close: dict[str, float] = {}
    rows = []
    t0 = time.time()
    for i, d in enumerate(days):
        p = DATA / "kline_daily_enriched" / f"date={d}" / "part.parquet"
        try:
            df = pl.read_parquet(p, columns=["symbol", "open", "high", "low", "close"])
        except Exception as e:  # noqa: BLE001
            print(f"  skip {d}: {e}")
            continue
        s = df["symbol"].to_list()
        o = df["open"].to_numpy()
        h = df["high"].to_numpy()
        lo = df["low"].to_numpy()
        c = df["close"].to_numpy()
        pc = np.array([prev_close.get(x, np.nan) for x in s])

        for x, v in zip(s, c):
            if np.isfinite(v) and v > 0:
                prev_close[x] = float(v)

        ok = (
            np.isfinite(pc) & (pc > 0) & np.isfinite(h) & np.isfinite(lo)
            & np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
            & (h >= lo) & (lo > 0)
        )
        if ok.sum() < 100:
            rows.append((d, int(ok.sum()), np.nan, np.nan, np.nan, np.nan))
            continue

        pc_, h_, lo_, o_, c_ = pc[ok], h[ok], lo[ok], o[ok], c[ok]
        a_rel = float(np.median((h_ - lo_) / c_))
        dip = float(np.mean(lo_ <= pc_ * (1 - DIP)))
        # 完整往返的机会（上界：跌到 d 又摸回昨收）
        rt = float(np.mean((lo_ <= pc_ * (1 - DIP)) & (h_ >= pc_)))
        gap = o_ / pc_ - 1.0
        i1 = c_ / o_ - 1.0
        sd = gap.std() * i1.std()
        mom = float(np.mean((gap - gap.mean()) * (i1 - i1.mean())) / sd) if sd > 0 else np.nan

        rows.append((d, int(ok.sum()), a_rel, dip, rt, mom))
        if i % 400 == 0:
            print(f"  {i}/{len(days)} {d} n={int(ok.sum())} "
                  f"A={a_rel:.4f} dip={dip:.3f} {time.time()-t0:.0f}s", flush=True)

    day = pl.DataFrame(
        rows,
        schema=["date", "n", "A", "dip", "rt", "mom"],
        orient="row",
    ).with_columns(pl.col("date").str.slice(0, 4).alias("y"))
    day.write_parquet(OUT / "hist_daily.parquet")

    # 首日无 prev_close ⇒ 写进来的是 NaN 而不是 null，必须先 drop_nans
    # 否则 median 被 NaN 污染，2005 整年会显示 nan。
    yr = (day.group_by("y").agg([
        pl.len().alias("days"),
        pl.col("n").median().alias("n_med"),
        pl.col("A").drop_nans().median().alias("A"),
        pl.col("dip").drop_nans().mean().alias("dip"),
        pl.col("rt").drop_nans().mean().alias("rt"),
        pl.col("mom").drop_nans().median().alias("mom"),
    ]).sort("y"))

    print("\n" + "=" * 100)
    print("表1 逐年（日K 全样本 2005-2026）")
    print(f"{'年':<6}{'日数':>5}{'股票中位':>9}{'振幅中位':>9}{'跌破1%占比':>11}"
          f"{'往返机会':>9}{'趋势corr':>9}{'印花税':>7}{'成本地板':>9}")
    for r in yr.iter_rows(named=True):
        st = stamp_bp(f"{r['y']}-06-01")
        print(f"{r['y']:<6}{r['days']:>5}{r['n_med']:>9.0f}{r['A']*100:>8.2f}%"
              f"{r['dip']*100:>10.1f}%{r['rt']*100:>8.1f}%{r['mom']:>+9.3f}"
              f"{st:>7.0f}{st+4:>9.0f}")

    # ---------- 外推 ----------
    print("\n" + "=" * 100)
    print("表2 按当年振幅线性外推的毛收益 vs 当年成本地板（免佣零滑点）")
    print("     毛收益 = 5分钟实测毛收益 × (当年振幅 / 3.39%)")
    print(f"{'年':<6}{'振幅':>7}{'倍数':>6}" + "".join(f"{k:>9}" for k in GROSS_BASE)
          + f"{'成本':>7}{'最好等级净':>11}")
    for r in yr.iter_rows(named=True):
        if not np.isfinite(r["A"]):
            continue
        k = r["A"] / A_BASE
        st = stamp_bp(f"{r['y']}-06-01") + 4
        g = {lv: v * k for lv, v in GROSS_BASE.items()}
        best = max(g.values()) - st
        line = "".join(f"{g[lv]:>+9.1f}" for lv in GROSS_BASE)
        flag = "  <= 过线" if best > 0 else ""
        print(f"{r['y']:<6}{r['A']*100:>6.2f}%{k:>6.2f}{line}{st:>7.0f}"
              f"{best:>+11.1f}{flag}")

    # ---------- 盈亏平衡振幅 ----------
    print("\n" + "=" * 100)
    print("表3 盈亏平衡振幅 A*（净=0 所需的当年中位振幅）")
    for lv, g in GROSS_BASE.items():
        for era, st in (("2023-08 后", 5), ("2008-09~2023-08", 10), ("2007-05~2008-04", 60)):
            cost = st + 4
            need = A_BASE * cost / g
            print(f"  {lv:<11} 成本{cost:>3.0f}bp  A*={need*100:>5.2f}%  "
                  f"（该时期实际中位振幅见上表）")
        print()

    day_a = day.filter(pl.col("A").is_finite())
    print(f"[全样本] 中位振幅 {day_a['A'].median()*100:.2f}%  "
          f"高于 3.39% 的年份数 {int((yr.filter(pl.col('A') > A_BASE).height))}/{yr.height}")
    print(f"wrote {OUT/'hist_daily.parquet'}")


if __name__ == "__main__":
    main()
