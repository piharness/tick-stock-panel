"""证伪：600817.SH 在接下来 20 日里会有游资炒作？

命题 P0（用户给定）：
  「600817.SH 是（某个）背景区的龙头，因此接下来 20 日会有游资炒作。」

本脚本不讨论叙事，只做四件事：
  1) 标的画像：它现在是什么状态？（换手、筹码、自由流通、涨停史、龙虎榜史）
  2) 无条件 base rate + 年度分解：20 日内出现「连板」的概率本身有多高。
  3) 条件 base rate：把全市场按「与 600817 同构」的特征切开，看同类股票的概率。
  4) 自身回放 + 资金门槛：它历史上处于同类状态时发生了什么；要炒它需要多少钱。

「游资炒作」的操作化定义（可证伪、可复现）：
  L1 连板   : 未来 20 个交易日内 max(consecutive_limit_ups) >= 2
              —— 游资炒作的公认签名是连板接力；单次涨停多为基本面/指数事件。
  L2 单板   : 未来 20 个交易日内至少 1 次涨停。
  （龙虎榜口径仅 247 日，单独在文本中说明，不并入主表。）

防前视：t 日特征只用 t 日及之前的收盘数据；标签窗口为 t+1..t+20 交易日。
交易日历：直接用 enriched 的日期分区（面板只含交易日），over("symbol") 按行序滚动。

输出：data/.scratch/proof_600817_youzi.json
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
OUT = ROOT / "data" / ".scratch"
TARGET = "600817.SH"
H = 20
START_YEAR = "2016"

pl.Config.set_tbl_rows(80)
pl.Config.set_tbl_width_chars(210)
pl.Config.set_tbl_cols(30)


def load_panel() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:4] >= START_YEAR]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select([
            "symbol", "date", "open", "high", "low", "close", "raw_close", "raw_high",
            "amount", "turnover_rate", "consecutive_limit_ups",
        ])
        .collect()
        .sort(["symbol", "date"])
    )
    return df


def add_features(df: pl.DataFrame) -> pl.DataFrame:
    """所有收益率一律用 close（前复权）自洽计算。

    血的教训：close 是前复权价、raw_close 是原始价，两者比值在本标的
    2016 段为 0.819、2026 段为 1.0。`close.shift(-H)/raw_close` 是跨口径
    相除，会凭空产生 -15% 的假跌幅（已修正）。
    前复权收益 = 含分红再投资的真实收益；其代价是历史上的 close 用到了
    未来除权信息，对 20 日 base rate 的描述性统计影响可忽略，正式回测
    应改用 adj_factor 快照。
    """
    df = df.with_columns([
        pl.col("consecutive_limit_ups").shift(-1).over("symbol").alias("lu1"),
        pl.col("close").shift(-H).over("symbol").alias("close_h"),
        pl.col("turnover_rate").shift(-1).over("symbol").alias("turn1"),
        pl.col("amount").shift(-1).over("symbol").alias("amt1"),
        # 触板：当日最高价 / 前收 - 1（同口径，均为原始价）
        ((pl.col("raw_high") / pl.col("raw_close").shift(1).over("symbol")) - 1.0).alias("touch"),
    ])
    df = df.with_columns([
        pl.col("lu1").rolling_max(H).over("symbol").alias("f_max_lu"),
        (pl.col("lu1") > 0).cast(pl.Int8).rolling_sum(H).over("symbol").alias("f_n_lu"),
        pl.col("turn1").rolling_max(H).over("symbol").alias("f_max_turn"),
        pl.col("amt1").rolling_max(H).over("symbol").alias("f_max_amt"),
        pl.col("touch").shift(-1).rolling_max(H).over("symbol").alias("f_max_touch"),
        (pl.col("close_h") / pl.col("close") - 1.0).alias("f_ret_H"),
        # 状态特征（t 日可观测）
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn20"),
        pl.col("amount").rolling_mean(20).over("symbol").alias("amt20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(120).over("symbol") - 1.0).alias("ret120"),
        (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1.0).alias("ret5"),
    ])
    return df


def attach_ownership(df: pl.DataFrame) -> pl.DataFrame:
    tf = pl.read_parquet(glob.glob(str(ROOT / "data/shareholder/top_float/*.parquet"))[0])
    tf = tf.with_columns(pl.col("code").cast(pl.Utf8))
    latest = tf.group_by("code").agg(pl.col("period_end").max().alias("period_end"))
    tf = tf.join(latest, on=["code", "period_end"], how="inner")
    own = tf.group_by("code").agg([
        pl.col("ratio").max().alias("top1_ratio"),
        pl.when(pl.col("ratio") >= 5.0).then(pl.col("ratio")).otherwise(0.0).sum().alias("block_ratio"),
    ]).with_columns(
        pl.col("code").str.replace(r"\..*$", "").alias("bare"),
        (100.0 - pl.col("block_ratio")).alias("free_float_pct"),
    )
    return df.with_columns(pl.col("symbol").str.replace(r"\..*$", "").alias("bare")).join(
        own.select(["bare", "top1_ratio", "free_float_pct"]), on="bare", how="left"
    ).drop("bare")


def auc(score: np.ndarray, label: np.ndarray) -> float:
    """手写 AUC（Mann-Whitney），不依赖 sklearn。"""
    m = np.isfinite(score)
    s, y = score[m], label[m].astype(bool)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    # 平均秩处理并列
    s_sorted = s[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def main() -> None:
    print("装载面板…", flush=True)
    df = load_panel()
    print(f"  行数 {df.height:,}  区间 {df['date'].min()} ~ {df['date'].max()}", flush=True)
    df = add_features(df)
    df = attach_ownership(df)
    valid = df.drop_nulls(["f_max_lu", "turn20", "ret20"]).filter(pl.col("f_ret_H").is_not_null())
    print(f"  有效标签样本 {valid.height:,}", flush=True)

    res: dict = {}

    # ---------------- 1. 画像 ----------------
    t = df.filter(pl.col("symbol") == TARGET).sort("date")
    last = t.tail(1).to_dicts()[0]
    inst = pl.read_parquet(ROOT / "data/instruments/instruments.parquet").filter(pl.col("symbol") == TARGET)
    tot = float(inst["total_shares"][0])
    hist = t.filter(pl.col("date").dt.year() >= 2016)
    prof = {
        "name": inst["name"][0],
        "last_date": str(last["date"]),
        "raw_close": last["raw_close"],
        "turnover_rate_pct": round(last["turnover_rate"], 3),
        "turn20_pct": round(last["turn20"], 3),
        "ret5_pct": round((last["ret5"] or 0) * 100, 2),
        "ret20_pct": round(last["ret20"] * 100, 2),
        "ret120_pct": round((last["ret120"] or 0) * 100, 2),
        "top1_ratio_pct": last["top1_ratio"],
        "free_float_pct": last["free_float_pct"],
        "total_shares_yi": round(tot / 1e8, 3),
        "mktcap_yi": round(tot * last["raw_close"] / 1e8, 2),
        "free_float_mktcap_yi": round(tot * last["raw_close"] / 1e8 * last["free_float_pct"] / 100, 2),
        "amt20_wan": round(last["amt20"] / 1e4, 0),
        "n_limitup_2026": int(hist.filter((pl.col("date").dt.year() == 2026) & (pl.col("consecutive_limit_ups") > 0)).height),
        "n_limitup_2016_2026": int(hist.filter(pl.col("consecutive_limit_ups") > 0).height),
        "n_bars_2016_2026": int(hist.height),
    }
    res["profile"] = prof
    print("\n=== 1. 标的画像 ===")
    print(json.dumps(prof, ensure_ascii=False, indent=1, default=str))

    # ---------------- 2. base rate + 年度 ----------------
    print("\n=== 2. 全市场无条件 base rate（未来 20 交易日）===")
    uncond = {
        "L0_触板_pct": round(valid.filter(pl.col("f_max_touch") >= 0.095).height / valid.height * 100, 2),
        "L1_连板_pct": round(valid.filter(pl.col("f_max_lu") >= 2).height / valid.height * 100, 2),
        "L2_单板_pct": round(valid.filter(pl.col("f_max_lu") >= 1).height / valid.height * 100, 2),
        "n": valid.height,
    }
    print("  " + json.dumps(uncond, ensure_ascii=False))
    yearly = []
    for (y,), seg in valid.group_by(pl.col("date").dt.year().alias("y"), maintain_order=True):
        yearly.append({
            "year": int(y), "n": seg.height,
            "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / seg.height * 100, 2),
            "P_单板%": round(seg.filter(pl.col("f_max_lu") >= 1).height / seg.height * 100, 2),
        })
    res["unconditional"] = uncond
    res["yearly"] = yearly
    print("\n  年度分解:")
    for r in yearly:
        print("   " + json.dumps(r, ensure_ascii=False))

    # ---------------- 3. 按换手分档 ----------------
    print("\n=== 3. 按「近 20 日日均换手率」分档 ===")
    v = valid.filter(pl.col("turn20").is_not_null())
    cuts = [0.5, 1.0, 2.0, 3.0, 5.0]
    v = v.with_columns(
        pl.when(pl.col("turn20") < cuts[0]).then(pl.lit("<0.5%"))
        .when(pl.col("turn20") < cuts[1]).then(pl.lit("0.5-1%"))
        .when(pl.col("turn20") < cuts[2]).then(pl.lit("1-2%"))
        .when(pl.col("turn20") < cuts[3]).then(pl.lit("2-3%"))
        .when(pl.col("turn20") < cuts[4]).then(pl.lit("3-5%"))
        .otherwise(pl.lit(">5%")).alias("turn_b")
    )
    order = ["<0.5%", "0.5-1%", "1-2%", "2-3%", "3-5%", ">5%"]
    tab = []
    for b in order:
        seg = v.filter(pl.col("turn_b") == b)
        if seg.height < 500:
            continue
        row = {
            "bucket": b, "n": seg.height,
            "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / seg.height * 100, 2),
            "P_单板%": round(seg.filter(pl.col("f_max_lu") >= 1).height / seg.height * 100, 2),
            "median_ret_H%": round(float(seg["f_ret_H"].median()) * 100, 2),
        }
        tab.append(row)
        print("  " + json.dumps(row, ensure_ascii=False))
    res["turn_buckets"] = tab

    # ---------------- 4. 同构子样本 ----------------
    print("\n=== 4. 「与 600817 同构」子样本 ===")
    names = pl.read_parquet(ROOT / "data/instruments/instruments.parquet").select(["symbol", "name"])
    twin = (
        v.join(names, on="symbol", how="left")
        .filter(~pl.col("name").str.contains("ST"))
        .filter(pl.col("top1_ratio") >= 50.0)
        .filter(pl.col("free_float_pct") <= 35.0)
        .filter(pl.col("turn20") < 1.0)
    )
    def stats(seg: pl.DataFrame) -> dict:
        return {
            "n_obs": seg.height, "n_symbols": int(seg["symbol"].n_unique()),
            "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / max(seg.height, 1) * 100, 2),
            "P_单板%": round(seg.filter(pl.col("f_max_lu") >= 1).height / max(seg.height, 1) * 100, 2),
            "median_ret_H%": round(float(seg["f_ret_H"].median()) * 100, 2),
            "mean_ret_H%": round(float(seg["f_ret_H"].mean()) * 100, 2),
        }
    twin_s = stats(twin)
    print("  同构(大股东>50%,自由流通<35%,换手<1%,非ST): " + json.dumps(twin_s, ensure_ascii=False))
    twin2 = twin.filter(pl.col("ret20") < -0.05)
    twin2_s = stats(twin2)
    print("  再叠加近20日跌>5%: " + json.dumps(twin2_s, ensure_ascii=False))
    twin3 = twin2.filter(pl.col("ret120") < -0.15)
    twin3_s = stats(twin3)
    print("  再叠加120日跌>15%: " + json.dumps(twin3_s, ensure_ascii=False))
    res["twins"] = {"base": twin_s, "down20": twin2_s, "down120": twin3_s}

    # ---------------- 5. 自身回放 ----------------
    print("\n=== 5. 600817 自身回放（未来 20 日）===")
    sa = t.drop_nulls(["f_max_lu", "turn20"])
    states = {
        "全部历史(2005-)": sa,
        "2016年后": sa.filter(pl.col("date").dt.year() >= 2016),
        "换手<1%": sa.filter(pl.col("turn20") < 1.0),
        "换手<1% 且 20日跌>5%": sa.filter((pl.col("turn20") < 1.0) & (pl.col("ret20") < -0.05)),
        "换手<1% 且 20日跌>5% 且 120日跌>15%": sa.filter(
            (pl.col("turn20") < 1.0) & (pl.col("ret20") < -0.05) & (pl.col("ret120") < -0.15)),
    }
    self_tab = {}
    for name, seg in states.items():
        seg = seg.filter(pl.col("f_ret_H").is_not_null())
        if seg.height == 0:
            continue
        row = {
            "n": seg.height,
            "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / seg.height * 100, 2),
            "P_单板%": round(seg.filter(pl.col("f_max_lu") >= 1).height / seg.height * 100, 2),
            "median_ret_H%": round(float(seg["f_ret_H"].median()) * 100, 2),
            "mean_ret_H%": round(float(seg["f_ret_H"].mean()) * 100, 2),
            "p10%": round(float(seg["f_ret_H"].quantile(0.1)) * 100, 2),
            "p90%": round(float(seg["f_ret_H"].quantile(0.9)) * 100, 2),
        }
        self_tab[name] = row
        print(f"  {name}: " + json.dumps(row, ensure_ascii=False))
    res["self_replay"] = self_tab

    # ---------------- 6. 可预测性 AUC ----------------
    print("\n=== 6. 用 t 日可观测特征预测「未来 20 日连板」的 AUC ===")
    lab = v["f_max_lu"].to_numpy() >= 2
    feats = {
        "近20日均换手": v["turn20"].fill_null(0).to_numpy(),
        "近20日涨幅": v["ret20"].fill_null(0).to_numpy(),
        "近5日涨幅": v["ret5"].fill_null(0).to_numpy(),
        "近20日均成交额(log)": np.log1p(v["amt20"].fill_null(0).to_numpy()),
    }
    aucs = {k: round(auc(x, lab), 3) for k, x in feats.items()}
    print("  " + json.dumps(aucs, ensure_ascii=False))
    res["auc"] = aucs

    # ---------------- 7. 资金门槛 ----------------
    free_cap = prof["free_float_mktcap_yi"] * 1e8
    cap = {
        "free_float_mktcap_yi": prof["free_float_mktcap_yi"],
        "amt20_wan": prof["amt20_wan"],
        "buy_5pct_free_float_wan": round(free_cap * 0.05 / 1e4, 0),
        "buy_10pct_free_float_wan": round(free_cap * 0.10 / 1e4, 0),
        "days_at_20pct_participation": round(free_cap * 0.10 / (last["amt20"] * 0.2), 1),
    }
    print("\n=== 7. 资金门槛 ===")
    print("  " + json.dumps(cap, ensure_ascii=False))
    res["capital"] = cap

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "proof_600817_youzi.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n写出 {OUT / 'proof_600817_youzi.json'}")


if __name__ == "__main__":
    main()
