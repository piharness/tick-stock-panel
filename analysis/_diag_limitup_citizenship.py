"""近 20 日 ≥4 个涨停的个股：公民等级变化 + 盘中点火特征（描述性诊断）。

口径（写死，不随结果调整）
--------------------------
- 涨停：`kline_daily_enriched.consecutive_limit_ups > 0`。该列由 pipeline 用
  「raw_close ≥ 交易所涨停价（含 0.005 容差）」递推得到；本脚本另用
  `price_limits` 重算并核对（见 `verify_limit_flag`）。
- 20 日窗口：交易日网格上的 20 行（先把 symbol × 全市场交易日补成网格，
  停牌日 lu=0），不是「最近 20 条行情」（母题 4：shift 数行不数天）。
- 入池事件：lu20(T) ≥ 4 且 lu20(T-1) < 4，即「第 4 个涨停当天」首次进池。
- 公民等级：完全复用 `services.citizenship` 的 `_quantile_rank` / `_assign_level`，
  只用当日横截面，保证与页面一致。等级 rank：core=0 … silent=3（越小越高）。
- 盘中特征：1 分钟线（2026-06-18 起）。分钟价是**原始价**（已与 raw_close 对齐）。
- 前瞻收益：enriched 前复权 close/open；跨除权日仍可比。
- 可成交：T+1 原始开盘价 < T+1 涨停价（否则一字板买不进）。

只读数据，写 `data/.scratch/limitup_citizenship.json`。
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.price_limits import (  # noqa: E402
    polars_limit_price,
    polars_price_limit_pct,
    price_limit_pct,
)
from app.services.citizenship import (  # noqa: E402
    LEVEL_ORDER,
    _assign_level,
    _quantile_rank,
)

DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"
MINUTE = DATA / "kline_minute"
LHB = DATA / "dragon_tiger"
OUT = DATA / ".scratch" / "limitup_citizenship.json"

LOOKBACK = 20
MIN_LIMIT_UPS = 4
START = "2026-02-02"          # 留出 20 日回看 + 事件前轨迹
OPEN_MIN = 9 * 60 + 30        # 09:30
MORNING_END = 10 * 60         # 10:00（含 09:31–10:00 共 30 根）


# ===== 工具 =====

def _dates() -> list[str]:
    names = sorted(d[5:] for d in os.listdir(ENRICHED) if d.startswith("date="))
    return [d for d in names if d >= START]


def verify_limit_flag(panel: pl.DataFrame, inst: pl.DataFrame) -> dict:
    """重算涨停价，核对 consecutive_limit_ups>0 与规则判定是否一致。"""
    df = panel.join(inst.select(["symbol", "name"]), on="symbol", how="left").sort(
        ["symbol", "date"]
    )
    df = df.with_columns(pl.col("raw_close").shift(1).over("symbol").alias("_prev_raw"))
    df = df.with_columns(
        polars_price_limit_pct(
            pl.col("symbol"),
            pl.col("date"),
            pl.col("name").fill_null("").str.to_uppercase().str.contains("ST", literal=True),
        ).alias("_lim_pct")
    ).with_columns(polars_limit_price(pl.col("_prev_raw"), pl.col("_lim_pct"), up=True).alias("_lim"))
    chk = df.filter(pl.col("_prev_raw").is_not_null() & (pl.col("_prev_raw") > 0))
    chk = chk.with_columns(
        (pl.col("raw_close") >= pl.col("_lim") - 0.005).alias("_calc"),
        (pl.col("consecutive_limit_ups") > 0).alias("_stored"),
    )
    return {
        "rows": chk.height,
        "agree": round(float(chk.select((pl.col("_calc") == pl.col("_stored")).mean()).item()), 6),
        "calc_limit_ups": int(chk["_calc"].sum()),
        "stored_limit_ups": int(chk["_stored"].sum()),
    }


def _tstat(xs: list[float]) -> tuple[float, float]:
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    if len(xs) < 3:
        return float("nan"), float("nan")
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    se = math.sqrt(var / len(xs))
    return m, (m / se if se > 0 else float("nan"))


def _stat(xs: list[float]) -> dict:
    clean = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if not clean:
        return {"n": 0, "mean": None, "median": None, "t": None, "win": None}
    clean.sort()
    m, t = _tstat(clean)
    med = clean[len(clean) // 2]
    return {
        "n": len(clean),
        "mean": round(m, 6),
        "median": round(med, 6),
        "t": round(t, 2) if math.isfinite(t) else None,
        "win": round(sum(1 for x in clean if x > 0) / len(clean), 4),
    }


def _date_stat(rows: list[dict], key: str) -> dict:
    """按日聚类的 t（同一交易日的事件不独立，避免把 N 当自由度）。"""
    by_day: dict[str, list[float]] = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        by_day.setdefault(r["date"], []).append(float(v))
    daily = [sum(v) / len(v) for v in by_day.values()]
    m, t = _tstat(daily)
    return {
        "n": len(rows) if rows else 0,
        "days": len(daily),
        "mean": round(m, 6) if math.isfinite(m) else None,
        "t_date": round(t, 2) if math.isfinite(t) else None,
    }


def _level_rank(level: str | None) -> int | None:
    if level is None:
        return None
    return {lv: i for i, lv in enumerate(LEVEL_ORDER)}.get(level)


def _limit_pct(symbol: str, d: date, name: str | None) -> float:
    """板块 + ST 时点规则（ST 在 2026-07-06 前后 5%/10% 不同，不能按板块写死）。"""
    return price_limit_pct(
        symbol, d, is_risk_warning=bool(name) and "ST" in str(name).upper()
    )


# ===== 主流程 =====

def main() -> None:
    dates = _dates()
    date_idx = {d: i for i, d in enumerate(dates)}
    inst = pl.read_parquet(
        DATA / "instruments" / "instruments.parquet",
        columns=["symbol", "name", "float_shares", "total_shares", "listing_date"],
    )

    cols = [
        "symbol", "date", "open", "high", "low", "close",
        "raw_close", "raw_high", "raw_low", "amount", "turnover_rate",
        "consecutive_limit_ups",
    ]
    panel = pl.concat(
        [pl.read_parquet(ENRICHED / f"date={d}" / "part.parquet", columns=cols) for d in dates]
    ).sort(["symbol", "date"])

    print(f"[data] {panel.height:,} rows, {panel['symbol'].n_unique()} symbols, {len(dates)} days")
    print("[check] limit flag:", verify_limit_flag(panel, inst))

    # ---- 交易日网格：停牌日补 lu=0，rolling 20 才是 20 个交易日 ----
    symbols = panel["symbol"].unique().sort().to_list()
    grid = pl.DataFrame({"date": [datetime.fromisoformat(d).date() for d in dates]}).join(
        pl.DataFrame({"symbol": symbols}), how="cross"
    )
    grid = grid.join(panel, on=["symbol", "date"], how="left")
    grid = grid.with_columns(pl.col("consecutive_limit_ups").fill_null(0))
    grid = grid.with_columns(
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int8).alias("lu")
    ).sort(["symbol", "date"])
    grid = grid.with_columns(
        pl.col("lu").rolling_sum(LOOKBACK).over("symbol").alias("lu20"),
        pl.col("lu").shift(1).over("symbol").alias("lu_prev"),
    )
    grid = grid.with_columns(
        pl.col("lu20").shift(1).over("symbol").alias("lu20_prev")
    )

    # ---- 公民等级（与 services.citizenship 同口径）----
    lv = (
        panel.join(inst.select(["symbol", "name", "float_shares"]), on="symbol", how="left")
        .filter(
            (pl.col("close") > 0) & (pl.col("amount") > 0)
            & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
        )
        .with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
        .with_columns([
            _quantile_rank("mcap").alias("p_scale"),
            _quantile_rank("turnover_rate").alias("p_turnover"),
            _quantile_rank("amount").alias("p_amount"),
        ])
        .with_columns(_assign_level().alias("level"))
        .select(["symbol", "date", "name", "mcap", "amount", "turnover_rate",
                 "p_scale", "p_turnover", "p_amount", "level"])
    )
    lv_map = {
        (r["symbol"], r["date"]): r
        for r in lv.iter_rows(named=True)
    }
    # 当日全市场等级分布（基准）
    market_dist: dict[str, dict[str, int]] = {}
    for (d, lvl, n) in (
        lv.group_by(["date", "level"]).len().sort("date").iter_rows()
    ):
        market_dist.setdefault(d.isoformat(), {})[lvl] = n

    # ---- 事件：首次达到 4 个涨停 ----
    ev = grid.filter(
        (pl.col("lu20") >= MIN_LIMIT_UPS)
        & ((pl.col("lu20_prev").is_null()) | (pl.col("lu20_prev") < MIN_LIMIT_UPS))
    )
    events = ev.select(["symbol", "date", "lu20", "consecutive_limit_ups"]).to_dicts()
    print(f"[event] entry events: {len(events)}  ({ev['date'].min()} .. {ev['date'].max()})")

    # 每只票的涨停日序列，用于找「本轮第一个涨停日」
    lu_days: dict[str, list[date]] = {}
    for (s, d) in grid.filter(pl.col("lu") > 0).select(["symbol", "date"]).iter_rows():
        lu_days.setdefault(s, []).append(d)

    # 前瞻收益查表
    px_map = {
        (r["symbol"], r["date"]): r
        for r in panel.select(
            ["symbol", "date", "open", "close", "raw_close", "consecutive_limit_ups"]
        ).iter_rows(named=True)
    }

    def fwd(symbol: str, d: str, offset: int) -> dict | None:
        i = date_idx.get(d)
        if i is None or i + offset >= len(dates):
            return None
        return px_map.get((symbol, datetime.fromisoformat(dates[i + offset]).date()))

    # 事件级基础字段 + 等级轨迹 + 前瞻
    for e in events:
        T = e["date"]
        s = e["symbol"]
        Ti = date_idx[T.isoformat()]
        row0 = lv_map.get((s, T))
        e["name"] = row0["name"] if row0 else None
        e["board"] = (
            "北交所" if s.endswith(".BJ") else
            "创业板" if s.startswith(("300", "301", "302")) else
            "科创板" if s.startswith(("688", "689")) else "主板"
        )
        # 等级轨迹（按交易日偏移，不是按行）
        for off in (-20, -10, -5, -1, 0, 5, 10, 20):
            j = Ti + off
            d = datetime.fromisoformat(dates[j]).date() if 0 <= j < len(dates) else None
            row = lv_map.get((s, d)) if d else None
            e[f"level_m{abs(off)}" if off < 0 else f"level_p{off}"] = row["level"] if row else None
            if off in (-20, 0):
                tag = "before" if off == -20 else "now"
                e[f"p_turn_{tag}"] = row["p_turnover"] if row else None
                e[f"p_amt_{tag}"] = row["p_amount"] if row else None
                e[f"mcap_{tag}"] = row["mcap"] if row else None
        lvl0 = lv_map.get((s, T))
        e["level"] = lvl0["level"] if lvl0 else None
        e["p_turn"] = lvl0["p_turnover"] if lvl0 else None
        e["p_amt"] = lvl0["p_amount"] if lvl0 else None
        e["mcap"] = lvl0["mcap"] if lvl0 else None
        prev = lv_map.get((s, datetime.fromisoformat(dates[Ti - 1]).date())) if Ti > 0 else None
        e["prev_level"] = prev["level"] if prev else None
        r0, r1 = _level_rank(e["level"]), _level_rank(e["prev_level"])
        e["transition"] = (
            "new" if r0 is None or r1 is None else
            "up" if r1 > r0 else "down" if r1 < r0 else "stay"
        )
        # 本轮第一个涨停日
        window_start = datetime.fromisoformat(dates[Ti - LOOKBACK + 1]).date()
        run = [x for x in lu_days.get(s, []) if window_start <= x <= T]
        e["run_start"] = run[0].isoformat() if run else None
        e["run_days"] = len(run)
        rs_row = lv_map.get((s, run[0])) if run else None
        e["level_run_start"] = rs_row["level"] if rs_row else None
        if run:
            ri = date_idx[run[0].isoformat()]
            pre_row = lv_map.get((s, datetime.fromisoformat(dates[ri - 1]).date())) if ri > 0 else None
            e["level_pre_run"] = pre_row["level"] if pre_row else None
        else:
            e["level_pre_run"] = None
        e["max_board"] = max(
            (px_map[(s, x)]["consecutive_limit_ups"] for x in run if (s, x) in px_map),
            default=0,
        )
        # 前瞻
        c0 = px_map.get((s, T))
        for h in (1, 3, 5, 10):
            x = fwd(s, T.isoformat(), h)
            e[f"ret_{h}d"] = (x["close"] / c0["close"] - 1) if (x and c0 and c0["close"]) else None
        n1 = fwd(s, T.isoformat(), 1)
        if n1 and c0 and c0["close"] and n1["open"]:
            e["gap_1d"] = n1["open"] / c0["close"] - 1
            e["next_lu"] = int(n1["consecutive_limit_ups"] > 0)
            # 真实可成交口径：T+1 开盘买（不是 T 日收盘买）
            e["oc_1"] = n1["close"] / n1["open"] - 1
            for h, key in ((5, "hold5_open"), (10, "hold10_open")):
                x = fwd(s, T.isoformat(), h)
                e[key] = (x["close"] / n1["open"] - 1) if x else None
            n2 = fwd(s, T.isoformat(), 2)
            e["oo_2"] = (n2["open"] / n1["open"] - 1) if n2 else None
            # 可成交：T+1 原始开盘 < T+1 涨停价
            pct = _limit_pct(s, datetime.fromisoformat(dates[Ti + 1]).date(), e["name"])
            lim = round(c0["raw_close"] * (1 + pct) + 1e-9, 2)
            ratio = (n1["raw_close"] / n1["close"]) if n1["close"] else 1.0
            raw_open = n1["open"] * ratio
            e["fillable"] = int(raw_open < lim - 0.005)
            e["t1_open_limit"] = int(raw_open >= lim - 0.005)
        else:
            e["gap_1d"] = e["next_lu"] = e["fillable"] = e["t1_open_limit"] = None
            e["oc_1"] = e["oo_2"] = e["hold5_open"] = e["hold10_open"] = None

    # ===== A. 等级变化 =====
    def share(rows: list[dict], key: str, val: str) -> float:
        xs = [r for r in rows if r.get(key) is not None]
        return round(sum(1 for r in xs if r[key] == val) / len(xs), 4) if xs else 0.0

    lv_change = {
        "n": len(events),
        "level_at_before": {k: share(events, "level_m20", k) for k in LEVEL_ORDER},
        "level_at_T": {k: share(events, "level", k) for k in LEVEL_ORDER},
        "level_at_run_start": {k: share(events, "level_run_start", k) for k in LEVEL_ORDER},
        "level_pre_run": {k: share(events, "level_pre_run", k) for k in LEVEL_ORDER},
        "level_at_p5": {k: share(events, "level_p5", k) for k in LEVEL_ORDER},
        "transition_at_T": {
            "up": share(events, "transition", "up"),
            "down": share(events, "transition", "down"),
            "stay": share(events, "transition", "stay"),
            "new": share(events, "transition", "new"),
        },
        "bg_or_silent_to_active_or_core": round(
            sum(
                1 for r in events
                if r.get("level_m20") in ("background", "silent")
                and r.get("level") in ("active", "core")
            ) / max(1, sum(1 for r in events if r.get("level_m20") and r.get("level"))),
            4,
        ),
        "rank_trajectory": {},
        "p_turnover_change": _stat([
            r["p_turn"] - r["p_turn_before"]
            for r in events if r.get("p_turn") is not None and r.get("p_turn_before") is not None
        ]),
        "p_amount_change": _stat([
            r["p_amt"] - r["p_amt_before"]
            for r in events if r.get("p_amt") is not None and r.get("p_amt_before") is not None
        ]),
        "mcap_before_median": sorted(
            r["mcap_before"] for r in events if r.get("mcap_before")
        )[len([r for r in events if r.get("mcap_before")]) // 2]
        if any(r.get("mcap_before") for r in events) else None,
    }
    for off in (-20, -10, -5, -1, 0, 5, 10, 20):
        key = f"level_m{abs(off)}" if off < 0 else f"level_p{off}"
        ranks = [_level_rank(r.get(key)) for r in events if r.get(key)]
        lv_change["rank_trajectory"][str(off)] = (
            round(sum(ranks) / len(ranks), 3) if ranks else None,
            len(ranks),
        )

    # 升格滞后：等级首次变成 active/core 的日期相对本轮首个涨停日。
    # 全样本均值被「T-20 已 active」的票主导（它们 lag=-1），因此拆成两组：
    # 事件前仍是 background/silent 的票，才真正回答「等级是不是领先首板」。
    lags, lags_bg = [], []
    for e in events:
        s, T = e["symbol"], e["date"]
        Ti = date_idx[T.isoformat()]
        rs = e.get("run_start")
        if not rs:
            continue
        start_i = date_idx[rs]
        first_active = None
        for j in range(max(0, Ti - LOOKBACK), Ti + 1):
            d = datetime.fromisoformat(dates[j]).date()
            row = lv_map.get((s, d))
            if row and row["level"] in ("active", "core"):
                first_active = j
                break
        if first_active is not None:
            lags.append(first_active - start_i)  # >0 = 涨停之后才升格
            if e.get("level_m20") in ("background", "silent"):
                lags_bg.append(first_active - start_i)
    lv_change["upgrade_lag_days_vs_run_start"] = _stat(lags)
    lv_change["upgrade_lag_if_bg_before"] = _stat(lags_bg)

    # 反向精度：升格样本里有多少同时是「4 涨停入池」事件（回答「升格能不能筛出它们」）
    up_keys = set()
    for e in events:
        s, T = e["symbol"], e["date"]
        Ti = date_idx[T.isoformat()]
        if Ti == 0:
            continue
        d0 = datetime.fromisoformat(dates[Ti - 1]).date()
        a, b = lv_map.get((s, d0)), lv_map.get((s, T))
        if a and b and a["level"] not in ("active", "core") and b["level"] in ("active", "core"):
            up_keys.add((s, T))
    # 全市场升格集合
    all_up = set()
    for (s, d), row in lv_map.items():
        i = date_idx.get(d.isoformat())
        if i is None or i == 0:
            continue
        prev = lv_map.get((s, datetime.fromisoformat(dates[i - 1]).date()))
        if prev and prev["level"] not in ("active", "core") and row["level"] in ("active", "core"):
            all_up.add((s, d))
    lv_change["share_upgrades_that_are_4lu_events"] = round(
        len(up_keys) / max(1, len(all_up)), 4
    )
    lv_change["n_upgrade_market"] = len(all_up)

    # 升格能不能当「打板筛子」：升格日未来 2 日出现涨停的比例 vs 全样本基率。
    # 这是对「等级=前瞻信号」这一前提的直接攻击；绝对概率比 lift 更重要。
    tmp = panel.sort(["symbol", "date"]).with_columns([
        (pl.col("consecutive_limit_ups").shift(-1).over("symbol") > 0).alias("_lu1"),
        (pl.col("consecutive_limit_ups").shift(-2).over("symbol") > 0).alias("_lu2"),
    ])
    keys = pl.DataFrame(
        {"symbol": [k[0] for k in all_up], "date": [k[1] for k in all_up]},
        schema={"symbol": pl.Utf8, "date": pl.Date},
    )
    up_tmp = tmp.join(keys, on=["symbol", "date"], how="semi")
    base2 = float(tmp.select((pl.col("_lu1") | pl.col("_lu2")).fill_null(False).mean()).item())
    up2 = float(up_tmp.select((pl.col("_lu1") | pl.col("_lu2")).fill_null(False).mean()).item())
    lv_change["upgrade_next2_limitup"] = {
        "upgrade": round(up2, 4),
        "base": round(base2, 4),
        "lift": round(up2 / base2, 2) if base2 else None,
    }
    # 全市场等级基准（逐日占比的均值），用来判断「启动前 77% active/core」是否反常
    if market_dist:
        base = {k: 0.0 for k in LEVEL_ORDER}
        for dist in market_dist.values():
            tot = sum(dist.values()) or 1
            for k in LEVEL_ORDER:
                base[k] += dist.get(k, 0) / tot
        lv_change["market_base"] = {k: round(v / len(market_dist), 4) for k, v in base.items()}

    # ===== B. 盘中点火特征 =====
    minute_dates = sorted(d[5:] for d in os.listdir(MINUTE) if d.startswith("date="))
    minute_set = set(minute_dates)
    # 每日 09:31–10:00 成交额（用于前 5 日同时段量能基准）
    amt30_by_day: dict[str, dict[str, float]] = {}
    for d in minute_dates:
        m = pl.read_parquet(MINUTE / f"date={d}" / "part.parquet")
        mins = (
            pl.col("datetime").dt.hour().cast(pl.Int32) * 60
            + pl.col("datetime").dt.minute().cast(pl.Int32)
        )
        g = (
            m.filter(mins <= MORNING_END)
            .group_by("symbol")
            .agg(pl.col("amount").sum().alias("amt30"))
        )
        amt30_by_day[d] = dict(g.iter_rows())

    minute_events = [e for e in events if e["date"].isoformat() in minute_set]
    print(f"[minute] events with intraday: {len(minute_events)}")

    bars_cache: dict[str, pl.DataFrame] = {}
    for e in minute_events:
        d, s, T = e["date"].isoformat(), e["symbol"], e["date"]
        if d not in bars_cache:
            bars_cache[d] = pl.read_parquet(MINUTE / f"date={d}" / "part.parquet")
        bars = bars_cache[d].filter(pl.col("symbol") == s).sort("datetime")
        if bars.height < 100:
            continue
        Ti = date_idx[d]
        prev = px_map.get((s, datetime.fromisoformat(dates[Ti - 1]).date()))
        if not prev or not prev["raw_close"]:
            continue
        pct = _limit_pct(s, T, e.get("name"))
        lim = round(prev["raw_close"] * (1 + pct) + 1e-9, 2)
        mins = (
            bars["datetime"].dt.hour().cast(pl.Int32) * 60
            + bars["datetime"].dt.minute().cast(pl.Int32)
        )
        touch = (bars["high"] >= lim - 0.005).to_list()
        seal = (bars["close"] >= lim - 0.005).to_list()
        first_touch = next((i for i, x in enumerate(touch) if x), None)
        first_seal = next((i for i, x in enumerate(seal) if x), None)
        breaks = sum(1 for i in range(1, len(seal)) if seal[i - 1] and not seal[i])
        amt30 = amt30_by_day.get(d, {}).get(s)
        prior = [amt30_by_day[dd].get(s) for dd in minute_dates[max(0, minute_dates.index(d) - 5):minute_dates.index(d)]]
        prior = [x for x in prior if x]
        total = bars["amount"].sum()
        e.update({
            "first_touch_min": int(mins[first_touch] - OPEN_MIN) if first_touch is not None else None,
            "first_seal_min": int(mins[first_seal] - OPEN_MIN) if first_seal is not None else None,
            "sealed_ratio": round(sum(seal) / len(seal), 4),
            "break_count": breaks,
            "close_sealed": int(seal[-1]),
            "auction_gap": round(bars["open"][0] / prev["raw_close"] - 1, 6),
            "amt30_share": round(amt30 / total, 4) if (amt30 and total) else None,
            "early_vol_ratio": round(amt30 / (sum(prior) / len(prior)), 3) if (amt30 and prior) else None,
        })
        if first_seal is None:
            typ = "未触板"
        elif not seal[-1]:
            typ = "炸板"
        elif e["first_seal_min"] is not None and e["first_seal_min"] <= 5 and breaks == 0:
            typ = "秒板"
        elif e["first_seal_min"] <= 60 and breaks <= 1:
            typ = "早盘封"
        elif e["first_seal_min"] <= 180 and breaks <= 2:
            typ = "盘中封"
        else:
            typ = "烂板"
        e["ignition_type"] = typ

    intraday = [e for e in minute_events if e.get("ignition_type")]
    by_type = []
    for typ in ("秒板", "早盘封", "盘中封", "烂板", "炸板", "未触板"):
        rows = [e for e in intraday if e["ignition_type"] == typ]
        if not rows:
            continue
        by_type.append({
            "type": typ,
            "n": len(rows),
            "gap_1d": _date_stat(rows, "gap_1d"),
            # T 日收盘价买（打板上界）：需要「在涨停价成交」，秒板几乎成交不了
            "ret_1d": _date_stat(rows, "ret_1d"),
            "ret_5d": _date_stat(rows, "ret_5d"),
            "ret_10d": _date_stat(rows, "ret_10d"),
            # T+1 开盘买：真实零售入口
            "oc_1": _date_stat(rows, "oc_1"),
            "oo_2": _date_stat(rows, "oo_2"),
            "hold5_open": _date_stat(rows, "hold5_open"),
            "next_lu_rate": round(sum(1 for r in rows if r.get("next_lu")) / len(rows), 4),
            "fillable_rate": round(sum(1 for r in rows if r.get("fillable")) / len(rows), 4),
            "t1_open_limit_rate": round(sum(1 for r in rows if r.get("t1_open_limit")) / len(rows), 4),
            "early_seal_share": round(sum(1 for r in rows if r["ignition_type"] in ("秒板", "早盘封")) / len(rows), 4),
            "sealed_ratio_mean": round(sum(r.get("sealed_ratio") or 0 for r in rows) / len(rows), 4),
            # T 日能在涨停价成交的粗略代理：未封板分钟占比（无委托队列数据，只能代理）
            "unsealed_share_mean": round(
                1 - sum(r.get("sealed_ratio") or 0 for r in rows) / len(rows), 4
            ),
            "fillable_oc_1": _date_stat([r for r in rows if r.get("fillable")], "oc_1"),
        })

    # 连续特征与次日结果的相关（Spearman 用秩相关，避免 scipy）
    def spearman(rows: list[dict], xk: str, yk: str) -> float | None:
        xs, ys = [], []
        for r in rows:
            x, y = r.get(xk), r.get(yk)
            if x is not None and y is not None:
                xs.append(float(x)); ys.append(float(y))
        if len(xs) < 10:
            return None
        def ranks(v):
            order = sorted(range(len(v)), key=lambda i: v[i])
            r = [0.0] * len(v)
            for pos, i in enumerate(order):
                r[i] = pos
            return r
        rx, ry = ranks(xs), ranks(ys)
        n = len(xs)
        mx, my = sum(rx) / n, sum(ry) / n
        cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
        sy = math.sqrt(sum((b - my) ** 2 for b in ry))
        return round(cov / (sx * sy), 4) if sx and sy else None

    corr = {
        "seal_time_vs_gap": spearman(intraday, "first_seal_min", "gap_1d"),
        "seal_time_vs_ret1": spearman(intraday, "first_seal_min", "ret_1d"),
        "sealed_ratio_vs_gap": spearman(intraday, "sealed_ratio", "gap_1d"),
        "sealed_ratio_vs_ret1": spearman(intraday, "sealed_ratio", "ret_1d"),
        "auction_gap_vs_ret1": spearman(intraday, "auction_gap", "ret_1d"),
        "early_vol_vs_gap": spearman(intraday, "early_vol_ratio", "gap_1d"),
        # 真实入口：T+1 开盘买。点火强度如果只抬高开盘价，这些相关应接近 0 或为负
        "seal_time_vs_oc1": spearman(intraday, "first_seal_min", "oc_1"),
        "sealed_ratio_vs_oc1": spearman(intraday, "sealed_ratio", "oc_1"),
        "auction_gap_vs_oc1": spearman(intraday, "auction_gap", "oc_1"),
        "gap_vs_oc1": spearman(intraday, "gap_1d", "oc_1"),
    }

    # ===== B2. 启动日（首板）的点火特征：后来的 4 板股 vs 普通首板 =====
    #
    # 「4 板股的点火」如果看第 4 板当天，看到的已经是接力而不是启动。
    # 这里回到每轮行情的**第一个涨停日**，用「未来 20 日内 lu20 是否达到 4」
    # 作标签，比较两类首板的盘中形态——这才是「点火」的可证伪定义。
    g = grid.join(inst.select(["symbol", "name"]), on="symbol", how="left")
    g = g.with_columns(
        pl.col("raw_close").shift(1).over("symbol").alias("_prev_raw"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("_ret20_prev"),
    )
    g = g.with_columns(
        polars_price_limit_pct(
            pl.col("symbol"), pl.col("date"),
            pl.col("name").fill_null("").str.to_uppercase().str.contains("ST", literal=True),
        ).alias("_pct")
    ).with_columns(polars_limit_price(pl.col("_prev_raw"), pl.col("_pct"), up=True).alias("_lim"))
    fwd_cols = [pl.col("lu20").shift(-i).over("symbol").alias(f"_f{i}") for i in range(21)]
    g = g.with_columns(fwd_cols).with_columns(
        pl.max_horizontal([pl.col(f"_f{i}") for i in range(21)]).alias("_fmax")
    ).with_columns((pl.col("_fmax") >= MIN_LIMIT_UPS).alias("hot20"))
    mstart = date.fromisoformat(minute_dates[0])
    mend = date.fromisoformat(dates[-21])  # 标签需要完整 20 个未来交易日
    fb = g.filter(
        (pl.col("lu") == 1) & (pl.col("lu_prev") == 0)
        # 「干净启动」：前 20 日一次涨停都没有。否则所谓首板只是上一轮行情的
        # 中继板，量价形态早已被前面的涨停污染，不能回答「点火」。
        & (pl.col("lu20_prev").fill_null(0) == 0)
        & (pl.col("date") >= mstart) & (pl.col("date") <= mend)
        & (pl.col("_prev_raw") > 0)
    )
    print(f"[first-board] {fb.height} first boards, hot rate {fb['hot20'].mean():.3f}")

    fb_rows: list[dict] = []
    for d in fb["date"].unique().sort().to_list():
        sub = fb.filter(pl.col("date") == d)
        syms = sub["symbol"].to_list()
        bars = pl.read_parquet(MINUTE / f"date={d.isoformat()}" / "part.parquet").filter(
            pl.col("symbol").is_in(syms)
        )
        lim = sub.select(["symbol", "_prev_raw", "_lim", "hot20"])
        b = bars.join(lim, on="symbol").with_columns([
            (pl.col("datetime").dt.hour().cast(pl.Int32) * 60
             + pl.col("datetime").dt.minute().cast(pl.Int32)).alias("mins"),
            (pl.col("high") >= pl.col("_lim") - 0.005).alias("touch"),
            (pl.col("close") >= pl.col("_lim") - 0.005).alias("sealed"),
        ]).sort(["symbol", "datetime"])
        agg = b.group_by("symbol").agg([
            pl.col("mins").filter(pl.col("touch")).min().alias("first_touch_min"),
            pl.col("mins").filter(pl.col("sealed")).min().alias("first_seal_min"),
            pl.col("sealed").mean().alias("sealed_ratio"),
            pl.col("sealed").last().alias("close_sealed"),
            (pl.col("sealed").cast(pl.Int8).diff() == -1).sum().alias("break_count"),
            pl.col("open").first().alias("_open1"),
            pl.col("amount").filter(pl.col("mins") <= MORNING_END).sum().alias("_amt30"),
            pl.col("amount").sum().alias("_amt_total"),
        ])
        joined = sub.join(agg, on="symbol", how="left")
        di = minute_dates.index(d.isoformat())
        prior_dates = minute_dates[max(0, di - 5):di]
        for r in joined.iter_rows(named=True):
            if r["sealed_ratio"] is None:
                continue  # 分钟线缺失，不能当成「未触板」
            row = lv_map.get((r["symbol"], d))
            amt30 = r["_amt30"]
            prior = [amt30_by_day[pd].get(r["symbol"]) for pd in prior_dates]
            prior = [x for x in prior if x]
            row_out = {
                "date": d.isoformat(),
                "symbol": r["symbol"],
                "board": (
                    "北交所" if r["symbol"].endswith(".BJ") else
                    "创业板" if r["symbol"].startswith(("300", "301", "302")) else
                    "科创板" if r["symbol"].startswith(("688", "689")) else "主板"
                ),
                "hot20": bool(r["hot20"]),
                "first_touch_min": (int(r["first_touch_min"] - OPEN_MIN)
                                    if r["first_touch_min"] is not None else None),
                "first_seal_min": (int(r["first_seal_min"] - OPEN_MIN)
                                   if r["first_seal_min"] is not None else None),
                "sealed_ratio": round(float(r["sealed_ratio"]), 4) if r["sealed_ratio"] is not None else None,
                "break_count": int(r["break_count"] or 0),
                "close_sealed": bool(r["close_sealed"]),
                "auction_gap": round(r["_open1"] / r["_prev_raw"] - 1, 6),
                "amt30_share": round(amt30 / r["_amt_total"], 4) if (amt30 and r["_amt_total"]) else None,
                "early_vol_ratio": (round(amt30 / (sum(prior) / len(prior)), 3)
                                    if (amt30 and prior) else None),
                "ret20_prev": r["_ret20_prev"],
                "p_amount": row["p_amount"] if row else None,
                "turnover_rate": row["turnover_rate"] if row else None,
                "mcap": row["mcap"] if row else None,
            }
            if row_out["first_seal_min"] is None:
                row_out["ignition_type"] = "未触板"
            elif not row_out["close_sealed"]:
                row_out["ignition_type"] = "炸板"
            elif row_out["first_seal_min"] <= 5 and row_out["break_count"] == 0:
                row_out["ignition_type"] = "秒板"
            elif row_out["first_seal_min"] <= 60 and row_out["break_count"] <= 1:
                row_out["ignition_type"] = "早盘封"
            elif row_out["first_seal_min"] <= 180 and row_out["break_count"] <= 2:
                row_out["ignition_type"] = "盘中封"
            else:
                row_out["ignition_type"] = "烂板"
            fb_rows.append(row_out)

    hot = [r for r in fb_rows if r["hot20"]]
    cold = [r for r in fb_rows if not r["hot20"]]

    def _mean(rows: list[dict], key: str) -> float | None:
        xs = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(xs) / len(xs), 4) if xs else None

    def _auc(rows: list[dict], key: str) -> float | None:
        pairs = [(r[key], r["hot20"]) for r in rows if r.get(key) is not None]
        if len(pairs) < 50:
            return None
        vals = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
        rank = [0.0] * len(pairs)
        for pos, i in enumerate(vals):
            rank[i] = pos + 1
        n_hot = sum(1 for _, y in pairs if y)
        n_cold = len(pairs) - n_hot
        if not n_hot or not n_cold:
            return None
        s = sum(rank[i] for i, (_, y) in enumerate(pairs) if y)
        return round((s - n_hot * (n_hot + 1) / 2) / (n_hot * n_cold), 3)

    feature_keys = [
        "auction_gap", "first_seal_min", "sealed_ratio", "break_count",
        "amt30_share", "early_vol_ratio", "ret20_prev", "p_amount",
        "turnover_rate", "mcap",
    ]
    fb_feature_stats = {
        k: {
            "hot_mean": _mean(hot, k),
            "cold_mean": _mean(cold, k),
            "auc_hot": _auc(fb_rows, k),
            "n": sum(1 for r in fb_rows if r.get(k) is not None),
        }
        for k in feature_keys
    }
    fb_by_type = []
    for typ in ("秒板", "早盘封", "盘中封", "烂板", "炸板", "未触板"):
        rows = [r for r in fb_rows if r["ignition_type"] == typ]
        if not rows:
            continue
        fb_by_type.append({
            "type": typ,
            "n": len(rows),
            "hot_rate": round(sum(1 for r in rows if r["hot20"]) / len(rows), 4),
            "sealed_ratio": _mean(rows, "sealed_ratio"),
            "auction_gap": _mean(rows, "auction_gap"),
        })
    # 简单规则的精确率/召回（不做拟合，只看可解释阈值）
    rule = [
        r for r in fb_rows
        if r["ignition_type"] in ("秒板", "早盘封") and (r["auction_gap"] or 0) >= 0.02
    ]
    rule_tp = sum(1 for r in rule if r["hot20"])
    fb_rule = {
        "rule": "首板属于秒板/早盘封 且 竞价代理高开 ≥2%",
        "n": len(rule),
        "precision": round(rule_tp / len(rule), 4) if rule else None,
        "recall": round(rule_tp / len(hot), 4) if hot else None,
        "base_rate": round(len(hot) / len(fb_rows), 4) if fb_rows else None,
    }
    split_d = date(2026, 9, 1)
    fb_oos = {}
    for tag, rows in (
        ("before_20260901", [r for r in fb_rows if date.fromisoformat(r["date"]) < split_d]),
        ("after_20260901", [r for r in fb_rows if date.fromisoformat(r["date"]) >= split_d]),
    ):
        h = [r for r in rows if r["hot20"]]
        fb_oos[tag] = {
            "n": len(rows),
            "hot_rate": round(len(h) / len(rows), 4) if rows else None,
            "auc_sealed_ratio": _auc(rows, "sealed_ratio"),
            "auc_auction_gap": _auc(rows, "auction_gap"),
        }

    first_board = {
        "n": len(fb_rows),
        "hot_n": len(hot),
        "hot_rate": round(len(hot) / len(fb_rows), 4) if fb_rows else None,
        "features": fb_feature_stats,
        "by_type": fb_by_type,
        "rule": fb_rule,
        "oos": fb_oos,
    }

    # ===== C. 龙虎榜 =====
    lhb_dates = sorted(
        os.path.basename(p)[5:15] for p in LHB.glob("date=*.json")
    )
    lhb_set = set(lhb_dates)
    lhb_rows = 0
    for e in events:
        d, s = e["date"].isoformat(), e["symbol"]
        if d not in lhb_set:
            continue
        path = LHB / f"date={d}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        all_items = {r.get("thscode"): r for r in doc.get("all", {}).get("stock_items", [])}
        seats: list[str] = []
        for seat in doc.get("hot_money", {}).get("hot_money_items", []):
            if any(r.get("thscode") == s for r in seat.get("rows", [])):
                seats.append(seat.get("name") or "")
        r = all_items.get(s)
        if r is None and not seats:
            continue
        lhb_rows += 1
        e["lhb"] = 1
        e["lhb_net"] = r.get("net_value") if r else None
        e["lhb_hot_net"] = r.get("hot_money_net_value") if r else None
        e["lhb_org_net"] = r.get("org_net_value") if r else None
        e["lhb_seats"] = ";".join([x for x in seats if x])
        e["lhb_seat_count"] = len(seats)
        e["lhb_reason"] = r.get("limit_reason") if r else None

    lhb_events = [e for e in events if e.get("lhb")]
    lhb_hot_pos = [e for e in lhb_events if (e.get("lhb_hot_net") or 0) > 0]
    lhb = {
        "coverage_dates": len(lhb_dates),
        "events_with_lhb": len(lhb_events),
        "share_with_lhb": round(len(lhb_events) / max(1, len(events)), 4),
        "hot_net_positive_share": round(len(lhb_hot_pos) / max(1, len(lhb_events)), 4),
        "hot_positive_outcome": {
            "ret_1d": _date_stat(lhb_hot_pos, "ret_1d"),
            "ret_5d": _date_stat(lhb_hot_pos, "ret_5d"),
            "gap_1d": _date_stat(lhb_hot_pos, "gap_1d"),
        },
        "hot_nonpositive_outcome": {
            "ret_1d": _date_stat([e for e in lhb_events if (e.get("lhb_hot_net") or 0) <= 0], "ret_1d"),
            "ret_5d": _date_stat([e for e in lhb_events if (e.get("lhb_hot_net") or 0) <= 0], "ret_5d"),
        },
        "top_seats": {},
    }
    for e in lhb_events:
        for name in filter(None, (e.get("lhb_seats") or "").split(";")):
            lhb["top_seats"][name] = lhb["top_seats"].get(name, 0) + 1
    lhb["top_seats"] = dict(sorted(lhb["top_seats"].items(), key=lambda kv: -kv[1])[:12])

    # ===== D. 稳定性：9 月前后 =====
    split = date(2026, 9, 1)

    def seg(rows: list[dict]) -> dict:
        return {
            "n": len(rows),
            "ret_1d": _date_stat(rows, "ret_1d"),
            "oc_1": _date_stat(rows, "oc_1"),
            "oo_2": _date_stat(rows, "oo_2"),
            "hold5_open": _date_stat(rows, "hold5_open"),
            "fillable_rate": round(sum(1 for r in rows if r.get("fillable")) / max(1, len(rows)), 4),
            "next_lu_rate": round(sum(1 for r in rows if r.get("next_lu")) / max(1, len(rows)), 4),
        }

    stability = {
        "before_20260901": seg([e for e in events if e["date"] < split]),
        "after_20260901": seg([e for e in events if e["date"] >= split]),
        "all": seg(events),
    }

    # ===== E. 当前名单（截至最新交易日）=====
    latest = dates[-1]
    latest_date = datetime.fromisoformat(latest).date()
    today_rows = grid.filter(
        (pl.col("date") == latest_date) & (pl.col("lu20") >= MIN_LIMIT_UPS)
    ).sort("lu20", descending=True)
    cohort = []
    prev_date = datetime.fromisoformat(dates[-2]).date()
    for r in today_rows.select(["symbol", "lu20", "consecutive_limit_ups"]).iter_rows(named=True):
        row = lv_map.get((r["symbol"], latest_date))
        prev = lv_map.get((r["symbol"], prev_date))
        cohort.append({
            "symbol": r["symbol"],
            "name": row["name"] if row else None,
            "level": row["level"] if row else None,
            "prev_level": prev["level"] if prev else None,
            "mcap": round(row["mcap"], 0) if row and row["mcap"] else None,
            "lu20": r["lu20"],
            "board_now": r["consecutive_limit_ups"],
            "p_turnover": round(row["p_turnover"], 4) if row else None,
            "p_amount": round(row["p_amount"], 4) if row else None,
            "max_board_20": 0,
        })
    # 20 日内最高连板 / 最近一次涨停日
    for c in cohort:
        s = c["symbol"]
        ds = [px_map[(s, d)] for d in lu_days.get(s, []) if (s, d) in px_map][-20:]
        c["max_board_20"] = max((x["consecutive_limit_ups"] for x in ds), default=0)
        c["last_lu"] = max((d.isoformat() for d in lu_days.get(s, []) if (s, d) in px_map), default=None)

    # ===== 汇总 =====
    out = {
        "as_of": latest,
        "params": {"lookback": LOOKBACK, "min_limit_ups": MIN_LIMIT_UPS,
                   "start": START, "minute_start": minute_dates[0], "minute_end": minute_dates[-1]},
        "counts": {
            "events": len(events),
            "minute_events": len(intraday),
            "unique_symbols": len({e["symbol"] for e in events}),
            "cohort_now": len(cohort),
        },
        "level_change": lv_change,
        "level_by_board": {},
        "intraday": {
            "by_type": by_type,
            "corr": corr,
            "features": {
                "auction_gap": _stat([e.get("auction_gap") for e in intraday]),
                "first_seal_min": _stat([e.get("first_seal_min") for e in intraday]),
                "sealed_ratio": _stat([e.get("sealed_ratio") for e in intraday]),
                "break_count": _stat([e.get("break_count") for e in intraday]),
                "amt30_share": _stat([e.get("amt30_share") for e in intraday]),
                "early_vol_ratio": _stat([e.get("early_vol_ratio") for e in intraday]),
            },
        },
        "first_board": first_board,
        "lhb": lhb,
        "stability": stability,
        "cohort": cohort,
        "cohort_level_dist": {k: sum(1 for c in cohort if c["level"] == k) for k in LEVEL_ORDER},
        "cohort_mcap_median": sorted(c["mcap"] for c in cohort if c["mcap"])[
            len([c for c in cohort if c["mcap"]]) // 2
        ] if any(c["mcap"] for c in cohort) else None,
    }
    for board in ("主板", "创业板", "科创板", "北交所"):
        rows = [e for e in events if e["board"] == board]
        if rows:
            out["level_by_board"][board] = {
                "n": len(rows),
                "core_or_active_share": round(sum(1 for r in rows if r.get("level") in ("active", "core")) / len(rows), 4),
                "bg_or_silent_before_share": round(sum(1 for r in rows if r.get("level_m20") in ("background", "silent")) / max(1, sum(1 for r in rows if r.get("level_m20"))), 4),
            }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    cohort_csv = OUT.with_name("limitup_cohort.csv")
    pl.DataFrame(cohort).write_csv(cohort_csv)

    # 精简打印
    print("\n== counts ==", out["counts"])
    print("\n== level change ==")
    print(json.dumps(lv_change, ensure_ascii=False, indent=1))
    print("\n== intraday by type ==")
    print(json.dumps(by_type, ensure_ascii=False, indent=1))
    print("\n== intraday features ==")
    print(json.dumps(out["intraday"]["features"], ensure_ascii=False, indent=1))
    print("\n== corr ==", corr)
    print("\n== first board ==")
    print(json.dumps(first_board, ensure_ascii=False, indent=1))
    print("\n== lhb ==", json.dumps(lhb, ensure_ascii=False, indent=1))
    print("\n== stability ==", json.dumps(stability, ensure_ascii=False, indent=1))
    print("\n== cohort now ==", len(cohort))
    for c in cohort[:15]:
        print(c)
    print(f"\n[out] {OUT}")


if __name__ == "__main__":
    main()
