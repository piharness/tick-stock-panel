"""新浪财经公开行情 HTTP 客户端。

职责: 把两类公开端点解成结构化行, 不做字段归一/单位换算 (那是 provider 层的事)。

  - 批量快照  GET https://hq.sinajs.cn/list=sh600000,sz000001,...
              响应是 GBK 纯文本, 每行 ``var hq_str_<code>="f0,f1,...";``。
              必须带 Referer, 否则 403。
  - 分钟K     GET https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
              返回 JSON 数组 (可能为 "null"), 每根 ``{day, open, high, low, close, volume, amount}``。

实测容量 (2026-11-12, 本机):
  - 批量快照: 800 只/请求返回完整 800 行、389ms; 900 只触发 HTTP 431
    (Request Header Fields Too Large) → 单请求硬上限 800。
  - 分钟K: datalen=1023 返回 1023 根 (≈4.3 个交易日); datalen=2000 → null
    (服务端硬顶 1023)。没有 offset/日期入参, 因此深度就是 ~4 天, 无法翻页。
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)

QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
MINUTE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
REFERER = "https://finance.sina.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# 单请求上限: 实测 900 只 → HTTP 431, 800 只正常
MAX_BATCH = 800
# 分钟K 单次最大根数 (服务端硬顶)
MINUTE_MAX_BARS = 1023


class SinaError(Exception):
    """新浪接口错误 (网络失败 / 非 200 / 响应不可解析)。"""


class SinaClient:
    """线程安全 (httpx.Client 可并发复用)。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Referer": REFERER, "User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    # ---- 批量快照 ----
    def quotes(self, codes: list[str]) -> list[tuple[str, list[str]]]:
        """拉一批 (≤800) 新浪代码, 返回 [(sina_code, fields)]。

        无数据的代码 (退市/不存在) 响应为空串 → 直接丢弃, 不伪造行。
        """
        if not codes:
            return []
        if len(codes) > MAX_BATCH:
            raise SinaError(f"单批代码数 {len(codes)} 超过上限 {MAX_BATCH}")
        try:
            resp = self._http.get(QUOTE_URL.format(codes=",".join(codes)))
        except httpx.HTTPError as e:
            raise SinaError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise SinaError(f"HTTP {resp.status_code}: 批量快照")
        text = resp.content.decode("gbk", errors="ignore")
        out: list[tuple[str, list[str]]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("var hq_str_") or '="' not in line:
                continue
            head, _, payload = line.partition('="')
            code = head[len("var hq_str_"):]
            payload = payload.rstrip(";").rstrip('"')
            if not payload:
                continue
            out.append((code, payload.split(",")))
        return out

    # ---- 分钟K ----
    def minute_bars(self, code: str, datalen: int = MINUTE_MAX_BARS) -> list[dict]:
        """单只标的最近 datalen 根 1 分钟K (含当日未收盘的那根)。"""
        n = max(1, min(int(datalen), MINUTE_MAX_BARS))
        try:
            resp = self._http.get(
                MINUTE_URL,
                params={"symbol": code, "scale": 1, "ma": "no", "datalen": n},
            )
        except httpx.HTTPError as e:
            raise SinaError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise SinaError(f"HTTP {resp.status_code}: 分钟K {code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise SinaError(f"分钟K 响应不是 JSON: {code}") from e
        if not isinstance(payload, list):
            # 服务端无数据时返回 null / {"result":...}
            return []
        return [row for row in payload if isinstance(row, dict)]
