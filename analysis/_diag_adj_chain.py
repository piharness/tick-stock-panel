"""端到端：用「反推的因子」把缺失窗口补上，算出来的前复权价与上游 forward 差多少？

判据只看一个数：整段价格序列的最大相对误差。

  - 反推到的是**因子**（中间量），因子对不代表价格对（累积乘积会把误差放大）；
  - 上游 `adjust="forward"` 已经证明与本地前复权同基（1.9e-6），所以它可以当
    "独立的第三方答案"来验收整条链路；
  - 若最大相对误差 ≲1e-4，则入库之后图上不可能看出差别（1e-4 在 500 根 K 上
    还不到 1 分钱）。

链路（与入库后 pipeline 完全一致）：
    adjusted(d) = raw(d) × Π_{t≤d} e(t) / Π_all e(t)
其中 e(t) = 已知窗口用本地 adj_factor，缺失窗口用 ratio(d)/ratio(d-1) 反推。

用法: python analysis/_diag_adj_chain.py --symbols 301004.SZ,601939.SH
产物: analysis/_diag_adj_chain_result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import polars as pl  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.kline_sync import (  # noqa: E402
    _compact_klines_to_df, _datetime_to_ms, get_client,
)

OUT = Path(__file__).with_name("_diag_adj_chain_result.json")


def _fetch(symbols: list[str], adjust: str, start: date, end: date) -> pl.DataFrame:
    """带重试的上游取数 —— 免费档会偶发 `Server disconnected`（实测 20:2x 一次）。"""
    import time

    tf = get_client()
    last: Exception | None = None
    for attempt in range(3):
        try:
            raw = tf.klines.batch(
                symbols, period="1d", adjust=adjust,
                start_time=_datetime_to_ms(datetime.combine(start, datetime.min.time())),
                end_time=_datetime_to_ms(datetime.combine(end, datetime.max.time())),
                count=10000, as_dataframe=False, show_progress=False,
            )
            break
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  retry {attempt + 1}/3 ({type(e).__name__})", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last  # type: ignore[misc]
    df = _compact_klines_to_df(raw)
    if df.is_empty():
        return df
    return df.with_columns(
        pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
        .dt.date().alias("date")
    ).select(["symbol", "date", "open", "high", "low", "close"])


def reconstruct(symbols: list[str], start: date, end: date) -> pl.DataFrame:
    """缺失窗口内的事件因子（e(d) = ratio(d)/ratio(d-1)）。"""
    fetch_start = start - timedelta(days=10)  # 首日事件需要前一天
    fwd = _fetch(symbols, "forward", fetch_start, end)
    non = _fetch(symbols, "none", fetch_start, end)
    j = fwd.rename({"close": "cf"}).join(
        non.rename({"close": "cr"}).select(["symbol", "date", "cr"]),
        on=["symbol", "date"], how="inner",
    ).sort(["symbol", "date"])
    j = j.with_columns((pl.col("cf") / pl.col("cr")).alias("ratio"))
    j = j.with_columns((pl.col("ratio") / pl.col("ratio").shift(1).over("symbol")).alias("e"))
    return j.filter(
        (pl.col("date") >= start) & (pl.col("date") <= end)
        & pl.col("e").is_not_null() & ((pl.col("e") - 1).abs() > 1e-7)
    ).select(["symbol", pl.col("date").alias("trade_date"), pl.col("e").alias("ex_factor")])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="301004.SZ,601939.SH,600235.SH")
    ap.add_argument("--gap-start", default="2024-08-01")
    ap.add_argument("--gap-end", default="2025-05-06")
    ap.add_argument("--span-start", default="2024-08-01")
    ap.add_argument("--span-end", default="2026-11-13")
    a = ap.parse_args()

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    gap_start, gap_end = date.fromisoformat(a.gap_start), date.fromisoformat(a.gap_end)
    span_start, span_end = date.fromisoformat(a.span_start), date.fromisoformat(a.span_end)

    recon = reconstruct(syms, gap_start, gap_end)
    stored = pl.read_parquet(settings.data_dir / "adj_factor" / "all.parquet").filter(
        pl.col("symbol").is_in(syms)
    ).select(["symbol", "trade_date", "ex_factor"])
    factors = pl.concat([stored, recon.select(["symbol", "trade_date", "ex_factor"])])

    raw = _fetch(syms, "none", span_start, span_end).sort(["symbol", "date"])
    truth = _fetch(syms, "forward", span_start, span_end).sort(["symbol", "date"])

    # 与 pipeline 同口径：cum_factor(<=d) / total_factor
    # 注意：`unique()` 不保证顺序（哈希实现），必须再 sort 一次才谈得上 cum_prod。
    # 少了这一步，cum 会按任意顺序累乘 —— 实测把 301004 的 1.44 事件排到最后，
    # 整段价格系统性偏 31%（正好是 1/1.4425）。pipeline 里 sort→unique→sort 同理。
    f = (factors.sort(["symbol", "trade_date"])
         .unique(subset=["symbol", "trade_date"], keep="last")
         .sort(["symbol", "trade_date"])
         .with_columns(pl.col("ex_factor").cum_prod().over("symbol").alias("cum")))
    total = f.group_by("symbol").agg(pl.col("cum").last().alias("total"))
    adj = raw.join_asof(
        f.select("symbol", "trade_date", "cum"), left_on="date", right_on="trade_date",
        by="symbol", strategy="backward",
    ).join(total, on="symbol", how="left")
    adj = adj.with_columns(
        (pl.col("cum").fill_null(1.0) / pl.col("total").fill_null(1.0)).alias("ratio")
    ).with_columns((pl.col("close") * pl.col("ratio")).alias("close_adj"))

    cmp = adj.select(["symbol", "date", "close", "close_adj"]).join(
        truth.select(["symbol", "date", pl.col("close").alias("close_fwd")]),
        on=["symbol", "date"], how="inner",
    )
    cmp = cmp.with_columns(
        ((pl.col("close_adj") / pl.col("close_fwd")) - 1).abs().alias("rel")
    )
    per = cmp.group_by("symbol").agg(
        pl.col("rel").max().alias("max_rel"),
        pl.col("rel").mean().alias("mean_rel"),
        pl.len().alias("n"),
    ).sort("symbol")
    worst = cmp.sort("rel", descending=True).head(3).select(
        ["symbol", "date", "close_adj", "close_fwd", "rel"]
    )

    result = {
        "symbols": syms,
        "gap": [gap_start.isoformat(), gap_end.isoformat()],
        "reconstructed_events": recon.height,
        "stored_events": stored.height,
        "per_symbol": per.to_dicts(),
        "worst_rows": worst.to_dicts(),
        "max_rel": float(per["max_rel"].max()) if per.height else None,
        "verdict": "lossless" if per.height and float(per["max_rel"].max()) < 1e-4 else "drift",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["verdict"] == "lossless" else 1


if __name__ == "__main__":
    raise SystemExit(main())
