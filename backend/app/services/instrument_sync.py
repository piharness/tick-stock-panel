"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.tickflow import client as tf_client
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]

#: 单交易所拉取的退避重试次数与基数 (秒)。free-api 的 BJ 端点在 09:10 会 503,
#: 同一函数 15:35 能成功 ⇒ 瞬时性成立, 但重试不能代替「失败时保留旧行」。
_EXCHANGE_RETRIES = 3
_RETRY_BACKOFF_S = 2.0


def _flatten_instruments(items: list[dict], *, current_limits: bool = True) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up") if current_limits else None
        row["limit_down"] = ext.get("limit_down") if current_limits else None
        rows.append(row)
    return rows


def _fetch_instruments_via_provider() -> list[dict] | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(仍应走 tickflow)时返回 None。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def sync_instruments(
    data_dir: Path,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。

    on_progress: 可选的逐交易所进度回调(只传一句人读文案)。全市场维表同步在
    进度条上只占 2%→8%, 而三家交易所各要 30-40s —— 没有这条链路时, 页面会
    在外观上「卡在 2%」两三分钟, 和真卡死无法区分。

    ## 为什么必须逐交易所补齐 (2026-11-13)

    旧实现里三个交易所共用一个 try: 单个交易所失败只记一条 WARNING, 其余照常
    写入。free-api 的 BJ 端点在 09:10 连续两天返回 503:

        2026-11-12 09:10:21 WARNING get_instruments(BJ) failed: 503
        2026-11-13 09:10:20 WARNING get_instruments(BJ) failed: 503
        2026-11-12 15:35:08 INFO    instruments BJ: 355 stocks   ← 下午那次成功过

    于是 15:35 写好的 355 只北交所被次日 09:10 的「SH+SZ」整表覆盖 ⇒ 维表里
    一只 BJ 都不剩, 而日线仍有 355 只。任何 `join instruments` 静默丢掉它们
    (2026-11-12 实测占全市场成交额 0.72%), 覆盖率判据也看不见 —— 因为它只用
    维表自身行数算下限。

    现在: 失败交易所按 3 次退避重试; 仍失败则**用旧表里该交易所的行补齐**;
    旧表也没有该交易所时直接抛错, 绝不用残缺结果覆盖完整维表。
    """
    all_rows = _fetch_instruments_via_provider()
    failed: list[str] = []
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        tf = get_client()
        # 免费接口的限价来自历史行情，不能盖上同步日期当作当日权威价。
        current_limits = tf_client.current_mode() == "api_key"
        all_rows = []
        for idx, ex in enumerate(_EXCHANGES, start=1):
            # 单个交易所实测可占 30-40s(响应头已 200, 慢在 body 解析), 三家的
            # 沉默加起来足够让页面停在 2% 三分钟。逐交易所回报, 用户能看出在动。
            if on_progress is not None:
                on_progress(f"个股维表 {ex}（{idx}/{len(_EXCHANGES)}）…")
            rows = _fetch_exchange_rows(tf, ex, current_limits=current_limits)
            if rows is None:
                failed.append(ex)
            else:
                all_rows.extend(rows)
                logger.info("instruments %s: %d stocks", ex, len(rows))

    if not all_rows:
        # 一个都没拉到 = 数据源整体不可用, 没有可写的信息: 保留旧表, 返回 0
        # (既有语义)。只有「部分成功」才会写出缩水的表, 那才是必须拦的。
        return 0

    df = pl.DataFrame(all_rows) if all_rows else pl.DataFrame({"symbol": []})
    existing = _read_existing_instruments(data_dir)
    if failed and existing is not None and "exchange" in existing.columns:
        # 失败交易所沿用旧行: 宁可用昨天的 355 只, 也不要今天的 0 只。
        kept = existing.filter(pl.col("exchange").is_in(failed))
        if kept.height:
            df = pl.concat([df, kept], how="diagonal_relaxed").unique(
                subset=["symbol"], keep="first",
            )
            logger.warning(
                "instruments: %s 拉取失败, 已用历史维表补齐 %d 行 (合计 %d)",
                "/".join(failed), kept.height, df.height,
            )
    if failed:
        missing = [
            ex for ex in failed
            if existing is None or "exchange" not in existing.columns
            or existing.filter(pl.col("exchange") == ex).height == 0
        ]
        if missing:
            raise RuntimeError(
                f"标的维表拉取不完整且无历史行可补: {'/'.join(missing)}。"
                "拒绝用残缺结果覆盖维表, 请稍后重试或检查数据源"
            )

    df = _preserve_share_fields(df, data_dir)
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    by_ex = (
        df.group_by("exchange").len().sort("exchange").rows()
        if "exchange" in df.columns else []
    )
    logger.info(
        "instruments synced: %d rows → %s (%s)", df.height, out,
        " ".join(f"{ex}={n}" for ex, n in by_ex),
    )
    return df.height


def _read_existing_instruments(data_dir: Path) -> pl.DataFrame | None:
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.is_file():
        return None
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001 — 旧表读不了不该阻断本次同步
        logger.warning("instruments 历史维表读取失败: %s", e)
        return None


def _fetch_exchange_rows(tf, ex: str, *, current_limits: bool) -> list[dict] | None:
    """单个交易所的标的列表, 3 次退避重试后仍失败返回 None。

    端点 503 是瞬时的 (同一函数在 11-12 15:35 成功过), 但只重试不够 ——
    09:10 与 15:35 的差别可能是网关限流, 重试仍可能全灭, 因此调用方必须能
    回落到旧表。
    """
    for attempt in range(1, _EXCHANGE_RETRIES + 1):
        try:
            items = tf.exchanges.get_instruments(ex, instrument_type="stock")
            if items:
                return _flatten_instruments(items, current_limits=current_limits)
            logger.warning("instruments %s 返回空列表 (%d/%d)", ex, attempt, _EXCHANGE_RETRIES)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "get_instruments(%s) failed (%d/%d): %s",
                ex, attempt, _EXCHANGE_RETRIES, str(e).replace("\n", " ")[:180],
            )
        if attempt < _EXCHANGE_RETRIES:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    return None


# 股本与静态交易规则字段: 部分行情源的标的维表只有代码/名称/市场/类型。
# 这些字段缺失会让 enriched 的 turnover_rate / 市值为空, 进而使
# 依赖换手率与流通市值的策略筛不出任何标的 —— 因此绝不能用 null 覆盖已有值。
# 涨跌停价随交易日变化，不从历史维表继承。
_SHARE_FIELDS = (
    "listing_date", "total_shares", "float_shares", "tick_size",
)


def _preserve_share_fields(df: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    """用现有维表补全 provider 未提供的股本/交易规则字段 (按 symbol 左连接)。

    新增标的仍保留 null (没有历史值可用, 不伪造); 只在同一 symbol 上把
    provider 的空值替换为既有值, 并在日志里给出补全规模。
    """
    available = [c for c in _SHARE_FIELDS if c in df.columns]
    if not available:
        return df
    if all(df[c].null_count() == 0 for c in available):
        return df  # provider 已提供全部字段, 无需补全

    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        logger.warning(
            "instruments provider 未提供股本字段且无历史维表可补全: "
            "enriched 换手率/市值将为 null, 依赖它们的策略会筛不出标的"
        )
        return df
    try:
        existing = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 历史维表读取失败, 无法补全股本字段: %s", e)
        return df
    if "symbol" not in existing.columns:
        return df

    keep = ["symbol", *[c for c in available if c in existing.columns]]
    if len(keep) <= 1:
        return df
    merged = df.join(existing.select(keep), on="symbol", how="left", suffix="_prev")
    filled: list[str] = []
    for col in keep[1:]:
        prev = f"{col}_prev"
        if prev not in merged.columns:
            continue
        # 逐行 coalesce: provider 有值用 provider, 为空才回落历史值。这样
        # 「部分标的缺字段」也能补齐, 而不只处理整列为空的源。
        if merged[col].null_count() and merged[prev].is_not_null().any():
            merged = merged.with_columns(
                pl.coalesce(pl.col(col), pl.col(prev)).alias(col)
            )
            filled.append(col)
    merged = merged.drop([f"{c}_prev" for c in keep[1:] if f"{c}_prev" in merged.columns])
    if filled:
        restored = int(merged.select(
            [pl.col(c).is_not_null().sum() for c in filled]
        ).row(0)[0])
        logger.info(
            "instruments provider 缺 %s, 已逐行从历史维表补全 (首列非空 %d/%d)",
            "/".join(filled), restored, merged.height,
        )
    return merged


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
