"""能不能不用 Pro 权限，把除权因子从「同一天的两条序列」反推出来？

背景：`ex_factors` 需要 Pro（Free 档直接 PermissionError），而 `klines.batch` 的
`adjust="forward"` 是免费的。二者一比就是答案：

    前复权的定义（indicators/pipeline.py::_apply_adj_factor）
        adjusted(d) = raw(d) × C(d) / T
        C(d) = Π_{t<=d} e(t)，T = Π_all e(t)，e(t) = 该次除权事件的 pre/post 比值

    因此  ratio(d) := forward(d) / none(d) = C(d) / T
          e(d) = C(d) / C(d-1) = ratio(d) / ratio(d-1)

⇒ 只要拿到同一窗口的 forward 与 none 两条序列，**不需要知道 T**，事件级
   ex_factor 就能逐日还原（无事件的日子 ratio 不变，比值恰为 1）。

本脚本用「已知窗口」做样本外验证：本地 adj_factor 里有 2025-05-07~2026-11-13
的真实事件（4151 只 / 9265 条），把反推结果与它逐条比对。
通过 ⇒ 2024-08~2025-05 那段缺失的因子也可以同样反推，入库就不再依赖 Pro。

退出码: 0 = 全部样本逐条一致（相对误差 < --tol）; 1 = 有出入; 2 = 环境/数据不足。
用法: python analysis/_diag_adj_reconstruct.py --symbols 8
产物: analysis/_diag_adj_reconstruct_result.json
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

OUT = Path(__file__).with_name("_diag_adj_reconstruct_result.json")


def _fetch(symbols: list[str], adjust: str, start: date, end: date) -> pl.DataFrame:
    tf = get_client()
    raw = tf.klines.batch(
        symbols, period="1d", adjust=adjust,
        start_time=_datetime_to_ms(datetime.combine(start, datetime.min.time())),
        end_time=_datetime_to_ms(datetime.combine(end, datetime.max.time())),
        count=10000, as_dataframe=False, show_progress=False,
    )
    df = _compact_klines_to_df(raw)
    if df.is_empty():
        return df
    return df.with_columns(
        pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
        .dt.date().alias("date")
    ).select(["symbol", "date", "close"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=8, help="取样标的数（有 1~3 次事件的）")
    ap.add_argument("--start", default="2025-05-07")
    ap.add_argument("--end", default="2026-11-13")
    ap.add_argument("--tol", type=float, default=1e-6)
    a = ap.parse_args()

    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end)
    # 必须多取几天"前一日"：e(d) = ratio(d)/ratio(d-1)，窗口首日的事件在缺前一天时
    # 根本算不出来（第一版实测 18 条里有 3 条正好落在首日，全被漏掉）。
    fetch_start = start - timedelta(days=10)

    fac = pl.read_parquet(settings.data_dir / "adj_factor" / "all.parquet")
    fac = fac.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") <= end))
    if fac.is_empty():
        print("本地 adj_factor 在窗口内没有事件，无法做样本外验证", file=sys.stderr)
        return 2

    # 取样：优先「事件数为 1~3」的票（多事件才能检验链式还原）
    per = fac.group_by("symbol").agg(
        pl.len().alias("n_ev"), pl.col("trade_date").min().alias("first_ev"),
    ).filter(pl.col("n_ev") <= 3).sort(["n_ev", "first_ev"], descending=[True, False])
    picked = per["symbol"].head(a.symbols).to_list()
    print("samples:", picked, file=sys.stderr)

    fwd = _fetch(picked, "forward", fetch_start, end)
    non = _fetch(picked, "none", fetch_start, end)
    if fwd.is_empty() or non.is_empty():
        print("上游无数据", file=sys.stderr)
        return 2

    j = fwd.rename({"close": "close_fwd"}).join(
        non.rename({"close": "close_raw"}), on=["symbol", "date"], how="inner"
    ).sort(["symbol", "date"])
    j = j.with_columns((pl.col("close_fwd") / pl.col("close_raw")).alias("ratio"))
    j = j.with_columns(
        (pl.col("ratio") / pl.col("ratio").shift(1).over("symbol")).alias("step")
    )
    # 只在比值真的变了的地方记事件（1e-7 以内视为无事件）
    recon = j.filter(
        pl.col("step").is_not_null() & ((pl.col("step") - 1).abs() > 1e-7)
    ).select(["symbol", "date", "step"])

    truth = fac.select(["symbol", "trade_date", "ex_factor"]).filter(
        pl.col("symbol").is_in(picked)
    ).rename({"trade_date": "date"})

    joined = truth.join(recon, on=["symbol", "date"], how="full", coalesce=True).sort(
        ["symbol", "date"]
    )
    joined = joined.with_columns(
        ((pl.col("step") / pl.col("ex_factor")) - 1).abs().alias("rel_err")
    )

    only_truth = joined.filter(pl.col("step").is_null())
    only_recon = joined.filter(pl.col("ex_factor").is_null())
    bad = joined.filter(pl.col("rel_err").is_not_null() & (pl.col("rel_err") > a.tol))
    max_err = joined["rel_err"].max()

    result = {
        "window": [start.isoformat(), end.isoformat()],
        "samples": picked,
        "n_truth_events": truth.height,
        "n_reconstructed": recon.height,
        "matched": joined.filter(pl.col("rel_err").is_not_null()).height,
        "only_in_stored": only_truth.height,
        "only_in_reconstructed": only_recon.height,
        "beyond_tol": bad.height,
        "max_rel_err": None if max_err is None else float(max_err),
        "tol": a.tol,
        "verdict": "exact" if (only_truth.height == 0 and only_recon.height == 0
                               and bad.height == 0) else "mismatch",
        "samples_detail": joined.head(20).to_dicts(),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "samples_detail"},
                     ensure_ascii=False, indent=2, default=str))
    if result["verdict"] != "exact":
        print("mismatch rows:", joined.filter(
            pl.col("rel_err").is_null() | (pl.col("rel_err") > a.tol)
        ).head(10).to_dicts(), file=sys.stderr)
    return 0 if result["verdict"] == "exact" else 1


if __name__ == "__main__":
    raise SystemExit(main())
