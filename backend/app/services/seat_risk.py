"""持仓排雷：席位锁仓状态查询。

只读 `data/dragon_tiger_seats/seat_relay_state.parquet`（由
`analysis/_build_seat_relay_state.py` 离线生成）。运行时不重算 relay5 / streak ——
那两个字段需要多日回溯，在请求里算既慢又会与诊断脚本形成第二份定义。

## 语义边界（重要）

- 这张表只覆盖**上过龙虎榜**的股票。持仓里的 ETF、未上榜个股查不到 = 无状态，
  不是「安全」，前端必须显示为「无数据」而不是「正常」。
- `level=review` 只表示「该人工复核」，依据是边缘显著的统计（按日 t=−1.86），
  不构成卖出指令。任何自动化卖出都必须另行预注册验证。
- 表的最新日期取决于席位抓取边界，与「今天」可能不同；调用方必须把 `as_of` 透出。
"""
from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

import polars as pl

STATE_FILE = "seat_relay_state.parquet"

_COLS = ("symbol", "date", "relay5", "net5_ratio", "streak", "dist_hi", "level")


def load_state(data_dir: Path, as_of: date_cls | None = None) -> tuple[str | None, dict[str, dict]]:
    """返回 (state_date, {symbol: state})；缺表返回 (None, {})。

    `as_of` 给定时只取 <= as_of 的最近一天（防未来函数）。
    """
    path = Path(data_dir) / "dragon_tiger_seats" / STATE_FILE
    if not path.is_file():
        return None, {}
    try:
        df = pl.read_parquet(path, columns=list(_COLS))
    except Exception:
        return None, {}
    if df.is_empty():
        return None, {}

    if as_of is not None:
        df = df.filter(pl.col("date") <= as_of.isoformat())
        if df.is_empty():
            return None, {}
    latest = df["date"].max()
    day = df.filter(pl.col("date") == latest)

    out: dict[str, dict] = {}
    for r in day.to_dicts():
        out[str(r["symbol"])] = {
            "relay5": None if r["relay5"] is None else round(float(r["relay5"]), 3),
            "net5_ratio": None if r["net5_ratio"] is None else round(float(r["net5_ratio"]), 4),
            "streak": int(r["streak"]) if r["streak"] is not None else None,
            "dist_hi": None if r["dist_hi"] is None else round(float(r["dist_hi"]), 4),
            "level": str(r["level"]),
        }
    return str(latest), out


def pick(state: dict[str, dict], symbols: list[str]) -> dict[str, dict]:
    """只取请求的标的，保持调用方顺序无关。"""
    return {s: state[s] for s in symbols if s in state}
