"""本地数据覆盖率与生存偏差报告 — 补完历史后必须跑一次。

回答三个问题(每一个都能证伪"全量下载成功"这句话):
  1. 逐年的交易日/标的/行数,标的数是否随时间单调增长(若不增长,说明有时间穿越)
  2. 每个 symbol 应有交易日 vs 实有交易日(应有 = listing_date → 末日之间的实际交易日)
     ⇒ 缺口 = 停牌,不是数据丢失;缺口为负才是数据丢失
  3. 哪些 symbol 在窗口中途消失(末个交易日 << 末日)⇒ 这批是退市/长停,
     而 fuyao 对已退市标的不提供服务(实测 Unknown thscode),所以它们是
     **结构性缺失**,任何跨窗口回测的收益都带正向生存偏差。

用法:
  backend/.venv/Scripts/python.exe analysis/_data_coverage_report.py
"""
from __future__ import annotations

import json
from datetime import date

import polars as pl

DATA = "data"
OUT = "data/research/coverage_report.json"


def main() -> int:
    daily = (
        pl.scan_parquet(f"{DATA}/kline_daily/**/*.parquet", extra_columns="ignore")
        .select(["date", "symbol"])
        .collect(engine="streaming")
    )
    inst = pl.read_parquet(f"{DATA}/instruments/instruments.parquet").select(
        ["symbol", "name", "listing_date", "exchange"]
    )

    all_days = sorted(daily["date"].unique().to_list())
    d_min, d_max = all_days[0], all_days[-1]
    print(f"日K交易日 {len(all_days)} 天, {d_min} ~ {d_max}, 行数 {daily.height:,}")

    # ---- 1. 逐年 ----
    by_year = (
        daily.with_columns(pl.col("date").dt.year().alias("year"))
        .group_by("year")
        .agg(
            pl.len().alias("rows"),
            pl.col("date").n_unique().alias("days"),
            pl.col("symbol").n_unique().alias("symbols"),
        )
        .sort("year")
    )
    print("\n年份  交易日  标的   行数")
    for r in by_year.iter_rows(named=True):
        print(f"{r['year']}  {r['days']:5d}  {r['symbols']:5d}  {r['rows']:>10,}")
    mono = by_year["symbols"].to_list() == sorted(by_year["symbols"].to_list())
    print(f"标的数逐年单调不减: {mono}")

    # ---- 2. symbol 级缺口 ----
    per_sym = (
        daily.group_by("symbol")
        .agg(pl.len().alias("rows"), pl.col("date").min().alias("first_d"), pl.col("date").max().alias("last_d"))
        .join(inst, on="symbol", how="left")
    )
    day_index = pl.DataFrame({"date": all_days}).with_row_index("idx")
    # 每只 symbol 的应有交易日数 = 交易日历中落在 [max(listing_date, d_min), min(last_d, d_max)] 的天数
    per_sym = per_sym.join(
        day_index.rename({"date": "first_d"}).select(["first_d", "idx"]), on="first_d", how="left"
    ).rename({"idx": "first_idx"}).join(
        day_index.rename({"date": "last_d"}).select(["last_d", "idx"]), on="last_d", how="left"
    ).rename({"idx": "last_idx"})
    per_sym = per_sym.with_columns((pl.col("last_idx") - pl.col("first_idx") + 1).alias("span_days"))
    per_sym = per_sym.with_columns((pl.col("span_days") - pl.col("rows")).alias("halt_days"))
    over = per_sym.filter(pl.col("halt_days") < 0)
    print(f"\nsymbol {per_sym.height} 只; 行数 > 跨度的(数据重复) {over.height} 只")
    if over.height:
        print(over.sort("halt_days").head(5).select(["symbol", "rows", "span_days", "halt_days"]))

    # ---- 3. 中途消失(结构性缺失的另一面)----
    vanished = per_sym.filter(pl.col("last_d") < d_max).sort("last_d")
    print(f"末个交易日 < {d_max} 的标的: {vanished.height} 只")
    print(vanished.tail(10).select(["symbol", "name", "first_d", "last_d", "rows"]))

    # ---- 4. 不在日K里的维表标的(退市/长停, fuyao 无服务)----
    missing = inst.join(per_sym.select("symbol"), on="symbol", how="anti").sort("listing_date")
    print(f"\n维表有、日K完全没有的标的: {missing.height} 只 ({100*missing.height/inst.height:.1f}%)")
    print(missing.head(10).select(["symbol", "name", "listing_date", "exchange"]))

    # ---- 5. 时点宇宙:每日实际交易数 vs 按 listing_date 应有数 ----
    listed_by_day = (
        inst.filter(pl.col("listing_date").is_not_null())
        .select(["symbol", pl.col("listing_date").str.to_date("%Y-%m-%d")])
        .join(pl.DataFrame({"date": all_days}), how="cross")
        .filter(pl.col("date") >= pl.col("listing_date"))
        .group_by("date")
        .agg(pl.col("symbol").n_unique().alias("n_listed"))
    )
    traded = daily.group_by("date").agg(pl.col("symbol").n_unique().alias("n_traded"))
    pit = traded.join(listed_by_day, on="date", how="left").with_columns(
        (pl.col("n_traded") / pl.col("n_listed")).alias("trade_ratio")
    ).sort("date")
    before_2010 = pit.filter(pl.col("date") < date(2010, 1, 1))
    after_2024 = pit.filter(pl.col("date") >= date(2024, 1, 1))
    print("\n时点宇宙(实际交易数 / 当日已上市数):")
    for label, sub in (("2005-2009", before_2010), ("2024+", after_2024)):
        if sub.height:
            r = sub.select(
                pl.col("n_traded").mean().alias("traded"),
                pl.col("n_listed").mean().alias("listed"),
                pl.col("trade_ratio").mean().alias("ratio"),
                pl.col("trade_ratio").min().alias("ratio_min"),
            ).row(0, named=True)
            print(f"  {label}: 交易 {r['traded']:.0f} / 已上市 {r['listed']:.0f} = {r['ratio']:.1%} (最低 {r['ratio_min']:.1%})")

    payload = {
        "generated_at": date.today().isoformat(),
        "daily": {
            "rows": daily.height,
            "days": len(all_days),
            "symbols": int(daily["symbol"].n_unique()),
            "d_min": str(d_min),
            "d_max": str(d_max),
            "by_year": by_year.to_dicts(),
            "symbols_monotonic": mono,
        },
        "universe": {
            "instruments": inst.height,
            "in_daily": per_sym.height,
            "never_in_daily": missing.height,
            "never_in_daily_pct": round(100 * missing.height / inst.height, 2),
            "vanished_before_end": vanished.height,
        },
        "integrity": {
            "duplicated_pairs": int(over.height),
            "missing_symbols": missing["symbol"].to_list(),
            "vanished": vanished.tail(50).select(["symbol", "last_d"]).to_dicts(),
        },
        "pit_universe": {
            "2005_2009_mean_ratio": round(float(before_2010["trade_ratio"].mean() or 0), 4),
            "2024plus_mean_ratio": round(float(after_2024["trade_ratio"].mean() or 0), 4),
        },
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    print(f"\n报告已写 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
