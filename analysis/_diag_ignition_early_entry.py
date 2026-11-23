"""游资「盘中点火」启动形态：提前介入 + 2 日快进快出（分钟级可成交回测）。

为什么要重做
------------
上一版（`_diag_limitup_citizenship.py`）回答的是「已经 4 板之后会怎样」，
入场点在第 4 板当天或 T+1 开盘——那已经不是「提前进入」。本脚本把决策点
前移到**点火发生的那一分钟**，用下一分钟开盘价成交，衡量：

1. 触发时买入价相对当日收盘（若封板即涨停价）便宜多少 → 成本优势；
2. 只赌 2 个交易日：+X% 止盈 / -Y% 止损 / 到期平仓 的实际期望；
3. 赌错时的左尾有多厚（MAE、跌超 5%/8% 概率）——直接检验
   「一般个股没有意外不会剧烈下跌」这个假设。

口径（写死，不随结果调整）
--------------------------
- 数据：`data/kline_minute`（2026-06-18 ~ 最新，101 个交易日，1 分钟，原始价）。
- 触发：某分钟同时满足
    a 该分钟涨幅 ≥ 2.0%（close vs 前一根 close）
    b 该分钟成交额 ≥ 过去 20 分钟均额的 5 倍
    c 该分钟收盘创当日新高（严格高于此前所有分钟 high）
    d 当日开盘至触发前累计涨幅 ≤ 3%（是「启动」不是「半山腰」）
    e 触发时当日涨幅（相对前收）≤ 7%（还没到涨停，成本可低于涨停价）
    f 时间在 09:35 之后（避开开盘噪音）
    g 非 ST、非北交所
- 每个 (symbol, date) 只取**当日第一次**触发，避免同一波拉升重复计数。
- 入场：触发分钟的**下一分钟开盘价**（不是触发价，保守）。
- 出场（主口径 A）：入场后 2 个交易日内
    · 分钟 low ≤ 入场价×(1+STOP) → 止损，按该分钟收盘价成交（保守，不给最优价）
    · 分钟 high ≥ 入场价×(1+TP)  → 止盈，按该分钟收盘价成交
    · 同一分钟两者都触 → 按止损（保守）
    · 到期（第 2 个交易日收盘）→ 收盘价平仓
- 口径 B：3 个交易日，同规则，用于看「多等一天」是否更好。
- 时间止损口径 C：不设止盈，只在第 2 日收盘平仓，看纯持有。

输出：`data/.scratch/ignition_early_entry.json` + `ignition_events.parquet`
只读数据，不改后端/前端。
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"
MINUTE = DATA / "kline_minute"
OUT = DATA / ".scratch" / "ignition_early_entry.json"
EVENTS_PARQUET = DATA / ".scratch" / "ignition_events.parquet"

# ---- 触发阈值（主口径） ----
MIN_MINS = 5               # 09:35 起
UP_1M = 0.020              # 单分钟涨幅
AMT_RATIO = 5.0            # 相对前 20 分钟均额
PRE_RUN_MAX = 0.030        # 开盘到触发前累计涨幅上限
DAY_GAIN_MAX = 0.070       # 触发时当日涨幅上限
STOP = -0.03               # 止损
TPS = [0.04, 0.05, 0.06, 0.08]
HOLD_DAYS = 2              # 主口径持有交易日数（含入场日）
HOLD_DAYS_ALT = 3

BOARD_OF = {"60": "主板", "00": "主板", "30": "创业板", "68": "科创板"}


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


def _date_stat(rows, key):
    by_day = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        by_day.setdefault(r["date"], []).append(float(v))
    daily = [sum(v) / len(v) for v in by_day.values()]
    m, t = _tstat(daily)
    return {
        "n": len(rows),
        "days": len(daily),
        "mean": round(m, 6) if math.isfinite(m) else None,
        "t_date": round(t, 2) if math.isfinite(t) else None,
    }


def _board(symbol: str) -> str | None:
    return BOARD_OF.get(symbol[:2])


# ===== 1. 日线基准 =====

def load_daily():
    names = sorted(d[5:] for d in os.listdir(ENRICHED) if d.startswith("date="))
    dates = [d for d in names if d >= "2026-06-01"]
    cols = ["symbol", "date", "open", "close", "raw_close", "amount",
            "turnover_rate", "consecutive_limit_ups"]
    df = pl.concat([
        pl.read_parquet(f"{ENRICHED}/date={d}/part.parquet", columns=cols) for d in dates
    ]).sort(["symbol", "date"])
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet",
                           columns=["symbol", "name", "float_shares"])
    df = df.join(inst, on="symbol", how="left")
    df = df.with_columns(
        pl.col("name").fill_null("").alias("name"),
        pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"),
    )
    return dates, df


# ===== 2. 扫分钟线，产事件 =====

def scan_events(dates: list[str], daily: pl.DataFrame) -> pl.DataFrame:
    keep = daily.filter(
        ~pl.col("name").str.to_uppercase().str.contains("ST", literal=True)
        & (pl.col("float_shares") > 0)
        & (pl.col("amount") > 0)
    ).select(["symbol", "date", "prev_raw", "raw_close", "consecutive_limit_ups", "name"])
    keep = keep.with_columns(
        pl.col("symbol").str.slice(0, 2).is_in(list(BOARD_OF)).alias("_ok_board")
    ).filter(pl.col("_ok_board")).drop("_ok_board")

    out = []
    for d in dates:
        p = f"{MINUTE}/date={d}/part.parquet"
        if not os.path.exists(p):
            continue
        m = pl.read_parquet(p).sort(["symbol", "datetime"])
        day_key = date.fromisoformat(d)
        ref = keep.filter(pl.col("date") == day_key).select(
            ["symbol", "prev_raw", "raw_close", "consecutive_limit_ups"]
        )
        if not ref.height:
            continue
        m = m.join(ref, on="symbol", how="inner")
        if not m.height:
            continue
        m = m.with_columns(
            pl.lit(day_key).alias("date"),
            (pl.int_range(pl.len()).over("symbol") + 1).alias("mi"),
            pl.col("close").shift(1).over("symbol").alias("_pc"),
            pl.col("amount").rolling_mean(20).over("symbol").shift(1).over("symbol").alias("_amt20"),
            pl.col("high").cum_max().over("symbol").shift(1).over("symbol").alias("_hi_prev"),
            pl.col("open").first().over("symbol").alias("_day_open"),
        )
        m = m.with_columns(
            (pl.col("close") / pl.col("_pc") - 1).alias("r1m"),
            (pl.col("amount") / pl.col("_amt20")).alias("amt_ratio"),
            (pl.col("close") / pl.col("_day_open") - 1).alias("run_pre"),
            (pl.col("close") / pl.col("prev_raw") - 1).alias("day_gain"),
        )
        sig = m.filter(
            (pl.col("mi") >= MIN_MINS)
            & (pl.col("prev_raw") > 0)
            & (pl.col("r1m") >= UP_1M)
            & (pl.col("amt_ratio") >= AMT_RATIO)
            & (pl.col("close") > pl.col("_hi_prev"))
            & (pl.col("run_pre") <= PRE_RUN_MAX)
            & (pl.col("day_gain") <= DAY_GAIN_MAX)
        )
        if not sig.height:
            continue
        first = sig.group_by("symbol").agg(pl.col("mi").min().alias("mi"))
        sig = sig.join(first, on="symbol").filter(pl.col("mi") == pl.col("mi_right")).drop("mi_right")
        nxt = m.select(
            pl.col("symbol"), (pl.col("mi") - 1).alias("mi"),
            pl.col("open").alias("entry_open"),
        )
        sig = sig.join(nxt, on=["symbol", "mi"], how="left")
        sig = sig.filter(pl.col("entry_open").is_not_null() & (pl.col("entry_open") > 0))
        if not sig.height:
            continue
        out.append(sig.select([
            "symbol", "date", "mi", "datetime", "close", "entry_open", "amt_ratio",
            "r1m", "run_pre", "day_gain", "raw_close", "consecutive_limit_ups", "prev_raw",
        ]))
    if not out:
        return pl.DataFrame()
    return pl.concat(out)


# ===== 3. 逐事件走 2 日路径 =====

def simulate(events: pl.DataFrame, dates: list[str]) -> pl.DataFrame:
    if not events.height:
        return events
    ev = events.with_columns(pl.col("date").cast(pl.Utf8))
    # 按 (date, symbol) 建索引，方便逐日读分钟线
    by_date: dict[str, list[dict]] = {}
    for r in ev.iter_rows(named=True):
        by_date.setdefault(r["date"], []).append(r)

    date_pos = {d: i for i, d in enumerate(dates)}
    rows = []
    for d, evs in by_date.items():
        i = date_pos.get(d)
        if i is None:
            continue
        horizon = dates[i:i + HOLD_DAYS_ALT]   # 入场日 + 后续 2 日
        syms = {e["symbol"] for e in evs}
        mins: dict[str, dict[str, pl.DataFrame]] = {}
        for dd in horizon:
            p = f"{MINUTE}/date={dd}/part.parquet"
            if not os.path.exists(p):
                continue
            sub = pl.read_parquet(p).select(
                ["symbol", "datetime", "open", "high", "low", "close"]
            ).filter(pl.col("symbol").is_in(list(syms))).sort(["symbol", "datetime"])
            mins[dd] = {
                (k[0] if isinstance(k, tuple) else k): v
                for k, v in sub.partition_by("symbol", as_dict=True).items()
            }
        for e in evs:
            s = e["symbol"]
            entry = e["entry_open"]
            path = []          # (date_idx, open, high, low, close)
            for k, dd in enumerate(horizon):
                if dd not in mins or s not in mins[dd]:
                    continue
                sub = mins[dd][s]
                if k == 0:
                    sub = sub.filter(pl.col("datetime") > e["datetime"])
                if not sub.height:
                    continue
                for r in sub.iter_rows(named=True):
                    path.append((k, r["open"], r["high"], r["low"], r["close"]))
            if not path:
                continue
            rec = dict(e)
            rec["board"] = _board(s)
            rec["entry_to_close"] = e["raw_close"] / entry - 1
            # 路径极值
            highs = [p[2] for p in path]
            lows = [p[3] for p in path]
            rec["mfe"] = max(highs) / entry - 1
            rec["mae"] = min(lows) / entry - 1
            rec["ret_close1"] = path[0][4] / entry - 1 if path else None
            # 到期收盘（最后一个有效日的最后一根）
            rec["ret_expiry"] = path[-1][4] / entry - 1
            rec["days_avail"] = path[-1][0] + 1
            # 规则回放：逐根判断
            for tp in TPS:
                pu = entry * (1 + tp)
                sl = entry * (1 + STOP)
                hit = None
                for (k, o, h, l, c) in path:
                    if l <= sl:
                        hit = (STOP, k, c / entry - 1, "stop")
                        break
                    if h >= pu:
                        hit = (tp, k, c / entry - 1, "tp")
                        break
                if hit is None:
                    hit = (None, path[-1][0], path[-1][4] / entry - 1, "expiry")
                rec[f"tp{int(tp*100)}_ret"] = hit[2]
                rec[f"tp{int(tp*100)}_kind"] = hit[3]
                rec[f"tp{int(tp*100)}_day"] = hit[1]
            # 纯持有到到期（无止盈止损）
            rec["hold_expiry"] = rec["ret_expiry"]
            rows.append(rec)
    return pl.DataFrame(rows)


# ===== 4. 统计 =====

def summarize(df: pl.DataFrame) -> dict:
    if not df.height:
        return {"n": 0}
    d = df.with_columns(
        (pl.col("consecutive_limit_ups") > 0).alias("sealed_at_trigger_day")
    )
    out = {"n": d.height, "days": d["date"].n_unique()}

    def seg(sub: pl.DataFrame) -> dict:
        r = {"n": sub.height}
        for tp in TPS:
            k = f"tp{int(tp*100)}"
            r[k] = {
                "mean": round(float(sub[f"{k}_ret"].mean()), 6),
                "win": round(float((sub[f"{k}_ret"] > 0).mean()), 4),
                "tp_rate": round(float((sub[f"{k}_kind"] == "tp").mean()), 4),
                "stop_rate": round(float((sub[f"{k}_kind"] == "stop").mean()), 4),
                "expiry_rate": round(float((sub[f"{k}_kind"] == "expiry").mean()), 4),
                "t_date": _date_stat(sub.to_dicts(), f"{k}_ret")["t_date"],
            }
        r["mfe"] = _stat(sub["mfe"].to_list())
        r["mae"] = _stat(sub["mae"].to_list())
        r["hold_expiry"] = _stat(sub["hold_expiry"].to_list())
        r["ret_close1"] = _stat(sub["ret_close1"].to_list())
        r["entry_to_close"] = _stat(sub["entry_to_close"].to_list())
        r["down5_rate"] = round(float((sub["mae"] <= -0.05).mean()), 4)
        r["down8_rate"] = round(float((sub["mae"] <= -0.08).mean()), 4)
        r["limit_down_rate"] = 0.0
        return r

    out["all"] = seg(d)
    for tp in (5,):
        k = f"tp{tp}"
        out[f"tp{tp}_by_sealed"] = {
            "封板": seg(d.filter(pl.col("sealed_at_trigger_day"))),
            "未封板": seg(d.filter(~pl.col("sealed_at_trigger_day"))),
        }
        out[f"tp{tp}_by_board"] = {
            b: seg(d.filter(pl.col("board") == b)) for b in ("主板", "创业板", "科创板")
        }
    # 按月稳定性
    d = d.with_columns(pl.col("date").str.slice(0, 7).alias("ym"))
    out["by_month"] = {
        ym: {
            "n": g.height,
            "tp5_mean": round(float(g["tp5_ret"].mean()), 6),
            "tp5_win": round(float((g["tp5_ret"] > 0).mean()), 4),
            "mae_med": round(float(g["mae"].median()), 6),
        }
        for ym, g in d.group_by("ym", maintain_order=True)
    }
    # 触发时刻分布
    d = d.with_columns(
        (pl.col("datetime").dt.hour().cast(pl.Int32) * 60
         + pl.col("datetime").dt.minute().cast(pl.Int32)).alias("mins")
    )
    out["trigger_hour"] = {
        "09:35-10:00": int(((d["mins"] <= 600) & (d["mins"] >= 575)).sum()),
        "10:00-11:30": int(((d["mins"] > 600) & (d["mins"] <= 690)).sum()),
        "13:00-14:30": int(((d["mins"] > 780) & (d["mins"] <= 870)).sum()),
        "14:30-15:00": int((d["mins"] > 870).sum()),
    }
    out["amt_ratio"] = _stat(d["amt_ratio"].to_list())
    out["day_gain_at_trigger"] = _stat(d["day_gain"].to_list())
    return out


def _json_safe(o):
    if isinstance(o, dict):
        return {(k if isinstance(k, (str, int, float, bool)) or k is None else str(k)):
                _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


def main() -> None:
    dates, daily = load_daily()
    mdates = sorted(d[5:] for d in os.listdir(MINUTE) if d.startswith("date="))
    mdates = [x for x in mdates if x in set(dates)]
    print(f"minute days {len(mdates)} {mdates[0]}..{mdates[-1]}")
    ev = scan_events(mdates, daily)
    print(f"events {ev.height}")
    if not ev.height:
        return
    sim = simulate(ev, mdates)
    print(f"simulated {sim.height}")
    sim.write_parquet(EVENTS_PARQUET)
    res = {"window": [mdates[0], mdates[-1]], "summary": summarize(sim)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(_json_safe(res), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    s = res["summary"]
    print("n", s["n"], "days", s["days"])
    for tp in TPS:
        k = f"tp{int(tp*100)}"
        print(k, s["all"][k])
    print("mae", s["all"]["mae"])
    print("mfe", s["all"]["mfe"])
    print("entry_to_close median", s["all"]["entry_to_close"]["median"])
    print("down5", s["all"]["down5_rate"], "down8", s["all"]["down8_rate"])
    print("sealed split", s["tp5_by_sealed"]["封板"]["n"], s["tp5_by_sealed"]["未封板"]["n"])
    print("by board", {k: v["n"] for k, v in s["tp5_by_board"].items()})
    print("by month", json.dumps(_json_safe(s["by_month"]), ensure_ascii=False))
    print("trigger hour", s["trigger_hour"])


if __name__ == "__main__":
    main()
