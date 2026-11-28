"""最大合约口径：贴线到底是「簿平均」还是「那一份不能拆的合约」。

卷六的前提：定价单位是不可分割的索取权，不是合约的加权平均。
约束簿面板里的 wdist 是 tsr 加权平均、near_share 是数量占比，两者都会把
「一份大合约贴线 + 七份小合约远离」平均成中间值。本脚本补第三个数：

  big_ratio = 簿内 tsr 最大的那份合约的 平仓线/现价
  max_ratio = 簿内所有合约里最贴线的那份

输出 data/.scratch/leverage/largest_contract_panel.parquet（symbol, date, ...），
供 _diag_chip_path_twin.py 的 big 变体使用。口径与 constraint_book 完全一致：
同一 (股票, 持有人, 起始日, 平仓线, 到期日) 合并；活合约 = start ≤ t 且（unfreeze > t 或未解押）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_constraint_book import load_daily  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
SRC = OUT / "pledge_all.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"


def main() -> None:
    op, cl, am, to = load_daily("2015-01-01")
    dates = cl.index
    syms = list(cl.columns)
    sym2idx = {s: i for i, s in enumerate(syms)}
    C = cl.to_numpy(float)

    # 2020-01 ~ 2026-07 的月末（与 constraint_book 面板同一批交易日）
    per = pd.Series(dates, index=dates).groupby([dates.year, dates.month]).tail(1)
    mids = [int(np.where(dates == d)[0][0]) for d in per if pd.Timestamp("2020-01-01") <= d <= pd.Timestamp("2026-07-31")]

    p = pd.read_parquet(SRC)
    p["s"] = pd.to_datetime(p["PF_START_DATE"], errors="coerce")
    p["u"] = pd.to_datetime(p["UNFREEZE_DATE"], errors="coerce")
    p["tsr"] = pd.to_numeric(p["PF_TSR"], errors="coerce")
    p["line"] = pd.to_numeric(p["OPENLINE"], errors="coerce")
    p["open"] = p["UNFREEZE_STATE"].astype(str).eq("未解押")
    p = p.dropna(subset=["s", "tsr", "line"])
    p = p[(p["tsr"] > 0) & (p["line"] > 0)]
    p = p[p["u"].notna() | p["open"]]
    p.loc[p["u"].notna() & (p["u"] <= p["s"]), "u"] = p["s"] + pd.Timedelta(days=1)
    inst = pd.read_parquet(INST)[["code", "symbol"]].drop_duplicates("code")
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    p["code"] = p["code"].astype(str).str.zfill(6)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    p = p.groupby(["symbol", "idx", "HOLDER_NAME", "s", "u", "line"], dropna=False, as_index=False)["tsr"].sum()
    print(f"contracts {len(p):,} / {p['symbol'].nunique():,} symbols", flush=True)

    rows = []
    for m in mids:
        me = dates[m]
        live = (p["s"] <= me) & (p["u"].isna() | (p["u"] > me))
        q = p[live].copy()
        if q.empty:
            continue
        px = C[m, q["idx"].to_numpy()]
        ok = np.isfinite(px) & (px > 0)
        q = q.assign(px=px)[ok]
        q["ratio"] = q["line"] / q["px"]
        q = q.sort_values(["idx", "tsr"], kind="mergesort")
        g = q.groupby("idx").agg(
            book=("tsr", "sum"), ncon=("tsr", "size"),
            big_tsr=("tsr", "last"), big_ratio=("ratio", "last"),
            max_ratio=("ratio", "max"),
        )
        g["date"] = me
        g["symbol"] = [syms[i] for i in g.index]
        rows.append(g.reset_index(drop=True))
        if m in (mids[0], mids[-1]):
            print(f"  {me.date()}: {len(g):,} books", flush=True)

    out = pd.concat(rows, ignore_index=True)
    out["ym"] = out["date"].dt.to_period("M").astype(str)
    out.to_parquet(OUT / "largest_contract_panel.parquet", index=False)
    print(f"wrote {len(out):,} rows", flush=True)


if __name__ == "__main__":
    main()
