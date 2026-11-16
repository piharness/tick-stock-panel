"""清理 2026-11 enriched 日分区的两类脏行 (只删不造)。

两类:
  1. 同 (symbol,date) 出现两次 —— staging 残留 (part-stream-*) 遗留, 其中一份
     quote_ts 为空、其余字段相同。保留 quote_ts 非空者。
  2. symbol 属于指数维表但已被移出 kline_daily —— 派生表必须跟着源表, 否则
     shift(N) 窗口被指数点位污染 (个股日线已不再含这些行)。

不停牌过滤: enriched 本就不含停牌日 (compute_enriched 的 filter_halt_days),
11-03/11-04 少 8/6 只属预期, 不在本脚本处理范围。

用法:
  python analysis/_fix_enriched_november.py --dry-run
  python analysis/_fix_enriched_november.py --apply
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ENR = DATA / "kline_daily_enriched"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("需要 --dry-run 或 --apply")

    idx = set(pd.read_parquet(
        DATA / "instruments_index" / "instruments_index.parquet", columns=["symbol"]
    )["symbol"])
    daily = pd.read_parquet(DATA / "kline_daily" / "date=2026-11")
    daily["_d"] = daily["date"].astype(str)

    tag = "enriched-november-20261112"
    backup = DATA / "integrity_backups" / tag
    total_del_dup = total_del_idx = 0

    for p in sorted(ENR.glob("date=2026-11-*")):
        files = sorted(p.glob("*.parquet"))
        if len(files) != 1:
            print(f"!! {p.name} files={len(files)}, 跳过 (需人工看)")
            continue
        f = files[0]
        df = pd.read_parquet(f)
        day = str(df["date"].iloc[0])
        n0 = len(df)

        # 1. 去重: quote_ts 非空优先
        df = df.assign(_h=df["quote_ts"].notna().astype(int)) \
               .sort_values(["symbol", "_h"], ascending=[True, False])
        df = df.drop_duplicates("symbol", keep="first")
        n1 = len(df)

        # 2. 剔除 daily 中已不存在的指数行
        allowed = set(daily.loc[daily["_d"] == day, "symbol"])
        df = df[df["symbol"].isin(allowed)]
        n2 = len(df)

        df = df.drop(columns=["_h"]).sort_values("symbol")
        print(f"{p.name}: {n0} -> 去重 {n1} (-{n0 - n1}) -> 剔指数 {n2} (-{n1 - n2})"
              f" | daily_sym={len(allowed)}")
        total_del_dup += n0 - n1
        total_del_idx += n1 - n2

        if args.apply and n0 != n2:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, backup / f"{p.name}__part.parquet")
            tmp = f.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, index=False)
            tmp.replace(f)

    print(f"\n合计: 去重 -{total_del_dup}, 剔指数 -{total_del_idx}")
    if args.dry_run:
        print("[dry-run] 未写盘")
    else:
        print(f"[apply] 备份: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
