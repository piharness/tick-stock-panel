"""构建持仓排雷用的席位状态表 —— data/dragon_tiger_seats/seat_relay_state.parquet。

## 为什么是离线表

`relay5` 需要「该股过去 5 个交易日的买入榜席位集合」，`streak` 需要连续上榜天数，
两者都无法在单日快照里算出来。运行时逐票回溯会把一次 GET 变成 N 次日线扫描，
且必然与 `analysis/_diag_seat_relay.py` 形成第二份定义（同一真相两个定义点）。

故：**只读 `_diag_seat_relay.py` 产出的面板**，本脚本不重算 relay5 / streak，
只补一个 `dist_hi`（T 日收盘 / 近 20 日收盘最高 − 1），并把阈值翻译成产品用的 level。

依赖链（可复现）：
  data/dragon_tiger_seats/date=*.parquet        原始席位（东财）
  → analysis/_diag_seat_relay.py                口径修复 + relay5/streak
  → data/.scratch/seat_relay_panel.parquet      事件面板（唯一真相）
  → 本脚本
  → data/dragon_tiger_seats/seat_relay_state.parquet   运行时读这张

## level 阈值的数据依据

`review`（锁仓+新高+streak≥5）：n=122，ex5 = −356bp，中位 −800bp，胜率 37%，
按日聚合 t = −1.86（93 个交易日）。同 streak≥5 但非锁仓的对照仅 −85bp（t=−0.42）。
**方向成立但只是边缘显著**，故 level 只驱动「人工复核」，不驱动任何自动卖出。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "dragon_tiger_seats" / "seat_relay_state.parquet"
PANEL = ROOT / "data" / ".scratch" / "seat_relay_panel.parquet"
KLINE = ROOT / "data" / "kline_daily_enriched"

# 阈值：与 docs/seat-relay.md 第 4 节一致，改动须同步文档与测试
REVIEW_RELAY = 0.2      # 买入前 5 席位里新面孔 <=1 个 = 锁仓
REVIEW_DIST_HI = -0.02  # 距 20 日收盘新高 2% 以内 = 仍在高位
REVIEW_STREAK = 5       # 连续上榜 >=5 日
WATCH_STREAK = 3        # 仅提示


def main() -> None:
    if not PANEL.exists():
        sys.exit(f"缺面板 {PANEL}；先跑 analysis/_diag_seat_relay.py")

    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel.date)

    files = sorted(glob.glob(str(KLINE / "date=*/part.parquet")))
    files = [f for f in files if "date=2025-1" in f or "date=2026" in f]
    k = pd.concat([pd.read_parquet(f, columns=["symbol", "date", "close"])
                   for f in files], ignore_index=True)
    k["date"] = pd.to_datetime(k.date.astype(str))
    k = k.sort_values(["symbol", "date"]).reset_index(drop=True)
    k["hi20"] = k.groupby("symbol").close.transform(lambda s: s.rolling(20, min_periods=10).max())

    d = panel.merge(k[["symbol", "date", "close", "hi20"]].rename(columns={"close": "t_close"}),
                    on=["symbol", "date"], how="left")
    d["dist_hi"] = d.t_close / d.hi20 - 1

    locked = d.relay5 <= REVIEW_RELAY
    near_hi = d.dist_hi >= REVIEW_DIST_HI
    d["level"] = np.select(
        [locked & near_hi & (d.streak >= REVIEW_STREAK),
         locked & near_hi & (d.streak >= WATCH_STREAK)],
        ["review", "watch"], default="ok",
    )

    out = d[["symbol", "date", "relay5", "net5_ratio", "streak", "dist_hi", "level"]].copy()
    out["date"] = out.date.dt.strftime("%Y-%m-%d")
    out = out.sort_values(["date", "symbol"]).reset_index(drop=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)

    print(f"写入 {OUT}  {len(out):,} 行 / {out.symbol.nunique():,} 只 / "
          f"{out.date.min()} ~ {out.date.max()}")
    print("\nlevel 分布：")
    print(out.level.value_counts().to_string())
    last = out[out.date == out.date.max()]
    print(f"\n最新日 {out.date.max()} 共 {len(last)} 只：")
    print(last.level.value_counts().to_string())
    rev = last[last.level == "review"]
    if not rev.empty:
        print("\nreview 名单：")
        print(rev[["symbol", "relay5", "streak", "dist_hi", "net5_ratio"]]
              .assign(dist_hi=lambda x: (x.dist_hi * 100).round(1),
                      net5_ratio=lambda x: (x.net5_ratio * 100).round(1))
              .to_string(index=False))


if __name__ == "__main__":
    main()
