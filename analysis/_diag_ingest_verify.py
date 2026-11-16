"""入库之后，新补进来的那 9 个月到底对不对？—— 用另一个供应商的前复权价验收。

`extend_history` 写的是 fuyao 的原始日K + fuyao 的除权因子，再由本地 pipeline 做
前复权。要证明它没错，不能只看"没报错"，要拿一条**独立来源**的已复权序列比：

    tickflow `adjust="forward"`（另一家供应商的前复权价）

两者在 2025-05-07 起的重叠区已知同基（600519 相对差 1.9e-6）。所以只要新补的
2024-08~2025-05 这一段也对得上（同量级），就说明：原始价、因子、复权算法、
分区落盘四步都是对的；对不上就是其中一步错了，且错在图上看得见。

用法: python analysis/_diag_ingest_verify.py --symbols 5
产物: analysis/_diag_ingest_verify_result.json
退出码: 0 = 全部样本 max_rel < --tol; 1 = 有漂移; 2 = 数据不足
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import polars as pl  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.kline_sync import (  # noqa: E402
    _compact_klines_to_df, _datetime_to_ms, get_client,
)
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

OUT = Path(__file__).with_name("_diag_ingest_verify_result.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-09-02")
    ap.add_argument("--end", default="2025-05-06", help="默认取刚补进来的那一段")
    ap.add_argument("--symbols", type=int, default=5)
    ap.add_argument("--tol", type=float, default=1e-3)
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)

    fac = pl.read_parquet(settings.data_dir / "adj_factor" / "all.parquet")
    in_win = fac.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") <= end))
    if in_win.is_empty():
        print("窗口内没有除权事件：无法验收", file=sys.stderr)
        return 2
    # 挑"窗口内有事件"的票：没有事件的票复权前后一样，比了也说明不了什么
    picked = (in_win.group_by("symbol").agg(pl.len().alias("n"))
              .sort("n", descending=True)["symbol"].head(a.symbols).to_list())

    repo = KlineRepository(DataStore(settings.data_dir))
    local = repo.get_daily_batch(picked, start, end, columns=["symbol", "date", "close"])
    if local.is_empty():
        print("本地 enriched 为空", file=sys.stderr)
        return 2

    tf = get_client()
    raw = tf.klines.batch(
        picked, period="1d", adjust="forward",
        start_time=_datetime_to_ms(datetime.combine(start, datetime.min.time())),
        end_time=_datetime_to_ms(datetime.combine(end, datetime.max.time())),
        count=10000, as_dataframe=False, show_progress=False,
    )
    ref = _compact_klines_to_df(raw)
    ref = ref.with_columns(
        pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
        .dt.date().alias("date")
    ).select(["symbol", "date", pl.col("close").alias("close_ref")])

    j = local.join(ref, on=["symbol", "date"], how="inner").with_columns(
        ((pl.col("close") / pl.col("close_ref")) - 1).abs().alias("rel")
    )
    per = j.group_by("symbol").agg(
        pl.col("rel").max().alias("max_rel"),
        pl.col("rel").mean().alias("mean_rel"),
        pl.len().alias("n"),
    ).sort("symbol")
    worst = j.sort("rel", descending=True).head(3).select(
        ["symbol", "date", "close", "close_ref", "rel"]
    )
    max_rel = float(per["max_rel"].max()) if per.height else None
    result = {
        "window": [start.isoformat(), end.isoformat()],
        "symbols": picked,
        "events_in_window": in_win.height,
        "compared_rows": j.height,
        "per_symbol": per.to_dicts(),
        "worst_rows": worst.to_dicts(),
        "max_rel": max_rel,
        "tol": a.tol,
        "verdict": "ok" if max_rel is not None and max_rel < a.tol else "drift",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["verdict"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
