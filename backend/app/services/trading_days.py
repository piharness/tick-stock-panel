"""已知交易日集合的唯一取数点 = kline_daily 分区（日分区 + 月分区双布局）。

为什么单独成模块: dragon_tiger 与 auction_benchmark 曾各写一份
`^date=(\\d{4}-\\d{2}-\\d{2})$` 的目录名正则。日 K 迁到月分区
(`data/kline_daily/date=2026-11`) 后, 两份正则都只看得见迁移边界前的日分区,
于是「≤目标日的最近交易日」永远解析成 2026-10-30 —— 而这两个消费者都指望它
决定榜单日期。

回归症状 (2026-11-13 实测): `/api/market-recap/dragon-tiger?date=2026-11-13`
返回 10-30 的榜单且 state=ok; 上游当日 66 只其实就在一次请求之外。页面把 14 天
滞后解释成「龙虎榜 T 日盘后才公布, 是信息到达的硬约束」——把缺陷讲成了规律。

判据: 本函数对 2026-11 的月分区必须返回与 `kline_daily` 数据列一致的日期集合;
日分区仍走目录名 (零 parquet 扫描)。

性能 (2026-11-20 修正): 早期注释写「9p 挂载实测 17ms/次, 不需要缓存」—— 那是在
月分区还只有十几个的时候量的。现在 `kline_daily` 有 **246 个月分区**,
`list_partition_dates` 要对每个分区 `scan_parquet` 读 `date` 列, 容器内实测
**19.5s/次**。`get_dragon_tiger` 一次请求调了它两遍（`target is None` 分支 +
`resolve_trade_date`）⇒ 38.5s, 直接顶穿前端 30s 超时。

三层缓存，全部靠**内容指纹**而不是时间：
  1. 进程内 `_DAYS_CACHE` —— 稳态 0.02s；
  2. 磁盘快照 `data/_cache/trading_days/kline_daily.json` —— 重启后 0.02s
     （不用再付那 19.5s，也不必等启动预热跑完）；
  3. 指纹不匹配时重算并原子覆盖快照。

指纹必须抓住两类变化：新增交易日/跨月 → 目录名集合变；当月继续写入（目录名不变）
→ 该月分区文件的 mtime 变。月分区在字符串序上排在 `date=2026-11-19` **之前**，
所以「最后 3 个目录」可能全是日分区，必须单独盯住最新那个月分区。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from datetime import date
from pathlib import Path

from app.market_time import cn_now
from app.tickflow.partitions import iter_partition_dirs, list_partition_dates

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
#: 快照目录故意放在 `_` 前缀下：不进任何 `date=*` / `**/*.parquet` 的业务扫描
SNAPSHOT_TABLE = "kline_daily"

_DAYS_LOCK = threading.Lock()
#: 计算去重：启动预热与首个请求可能同时 miss，两遍 19s 的扫描不该发生
_DAYS_BUILD = threading.Lock()
#: data_dir → (指纹, 交易日列表)
_DAYS_CACHE: dict[str, tuple[dict, list[date]]] = {}


def _fingerprint(parts: list[Path]) -> dict:
    """目录名集合的摘要 + 最新月分区与最后几个分区的 mtime。

    返回 dict 而不是 tuple：它要被原样写进 JSON，还要能从 JSON 读回来做等值比较。
    用摘要而不是把 590 个目录名全存进去——快照文件因此只有几 KB。
    """
    names = "\n".join(p.name for p in parts)
    digest = hashlib.sha256(names.encode("utf-8")).hexdigest()[:16]
    stamps: list[list] = []
    monthly = [p for p in parts if len(p.name) == 12]
    watch = (monthly[-1:] if monthly else []) + parts[-3:]
    for p in watch:
        with contextlib.suppress(OSError):
            stamps.append([p.name, p.stat().st_mtime_ns])
    return {"dirs": len(parts), "names": digest, "stamps": stamps}


def snapshot_path(data_dir: Path) -> Path:
    return Path(data_dir) / "_cache" / "trading_days" / f"{SNAPSHOT_TABLE}.json"


def _read_snapshot(path: Path, fp: dict) -> list[date] | None:
    """指纹不匹配 / 文件损坏 / 版本不同 → None（调用方重算，不报错）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("version") != SNAPSHOT_VERSION or payload.get("fingerprint") != fp:
        return None
    try:
        # 去重 + 排序：文件被手工改坏时也不能让调用方拿到乱序/重复的日历
        return sorted({date.fromisoformat(s) for s in payload["days"]})
    except (KeyError, TypeError, ValueError):
        return None


def _write_snapshot(path: Path, fp: dict, days: list[date]) -> None:
    """原子写。失败只记日志：快照是加速手段，不是正确性前提。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": SNAPSHOT_VERSION,
                    "table": SNAPSHOT_TABLE,
                    "built_at": cn_now().isoformat(timespec="seconds"),
                    "fingerprint": fp,
                    "days": [d.isoformat() for d in days],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("交易日历快照写入失败(不影响结果): %s", exc)


def kline_trading_days(data_dir: Path) -> list[date]:
    """本地日 K 覆盖的交易日 (升序)。扫描失败返回空表, 由调用方决定降级行为。

    返回的列表是副本：调用方会 `sort(reverse=True)` / `filter` 后自用，
    共享同一个 list 对象迟早会被就地排序改坏。
    """
    base = Path(data_dir) / "kline_daily"
    parts = iter_partition_dirs(base)
    if not parts:
        return []
    key = str(data_dir)
    fp = _fingerprint(parts)
    with _DAYS_LOCK:
        hit = _DAYS_CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return list(hit[1])

    with _DAYS_BUILD:
        # 二次检查：等锁期间别的线程（启动预热）可能已经算完
        parts = iter_partition_dirs(base)
        fp = _fingerprint(parts)
        with _DAYS_LOCK:
            hit = _DAYS_CACHE.get(key)
        if hit is not None and hit[0] == fp:
            return list(hit[1])

        snap = snapshot_path(Path(data_dir))
        days = _read_snapshot(snap, fp)
        if days is None:
            days = sorted(list_partition_dates(base))
            _write_snapshot(snap, fp, days)
        with _DAYS_LOCK:
            _DAYS_CACHE[key] = (fp, days)
    return list(days)
