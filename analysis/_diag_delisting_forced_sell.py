#!/usr/bin/env python3
"""_diag_delisting_forced_sell.py — 退市/ST 清仓「被迫卖压 → 回补」事件研究

背景（docs/structural-edge/forced-counterparty-ledger.md 第 3 类被迫流）：
  基金合同 + 风控红线禁止持有 ST / 退市股，叠加赎回压力 → 逼近退市红线时不计成本出清。
  假说：这股被迫卖压把价格砸出「坑」（overshoot），之后应回补（均值回归）。

事件定义（规则驱动、无歧义）：每只股票**首次**收盘价（原始价 raw_close）跌破阈值
  1.5 / 1.2 / 1.0 元 —— 进入面值退市危险区（规则：连续 20 交易日 < 1 元即终止上市）。

核心方法：生存偏差修正。
  本地 `kline_daily_enriched` 只含在市标的（5,573 只），已退市股不在库内；只在这上面
  测「跌破后回补」会系统性高估（幸存者才看得到回补）。用补全包 `stock_gap_daily.parquet`
  （306 缺口股，含 154 退市）并入，得到「含已退市股」的全样本。
  修正口径：缺口股事件若 20 日内无后续数据（= 退市/终止交易），forward 收益记为 -100%。

产出：analysis/_diag_delisting_forced_sell_result.json
      data/_diag_delisting_forced_sell_events.parquet

运行：backend/.venv/Scripts/python.exe analysis/_diag_delisting_forced_sell.py
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

ENR = Path("data/kline_daily_enriched")
GAP = Path("data/backfill_20261117/stock_gap_daily.parquet")
OUT_JSON = Path(__file__).with_name("_diag_delisting_forced_sell_result.json")
OUT_EV = Path("data/_diag_delisting_forced_sell_events.parquet")

THRESHOLDS = [1.5, 1.2, 1.0]


def load_full_panel() -> pl.DataFrame:
    enr = (
        pl.scan_parquet(str(ENR / "**" / "*.parquet"), extra_columns="ignore")
        .select(["symbol", "date", "raw_close"])
        .collect()
    )
    enr = enr.with_columns(pl.lit("enr").alias("src"))
    gap = pl.read_parquet(GAP).select(["symbol", "date", "close"]).rename({"close": "raw_close"})
    gap = gap.with_columns(pl.lit("gap").alias("src"))
    full = pl.concat([enr, gap]).unique(subset=["symbol", "date"], keep="last")
    # 关键：unique 不保序，sort 必须在所有 shift 之前
    return full.sort(["symbol", "date"])


def summarize(events: pl.DataFrame, thr: float) -> list[dict]:
    out = []
    for horizon, col in [("fwd20", "fwd20"), ("fwd60", "fwd60")]:
        for scope, mask in [
            ("all", pl.lit(True)),
            ("enr_only", pl.col("src") == "enr"),
        ]:
            sub = events.filter(mask)
            valid = sub.filter(pl.col(col).is_not_null())
            no_data = sub.filter(pl.col(col).is_null())
            # 缺口股 + 无后续数据 = 退市/终止 → 记为 -100%
            ended = no_data.filter(pl.col("src") == "gap")
            recent = no_data.filter(pl.col("src") == "enr")
            n = sub.height
            n_valid = valid.height
            n_ended = ended.height
            n_recent = recent.height
            # 修正 mean：有效收益 + ended(-1)，recent 排除（尚在交易、数据未到）
            if n - n_recent > 0:
                valid_ret = valid[col].to_list()
                corrected = valid_ret + [-1.0] * n_ended
                corrected_mean = sum(corrected) / len(corrected)
            else:
                corrected_mean = None
            med = valid[col].median() if n_valid else None
            mean = valid[col].mean() if n_valid else None
            out.append({
                "threshold": thr, "horizon": horizon, "scope": scope,
                "n": n, "n_valid": n_valid, "n_ended": n_ended, "n_recent": n_recent,
                "ended_pct": round(n_ended / n * 100, 1) if n else None,
                "fwd_median_bp": round(med * 1e4, 1) if med is not None else None,
                "fwd_mean_bp": round(mean * 1e4, 1) if mean is not None else None,
                "corrected_mean_bp": round(corrected_mean * 1e4, 1) if corrected_mean is not None else None,
            })
    return out


def main() -> None:
    print("读取并合并 enriched + 缺口股 ...")
    full = load_full_panel()
    print(f"full rows={full.height} symbols={full['symbol'].n_unique()}")

    full = full.with_columns(
        pl.col("raw_close").shift(-20).over("symbol").alias("px20"),
        pl.col("raw_close").shift(-60).over("symbol").alias("px60"),
    )
    full = full.with_columns(
        (pl.col("px20") / pl.col("raw_close") - 1).alias("fwd20"),
        (pl.col("px60") / pl.col("raw_close") - 1).alias("fwd60"),
    )

    all_results: list[dict] = []
    event_frames: list[pl.DataFrame] = []
    for thr in THRESHOLDS:
        ev = full.filter(pl.col("raw_close") < thr)
        ev_first = ev.group_by("symbol", maintain_order=True).first()
        ev_first = ev_first.with_columns(pl.lit(thr).alias("threshold"))
        event_frames.append(ev_first)
        all_results.extend(summarize(ev_first, thr))

    events = pl.concat(event_frames).select(
        ["symbol", "date", "raw_close", "src", "threshold", "px20", "px60", "fwd20", "fwd60"]
    )
    events.write_parquet(OUT_EV)

    OUT_JSON.write_text(json.dumps({"n_symbols": full["symbol"].n_unique(), "results": all_results},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 104)
    print("退市/ST 清仓「被迫卖压→回补」— 首次跌破阈值后 forward 收益（bp，生存偏差修正）")
    print("=" * 104)
    print(f"{'阈值':<5}{'horizon':<8}{'范围':<10}{'n':>6}{'有效':>6}{'退市':>6}{'退市%':>7}{'中位':>8}{'均值':>9}{'修正均值':>10}")
    for r in all_results:
        print(f"{r['threshold']:<5}{r['horizon']:<8}{r['scope']:<10}{r['n']:>6}{r['n_valid']:>6}"
              f"{r['n_ended']:>6}{str(r['ended_pct']):>7}{str(r['fwd_median_bp']):>8}"
              f"{str(r['fwd_mean_bp']):>9}{str(r['corrected_mean_bp']):>10}")

    print(f"\n事件明细: {OUT_EV}   ({events.height} 行)")
    print(f"JSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
