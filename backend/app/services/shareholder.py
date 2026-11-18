"""股东结构 —— 户数集中度 / 十大流通股东 / 限售解禁 / 抛压象限。

数据源（由 `analysis/_pull_shareholder.py` 拉取后落盘）：
  data/shareholder/holder_num/part.parquet  东财 RPT_HOLDERNUM_DET，2014Q1 起 51 期
  data/shareholder/top_float/part.parquet   东财 RPT_F10_EH_FREEHOLDERS，最近 8 期
  data/shareholder/lift/part.parquet        东财 RPT_LIFT_STAGE，含未来日程

**为什么这三个而不是"筹码分布"**：筹码分布（CYQ）是 close/volume/turnover 的
确定性函数，`indicators/levels.py` 已经算过；它描述"成本在哪里"，不含新信息。
这三个是真实持仓数据，其中**只有户数变化率**经回测证明有独立于量价的信息
（见 `analysis/_diag_shareholder_alpha.py` / `_shape.py` / `_screen_utility.py`）。

单位（已反推校验，不要凭字段名猜）：
  HOLDER_NUM        户
  AVG_MARKET_CAP    元      （茅台 2026Q3：4490 股 × 1258.62 ≈ 565 万，✓）
  free_shares       万股    （平安银行 2018-05-21：25224.8 万股 = 2.52 亿股 ÷ 171 亿股本 = 1.47% = total_ratio ✓）
  lift_mcap         万元    （276211 万元 = 27.6 亿；27.6 亿 ÷ 2.52 亿股 = 10.95 元/股，与该批定增价吻合 ✓）
  total_ratio       占总股本比例（0-1，不是百分数）
  free_ratio        占流通股比例（0-1）

处置口径：
  - 户数因子用**公告日**对齐（notice_date），不是报告期 —— 季报滞后 1 个月、
    年报滞后 3 个月，用报告期做观测日等于偷看未来。
  - 分位是**同报告期全市场横截面分位**，不是该票自身的时间序列分位。
    两者不能互换：前者回答"它相对同期的其他票有多极端"。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

#: 象限回测常数。**不在这里硬编码数字** —— 由 `analysis/_diag_screen_utility.py --write`
#: 产出 data/shareholder/quadrant_stats.json，本模块只读。缺文件时返回 None，
#: 页面显示"未校准"，不显示编造的数字。
STATS_FILE = "quadrant_stats.json"

_cache: dict[str, tuple[float, Any]] = {}


def _read_cached(path: Path) -> pl.DataFrame:
    """按 mtime 缓存 parquet。data/ 是 9p 挂载，重复 read_parquet 比缓存查表贵得多。"""
    if not path.exists():
        return pl.DataFrame()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(path)
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001 - 坏文件不该让整页 500
        logger.warning("股东数据读取失败 %s: %s", path, e)
        return pl.DataFrame()
    _cache[key] = (mtime, df)
    return df


def _holder_num(data_dir: Path) -> pl.DataFrame:
    df = _read_cached(data_dir / "shareholder" / "holder_num" / "part.parquet")
    if df.is_empty():
        return df
    return df.sort(["code", "period_end"]).with_columns(
        [
            (pl.col("HOLDER_NUM") / pl.col("HOLDER_NUM").shift(1).over("code") - 1).alias("chg_q"),
            (pl.col("HOLDER_NUM") / pl.col("HOLDER_NUM").shift(4).over("code") - 1).alias("chg_yoy"),
        ]
    )


def _period_percentile(holder: pl.DataFrame) -> pl.DataFrame:
    """每个报告期内，chg_yoy 的全市场分位（0-1，越大＝户数增得越多）。"""
    if holder.is_empty():
        return pl.DataFrame()
    return holder.filter(pl.col("chg_yoy").is_finite()).with_columns(
        (pl.col("chg_yoy").rank("average").over("period_end")
         / pl.col("chg_yoy").count().over("period_end")).alias("pct_yoy")
    ).select(["code", "period_end", "pct_yoy"])


def load_quadrant_stats(data_dir: Path) -> dict | None:
    p = data_dir / "shareholder" / STATS_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("象限统计读取失败 %s: %s", p, e)
        return None


def _symbol_code(symbol: str) -> str:
    return symbol.split(".")[0] if "." in symbol else symbol


def market_turnover_pct(repo, window: int = 20) -> dict[str, float]:
    """全市场近 `window` 日平均换手率的横截面分位（0-1）。

    实现搬到 `services/market_percentiles.py`：买点判据也要用同一组分位，
    两处各读一遍 20 个分区会让页面请求多付一倍 9p 元数据开销。
    分位而非绝对值：换手率的绝对水平跨年份不可比（2015 与 2024 中枢差一倍）。
    """
    from app.services import market_percentiles as mp

    return mp.turnover_pct(repo, window)


def _holder_trend(holder: pl.DataFrame, code: str, periods: int = 10) -> list[dict]:
    sub = holder.filter(pl.col("code") == code).tail(periods)
    if sub.is_empty():
        return []
    pct = _period_percentile(holder)
    sub = sub.join(pct, on=["code", "period_end"], how="left")
    out = []
    for r in sub.to_dicts():
        out.append({
            "period_end": r["period_end"],
            "notice_date": r.get("notice_date"),
            "holder_num": int(r["HOLDER_NUM"]) if r.get("HOLDER_NUM") else None,
            "chg_q": _round(r.get("chg_q"), 4),
            "chg_yoy": _round(r.get("chg_yoy"), 4),
            "avg_mcap": _round(r["AVG_MARKET_CAP"] / 1e4, 1) if r.get("AVG_MARKET_CAP") else None,  # 元 → 万元
            "pct_yoy": _round(r.get("pct_yoy"), 3),
        })
    return out


def _top_float(data_dir: Path, code: str) -> dict:
    """十大流通股东。

    **两个易错单位**（都实测过，不是推测）：
      - `ratio`（东财 FREE_HOLDNUM_RATIO）是**百分数**：茅台集团 2026Q3 = 54.499，
        含义 54.5%。直接当比例用会得出"机构占比 6773%"。这里统一除 100 成 0-1。
      - `is_org`（IS_HOLDORG）是**字符串** "1"/"0"，不是布尔。`bool("0")` 为 True，
        所以必须先转成数字再判等，否则每一行都会被标成机构。
    """
    df = _read_cached(data_dir / "shareholder" / "top_float" / "part.parquet")
    if df.is_empty():
        return {"period_end": None, "rows": [], "org_ratio": None}
    sub = df.filter(pl.col("code") == code)
    if sub.is_empty():
        return {"period_end": None, "rows": [], "org_ratio": None}
    latest = sub["period_end"].max()
    cur = sub.filter(pl.col("period_end") == latest).sort("rank")
    rows = []
    for r in cur.to_dicts():
        rows.append({
            "rank": r.get("rank"),
            "name": (r.get("holder_name") or "")[:40],
            "ratio": _round(r["ratio"] / 100.0, 4) if r.get("ratio") is not None else None,
            "is_org": str(r.get("is_org")).strip() in ("1", "1.0", "True", "true"),
            "type": r.get("holder_type"),
            # 单位＝股；正=加仓，负=减仓。不做单位换算，前端按万/亿格式化
            "change": r.get("HOLD_NUM_CHANGE"),
        })
    org = sum((r["ratio"] or 0) for r in rows if r["is_org"])
    return {"period_end": latest, "rows": rows, "org_ratio": _round(org, 4)}


def _lift(data_dir: Path, code: str, days: int = 365) -> list[dict]:
    """未来 `days` 天内的解禁。窗口用「今天 + 365 天」而不是「月 +12」：
    后者在月末会算成下下个月的 1 号（2026-11-17 + 12 月 → 2027-10-01），
    把最后一个月整段漏掉。"""
    df = _read_cached(data_dir / "shareholder" / "lift" / "part.parquet")
    if df.is_empty():
        return []
    today = cn_today()
    horizon = (today + timedelta(days=days)).isoformat()
    sub = df.filter(
        (pl.col("code") == code) & (pl.col("free_date") >= today.isoformat())
        & (pl.col("free_date") <= horizon)
    ).sort("free_date")
    out = []
    for r in sub.to_dicts():
        d = date.fromisoformat(r["free_date"])
        out.append({
            "free_date": r["free_date"],
            "days_to": (d - today).days,
            "pct_of_float": _round(r.get("free_ratio"), 4),
            "pct_of_total": _round(r.get("total_ratio"), 4),
            "mcap_yi": _round((r.get("lift_mcap") or 0) / 1e4, 2),  # 万元 → 亿元
            "shares_type": r.get("shares_type"),
        })
    return out


def _round(v, n: int):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, n)


def _quadrant(p_to: float | None, p_hy: float | None, stats: dict | None) -> dict:
    """把两只分位落进 2×2 象限，并挂上该象限的实测超额（若已校准）。"""
    if p_to is None or p_hy is None:
        return {"key": None, "label": "分位不足", "stats": None}
    # 换手率低 = 抛压小；户数不增 = 筹码不在扩散
    low_turn = p_to <= 0.5
    low_hold = p_hy <= 0.5
    key = ("Q1" if low_turn and low_hold else
           "Q2" if not low_turn and low_hold else
           "Q3" if low_turn and not low_hold else "Q4")
    label = {
        "Q1": "低换手 · 筹码集中",
        "Q2": "高换手 · 筹码集中",
        "Q3": "低换手 · 筹码扩散",
        "Q4": "高换手 · 筹码扩散",
    }[key]
    st = None
    if stats and isinstance(stats.get("quadrants"), dict):
        q = stats["quadrants"].get(key)
        if q:
            st = q
    # 四格全给：只给当前格的话，用户看不到"另外三格历史上是什么样"，
    # 也就无法判断当前格是"相对最好"还是"绝对能赚钱"。
    allq = (stats or {}).get("quadrants") if stats else None
    return {"key": key, "label": label, "stats": st, "all": allq}


def build_profile(repo, symbol: str) -> dict:
    """个股股东画像。任何一项缺失都返回 None 而不是 0/空串，前端据此显示"—"。

    **只对股票开放**：股东户数的 key 是 6 位代码，`000001.SH`（上证指数）与
    `000001.SZ`（平安银行）前缀完全一样。不按资产类型隔离的话，查上证指数会
    拿到平安银行的一整套股东数据 —— 数字齐全、格式正确、没有任何报错，是
    最难发现的那种错。
    """
    data_dir = Path(repo.store.data_dir)
    try:
        asset_type = repo.resolve_asset_type(symbol)
    except Exception:  # noqa: BLE001
        asset_type = "stock"
    if asset_type != "stock":
        return {
            "symbol": symbol,
            "available": False,
            "reason": f"{asset_type.upper()} 没有股东户数与十大流通股东 —— 这类数据只对个股披露",
            "holder": {"latest": None, "trend": [], "percentile_basis": "同报告期全市场横截面"},
            "top_float": {"period_end": None, "rows": [], "org_ratio": None},
            "lift": [],
            "pressure": {
                "turnover_pct": None, "holder_pct": None,
                "as_of": cn_today().isoformat(), "turnover_window": 20,
                "quadrant": {"key": None, "label": "不适用", "stats": None, "all": None},
                "stats_meta": None,
            },
        }

    code = _symbol_code(symbol)
    holder = _holder_num(data_dir)
    trend = _holder_trend(holder, code, periods=10) if not holder.is_empty() else []
    stats = load_quadrant_stats(data_dir)

    latest = trend[-1] if trend else None
    p_hy = latest.get("pct_yoy") if latest else None
    to_pct = market_turnover_pct(repo)
    p_to = to_pct.get(symbol)

    return {
        "symbol": symbol,
        "available": bool(trend),
        "reason": None if trend else "本地没有这只票的股东户数（未上市 / 非 A 股 / 2014 年前退市）",
        "holder": {
            "latest": latest,
            "trend": trend,
            # 口径必须随数字一起走：分位是"同报告期全市场"，不是该票自身时序
            "percentile_basis": "同报告期全市场横截面",
        },
        "top_float": _top_float(data_dir, code),
        "lift": _lift(data_dir, code),
        "pressure": {
            "turnover_pct": _round(p_to, 3),
            "holder_pct": _round(p_hy, 3),
            "as_of": cn_today().isoformat(),
            "turnover_window": 20,
            "quadrant": _quadrant(p_to, p_hy, stats),
            "stats_meta": (stats or {}).get("meta"),
        },
    }
