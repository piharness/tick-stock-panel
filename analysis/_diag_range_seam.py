"""长区间回看的两条路是否给出同一条价格序列 —— 接缝检验。

问题：个股分析页要支持任意起止日（例：平安银行 2024-09-01 → 2026-11-13），
而本地 enriched 日线只覆盖 2025-05-07 起。可选的两条路：

  A. 把上游历史写进 kline_daily / kline_daily_enriched（会造出「1 只票的
     2024-09 分区」，全市场横截面分析静默退化）；
  B. 只读穿透：上游原始 bar + 本地同一套 compute_enriched，在内存里算出
     同一形状的 enriched 行，不落盘、不改变全市场表的覆盖。

B 能不能用，只取决于一件事：在「上游窗口」与「本地 enriched」重叠的那段
日期上，两条路算出来的 OHLC 是否逐根相等。不等就是接缝，接缝就是图上
一根假 K 线。本脚本就是这条判据，退出码 0=接缝不存在。

用法：
  python analysis/_diag_range_seam.py --symbol 600340.SH --start 2024-08-20
产物：analysis/_diag_range_seam_result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import polars as pl  # noqa: E402

from app.indicators.pipeline import compute_enriched  # noqa: E402
from app.services.kline_sync import sync_daily_batch  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402
from app.config import settings  # noqa: E402

OUT = Path(__file__).with_name("_diag_range_seam_result.json")
COLS = ["open", "high", "low", "close", "volume"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="600340.SH")
    ap.add_argument("--start", default="2024-08-20")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--tol", type=float, default=1e-6)
    ap.add_argument("--vol-tol", type=float, default=1.0, help="volume 取整容差(手)")
    a = ap.parse_args()

    end = date.fromisoformat(a.end)

    # 1) 本地 enriched（唯一真相）
    repo = KlineRepository(DataStore(settings.data_dir))
    local = repo.get_daily(a.symbol, date(2025, 1, 1), end)
    if local.is_empty():
        print("本地 enriched 无数据", file=sys.stderr)
        return 2
    local_first, local_last = local["date"].min(), local["date"].max()

    # 2) 上游原始窗口 → 同一套 compute_enriched（含预热，保证 MA60 有值）
    #    免费档一次要两年会超时（实测 539 根 / 52s 后 ReadTimeout），按 ≤180 天
    #    分段取再拼——症状是"上游没数据"，实际是"一次要太多"。
    raw_parts: list[pl.DataFrame] = []
    cur = date.fromisoformat(a.start)
    while cur <= end:
        nxt = min(cur + timedelta(days=180), end)
        part = sync_daily_batch(
            [a.symbol],
            start_time=datetime.combine(cur, datetime.min.time()),
            end_time=datetime.combine(nxt, datetime.max.time()),
        )
        if not part.is_empty():
            raw_parts.append(part)
            print(f"  fetched {cur}~{nxt}: {part.height} rows", file=sys.stderr)
        else:
            print(f"  fetched {cur}~{nxt}: 0 rows", file=sys.stderr)
        cur = nxt + timedelta(days=1)
    raw = pl.concat(raw_parts, how="diagonal_relaxed").unique(subset=["date"], keep="last") if raw_parts else pl.DataFrame()
    if raw.is_empty():
        print("上游无数据", file=sys.stderr)
        return 2
    raw = raw.sort("date")
    factors = pl.DataFrame()
    fp = repo.store.data_dir / "adj_factor" / "all.parquet"
    if fp.exists():
        f = pl.scan_parquet(fp).filter(pl.col("symbol") == a.symbol).collect()
        if not f.is_empty():
            factors = f
    remote = compute_enriched(raw, factors=factors)

    # 3) 重叠区逐根比对
    keep = [c for c in ["date", *COLS] if c in local.columns and c in remote.columns]
    lo = local.select(keep).filter(pl.col("date") >= local_first)
    ro = remote.select(keep).filter(pl.col("date") >= local_first)
    joined = lo.join(ro, on="date", how="inner", suffix="_remote")
    diffs: list[dict] = []
    # 量能容差单独一支: 上游 volume 与本地落盘的 volume 差 1 手是「同一根 bar 被
    # 转两次」的取整差（本仓踩过，见 .workbuddy 记忆 37），一天 1.4M 手差 1 手
    # 在图上不可见。把它算成接缝会让这条判据永远红 —— 永远红的判据等于没有判据。
    VOL_TOL = a.vol_tol
    for c in COLS:
        if c not in local.columns:
            continue
        tol = VOL_TOL if c == "volume" else a.tol
        d = (joined[c] - joined[f"{c}_remote"]).abs()
        mx = d.max()
        if mx is not None and mx > tol:
            bad = joined.filter(d > tol)
            diffs.append({
                "col": c, "max_abs": float(mx), "n_mismatch": bad.height, "tol": tol,
                "sample": bad.select(["date", c, f"{c}_remote"]).head(3).to_dicts(),
            })
    # 只记录、不判失败: 取整差本身是有用的证据（说明两条路的差异全在尾部舍入）
    vol_rounding = None
    if "volume" in local.columns:
        d = (joined["volume"] - joined["volume_remote"]).abs()
        mx = d.max()
        if mx is not None and 0 < mx <= VOL_TOL:
            vol_rounding = {"max_abs": float(mx), "n_rows": joined.filter(d > 0).height,
                            "n_total": joined.height, "tol": VOL_TOL}

    result = {
        "symbol": a.symbol,
        "local_first": str(local_first),
        "local_last": str(local_last),
        "local_rows": local.height,
        "remote_first": str(raw["date"].min()),
        "remote_last": str(raw["date"].max()),
        "remote_rows": raw.height,
        "overlap_rows": joined.height,
        "overlap_first": str(joined["date"].min()) if joined.height else None,
        "overlap_last": str(joined["date"].max()) if joined.height else None,
        "adj_factor_rows_for_symbol": factors.height,
        "adjusted": not factors.is_empty(),
        "tol": a.tol,
        "volume_rounding": vol_rounding,
        "mismatches": diffs,
        "verdict": "seam-free" if not diffs else "seam",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "mismatches"},
                     ensure_ascii=False, indent=2, default=str))
    for d in diffs:
        print("MISMATCH", d["col"], "max", d["max_abs"], "n", d["n_mismatch"], d["sample"])
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
