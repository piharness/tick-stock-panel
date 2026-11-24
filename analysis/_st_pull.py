"""拉取权威 ST 历史（baostock isST 日频），供 PIT 样本池使用。

为什么不能自己猜：`docs` 与记忆已证伪「5% 涨跌幅指纹」重建 ST 区间
（金标准 precision 0.448 / recall 0.423），所以只能取交易所口径的 isST。

产物：`data/_st_hist/st_{chunk}.parquet`，只存 isST=1 的日期（稀疏），
schema = symbol(str), date(date)。缺该 symbol 文件 = 未取到（不等于「非 ST」），
调用方必须显式区分「无记录」与「有记录且非 ST」——本脚本用 manifest 记录
成功取数的 symbol 列表，`--merge` 输出 `data/_st_hist/st_days.parquet` 与
`data/_st_hist/st_covered.parquet`。

用法：
    python analysis/_st_pull.py --workers 8            # 断点续跑
    python analysis/_st_pull.py --merge
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "data" / "_probe_t0"))
sys.path.insert(0, str(ROOT / "data" / "_probe_t0" / "_extlib"))

OUT = ROOT / "data" / "_st_hist"
START = "2014-06-01"
END = "2026-11-24"
CHUNK = 120


def to_bs(sym: str) -> str | None:
    code, _, ex = sym.partition(".")
    if ex == "SH" and code[0] == "6":
        return f"sh.{code}"
    if ex == "SZ" and code[0] in ("0", "3"):
        return f"sz.{code}"
    return None  # 北交所 / 其他：baostock 无覆盖


def worker(args: tuple[int, list[str]]) -> tuple[int, int, str]:
    idx, syms = args
    dst = OUT / f"st_{idx:04d}.parquet"
    if dst.exists():
        return idx, len(syms), "skip"
    from _fixed_bs import FixedBaostock  # noqa: E402

    recs: list[tuple[str, str]] = []
    covered: list[str] = []
    bs = FixedBaostock()
    try:
        if not bs.login():
            return idx, 0, "login_fail"
        for sym in syms:
            code = to_bs(sym)
            if code is None:
                continue
            for attempt in range(2):
                try:
                    r = bs.query_k(code, "date,isST", START, END, frequency="d")
                    break
                except Exception:
                    r = {"rows": [], "error_code": "EXC"}
                    time.sleep(1.5)
                    try:
                        bs.logout()
                    except Exception:
                        pass
                    bs = FixedBaostock()
                    bs.login()
            rows = r.get("rows") or []
            if r.get("error_code") == "0":
                # 有响应即视为已覆盖（哪怕该票全区间非 ST，rows 也非空）
                if rows:
                    covered.append(sym)
                for row in rows:
                    if len(row) >= 2 and row[1] == "1":
                        recs.append((sym, row[0]))
    finally:
        try:
            bs.logout()
        except Exception:
            pass

    df = pl.DataFrame(recs, schema={"symbol": pl.String, "date": pl.String}, orient="row")
    df = df.with_columns(pl.col("date").str.to_date()).unique()
    df.write_parquet(dst)
    pl.DataFrame({"symbol": covered}, schema={"symbol": pl.String}).write_parquet(
        OUT / f"cov_{idx:04d}.parquet"
    )
    return idx, len(covered), "ok"


def merge() -> None:
    days = pl.read_parquet(str(OUT / "st_*.parquet"))
    cov = pl.read_parquet(str(OUT / "cov_*.parquet"))
    days.write_parquet(OUT / "st_days.parquet")
    cov.write_parquet(OUT / "st_covered.parquet")
    print(f"st_days={days.height:,} symbols_with_st={days['symbol'].n_unique():,} "
          f"covered={cov.height:,}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--merge", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.merge:
        merge()
        return

    syms = (
        pl.scan_parquet(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet"))
        .select("symbol").unique().collect()["symbol"].to_list()
    )
    gap = pl.read_parquet(str(ROOT / "data/backfill_20261117/stock_gap_daily.parquet"))
    syms = sorted(set(syms) | set(gap["symbol"].unique().to_list()))
    tasks = [(i, syms[i * CHUNK:(i + 1) * CHUNK]) for i in range((len(syms) + CHUNK - 1) // CHUNK)]
    print(f"symbols={len(syms):,} chunks={len(tasks)} workers={a.workers}", flush=True)

    import multiprocessing as mp

    t0 = time.time()
    done = 0
    with mp.Pool(a.workers) as pool:
        for idx, n, status in pool.imap_unordered(worker, tasks):
            done += 1
            if done % 5 == 0 or status != "ok":
                el = time.time() - t0
                print(f"[{done}/{len(tasks)}] chunk={idx} n={n} {status} "
                      f"elapsed={el/60:.1f}min eta={el/done*(len(tasks)-done)/60:.1f}min",
                      flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
