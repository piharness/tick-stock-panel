"""L1 逐年净收益 —— 成本不是常数，19bp 只是 2023-08-28 之后的往返成本。

## 为什么必须单列

`docs/decision-target-prereg.md` §4 把经济门槛定在 **19bp/往返**
（佣金 2bp×2 + 滑点 5bp×2 + 卖出印花 5bp）。作为**当前**的准入门槛，这个数
没错：决策问的是「现在做不做」。但拿它去扣 2005-2026 的毛收益，等于用 2026
年的税率结算 2005 年的交易。

## 印花税的实际路径（多源一致：财政部/央视/基金年报）

| 期间 | 税率 | 征收 | 往返印花 |
|---|---|---|---|
| 2005-01-24 → 2007-05-29 | 0.1% | 双边 | 20bp |
| **2007-05-30 → 2008-04-23** | **0.3%** | **双边** | **60bp** |
| 2008-04-24 → 2008-09-18 | 0.1% | 双边 | 20bp |
| 2008-09-19 → 2023-08-27 | 0.1% | 单边 | 10bp |
| 2023-08-28 起 | 0.05% | 单边 | 5bp |

2007-05-30 的「5·30」上调是 1997 年以来唯一一次上调，只维持了 11 个月。
这一段正好落在 L1 样本的第二年（2007 gross +82.6bp）。

## 结论（预先说明）

- 全期净收益从「统一扣 19bp」的 77.0bp 降到 **约 67bp**；
- **2005 年净只剩 +6.7bp**（gross 40.7 − 34），是 22 年里最薄的一年；
- 2007 年从 82.6 掉到 **+25.3bp**（−69%）；
- 没有年份翻负 —— 但「22 年每年为正」这句话的真实强度比 gross 表弱得多。

**本脚本不改预注册的 19bp。** 它只回答「如果按当年的税率结算，历史净收益
是多少」，供 §5.3 引用；要改门槛口径得走预注册自己的签字流程。

用法：

    python analysis/_diag_l1_net_cost.py            # 只打印
    python analysis/_diag_l1_net_cost.py --write    # 写 _result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort as l1  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_l1_net_cost_result.json"
ANCHOR_END = "2026-11-06"

#: 往返印花税（bp），按入场日的自然日区间查表。
STAMP_PERIODS = (
    ("2005-01-24", "2007-05-29", 20.0),
    ("2007-05-30", "2008-04-23", 60.0),
    ("2008-04-24", "2008-09-18", 20.0),
    ("2008-09-19", "2023-08-27", 10.0),
    ("2023-08-28", "2099-12-31", 5.0),
)
#: 与预注册 §4 的 19bp 同源：佣金 2bp×2 + 滑点 5bp×2 = 14bp，印花另计。
COMMISSION_AND_SLIP_BP = 14.0
#: 预注册用的常数往返成本（仅用于对照）。
FLAT_COST_BP = 19.0


def stamp_bp(day: str) -> float:
    for lo, hi, bp in STAMP_PERIODS:
        if lo <= day <= hi:
            return bp
    return 5.0


def build() -> dict:
    cells = l1.cohort_cells(DATA, end=ANCHOR_END)
    df, m = l1._panel(DATA)
    ix = {d: i for i, d in enumerate(m.dates)}
    sx = {s: j for j, s in enumerate(m.symbols)}
    rec = []
    for d, s in zip(cells["date"].to_list(), cells["symbol"].to_list()):
        i, j = ix.get(d), sx.get(s)
        if i is None or j is None:
            continue
        o, c = m.open[i, j], m.close[i, j]
        if np.isfinite(o) and o > 0 and np.isfinite(c):
            rec.append((d, c / o - 1.0))
    r = pl.DataFrame(rec, schema=["date", "ret"], orient="row").with_columns(
        pl.col("date").str.slice(0, 4).alias("y")
    )
    per = r.group_by("date").agg(pl.col("ret").mean().alias("r")).with_columns(
        pl.col("date").str.slice(0, 4).alias("y")
    )

    rows: dict[str, dict] = {}
    for y in sorted(set(per["y"].to_list())):
        sub = per.filter(pl.col("y") == y)
        gross = float(sub["r"].mean()) * 1e4
        days = sub["date"].to_list()
        tax = float(np.mean([stamp_bp(d) for d in days]))
        cost = COMMISSION_AND_SLIP_BP + tax
        rows[y] = {
            "gross_bp": round(gross, 1),
            "stamp_bp": round(tax, 1),
            "cost_bp": round(cost, 1),
            "net_bp": round(gross - cost, 1),
            "net_flat19_bp": round(gross - FLAT_COST_BP, 1),
            "bias_bp": round((gross - cost) - (gross - FLAT_COST_BP), 1),
        }

    mean_gross = float(np.mean([v["gross_bp"] for v in rows.values()]))
    mean_net = float(np.mean([v["net_bp"] for v in rows.values()]))
    mean_flat = float(np.mean([v["net_flat19_bp"] for v in rows.values()]))
    return {
        "generated_at": "2026-11-15",
        "anchor_end": ANCHOR_END,
        "stamp_periods": [{"from": a, "to": b, "round_trip_bp": c} for a, b, c in STAMP_PERIODS],
        "flat_cost_bp": FLAT_COST_BP,
        "by_year": rows,
        "summary": {
            "mean_gross_bp": round(mean_gross, 1),
            "mean_net_by_year_bp": round(mean_net, 1),
            "mean_net_flat19_bp": round(mean_flat, 1),
            "understatement_bp": round(mean_flat - mean_net, 1),
            "years_negative_by_year": [y for y, v in rows.items() if v["net_bp"] <= 0],
            "years_negative_flat": [y for y, v in rows.items() if v["net_flat19_bp"] <= 0],
            "thinnest_year": min(rows.items(), key=lambda kv: kv[1]["net_bp"])[0],
            "thinnest_net_bp": round(min(v["net_bp"] for v in rows.values()), 1),
        },
        "note": (
            "预注册的 19bp 是**当前**准入门槛，不改；本表只给历史净收益。"
            "两者口径不同：门槛回答「现在值不值得做」，本表回答「当年实际赚了多少」。"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    data = build()
    print("年   gross   印花   成本    净(逐年)   净(统一19bp)  偏差")
    for y, v in data["by_year"].items():
        print("%s %+7.1f %6.1f %6.1f  %+8.1f  %+10.1f  %+7.1f"
              % (y, v["gross_bp"], v["stamp_bp"], v["cost_bp"],
                 v["net_bp"], v["net_flat19_bp"], v["bias_bp"]))
    s = data["summary"]
    print("\n逐年均值 gross %+.1f  净(逐年成本) %+.1f  净(统一19bp) %+.1f"
          % (s["mean_gross_bp"], s["mean_net_by_year_bp"], s["mean_net_flat19_bp"]))
    print("统一 19bp 高估历史净收益 %+.1fbp；翻负年份（逐年）%s；最薄 %s %+.1fbp"
          % (s["understatement_bp"], s["years_negative_by_year"] or "无",
             s["thinnest_year"], s["thinnest_net_bp"]))
    if args.write:
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print("written:", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
