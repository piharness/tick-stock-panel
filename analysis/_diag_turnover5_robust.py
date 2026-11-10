"""对「换手率 ≥ 25% → 5 日跑输」这一条唯一存活的信号做失效测试。

它已经是全部实验里最后一个活着的候选，所以必须用前面杀掉其他候选的同一套刀：
分半段 / 月度符号 / 剔除最好 5 天 / 阈值敏感性 / 持有期敏感性 /
换涨幅箱口径 vs 全市场口径 / 板块拆分 / 与涨停的重叠 / 波动率对照。

输出：analysis/_diag_turnover5_robust_result.json
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).with_name("_diag_turnover5_robust_result.json")

import importlib.util

spec = importlib.util.spec_from_file_location(
    "s2p", Path(__file__).with_name("_diag_signal_to_product.py")
)
s2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2p)


def daily_series(df: pl.DataFrame, cond: str, col: str) -> pl.DataFrame:
    return (
        df.filter(pl.col(cond))
        .select(["date", col])
        .drop_nulls()
        .group_by("date")
        .agg(pl.col(col).mean().alias("v"), pl.len().alias("k"))
        .sort("date")
    )


def stats(s: pl.DataFrame, h: int) -> dict:
    x = s["v"]
    n = len(x)
    if n < 5:
        return {"days": n}
    neff = n / h
    se = x.std() / math.sqrt(neff)
    m = x.mean()
    return {
        "days": n,
        "mean_bp": round(m * 1e4, 1),
        "t": round(m / se, 2) if se > 0 else None,
        "mde_bp": round(2 * se * 1e4, 1),
        "pct_pos_days": round((x > 0).mean(), 3),
    }


def main() -> None:
    raw = s2p.load_daily()
    df = s2p.add_excess(s2p.build(raw))
    df = df.with_columns(
        (pl.col("turnover_rate") >= 25).alias("t_tr25"),
        ((pl.col("turnover_rate") >= 25) & (pl.col("consecutive_limit_ups") >= 1)).alias("t_tr25_limit"),
        ((pl.col("turnover_rate") >= 25) & (pl.col("consecutive_limit_ups") == 0)).alias(
            "t_tr25_nolimit"
        ),
        pl.col("symbol").str.slice(0, 3).alias("pfx"),
    )
    # 补 H3 / H10：主脚本只算了 1/2/5
    for h in (3, 10):
        df = df.with_columns(
            (pl.col("close").shift(-h).over("symbol") / pl.col("next_open") - 1).alias(f"r{h}")
        )
        df = df.with_columns(
            (pl.col("r{h}") - pl.col(f"m{{{h}}}").fill_null(0)) if False else pl.col(f"r{h}")
        )
        mkt = df.group_by("date").agg(pl.col(f"r{h}").mean().alias(f"m{h}"))
        df = df.join(mkt, on="date", how="left")
        df = df.with_columns((pl.col(f"r{h}") - pl.col(f"m{h}")).alias(f"x{h}"))
        bm = df.group_by(["date", "bucket"]).agg(
            pl.col(f"r{h}").mean().alias(f"bm{h}"), pl.len().alias(f"bn{h}")
        )
        df = df.join(bm, on=["date", "bucket"], how="left")
        df = df.with_columns(
            pl.when(pl.col(f"bn{h}") >= 5)
            .then(pl.col(f"r{h}") - pl.col(f"bm{h}"))
            .otherwise(None)
            .alias(f"c{h}")
        )
    res: dict = {"base": {}, "split": {}, "monthly": {}, "drop_best": {}, "threshold": {}, "board": {}, "vol_control": {}}

    base = daily_series(df, "t_tr25", "c5")
    res["base"]["H5_bucket"] = stats(base, 5)
    res["base"]["H5_market"] = stats(daily_series(df, "t_tr25", "x5"), 5)
    for h in (1, 2, 3, 5, 10):
        col = f"c{h}" if f"c{h}" in df.columns else None
        if col:
            res["base"][f"H{h}_bucket"] = stats(daily_series(df, "t_tr25", col), h)

    # 分半段
    dts = base["date"].to_list()
    mid = dts[len(dts) // 2]
    res["split"] = {
        "mid": str(mid),
        "first_half": stats(base.filter(pl.col("date") < mid), 5),
        "second_half": stats(base.filter(pl.col("date") >= mid), 5),
    }

    # 月度符号
    mo = (
        base.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m"))
        .group_by("m")
        .agg(pl.col("v").mean().alias("mv"), pl.len().alias("k"))
        .sort("m")
    )
    res["monthly"] = {
        "n_months": mo.height,
        "neg_months": int((mo["mv"] < 0).sum()),
        "detail": [
            {"m": r["m"], "mean_bp": round(r["mv"] * 1e4, 1), "days": r["k"]}
            for r in mo.iter_rows(named=True)
        ],
    }

    # 剔除最好 5 天（对「持有跑输」而言，最好 = 最正）
    srt = base.sort("v", descending=True)
    res["drop_best"] = {
        "drop5": stats(srt.tail(srt.height - 5), 5),
        "drop10": stats(srt.tail(srt.height - 10), 5),
        "best5_sum_bp": round(srt["v"].head(5).sum() * 1e4, 1),
        "total_days": base.height,
    }

    # 阈值敏感性
    for thr in (15, 20, 22, 25, 30, 40):
        cond = (pl.col("turnover_rate") >= thr).alias("cond")
        d2 = df.with_columns(cond)
        s = daily_series(d2, "cond", "c5")
        res["threshold"][f">={thr}"] = {**stats(s, 5), "events": int(d2.filter(pl.col("cond")).height)}

    # 板块拆分
    for name, pref in (("创业板/科创板20cm", ("300", "301", "302", "688", "689")), ("主板10cm", ("000", "001", "002", "003", "600", "601", "603", "605", "920"))):
        d3 = df.with_columns(pl.col("pfx").is_in(pref).alias("p"))
        s = daily_series(d3.filter(pl.col("p")), "t_tr25", "c5")
        res["board"][name] = stats(s, 5)

    # 涨停 vs 非涨停 拆分（换手高的效应是不是只在涨停股上）
    res["board"]["换手≥25%且非涨停"] = stats(daily_series(df, "t_tr25_nolimit", "c5"), 5)

    # 波动率对照：换手高的股票波动大，会不会只是「高波动 → 反转」
    df2 = df.with_columns(
        pl.col("pct").abs().rolling_mean(20).over("symbol").alias("amp20")
    )
    # 同日内按 amp20 十等分，看换手≥25% 是否只在最高波动档
    df3 = df2.with_columns(
        (pl.col("amp20").rank("ordinal").over("date") / pl.len().over("date") * 10)
        .ceil()
        .alias("amp_dec")
    )
    vol_rows = {}
    for dec in range(1, 11):
        s = daily_series(df3.filter(pl.col("amp_dec") == dec), "t_tr25", "c5")
        vol_rows[f"dec{dec}"] = stats(s, 5)
    res["vol_control"] = vol_rows

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    def fmt(d: dict) -> str:
        if not d.get("days") or "mean_bp" not in d:
            return "样本不足"
        return f"{d['mean_bp']:>8.1f}bp t={d['t']:>6} days={d['days']:>4} MDE={d.get('mde_bp','-')}"

    print("基线（换手≥25%，同涨幅箱口径）")
    for k in ("H1_bucket", "H2_bucket", "H3_bucket", "H5_bucket", "H10_bucket", "H5_market"):
        if k in res["base"]:
            print(f"  {k:<12}{fmt(res['base'][k])}")
    print(f"\n分半段 (mid={res['split']['mid']})")
    print(f"  前半{fmt(res['split']['first_half'])}")
    print(f"  后半{fmt(res['split']['second_half'])}")
    print(f"\n月度：{res['monthly']['neg_months']}/{res['monthly']['n_months']} 个月为负")
    print(f"\n剔除最好 5 天：{fmt(res['drop_best']['drop5'])}  (最好5天合计 {res['drop_best']['best5_sum_bp']}bp)")
    print(f"剔除最好10 天：{fmt(res['drop_best']['drop10'])}")
    print("\n阈值敏感性")
    for k, v in res["threshold"].items():
        print(f"  {k:<6}{fmt(v)} events={v.get('events')}")
    print("\n板块")
    for k, v in res["board"].items():
        print(f"  {k:<20}{fmt(v)}")
    print("\n波动率十分位（1=最安静 10=最躁动）")
    for k, v in res["vol_control"].items():
        print(f"  {k:<8}{fmt(v)}")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
