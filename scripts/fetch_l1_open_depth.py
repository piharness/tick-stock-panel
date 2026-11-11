"""补 cohort 自己的首根分钟 bar：把「开盘能吃下多少」从迁移变成实测。

为什么需要它
------------
`analysis/_diag_l1_open_depth.py` 的第一版只能拿 `data/kline_minute` 里那 38–40 只
（全是 000xxx.SZ）当参照类，而 cohort 的 2,752 只-日与它**交集为 0**；按 gap 迁移
后 bar0 份额的留一日误差和它自己的中位数同量级，判据落在误差里。要判就只能补
cohort 自己那 2,752 个 (date, symbol) 的 09:31 bar。

数据源
------
`backend/app/plugins/tdxquant` 的 `get_minute(symbols, start, end)`：单次请求可带
多只、按日期窗口返回 240 根/日，含 `amount`。TickFlow 免费 key 对
`kline.minute.*` 无权限（见 `data/capabilities.json` 的 probe_log），所以走 tdx。

口径
----
- 只取**事件日当天**的分钟（分母的日成交额用 `kline_daily_enriched.amount`，
  两者都是原始人民币金额，不受复权影响）。
- 每个请求最多 `BATCH` 只，按事件日分组；已缓存的 (date,symbol) 跳过，可断点续跑。
- 缓存写**一个** parquet（`data/research/l1_open_depth/part.parquet`）：
  `./data` 是 9p 挂载，元数据按文件数线性收费，不能按日分 316 个文件。
- 失败不清缓存：缺的 cell 留给分析脚本按覆盖报告，不静默补零。

用法
----
    backend/.venv/Scripts/python.exe scripts/fetch_l1_open_depth.py [--limit N] [--dry-run]

不写产物、不算结论；结论在 `analysis/_diag_l1_open_depth.py`。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "research" / "l1_open_depth" / "part.parquet"
BATCH = 20
SLEEP = 1.0
COLS = ["date", "symbol", "b0_open", "b0_high", "b0_low", "b0_close", "b0_amt", "b0_vol", "nbars"]


def cohort_cells() -> pl.DataFrame:
    """复用 cohort 的唯一产地，不在这里另建一份成员表。"""
    spec = importlib.util.spec_from_file_location(
        "_curv", ROOT / "analysis" / "_diag_l1_vwap_curvature.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_cohort()


def provider():
    sys.path.insert(0, str(ROOT / "backend"))
    from app.plugins.tdxquant.provider import TdxQuantProvider

    return TdxQuantProvider()


def load_cache() -> pl.DataFrame:
    if not CACHE.exists():
        return pl.DataFrame()
    return pl.read_parquet(CACHE)


def merge(cache: pl.DataFrame, frames: list[pl.DataFrame]) -> pl.DataFrame:
    parts = ([cache] if cache.height else []) + [f for f in frames if f.height]
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="vertical_relaxed").unique(subset=["date", "symbol"], keep="last")


def write_cache(df: pl.DataFrame) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp.parquet")
    df.sort(["date", "symbol"]).write_parquet(tmp)
    tmp.replace(CACHE)


def bar0_from(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()
    m = df.with_columns(pl.col("datetime").dt.time().alias("tm"))
    t0 = m["tm"].min()
    if str(t0) != "09:31:00":
        return pl.DataFrame()
    b0 = m.filter(pl.col("tm") == t0).select(
        pl.col("symbol"),
        (pl.col("datetime").dt.date().cast(pl.Utf8)).alias("date"),
        pl.col("open").alias("b0_open"),
        pl.col("high").alias("b0_high"),
        pl.col("low").alias("b0_low"),
        pl.col("close").alias("b0_close"),
        pl.col("amount").alias("b0_amt"),
        pl.col("volume").alias("b0_vol"),
    )
    nb = m.group_by("symbol").agg(pl.len().alias("nbars"))
    return b0.join(nb, on="symbol", how="left")


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--limit", type=int, default=0, help="最多拉多少个事件日")
    ap_.add_argument("--dry-run", action="store_true")
    args = ap_.parse_args()

    cells = cohort_cells()
    cache = load_cache()
    have = set(zip(cache["date"].to_list(), cache["symbol"].to_list())) if cache.height else set()
    keep = [((d, s) not in have) for d, s in zip(cells["date"].to_list(), cells["symbol"].to_list())]
    need = cells.filter(pl.Series(keep))
    dates = sorted(need["date"].unique().to_list())
    if args.limit:
        dates = dates[: args.limit]
    print(f"cohort {cells.height} cells / {cells['date'].n_unique()} days | "
          f"cached {len(have)} | todo {need.height} cells / {len(dates)} days")

    if args.dry_run:
        print(f"would issue ~{len(dates)} requests (batch {BATCH})")
        return

    prov = provider()
    pending = need.filter(pl.col("date").is_in(dates))
    frames: list[pl.DataFrame] = []
    t0 = time.time()
    failed: list[str] = []
    got = 0

    def pull(chunk: list[str], d: datetime) -> pl.DataFrame:
        """单次请求；额度/频控（AuthError）退避重试，短返回退到 5 只一批再试。"""
        for attempt in range(3):
            try:
                seg = bar0_from(prov.get_minute(chunk, d, d + timedelta(days=1)))
                return seg if seg.height else pl.DataFrame()
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if "Auth" in name and attempt < 2:
                    print(f"  throttle {d.date()}, sleep 60s", flush=True)
                    time.sleep(60)
                    continue
                failed.append(f"{d.date()} {len(chunk)}syms: {name}: {str(e)[:120]}")
                if len(failed) <= 3:
                    print(f"  FAIL {failed[-1]}", flush=True)
                break
        if len(chunk) <= 5:
            return pl.DataFrame()
        out: list[pl.DataFrame] = []
        for k in range(0, len(chunk), 5):
            try:
                seg = bar0_from(prov.get_minute(chunk[k : k + 5], d, d + timedelta(days=1)))
                if seg.height:
                    out.append(seg)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{d.date()} retry: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(SLEEP)
        return pl.concat(out) if out else pl.DataFrame()

    for i, day in enumerate(dates, 1):
        syms = sorted(pending.filter(pl.col("date") == day)["symbol"].to_list())
        d = datetime.fromisoformat(day)
        for k in range(0, len(syms), BATCH):
            seg = pull(syms[k : k + BATCH], d)
            if seg.height:
                frames.append(seg)
                got += seg.height
            time.sleep(SLEEP)
        if i % 10 == 0 or i == len(dates):
            el = time.time() - t0
            print(f"  [{i}/{len(dates)}] cells {got} elapsed {el:.0f}s "
                  f"eta {el / i * (len(dates) - i):.0f}s fails {len(failed)}", flush=True)
        if i % 25 == 0 and frames:
            cache = merge(cache, frames)
            write_cache(cache)
            frames = []

    if frames:
        cache = merge(cache, frames)
        write_cache(cache)
    elif not cache.height:
        print("no cells fetched")
        return

    cov = cache.height / cells.height if cells.height else 0.0
    print(f"done: cache {cache.height} cells ({cov*100:.1f}% of cohort), "
          f"{cache['date'].n_unique()} days, fails {len(failed)}")
    for f in failed[:5]:
        print(f"  FAIL {f}")


if __name__ == "__main__":
    main()
