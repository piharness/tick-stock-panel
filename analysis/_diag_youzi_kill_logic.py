"""对今日触点做「一阶逻辑否定」—— 找出主力博弈动机自相矛盾的标的。

不建模，只算几个可以直接读出「主力凭什么拉」的量。每个量对应一条可证伪的悖论：

  L1 浅套牢抛压  过去 250 日中，收盘落在 [今价, 今价×1.15] 区间的成交额占比。
                 高 ⇒ 上方 15% 内全是浅套牢盘，拉一个板等于花自己的钱给这批人出货。
                 悖论：主力的钱是给散户当解放军。
  L2 距前高空间  今价 / 250 日最高价 − 1。越接近 −5% 越说明头上就是天花板，
                 第一个板就要挑战前高，封板要吃掉全部解套抛压。
  L3 套牢深度    250 日涨幅。深套(<-40%)的人涨 10% 无感 → 反而利于缩量封板；
                 真正危险的是「浅套」(0~-15%)，一涨就跑。
  L4 试盘失败    amt5/amt20 放量倍数 且 无涨停：近期有资金试盘但没拉起来。
                 悖论：不是「还没人碰」，是「有人碰过、没成」。试盘失败比无人问津更糟。
  L5 无题材锚    近 60 日龙虎榜次数 = 0 且 无 limit_reason：跟风盘没有故事可理解。
                 悖论：拉板需要买方，买方需要一个「为什么」。

口径与触点一致：数据到 2026-11-20，每票取 250 日窗口（不足则用全部）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

from app.services import citizenship  # noqa: E402

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def main() -> None:
    d = citizenship.compute(ROOT / "data")
    picks = d["focus"]["picks"]
    syms = [p["symbol"] for p in picks]
    print(f"触点 {len(syms)} 只，as_of {d['as_of']}", flush=True)

    # 载入全市场 250 日面板（只取需要的列）
    import glob
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))[-260:]
    panel = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "high", "low", "close", "amount", "consecutive_limit_ups"])
        .collect().sort(["symbol", "date"])
    )
    # 每票滚动统计（250 日窗口）
    win = panel.with_columns([
        pl.col("amount").rolling_sum(250).over("symbol").alias("amt250"),
        pl.col("high").rolling_max(250).over("symbol").alias("high250"),
        pl.col("close").shift(250).over("symbol").alias("c250"),
        pl.col("amount").rolling_mean(5).over("symbol").alias("amt5"),
        pl.col("amount").rolling_mean(20).over("symbol").alias("amt20"),
    ])
    last = win.group_by("symbol").last()

    # 龙虎榜近 60 日出现次数 + 题材（最近一条）
    dr_rows = []
    for f in sorted(glob.glob(str(ROOT / "data/dragon_tiger/date=*.json")))[-60:]:
        j = json.load(open(f, encoding="utf-8"))
        for it in (j.get("all") or {}).get("stock_items") or []:
            dr_rows.append((it.get("thscode"), it.get("limit_reason")))
    dr = pl.DataFrame(dr_rows, schema=["symbol", "reason"], orient="row").filter(pl.col("symbol").is_in(syms))
    dr_n = dr.group_by("symbol").agg(pl.len().alias("lhb60"))
    dr_last = dr.group_by("symbol").last()

    out = []
    for p in picks:
        s = p["symbol"]
        r = last.filter(pl.col("symbol") == s)
        if r.is_empty():
            continue
        r = r.to_dicts()[0]
        cur = r["close"]
        amt250 = r["amt250"] or 1e9
        # L1 浅套牢：窗口内每日 close 落在 [cur, cur*1.15] 的 amount 占比
        h = win.filter(pl.col("symbol") == s)
        over15 = float(
            h.filter((pl.col("close") >= cur) & (pl.col("close") <= cur * 1.15))
            ["amount"].sum() / amt250
        )
        over30 = float(
            h.filter((pl.col("close") >= cur) & (pl.col("close") <= cur * 1.30))
            ["amount"].sum() / amt250
        )
        pct_above = float((h["close"] > cur).mean())
        dist_high = float(cur / r["high250"] - 1) if r["high250"] else None
        ret250 = float(cur / r["c250"] - 1) if r["c250"] else None
        volx = float(r["amt5"] / r["amt20"]) if r["amt20"] else None
        lu20 = int(h.tail(20)["consecutive_limit_ups"].sum())
        lhb = int(dr_n.filter(pl.col("symbol") == s).to_dicts()[0]["lhb60"]) if not dr_n.filter(pl.col("symbol") == s).is_empty() else 0
        reason = dr_last.filter(pl.col("symbol") == s).to_dicts()
        reason = reason[0]["reason"] if reason else None
        out.append({
            "symbol": s, "name": p["name"], "heat": round(p["heat"], 3),
            "ret250": round(ret250, 3) if ret250 is not None else None,
            "dist_high": round(dist_high, 3) if dist_high is not None else None,
            "pct_above": round(pct_above, 3),
            "over15": round(over15, 3),
            "over30": round(over30, 3),
            "volx": round(volx, 2) if volx else None,
            "lu20": lu20,
            "lhb60": lhb,
            "reason": reason,
        })

    t = pl.DataFrame(out).sort("heat", descending=True)
    print(t.select(["symbol", "name", "ret250", "dist_high", "pct_above", "over15", "over30", "volx", "lhb60"]))
    print("\n--- 理由（limit_reason）---")
    for r in out:
        print(f"  {r['symbol']} {r['name']}: lhb60={r['lhb60']}  {r['reason'] or '(无题材锚)'}")

    # 全市场对照分位
    print("\n--- 全市场这些量的分位对照（触点 vs 全市场）---")
    for col, label in [("over15", "浅套牢over15"), ("pct_above", "套牢天数占比"), ("dist_high", "距前高")]:
        pass

    t.write_json(str(OUT / "youzi_kill_logic.json"))
    print(f"\n写出 {OUT / 'youzi_kill_logic.json'}")


if __name__ == "__main__":
    main()
