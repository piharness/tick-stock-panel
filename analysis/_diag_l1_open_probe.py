"""L1 的价格那一半能不能变红：`open` 是不是就由 09:31 那根 bar 定义。

round5 §7 与预算文档把「09:25 竞价成交价 vs 日线 `open` 的偏差」写成本轮唯一
值得做的实验（R5-E / L1），判据是中位偏差 <=5bp 且 |p95| <=20bp。

这个判据有一个和「事件条件表负控恒无行」对称的失效模式：如果日线 `open`
本来就从同一上游的第一根分钟 bar 取，那么偏差恒为 0，判据**在什么条件下都
不会红**。所以先测一件事——两路是不是独立的。

本地数据恰好够测：`data/kline_minute` 的 21 个分区里有 20 个从 09:31 开始
（只有 2026-10-08 从 13:48 开始，那个分区整段弃用）。09:31 那根 bar 的
`open` 就是当日第一笔可成交价；把它和 `kline_daily_enriched.open` 逐 symbol
对一遍，得到偏差分布。

预注册：

    R-L1a 独立性。 若 |diff| <= 1e-9（逐位相同）的比例 >= 99%，则判据在本
          数据源上恒真，不能作为验收标准；此时唯一的合法写法是把它降级为
          「同一上游的自洽检查」。
    R-L1b 可测性。 若逐位相同的比例 < 99%，偏差分布即为 L1 价格的实测值，
          用 round5 的 |5bp| / |p95 20bp| 判它。
    R-L1c 成交量。 09:31 bar 的 volume 占全日 volume 的份额，是竞价+首分钟
          的吞吐上界；份额本身不是成交率，但缺口大于两个数量级时，L1 的
          「价格通过」不能推成「能成交」。

ASCII 输出（GBK 控制台）。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TOL = 1e-9


def main() -> None:
    mfiles = sorted(glob.glob(str(DATA / "kline_minute" / "date=*" / "*.parquet")))
    rows = []
    all_diff = []
    all_share = []
    for mf in mfiles:
        date = mf.split("date=")[1][:10]
        m = pl.read_parquet(mf, columns=["symbol", "datetime", "open", "close", "volume"])
        m = m.with_columns(pl.col("datetime").dt.time().alias("tm"))
        first_tm = m["tm"].min()
        if str(first_tm) >= "09:32:00":
            rows.append({"date": date, "skipped": f"first bar {first_tm}"})
            continue
        m31 = m.filter(pl.col("tm") == first_tm).select(
            "symbol",
            pl.col("open").alias("m_open"),
            pl.col("close").alias("m_close"),
            pl.col("volume").alias("m_vol"),
        )
        g = sorted(glob.glob(str(DATA / "kline_daily_enriched" / f"date={date}" / "*.parquet")))
        if not g:
            rows.append({"date": date, "skipped": "no daily partition"})
            continue
        d = pl.read_parquet(g[0], columns=["symbol", "open", "close", "volume"])
        j = m31.join(d, on="symbol", how="inner").filter(
            pl.col("open").is_not_null() & (pl.col("open") > 0)
            & pl.col("m_open").is_not_null() & (pl.col("m_open") > 0)
        )
        if j.height == 0:
            rows.append({"date": date, "skipped": "empty join"})
            continue
        mo = j["m_open"].to_numpy()
        do = j["open"].to_numpy()
        diff_bp = (mo / do - 1.0) * 1e4
        share = (j["m_vol"] / j["volume"]).to_numpy()
        share = share[np.isfinite(share) & (share > 0)]
        exact = float((np.abs(mo - do) <= TOL).mean())
        # 顺带问一句：日线 open 是不是 09:31 bar 的 close（另一种上游口径）
        exact_close = float((np.abs(j["m_close"].to_numpy() - do) <= TOL).mean())
        all_diff.append(diff_bp)
        all_share.append(share)
        rows.append({
            "date": date,
            "first_bar": str(first_tm),
            "n": int(j.height),
            "exact_open_share": round(exact, 4),
            "exact_close_share": round(exact_close, 4),
            "median_abs_bp": round(float(np.median(np.abs(diff_bp))), 3),
            "p95_abs_bp": round(float(np.percentile(np.abs(diff_bp), 95)), 3),
            "p05_bp": round(float(np.percentile(diff_bp, 5)), 3),
            "p95_bp": round(float(np.percentile(diff_bp, 95)), 3),
            "median_vol_share": round(float(np.median(share)), 4) if share.size else None,
        })
        print(f"{date}  n={j.height:5d}  first={first_tm}  "
              f"exact_open {exact*100:6.2f}%  exact_close {exact_close*100:6.2f}%  "
              f"|diff| med {np.median(np.abs(diff_bp)):7.2f}bp "
              f"p95 {np.percentile(np.abs(diff_bp),95):7.2f}bp  "
              f"vol share {np.median(share)*100 if share.size else float('nan'):5.2f}%")

    diff = np.concatenate(all_diff) if all_diff else np.array([])
    share = np.concatenate(all_share) if all_share else np.array([])
    exact_all = float((np.abs(diff) <= TOL).mean())
    out = {
        "levels": rows,
        "pooled": {
            "n_cells": int(diff.size),
            "exact_share": round(exact_all, 5),
            "median_abs_bp": round(float(np.median(np.abs(diff))), 3),
            "p95_abs_bp": round(float(np.percentile(np.abs(diff), 95)), 3),
            "p05_bp": round(float(np.percentile(diff, 5)), 3),
            "p95_bp": round(float(np.percentile(diff, 95)), 3),
            "frac_gt_5bp": round(float((np.abs(diff) > 5).mean()), 5),
            "frac_gt_20bp": round(float((np.abs(diff) > 20).mean()), 5),
            "median_vol_share": round(float(np.median(share)), 4) if share.size else None,
            "p10_vol_share": round(float(np.percentile(share, 10)), 4) if share.size else None,
        },
        "verdict": {},
    }
    p = out["pooled"]
    out["verdict"]["R-L1a_independent"] = bool(p["exact_share"] < 0.99)
    out["verdict"]["R-L1a_reading"] = (
        f"exact_share={p['exact_share']:.4f} -> "
        + ("两路独立，偏差分布可作判据" if p["exact_share"] < 0.99
           else "同一上游，判据恒真，必须降级为自洽检查"))
    out["verdict"]["R-L1b_pass_5bp_20bp"] = bool(
        p["median_abs_bp"] <= 5.0 and p["p95_abs_bp"] <= 20.0)
    out["verdict"]["R-L1c_vol_share"] = p["median_vol_share"]
    body = json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True)
    (Path("analysis") / "_diag_l1_open_probe_result.json").write_text(body, encoding="utf-8")
    print(f"\npooled n={p['n_cells']} exact {p['exact_share']*100:.4f}% "
          f"|diff| med {p['median_abs_bp']}bp p95 {p['p95_abs_bp']}bp "
          f"| >5bp {p['frac_gt_5bp']*100:.2f}% >20bp {p['frac_gt_20bp']*100:.2f}%")
    print(f"median vol share of first bar = {p['median_vol_share']}")
    print(f"R-L1a independent: {out['verdict']['R-L1a_independent']} -- "
          f"{out['verdict']['R-L1a_reading']}")
    print(f"R-L1b pass (med<=5 / p95<=20): {out['verdict']['R-L1b_pass_5bp_20bp']}")


if __name__ == "__main__":
    main()
