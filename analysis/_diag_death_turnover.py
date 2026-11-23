"""检验「死亡换手」是不是伪命题。

待攻击的结论：
  连板股当日换手越高，次日晋级率越低（<5% → 63.8%，>20% → 28.1%）。

混杂怀疑（复合代理）：
  一字板 = 买不进 = 成交极少 = 换手天然低，而一字板的晋级率天然高。
  若如此，则「低换手高晋级」只是「一字板高晋级」的镜像，换手本身不含信息。

做法：
  1) 先看一字板 vs 非一字板的晋级率差距。
  2) 在**非一字板**内部重新按换手分档 —— 若梯度消失，则换手是伪变量。
  3) 再控制板数（只看 4 板 / 只看 3 板）重做一次，排除「高板数=低换手」。
  4) 同时控制流通市值（大票换手天然低）。

一字板判定：当日 high == low（复权价比例不变），且当日涨停。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(60)
pl.Config.set_tbl_width_chars(200)


def load() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:4] >= "2019"]
    d = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "high", "low", "close", "open", "amount",
                 "turnover_rate", "consecutive_limit_ups"])
        .collect()
        .sort(["symbol", "date"])
    )
    d = d.with_columns([
        pl.col("consecutive_limit_ups").shift(-1).over("symbol").alias("nxt"),
        pl.col("close").shift(-20).over("symbol").alias("c20"),
        (pl.col("amount") / (pl.col("turnover_rate") / 100.0)).alias("fmcap"),
        ((pl.col("high") - pl.col("low")) / pl.col("close") < 0.0015).alias("is_flat"),
    ])
    d = d.with_columns((pl.col("c20") / pl.col("close") - 1.0).alias("f20"))
    d = d.with_columns(
        (pl.col("is_flat") & (pl.col("consecutive_limit_ups") > 0)).alias("oneword")
    )
    return d


def promo(seg: pl.DataFrame) -> tuple[float, float, int]:
    if seg.height == 0:
        return float("nan"), float("nan"), 0
    p = seg.filter(pl.col("nxt") > pl.col("consecutive_limit_ups")).height / seg.height
    r = float(seg["f20"].mean()) * 100 if seg["f20"].is_not_null().any() else float("nan")
    return p * 100, r, seg.height


def main() -> None:
    d = load().filter(
        (pl.col("consecutive_limit_ups") >= 1) & pl.col("nxt").is_not_null()
        & pl.col("f20").is_not_null())
    print(f"连板样本 {d.height:,}", flush=True)
    res = {}

    print("\n=== 0. 一字板 vs 非一字板（全部 k>=1）===")
    for lab, seg in [("一字板", d.filter(pl.col("oneword"))),
                     ("非一字板", d.filter(~pl.col("oneword")))]:
        p, r, n = promo(seg)
        print(f"  {lab}: n={n:6d} 次日晋级 {p:5.1f}%  20日均 {r:+6.2f}%")
        res[f"oneword_{lab}"] = {"n": n, "promo": round(p, 1), "ret20": round(r, 2)}

    print("\n=== 1. 非一字板内部，按换手分档（k>=2）===")
    base = d.filter((pl.col("consecutive_limit_ups") >= 2) & ~pl.col("oneword"))
    tab = []
    for lo, hi, lab in [(0, 3, "<3%"), (3, 6, "3-6%"), (6, 10, "6-10%"),
                        (10, 15, "10-15%"), (15, 25, "15-25%"), (25, 999, ">25%")]:
        seg = base.filter((pl.col("turnover_rate") >= lo) & (pl.col("turnover_rate") < hi))
        p, r, n = promo(seg)
        if n < 100:
            continue
        tab.append({"换手": lab, "n": n, "次日晋级%": round(p, 1), "20日均%": round(r, 2)})
        print(f"  {lab:8s}: n={n:5d}  次日晋级 {p:5.1f}%  20日均 {r:+6.2f}%")
    res["nonflat_by_turn"] = tab

    print("\n=== 2. 控板数：只在 4 板样本内按换手分档 ===")
    k4 = d.filter(pl.col("consecutive_limit_ups") == 4)
    tab4 = []
    for lo, hi, lab in [(0, 6, "<6%"), (6, 12, "6-12%"), (12, 20, "12-20%"), (20, 999, ">20%")]:
        seg = k4.filter((pl.col("turnover_rate") >= lo) & (pl.col("turnover_rate") < hi))
        p, r, n = promo(seg)
        if n < 50:
            continue
        tab4.append({"换手": lab, "n": n, "次日晋级%": round(p, 1), "20日均%": round(r, 2)})
        print(f"  {lab:8s}: n={n:5d}  次日晋级 {p:5.1f}%  20日均 {r:+6.2f}%")
    res["k4_by_turn"] = tab4

    print("\n=== 3. 控市值：非一字板 k>=2，按流通市值分档再按换手 ===")
    b = base.with_columns(
        pl.when(pl.col("fmcap") < 5e9).then(pl.lit("小<50亿"))
        .when(pl.col("fmcap") < 2e10).then(pl.lit("中50-200亿"))
        .otherwise(pl.lit("大>200亿")).alias("cap")
    )
    for c in ["小<50亿", "中50-200亿", "大>200亿"]:
        seg_c = b.filter(pl.col("cap") == c)
        if seg_c.height < 300:
            continue
        line = f"  {c}: n={seg_c.height:5d}"
        for lo, hi, lab in [(0, 6, "低换手"), (6, 15, "中"), (15, 999, "高换手")]:
            seg = seg_c.filter((pl.col("turnover_rate") >= lo) & (pl.col("turnover_rate") < hi))
            p, r, n = promo(seg)
            line += f"  | {lab}(n={n:4d}) 晋级{p:5.1f}% 20日{r:+6.2f}%"
        print(line)
    res["by_cap_turn"] = True

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe_death_turnover.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n写出 {OUT/'probe_death_turnover.json'}")


if __name__ == "__main__":
    main()
