"""补 cohort 自己的分钟曲线：把「开盘能吃下多少」从迁移变成实测。

为什么需要它
------------
`analysis/_diag_l1_open_depth.py` 的第一版只能拿 `data/kline_minute` 里那 38–40 只
（全是 000xxx.SZ）当参照类，而 cohort 的 2,752 只-日与它**交集为 0**；按 gap 迁移
后 bar0 份额的留一日误差和它自己的中位数同量级，判据落在误差里。要判就只能补
cohort 自己那 2,752 个 (date, symbol) 的**整条 240 根分钟曲线**。

两种产物，不是两种口径
----------------------
- `--mode bar0`：每格只留首根，写 `part.parquet`（9 列）。回答「首根占比多少」。
- `--mode curves`：每格留全部 240 根（含 amount），写 `curves.parquet`。回答
  「c(k) 的形状」「k≈52 的局部斜率」「cohort 自己的 dc/dk」。

上一版只有前一种，`bar0_from` 把 240 根里其余 239 根直接丢掉。于是
`_diag_l1_kstar_slope_provenance` 缺的三个量（cohort 的 dc/dk、k≈52 局部斜率、
c(k) 形状）**一个都补不上**：bar0 缓存打满时 `--dry-run` 报 0 次请求，看起来全绿，
实际什么都不会发生。曲线模式必须单独存在，否则「抓 240 根」只是把 0 次请求跑一遍。

数据源
------
`backend/app/plugins/tdxquant` 的 `get_minute(symbols, start, end)`：单次请求可带
多只、按日期窗口返回 240 根/日，含 `amount`。TickFlow 免费 key 对
`kline.minute.*` 无权限（见 `data/capabilities.json` 的 probe_log），所以走 tdx。

口径
----
- 只取**事件日当天**的分钟（分母的日成交额用 `kline_daily_enriched.amount`，
  两者都是原始人民币金额，不受复权影响）。
- 每个请求最多 `BATCH` 只，按事件日分组；已缓存且**完整**的 (date,symbol) 跳过，
  可断点续跑。不完整的格（少于 `CURVE_MIN_BARS` 根）视为没抓过，下轮重抓。
- 缓存各写**一个** parquet（`./data` 是 9p 挂载，元数据按文件数线性收费，不能按日
  分 316 个文件）。
- 失败不清缓存：缺的 cell 留给分析脚本按覆盖报告，不静默补零；空返回也记为失败，
  否则整天消失而 `failed` 为空。

用法
----
    backend/.venv/Scripts/python.exe scripts/fetch_l1_open_depth.py --mode curves --dry-run
    backend/.venv/Scripts/python.exe scripts/fetch_l1_open_depth.py --mode curves --limit 3

不写结论；结论在 `analysis/_diag_l1_*.py`。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "research" / "l1_open_depth" / "part.parquet"
CURVES = ROOT / "data" / "research" / "l1_open_depth" / "curves.parquet"
BATCH = 20
SLEEP = 1.0
BAR0_COLS = ["date", "symbol", "b0_open", "b0_high", "b0_low", "b0_close",
             "b0_amt", "b0_vol", "nbars"]
CURVE_COLS = ["date", "symbol", "k", "open", "high", "low", "close",
              "amount", "volume", "nbars"]
CURVE_FULL_BARS = 240    # 全日 240 根
CURVE_MIN_BARS = 200     # 低于此视为不完整：下轮重抓，不当已完成


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


def _load(path: Path) -> pl.DataFrame:
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def load_cache() -> pl.DataFrame:
    return _load(CACHE)


def load_curves() -> pl.DataFrame:
    return _load(CURVES)


def merge(cache: pl.DataFrame, frames: list[pl.DataFrame],
          subset: list[str]) -> pl.DataFrame:
    parts = ([cache] if cache.height else []) + [f for f in frames if f.height]
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="vertical_relaxed").unique(subset=subset, keep="last")


def _write(df: pl.DataFrame, path: Path, sort: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    df.sort(sort).write_parquet(tmp)
    tmp.replace(path)


def write_cache(df: pl.DataFrame) -> None:
    _write(df, CACHE, ["date", "symbol"])


def write_curves(df: pl.DataFrame) -> None:
    _write(df, CURVES, ["date", "symbol", "k"])


def bar0_from(df: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """取每只票**自己的** 09:31 bar，并把没有 09:31 的票交回去。

    上一版用全局 `min(tm)` 当首根：一批 20 只里只要有一只 09:31 无成交（停牌、
    一字板无撮合），它的"首根"就会被写成 09:32 的开盘价，而 `nbars` 仍是 240，
    没有任何告警——`curve[k=0].amount == b0_amt` 的比对会差一整根的量。
    现在按 symbol 各自的 `tm0` 取，且 `tm0` 必须是 09:31；不合格的票进 `dropped`。
    """
    if df.is_empty():
        return pl.DataFrame(), []
    m = df.with_columns(pl.col("datetime").dt.time().alias("tm"))
    per = m.group_by("symbol").agg(
        pl.col("tm").min().alias("tm0"), pl.len().alias("nbars")
    )
    good = per.filter(pl.col("tm0") == pl.lit(dtime(9, 31)))
    dropped = sorted(per.filter(pl.col("tm0") != pl.lit(dtime(9, 31)))["symbol"].to_list())
    if not good.height:
        return pl.DataFrame(), dropped
    b0 = m.join(good.select("symbol", "tm0"), on="symbol").filter(
        pl.col("tm") == pl.col("tm0")
    ).select(
        pl.col("symbol"),
        (pl.col("datetime").dt.date().cast(pl.Utf8)).alias("date"),
        pl.col("open").alias("b0_open"),
        pl.col("high").alias("b0_high"),
        pl.col("low").alias("b0_low"),
        pl.col("close").alias("b0_close"),
        pl.col("amount").alias("b0_amt"),
        pl.col("volume").alias("b0_vol"),
    )
    return b0.join(good.select("symbol", "nbars"), on="symbol", how="left"), dropped


def curves_from(df: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """保留每只票的**全部**分钟 bar，并按 symbol 各自编 k（0 = 09:31）。

    k 必须是**单只票内**的序号，不是整批的行号：一批 20 只的帧里全局行号会让
    `curve[k]` 指向别人的 bar，而下游 `Δ(k)` 只在单票内求和。首根同 `bar0_from`
    判据（tm0 == 09:31），不合格进 `dropped`，不给「用 09:32 冒充首根」留路。
    """
    if df.is_empty():
        return pl.DataFrame(), []
    m = df.with_columns(pl.col("datetime").dt.time().alias("tm"))
    per = m.group_by("symbol").agg(
        pl.col("tm").min().alias("tm0"), pl.len().alias("nbars")
    )
    good = per.filter(pl.col("tm0") == pl.lit(dtime(9, 31)))
    dropped = sorted(per.filter(pl.col("tm0") != pl.lit(dtime(9, 31)))["symbol"].to_list())
    if not good.height:
        return pl.DataFrame(), dropped
    out = (
        m.join(good.select("symbol", "nbars"), on="symbol")   # inner：不合格的票必须被剔除
        .sort(["symbol", "datetime"])
        .with_columns(
            pl.int_range(0, pl.len()).over("symbol").alias("k"),
            pl.col("datetime").dt.date().cast(pl.Utf8).alias("date"),
        )
        .select(CURVE_COLS)
    )
    return out, dropped


def requests_needed(per_day_counts: list[int], batch: int = BATCH) -> int:
    """请求数按 batch 累加：一天 40 只需 2 次，不是 1 次（天数只是下界）。"""
    return int(sum(-(-n // batch) for n in per_day_counts))


def complete_cells(curves: pl.DataFrame) -> set[tuple[str, str]]:
    """只有根数达标的格才算「已抓」；半截曲线会让 c(k) 在 k 尾凭空截断。"""
    if curves.is_empty():
        return set()
    per = curves.group_by(["date", "symbol"]).agg(pl.len().alias("rows"))
    ok = per.filter(pl.col("rows") >= CURVE_MIN_BARS)
    return set(zip(ok["date"].to_list(), ok["symbol"].to_list()))


def curve_integrity(curves: pl.DataFrame) -> dict:
    """k 必须 0..n−1 连续、且 n == nbars；否则 `Δ(k)` 会静默错位。"""
    if curves.is_empty():
        return {"cells": 0, "broken": 0, "full": 0, "min_bars": 0}
    per = curves.group_by(["date", "symbol"]).agg(
        pl.len().alias("rows"),
        pl.col("k").max().alias("kmax"),
        pl.col("k").min().alias("kmin"),
        pl.col("nbars").max().alias("nbars"),
    )
    broken = per.filter(
        (pl.col("rows") != pl.col("kmax") + 1)
        | (pl.col("kmin") != 0)
        | (pl.col("rows") != pl.col("nbars"))
    )
    return {
        "cells": per.height,
        "broken": broken.height,
        "full": per.filter(pl.col("rows") >= CURVE_FULL_BARS).height,
        "min_bars": int(per["rows"].min()) if per.height else 0,
    }


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--mode", choices=("bar0", "curves"), default="bar0")
    ap_.add_argument("--limit", type=int, default=0, help="最多拉多少个事件日")
    ap_.add_argument("--dry-run", action="store_true")
    args = ap_.parse_args()

    curves_mode = args.mode == "curves"
    cells = cohort_cells()
    if curves_mode:
        cache = load_curves()
        have = complete_cells(cache)
    else:
        cache = load_cache()
        have = set(zip(cache["date"].to_list(), cache["symbol"].to_list())) if cache.height else set()
    keep = [((d, s) not in have) for d, s in zip(cells["date"].to_list(), cells["symbol"].to_list())]
    need = cells.filter(pl.Series(keep))
    dates = sorted(need["date"].unique().to_list())
    if args.limit:
        dates = dates[: args.limit]
    pending = need.filter(pl.col("date").is_in(dates))
    print(f"[{args.mode}] cohort {cells.height} cells / {cells['date'].n_unique()} days | "
          f"done {len(have)} | todo {pending.height} cells / {len(dates)} days")

    if args.dry_run:
        # 请求数按 batch 累加，不是天数：一天 40 只需 2 次请求，day 数只是下界
        per_day = pending.group_by("date").agg(pl.len().alias("n"))
        ns = per_day["n"].to_list() or [0]
        n_batch = requests_needed(ns)
        # 两个数不可互换：`requests_needed` 是**批**数，实际 HTTP 请求是**每只 symbol
        # 一次** `provider.iter_minute → client.his_dats`（实测 ~10.6s/只），所以真实
        # 请求数 = cells。上一版只打 batch 数，316 天读成「316 请求 / 约 50 分钟」，
        # 而实测是 92 秒/天、8 小时量级 —— 差的是这一个乘数，不是网络。
        print(f"would issue ~{pending.height} HTTP requests "
              f"= {n_batch} batches of ≤{BATCH} "
              f"(provider 每只一次 his_dats，不是每批一次); "
              f"{pending.height} cells over {len(dates)} days, "
              f"per-day median {per_day['n'].median() if per_day.height else 0} "
              f"max {max(ns)}")
        return

    if not pending.height:
        print(f"nothing to fetch in {args.mode} mode")
        return

    extract = curves_from if curves_mode else bar0_from
    prov = provider()
    frames: list[pl.DataFrame] = []
    t0 = time.time()
    failed: list[str] = []
    got = 0

    def pull(chunk: list[str], d: datetime) -> pl.DataFrame:
        """单次请求；额度/频控（AuthError）退避重试，短返回退到 5 只一批再试。"""
        for attempt in range(3):
            try:
                seg, drop = extract(prov.get_minute(chunk, d, d + timedelta(days=1)))
                if drop:
                    failed.append(f"{d.date()} no-0931 {len(drop)}syms: {drop[:3]}")
                if not seg.height:
                    # 整批空返回：上一版这里静默通过，整天消失而 failed 为空
                    failed.append(f"{d.date()} {len(chunk)}syms: empty response")
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
                seg, drop = extract(
                    prov.get_minute(chunk[k: k + 5], d, d + timedelta(days=1)))
                if drop:
                    failed.append(f"{d.date()} no-0931 {len(drop)}syms: {drop[:3]}")
                if seg.height:
                    out.append(seg)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{d.date()} retry: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(SLEEP)
        return pl.concat(out) if out else pl.DataFrame()

    subset = ["date", "symbol", "k"] if curves_mode else ["date", "symbol"]
    flush = write_curves if curves_mode else write_cache

    for i, day in enumerate(dates, 1):
        syms = sorted(pending.filter(pl.col("date") == day)["symbol"].to_list())
        d = datetime.fromisoformat(day)
        for k in range(0, len(syms), BATCH):
            seg = pull(syms[k: k + BATCH], d)
            if seg.height:
                frames.append(seg)
                got += seg.select("date", "symbol").unique().height
            time.sleep(SLEEP)
        if i % 10 == 0 or i == len(dates):
            el = time.time() - t0
            print(f"  [{i}/{len(dates)}] cells {got} elapsed {el:.0f}s "
                  f"eta {el / i * (len(dates) - i):.0f}s fails {len(failed)}", flush=True)
        if i % 25 == 0 and frames:
            cache = merge(cache, frames, subset)
            flush(cache)
            frames = []

    if frames:
        cache = merge(cache, frames, subset)
        flush(cache)
    elif not cache.height:
        print("no cells fetched")
        return

    if curves_mode:
        integ = curve_integrity(cache)
        print(f"done: curves {cache.height} rows, {integ['cells']} cells "
              f"({len(complete_cells(cache))} complete), full-240 {integ['full']}, "
              f"broken-k {integ['broken']}, min rows {integ['min_bars']}, "
              f"fails {len(failed)}")
    else:
        cov = cache.height / cells.height if cells.height else 0.0
        print(f"done: cache {cache.height} cells ({cov * 100:.1f}% of cohort), "
              f"{cache['date'].n_unique()} days, fails {len(failed)}")
    for f in failed[:5]:
        print(f"  FAIL {f}")


if __name__ == "__main__":
    main()
