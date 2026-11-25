"""锁仓横盘 · 首次放量触发：前向登记与评估（prereg-lock-surge-2026-11-25）。

判据、阈值、指标定义全部冻结在 `docs/structural-edge/prereg-lock-surge.md`，
本模块只是那篇文档的唯一实现点。改动这里必须先改文档。

三条与 `shortline_audit.py` 相同的纪律：

1. **只有登记之后的日期算样本外**。`PREREG_START` 之前的行一律降级为 backfill，
   即便调用方传 `source="forward"`。
2. **N 是月数不是股票日数**。fwd20 重叠 20 倍，先按自然月聚簇再求 t。
3. **同日配对**。放量组与安静组来自同一天的同一个池子，只差 `tr_ratio` 这一维；
   不跨日比较，避免「放量组碰巧出现在指数上涨的月份」。

一个额外的工程约束：`record_day` 只接受**已收盘**的分区（`quote_ts` ≥ 当日 15:05）。
盘中跑出来的换手不是全天换手，会把 `tr_ratio` 系统性压低。
"""

from __future__ import annotations

import json
import math
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

import polars as pl

LEDGER = "shortline/lock_surge_ledger.jsonl"
ARTIFACT = "shortline/lock_surge.json"

# ---- 预注册常量（冻结；改动先改 docs/structural-edge/prereg-lock-surge.md）----

PREREG_ID = "lock-surge-2026-11-25"
PREREG_START = "2026-11-26"      # 前向登记从这一天起算
#: 2026-11-25 首次运行后按 prereg §8 关闭：设计/留出都无可测增量，且 6 个月 MDE≈977bp
#: 远高于 80bp 判据 ⇒ 不采纳、不启动前向登记。`--record` 仍可用，但不应接定时任务。
PREREG_STATUS = "closed"
SURGE_RATIO = 2.0                # 当日换手 / 60 日均换手
FWD = 20                         # 持有 20 个交易日（T+1 开盘买，T+21 开盘卖）
COST_RT = 0.0012                 # 双边 12bp
GATE_DIFF_BP = 80.0              # 主判据：月均配对差 ≥ 80bp
GATE_T = 1.65                    # 主判据：按月 t ≥ 1.65
GATE_POS_MONTHS = 0.60           # 稳健：正月份占比
GATE_MIN_PER_DAY = 3.0           # 容量：放量组日均只数
GATE_MIN_MONTHS = 6              # 少于这么多月不裁决

PREREG_NOTE = (
    "锁仓组内按 tr_ratio = 当日换手/60日均换手 ≥ 2.0 分「首次放量 vs 安静」，"
    "同日配对；主指标为 T+1 开盘买、T+21 开盘卖的 20 日超额差。"
    "判据见 docs/structural-edge/prereg-lock-surge.md。"
)


# ------------------------------------------------------------------ 路径与 IO

def ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / LEDGER


def artifact_path(data_dir: Path) -> Path:
    return Path(data_dir) / ARTIFACT


def read_ledger(data_dir: Path) -> list[dict]:
    f = ledger_path(data_dir)
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_artifact(data_dir: Path) -> dict | None:
    f = artifact_path(data_dir)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ------------------------------------------------------------------ 登记

def _is_final(data_dir: Path, day: str) -> bool:
    """分区是否已收盘定型：`quote_ts` 的构建时间 ≥ 当日 15:05。

    盘中构建的分区换手只到那一刻，用它算 tr_ratio 会把「放量」漏掉一半。
    分区隔天重建也满足这个条件（构建时间只会更晚）。
    """
    f = Path(data_dir) / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    if not f.exists():
        return False
    try:
        ts = pl.read_parquet(f, columns=["quote_ts"])["quote_ts"].max()
    except Exception:
        return False
    if ts is None:
        return False
    sec = ts / 1000.0 if ts > 1e11 else float(ts)
    try:
        built = datetime.fromtimestamp(sec)
    except (OSError, OverflowError, ValueError):
        return False
    return built.strftime("%Y-%m-%d %H:%M") >= f"{day} 15:05"


def _split(band: list[dict], ratio: float = SURGE_RATIO) -> tuple[list[str], list[str]]:
    surge = [r["symbol"] for r in band if (r.get("tr") or 0) >= ratio]
    calm = [r["symbol"] for r in band if (r.get("tr") or 0) < ratio]
    return surge, calm


def record_day(
    data_dir: Path,
    as_of: date_cls | None = None,
    source: str = "forward",
    window: str | None = None,
    force: bool = False,
) -> dict | None:
    """把某一天锁仓组的「放量 / 安静」二分追加进台账。幂等：同日期已存在则跳过。

    预注册守卫：`PREREG_START` 之前的日期一律标 backfill（即使传 forward）。
    收盘守卫：只有 `quote_ts ≥ 当日 15:05` 的分区才登记；盘中调用直接返回 None。
    """
    from app.services import citizenship  # 局部导入，避免模块级环

    data_dir = Path(data_dir)
    payload = citizenship.get_citizenship(data_dir, as_of)
    day = payload.get("as_of")
    if day is None:
        return None
    if source == "forward" and day < PREREG_START:
        source = "backfill"
    # 收盘守卫：前向登记一律要求已收盘；今天及以后的日期即使标 backfill 也要求，
    # 否则有人可以用 backfill 把盘中快照写进台账。历史分区 quote_ts 为空
    # （老版本没有这一列），但它们的「已收盘」由日期本身保证。
    if (source == "forward" or day >= date_cls.today().isoformat()) and not _is_final(data_dir, day):
        return None
    rows = read_ledger(data_dir)
    if not force and any(r.get("date") == day for r in rows):
        return None

    band = (payload.get("focus") or {}).get("lock", {}).get("band") or []
    row = {
        "date": day,
        "source": source,
        "window": window or ("forward" if source == "forward" else "backfill"),
        "prereg_id": PREREG_ID,
        "rule": f"tr_ratio>={SURGE_RATIO}",
        "n_band": len(band),
        # 存**原始 tr** 而不是存二分结果：阈值冻结在代码里，邻域诊断（1.5/3.0）
        # 才可能在不重跑历史的前提下复核。band 成员本身是登记时冻结的。
        "band": [
            {
                "symbol": r["symbol"],
                "tr": round(float(r["tr_ratio"]), 3),
                "turn60": round(float(r["turn60"]), 3),
                "ret20": round(float(r["ret20"]), 4),
                "mcap": float(r["mcap"]),
                "amt60": float(r["amt60"]),
            }
            for r in sorted(band, key=lambda x: -(x.get("tr_ratio") or 0))
        ],
        "generated_at": date_cls.today().isoformat(),
    }
    f = ledger_path(data_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# ------------------------------------------------------------------ 价格与指标

def _open_prices(data_dir: Path, day: str) -> dict[str, tuple[float, float]]:
    """{symbol: (open, one_word?)}。一字 = open==high==low，用于诊断买不进的比例。"""
    f = Path(data_dir) / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    if not f.exists():
        return {}
    df = pl.read_parquet(f, columns=["symbol", "open", "high", "low"]).filter(
        pl.col("open") > 0
    )
    return {
        sym: (o, 1.0 if (o == h == l) else 0.0)
        for sym, o, h, l in df.iter_rows()
    }


def _exc_table(data_dir: Path, days: list[str], targets: set[str]) -> dict[str, dict]:
    """对**台账里的日期**给出 open→open 的 20 日超额表，以及一字标签。

    exc20 = open[t+21]/open[t+1] − 1 − 全市场同日同窗等权均值。
    全市场分母只含「三个价格都在」的票；缺数据的票自然落在分母外。
    只算 targets（台账日期），不遍历全历史 —— 否则 5,000 多个分区会被读两遍。
    """
    idx = {d: i for i, d in enumerate(days)}
    out: dict[str, dict] = {}
    for day in sorted(targets):
        i = idx.get(day)
        if i is None or i + FWD + 1 >= len(days):
            continue
        d1, d21 = days[i + 1], days[i + 1 + FWD]
        o1 = _open_prices(data_dir, d1)
        o21 = _open_prices(data_dir, d21)
        if not o1 or not o21:
            continue
        rets: dict[str, float] = {}
        oneword: dict[str, float] = {}
        for sym, (px1, ow) in o1.items():
            px21 = o21.get(sym)
            if not px21 or px1 <= 0:
                continue
            rets[sym] = px21[0] / px1 - 1.0
            oneword[sym] = ow
        if len(rets) < 50:
            continue
        mkt = sum(rets.values()) / len(rets)
        out[day] = {
            "exc": {k: v - mkt for k, v in rets.items()},
            "oneword": oneword,
            "mkt": mkt,
        }
    return out


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _t(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else None


def _month_key(day: str) -> str:
    return day[:7]


def _daily_rows(data_dir: Path, rows: list[dict], ratio: float = SURGE_RATIO) -> list[dict]:
    """把台账行变成逐日读数（放量/安静两组的超额与一字比例）。"""
    days = [d.isoformat() for d in _enriched_days(data_dir)]
    exc_by_day = _exc_table(data_dir, days, {r["date"] for r in rows})
    out = []
    for row in rows:
        tab = exc_by_day.get(row["date"])
        if tab is None:
            continue
        exc, oneword = tab["exc"], tab["oneword"]
        s, c = _split(row.get("band", []), ratio)
        s = [x for x in s if x in exc]
        c = [x for x in c if x in exc]
        if not s or not c:
            continue
        s_ex = _mean([exc[x] for x in s])
        c_ex = _mean([exc[x] for x in c])
        if s_ex is None or c_ex is None:
            continue
        out.append({
            "date": row["date"],
            "month": _month_key(row["date"]),
            "window": row.get("window", "forward"),
            "source": row.get("source", "forward"),
            "n_surge": len(s),
            "n_calm": len(c),
            "surge_ex": s_ex,
            "calm_ex": c_ex,
            "diff": s_ex - c_ex,
            "oneword": _mean([oneword.get(x, 0.0) for x in s]) or 0.0,
        })
    return out


def _enriched_days(data_dir: Path) -> list[date_cls]:
    from app.services import citizenship

    return citizenship._enriched_days(Path(data_dir))


def summarize(daily: list[dict]) -> dict:
    """月聚簇统计：均值、t、正月份占比、容量。空样本返回可读的 None。"""
    if not daily:
        return {
            "days": 0, "months": 0, "diff_bp": None, "diff_t": None,
            "surge_bp": None, "calm_bp": None, "net_surge_bp": None,
            "pos_month_share": None, "n_surge": None, "oneword_share": None,
            "monthly": [],
        }
    by_month: dict[str, list[dict]] = {}
    for r in daily:
        by_month.setdefault(r["month"], []).append(r)
    monthly = []
    for m in sorted(by_month):
        rs = by_month[m]
        monthly.append({
            "month": m,
            "days": len(rs),
            "diff": _mean([r["diff"] for r in rs]),
            "surge": _mean([r["surge_ex"] for r in rs]),
            "calm": _mean([r["calm_ex"] for r in rs]),
        })
    diffs = [x["diff"] for x in monthly]
    surge = [x["surge"] for x in monthly]
    return {
        "days": len(daily),
        "months": len(monthly),
        "diff_bp": round((_mean(diffs) or 0) * 1e4, 1),
        "diff_t": round(_t(diffs) or 0, 2),
        "surge_bp": round((_mean(surge) or 0) * 1e4, 1),
        "calm_bp": round((_mean([x["calm"] for x in monthly]) or 0) * 1e4, 1),
        "net_surge_bp": round((( _mean(surge) or 0) - COST_RT) * 1e4, 1),
        "pos_month_share": round(sum(1 for d in diffs if d > 0) / len(diffs), 3),
        "n_surge": round(_mean([r["n_surge"] for r in daily]) or 0, 1),
        "oneword_share": round(_mean([r["oneword"] for r in daily]) or 0, 3),
        "monthly": [
            {k: (round(v * 1e4, 1) if isinstance(v, float) and k != "days" else v)
             for k, v in x.items()}
            for x in monthly
        ],
    }


def _verdict(stats: dict) -> dict:
    """按冻结判据裁决。只有前向窗口调用；backfill 只给读数。"""
    m = stats["months"]
    if m < GATE_MIN_MONTHS:
        return {"verdict": "insufficient", "reason": f"前向可评估 {m}/{GATE_MIN_MONTHS} 月"}
    checks = {
        "main_diff": (stats["diff_bp"] or 0) >= GATE_DIFF_BP,
        "main_t": (stats["diff_t"] or 0) >= GATE_T,
        "red_net": (stats["net_surge_bp"] or 0) >= 0,
        "pos_months": (stats["pos_month_share"] or 0) >= GATE_POS_MONTHS,
        "capacity": (stats["n_surge"] or 0) >= GATE_MIN_PER_DAY,
    }
    last3 = [x["diff"] for x in stats["monthly"][-3:]]
    checks["last3_sign"] = not (
        (stats["diff_bp"] or 0) > 0 and sum(last3) < 0
    )
    return {
        "verdict": "pass" if all(checks.values()) else "reject",
        "checks": checks,
    }


def stats_for_window(data_dir: Path, window: str, ratio: float = SURGE_RATIO) -> dict:
    """某个窗口的读数（ratio 可换，只用于邻域诊断；裁决永远用冻结的 SURGE_RATIO）。"""
    rows = [r for r in read_ledger(data_dir) if r.get("window") == window]
    return summarize(_daily_rows(Path(data_dir), rows, ratio))


def evaluate(data_dir: Path) -> dict:
    """分窗口读数 + 只对前向窗口裁决。产物写 `data/shortline/lock_surge.json`。"""
    data_dir = Path(data_dir)
    rows = read_ledger(data_dir)
    daily = _daily_rows(data_dir, rows)
    windows: dict[str, list[dict]] = {}
    for r in daily:
        windows.setdefault(r["window"], []).append(r)
    out = {
        "prereg_id": PREREG_ID,
        "status": PREREG_STATUS,
        "rule": PREREG_NOTE,
        "gate": {
            "diff_bp": GATE_DIFF_BP, "t": GATE_T,
            "pos_months": GATE_POS_MONTHS, "min_per_day": GATE_MIN_PER_DAY,
            "min_months": GATE_MIN_MONTHS, "cost_rt": COST_RT,
            "surge_ratio": SURGE_RATIO, "fwd_days": FWD,
        },
        "windows": {k: summarize(v) for k, v in sorted(windows.items())},
        "ledger_days": len(rows),
        "generated_at": date_cls.today().isoformat(),
    }
    fwd = windows.get("forward", [])
    out.update(_verdict(summarize(fwd)))
    f = artifact_path(data_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def backfill(data_dir: Path, start: str, end: str, window: str, step: int = 20) -> dict:
    """按每 20 个交易日取一个基期回填。`step=20` 让 20 日持有窗口不重叠。

    返回登记情况。已存在的日期自动跳过（幂等）。
    """
    data_dir = Path(data_dir)
    days = [d for d in _enriched_days(data_dir) if start <= d.isoformat() <= end]
    if not days:
        return {"registered": 0, "skipped": 0, "reason": "窗口内没有交易日"}
    picked = days[::step]
    registered = 0
    existing = {r.get("date") for r in read_ledger(data_dir)}
    for d in picked:
        if d.isoformat() in existing:
            continue
        row = record_day(data_dir, d, source="backfill", window=window)
        if row is not None:
            registered += 1
    return {"registered": registered, "picked": len(picked), "skipped": len(picked) - registered}
