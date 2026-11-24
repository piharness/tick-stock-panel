"""席位接力（seat relay）—— 「每天换一拨人买」是否携带独立信息。

## 为什么需要这个脚本

原始落盘 `data/dragon_tiger_seats/*.parquet` 把 1/3/10/30 日榜混在一起，
同一 (symbol, date, dept) 会出现多条不同金额。直接聚合会把「3 日累计买入」
当成「单日买入」，金额与占比全部失真。

去重口径（已实测验证）：每条记录的 `buy / buy_ratio` = 该榜的隐含分母。
  - 单日榜的隐含分母 == 当日成交额（002242 2026-11-17: 3.544e9 vs amount 3.544e9，精确相等）
  - 多日榜的隐含分母 == 窗口累计成交额（同日另一组 7.59e9 ≈ 2x）
故：保留 |denom / amount - 1| < 0.5% 的记录 = 单日榜。取不到单日榜的事件丢弃（不猜）。

## 指标定义

对每个 (symbol, date) 的单日买入前 5 席位：
  n_new(k)   = 今日买入前5中，未出现在该股过去 k 个交易日买入榜的席位数
  relay(k)   = n_new(k) / 5                    ← 「接力度」：1=全新面孔，0=全是老面孔
  streak     = 连续上榜天数（含当日）
  net5_ratio = 前5席位净买入合计 / 当日成交额

## 输出

data/.scratch/seat_relay_panel.parquet  —— 事件面板（供后续分组回测）
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SEAT = ROOT / "data" / "dragon_tiger_seats"
KLINE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"
OUT.mkdir(parents=True, exist_ok=True)

DENOM_TOL = 0.005  # 隐含分母与当日成交额的相对容差


def load_seats() -> pd.DataFrame:
    files = sorted(glob.glob(str(SEAT / "date=*.parquet")))
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df[df.buy > 0].copy()
    df["denom"] = df.buy / df.buy_ratio.replace(0, np.nan)
    return df


def load_amount() -> pd.DataFrame:
    files = sorted(glob.glob(str(KLINE / "date=*/part.parquet")))
    files = [f for f in files if "date=2025-1" in f or "date=2026" in f]
    amt = pd.concat(
        [pd.read_parquet(f, columns=["symbol", "date", "amount", "turnover_rate"]) for f in files],
        ignore_index=True,
    )
    return amt


def main() -> None:
    seats = load_seats()
    amt = load_amount()
    amt["date"] = amt.date.astype(str)

    print(f"原始席位记录 {len(seats):,} 行（buy>0）", flush=True)
    print(f"事件数 {(seats.symbol + seats.date).nunique():,}", flush=True)

    # ---- 只保留单日榜 ----
    m = seats.merge(amt, on=["symbol", "date"], how="left", suffixes=("", "_k"))
    m["denom_rel"] = (m.denom / m.amount - 1).abs()
    ms = m[m.denom_rel <= DENOM_TOL].copy()
    print(f"单日榜记录 {len(ms):,} 行（匹配率 {len(ms) / len(seats):.1%}）", flush=True)

    # 每个 (symbol,date,side) 应有 5 条
    cnt = ms[ms.side == "buy"].groupby(["symbol", "date"]).size()
    print("\n每 (symbol,date) 单日买入席位数分布：")
    print(cnt.value_counts().sort_index().to_string(), flush=True)

    # ---- 买入前 5 ----
    b = ms[ms.side == "buy"].sort_values(["symbol", "date", "buy"], ascending=[True, True, False])
    b["rk"] = b.groupby(["symbol", "date"]).cumcount() + 1
    b5 = b[b.rk <= 5].copy()
    # 同一席位可能因数据把买卖拆成两条，去重保留金额最大者
    b5 = b5.sort_values("buy", ascending=False).drop_duplicates(["symbol", "date", "dept_code"])
    print(f"\n前5席位明细 {len(b5):,} 行，覆盖 {b5.symbol.nunique()} 只票、{b5.date.nunique()} 天", flush=True)

    # ---- 逐票时间序列：接力度 ----
    b5 = b5.sort_values(["symbol", "date"])
    b5["net"] = b5.net.fillna(0)

    rows = []
    for sym, g in b5.groupby("symbol", sort=False):
        dates = sorted(g.date.unique())
        idx = {d: i for i, d in enumerate(dates)}
        by_date = {d: set(x.dept_code) for d, x in g.groupby("date")}
        net_by_date = g.groupby("date").net.sum().to_dict()
        amt_by_date = g.groupby("date").amount.first().to_dict()
        prev = None
        for i, d in enumerate(dates):
            cur = by_date[d]
            rec = {"symbol": sym, "date": d, "n_seat": len(cur),
                   "net5_ratio": net_by_date[d] / amt_by_date[d] if amt_by_date[d] else np.nan}
            for k in (5, 10):
                past = set()
                for j in range(max(0, i - k), i):
                    past |= by_date[dates[j]]
                rec[f"n_new{k}"] = len(cur - past)
                rec[f"relay{k}"] = len(cur - past) / len(cur) if cur else np.nan
            rec["is_first"] = i == 0 or (pd.Timestamp(d) - pd.Timestamp(dates[i - 1])).days > 10
            rows.append(rec)

    panel = pd.DataFrame(rows)
    # 连续上榜天数：同日历周内连续（间隔<=4天算连续，跳过周末）
    panel["date"] = pd.to_datetime(panel.date)
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
    gap = panel.groupby("symbol").date.diff().dt.days
    panel["new_seg"] = (gap.isna()) | (gap > 4)
    panel["streak"] = panel.groupby([panel.symbol, panel.new_seg.cumsum()]).cumcount() + 1

    panel.to_parquet(OUT / "seat_relay_panel.parquet", index=False)
    print(f"\n事件面板 {len(panel):,} 行 → {OUT / 'seat_relay_panel.parquet'}", flush=True)
    print("\n=== streak 分布 ===")
    print(panel.streak.value_counts().sort_index().head(15).to_string(), flush=True)
    print("\n=== streak>=3 子样本的 relay5 分布 ===")
    sub = panel[panel.streak >= 3]
    print(f"n={len(sub):,}  票={sub.symbol.nunique()}")
    print(sub.relay5.describe().to_string(), flush=True)
    print("\n=== relay5 分桶（streak>=3）===")
    print(pd.cut(sub.relay5, [-.01, .2, .4, .6, .8, 1.01]).value_counts().sort_index().to_string(), flush=True)
    print("\n=== net5_ratio 分布（streak>=3）===")
    print(sub.net5_ratio.describe().to_string(), flush=True)


if __name__ == "__main__":
    main()
