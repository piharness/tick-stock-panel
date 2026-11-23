"""独立验证 backfill_bs_minute 已下载批次（不信任 _state.jsonl 的 ok）。

_state.jsonl 的 ok 只表示「返回了非空行」，覆盖度从未被验证过。本脚本用
**独立数据源**（backfill_20261117/stock_gap_daily.parquet 原始价日线）逐只对账：

  1. 交易日覆盖：日线有成交的日期 vs f5 里出现的日期 → 缺失日
  2. 收盘价同源：每日最后一根 5min close vs 日线 raw close
  3. 成交量单位：每日 sum(volume_raw)/100 vs 日线 volume（手）
  4. 每日根数：应为 48（全交易日），统计偏离

用法: python analysis/_verify_bs_minute_batch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
F5 = DATA / "backfill_bs_minute" / "f5"
BS_START = "2020-01-02"


def main() -> None:
    daily = pl.read_parquet(
        DATA / "backfill_20261117/stock_gap_daily.parquet",
        columns=["symbol", "date", "open", "high", "low", "close", "volume"],
    ).filter(pl.col("date") >= pl.lit(BS_START).str.to_date())

    files = sorted(F5.glob("*.parquet"))
    print(f"f5 文件 {len(files)} 只 / 日线 {daily.height} 行 "
          f"/ {daily['symbol'].n_unique()} 只", flush=True)

    cover, px, vol, bars, bad = [], [], [], [], []

    for f in files:
        sym = f.stem
        d = pl.read_parquet(f)
        d = d.with_columns(pl.col("datetime").dt.date().alias("date"))

        g = daily.filter(pl.col("symbol") == sym)
        # 日线口径：有成交才算该日应存在（volume>0）
        g = g.filter(pl.col("volume") > 0)
        if g.height == 0:
            bad.append((sym, "日线无成交", d.height))
            continue

        n_bar = d.group_by("date").len().rename({"len": "n"})
        got = set(n_bar["date"].to_list())
        exp = set(g["date"].to_list())
        miss = exp - got
        extra = got - exp
        cover.append((sym, len(exp), len(got & exp), len(miss), len(extra)))

        nvals = n_bar["n"]
        bars.append((sym, int(nvals.min()), int(nvals.max()),
                     float(nvals.median()), int((nvals != 48).sum())))

        # 每日最后一根 close vs 日线 close
        lastbar = (d.sort("datetime").group_by("date").agg(pl.col("close").last())
                   .join(g.select(["date", pl.col("close").alias("dclose")]),
                         on="date", how="inner"))
        if lastbar.height:
            diff = (lastbar["close"] - lastbar["dclose"]).abs()
            px.append((sym, int(lastbar.height), float(diff.max()),
                       float((diff > 0.0011).mean())))

        # 每日成交量：sum(volume_raw)/100 vs 日线 volume
        v = (d.group_by("date").agg(pl.col("volume_raw").sum().alias("vbs"))
             .join(g.select(["date", "volume"]), on="date", how="inner")
             .filter(pl.col("volume") > 0))
        if v.height:
            r = v["vbs"] / 100.0 / v["volume"]
            vol.append((sym, int(v.height), float(r.median()),
                        float(r.quantile(0.01)), float(r.quantile(0.99))))

    # ---------------- 汇总
    c = pl.DataFrame(cover, schema=["symbol", "expected", "matched", "missing", "extra"],
                     orient="row")
    c = c.with_columns((pl.col("matched") / pl.col("expected")).alias("cover_ratio"))
    tot_exp, tot_miss = int(c["expected"].sum()), int(c["missing"].sum())
    print("\n=== 1. 交易日覆盖 ===")
    print(f"应覆盖 {tot_exp:,} 只-日 / 实际匹配 {int(c['matched'].sum()):,} "
          f"⇒ 覆盖率 {c['matched'].sum()/tot_exp:.6f}")
    print(f"缺失日合计 {tot_miss:,}（{c.filter(pl.col('missing')>0).height} 只票有缺失）")
    print(f"多出日合计 {int(c['extra'].sum()):,}（f5 有但日线无成交 ⇒ 停牌日占位/口径差异）")
    worst = c.sort("cover_ratio").head(8)
    print("覆盖最差 8 只：")
    for r in worst.iter_rows(named=True):
        print(f"  {r['symbol']} exp={r['expected']} got={r['matched']} "
              f"miss={r['missing']} extra={r['extra']} ratio={r['cover_ratio']:.4f}")

    b = pl.DataFrame(bars, schema=["symbol", "min_n", "max_n", "med_n", "days_ne_48"],
                     orient="row")
    print("\n=== 2. 每日根数（应为 48）===")
    print(f"日根数中位 median={b['med_n'].median():.1f} "
          f"min={b['min_n'].min()} max={b['max_n'].max()}")
    print(f"偏离 48 的 只-日 合计 {int(b['days_ne_48'].sum()):,}")

    p = pl.DataFrame(px, schema=["symbol", "days", "max_diff", "frac_gt_0.001"],
                     orient="row")
    print("\n=== 3. 收盘价同源（末根 5min close vs 日线 raw close）===")
    print(f"比对 {int(p['days'].sum()):,} 只-日；max|Δ| 最大值 {p['max_diff'].max():.6f}")
    print(f">0.001 占比：median={p['frac_gt_0.001'].median():.6f} "
          f"p90={p['frac_gt_0.001'].quantile(0.90):.6f}")
    bad_px = p.filter(pl.col("frac_gt_0.001") > 0.01).sort("frac_gt_0.001", descending=True)
    print(f"超 1% 日不等的票：{bad_px.height} 只")
    for r in bad_px.head(8).iter_rows(named=True):
        print(f"  {r['symbol']} days={r['days']} max|Δ|={r['max_diff']:.4f} "
              f"frac={r['frac_gt_0.001']:.3f}")

    vv = pl.DataFrame(vol, schema=["symbol", "days", "med", "p1", "p99"], orient="row")
    print("\n=== 4. 成交量单位（sum(volume_raw)/100 ÷ 日线 volume，应≈1）===")
    print(f"比值 median={vv['med'].median():.4f} "
          f"p1={vv['p1'].quantile(0.05):.4f} p99={vv['p99'].quantile(0.95):.4f}")

    if bad:
        print(f"\n异常 {len(bad)} 只：{bad[:5]}")


if __name__ == "__main__":
    sys.exit(main())
