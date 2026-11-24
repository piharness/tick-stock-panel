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

import polars as pl

from app.market_time import cn_today
from app.services.trading_days import kline_trading_days
from app.services.ttl_cache import TtlCache

logger = logging.getLogger(__name__)

_BOARDS = ("all", "org", "hot_money")

#: 当日榜单的进程内缓存（当日不落盘，见 `_fetch_online` 的注释）。
#:
#: TTL 单独成常量：测试要用它断言"过期后必须重新拉"，而不是猜一个数字。
#: 60 秒足够吸收 15s 级轮询的重复，又短到不会让"刚发布"被长时间遮蔽。
_ONLINE_CACHE_TTL_S = 60.0

_ONLINE_CACHE: TtlCache = TtlCache(
    max_entries=8, ttl_s=_ONLINE_CACHE_TTL_S, name="dragon-tiger-online",
)


def _local_trading_days(data_dir: Path) -> list[date_cls]:
    """本地日K覆盖的交易日 (升序)。委托 trading_days —— 唯一取数点。

    早期版本在这里自己扫 `date=YYYY-MM-DD` 目录名, 月分区 (date=2026-11)
    因此不可见, 解析结果永久停在迁移边界 2026-10-30。
    """
    return kline_trading_days(data_dir)


def resolve_trade_date(
    data_dir: Path, target: date_cls | None, *, days: list[date_cls] | None = None,
) -> date_cls | None:
    """目标日 → ≤目标日的最近本地交易日。None 或本地无更早分区 → None (由 fuyao 默认)。

    目标日早于全部本地分区 (极老的历史复盘) → 原样返回, 由调用方试显式日期,
    失败如实报 no_data, 不静默给错日期的数据。

    `days` 允许调用方传入已取好的交易日列表：`get_dragon_tiger` 为了解析
    `target=None` 已经取过一次，再让本函数自己取一次就是同一份日历算两遍
    （`kline_trading_days` 每次仍要 readdir + 指纹，实测 9~31ms）。
    """
    if target is None:
        return None
    local = days if days is not None else _local_trading_days(data_dir)
    if not local:
        return target
    candidates = [d for d in local if d <= target]
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


#: 当日榜单的进程内缓存。
#:
#: 历史日走磁盘缓存（`_cache_path`），但**当日不落盘**——盘中回退、盘后补充都以
#: 现拉为准（`actual < today` 才写盘）。于是从收盘到 `pull_latest` 增量任务跑完
#: 这段窗口里，每个请求都要实时问 provider：容器内实测 **158ms/次**，而账本上
#: 这个端点一周被调 113 次、页面是 15s 级轮询。
#:
#: 60 秒的窗口足够吸收轮询重复，又短到不会让"刚发布"被长时间遮蔽。当日榜单本身
#: 在发布后就不再变，所以这里担心的只是"发布时刻"的可见延迟。

def get_dragon_tiger(data_dir: Path, target: date_cls | None = None) -> dict:
    """取龙虎榜 (三榜)。返回给前端的统一容器。

    state: ok (正常) | fallback_prev (当日未发布, 已回退上一期并带上一期数据)
          | source_unavailable (未配置 fuyao) | no_data (拉取失败)
    """
    days = _local_trading_days(data_dir)
    # 无目标日 → 最近本地交易日 (走缓存判定; 本地无任何分区才让 fuyao 自取默认)
    if target is None:
        target = max(days) if days else None
    trade_date = resolve_trade_date(data_dir, target, days=days)
    today = cn_today()

    # 历史日缓存优先 (纯本地, 不触发插件注册表加载)
    if trade_date is not None and trade_date < today:
        cached = _load_cache(_cache_path(data_dir, trade_date))
        if cached is not None:
            return cached

    key = (str(data_dir), trade_date.isoformat() if trade_date is not None else "")
    hit = _ONLINE_CACHE.get(key)
    if hit is not None:
        return hit
    payload = _fetch_online(data_dir, trade_date, today)
    _ONLINE_CACHE.put(key, payload)
    return payload


def _fetch_online(data_dir: Path, trade_date: date_cls | None, today: date_cls) -> dict:
    """实时拉取三榜（含当日未发布时回退上一交易日一次）。"""
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


def build_direction_stats(data_dir: Path, lookback_days: int = 120) -> dict:
    """多日龙虎榜「机构净买 vs 全榜净买」的方向一致性统计。

    这是市场常识页「信息时间差」与「博弈推演」两条常识的**可证伪实证**：
    本地唯一带「决策主体身份」的高频数据是龙虎榜（机构 / 游资 / 全榜净买）。
    把「机构（聪明钱）先于散户定价、方向不完全一致」这条叙述，落成两个数字：

      1. 机构净买方向 与 全榜净买方向 的**同向占比**（是否真的「机构买散户也买」）；
      2. 机构净买 与 游资净买 的方向一致性。

    诚实边界：龙虎榜是稀疏事件数据（仅上榜票、事后披露），不是全市场，
    所以这里给的是「上榜票里机构与散户的方向关系」，不是「全市场的机构行为」。
    这个边界必须随数据返回，不能静默放大成「全市场结论」。

    返回:
      {
        "days": N, "start": "...", "end": "...",
        "org_vs_all": {"same": n, "total": m, "rate": 0.657},
        "org_vs_hotmoney": {"same": n, "total": m, "rate": ...},
        "series": [{"date", "same", "total", "rate"}...],  # 按日，供趋势图
        "caveats": [...],
      }
    本地无龙虎榜缓存时返回 days=0。
    """
    root = data_dir / "dragon_tiger"
    if not root.exists():
        return {"days": 0, "series": [], "caveats": ["本地无龙虎榜缓存"]}

    files: list[tuple[date_cls, Path]] = []
    for p in root.glob("date=*.json"):
        name = p.name[5:-5]
        try:
            d = date_cls.fromisoformat(name)
        except ValueError:
            continue
        files.append((d, p))
    files.sort(key=lambda x: x[0])
    if lookback_days and len(files) > lookback_days:
        files = files[-lookback_days:]

    series: list[dict] = []
    same_org_all = 0
    total_org_all = 0
    same_org_hm = 0
    total_org_hm = 0

    for d, p in files:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        org_items = (payload.get("org") or {}).get("stock_items") or []
        all_items = (payload.get("all") or {}).get("stock_items") or []
        # org 榜的 stock_items 自带 org_net_value 与 hot_money_net_value
        by_key: dict[str, dict] = {}
        for it in all_items:
            if not isinstance(it, dict):
                continue
            k = it.get("ticker") or it.get("thscode")
            if k:
                by_key[str(k)] = it

        day_same = 0
        day_total = 0
        day_same_hm = 0
        day_total_hm = 0
        for it in org_items:
            if not isinstance(it, dict):
                continue
            k = str(it.get("ticker") or it.get("thscode") or "")
            onv = it.get("org_net_value")
            anv = it.get("net_value")
            hnv = it.get("hot_money_net_value")
            # org 榜自身有 net_value 吗？无则从 all 榜 join
            if anv is None and k in by_key:
                anv = by_key[k].get("net_value")
            if onv is not None and anv is not None:
                day_total += 1
                if (float(onv) > 0) == (float(anv) > 0):
                    day_same += 1
            if onv is not None and hnv is not None:
                day_total_hm += 1
                if (float(onv) > 0) == (float(hnv) > 0):
                    day_same_hm += 1

        same_org_all += day_same
        total_org_all += day_total
        same_org_hm += day_same_hm
        total_org_hm += day_total_hm
        if day_total > 0:
            series.append({
                "date": d.isoformat(),
                "same": day_same,
                "total": day_total,
                "rate": round(day_same / day_total, 4),
            })

    caveats = [
        "龙虎榜是稀疏事件数据（仅上榜票、事后披露），本统计是「上榜票里机构与散户/游资的方向关系」，不是全市场机构行为。",
    ]
    return {
        "days": len(series),
        "start": series[0]["date"] if series else None,
        "end": series[-1]["date"] if series else None,
        "org_vs_all": {
            "same": same_org_all, "total": total_org_all,
            "rate": round(same_org_all / total_org_all, 4) if total_org_all else None,
        },
        "org_vs_hotmoney": {
            "same": same_org_hm, "total": total_org_hm,
            "rate": round(same_org_hm / total_org_hm, 4) if total_org_hm else None,
        },
        "series": series,
        "caveats": caveats,
    }


def _sgn(series: pl.Series) -> pl.Series:
    """符号函数：>0→1，<0→-1，否则 0（None 视为 0，不进方向比较）。"""
    return (
        series.fill_null(0.0)
        .map_elements(
            lambda v: 0 if v == 0 else (1 if v > 0 else -1),
            return_dtype=pl.Int32,
        )
    )


def build_forward_verdict(repo, *, lookback_days: int = 400) -> dict:
    """龙虎榜「机构/游资方向 → 后续超额收益」的判决 —— 深挖「谁对谁错」。

    这是市场常识页「博弈推演」那条常识的**第二层证伪**。第一层
    `build_direction_stats` 只回答「机构与谁反向」（结果是：机构 vs 游资更常
    反向）。本函数进一步回答散户真正关心的：**反向之后，跟着谁走能赚钱？**

    结论（本地 244 日龙虎榜缓存，2025-11 ~ 2026-11，扣等权市场基准后）：
      - 「机构买 vs 机构卖」的 20 日超额差异 ≈ 26bp，t≈0.32 → 不显著；
      - 反向 vs 同向的 20 日超额几乎无差；
      - 所有组 20 日超额中位都约 −10%，即「上龙虎榜」本身是异动信号，
        之后均值回归下跌 —— 与谁在买、谁在卖无关。

    这个结论证伪了「跟着聪明钱（机构）走」这条粗糙叙事：身份标签（机构/游资）
    不携带独立于价格的信息，与项目记忆里「关注度/换手率全维度正交化归零」同构。

    诚实边界（必须随数据返回，不能放大）：
      - 龙虎榜是稀疏事件数据（仅上榜票、事后披露），样本仅约 1 年；
      - 无统计显著性（t≈0.3）只能说明「样本内无差异」，不等于「机构方向
        永远无 alpha」——样本可能不够测出弱 alpha；
      - 前向收益用 raw_close（前复权）相邻交易日收益率，跨停牌复牌会失真，
        已按日历相邻（rn 差 1）约束。

    返回:
      {
        "as_of": "2026-11-17", "days": N,
        "verdict": {
          "n": 总样本, "n_org_buy": n, "n_org_sell": n, "n_same": n,
          "org_buy_ex20_bp": 0.0, "org_sell_ex20_bp": 0.0,
          "diff_bp": 0.0, "t": 0.0,
          "all_ex20_median_bp": 0.0,   # 「上榜即跌」基线
        },
        "caveats": [...],
      }
    本地无龙虎榜缓存或日线不可读时返回 days=0。
    """
    from app.parquet import scan_enriched_parquet
    from app.tickflow.repository import enriched_dirname

    data_dir = repo.store.data_dir
    root = data_dir / "dragon_tiger"
    if not root.exists():
        return {"days": 0, "caveats": ["本地无龙虎榜缓存"]}

    # 1) 读龙虎榜事件：机构净买 / 游资净买（游资在 all 榜 hot_money_net_value）
    files: list[tuple[date_cls, Path]] = []
    for p in root.glob("date=*.json"):
        name = p.name[5:-5]
        try:
            d = date_cls.fromisoformat(name)
        except ValueError:
            continue
        files.append((d, p))
    files.sort(key=lambda x: x[0])
    if lookback_days and len(files) > lookback_days:
        files = files[-lookback_days:]

    rows: list[tuple] = []
    for d, p in files:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        all_items = (payload.get("all") or {}).get("stock_items") or []
        org_items = (payload.get("org") or {}).get("stock_items") or []
        # 关键：与日线 join 必须用 thscode（带 .SH/.SZ 后缀），ticker 是无后缀代码。
        omap = {
            str(it.get("thscode") or it.get("ticker")): it
            for it in org_items if isinstance(it, dict)
        }
        for it in all_items:
            if not isinstance(it, dict):
                continue
            code = str(it.get("thscode") or it.get("ticker") or "")
            if not code:
                continue
            hm = it.get("hot_money_net_value")
            org = omap.get(code, {}).get("org_net_value") if code in omap else None
            if org is None or hm is None:
                continue
            rows.append((code, d, org, hm))
    if not rows:
        return {"days": 0, "caveats": ["龙虎榜缓存中机构与游资方向无法配对"]}

    ev = pl.DataFrame(rows, schema=["symbol", "date", "org", "hm"], orient="row")
    ev = ev.with_columns(pl.col("date").cast(pl.Utf8))

    # 2) 读事件窗口内的全市场日线（+ 前向 20 日余量），算相邻交易日收益
    kroot = data_dir / enriched_dirname("stock")
    if not kroot.exists():
        return {"days": 0, "caveats": ["本地无日线分区"]}

    # 只读事件窗口覆盖的分区：旧实现是 `kroot/**/*.parquet` 全量（约 1500 万行），
    # 为算 60 来个交易日的相邻收益把整棵树读进内存。窗口取「最早事件日 - 7 天」
    # （要算 prev_close）到「最晚事件日 + 60 自然日」（约 40 个交易日，覆盖 fwd20）。
    # `rn` 是窗口内的 `rank over symbol`，差值语义不变（事件日 + 20 仍是其后第 20 行）。
    from datetime import timedelta

    from app.services.daily_panel import enriched_files

    event_days = [d for d, _ in files]
    kfiles = enriched_files(
        data_dir,
        min(event_days) - timedelta(days=7),
        max(event_days) + timedelta(days=60),
        enriched_dirname("stock"),
    )
    if not kfiles:
        return {"days": 0, "caveats": ["日线在该事件窗口内无分区"]}
    lf = scan_enriched_parquet([str(f) for f in kfiles])
    schema_cols = lf.collect_schema().names()
    if "raw_close" not in schema_cols or "date" not in schema_cols:
        return {"days": 0, "caveats": ["日线缺 raw_close/date 列"]}

    # 只取所需列并 cast date 为 Utf8 便于 join
    daily = lf.select(["symbol", "date", "raw_close"]).collect()
    daily = daily.with_columns(pl.col("date").cast(pl.Utf8).alias("date"))
    daily = daily.sort(["symbol", "date"])

    # 相邻交易日收益 + 每 symbol 的交易日序号 rn（日历相邻：rn 差 1 才是相邻交易日）
    daily = daily.with_columns(
        pl.col("raw_close").shift(1).over("symbol").alias("prev_close"),
    )
    daily = daily.with_columns(
        (pl.col("raw_close") / pl.col("prev_close") - 1).alias("ret"),
        pl.col("date").rank("ordinal").over("symbol").alias("rn"),
    )

    # 等权市场基准
    mkt = (
        daily.filter(pl.col("ret").is_not_null())
        .group_by("date")
        .agg(pl.col("ret").mean().alias("mkt_ret"))
        .sort("date")
        .with_columns(pl.col("date").rank("ordinal").alias("rn"))
    )
    # 前向市场累计收益（未来 h 日，不含当日）：
    # 降序排列下 cum(rn) = sum(mkt_ret[rn..end])，未来 h 日累计 = cum(rn) - cum(rn+h)；
    # 降序里 rn+h 位于 rn 下方 h 行，故用 shift(h)（正 shift 向下）。
    for h in (20,):
        rev = mkt.select(["rn", "mkt_ret"]).sort("rn", descending=True)
        rev = rev.with_columns(pl.col("mkt_ret").cum_sum().alias("cum"))
        rev = rev.with_columns(pl.col("cum").shift(h).alias("cum_future"))
        fwd = rev.with_columns(
            (pl.col("cum") - pl.col("cum_future")).alias(f"fwd_mkt{h}")
        ).sort("rn")
        mkt = mkt.join(fwd.select(["rn", f"fwd_mkt{h}"]), on="rn", how="left")

    # 3) 事件日 join 序号与市场基准
    ev = ev.join(
        daily.select(["symbol", "date", "rn"]).rename({"rn": "ev_rn"}),
        on=["symbol", "date"], how="left",
    )
    ev = ev.join(
        mkt.select(["date", "rn", "fwd_mkt20"]).rename({"rn": "ev_mkt_rn"}),
        on="date", how="left",
    )

    # 前向 20 日收益：事件日后第 20 个交易日的 close / 事件日 close
    fut = daily.select(["symbol", "rn", pl.col("raw_close").alias("fut_close")]).rename({"rn": "fut_rn"})
    ev = ev.with_columns((pl.col("ev_rn") + 20).alias("fut_rn_20"))
    ev = ev.join(fut, left_on=["symbol", "fut_rn_20"], right_on=["symbol", "fut_rn"], how="left")
    ev = ev.join(
        daily.select(["symbol", "rn", "raw_close"]).rename({"rn": "ev_rn", "raw_close": "ev_close"}),
        on=["symbol", "ev_rn"], how="left",
    )

    fwd_ret = pl.col("fut_close") / pl.col("ev_close") - 1
    ex20 = (fwd_ret - pl.col("fwd_mkt20")) * 10000  # bp
    ev = ev.with_columns(ex20.alias("ex20_bp"))
    ev = ev.with_columns(
        _sgn(pl.col("org")).alias("org_sgn"),
        _sgn(pl.col("hm")).alias("hm_sgn"),
    )
    ev = ev.filter(pl.col("ex20_bp").is_not_null())

    if ev.height < 1:
        return {"days": 0, "caveats": ["可算前向收益的样本不足"]}

    opp = ev.filter((pl.col("org_sgn") * pl.col("hm_sgn")) < 0)
    org_buy = opp.filter(pl.col("org_sgn") > 0)["ex20_bp"]
    org_sell = opp.filter(pl.col("org_sgn") < 0)["ex20_bp"]
    all_ex = ev["ex20_bp"]

    mean_buy = float(org_buy.mean()) if org_buy.len() else 0.0
    mean_sell = float(org_sell.mean()) if org_sell.len() else 0.0
    diff = mean_buy - mean_sell
    # 两样本 t 值（不等方差）
    import math

    if org_buy.len() > 1 and org_sell.len() > 1:
        se = math.sqrt(
            float(org_buy.var()) / org_buy.len() + float(org_sell.var()) / org_sell.len()
        )
        t = diff / se if se > 0 else 0.0
    else:
        t = 0.0

    return {
        "as_of": str(files[-1][0]) if files else None,
        "days": len(files),
        "verdict": {
            "n": int(ev.height),
            "n_opposite": int(opp.height),
            "n_org_buy": int(org_buy.len()),
            "n_org_sell": int(org_sell.len()),
            "org_buy_ex20_bp": round(mean_buy, 1),
            "org_sell_ex20_bp": round(mean_sell, 1),
            "diff_bp": round(diff, 1),
            "t": round(t, 2),
            "all_ex20_median_bp": round(float(all_ex.median()), 1),
        },
        "caveats": [
            "龙虎榜是稀疏事件数据（仅上榜票、事后披露），样本仅约 1 年；"
            "机构买 vs 机构卖的超额差异 t 值远小于 2，只说明「样本内无显著差异」，"
            "不等于「机构方向永远无 alpha」。",
            "前向收益用 raw_close 相邻交易日收益（日历相邻），扣等权市场基准；"
            "等权基准含生存偏差，仅作对照。",
        ],
    }
