"""证伪 · 第二轮：攻击「有背景 / 是龙头 ⇒ 会被游资炒」这个因果。

三个检验：
  A) 行业龙头是否更容易连板？（用同花顺行业 + 每日反推流通市值排名定义龙头，
     不引入未来信息；控制换手档后看龙头是否有增量）
  B) 「背景」代理：第一大股东持股比例越高（国资/集团控股），连板概率更高还是更低？
  C) 在 600817 所在档位（turn20<1%）内部，还有多少可判别信息？（AUC）

流通市值反推：fmcap = amount / (turnover_rate/100)，即当日成交额 / 换手率。
该式在项目内已验证（工行 2.44 万亿），不依赖股本表，且每日可得（无前视）。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"
H = 20
START_YEAR = "2019"

pl.Config.set_tbl_rows(60)
pl.Config.set_tbl_width_chars(200)
pl.Config.set_tbl_cols(24)


def auc(score: np.ndarray, label: np.ndarray) -> float:
    m = np.isfinite(score)
    s, y = score[m], label[m].astype(bool)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    ss = s[order]
    i = 0
    while i < len(ss):
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def main() -> None:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:4] >= START_YEAR]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "turnover_rate", "amount",
                 "consecutive_limit_ups", "consecutive_limit_downs"])
        .collect()
        .sort(["symbol", "date"])
    )
    print(f"面板 {df.height:,} 行", flush=True)

    df = df.with_columns([
        pl.col("consecutive_limit_ups").shift(-1).over("symbol").alias("lu1"),
        pl.col("close").shift(-H).over("symbol").alias("close_h"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(120).over("symbol") - 1.0).alias("ret120"),
    ])
    df = df.with_columns([
        pl.col("lu1").rolling_max(H).over("symbol").alias("f_max_lu"),
        (pl.col("close_h") / pl.col("close") - 1.0).alias("f_ret_H"),
        (pl.col("amount") / (pl.col("turnover_rate") / 100.0)).alias("fmcap"),
    ])
    df = df.filter(pl.col("fmcap").is_finite() & (pl.col("fmcap") > 0))

    # 行业
    hy = pl.read_parquet(str(ROOT / "data/ext_data/ext_hy_ths/part.parquet")).select(
        ["symbol", "所属同花顺行业"]).rename({"所属同花顺行业": "industry"})
    hy = hy.with_columns(pl.col("industry").str.split("-").list.first().alias("ind1"))
    # 大股东
    tf = pl.read_parquet(glob.glob(str(ROOT / "data/shareholder/top_float/*.parquet"))[0])
    latest = tf.group_by("code").agg(pl.col("period_end").max().alias("period_end"))
    tf = tf.join(latest, on=["code", "period_end"], how="inner")
    own = tf.group_by("code").agg([
        pl.col("ratio").max().alias("top1"),
        pl.when(pl.col("ratio") >= 5.0).then(pl.col("ratio")).otherwise(0.0).sum().alias("blk"),
    ]).with_columns(
        pl.col("code").str.replace(r"\..*$", "").alias("bare"),
        (100.0 - pl.col("blk")).alias("free_pct"),
    )

    d = (df.with_columns(pl.col("symbol").str.replace(r"\..*$", "").alias("bare"))
         .join(hy.select(["symbol", "industry", "ind1"]), on="symbol", how="left")
         .join(own.select(["bare", "top1", "free_pct"]), on="bare", how="left"))

    v = d.drop_nulls(["f_max_lu", "turn20", "ret20", "f_ret_H", "industry"])
    print(f"有效 {v.height:,}", flush=True)

    res = {}

    # ---------- A) 行业龙头 ----------
    print("\n=== A) 行业内流通市值排名 vs 连板概率（控制换手档）===")
    v2 = v.filter(pl.col("fmcap").is_not_null())
    # 每日每行业按 fmcap 排名
    v2 = v2.with_columns(
        pl.col("fmcap").rank("ordinal", descending=True).over(["date", "industry"]).alias("rk"),
        pl.len().over(["date", "industry"]).alias("n_ind"),
    ).with_columns(
        pl.when(pl.col("rk") <= 3).then(pl.lit("行业前3(龙头)"))
        .when(pl.col("rk") <= (pl.col("n_ind") * 0.2).ceil()).then(pl.lit("行业前20%"))
        .otherwise(pl.lit("其余")).alias("lead")
    )
    v2 = v2.with_columns(
        pl.when(pl.col("turn20") < 1.0).then(pl.lit("<1%"))
        .when(pl.col("turn20") < 3.0).then(pl.lit("1-3%"))
        .otherwise(pl.lit(">3%")).alias("tb")
    )
    tabA = []
    for tb in ["<1%", "1-3%", ">3%"]:
        for ld in ["行业前3(龙头)", "行业前20%", "其余"]:
            seg = v2.filter((pl.col("tb") == tb) & (pl.col("lead") == ld))
            if seg.height < 300:
                continue
            row = {"turn档": tb, "地位": ld, "n": seg.height,
                   "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / seg.height * 100, 2),
                   "median_ret_H%": round(float(seg["f_ret_H"].median()) * 100, 2)}
            tabA.append(row)
            print("  " + json.dumps(row, ensure_ascii=False))
    res["leader"] = tabA

    # ---------- B) 大股东持股 vs 连板 ----------
    print("\n=== B) 第一大股东持股比例 vs 连板概率（只在 turn20<1% 内）===")
    low = v.filter(pl.col("turn20") < 1.0).drop_nulls(["top1"])
    low = low.with_columns(
        pl.when(pl.col("top1") < 20).then(pl.lit("<20%(分散)"))
        .when(pl.col("top1") < 35).then(pl.lit("20-35%"))
        .when(pl.col("top1") < 50).then(pl.lit("35-50%"))
        .when(pl.col("top1") < 65).then(pl.lit("50-65%(控股)"))
        .otherwise(pl.lit(">65%(绝对控股)")).alias("ob")
    )
    tabB = []
    for ob in ["<20%(分散)", "20-35%", "35-50%", "50-65%(控股)", ">65%(绝对控股)"]:
        seg = low.filter(pl.col("ob") == ob)
        if seg.height < 300:
            continue
        row = {"大股东": ob, "n": seg.height,
               "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / seg.height * 100, 2),
               "P_单板%": round(seg.filter(pl.col("f_max_lu") >= 1).height / seg.height * 100, 2),
               "median_ret_H%": round(float(seg["f_ret_H"].median()) * 100, 2)}
        tabB.append(row)
        print("  " + json.dumps(row, ensure_ascii=False))
    res["ownership"] = tabB

    # ---------- C) 低换手档内的判别力 ----------
    print("\n=== C) 只在 turn20<1% 内，还有多少可判别信息 ===")
    lo = v.filter(pl.col("turn20") < 1.0).drop_nulls(["ret20", "f_ret_H"])
    lab = lo["f_max_lu"].to_numpy() >= 2
    print(f"  样本 {lo.height:,}  正例 {int(lab.sum()):,} ({lab.mean()*100:.2f}%)")
    feats = {
        "近20日均换手": lo["turn20"].to_numpy(),
        "近20日涨幅": lo["ret20"].fill_null(0).to_numpy(),
        "大股东持股": lo["top1"].fill_null(0).to_numpy(),
        "自由流通占比": lo["free_pct"].fill_null(0).to_numpy(),
        "log流通市值": np.log1p(lo["fmcap"].fill_null(0).to_numpy()),
    }
    aucs = {k: round(auc(x, lab), 3) for k, x in feats.items()}
    print("  AUC: " + json.dumps(aucs, ensure_ascii=False))
    res["auc_low_turn"] = aucs

    # 600817 在这些维度上的分位
    t = d.filter(pl.col("symbol") == "600817.SH").sort("date").tail(1).to_dicts()[0]
    print("\n  600817 当前位置: turn20=%.3f%%  ret20=%.2f%%  大股东=%.1f%%  自由流通=%.1f%%  流通市值=%.1f亿"
          % (t["turn20"], t["ret20"] * 100, t["top1"], t["free_pct"], t["fmcap"] / 1e8))
    res["target_pos"] = {k: (float(t[k]) if t[k] is not None else None)
                         for k in ["turn20", "ret20", "top1", "free_pct", "fmcap"]}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "proof_600817_youzi2.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n写出 {OUT / 'proof_600817_youzi2.json'}")


if __name__ == "__main__":
    main()
