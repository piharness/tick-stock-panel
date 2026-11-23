"""博弈推论的可执行检验：量化在 T+1 开盘砸盘，是否留下可捡的价差.

两个假设，都能用日线直接否证：
  H1「隔夜修复」：量化集中在 T+1 开盘卖出 ⇒ T+1 收盘被压低 ⇒ T+1 收盘买、T+2 开盘卖应有正超额。
  H2「限价卖优于市价」：T+1 开盘挂 +x% 限价卖，成交率足够高时，期望优于开盘/收盘市价卖。

分母口径：T 日换手板（非一字、非 ST、上市>=90 天）。收益全部对同期全市场等权超额。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.price_limits import polars_limit_price, polars_price_limit_pct  # noqa: E402

DAILY = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "game_shift3.json"
COLS = ["symbol", "date", "open", "high", "close", "raw_close", "raw_high", "consecutive_limit_ups"]
YEARS = list(range(2015, 2027))
PAD = 12


def main() -> None:
    ds_all = sorted(d[5:] for d in os.listdir(DAILY) if d.startswith("date="))
    by_year: dict[str, list[str]] = {}
    for d in ds_all:
        by_year.setdefault(d[:4], []).append(d)
    inst = pl.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")
    names = dict(inst.select(["symbol", "name"]).iter_rows())
    listing = dict(inst.select(["symbol", "listing_date"]).iter_rows())

    out: dict[str, dict] = {}
    for y in YEARS:
        ds = by_year.get(str(y), [])
        if not ds:
            continue
        frames = []
        for d in ds + by_year.get(str(y + 1), [])[:PAD]:
            p = DAILY / f"date={d}" / "part.parquet"
            if p.exists():
                frames.append(pl.read_parquet(p, columns=COLS))
        df = pl.concat(frames).sort(["symbol", "date"])
        df = df.with_columns(
            [
                pl.col("symbol").replace_strict(names, default="").alias("name"),
                pl.col("symbol").replace_strict(listing, default=None).alias("ld"),
            ]
        )
        df = df.filter(~pl.col("name").str.to_uppercase().str.contains("ST", literal=True))
        df = df.filter(
            pl.col("ld").is_null()
            | ((pl.col("date") - pl.col("ld").str.to_date()).dt.total_days() >= 90)
        )
        df = df.with_columns(
            (pl.col("open") * pl.col("raw_close") / pl.col("close")).alias("raw_open")
        )
        df = df.with_columns(pl.col("raw_close").shift(1).over("symbol").alias("prev"))
        df = df.with_columns(
            polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.lit(False)).alias("pct")
        )
        df = df.with_columns(polars_limit_price(pl.col("prev"), pl.col("pct"), up=True).alias("lim"))
        for f in ("open", "high", "close"):
            for k in (1, 2, 3):
                df = df.with_columns(pl.col(f).shift(-k).over("symbol").alias(f"{f}{k}"))
        # 全窗口市场基准
        df = df.with_columns(
            [
                (pl.col("close1") / pl.col("open1") - 1).alias("b_o1c1"),
                (pl.col("open2") / pl.col("close1") - 1).alias("b_c1o2"),
                (pl.col("close2") / pl.col("open2") - 1).alias("b_o2c2"),
                (pl.col("close2") / pl.col("close1") - 1).alias("b_c1c2"),
                (pl.col("open1") / pl.col("close") - 1).alias("b_gap"),
            ]
        )
        for c in [c for c in df.columns if c.startswith("b_")]:
            df = df.with_columns(
                (pl.col(c) - pl.col(c).mean().over("date")).alias(c.replace("b_", "x_"))
            )
        df = df.filter(pl.col("prev").is_not_null() & (pl.col("prev") > 0))
        df = df.with_columns(
            [
                (pl.col("raw_close") >= pl.col("lim") - 0.005).alias("lu"),
                (pl.col("raw_open") >= pl.col("lim") - 0.005).alias("yizi"),
            ]
        )
        cur = df.filter((pl.col("date").dt.year() == y) & pl.col("lu") & ~pl.col("yizi"))
        cur = cur.with_columns(
            [
                (pl.col("close1") / pl.col("open1") - 1).alias("r_o1c1"),
                (pl.col("open2") / pl.col("close1") - 1).alias("r_c1o2"),
                (pl.col("close2") / pl.col("open2") - 1).alias("r_o2c2"),
                (pl.col("close2") / pl.col("close1") - 1).alias("r_c1c2"),
                (pl.col("close3") / pl.col("close2") - 1).alias("r_c2c3"),
                (pl.col("open1") / pl.col("close") - 1).alias("r_gap"),
                ((pl.col("high1") / pl.col("open1") - 1).fill_null(0)).alias("r_hi1"),
            ]
        )

        def m(c: str) -> float | None:
            v = cur[c].drop_nulls()
            return round(float(v.mean()), 6) if v.len() else None

        rec = {
            "n": cur.height,
            "gap": m("r_gap"),
            "x_gap": m("x_gap"),
            "o1c1": m("r_o1c1"),
            "x_o1c1": m("x_o1c1"),
            "c1o2": m("r_c1o2"),
            "x_c1o2": m("x_c1o2"),
            "o2c2": m("r_o2c2"),
            "x_o2c2": m("x_o2c2"),
            "c1c2": m("r_c1c2"),
            "x_c1c2": m("x_c1c2"),
            "c2c3": m("r_c2c3"),
            "hi1": m("r_hi1"),
        }
        # H2 限价卖：T+1 开盘挂 +x%
        for thr in (0.01, 0.02, 0.03, 0.05):
            hit = cur["r_hi1"] >= thr
            fill = float(hit.mean())
            # 成交则锁定 thr（相对开盘），未成交按 T+1 收盘卖
            pnl = (
                cur.select(
                    [
                        pl.when(hit).then(thr).otherwise(pl.col("r_o1c1")).alias("p"),
                    ]
                )["p"]
                .drop_nulls()
                .mean()
            )
            rec[f"limit_{int(thr*100)}"] = {
                "fill": round(fill, 4),
                "pnl": round(float(pnl), 6),
                "pnl_ex": round(float(pnl) - (m("b_o1c1") or 0), 6),
            }
        out[str(y)] = rec
        print(y, "n", cur.height, flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
