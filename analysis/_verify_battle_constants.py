"""重算 /battle 里引用的两个历史常数，并把结果写成前端可 import 的 JSON。

背景：页面曾经把「−45.1bp / t=−2.61（239 日、68 席位）」和「60 日 353 样本、
高开≥5% 子集 −1.97% vs 整体 +0.54%」写成散文常量。这类数字的死法是：写下的
那天是对的，之后没人再算，窗口滚走以后页面还在拿它当结论。本脚本给它一个
唯一的重算入口 —— 数字从数据来，`--check` 能在漂移时退出非零。

口径（与 docs/dragon-tiger-assessment.md §三 一致）：
  T 日盘后榜单 → T+1 开盘买入 → 持有 H 日到 close[T+1+H]
  超额 = 个股收益 − 同一入场日全市场等权同窗口收益
  t 值 = 按入场日聚合后的日度均值序列（不是按笔）

用法：
  python analysis/_verify_battle_constants.py            # 只打印
  python analysis/_verify_battle_constants.py --write     # 写 frontend/src/lib/battleBenchmarks.json
  python analysis/_verify_battle_constants.py --check     # 与已有 JSON 比，漂移 >5% 退出 1
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DATA = ROOT / "data"
OUT = ROOT / "frontend" / "src" / "lib" / "battleBenchmarks.json"

HOLDS = (1, 2, 5)


def load_panel() -> pl.DataFrame:
    """全部日 K → (date, symbol, open, close)。月/日分区都读。

    extra_columns="ignore" 是必需的：月分区 (date=2026-11) 带 quote_ts 列，
    日分区不带；默认扫描会抛 SchemaError（"extra column in file outside of
    expected schema"）而不是降级。这也是任何想 glob 整张 kline_daily 的脚本
    都会踩的坑。
    """
    lf = pl.scan_parquet(
        str(DATA / "kline_daily" / "**" / "*.parquet"),
        extra_columns="ignore",
    )
    df = lf.select(["date", "symbol", "open", "close"]).collect()
    return df.with_columns(pl.col("date").cast(pl.Date, strict=False)).drop_nulls()


def dragon_tiger_constants(panel: pl.DataFrame) -> dict:
    days = sorted(panel["date"].unique().to_list())
    cal = pl.DataFrame({"date": days, "i": list(range(len(days)))}).with_columns(
        pl.col("date").cast(pl.Date)
    )
    panel = panel.join(cal, on="date", how="left")

    rows: list[dict] = []
    for path in sorted((DATA / "dragon_tiger").glob("date=*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for it in ((payload.get("all") or {}).get("stock_items") or []):
            if (it.get("range_days") or 1) != 1:
                continue
            sym = it.get("thscode")
            if sym:
                rows.append({"bdate": date.fromisoformat(payload["trade_date"]), "sym": sym})
    if not rows:
        return {"error": "no cached dragon tiger"}

    board = pl.DataFrame(rows).unique().join(
        cal.rename({"date": "bdate", "i": "bi"}), on="bdate", how="inner"
    ).with_columns((pl.col("bi") + 1).alias("entry_i"))

    opens = panel.select(["symbol", "i", "open"]).rename({"symbol": "sym"})
    closes = panel.select([pl.col("symbol").alias("sym"), (pl.col("i")).alias("ic"), pl.col("close")])

    out: dict = {"days": board["bdate"].n_unique(), "trades": board.height}
    for h in HOLDS:
        exits = closes.with_columns((pl.col("ic") - h).alias("i")).select(["sym", "i", "close"])
        pairs = opens.join(exits, on=["sym", "i"], how="inner").with_columns(
            ((pl.col("close") / pl.col("open")) - 1.0).alias("ret")
        )
        bench = pairs.group_by("i").agg(pl.col("ret").mean().alias("bench"))
        px = pairs.join(board.select(["sym", "entry_i"]), left_on=["sym", "i"],
                        right_on=["sym", "entry_i"], how="inner")
        px = px.join(bench, on="i", how="left").with_columns((pl.col("ret") - pl.col("bench")).alias("excess"))
        daily = px.group_by("i").agg(pl.col("excess").mean().alias("m")).sort("i")
        s = daily["m"].to_list()
        n = len(s)
        mean = sum(s) / n if n else 0.0
        sd = math.sqrt(sum((v - mean) ** 2 for v in s) / (n - 1)) if n > 1 else 0.0
        t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
        out[f"H{h}"] = {"excess_bp": round(mean * 1e4, 1), "t": round(t, 2), "n_days": n}
    return out


def auction_constants(window: int = 60) -> dict:
    """竞价名单：近 window 个交易日的整体与高开≥5% 子集收益。

    需要 fuyao provider（一年内历史），一次一天地取；day0 收益用本地日 K 现算。
    """
    from app.services import auction_benchmark as ab  # noqa: PLC0415

    provider = ab._provider()
    if provider is None:
        return {"error": "fuyao not configured"}
    days = sorted(ab._local_trading_days(DATA))[-window:]
    all_ocs: list[float] = []
    chase_ocs: list[float] = []
    used = 0
    for d in days:
        try:
            raw = provider.short_term_benchmark(d.isoformat())
        except Exception:  # noqa: BLE001 — 单日失败不中断窗口
            continue
        items = [r for r in (raw.get("item") or []) if isinstance(r, dict)]
        if not items:
            continue
        enriched = ab._enrich(DATA, d, items)
        used += 1
        for row in enriched:
            oc = row.get("day0_oc")
            if oc is None:
                continue
            all_ocs.append(oc)
            if (row.get("auction_pct") or 0) >= 5:
                chase_ocs.append(oc)
    if not all_ocs:
        return {"error": "no samples"}
    return {
        "window_days": window,
        "days_used": used,
        "n": len(all_ocs),
        "all_mean": round(sum(all_ocs) / len(all_ocs), 6),
        "chase_ge5_n": len(chase_ocs),
        "chase_ge5_mean": round(sum(chase_ocs) / len(chase_ocs), 6) if chase_ocs else None,
        "as_of": max(days).isoformat(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--auction-window", type=int, default=60)
    args = ap.parse_args()

    print("读取日 K 面板…", flush=True)
    panel = load_panel()
    print(f"  {panel.height} 行 / {panel['date'].n_unique()} 个交易日 / {panel['symbol'].n_unique()} 只", flush=True)

    print("重算龙虎榜常数…", flush=True)
    dt = dragon_tiger_constants(panel)
    print(f"  {dt}", flush=True)

    print(f"重算竞价常数（近 {args.auction_window} 个交易日，逐日取数）…", flush=True)
    auc = auction_constants(args.auction_window)
    print(f"  {auc}", flush=True)

    payload = {"generated_at": date.today().isoformat(), "dragon_tiger": dt, "auction": auc}

    if args.write:
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"写入 {OUT}", flush=True)

    if args.check:
        if not OUT.exists():
            print("没有可比的基准文件；先跑 --write", flush=True)
            return 1
        old = json.loads(OUT.read_text(encoding="utf-8"))
        drift = []
        for key in ("dragon_tiger", "auction"):
            for field, new_v in payload[key].items():
                old_v = (old.get(key) or {}).get(field)
                if isinstance(new_v, (int, float)) and isinstance(old_v, (int, float)) and old_v:
                    if abs(new_v - old_v) / abs(old_v) > 0.05:
                        drift.append(f"{key}.{field}: {old_v} → {new_v}")
        if drift:
            print("漂移超过 5%:\n  " + "\n  ".join(drift), flush=True)
            return 1
        print("与基准一致（±5%）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
