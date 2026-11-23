"""通用标的探针：一只票「未来 20 日会不会被游资炒」，以及「如果已经在炒，还能不能追」。

对 600817.SH / 603324.SH 使用完全同一套口径，便于对比。

口径（与 _diag_600817_youzi.py 一致）：
  - 标签：未来 H=20 个交易日内 max(consecutive_limit_ups) >= 2 记为「连板」。
  - 状态特征只用 t 日及之前：turn20 / ret20 / ret120 / 大股东 / 自由流通。
  - 收益一律用 close（前复权）自洽计算，绝不与 raw_close 混除。

新增（回答「已经在炒还能不能追」）：
  - 连板晋级率：给定当日连板高度 k，次日晋级到 k+1 的条件概率。
  - 死亡换手：在 k>=2 的样本里，按当日换手率分档看次日晋级率。
  - 可执行性：次日开盘涨幅 > 9.5% 视为一字板买不进，单独统计。

用法：python analysis/_diag_youzi_probe.py 600817.SH 603324.SH
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
OUT = ROOT / "data" / ".scratch"
H = 20
START_YEAR = "2016"

pl.Config.set_tbl_rows(80)
pl.Config.set_tbl_width_chars(210)
pl.Config.set_tbl_cols(30)


def main() -> None:
    syms = sys.argv[1:] or ["600817.SH", "603324.SH"]

    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:4] >= START_YEAR]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "close", "raw_close", "raw_high",
                 "amount", "turnover_rate", "consecutive_limit_ups"])
        .collect()
        .sort(["symbol", "date"])
    )
    print(f"面板 {df.height:,} 行", flush=True)

    df = df.with_columns([
        pl.col("consecutive_limit_ups").shift(-1).over("symbol").alias("nxt_lu"),
        pl.col("close").shift(-1).over("symbol").alias("c1"),
        pl.col("close").shift(-5).over("symbol").alias("c5"),
        pl.col("close").shift(-H).over("symbol").alias("c20"),
        pl.col("open").shift(-1).over("symbol").alias("o1"),
        pl.col("consecutive_limit_ups").shift(-1).rolling_max(H).over("symbol").alias("f_max_lu"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn20"),
        pl.col("amount").rolling_mean(20).over("symbol").alias("amt20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(120).over("symbol") - 1.0).alias("ret120"),
    ])
    df = df.with_columns([
        (pl.col("c1") / pl.col("close") - 1.0).alias("f1"),
        (pl.col("c5") / pl.col("close") - 1.0).alias("f5"),
        (pl.col("c20") / pl.col("close") - 1.0).alias("f20"),
        (pl.col("o1") / pl.col("close") - 1.0).alias("gap1"),
    ])

    # 股东
    tf = pl.read_parquet(glob.glob(str(ROOT / "data/shareholder/top_float/*.parquet"))[0])
    latest = tf.group_by("code").agg(pl.col("period_end").max().alias("period_end"))
    tf = tf.join(latest, on=["code", "period_end"], how="inner")
    own = tf.group_by("code").agg([
        pl.col("ratio").max().alias("top1"),
        pl.when(pl.col("ratio") >= 5.0).then(pl.col("ratio")).otherwise(0.0).sum().alias("blk"),
    ]).with_columns(
        pl.col("code").str.replace(r"\..*$", "").alias("bare"),
        (100.0 - pl.col("blk")).alias("free_pct"),
    )
    names = pl.read_parquet(ROOT / "data/instruments/instruments.parquet").select(
        ["symbol", "name", "total_shares"])
    hy = pl.read_parquet(str(ROOT / "data/ext_data/ext_hy_ths/part.parquet")).select(
        ["symbol", "所属同花顺行业"]).rename({"所属同花顺行业": "industry"})

    d = (df.with_columns(pl.col("symbol").str.replace(r"\..*$", "").alias("bare"))
         .join(names, on="symbol", how="left")
         .join(hy, on="symbol", how="left")
         .join(own.select(["bare", "top1", "free_pct"]), on="bare", how="left"))

    # ---------------- 1. 画像 ----------------
    print("\n=== 1. 画像对比（最后交易日 2026-11-20）===")
    prof = {}
    for s in syms:
        r = d.filter(pl.col("symbol") == s).sort("date").tail(1).to_dicts()[0]
        prof[s] = {
            "name": r["name"], "industry": r["industry"],
            "close": r["raw_close"], "chg_pct": round(r["f1"] * 100, 2) if r["f1"] is not None else None,
            "lu_height": int(r["consecutive_limit_ups"]),
            "turn_pct": round(r["turnover_rate"], 2),
            "turn20_pct": round(r["turn20"], 2),
            "amt_yi": round(r["amount"] / 1e8, 2),
            "amt20_yi": round(r["amt20"] / 1e8, 2),
            "ret20_pct": round(r["ret20"] * 100, 2) if r["ret20"] is not None else None,
            "ret120_pct": round(r["ret120"] * 100, 2) if r["ret120"] is not None else None,
            "top1_pct": round(r["top1"], 1) if r["top1"] else None,
            "free_pct": round(r["free_pct"], 1) if r["free_pct"] else None,
            "mktcap_yi": round(float(r["total_shares"]) * r["raw_close"] / 1e8, 1),
            "free_mktcap_yi": round(float(r["total_shares"]) * r["raw_close"] / 1e8 * (r["free_pct"] or 0) / 100, 1),
        }
    for s in syms:
        print(f"  {s}: " + json.dumps(prof[s], ensure_ascii=False))

    # ---------------- 2. 触发条件 ----------------
    print("\n=== 2. 三条触发条件（上一轮定义）===")
    trig = {}
    for s in syms:
        p = prof[s]
        c = {
            "① ret20>+15%": (p["ret20_pct"] or -99) > 15,
            "② turn20>1.5%": (p["turn20_pct"] or 0) > 1.5,
            "③ 龙虎榜游资净买": None,
        }
        trig[s] = c
        print(f"  {s}: ①={c['① ret20>+15%']} ②={c['② turn20>1.5%']} "
              f"(ret20={p['ret20_pct']}%, turn20={p['turn20_pct']}%)")

    # ---------------- 3. 连板晋级率 ----------------
    print("\n=== 3. 连板晋级率：当日 k 板 → 次日 (k+1) 板 ===")
    base = d.filter(pl.col("nxt_lu").is_not_null() & pl.col("f20").is_not_null())
    rows = []
    for k, lo, hi in [(1, 1, 1), (2, 2, 2), (3, 3, 3), (4, 4, 4), (5, 5, 5), ("6+", 6, 99)]:
        seg = base.filter((pl.col("consecutive_limit_ups") >= lo) & (pl.col("consecutive_limit_ups") <= hi))
        if seg.height < 50:
            continue
        promote = seg.filter(pl.col("nxt_lu") > seg["consecutive_limit_ups"]).height / seg.height
        oneword = seg.filter(pl.col("gap1") > 0.095).height / seg.height
        row = {
            "板数": k, "n": seg.height,
            "次日晋级%": round(promote * 100, 1),
            "次日一字%(买不进)": round(oneword * 100, 1),
            "次日均收益%": round(float(seg["f1"].mean()) * 100, 2),
            "5日均收益%": round(float(seg["f5"].mean()) * 100, 2),
            "20日均收益%": round(float(seg["f20"].mean()) * 100, 2),
            "20日中位%": round(float(seg["f20"].median()) * 100, 2),
            "20日最大回撤p10%": round(float(seg["f20"].quantile(0.1)) * 100, 1),
        }
        rows.append(row)
        print("  " + json.dumps(row, ensure_ascii=False))

    # ---------------- 4. 死亡换手 ----------------
    print("\n=== 4. 死亡换手：k>=2 时，当日换手率 vs 次日晋级率 ===")
    hi2 = base.filter(pl.col("consecutive_limit_ups") >= 2)
    for lo, hi, lab in [(0, 5, "<5%"), (5, 10, "5-10%"), (10, 15, "10-15%"),
                        (15, 20, "15-20%"), (20, 100, ">20%")]:
        seg = hi2.filter((pl.col("turnover_rate") >= lo) & (pl.col("turnover_rate") < hi))
        if seg.height < 30:
            continue
        promote = seg.filter(pl.col("nxt_lu") > seg["consecutive_limit_ups"]).height / seg.height
        print(f"  换手{lab}: n={seg.height:5d}  次日晋级 {promote*100:5.1f}%  "
              f"次日均 {float(seg['f1'].mean())*100:+5.2f}%  20日均 {float(seg['f20'].mean())*100:+6.2f}%")

    # ---------------- 5. 各自自身回放 ----------------
    print("\n=== 5. 自身回放：处于当前状态时，未来 20 日 ===")
    replay = {}
    for s in syms:
        t = d.filter(pl.col("symbol") == s).sort("date")
        p = prof[s]
        sa = t.drop_nulls(["f_max_lu", "turn20", "ret20", "f20"])
        seg = sa.filter(
            (pl.col("turn20") < max(p["turn20_pct"] * 1.5, 0.6))
            & (pl.col("ret20") > p["ret20_pct"] / 100 - 0.10)
            & (pl.col("ret20") < p["ret20_pct"] / 100 + 0.10)
        )
        row = {
            "n": seg.height,
            "P_连板%": round(seg.filter(pl.col("f_max_lu") >= 2).height / max(seg.height, 1) * 100, 2),
            "P_单板%": round(seg.filter(pl.col("f_max_lu") >= 1).height / max(seg.height, 1) * 100, 2),
            "20日中位%": round(float(seg["f20"].median()) * 100, 2) if seg.height else None,
            "20日均%": round(float(seg["f20"].mean()) * 100, 2) if seg.height else None,
            "n_bars_total": t.height,
        }
        replay[s] = row
        print(f"  {s} (同类状态窗口): " + json.dumps(row, ensure_ascii=False))

    # ---------------- 6. 龙虎榜 ----------------
    print("\n=== 6. 龙虎榜（2025-11 ~ 2026-11-19）===")
    for s in syms:
        bare = s.split(".")[0]
        recs = []
        for f in sorted(glob.glob(str(ROOT / "data/dragon_tiger/date=*.json"))):
            try:
                j = json.loads(Path(f).read_text(encoding="utf-8"))
            except Exception:
                continue
            for k in ("all", "hot_money", "org"):
                for it in (j.get(k) or {}).get("stock_items") or []:
                    if str(it.get("ticker")) == bare:
                        recs.append({
                            "date": j.get("trade_date"), "board": k,
                            "change": it.get("change"),
                            "net": it.get("net_value"),
                            "hm_net": it.get("hot_money_net_value"),
                            "org_net": it.get("org_net_value"),
                            "reason": it.get("limit_reason"),
                        })
        uniq = sorted({(r["date"], r["board"]): r for r in recs}.values(), key=lambda x: x["date"])
        print(f"  {s}: 上榜 {len(uniq)} 次")
        for r in uniq[-6:]:
            print(f"    {r['date']} {r['board']:9s} chg={r['change']} "
                  f"net={r['net']:,.0f} hm={r['hm_net'] if r['hm_net'] is None else format(r['hm_net'], ',.0f')} "
                  f"org={r['org_net'] if r['org_net'] is None else format(r['org_net'], ',.0f')} {r['reason'] or ''}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe_compare.json").write_text(json.dumps({
        "profile": prof, "promote": rows, "replay": replay,
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n写出 {OUT/'probe_compare.json'}")


if __name__ == "__main__":
    main()
