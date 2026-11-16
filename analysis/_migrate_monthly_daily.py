"""把 2026-11 的日分区合并进月分区 date=2026-11, 消除混合布局。

背景: 提交 8e03f63 (2026-11-08) 起 kline_daily 写月分区, 但迁移脚本未执行。
容器 2026-11-12 21:05 重启加载新代码后, 第一次写入 (21:10) 直接落到
date=2026-11, 于是同一天 (11-12) 同时存在月分区与日分区 ⇒ glob 读两份;
且 locate_partition_dir 优先返回月分区 ⇒ 11-02..11-11 在按日定位的读路径上
静默读不到 (月分区里只有 11-12)。

本脚本只做数据搬迁, 不改代码:
  1. 备份全部 date=2026-11* 目录到 integrity_backups/<tag>/
  2. 逐日取权威来源 (日分区优先; 11-05 取 part.parquet 而非 part-stream-0)
  3. 合并去重 -> 月分区 part.parquet 原子替换
  4. 把旧日分区移入备份目录 (不物理删除)

指数行 (000002.SH / 000688.SH 等 asset_type=index) 不进个股日线:
维表可判定, 无维表时按「symbol 在 instruments_index 中」剔除。

用法:
  python analysis/_migrate_monthly_daily.py --dry-run
  python analysis/_migrate_monthly_daily.py --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TABLE = "kline_daily"
BASE = DATA / TABLE
MONTH = "2026-11"
REQUIRED = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"]


def _load_dir(p: Path) -> pd.DataFrame:
    files = sorted(p.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def _index_symbols() -> set[str]:
    f = DATA / "instruments_index" / "instruments_index.parquet"
    if not f.exists():
        return set()
    return set(pd.read_parquet(f, columns=["symbol"])["symbol"])


def build() -> tuple[pd.DataFrame, list[dict]]:
    idx_syms = _index_symbols()
    month_dir = BASE / f"date={MONTH}"
    daily_dirs = sorted(
        p for p in BASE.glob("date=2026-11-*") if p.is_dir()
    )
    log: list[dict] = []
    frames: list[pd.DataFrame] = []

    # 月分区现有内容 (11-12 的最新收尾快照) 打头, 作为同键优先来源
    if month_dir.exists():
        m = _load_dir(month_dir)
        log.append({"src": month_dir.name, "rows": len(m),
                    "dates": sorted(str(x) for x in m["date"].unique())})
        frames.append(m)

    for p in daily_dirs:
        df = _load_dir(p)
        if df.empty:
            log.append({"src": p.name, "rows": 0})
            continue
        frames.append(df)
        log.append({"src": p.name, "rows": len(df),
                    "dates": sorted(str(x) for x in df["date"].unique())})

    all_df = pd.concat(frames, ignore_index=True, sort=False)
    for c in REQUIRED:
        if c not in all_df.columns:
            all_df[c] = pd.NA
    all_df = all_df[REQUIRED]

    # 剔除指数行
    n_before = len(all_df)
    is_idx = all_df["symbol"].isin(idx_syms)
    all_df = all_df[~is_idx]
    dropped_idx = n_before - len(all_df)

    # 同 (symbol,date) 去重: quote_ts 非空优先, 其次保留先出现者(月分区打头)
    all_df = all_df.assign(
        _has_ts=all_df["quote_ts"].notna().astype(int),
    ).sort_values(["symbol", "date", "_has_ts"], ascending=[True, True, False])
    n_before = len(all_df)
    all_df = all_df.drop_duplicates(["symbol", "date"], keep="first")
    dropped_dup = n_before - len(all_df)

    all_df = all_df.drop(columns=["_has_ts"]).sort_values(["date", "symbol"]).reset_index(drop=True)
    all_df["date"] = pd.to_datetime(all_df["date"]).dt.date
    all_df["quote_ts"] = pd.to_numeric(all_df["quote_ts"], errors="coerce").astype("Int64")
    return all_df, [*log, {"dropped_index_rows": dropped_idx, "dropped_dup_rows": dropped_dup}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("需要 --dry-run 或 --apply")

    out, log = build()
    for item in log:
        print(" ", item)

    by_day = out.groupby("date").size().to_dict()
    print("\n== 合并结果 ==")
    print("rows", len(out), "days", len(by_day),
          "uniq(sym,date)", len(out.drop_duplicates(["symbol", "date"])))
    for d in sorted(by_day):
        print(f"   {d} rows={by_day[d]}")
    print("date range", min(by_day), "..", max(by_day))

    if args.dry_run:
        print("\n[dry-run] 未写盘")
        return 0

    tag = "monthly-migration-20261112"
    backup = DATA / "integrity_backups" / tag
    backup.mkdir(parents=True, exist_ok=True)

    month_dir = BASE / f"date={MONTH}"
    # 1. 备份现有月分区
    if month_dir.exists():
        b = backup / month_dir.name
        if b.exists():
            shutil.rmtree(b)
        shutil.copytree(month_dir, b)

    # 2. 备份并移走旧日分区
    moved = []
    for p in sorted(BASE.glob("date=2026-11-*")):
        if not p.is_dir():
            continue
        b = backup / p.name
        if b.exists():
            shutil.rmtree(b)
        shutil.copytree(p, b)
        moved.append(p.name)

    # 3. 写月分区 (先写临时名, 成功后替换)
    month_dir.mkdir(parents=True, exist_ok=True)
    tmp = month_dir / "part.parquet.tmp"
    out.to_parquet(tmp, index=False)
    (month_dir / "part.parquet").unlink(missing_ok=True)
    tmp.rename(month_dir / "part.parquet")

    # 4. 移除旧日分区 (已在备份里)
    for name in moved:
        shutil.rmtree(BASE / name)

    print(f"\n[apply] 已写 {month_dir}/part.parquet ({len(out)} 行)")
    print(f"[apply] 备份: {backup}")
    print(f"[apply] 移除日分区 {len(moved)} 个: {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
