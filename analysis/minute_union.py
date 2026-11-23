"""5min 全历史面板的唯一读取入口：baostock 回补段 ∪ 本地采集段。

为什么必须有这个文件
--------------------
`data/kline_5min/`（本地采集，2024-09-02 起，前复权）与
`data/backfill_bs_minute/f5_merged/`（baostock 回补，2020-01-02 起，已投影为前复权）
是**两套物理布局**：前者按 `date=` 分区（一天一文件、全市场），后者按 symbol 一文件。
所以它们不可能「一起 glob」——任何试图把两个目录当同构数据源扫描的代码，
要么静默丢掉一半数据，要么把重叠日读两遍。本仓已发生过「分区多一个文件 ⇒
同一天被读两遍且不报错」的事故（`data/backfill_20261117/MANIFEST.md`）。

切分点是硬边界，不是 dedupe
--------------------------
    bs   : 2020-01-02 ~ 2024-11-06   （baostock 逐只）
    local: 2024-11-07 ~ 2026-11-16   （本地逐日采集）

选 2024-11-07 而不是 2024-09-02：本地 5min 前 42 天是采集爬坡（2024-09-02 仅 9 只，
11-07 才 4,753 只），把爬坡段交给 baostock 既更全也更一致。两段**没有重叠**，
所以下面是 concat 而不是 join/去重——去掉 dedupe 是因为去重会掩盖边界画错。

口径一致性（已实测，见 MANIFEST.md）
-----------------------------------
**两段在磁盘上都是原始价**（这一点此前被写反过，代价是边界处 2.19% 的静默跳空）：
- 本地 `kline_5min`：按「拉取当日的前复权」落盘，而当日 ratio 恒为 1 ⇒ 等于当天原始价。
  实测 601127.SH 2024-11-07 末根 close = 135.87 = 日线 `raw_close`。
- 回补段：baostock `adjustflag=3` 原始价，与本地日线 `raw_close` 同源。
- 因此 `load()` 默认 `adjust=True`，对**拼接后的整段**统一做一次
  `apply_minute_adjustment`（投影到截至今日的前复权），结果与日线 `close` 精确一致；
  涨停/成交额等需要用原始价的场景传 `adjust=False`。
- volume：都是**手**（回补段已 ÷100）。amount：都是**元** = 手 × 价 × 100。

用法
----
    from analysis.minute_union import load
    df = load(["601127.SH"], "2024-10-01", "2024-11-15")            # 前复权（默认）
    df = load(["601127.SH"], "2024-11-06", "2024-11-07", adjust=False)  # 原始价
    df = load(None, "2024-11-07", "2024-11-15")                     # 全市场当日
    python analysis/minute_union.py --check                         # 边界自检
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(r"D:/DSH/tick-stock-panel")
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
BS_DIR = DATA / "backfill_bs_minute" / "f5_merged"
BS_INDEX = DATA / "backfill_bs_minute" / "_bs_index.parquet"
LOCAL_DIR = DATA / "kline_5min"

#: 默认切分点：本地段在此之前是采集爬坡（2024-09-02 仅 9 只，11-07 才 4,753 只），
#: 该段整体让给 baostock。**真正的拼接点按票取**（见 `_bs_last()`）：
#: 52 只 ST/停牌股的 baostock 数据一直延伸到 2026 年，越过这个日期。
SPLIT = "2024-11-07"

COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

_BS_LAST_CACHE: dict[str, str] | None = None


def _bs_last() -> dict[str, str]:
    """每只票 baostock 段的最后一根日期。由 merge_bs_minute.py 落索引，避免扫全量。

    键统一成**当前码**：索引里的键是落地码（300114），拼接后与本地段 join 前
    已经 canonicalize 成当前码（302132），两边不一致会让 `_bs_last` 恒为 null
    ⇒ 本地段全收 ⇒ 边界两侧重叠。多个历史码命中同一实体时取最晚的一根。
    """
    global _BS_LAST_CACHE
    if _BS_LAST_CACHE is None:
        if BS_INDEX.exists():
            from analysis.code_alias import resolve
            t = pl.read_parquet(BS_INDEX, columns=["symbol", "last"])
            cache: dict[str, str] = {}
            for s, d in zip(t["symbol"].to_list(), t["last"].to_list()):
                k, v = resolve(s), str(d)[:10]
                cache[k] = max(cache.get(k, ""), v)
            _BS_LAST_CACHE = cache
        else:
            _BS_LAST_CACHE = {}
    return _BS_LAST_CACHE


def _expand_codes(symbols: list[str] | None) -> list[str] | None:
    """请求码 → 该实体曾用过的全部代码（含自身）。

    分钟按**当时代码**落盘（300114），研究侧按**当前码**取数（302132）。
    只按字面文件名/分区值找文件，换码股的新码请求会返回空或只剩后半段，
    且不报错 —— 是本文件头注那条「同一实体两个代码」轴在读取侧的落点。
    """
    if symbols is None:
        return None
    from analysis.code_alias import historical_codes
    out: list[str] = []
    for s in symbols:
        out.append(s)
        out.extend(historical_codes(s))
    return list(dict.fromkeys(out))


def load(symbols: list[str] | None, start: str, end: str,
         adjust: bool = True) -> pl.DataFrame:
    """读取 [start, end] 的 5min 面板（含两端）。symbols=None 表示全市场。

    拼接规则（每只票单独判定，不用全局硬边界）：
      - 日期 < SPLIT：只用 baostock 段（本地该段是爬坡期，覆盖不全）；
      - 日期 ≥ SPLIT：本地段，但只要该票 baostock 段已经覆盖到更晚，
        本地段一律丢弃（52 只 ST/停牌股的 bs 数据延伸到 2026 年）。
    这样任何 (symbol, datetime) 只可能来自一段，不需要 dedupe。

    `adjust=True`（默认）对拼接后的整段统一投影到前复权，与日线 `close` 同口径；
    `adjust=False` 返回原始价（涨停价、成交额、盘口约束只能用这个）。
    """
    frames = []
    codes = _expand_codes(symbols)
    # bs 段按请求区间整段读：52 只 ST/停牌股的 bs 数据一直延伸到 2026 年，
    # 不能只读到 SPLIT 就截断（否则这些票 2024-11 之后凭空消失）。
    bs = _load_bs(codes, start, end)
    if not bs.is_empty():
        frames.append(bs.with_columns(pl.lit("bs").alias("_src")))
    if end >= SPLIT:
        loc = _load_local(codes, max(start, SPLIT), end)
        if not loc.is_empty():
            frames.append(loc.with_columns(pl.lit("local").alias("_src")))

    if not frames:
        return pl.DataFrame()
    out = pl.concat(frames).sort(["symbol", "datetime"])
    # 代码变更 canonicalize：分钟按当时代码（如 300114 中航电测）、日线按当前码
    # （302132 中航成飞），不归一化则按 symbol 的日线 join / 复权因子 lookup 会静默漏票。
    # 顺序必须在分段过滤之前：过滤靠 `_bs_last` 的 join，两边都是当前码才命中。
    from analysis.code_alias import alias_map
    am = alias_map()
    if am:
        out = out.with_columns(
            pl.col("symbol").replace(list(am.keys()), list(am.values())).alias("symbol")
        )
    last = _bs_last()
    if last and out.height:
        # 每票的 bs 末根之后才算本地段；无 bs 记录的票全收
        cut = pl.DataFrame(
            {"symbol": list(last.keys()), "_bs_last": list(last.values())},
            schema={"symbol": pl.String, "_bs_last": pl.String},
        ).with_columns(pl.col("_bs_last").str.to_date())
        out = (out.with_columns(pl.col("datetime").dt.date().alias("_d"))
               .join(cut, on="symbol", how="left")
               .filter((pl.col("_src") == "bs") |
                       pl.col("_bs_last").is_null() |
                       (pl.col("_d") > pl.col("_bs_last")))
               .drop(["_d", "_bs_last"]))
    out = out.drop("_src")
    if not adjust:
        return out
    from backend.app.services.minute_adjust import apply_minute_adjustment
    return apply_minute_adjustment(out, DATA, asset_type="stock")


def _load_bs(symbols: list[str] | None, start: str, end: str) -> pl.DataFrame:
    if symbols is None:
        files = sorted(BS_DIR.glob("*.parquet"))
    else:
        files = [BS_DIR / f"{s}.parquet" for s in symbols]
        files = [f for f in files if f.exists()]
    if not files:
        return pl.DataFrame()
    return pl.concat([
        pl.read_parquet(f)
          .filter(pl.col("datetime").dt.date().is_between(
              pl.lit(start).str.to_date(), pl.lit(end).str.to_date()))
        for f in files
    ])


def _load_local(symbols: list[str] | None, start: str, end: str) -> pl.DataFrame:
    if not LOCAL_DIR.exists():
        return pl.DataFrame()
    days = sorted(p[5:] for p in os.listdir(LOCAL_DIR)
                  if p.startswith("date=") and start <= p[5:] <= end)
    if not days:
        return pl.DataFrame()
    out = []
    for d in days:
        t = pl.read_parquet(LOCAL_DIR / f"date={d}" / "part.parquet", columns=COLS)
        if symbols is not None:
            t = t.filter(pl.col("symbol").is_in(symbols))
        out.append(t)
    return pl.concat(out) if out else pl.DataFrame()


def _day_before(iso: str) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) - timedelta(days=1)).isoformat()


def check() -> None:
    """边界自检：硬边界两侧同基准，且拼接后与日线 close / raw_close 各自对齐。

    只查「文件在不在」是不够的——上一版就是两段基准不同（一段投影一段原始），
    拼接后在每只有分红的票上产生 2% 级跳空且零报错。所以这里做**数值**对账：
    取一只同时有回补段与本地段的票，抽边界前后各 3 个交易日，
    分别用 adjust=False / True 与日线 raw_close / close 比对。
    """
    files = sorted(BS_DIR.glob("*.parquet"))
    if not files:
        print("回补段为空（先跑 backfill_bs_minute.py）")
        return
    days = sorted(p[5:] for p in os.listdir(LOCAL_DIR) if p.startswith("date="))
    print(f"回补段 {len(files)} 只 / 本地段 {days[0]} ~ {days[-1]} / 边界 {SPLIT}")

    # 1) 结构：越过边界的标的属于「bs 一直覆盖到 2026」的 ST/停牌股，
    #    load() 按票取真实末根，不靠这条全局边界，所以这里只做告知。
    over = []
    for f in files:
        mx = pl.read_parquet(f, columns=["datetime"])["datetime"].max()
        if str(mx)[:10] >= SPLIT:
            over.append((f.stem, str(mx)[:10]))
    print(f"越过全局边界 {SPLIT} 的标的：{len(over)} 只"
          f"（ST/停牌，按票取末根，非错误）{over[:3]}")

    # 2) 数值：取一只跨边界的票做双基准对账
    cand = [f.stem for f in files
            if not pl.read_parquet(f, columns=["datetime"])["datetime"].is_empty()]
    sym = next((s for s in cand if s in ("601127.SH", "000001.SZ")), cand[0])
    probe = "2024-11-04"
    raw = load([sym], probe, "2024-11-08", adjust=False)
    adj = load([sym], probe, "2024-11-08", adjust=True)
    if raw.is_empty():
        print(f"{sym} 抽取为空，跳过数值对账")
        return
    dl = pl.concat([
        pl.read_parquet(LOCAL_DIR.parent / "kline_daily_enriched" / f"date={d}" / "part.parquet",
                        columns=["symbol", "date", "close", "raw_close"])
          .filter(pl.col("symbol") == sym)
        for d in sorted({str(x)[:10] for x in raw["datetime"].dt.date().to_list()})
    ])
    k = (raw.group_by(pl.col("datetime").dt.date().alias("date"))
         .agg(pl.col("close").last().alias("r")))
    k = k.join(adj.group_by(pl.col("datetime").dt.date().alias("date"))
               .agg(pl.col("close").last().alias("a")), on="date").join(dl, on="date")
    k = k.with_columns([(pl.col("r") - pl.col("raw_close")).abs().alias("d_raw"),
                        (pl.col("a") - pl.col("close")).abs().alias("d_adj")])
    print(f"\n{sym} 双基准对账（{probe} ~ 2024-11-08）")
    print(k.select(["date", "r", "raw_close", "d_raw", "a", "close", "d_adj"]))
    print(f"  max|原始 - raw_close| = {k['d_raw'].max():.6f}  (应 ~0)")
    print(f"  max|前复权 - close|   = {k['d_adj'].max():.6f}  (应 ~0)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()


if __name__ == "__main__":
    main()
