"""锁仓横盘形态（turn60≈0.96%、ret20≈−3.5%、小流通盘）的前向读数。

用途：给 `citizenship.LOCK_*` 常量做校准与外部验证，不产出交易信号。
口径纪律（沿用本项目已冻结的三条）：
  1) 前向收益 = 基期收盘买入、持有 20 个交易日；超额 = 减当日全市场等权。
  2) 事件扎堆时 N 是**基期数**不是股票日数：先按日聚合，再对日均值做 t。
  3) 必须有同日同池对照（只让换手这一维变），否则「低换手」与「小市值/下跌」
     混在一起，分不清是哪一维在起作用。

跑法：backend/.venv/Scripts/python.exe analysis/_diag_lock_pattern.py --run
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import polars as pl

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
START = "2021-01-01"
END = "2026-10-31"          # 留出 20 个交易日的前向窗口
FWD = 20


def load_panel() -> tuple[pl.DataFrame, list[str]]:
    dirs = sorted(glob.glob(os.path.join(DATA, "kline_daily_enriched", "date=*")))
    days = [os.path.basename(d)[5:] for d in dirs]
    keep = [i for i, d in enumerate(days) if START <= d <= END]
    dirs = [dirs[i] for i in keep]
    frames = []
    for d in dirs:
        f = os.path.join(d, "part.parquet")
        if not os.path.exists(f):
            continue
        frames.append(pl.read_parquet(
            f, columns=["symbol", "date", "close", "amount", "turnover_rate",
                        "consecutive_limit_ups"]))
    p = pl.concat(frames, how="vertical_relaxed").sort(["symbol", "date"])
    return p, days


def features(p: pl.DataFrame) -> pl.DataFrame:
    p = p.with_columns([
        pl.col("turnover_rate").rolling_mean(60, min_samples=55).over("symbol").alias("turn60"),
        pl.col("amount").rolling_mean(60, min_samples=55).over("symbol").alias("amt60"),
        pl.col("close").shift(-FWD).over("symbol").alias("_cfwd"),
        pl.col("close").shift(20).over("symbol").alias("_c20"),
        pl.col("consecutive_limit_ups").gt(0).cast(pl.Int8).alias("_lu"),
    ])
    return p.with_columns([
        (pl.col("_cfwd") / pl.col("close") - 1).alias("fwd20"),
        (pl.col("close") / pl.col("_c20") - 1).alias("ret20"),
        # 点火事件：未来 20 个交易日内是否出现涨停（含一字）
        pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol").alias("_lu_fwd"),
    ]).with_columns(
        (pl.col("_lu_fwd").fill_null(0) >= 1).alias("lu_fwd20")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if not args.run:
        print(__doc__)
        return 0
    p, days = load_panel()
    f = features(p)
    inst = pl.read_parquet(os.path.join(DATA, "instruments", "instruments.parquet"),
                           columns=["symbol", "name", "float_shares"])
    f = (f.join(inst, on="symbol", how="left")
         .filter((pl.col("float_shares") > 0) & pl.col("close").is_not_null()))
    f = f.with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
    # ST 用当前名称近似（历史名称不在本地）；这是本脚本的已知偏差，方向：高估收益。
    f = f.filter(~pl.col("name").fill_null("").str.contains("ST"))

    # 基期：每月首个交易日
    dates = sorted(f["date"].unique().to_list())
    base, seen = [], set()
    for d in dates:
        k = str(d)[:7]
        if k not in seen:
            seen.add(k)
            base.append(d)

    rows = []
    for d in base:
        snap = f.filter(pl.col("date") == d).filter(
            pl.col("fwd20").is_not_null()
            & pl.col("turn60").is_not_null()
            & pl.col("ret20").is_not_null()
        )
        if snap.height < 300:
            continue
        mkt = snap["fwd20"].mean()
        # 目标形态：低换手 + 横盘略跌 + 小流通盘 + 有最低成交额
        hit = snap.filter(
            (pl.col("turn60") >= 0.6) & (pl.col("turn60") <= 1.3)
            & (pl.col("ret20") >= -0.06) & (pl.col("ret20") <= -0.01)
            & (pl.col("mcap") <= 25e8) & (pl.col("amt60") >= 1.5e7)
        )
        # 对照：同日、同市值/同涨幅段、成交额达标，只把换手换到 2%~8%
        ctrl = snap.filter(
            (pl.col("turn60") >= 2.0) & (pl.col("turn60") <= 8.0)
            & (pl.col("ret20") >= -0.06) & (pl.col("ret20") <= -0.01)
            & (pl.col("mcap") <= 25e8) & (pl.col("amt60") >= 1.5e7)
        )
        rows.append({
            "date": d,
            "n_hit": hit.height,
            "n_ctrl": ctrl.height,
            "hit_ex": (hit["fwd20"].mean() - mkt) if hit.height else None,
            "ctrl_ex": (ctrl["fwd20"].mean() - mkt) if ctrl.height else None,
            "hit_med": hit["fwd20"].median() if hit.height else None,
            "hit_p10": hit["fwd20"].quantile(0.10) if hit.height else None,
            "mkt": mkt,
            "hit_lu": hit["lu_fwd20"].mean() if hit.height else None,
            "ctrl_lu": ctrl["lu_fwd20"].mean() if ctrl.height else None,
        })

    r = pl.DataFrame(rows).drop_nulls()
    n_base = r.height
    print(f"基期数 {n_base}（{r['date'][0]} .. {r['date'][-1]}），命中股票日合计 {int(r['n_hit'].sum())}")
    def stat(col: str) -> tuple[float, float, float]:
        s = r[col]
        m, sd = float(s.mean()), float(s.std())
        t = m / (sd / (len(s) ** 0.5)) if sd else float("nan")
        return m, t, float(s.median())
    for col, name in (("hit_ex", "命中组超额"), ("ctrl_ex", "同池高换手对照"),
                      ("hit_med", "命中组 fwd20 中位"), ("hit_p10", "命中组 fwd20 p10")):
        m, t, med = stat(col)
        print(f"{name:16s} 均值 {m*100:+.2f}%  t={t:.2f}  中位 {med*100:+.2f}%")
    diff = r["hit_ex"] - r["ctrl_ex"]
    t = float(diff.mean() / (diff.std() / (len(diff) ** 0.5)))
    print(f"差值（低换手 − 高换手） {diff.mean()*100:+.2f}%  t={t:.2f}  "
          f"正基期 {int((diff > 0).sum())}/{len(diff)}")
    dl = r["hit_lu"] - r["ctrl_lu"]
    tl = float(dl.mean() / (dl.std() / (len(dl) ** 0.5)))
    print(f"未来 20 日出现过涨停：命中 {r['hit_lu'].mean()*100:.1f}%  "
          f"对照 {r['ctrl_lu'].mean()*100:.1f}%  差 {dl.mean()*100:+.1f}pp  t={tl:.2f}  "
          f"正基期 {int((dl > 0).sum())}/{len(dl)}")
    print(f"命中组日均只数 {r['n_hit'].mean():.1f}（min {int(r['n_hit'].min())}, max {int(r['n_hit'].max())}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
