"""一个重复键如何让整张面板的名单算不出来 —— 定位、取证、并在副本上验证修复。

## 这条判据为什么必须存在

`app.decision.l1_cohort._panel()` 拒绝重复 `(date, symbol)`，理由是「矩阵法会静默
覆盖一格，样本无声减少」。这个守卫是对的，但它是**全panel**判据：任意一天出问题，
**所有** as_of 都拒绝。于是 2026-11-12 实测：

    candidates('2026-11-10') / ('2026-11-11') / ('2026-11-12') 一律 ValueError

⇒ `analysis/auction_order.resolve_pool` 处处 `exit 4` ⇒ 09:20 的下单表整条停摆，
而病因句只写「candidates 失败 ValueError(重复行)」，不写**是哪一天、哪个目录**。

## 成因链（2026-11-12 实测，逐环可复算）

1. `data/kline_daily/date=2026-11-05/` 里有两个文件：`part.parquet`（5572 行，带
   `quote_ts`）与 `part-stream-0.parquet`（4069 行，无 `quote_ts` 列）。
   后者是 `kline_sync` 的 staging 落名（`replace_with_retry` 之后的清理没覆盖到）。
2. **目录级读取看到同一天两遍**：9641 行 / 唯一 symbol 5572。读取器不报，因为
   `scan_daily_parquet` 的契约是「容忍增量 schema」，不是「容忍重复行」。
3. 该日期的 enriched 分区被重写时把两份并了进去 ⇒ `kline_daily_enriched/date=2026-11-05`
   出现 3 个重复 `(date, symbol)`：`301559.SZ` / `301608.SZ` / `688777.SH`，
   两份只差 `amount`（整元 vs 浮点）与 `quote_ts`（有/无）。
4. `_panel()` 的全局判据 ⇒ 全日期拒绝。

## 这一份做什么

**不改 `data/`**。只做三件事：定位（哪些日期/目录/键）、归因（每个重复键的两行差在哪）、
在**临时副本**上验证修复（删掉多余键后 `candidates` 是否恢复，且恢复出来的名单是否
与冻结的 11-12 cohort 逐字一致）。副本用完即弃，路径打印在产物里。

判据方向：修复必须**保住答案**，不是「让报错消失」。所以最后一条断言是名字集合相等，
不是「跑通了」。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "_diag_panel_dup_key_result.json"
WORK = ROOT / ".workbuddy" / "tmp" / "dupkey"
FROZEN = Path(__file__).resolve().parent / "auction_pool_cohort_2026-11-12.json"

#: 日线表名。`bars` 只导出 enriched 的名字，日线那份在这里写死一次并注明来路。
STOCK_DAILY = "kline_daily"

sys.path.insert(0, str(ROOT / "backend"))
from app.decision import bars  # noqa: E402
from app.decision.l1_cohort import candidates  # noqa: E402
from app.parquet import scan_daily_parquet, scan_enriched_parquet  # noqa: E402


def _stray_files(data: Path) -> list[dict]:
    """每个日分区目录里「文件数 > 1」的那些 —— 这就是「同一天被读两遍」的来路。

    只列目录名与文件名/行数，不读全表：`data/` 是 9p 挂载，按文件数线性收费。
    """
    base = data / STOCK_DAILY
    if not base.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or not d.name.startswith("date="):
            continue
        files = sorted(d.glob("*.parquet"))
        if len(files) > 1:
            out.append({"partition": d.name,
                        "files": [{"name": f.name,
                                   "bytes": f.stat().st_size} for f in files]})
    return out


def _dup_keys(data: Path) -> dict:
    """enriched 全panel 的重复 `(date, symbol)`，并给出每个键两行的差异列。"""
    df = scan_enriched_parquet(Path(data) / bars.STOCK_ENRICHED).collect()
    df = df.with_columns(pl.col("date").cast(pl.Utf8))
    dup = df.filter(pl.struct("date", "symbol").is_duplicated())
    keys: list[dict] = []
    for r in dup.select(["date", "symbol"]).unique().iter_rows(named=True):
        sub = df.filter((pl.col("date") == r["date"]) & (pl.col("symbol") == r["symbol"]))
        rows = sub.to_dicts()
        diff = {k: [rows[0][k], rows[1][k]] for k in rows[0] if rows[0][k] != rows[1][k]}
        keys.append({"date": r["date"], "symbol": r["symbol"], "rows": sub.height,
                     "diff": {k: [str(a), str(b)] for k, (a, b) in diff.items()}})
    return {"n_rows": df.height, "n_duplicate_keys": len(keys), "keys": keys,
            "dates": sorted({k["date"] for k in keys})}


def _repair(part: Path) -> dict:
    """副本上的修复：同一 `(date, symbol)` 保留 `quote_ts` 非空的那一行。

    为什么留「有 quote_ts」的那行而不是「最后一行」：`quote_ts` 是这一格被 enriched
    真读过行情的证据；没有它的那行来自 staging 快照，本来就不该进 enriched。
    `keep="last"` 是写入端的口径，但它把「哪一行是后写的」当成了「哪一行是对的」——
    在这条链上后者才是唯一有意义的问题。
    """
    before = pl.read_parquet(part)
    fixed = (before.with_columns(pl.col("quote_ts").is_not_null().alias("_stamped"))
             .sort(["_stamped"], descending=False)     # 无戳在前、有戳在后
             .unique(subset=["symbol", "date"], keep="last")
             .drop("_stamped").sort(["symbol", "date"]))
    fixed.write_parquet(part)
    return {"rows_before": before.height, "rows_after": fixed.height}


def main() -> int:
    data = ROOT / "data"
    out: dict = {"how": "定位 → 归因 → 在临时副本上验证修复；不改 data/"}

    out["stray"] = _stray_files(data)
    print(f"[1] 多文件的分区 {len(out['stray'])} 个："
          f"{[s['partition'] for s in out['stray']]}")

    out["dup"] = _dup_keys(data)
    print(f"[2] enriched 重复 (date,symbol) 键 {out['dup']['n_duplicate_keys']} 个，"
          f"落在 {out['dup']['dates']}")
    for k in out["dup"]["keys"][:3]:
        print(f"    {k['date']} {k['symbol']} ×{k['rows']}  差异列 "
              f"{list(k['diff'])}")

    # 目录级读取：同一天被读两遍的直接证据（用仓内读取器，不用裸 polars）
    day = out["dup"]["dates"][0] if out["dup"]["dates"] else None
    if day:
        d = data / STOCK_DAILY / f"date={day}"
        df = scan_daily_parquet(d).collect()
        out["folder_read"] = {"partition": f"date={day}", "rows": df.height,
                              "uniq_symbols": df["symbol"].n_unique(),
                              "files": [f.name for f in sorted(d.glob("*.parquet"))]}
        print(f"[3] 目录级读取 data/kline_daily/date={day}: {df.height} 行 / "
              f"{df['symbol'].n_unique()} 只 ⇒ 重复 {df.height - df['symbol'].n_unique()} 行")

    # 拒绝面：三个日期都拒，说明这是全panel判据不是某一天
    refuse = {}
    for d in ("2026-10-30", "2026-11-11", "2026-11-12"):
        try:
            doc = candidates(data, d)
            refuse[d] = {"ok": True, "n": len(doc.get("candidates") or [])}
        except Exception as e:  # noqa: BLE001
            refuse[d] = {"ok": False, "err": f"{type(e).__name__}: {e}"}
    out["refusal"] = refuse
    print(f"[4] candidates 拒绝面：{ {k: v['ok'] for k, v in refuse.items()} }")

    # 副本 + 修复 + 重跑
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "data").mkdir(parents=True)
    shutil.copytree(data / bars.STOCK_ENRICHED, WORK / "data" / bars.STOCK_ENRICHED)
    part = WORK / "data" / bars.STOCK_ENRICHED / f"date={day}" / "part.parquet"
    out["repair"] = {"partition": str(part.relative_to(ROOT)), **_repair(part)}

    doc = candidates(WORK / "data", "2026-11-11")
    got = sorted(r["symbol"] for r in doc.get("candidates") or [])
    frozen = sorted(json.loads(FROZEN.read_text(encoding="utf-8"))["symbols"])
    out["repaired"] = {"n": len(got), "entry_date": doc.get("entry_date"),
                       "same_as_frozen": got == frozen,
                       "only_in_repaired": sorted(set(got) - set(frozen)),
                       "only_in_frozen": sorted(set(frozen) - set(got))}
    print(f"[5] 副本修复后 candidates(2026-11-11) = {len(got)} 只，"
          f"与冻结 cohort（{len(frozen)} 只）一致={got == frozen}")
    print(f"    副本留在 {WORK.relative_to(ROOT)}（用完可删）")

    out["verdict"] = ("repair_preserves_the_answer"
                      if got == frozen and out["dup"]["n_duplicate_keys"] else "fail")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物 {OUT.relative_to(ROOT)}")
    return 0 if out["verdict"] == "repair_preserves_the_answer" else 1


if __name__ == "__main__":
    sys.exit(main())
