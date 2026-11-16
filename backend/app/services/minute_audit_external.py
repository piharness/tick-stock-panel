"""分钟审计的外部轴：fuyao 日线作为**独立来源**。

本地日线与被检的分钟表可能同源（同一次同步的两个产物），拿它当「真值」是自证。
fuyao 走独立数据链路，一次 10d dump（约 1MB）覆盖全市场，所以不是抽样 ——
覆盖窗口内全部 `(day, symbol)` 逐只比。

与 S 轴（日线自身均价 vs 本行 low/high）互补，各有各看不见的那一半：
* 两侧都含大宗/盘后成交时 Σ 对账**恒等**，只有 S 看得见（920640 / 920964 / 920375）；
* 成交价落在 [low, high] 之内时 S 恒绿，只有外部轴看得见（920112 11-05）。
2026-11-13 实测 55 个可疑组合：本地日线 ≠ fuyao **0 处**，Σmin 全量偏差 **55/55**，
Σmin 网格偏差 **1/55** —— 否掉「是不是本表自己错了」的那一票在外部轴上。

默认不联网：只在显式 `--external` 时取；网络/依赖异常一律降级为「不可用」，
不让一条观测轴把整次审计带崩。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl


def external_check(
    data_dir: Path, frames: dict[str, pl.DataFrame]
) -> tuple[pl.DataFrame | None, str]:
    """返回 (逐 (day,symbol) 的本地/外部并列表, 说明)。

    `frames` 由 `minute_audit.audit_days(..., keep=...)` 产出（参与 M 对账的行）。
    任何异常都退化成 (None, 原因)，调用方只打印不判定。
    """
    if not frames:
        return None, "无可比数据"
    local = pl.concat([
        f.with_columns(pl.lit(d).alias("day")).select(
            pl.lit(d).alias("day"), "symbol", "dvol", "damt", "m_all", "m_can")
        for d, f in frames.items()
    ])
    symbols = sorted(set(local["symbol"].to_list()))
    try:
        from app.plugins.fuyao import provider as fuyao

        ok, why = fuyao.availability()
        if not ok:
            return None, why
        prov = fuyao.FuyaoProvider()
        try:
            lo = datetime.fromisoformat(min(frames))
            hi = datetime.fromisoformat(max(frames))
            ext = prov.get_daily(symbols, lo - timedelta(days=2), hi + timedelta(days=2))
        finally:
            prov.close()
    except Exception as e:  # noqa: BLE001 — 网络/依赖一律降级，不让判据崩
        return None, f"{type(e).__name__}: {e}"
    if ext is None or ext.is_empty():
        return None, "外部源返回空"
    ext = ext.select(
        pl.col("symbol").cast(pl.Utf8), pl.col("date").cast(pl.Date),
        pl.col("volume").cast(pl.Float64).alias("fvol"),
    ).with_columns(pl.col("date").cast(pl.Utf8).alias("day"))
    return local.join(ext, on=["day", "symbol"], how="left"), f"{len(symbols)} 只"
