"""龙虎榜数据服务 (fuyao 专有) — 复盘页卡片 + AI 复盘上下文。

非路由数据集: tickflow 无对应能力, 直接经 custom_sources 调 fuyao provider;
fuyao 未配置时返回 source_unavailable 状态, 前端降级提示。

数据契约 (实测 2026-08, 文档的 limit_reason/amount 实际不返回):
- 三榜一次取齐: all/org 为股票表 (org 额外带机构 4 字段), hot_money 为席位表
- 历史榜单不可变 → 按日落 JSON 缓存 (data/dragon_tiger/date=YYYY-MM-DD.json)
- 当日榜单盘中未发布 → 自动回退上一交易日并标记 state=fallback_prev

日期解析: 接口对显式非交易日报 code=1002, 本层用本地 kline_daily 分区日期
把目标日回退到「≤目标日的最近交易日」, 规避报错。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import date as date_cls
from pathlib import Path

from app.market_time import cn_today
from app.services.trading_days import kline_trading_days

logger = logging.getLogger(__name__)

_BOARDS = ("all", "org", "hot_money")


def _local_trading_days(data_dir: Path) -> list[date_cls]:
    """本地日K覆盖的交易日 (升序)。委托 trading_days —— 唯一取数点。

    早期版本在这里自己扫 `date=YYYY-MM-DD` 目录名, 月分区 (date=2026-11)
    因此不可见, 解析结果永久停在迁移边界 2026-10-30。
    """
    return kline_trading_days(data_dir)


def resolve_trade_date(data_dir: Path, target: date_cls | None) -> date_cls | None:
    """目标日 → ≤目标日的最近本地交易日。None 或本地无更早分区 → None (由 fuyao 默认)。

    目标日早于全部本地分区 (极老的历史复盘) → 原样返回, 由调用方试显式日期,
    失败如实报 no_data, 不静默给错日期的数据。
    """
    if target is None:
        return None
    days = _local_trading_days(data_dir)
    if not days:
        return target
    candidates = [d for d in days if d <= target]
    return max(candidates) if candidates else target


def _prev_trading_day(data_dir: Path, d: date_cls) -> date_cls | None:
    days = _local_trading_days(data_dir)
    earlier = [x for x in days if x < d]
    return max(earlier) if earlier else None


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        return None
    return custom_sources.get_provider("fuyao")


def _fetch_boards(provider, d: date_cls) -> dict:
    """三榜取齐。任一榜失败抛 FuyaoError (整日失败, 不留半套缓存)。"""
    return {
        bt: _boards_of(provider, bt, d.isoformat()) for bt in _BOARDS
    }


def _boards_of(provider, board_type: str, iso: str | None) -> dict:
    data = provider.dragon_tiger(board_type, iso)
    return {
        "trade_date": data.get("trade_date"),
        "stock_count": data.get("stock_count"),
        "count": data.get("count"),
        "stock_items": normalize_stock_items(data.get("stock_items") or []),
        "hot_money_items": normalize_hot_money_items(data.get("hot_money_items") or []),
    }


_TICKER_RE = re.compile(r"^(\d{6})(?:\.[A-Za-z]{2})?$")


def derive_thscode(ticker: object) -> str | None:
    """由 ticker 推导带市场后缀的 thscode (如 002731 → 002731.SZ)。

    fuyao 对退市整理期股 / B 股返回的 thscode 可能为 null (ticker 有值),
    不能把 null 透传给前端 (各消费方按 string 契约使用)。规则:
      6xxxxx / 900xxx (沪B)         → .SH
      0xxxxx / 3xxxxx / 200xxx(深B)  → .SZ
      4xxxxx / 8xxxxx (北交所)       → .BJ
    """
    if not isinstance(ticker, str):
        return None
    raw = ticker.strip()
    m = _TICKER_RE.match(raw)
    if not m:
        return None
    if "." in raw:
        return raw
    code = m.group(1)
    if code.startswith("900") or code[0] == "6":
        return f"{code}.SH"
    if code.startswith("200") or code[0] in ("0", "3"):
        return f"{code}.SZ"
    if code[0] in ("4", "8"):
        return f"{code}.BJ"
    return None


def normalize_stock_items(items: object) -> list[dict]:
    """榜单个股行规范化: thscode 缺失时由 ticker 推导, 两者都不可用则丢弃并告警。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    dropped: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("thscode")
        if not isinstance(code, str) or not code.strip():
            derived = derive_thscode(item.get("ticker"))
            if derived is None:
                dropped.append(str(item.get("name") or item.get("ticker") or "?"))
                continue
            item = {**item, "thscode": derived}
        out.append(item)
    if dropped:
        logger.warning(
            "龙虎榜 %d 条标的缺少可识别代码 (thscode/ticker 均不可用), 已跳过: %s",
            len(dropped), dropped[:5])
    return out


def normalize_hot_money_items(items: object) -> list[dict]:
    """游资席位行: 席位本身无代码, 但关联股票 rows 必须带可用 thscode。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for seat in items:
        if not isinstance(seat, dict):
            continue
        rows = seat.get("rows")
        if isinstance(rows, list):
            seat = {**seat, "rows": normalize_stock_items(rows)}
        out.append(seat)
    return out


def normalize_payload(payload: dict) -> dict:
    """规范化整个龙虎榜容器 (新拉取与历史缓存读回共用)。"""
    for board in ("all", "org"):
        b = payload.get(board)
        if isinstance(b, dict):
            payload[board] = {
                **b,
                "stock_items": normalize_stock_items(b.get("stock_items") or []),
            }
    hot = payload.get("hot_money")
    if isinstance(hot, dict):
        payload["hot_money"] = {
            **hot,
            "hot_money_items": normalize_hot_money_items(hot.get("hot_money_items") or []),
        }
    return payload


def _cache_path(data_dir: Path, d: date_cls) -> Path:
    return data_dir / "dragon_tiger" / f"date={d.isoformat()}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 旧版本写入的缓存可能已含 thscode=null (fuyao 退市/B股数据), 读回时同样规范化
    return normalize_payload(data) if isinstance(data, dict) else None


def _store_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_dragon_tiger(data_dir: Path, target: date_cls | None = None) -> dict:
    """取龙虎榜 (三榜)。返回给前端的统一容器。

    state: ok (正常) | fallback_prev (当日未发布, 已回退上一期并带上一期数据)
          | source_unavailable (未配置 fuyao) | no_data (拉取失败)
    """
    # 无目标日 → 最近本地交易日 (走缓存判定; 本地无任何分区才让 fuyao 自取默认)
    if target is None:
        days = _local_trading_days(data_dir)
        target = max(days) if days else None
    trade_date = resolve_trade_date(data_dir, target)
    today = cn_today()

    # 历史日缓存优先 (纯本地, 不触发插件注册表加载)
    if trade_date is not None and trade_date < today:
        cached = _load_cache(_cache_path(data_dir, trade_date))
        if cached is not None:
            return cached

    provider = _provider()
    if provider is None:
        return {"state": "source_unavailable"}

    from app.plugins.fuyao.client import FuyaoError

    # 日期未知 (target=None / 本地无分区) → 省略 date 让 fuyao 取最近已发布交易日
    explicit = trade_date.isoformat() if trade_date is not None else None
    try:
        raw = {bt: _boards_of(provider, bt, explicit) for bt in _BOARDS}
        try:
            actual = date_cls.fromisoformat(str(raw["all"].get("trade_date")))
        except ValueError:
            actual = None
        payload = {
            "state": "ok",
            "requested_date": explicit,
            "trade_date": raw["all"].get("trade_date"),
            **raw,
        }
        # 历史日不可变 → 落缓存; 当日不缓存 (盘中 fallback / 盘后补充都以现拉为准)
        if actual is not None and actual < today:
            with contextlib.suppress(OSError):
                _store_cache(_cache_path(data_dir, actual), payload)
        return payload
    except FuyaoError as e:
        # 显式日期失败 (当日未发布 / 边界日) → 回退上一交易日一次
        if trade_date is not None:
            prev = _prev_trading_day(data_dir, trade_date)
            if prev is not None:
                try:
                    cached_prev = _load_cache(_cache_path(data_dir, prev))
                    if cached_prev is not None:
                        base = dict(cached_prev)
                        base.pop("state", None)
                        return {**base, "state": "fallback_prev",
                                "requested_date": explicit}
                    raw_prev = _fetch_boards(provider, prev)
                    base = {
                        "requested_date": prev.isoformat(),
                        "trade_date": prev.isoformat(),
                        **raw_prev,
                    }
                    with contextlib.suppress(OSError):
                        _store_cache(_cache_path(data_dir, prev), {**base, "state": "ok"})
                    return {**base, "state": "fallback_prev",
                            "requested_date": explicit}
                except FuyaoError:
                    pass
        logger.warning("龙虎榜拉取失败: %s", e)
        return {"state": "no_data", "message": str(e)}


def has_cache(data_dir: Path, d: date_cls) -> bool:
    """该交易日是否已有本地缓存 —— 增量任务的幂等判据。"""
    return _cache_path(data_dir, d).exists()


def pull_latest(data_dir: Path, expect: date_cls | None = None) -> dict:
    """盘后增量: 把「服务端最近已发布交易日」的三榜写进本地缓存。

    为什么不能复用 get_dragon_tiger: 那条在线路径只在 actual < today 时写缓存
    (历史榜单不可变, 当日以现拉为准), 而增量任务跑的时刻「今天」正是目标日
    ⇒ 拿它做每日增量永远写不出当日缓存。这是历史缓存长期缺失的直接原因。

    expect 默认取今天。返回里的 is_expected_day 才是完成判据:
    服务端尚未发布目标日时会返回上一交易日, state 仍是 ok, 只看 state 会得到
    「每天都绿、但每天都没写今天」的假成功。
    """
    provider = _provider()
    if provider is None:
        return {"state": "source_unavailable"}

    from app.plugins.fuyao.client import FuyaoError

    try:
        raw = {bt: _boards_of(provider, bt, None) for bt in _BOARDS}
    except FuyaoError as e:
        logger.warning("龙虎榜增量拉取失败: %s", e)
        return {"state": "no_data", "message": str(e)}

    iso = raw["all"].get("trade_date")
    try:
        actual = date_cls.fromisoformat(str(iso))
    except ValueError:
        return {"state": "no_data", "message": f"trade_date 不可解析: {iso!r}"}

    payload = {"state": "ok", "requested_date": None, "trade_date": iso, **raw}
    try:
        _store_cache(_cache_path(data_dir, actual), payload)
    except OSError as e:
        # 拉到了但没存下来 ⇒ 必须显式失败, 不能当成功 (次日即超出一年窗口就永久缺)
        logger.warning("龙虎榜缓存写入失败 %s: %s", actual, e)
        return {"state": "store_failed", "trade_date": iso, "message": str(e)}

    expected = expect if expect is not None else cn_today()
    return {
        "state": "ok",
        "trade_date": iso,
        "expected_date": expected.isoformat(),
        "is_expected_day": actual == expected,
        "stocks": len((raw.get("all") or {}).get("stock_items") or []),
        "seats": len((raw.get("hot_money") or {}).get("hot_money_items") or []),
    }


def build_recap_context(data_dir: Path) -> str:
    """AI 复盘的龙虎榜摘要段 (纯文本, 失败返回空串不影响复盘)。"""
    try:
        payload = get_dragon_tiger(data_dir, None)
        if payload.get("state") not in ("ok", "fallback_prev"):
            return ""
        items = payload.get("all", {}).get("stock_items") or []
        org_items = payload.get("org", {}).get("stock_items") or []
        hm_items = payload.get("hot_money", {}).get("hot_money_items") or []
        trade_date = payload.get("trade_date") or ""
        lines = [f"(数据日期: {trade_date})"]

        top_buy = sorted(
            [i for i in items if (i.get("net_value") or 0) > 0],
            key=lambda x: x.get("net_value") or 0, reverse=True,
        )[:5]
        if top_buy:
            lines.append("净买入居前: " + "; ".join(
                f"{i.get('name')}({i.get('thscode')}) 净买{float(i.get('net_value') or 0)/1e8:.2f}亿"
                f" 涨跌{(i.get('change') or 0)*100:.1f}%"
                + (" [3日榜]" if i.get("range_days") == 3 else "")
                for i in top_buy))
        top_sell = sorted(
            [i for i in items if (i.get("net_value") or 0) < 0],
            key=lambda x: x.get("net_value") or 0,
        )[:5]
        if top_sell:
            lines.append("净卖出居前: " + "; ".join(
                f"{i.get('name')} 净卖{abs(float(i.get('net_value') or 0))/1e8:.2f}亿"
                for i in top_sell))
        if org_items:
            lines.append("机构净买居前: " + "; ".join(
                f"{i.get('name')} 机构净买{float(i.get('org_net_value') or 0)/1e8:.2f}亿"
                for i in sorted(org_items, key=lambda x: x.get("org_net_value") or 0, reverse=True)[:5]))
        if hm_items:
            lines.append("活跃游资: " + "; ".join(
                f"{h.get('name')} 净买{float(h.get('buying') or 0)/1e8:.2f}亿"
                for h in sorted(hm_items, key=lambda x: x.get('buying') or 0, reverse=True)[:5]))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — 摘要失败不影响复盘主流程
        logger.debug("龙虎榜复盘摘要构建失败: %s", e)
        return ""
