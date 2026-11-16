"""P0 修复验证：三件事一起做，名单是否逐字不变（**不写 data/**）。

上一轮已验证「去重 → candidates(2026-11-11) 恢复 20 只」。
本轮加两个变量，因为发现了上一轮没看的第二层：

1. `kline_daily/date=2026-11-05/` 除 `part.parquet` 还有 `part-stream-0.parquet`；
   二者 symbol 集合是**包含**关系（stream-0 ⊂ part，4069/4069），
   OHLC 在 4069 只上 maxabs=**0**，volume 仅 6 只差（4 只差 1 股、2 只北交所差 1000/2000）
   ⇒ stream-0 是盘后修正前的旧快照，删除不丢数据、丢的是旧值。
2. `part.parquet` 自己带着 **2 个指数行**（000002.SH 上证A指 / 000688.SH 科创50），
   只出现在最近 4 个分区。指数本该走 `kline_index_daily`（`load_benchmark_momentum` 只读那里）。

要做的是回答：**去掉这两类脏数据，名单会不会变？**
变 ⇒ 说明冻结的 11-12 cohort 本来就吃了脏数据，那是更大的事；
不变 ⇒ 修复是「保住答案」，不是「让报错消失」。

用法：
    backend/.venv/Scripts/python.exe analysis/_diag_panel_fix_verify.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort  # noqa: E402

ENRICHED = ROOT / "data" / "kline_daily_enriched"
INDEX_SYMBOLS = {
    "000001.SH", "000002.SH", "000300.SH", "000688.SH", "000905.SH", "000016.SH",
    "399001.SZ", "399005.SZ", "399006.SZ", "399102.SZ", "399107.SZ", "899050.BJ",
}
AS_OF = "2026-11-11"


def _prepare(tag: str, *, drop_index: bool) -> tuple[Path, dict]:
    """在系统临时目录造一份 kline_daily_enriched，返回 (data_dir, 统计)。"""
    df = pl.read_parquet(str(ENRICHED / "**" / "*.parquet"))
    raw_rows, raw_sym = df.height, df["symbol"].n_unique()

    dup_keys = df.select(pl.struct("date", "symbol")).is_duplicated().sum()

    # 去重：保留 quote_ts 非空那行（= part.parquet 版本，amount 是整元）。
    # 不用 keep="last"/"first"，因为行的物理顺序不承诺语义。
    if "quote_ts" in df.columns:
        df = df.sort("quote_ts", nulls_last=True)
    df = df.unique(subset=["date", "symbol"], keep="first", maintain_order=True)
    after_dedup = df.height

    n_index = df.filter(pl.col("symbol").is_in(list(INDEX_SYMBOLS))).height
    if drop_index:
        df = df.filter(~pl.col("symbol").is_in(list(INDEX_SYMBOLS)))

    out = Path(tempfile.mkdtemp(prefix=f"clean_{tag}_")) / "kline_daily_enriched"
    out.mkdir(parents=True)
    df.write_parquet(out / "part.parquet")

    return out.parent, {
        "tag": tag,
        "drop_index": drop_index,
        "rows_raw": raw_rows,
        "rows_after_dedup": after_dedup,
        "dup_keys_before": int(dup_keys),
        "index_rows_dropped": int(n_index) if drop_index else 0,
        "rows_final": df.height,
        "uniq_symbols_raw": raw_sym,
        "uniq_symbols_final": df["symbol"].n_unique(),
    }


def _run(data_dir: Path) -> dict:
    try:
        r = l1_cohort.candidates(data_dir, AS_OF)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    syms = [c["symbol"] for c in r.get("candidates", r.get("rows", []))]
    return {"ok": True, "n": len(syms), "symbols": syms}


def main() -> int:
    report: dict = {"as_of": AS_OF, "how": "临时目录副本；data/ 零改动"}

    # 基线：原样
    report["A_asis"] = _run(ROOT / "data")

    dirs = []
    try:
        for tag, drop in [("dedup_only", False), ("dedup_noindex", True)]:
            d, stats = _prepare(tag, drop_index=drop)
            dirs.append(d)
            stats.update(_run(d))
            report[tag] = stats
    finally:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)

    # 逐字比对
    def syms(k):
        v = report.get(k, {})
        return v.get("symbols") if v.get("ok") else None

    base = syms("dedup_only")
    report["identity"] = {
        "dedup_only_vs_dedup_noindex": base == syms("dedup_noindex"),
        "n_dedup_only": None if base is None else len(base),
        "n_dedup_noindex": None if syms("dedup_noindex") is None else len(syms("dedup_noindex")),
    }

    out = ROOT / "analysis" / "_diag_panel_fix_verify_result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
