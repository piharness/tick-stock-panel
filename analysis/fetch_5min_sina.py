"""拉取 ETF 与股票的 5 分钟线，用于「tick 制度」负控实验。

为什么必须自己去拉
------------------
本地 `data/kline_5min` 只有股票、且只到 2026-11-16；`data/kline_etf_minute` 是空目录；
`data/kline_minute` 实测只含 00/30 段股票（2,336 只），**无 ETF**。
baostock 实测**支持 ETF 日线、不支持 ETF 分钟**（`sh.510050` 5min 返回 0 行，
同一天日线返回 4 行）⇒ 「用现成 baostock 补数链路」这条路对 ETF 是死的。

东财 push2his 首探可用（`klt=5` 返 1,488 根 ≈ 31 交易日），但**约 10 次请求后整个
`*.push2his.eastmoney.com` 段位被 WAF 掐断**（`RemoteProtocolError: Server disconnected`，
重试 4 次全败；同一时刻 `push2.eastmoney.com` 与 `qt.gtimg.cn` 正常）⇒ 不可作主力。
腾讯 `mkline` 返回非 JSON。**新浪 `CN_MarketDataService.getKLineData` 是唯一稳定通道**，
`datalen=1023` 是硬顶（传 4000 返 `null`），即 **1,023 根 ≈ 21 个交易日 + 1 个残日**。

21 天够不够
-----------
够。要估的是 ρ₁（相邻 bar 收益的相关系数），池化后每只标的 21×47 ≈ 987 个相邻对，
~1,600 只 ETF ⇒ 155 万个对。日级 bootstrap 的 n_eff = 21 天（不是 155 万），
但本实验比的是 **7 倍量级的差距**，21 天的窗宽不会把 7 倍读成 1 倍。

为什么股票也要重拉一遍
----------------------
如果 ETF 用新浪、股票用本地，两条腿的窗口（21 vs 27 天）、数据源、聚合规则都不同，
「ETF 与股票同 tick 时 ρ₁ 差 7 倍」这个结论就会被"窗口不同"解释掉。同源同窗是
这个负控的全部价值。代价只有 ~1,700 次请求。

落盘位置
--------
`data/etf_5min_sina/` 与 `data/stock_5min_sina/`，**不写进 `data/kline_5min`**——
分区目录里多一个文件会让同一天被读两遍且不报错（本仓已知事故）。

口径
----
- 新浪 kline 默认不复权 ⇒ 与本地 `kline_5min`（已核验存原始价，`abs(mclose-raw_close)`=4e-18）同基准。
- 字段 day/open/high/low/close/volume/amount，**是标准 OHLC 顺序**（与东财的 O-C-H-L 不同）。
- 单日 48 根、收盘时刻 09:35…15:00，与本地 `kline_5min` 完全一致（已核验）。
- 分析侧只保留 48 根的标的-日：缺根的多为停牌/半日/新上市，混进来会把 ρ₁ 拉向 0。
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import polars as pl

ROOT = Path(os.environ.get("TSP_ROOT") or r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"

URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://finance.sina.com.cn/"}
DATALEN = 1023


def sina_code(symbol: str) -> str:
    code, _, ex = symbol.partition(".")
    return ("sh" if ex == "SH" else "sz") + code


def fetch_one(client: httpx.Client, symbol: str, tries: int = 4):
    p = {"symbol": sina_code(symbol), "scale": "5", "ma": "no", "datalen": str(DATALEN)}
    last = ""
    for k in range(tries):
        try:
            r = client.get(URL, params=p, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                last = f"http{r.status_code}"
                time.sleep(1.0 + k * 1.5)
                continue
            t = r.text.strip()
            if not t.startswith("["):
                # 新浪对「无数据/代码不存在」返回字面量 null —— 合法空，不重试
                if t in ("null", "", "[]"):
                    return symbol, []
                last = f"weird:{t[:30]}"
                time.sleep(1.0 + k * 1.5)
                continue
            rows = json.loads(t)
            out = []
            for o in rows:
                try:
                    out.append({
                        "symbol": symbol,
                        "datetime": o["day"],
                        "open": float(o["open"]),
                        "high": float(o["high"]),
                        "low": float(o["low"]),
                        "close": float(o["close"]),
                        "volume": float(o.get("volume") or 0),
                        "amount": float(o.get("amount") or 0),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            return symbol, out
        except Exception as e:                                    # noqa: BLE001
            last = f"{type(e).__name__}:{e}"
            time.sleep(1.0 + k * 1.5)
    return symbol, f"FAIL {last}"


def run(universe: list[str], out_dir: Path, tag: str, workers: int = 6) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_p = out_dir / "_done.json"
    done: set[str] = set(json.loads(ckpt_p.read_text("utf-8"))) if ckpt_p.exists() else set()
    todo = [s for s in universe if s not in done]
    print(f"[{tag}] 标的 {len(universe)}  已完成 {len(done)}  待抓 {len(todo)}", flush=True)

    frames: list[dict] = []
    bad: list[tuple[str, str]] = []
    empty = 0
    t0 = time.time()
    with httpx.Client(limits=httpx.Limits(max_connections=workers + 6)) as client:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_one, client, s): s for s in todo}
            for i, fu in enumerate(as_completed(futs), 1):
                sym, res = fu.result()
                if isinstance(res, str):
                    bad.append((sym, res))
                elif not res:
                    empty += 1
                    done.add(sym)
                else:
                    frames.extend(res)
                    done.add(sym)
                if i % 100 == 0 or i == len(todo):
                    el = time.time() - t0
                    print(f"  {i}/{len(todo)}  行 {len(frames):,}  空 {empty}  失败 {len(bad)}  "
                          f"{el:.0f}s ({i / max(el, 1e-9):.2f}/s)", flush=True)
                    ckpt_p.write_text(json.dumps(sorted(done)), "utf-8")

    ckpt_p.write_text(json.dumps(sorted(done)), "utf-8")
    if bad:
        (out_dir / "_failed.json").write_text(json.dumps(done and bad, ensure_ascii=False, indent=1), "utf-8")
        print(f"[{tag}] 失败 {len(bad)} 只，见 _failed.json", flush=True)

    if frames:
        df = (pl.DataFrame(frames)
              .with_columns(pl.col("datetime").str.strptime(pl.Datetime, "%Y-%m-%d %H:%M:%S"))
              .with_columns(pl.col("datetime").dt.date().alias("date"))
              .sort(["symbol", "datetime"]))
        p = out_dir / f"{tag}_5min.parquet"
        df.write_parquet(p)
        print(f"[{tag}] 写入 {p}  行 {df.height:,}  "
              f"标的 {df['symbol'].n_unique()}  日期 {df['date'].n_unique()}", flush=True)
        print(f"[{tag}] 日期范围 {df['date'].min()} ~ {df['date'].max()}", flush=True)


def etf_universe() -> list[str]:
    """窗口内 >=15 天有成交、中位成交额 > 0 的 ETF。"""
    import datetime as dt
    d = pl.read_parquet(DATA / "kline_etf_daily/*/*.parquet",
                        columns=["symbol", "date", "amount"])
    w = d.filter((pl.col("date") >= dt.date(2026, 10, 22)) & (pl.col("date") <= dt.date(2026, 11, 20)))
    g = w.group_by("symbol").agg(pl.len().alias("nd"),
                                 pl.col("amount").median().alias("mamt"))
    live = g.filter(pl.col("nd") >= 15, pl.col("mamt") > 0)
    return sorted(live["symbol"].unique().to_list())


def stock_universe() -> list[str]:
    """价格分层：¥<6 全取（与 ETF 的 tick 区间重叠最密），¥>=6 每档 40 只。"""
    import numpy as np
    d = pl.read_parquet(DATA / "kline_daily_enriched/date=2026-11-16/*.parquet",
                        columns=["symbol", "raw_close", "amount"])
    d = d.filter(pl.col("raw_close") > 0, pl.col("amount") > 0,
                 ~pl.col("symbol").str.ends_with(".BJ"))
    edges = np.array([0, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 25, 30, 40,
                      50, 70, 100, 150, 300, 10000.0])
    px = d["raw_close"].to_numpy()
    d = d.with_columns(pl.Series("bi", np.clip(np.digitize(px, edges[1:-1]), 0, len(edges) - 2)))
    pick: list[str] = []
    for bi, g in sorted(d.group_by("bi"), key=lambda kv: kv[0]):
        syms = sorted(g["symbol"].to_list())
        cap = 10000 if edges[bi] < 6 else 40
        pick.extend(syms[:cap])
    return sorted(set(pick))


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 6

    if which in ("all", "etf"):
        u = etf_universe()
        print(f"[etf] 可交易 ETF {len(u)} 只", flush=True)
        run(u, DATA / "etf_5min_sina", "etf", workers)
    if which in ("all", "stock"):
        u = stock_universe()
        print(f"[stock] 分层抽样 {len(u)} 只", flush=True)
        run(u, DATA / "stock_5min_sina", "stock", workers)
    if which in ("all", "cb"):
        # 可转债：T+0、单只券（非一篮子）、最小报价单位 0.001 元。
        # 用来分离「T+0 消除冲击项」与「一篮子消除冲击项」两个解释。
        cb = json.loads((ROOT / "analysis/_cb_list.json").read_text("utf-8"))
        u = sorted({f"{x['f12']}.{'SH' if x['f13'] == 1 else 'SZ'}" for x in cb})
        print(f"[cb] 可转债 {len(u)} 只", flush=True)
        run(u, DATA / "cb_5min_sina", "cb", workers)


if __name__ == "__main__":
    main()
