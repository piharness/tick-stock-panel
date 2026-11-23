"""「试盘痕迹」诊断：在公民等级底层里，找游资小额试盘留下的日内结构异常。

为什么换这个问题
----------------
上一轮已经证明：在 +3% 追涨、赌当日封板这个动作上，胜率与赔率精确抵消，
任何形态 2 日期望都为负。原因是「形态」本身就是买入需求，价格已经把它定价。

本脚本把决策点再左移一层：**不看它拉起来之后，看它拉起来之前有没有人试过**。
游资在小盘股上广撒网、先小额试盘、再决定是否点火；试盘的特征是
「有量、但价被压住」——常规的量价指标（涨幅、换手、成交额）看不见，
只能从**日内成交额的时序结构**里找痕迹。

痕迹候选（全部是日内结构，不是量能水平）
----------------------------------------
1 `net_bias`   = (上涨分钟成交额 − 下跌分钟成交额) / 全天成交额
                 —— 有人在涨的时候成交更多，但价格没被推上去（吸筹）
2 `close_pos`  = (收盘 − 最低) / (最高 − 最低)
                 —— 收盘被"托"在区间上部，但全天涨幅不大
3 `tail_share` = 14:30 后成交额 / 全天
                 —— 尾盘承接
4 `pulse`      = 单分钟涨幅 ≥1.5% 的次数
                 —— 脉冲式试探（拉了又撤）
5 `max_r`      = 最大单分钟涨幅
6 `vwap_dev`   = 收盘 / VWAP − 1
                 —— 收盘价高于全天均价 = 尾盘有人在买
7 `updn`       = 上涨分钟数 / 下跌分钟数
8 `amt_top`    = 最大单分钟成交额 / 全天
                 —— 是否有"一笔大单"突然出现
9 `r_std`      = 分钟收益标准差（波动结构）

关键检验
--------
- **正交化**：所有指标都必须控制「当日涨幅 + 流通市值分位 + 换手率分位 + 前 5 日涨幅」
  之后仍然有增量，否则只是量价水平的投影（母题：换手/成交额构造正交化后归零）。
- **不看 AUC，看赔率**：目标不是胜率，而是「命中时赚多少 / 没命中时亏多少」。
- **标签**：未来 5 个交易日内 ① 是否出现涨停 ② 最大涨幅 ③ 5 日收盘收益。

只读数据，输出 `data/.scratch/probe_footprint.json` + `probe_daily.parquet`。
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.citizenship import (  # noqa: E402
    LEVEL_ORDER,
    _assign_level,
    _quantile_rank,
)

DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"
MINUTE = DATA / "kline_minute"
OUT = DATA / ".scratch" / "probe_footprint.json"
DAILY_PARQUET = DATA / ".scratch" / "probe_daily.parquet"

START = "2026-06-01"
TAIL_MIN = 870        # 14:30
OPEN_MIN = 600        # 10:00
PULSE = 0.015
FWD = 5


# ===== 工具 =====

def _tstat(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    if len(xs) < 3:
        return float("nan"), float("nan")
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    se = math.sqrt(var / len(xs))
    return m, (m / se if se > 0 else float("nan"))


def _quant(xs, q):
    xs = sorted(x for x in xs if x is not None and math.isfinite(x))
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def _stat(xs):
    clean = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if not clean:
        return {"n": 0}
    m, t = _tstat(clean)
    return {
        "n": len(clean),
        "mean": round(m, 6),
        "median": round(_quant(clean, 0.5), 6),
        "p95": round(_quant(clean, 0.95), 6),
        "p99": round(_quant(clean, 0.99), 6),
        "t": round(t, 2) if math.isfinite(t) else None,
        "win": round(sum(1 for x in clean if x > 0) / len(clean), 4),
    }


def _json_safe(o):
    if isinstance(o, dict):
        return {(k if isinstance(k, (str, int, float, bool)) or k is None else str(k)):
                _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


# ===== 1. 分钟线 → 日级痕迹 =====

def footprint(d: str) -> pl.DataFrame | None:
    p = f"{MINUTE}/date={d}/part.parquet"
    if not os.path.exists(p):
        return None
    m = pl.read_parquet(p).sort(["symbol", "datetime"])
    m = m.with_columns(
        pl.col("close").shift(1).over("symbol").alias("_pc"),
        (pl.col("datetime").dt.hour().cast(pl.Int32) * 60
         + pl.col("datetime").dt.minute().cast(pl.Int32)).alias("mins"),
    )
    m = m.with_columns(
        (pl.col("close") / pl.col("_pc") - 1).fill_null(0.0).alias("r")
    )
    a = m.group_by("symbol", maintain_order=True).agg([
        pl.col("amount").sum().alias("amt"),
        (pl.col("amount") * pl.when(pl.col("r") > 0).then(1.0).otherwise(0.0)).sum().alias("up_amt"),
        (pl.col("amount") * pl.when(pl.col("r") < 0).then(1.0).otherwise(0.0)).sum().alias("dn_amt"),
        pl.col("r").max().alias("max_r"),
        (pl.col("r") >= PULSE).sum().alias("pulse"),
        pl.col("r").std().alias("r_std"),
        (pl.col("r") > 0).sum().alias("nup"),
        (pl.col("r") < 0).sum().alias("ndn"),
        pl.col("amount").max().alias("amt_max"),
        pl.col("close").sort_by("datetime").last().alias("c"),
        pl.col("open").sort_by("datetime").first().alias("o"),
        pl.col("high").max().alias("h"),
        pl.col("low").min().alias("l"),
        (pl.col("amount") / pl.col("close")).sum().alias("_sh"),
        (pl.col("amount") * pl.when(pl.col("mins") >= TAIL_MIN).then(1.0).otherwise(0.0)).sum().alias("tail_amt"),
        (pl.col("amount") * pl.when(pl.col("mins") <= OPEN_MIN).then(1.0).otherwise(0.0)).sum().alias("open_amt"),
        pl.len().alias("bars"),
    ])
    a = a.filter((pl.col("amt") > 0) & (pl.col("bars") >= 60) & (pl.col("_sh") > 0))
    a = a.with_columns([
        ((pl.col("up_amt") - pl.col("dn_amt")) / pl.col("amt")).alias("net_bias"),
        (pl.col("amt") / pl.col("_sh")).alias("vwap"),
        ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l")).replace(0.0, None)).alias("close_pos"),
        (pl.col("tail_amt") / pl.col("amt")).alias("tail_share"),
        (pl.col("open_amt") / pl.col("amt")).alias("open_share"),
        (pl.col("nup") / pl.col("ndn").replace(0, None)).alias("updn"),
        (pl.col("amt_max") / pl.col("amt")).alias("amt_top"),
    ])
    a = a.with_columns([
        (pl.col("c") / pl.col("vwap") - 1).alias("vwap_dev"),
        pl.lit(d).alias("date"),
    ])
    return a.select([
        "symbol", "date", "net_bias", "close_pos", "tail_share", "open_share",
        "pulse", "max_r", "r_std", "updn", "amt_top", "vwap_dev", "c", "o", "h", "l", "amt",
    ])


# ===== 2. 日线 + 标签 + 等级 =====

def build_panel(fps: pl.DataFrame) -> pl.DataFrame:
    names = sorted(x[5:] for x in os.listdir(ENRICHED) if x.startswith("date="))
    dates = [x for x in names if x >= START]
    cols = ["symbol", "date", "close", "open", "high", "low", "raw_close", "amount",
            "turnover_rate", "consecutive_limit_ups"]
    D = pl.concat([
        pl.read_parquet(f"{ENRICHED}/date={d}/part.parquet", columns=cols) for d in dates
    ]).sort(["symbol", "date"])
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet",
                           columns=["symbol", "name", "float_shares"])
    D = D.join(inst, on="symbol", how="left").with_columns(pl.col("name").fill_null(""))
    D = D.with_columns(
        (pl.col("close") * pl.col("float_shares")).alias("mcap"),
        (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("ret5"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("ret20"),
        (pl.col("high").shift(-1).over("symbol")).alias("h1"),
        (pl.col("high").shift(-2).over("symbol")).alias("h2"),
        (pl.col("high").shift(-3).over("symbol")).alias("h3"),
        (pl.col("high").shift(-4).over("symbol")).alias("h4"),
        (pl.col("high").shift(-5).over("symbol")).alias("h5"),
        (pl.col("close").shift(-5).over("symbol")).alias("c5"),
        (pl.col("consecutive_limit_ups").shift(-1).over("symbol") > 0).alias("lu1"),
        (pl.col("consecutive_limit_ups").shift(-2).over("symbol") > 0).alias("lu2"),
        (pl.col("consecutive_limit_ups").shift(-3).over("symbol") > 0).alias("lu3"),
        (pl.col("consecutive_limit_ups").shift(-4).over("symbol") > 0).alias("lu4"),
        (pl.col("consecutive_limit_ups").shift(-5).over("symbol") > 0).alias("lu5"),
        pl.col("name").str.to_uppercase().str.contains("ST", literal=True).alias("is_st"),
    )
    D = D.with_columns(
        pl.max_horizontal("h1", "h2", "h3", "h4", "h5").alias("fwd_high5"),
        pl.max_horizontal([
            pl.col("lu1").fill_null(False), pl.col("lu2").fill_null(False),
            pl.col("lu3").fill_null(False), pl.col("lu4").fill_null(False),
            pl.col("lu5").fill_null(False),
        ]).alias("hit_lu5"),
    ).with_columns(
        (pl.col("fwd_high5") / pl.col("close") - 1).alias("fwd_max5"),
        (pl.col("c5") / pl.col("close") - 1).alias("fwd_ret5"),
    )
    D = D.with_columns(
        _quantile_rank("mcap").alias("p_scale"),
        _quantile_rank("turnover_rate").alias("p_turnover"),
        _quantile_rank("amount").alias("p_amount"),
    ).with_columns(_assign_level().alias("level"))
    D = D.sort(["symbol", "date"]).with_columns(
        pl.col("level").shift(1).over("symbol").alias("prev_level")
    )
    D = D.with_columns(pl.col("date").cast(pl.Utf8))
    X = fps.join(D, on=["symbol", "date"], how="inner")
    X = X.filter(
        ~pl.col("is_st").fill_null(False)
        & (pl.col("float_shares") > 0)
        & (pl.col("close") > 0)
        & pl.col("mcap").is_not_null()
        & (pl.col("mcap") > 1e9)
    )
    X = X.with_columns((pl.col("close") / pl.col("o") - 1).alias("day_ret"))
    return X


# ===== 3. 正交化：控制量价水平后的残差 =====

def residualize(X: pl.DataFrame, target: str) -> pl.DataFrame:
    """按「当日涨幅 × 市值分位 × 换手分位」分组，取组内 z-score。

    零成本的正交化：同一 (day_ret 十分位, mcap 十分位, turnover 十分位) 内，
    把指标减去组内均值、除以组内标准差。这样剩下的就是「同涨幅、同规模、
    同换手」条件下仍有的结构差异。
    """
    d = X.with_columns([
        (pl.col("day_ret").rank("average") / pl.len() * 10).floor()
            .clip(0, 9).cast(pl.Int32).alias("_q_dr"),
        (pl.col("mcap").rank("average") / pl.len() * 10).floor()
            .clip(0, 9).cast(pl.Int32).alias("_q_mc"),
        (pl.col("turnover_rate").rank("average") / pl.len() * 10).floor()
            .clip(0, 9).cast(pl.Int32).alias("_q_to"),
        (pl.col("r_std").rank("average") / pl.len() * 10).floor()
            .clip(0, 9).cast(pl.Int32).alias("_q_vol"),
    ])
    g = ["_q_dr", "_q_mc", "_q_to", "_q_vol"]
    d = d.with_columns([
        pl.col(target).mean().over(g).alias("_m"),
        pl.col(target).std().over(g).alias("_s"),
    ])
    d = d.with_columns(
        pl.when(pl.col("_s") > 1e-12)
        .then((pl.col(target) - pl.col("_m")) / pl.col("_s"))
        .otherwise(0.0)
        .alias(f"{target}_res")
    )
    return d.drop(["_m", "_s"])


# ===== 4. 统计 =====

def bucket_report(X: pl.DataFrame, col: str, nq: int = 10) -> list[dict]:
    d = X.filter(pl.col(col).is_not_null())
    if d.height < 100:
        return []
    qs = [i / nq for i in range(1, nq)]
    cuts = [d[col].quantile(q) for q in qs]
    out = []
    for i in range(nq):
        lo = None if i == 0 else cuts[i - 1]
        hi = None if i == nq - 1 else cuts[i]
        s = d.filter(pl.col(col).is_not_null())
        if lo is not None:
            s = s.filter(pl.col(col) > lo)
        if hi is not None:
            s = s.filter(pl.col(col) <= hi)
        if s.height < 50:
            continue
        out.append({
            "bucket": i + 1,
            "lo": None if lo is None else round(float(lo), 6),
            "hi": None if hi is None else round(float(hi), 6),
            "n": s.height,
            "hit_lu5": round(float(s["hit_lu5"].mean()), 4),
            "fwd_ret5": round(float(s["fwd_ret5"].mean()), 6),
            "fwd_max5": round(float(s["fwd_max5"].mean()), 6),
            "win5": round(float((s["fwd_ret5"] > 0).mean()), 4),
            "big_win": round(float((s["fwd_ret5"] >= 0.10).mean()), 4),
            "max5_p90": round(float(s["fwd_max5"].quantile(0.90)), 6),
            "max5_p10": round(float(s["fwd_max5"].quantile(0.10)), 6),
        })
    return out


def main() -> None:
    mdates = sorted(x[5:] for x in os.listdir(MINUTE) if x.startswith("date="))
    mdates = [x for x in mdates if x >= START]
    print(f"minute days {len(mdates)}")
    fps = []
    for d in mdates:
        f = footprint(d)
        if f is not None and f.height:
            fps.append(f)
    fp = pl.concat(fps)
    print(f"footprint rows {fp.height}")
    X = build_panel(fp)
    print(f"panel rows {X.height} stocks {X['symbol'].n_unique()} days {X['date'].n_unique()}")

    X = X.filter(pl.col("fwd_ret5").is_not_null())
    print(f"with labels {X.height}")

    feats = ["net_bias", "close_pos", "tail_share", "open_share", "pulse",
             "max_r", "r_std", "updn", "amt_top", "vwap_dev"]
    for f in feats:
        X = residualize(X, f)
        X = X.with_columns(pl.col(f"{f}_res").fill_null(0.0).alias(f"{f}_res"))

    X.write_parquet(DAILY_PARQUET)

    res = {"window": [mdates[0], mdates[-1]], "n": X.height,
           "base": {
               "hit_lu5": round(float(X["hit_lu5"].mean()), 4),
               "fwd_ret5": round(float(X["fwd_ret5"].mean()), 6),
               "fwd_max5": round(float(X["fwd_max5"].mean()), 6),
               "win5": round(float((X["fwd_ret5"] > 0).mean()), 4),
               "big_win": round(float((X["fwd_ret5"] >= 0.10).mean()), 4),
           },
           "raw": {}, "res": {}}

    for f in feats:
        res["raw"][f] = bucket_report(X, f)
        res["res"][f] = bucket_report(X, f"{f}_res")

    # 只在底层（background/silent）里看
    low = X.filter(pl.col("level").is_in(["background", "silent"]))
    res["low_tier"] = {
        "n": low.height,
        "hit_lu5": round(float(low["hit_lu5"].mean()), 4),
        "fwd_ret5": round(float(low["fwd_ret5"].mean()), 6),
        "feats": {f: bucket_report(low, f"{f}_res", 5) for f in feats},
    }

    # 等级变化
    up = X.filter(pl.col("prev_level").is_not_null())
    rank = {lv: i for i, lv in enumerate(LEVEL_ORDER)}
    up = up.with_columns(
        pl.col("level").replace_strict(rank).alias("_r0"),
        pl.col("prev_level").replace_strict(rank).alias("_r1"),
    )
    upgraded = up.filter(pl.col("_r1") > pl.col("_r0"))
    res["level"] = {
        "upgrade_n": upgraded.height,
        "upgrade_hit_lu5": round(float(upgraded["hit_lu5"].mean()), 4),
        "upgrade_ret5": round(float(upgraded["fwd_ret5"].mean()), 6),
        "base_hit_lu5": round(float(up["hit_lu5"].mean()), 4),
        "base_ret5": round(float(up["fwd_ret5"].mean()), 6),
        "by_level": {
            lv: {"n": int(g.height),
                 "hit_lu5": round(float(g["hit_lu5"].mean()), 4),
                 "ret5": round(float(g["fwd_ret5"].mean()), 6)}
            for lv, g in X.group_by("level")
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(_json_safe(res), ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")

    print("\nbase", res["base"])
    for f in feats:
        b = res["res"][f]
        if b:
            top = b[-1]
            print(f"{f:12s} Q10 hit={top['hit_lu5']:.3f} ret5={top['fwd_ret5']:+.4f} "
                  f"bigw={top['big_win']:.3f} p90max={top['max5_p90']:+.3f} | "
                  f"Q1 hit={b[0]['hit_lu5']:.3f} ret5={b[0]['fwd_ret5']:+.4f}")
    print("\nlow tier n", res["low_tier"]["n"], "hit", res["low_tier"]["hit_lu5"])
    print("level", json.dumps(_json_safe(res["level"]["by_level"]), ensure_ascii=False))
    print("upgrade", res["level"]["upgrade_n"], res["level"]["upgrade_hit_lu5"],
          "vs base", res["level"]["base_hit_lu5"])


if __name__ == "__main__":
    main()
