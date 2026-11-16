"""通达信裸协议 HTTP-less 客户端 (tdxrs 传输层)。

为什么单独一层: tdxrs 是 Rust/PyO3 扩展, 有两类宿主进程无法承受的行为 ——
Rust panic 会以 `PanicException` (BaseException 子类, 不是 Exception) 抛出,
以及单连接状态在中途可能失效。上层 provider 不应关心这些, 因此这里统一
做三件事:

  1. 懒连接 + 单次失败重连重试 (失败仍抛 TdxError, 由调用方按批隔离);
  2. `except BaseException` 兜底捕获 panic, 绝不让它冒泡到轮询线程;
  3. 响应代码回读校验 (见 `quotes`): 服务端对无法识别的北交所旧代码会
     返回别的代码的数据 (实测 430047 → 600839), 不校验就等于静默串票。

协议本身无 Key、无积分、无 HTTP 端点, 默认端口 7709。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# 单请求上限: 实测 61 只起服务端只回 60 行 (静默截断, 不报错)
MAX_QUOTE_BATCH = 60
# 单次 K 线根数上限 (协议硬顶, 传更大也只回 800)
MAX_BARS = 800

MARKET_SZ = 0
MARKET_SH = 1
MARKET_BJ = 2

CATEGORY_1MIN = 7
CATEGORY_DAILY = 9


class TdxError(RuntimeError):
    """通达信协议调用失败 (连接/解析/panic)。调用方须按批隔离, 不得当成空数据。"""


def module_missing_reason() -> str | None:
    """tdxrs 不可用的原因; 可用返回 None。

    必须在插件注册前回答"为什么不可用", 而不是等第一次拉取才炸 ——
    设置页的灰显卡片要能显示具体原因。
    """
    if sys.version_info[:2] != (3, 11):
        return (
            f"tdxrs 仅提供 cp311 wheel (当前 Python {sys.version_info.major}."
            f"{sys.version_info.minor}); 请用 Python 3.11 运行后端 (Docker 镜像即 3.11)"
        )
    try:
        if importlib.util.find_spec("tdxrs") is None:
            return "tdxrs 未安装 (pip install tdxrs)"
    except Exception as e:  # noqa: BLE001 — 损坏安装时 find_spec 也可能抛
        return f"tdxrs 安装损坏: {e}"
    return None


class TdxClient:
    """线程独占的单连接客户端 (tdxrs client 非线程安全, 每线程各持一个)。

    `servers` 为空时由 tdxrs 自动选最快节点; 环境变量 TDX_SERVERS 可指定
    "ip:port,ip:port" 覆盖 (多机部署或默认列表失联时使用)。
    """

    def __init__(self, servers: list[tuple[str, str, int]] | None = None) -> None:
        self._servers = servers or _servers_from_env()
        self._client: Any | None = None
        self._lock = threading.Lock()
        self.calls = 0

    # ---- 生命周期 ----
    def _ensure(self) -> Any:
        if self._client is not None:
            return self._client
        from tdxrs import TdxHqClient  # 延迟导入: 未安装时插件已提前灰显

        client = TdxHqClient()
        if self._servers:
            client.set_servers(self._servers)
        client.connect_to_any()
        self._client = client
        return client

    def _reset(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except BaseException:  # noqa: BLE001 — 断连失败不影响重建
            pass

    def close(self) -> None:
        with self._lock:
            self._reset()

    def _call(self, fn: str, *args: Any, **kwargs: Any) -> Any:
        """调用一个 tdxrs 方法, 失败重连重试一次。

        panic 是 BaseException, 必须单独兜住: 否则会穿过 quote_service 的
        `except Exception` 直接终止盘中轮询线程。
        """
        last: BaseException | None = None
        for attempt in range(2):
            with self._lock:
                try:
                    client = self._ensure()
                    self.calls += 1
                    return getattr(client, fn)(*args, **kwargs)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as e:  # noqa: BLE001 — 含 PanicException
                    last = e
                    self._reset()
            if attempt == 0:
                time.sleep(0.2)
        raise TdxError(f"{fn} 失败: {type(last).__name__}: {last}") from None

    # ---- 接口 ----
    def quotes(self, pairs: list[tuple[int, str]]) -> list[dict]:
        """批量五档快照 (≤60 只/请求)。

        返回行里的 `code` 必须与请求代码逐条相等: 北交所老代码 (43x/83x/87x)
        实测会被服务端映射成别的票, 只按位置对齐会静默污染行情表。
        """
        if not pairs:
            return []
        if len(pairs) > MAX_QUOTE_BATCH:
            raise ValueError(f"quotes 单批最多 {MAX_QUOTE_BATCH} 只, 收到 {len(pairs)}")
        rows = self._call("get_security_quotes", pairs) or []
        wanted = {code for _, code in pairs}
        out: list[dict] = []
        for row in rows:
            code = str(row.get("code") or "")
            if code not in wanted:
                logger.debug("通达信返回了非请求代码 %s (请求 %s), 已丢弃", code, sorted(wanted)[:3])
                continue
            out.append(row)
        return out

    def bars(self, market: int, code: str, category: int, offset: int, count: int) -> list[dict]:
        if count > MAX_BARS:
            count = MAX_BARS
        return self._call("get_security_bars", category, market, code, offset, count) or []

    def history_minute_time(self, market: int, code: str, date_int: int) -> list[dict]:
        """历史分时 (按日期, 240 点/日, 只含 close/avg_price/vol)。

        用途: 1 分钟 K 只保留最近 ~98 个交易日, 更早的精细度只剩分时。
        价格口径为当日真实成交价 (非复权), 与分钟 K 同源。
        """
        return self._call("get_history_minute_time_data", market, code, date_int) or []


def _servers_from_env() -> list[tuple[str, str, int]]:
    raw = str(os.environ.get("TDX_SERVERS") or "").strip()
    if not raw:
        return []
    out: list[tuple[str, str, int]] = []
    for i, item in enumerate(raw.split(",")):
        host, _, port = item.strip().partition(":")
        if not host:
            continue
        try:
            out.append((f"env{i}", host, int(port or 7709)))
        except ValueError:
            continue
    return out
