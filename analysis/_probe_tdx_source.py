"""通达信裸协议数据源 — 能力与成本实测 (可复跑)。

用途: 插件 `app/plugins/tdx` 的每一条口径断言都来自这里; 服务端行为漂移时
(深度变浅、批量上限变化、北交所串票修复/扩大) 重跑本脚本即可发现。

运行 (必须 Python 3.11, tdxrs 只发 cp311 wheel):
    python analysis/_probe_tdx_source.py

输出: analysis/_probe_tdx_source_result.json
"""
from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tdxrs import TdxHqClient
from tdxrs.constants import KLINE_1MIN, MARKET_BJ, MARKET_SH, MARKET_SZ

OUT = Path(__file__).with_name("_probe_tdx_source_result.json")
SH_STOCK = ("600", "601", "603", "605", "688", "689")
SZ_STOCK = ("000", "001", "002", "003", "300", "301", "302")


def _client() -> TdxHqClient:
    c = TdxHqClient()
    c.connect_to_any()
    return c


def _list_all(c: TdxHqClient, market: int, pages: int) -> list[dict]:
    out: list[dict] = []
    for start in range(0, pages * 1000, 1000):
        rows = c.get_security_list(market, start) or []
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
    return out


def measure_latency(c: TdxHqClient) -> dict:
    ts = []
    for _ in range(20):
        t = time.time()
        c.get_security_count(MARKET_SH)
        ts.append(1000 * (time.time() - t))
    q = []
    codes = [(MARKET_SH, f"60{i:04d}") for i in range(0, 60)]  # 无效码也计时 (只测往返)
    for _ in range(10):
        t = time.time()
        c.get_security_quotes([(MARKET_SH, "600519")] * 0 + codes)
        q.append(1000 * (time.time() - t))
    b = []
    for _ in range(10):
        t = time.time()
        c.get_security_bars(KLINE_1MIN, MARKET_SH, "600519", 0, 800)
        b.append(1000 * (time.time() - t))
    return {
        "count_p50_ms": round(statistics.median(ts), 1),
        "quotes60_p50_ms": round(statistics.median(q), 1),
        "bars800_p50_ms": round(statistics.median(b), 1),
    }


def measure_market(c: TdxHqClient) -> dict:
    sh = _list_all(c, MARKET_SH, 28)
    sz = _list_all(c, MARKET_SZ, 25)
    sh_s = [str(x["code"]) for x in sh if str(x["code"]).startswith(SH_STOCK)]
    sz_s = [str(x["code"]) for x in sz if str(x["code"]).startswith(SZ_STOCK)]
    pool = [(MARKET_SH, x) for x in sh_s] + [(MARKET_SZ, x) for x in sz_s]
    rounds = []
    for _ in range(3):
        t = time.time()
        got = 0
        for i in range(0, len(pool), 60):
            got += len(c.get_security_quotes(pool[i : i + 60]) or [])
        rounds.append({"seconds": round(time.time() - t, 2), "rows": got})
    # 批量上限
    caps = {}
    sample = [x[1] for x in pool[:120]]
    for n in (60, 61, 80):
        rows = c.get_security_quotes([(MARKET_SH, code) for code in sample[:n]]) or []
        caps[n] = len(rows)
    return {"sh_stocks": len(sh_s), "sz_stocks": len(sz_s), "pool": len(pool),
            "rounds": rounds, "batch_cap": caps}


def measure_minute_depth(c: TdxHqClient) -> dict:
    total, pages = 0, []
    for start in range(0, 800 * 40, 800):
        rows = c.get_security_bars(KLINE_1MIN, MARKET_SH, "600519", start, 800) or []
        if not rows:
            break
        total += len(rows)
        pages.append({"start": start, "rows": len(rows),
                      "first": rows[0]["datetime"], "last": rows[-1]["datetime"]})
        if len(rows) < 800:
            break
    day = c.get_security_bars(KLINE_1MIN, MARKET_SH, "600519", 0, 250) or []
    last_day = day[-1]["datetime"][:10]
    bars_today = [r for r in day if r["datetime"].startswith(last_day)]
    return {"total_bars": total, "trading_days": round(total / 240, 1),
            "pages": len(pages), "earliest": pages[-1]["first"] if pages else None,
            "latest": pages[0]["last"] if pages else None,
            "bars_per_day": len(bars_today),
            "first_bar": bars_today[0]["datetime"][-5:] if bars_today else None,
            "last_bar": bars_today[-1]["datetime"][-5:] if bars_today else None}


def measure_minute_cost(c: TdxHqClient, pool: list[tuple[int, str]]) -> dict:
    sample = pool[:200]
    t = time.time()
    rows = 0
    for market, code in sample:
        rows += len(c.get_security_bars(KLINE_1MIN, market, code, 0, 240) or [])
    dt = time.time() - t
    return {"symbols": len(sample), "rows": rows, "seconds": round(dt, 1),
            "ms_per_symbol": round(1000 * dt / len(sample)),
            "full_market_estimate_s": round(dt * 5500 / len(sample))}


def measure_bj(c: TdxHqClient) -> dict:
    codes = ["430047", "831010", "832000", "920002", "920008", "920099"]
    out = {}
    for code in codes:
        try:
            q = c.get_security_quotes([(MARKET_BJ, code)]) or []
            row = q[0] if q else {}
            out[code] = {"returned_code": row.get("code"), "price": row.get("price")}
        except Exception as e:  # noqa: BLE001
            out[code] = {"error": f"{type(e).__name__}: {e}"}
    return out


def measure_history_minute(c: TdxHqClient) -> dict:
    from datetime import date, timedelta

    today = date.today()
    out = {}
    for back in (30, 365, 1100):
        d = today - timedelta(days=back)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        rows = c.get_history_minute_time_data(MARKET_SH, "600519", int(d.strftime("%Y%m%d"))) or []
        out[str(d)] = {"points": len(rows), "sample": rows[0] if rows else None}
    return out


def main() -> None:
    c = _client()
    result: dict = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        result["latency"] = measure_latency(c)
        result["market"] = measure_market(c)
        result["minute_depth"] = measure_minute_depth(c)
        result["history_minute"] = measure_history_minute(c)
        result["bj"] = measure_bj(c)
        # 分钟成本单独一个连接, 避免前面长连接的状态影响
        c2 = _client()
        try:
            sh = _list_all(c2, MARKET_SH, 28)
            pool = [(MARKET_SH, str(x["code"])) for x in sh if str(x["code"]).startswith(SH_STOCK)]
            result["minute_cost"] = measure_minute_cost(c2, pool)
        finally:
            c2.disconnect()
    finally:
        c.disconnect()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
