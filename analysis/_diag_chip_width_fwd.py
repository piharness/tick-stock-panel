"""日内成本带宽度有没有独立于「换手/振幅」的信息 —— 前瞻检验。

判据（预注册，先写死再看数）
----------------------------
H0（熵只是换手的重命名）：在 turnover 分位内，ivd 分组对未来 20 日超额无差异。
H1（宽度有独立投影）：同一 turnover 分位内，低 ivd 组显著优于高 ivd 组。
   —— 因为「在窄价格带里完成大量换手」= 成本重置；「宽价格带里完成同样换手」= 派发。

三个检验（全部按日聚合求 t，不并列计数）：
  A 单变量：ivd 五分位 → fwd20 超额
  B 控制宽度：amp 五分位内 → ivd 三分位
  C 控制换手：turnover 五分位内 → ivd 三分位
  D 交叉：turnover 高组内，ivd 低 vs 高（理论的核心格）

口径：
  - ivd 用 lo/hi 两个界各跑一遍；结论必须在两端同向，否则记为「依赖 bar 内形状假设」。
  - 剔除当日 |ret|>9.5%（涨停/跌停封板使 ivd 机械趋零，会把「制度」读成「熵」）。
  - fwd20 = close[didx+21]/open[didx+1] − 1（T+1 开盘进，20 个交易日后收盘出）。
  - 超额 = 个股 fwd20 − 当日样本等权均值（900 只抽样池，非全市场）。

用法:
  python analysis/_diag_chip_width_fwd.py
输出:
  data/.scratch/chip_width_fwd.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
ENT = ROOT / "data" / ".scratch" / "chip_entropy" / "part.parquet"
KL = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "chip_width_fwd.json"
H = 20
MIN_DAY_N = 200


def build(ent_path: Path = ENT) -> pl.DataFrame:
    ent = pl.read_parquet(ent_path)
    files = [str(p) for p in sorted(KL.glob("date=*/part.parquet"))]
    py = pl.scan_parquet(files, extra_columns="ignore").select(
        ["symbol", "date", "open", "close", "high", "low", "raw_close",
         "turnover_rate", "amount"]
    ).collect()
    py = py.join(ent, on=["symbol", "date"], how="inner")
    cal = py.select("date").unique().sort("date").with_row_index("didx")
    py = py.join(cal, on="date", how="left").sort(["symbol", "didx"])

    py = py.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("raw_close")).alias("amp"),
    ).with_columns(
        # 量比：当日额 / 20 日均额（用全局日历对齐后按 symbol 滚动）
        (pl.col("amount") / pl.col("amount").rolling_mean(20).over("symbol")).alias("vr20"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("to20"),
        (pl.col("raw_close") / pl.col("raw_close").shift(20).over("symbol") - 1).alias("ret20"),
    )

    # 前瞻：entry = didx+1 开盘；exit = didx+21 收盘
    ent_px = py.select(["symbol", "didx", "open"]).with_columns(
        (pl.col("didx") - 1).alias("didx")).rename({"open": "entry"})
    ext_px = py.select(["symbol", "didx", "close"]).with_columns(
        (pl.col("didx") - H - 1).alias("didx")).rename({"close": "exit"})
    py = py.join(ent_px, on=["symbol", "didx"], how="left").join(
        ext_px, on=["symbol", "didx"], how="left")
    py = py.with_columns(
        (pl.col("exit") / pl.col("entry") - 1).alias("fwd20"),
        (pl.col("raw_close") / pl.col("raw_close").shift(1).over("symbol") - 1).alias("ret1"),
    )
    py = py.with_columns((pl.col("fwd20") - pl.col("fwd20").mean().over("date")).alias("exc"))
    return py.filter(
        pl.col("fwd20").is_not_null() & pl.col("amp").is_not_null()
        & (pl.col("amp") > 0) & (pl.col("turnover_rate") > 0)
        & (pl.col("ret1").abs() < 0.095)      # 剔涨跌停封板
        & (pl.col("ivd_lo") > 0) & (pl.col("ivd_lo") < 0.15)
        & (pl.col("ivd_hi") < 0.20)
    )


def q(d: pl.DataFrame, col: str, k: int) -> pl.Expr:
    r = pl.col(col).rank(method="ordinal").over("date")
    n = pl.col(col).count().over("date")
    return (((r - 1) * k / n).floor().clip(0, k - 1) + 1).cast(pl.Int32)


def stat(d: pl.DataFrame, keys: list[str]) -> list[dict]:
    daily = (d.group_by(["date"] + keys)
             .agg(pl.col("exc").mean().alias("m"), pl.len().alias("n"))
             .filter(pl.col("n") >= 3))
    out = []
    for kv, sub in daily.group_by(keys):
        s = sub["m"]
        if s.len() < MIN_DAY_N // 5:
            continue
        mu, sd = float(s.mean()), float(s.std())
        out.append({
            "k": kv if not isinstance(kv, tuple) else list(kv),
            "days": s.len(),
            "n": int(sub["n"].sum()),
            "bp": round(mu * 1e4, 1),
            "t": round(mu / (sd / s.len() ** 0.5), 2) if sd > 0 else None,
        })
    return sorted(out, key=lambda x: str(x["k"]))


def spread(d: pl.DataFrame, outer: str, inner: str, ko: int, ki: int,
           pick: tuple[int, int]) -> dict:
    d = d.with_columns(q(d, outer, ko).alias("o"), q(d, inner, ki).alias("i"))
    daily = (d.group_by(["date", "o", "i"])
             .agg(pl.col("exc").mean().alias("m")).filter(pl.col("m").is_not_null()))
    lo = daily.filter((pl.col("o") == pick[0]) & (pl.col("i") == pick[1]))
    hi = daily.filter((pl.col("o") == pick[0]) & (pl.col("i") == pick[2]))
    a = dict(zip(lo["date"].to_list(), lo["m"].to_list()))
    b = dict(zip(hi["date"].to_list(), hi["m"].to_list()))
    common = sorted(set(a) & set(b))
    diff = [a[k] - b[k] for k in common]
    n = len(diff)
    if n < 30:
        return {"n_days": n, "note": "配对日不足"}
    mu = sum(diff) / n
    sd = (sum((x - mu) ** 2 for x in diff) / (n - 1)) ** 0.5
    return {
        "outer_group": pick[0], "inner_low": pick[1], "inner_high": pick[2],
        "n_days": n,
        "diff_bp": round(mu * 1e4, 1),
        "t": round(mu / (sd / n ** 0.5), 2) if sd > 0 else None,
        "hit": round(sum(1 for x in diff if x > 0) / n, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ent", default=str(ENT))
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    d = build(Path(a.ent))
    print(f"样本 {d.height:,} 股票-日，{d['symbol'].n_unique()} 只，"
          f"{d['date'].min()} → {d['date'].max()}")
    rep: dict = {"n": d.height, "symbols": d["symbol"].n_unique(),
                 "period": [str(d["date"].min()), str(d["date"].max())]}

    d = d.with_columns(
        q(d, "ivd_lo", 5).alias("q_lo"), q(d, "ivd_hi", 5).alias("q_hi"),
    )
    rep["A_univariate_ivd_lo"] = stat(d.rename({"q_lo": "g"}), ["g"])
    rep["A_univariate_ivd_hi"] = stat(d.rename({"q_hi": "g"}), ["g"])
    print("\n=== A 单变量 ivd_lo 五分位（Q1=最窄成本带）===")
    for r in rep["A_univariate_ivd_lo"]:
        print(f"  {r['k']}: {r['bp']:+.1f}bp t={r['t']} n={r['n']}")
    print("=== A 单变量 ivd_hi 五分位 ===")
    for r in rep["A_univariate_ivd_hi"]:
        print(f"  {r['k']}: {r['bp']:+.1f}bp t={r['t']} n={r['n']}")

    for tag, ivd in [("lo", "ivd_lo"), ("hi", "ivd_hi")]:
        for outer in ["amp", "turnover_rate", "vr20"]:
            d2 = d.with_columns(q(d, outer, 5).alias("o"), q(d, ivd, 3).alias("i"))
            rep[f"B_{outer}_x_{ivd}_{tag}"] = stat(d2, ["o", "i"])
            # 最高档 outer 内 ivd 低 vs 高
            rep[f"D_{outer}_top_x_{ivd}_{tag}"] = spread(d, outer, ivd, 5, 3, (5, 1, 3))
            rep[f"D_{outer}_bot_x_{ivd}_{tag}"] = spread(d, outer, ivd, 5, 3, (1, 1, 3))

    for k in rep:
        if k.startswith("D_"):
            print(f"\n{k}: {json.dumps(rep[k], ensure_ascii=False)}")

    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
