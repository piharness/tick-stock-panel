"""盘中「提前进入」：在涨幅 3%/5%/7% 时买入，赌它当日封板，2 日内快进快出。

上一版脚本（`_diag_ignition_early_entry.py`）的「分钟异动」触发条件太窄
（要求开盘到触发涨幅 ≤3% 且量比 ≥5），101 天只抓到 1,088 次、其中仅 58 次
当日封板，覆盖不到「真的会涨停」的票。本脚本换成用户实际描述的动作：

    「看到它拉起来了 → 在还没涨停的时候买进去 → 赌它封板 → 2 天不涨就跑」

于是问题被拆成两个可直接回答的量：

  1. 在 +x% 买入、当日封板的概率是多少（= 这个赌局的胜率）；
  2. 封板与不封板两组的 2 日收益、左尾（MAE、跌超 5%/8%）分别是什么。

口径（写死）
------------
- 分钟线 2026-06-18 ~ 最新（原始价）。`cum = close/prev_raw - 1`，prev_raw 取自
  日线 `raw_close.shift(1)`；首次 `cum >= x` 的分钟为触发点，入场 = **下一分钟开盘**。
- 每个 (symbol, date) 只取首次触及，避免同一波重复计数。
- T 日剩余路径用分钟线；T+1 / T+2 用日线 `raw_high/raw_low/raw_close`（同为原始价，
  2 日内分红影响可忽略）。
- 出场：止损 -3%（优先判定，保守）→ 止盈 +5% → 第 2 日收盘平仓。
- 排除 ST、北交所；要求触发时有成交额。
- 分板块：主板 10%、创业板/科创板 20%，所以「买入价 → 涨停价」的空间不同，
  报告里按板块分开给。

输出：`data/.scratch/early_entry_threshold.json`
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

DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"
MINUTE = DATA / "kline_minute"
OUT = DATA / ".scratch" / "early_entry_threshold.json"

THRESHOLDS = [0.03, 0.05, 0.07]
STOPS = [-0.02, -0.03, -0.05]
TPS = [0.03, 0.05, 0.08]
HOLD_DAYS = 2
BOARD_OF = {"60": "主板", "00": "主板", "30": "创业板", "68": "科创板"}
LIMIT_OF = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20}


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
        "p10": round(_quant(clean, 0.10), 6),
        "p05": round(_quant(clean, 0.05), 6),
        "p01": round(_quant(clean, 0.01), 6),
        "t": round(t, 2) if math.isfinite(t) else None,
        "win": round(sum(1 for x in clean if x > 0) / len(clean), 4),
    }


def _date_t(rows, key):
    by_day = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        by_day.setdefault(r["date"], []).append(float(v))
    daily = [sum(v) / len(v) for v in by_day.values()]
    m, t = _tstat(daily)
    return {"days": len(daily), "mean": round(m, 6) if math.isfinite(m) else None,
            "t_date": round(t, 2) if math.isfinite(t) else None}


def _json_safe(o):
    if isinstance(o, dict):
        return {(k if isinstance(k, (str, int, float, bool)) or k is None else str(k)):
                _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


# ===== 日线面板 =====

def load_daily():
    names = sorted(d[5:] for d in os.listdir(ENRICHED) if d.startswith("date="))
    dates = [d for d in names if d >= "2026-06-01"]
    cols = ["symbol", "date", "raw_close", "raw_high", "raw_low", "amount",
            "turnover_rate", "consecutive_limit_ups"]
    df = pl.concat([
        pl.read_parquet(f"{ENRICHED}/date={d}/part.parquet", columns=cols) for d in dates
    ]).sort(["symbol", "date"])
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet",
                           columns=["symbol", "name", "float_shares"])
    df = df.join(inst, on="symbol", how="left").with_columns(
        pl.col("name").fill_null(""),
        pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"),
        pl.col("raw_high").shift(-1).over("symbol").alias("h1"),
        pl.col("raw_low").shift(-1).over("symbol").alias("l1"),
        pl.col("raw_close").shift(-1).over("symbol").alias("c1"),
        pl.col("raw_high").shift(-2).over("symbol").alias("h2"),
        pl.col("raw_low").shift(-2).over("symbol").alias("l2"),
        pl.col("raw_close").shift(-2).over("symbol").alias("c2"),
        (pl.col("consecutive_limit_ups").shift(-1).over("symbol") > 0).alias("lu_t1"),
        (pl.col("consecutive_limit_ups").shift(-2).over("symbol") > 0).alias("lu_t2"),
    )
    return dates, df


# ===== 扫分钟线：首次触及 x% =====

def scan_touch(dates: list[str], daily: pl.DataFrame) -> pl.DataFrame:
    keep = daily.filter(
        ~pl.col("name").str.to_uppercase().str.contains("ST", literal=True)
        & (pl.col("float_shares") > 0)
        & (pl.col("amount") > 0)
        & (pl.col("prev_raw") > 0)
    ).select([
        "symbol", "date", "prev_raw", "raw_close", "consecutive_limit_ups",
        "h1", "l1", "c1", "h2", "l2", "c2", "lu_t1", "lu_t2", "turnover_rate",
    ])
    keep = keep.with_columns(
        pl.col("symbol").str.slice(0, 2).pipe(
            lambda s: s.is_in(list(BOARD_OF))
        ).alias("_ok")
    ).filter(pl.col("_ok")).drop("_ok")

    out = []
    for d in dates:
        p = f"{MINUTE}/date={d}/part.parquet"
        if not os.path.exists(p):
            continue
        day = daily.filter(pl.col("date") == __import__("datetime").date.fromisoformat(d))
        if not day.height:
            continue
        ref = keep.filter(pl.col("date") == day["date"][0])
        if not ref.height:
            continue
        m = pl.read_parquet(p).filter(pl.col("symbol").is_in(ref["symbol"].to_list()))
        if not m.height:
            continue
        m = m.sort(["symbol", "datetime"]).join(ref.drop("date"), on="symbol", how="inner")
        m = m.with_columns(pl.lit(d).alias("date"))
        m = m.with_columns(
            (pl.int_range(pl.len()).over("symbol") + 1).alias("mi"),
            (pl.col("close") / pl.col("prev_raw") - 1).alias("cum"),
            (pl.col("open").first().over("symbol") / pl.col("prev_raw") - 1).alias("gap_open"),
            (pl.col("amount") / pl.col("amount").rolling_mean(20).over("symbol")
             .shift(1).over("symbol")).alias("amt_ratio"),
            (pl.col("close") / pl.col("open").first().over("symbol") - 1).alias("run_pre"),
            pl.col("open").shift(-1).over("symbol").alias("entry"),
            pl.col("high").cum_max(reverse=True).over("symbol").alias("_rev_hi"),
            pl.col("low").cum_min(reverse=True).over("symbol").alias("_rev_lo"),
            pl.col("close").last().over("symbol").alias("_day_close"),
        )
        m = m.with_columns(
            pl.col("_rev_hi").shift(-1).over("symbol").alias("hi_after"),
            pl.col("_rev_lo").shift(-1).over("symbol").alias("lo_after"),
        )
        for x in THRESHOLDS:
            hit = m.filter((pl.col("cum") >= x) & (pl.col("entry") > 0))
            if not hit.height:
                continue
            first = hit.group_by("symbol").agg(pl.col("mi").min().alias("mi0"))
            hit = hit.join(first, on="symbol").filter(pl.col("mi") == pl.col("mi0")).drop("mi0")
            hit = hit.with_columns(pl.lit(float(x)).alias("thr"))
            out.append(hit.select([
                "symbol", "date", "mi", "datetime", "thr", "entry", "cum",
                "gap_open", "amt_ratio", "run_pre",
                "hi_after", "lo_after", "_day_close", "prev_raw", "raw_close",
                "consecutive_limit_ups", "h1", "l1", "c1", "h2", "l2", "c2",
                "lu_t1", "lu_t2", "turnover_rate",
            ]))
    return pl.concat(out) if out else pl.DataFrame()


# ===== 规则回放（向量化） =====

def replay(e: pl.DataFrame) -> pl.DataFrame:
    e = e.with_columns(
        pl.col("date").cast(pl.Utf8),
        pl.col("symbol").str.slice(0, 2).replace_strict(
            {k: v for k, v in BOARD_OF.items()}, default="其他"
        ).alias("board"),
    )
    e = e.with_columns(
        pl.col("board").replace_strict(LIMIT_OF, default=0.10).alias("limit_pct")
    )
    e = e.with_columns(
        pl.min_horizontal(
            pl.col("lo_after").fill_null(1e9), pl.col("l1").fill_null(1e9),
            pl.col("l2").fill_null(1e9),
        ).alias("mae_px"),
        pl.max_horizontal(
            pl.col("hi_after").fill_null(-1e9), pl.col("h1").fill_null(-1e9),
            pl.col("h2").fill_null(-1e9),
        ).alias("mfe_px"),
        pl.col("c2").fill_null(pl.col("c1")).fill_null(pl.col("_day_close")).alias("exit_px"),
    )
    e = e.with_columns(
        (pl.col("mae_px") / pl.col("entry") - 1).alias("mae"),
        (pl.col("mfe_px") / pl.col("entry") - 1).alias("mfe"),
        (pl.col("exit_px") / pl.col("entry") - 1).alias("hold_ret"),
        (pl.col("_day_close") / pl.col("entry") - 1).alias("e2c"),
        (pl.col("raw_close") / pl.col("entry") - 1).alias("e2c_day"),
        (pl.col("consecutive_limit_ups") > 0).alias("sealed"),
        # 触发时相对涨停价还有多少空间
        ((pl.col("prev_raw") * (1 + pl.col("limit_pct"))) / pl.col("entry") - 1).alias("room_to_limit"),
    )
    for st in STOPS:
        for tp in TPS:
            tag = f"s{int(-st*100)}_t{int(tp*100)}"
            stopped = pl.col("mae") <= st
            tp_hit = (~stopped) & (pl.col("mfe") >= tp)
            e = e.with_columns(
                pl.when(stopped).then(pl.lit(st))
                .when(tp_hit).then(pl.lit(tp))
                .otherwise(pl.col("hold_ret")).alias(f"r_{tag}")
            )
    return e


# ===== 统计 =====

def seg_stats(sub: pl.DataFrame) -> dict:
    if not sub.height:
        return {"n": 0}
    rows = sub.to_dicts()
    out = {
        "n": sub.height,
        "days": sub["date"].n_unique(),
        "seal_rate": round(float(sub["sealed"].mean()), 4),
        "lu_t1_rate": round(float(sub["lu_t1"].fill_null(False).mean()), 4),
        "e2c": _stat(sub["e2c"].to_list()),
        "mae": _stat(sub["mae"].to_list()),
        "mfe": _stat(sub["mfe"].to_list()),
        "hold": _stat(sub["hold_ret"].to_list()),
        "down3_rate": round(float((sub["mae"] <= -0.03).mean()), 4),
        "down5_rate": round(float((sub["mae"] <= -0.05).mean()), 4),
        "down8_rate": round(float((sub["mae"] <= -0.08).mean()), 4),
        "room_to_limit": _stat(sub["room_to_limit"].to_list()),
        "seal_by_hour": {},
    }
    for tag in (f"s{int(-st*100)}_t{int(tp*100)}" for st in STOPS for tp in TPS):
        col = f"r_{tag}"
        out[tag] = {
            "mean": round(float(sub[col].mean()), 6),
            "win": round(float((sub[col] > 0).mean()), 4),
            "stop_rate": round(float((sub["mae"] <= float(tag.split("_")[0][1:]) / -100).mean()), 4)
            if False else None,
            "t_date": _date_t(rows, col)["t_date"],
        }
    return out


def main() -> None:
    dates, daily = load_daily()
    mdates = sorted(d[5:] for d in os.listdir(MINUTE) if d.startswith("date="))
    mdates = [x for x in mdates if x in set(dates)]
    print(f"minute days {len(mdates)} {mdates[0]}..{mdates[-1]}")
    ev = scan_touch(mdates, daily)
    print(f"touch events {ev.height}")
    if not ev.height:
        return
    sim = replay(ev)
    sim.write_parquet(DATA / ".scratch" / "early_entry_events.parquet")
    print("sim", sim.shape)

    res = {"window": [mdates[0], mdates[-1]], "by_threshold": {}}
    for x in THRESHOLDS:
        sub = sim.filter(pl.col("thr") == x)
        blk = {"all": seg_stats(sub), "by_board": {}, "by_sealed": {}, "by_month": {}}
        for b in ("主板", "创业板", "科创板"):
            blk["by_board"][b] = seg_stats(sub.filter(pl.col("board") == b))
        blk["by_sealed"] = {
            "封板": seg_stats(sub.filter(pl.col("sealed"))),
            "未封板": seg_stats(sub.filter(~pl.col("sealed"))),
        }
        for ym, g in sub.with_columns(pl.col("date").str.slice(0, 7).alias("ym")).group_by("ym"):
            blk["by_month"][str(ym)] = {
                "n": g.height,
                "seal_rate": round(float(g["sealed"].mean()), 4),
                "s3_t5": round(float(g["r_s3_t5"].mean()), 6),
                "mae_med": round(float(g["mae"].median()), 6),
            }
        res["by_threshold"][f"{int(x*100)}pct"] = blk

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(_json_safe(res), ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")

    for x in THRESHOLDS:
        a = res["by_threshold"][f"{int(x*100)}pct"]["all"]
        print(f"\n=== 触及 +{int(x*100)}% ===  n={a['n']} days={a['days']}")
        print(f"  当日封板率 {a['seal_rate']:.3f}  次日涨停率 {a['lu_t1_rate']:.3f}")
        print(f"  触发→收盘 {a['e2c']['median']:+.4f} (mean {a['e2c']['mean']:+.4f})")
        print(f"  距涨停空间 median {a['room_to_limit']['median']:+.4f}")
        print(f"  MAE med {a['mae']['median']:+.4f} p10 {a['mae']['p10']:+.4f}  跌超5% {a['down5_rate']:.3f} 跌超8% {a['down8_rate']:.3f}")
        print(f"  MFE med {a['mfe']['median']:+.4f}")
        for st in STOPS:
            for tp in TPS:
                tag = f"s{int(-st*100)}_t{int(tp*100)}"
                r = a[tag]
                print(f"    止损{int(st*100)}% 止盈{tp*100}%: mean {r['mean']:+.4f} win {r['win']:.3f} t={r['t_date']}")
        for b, v in res["by_threshold"][f"{int(x*100)}pct"]["by_board"].items():
            if v.get("n"):
                print(f"    [{b}] n={v['n']} 封板率 {v['seal_rate']:.3f} s3t5 {v['s3_t5']['mean']:+.4f} mae_med {v['mae']['median']:+.4f}")


if __name__ == "__main__":
    main()
