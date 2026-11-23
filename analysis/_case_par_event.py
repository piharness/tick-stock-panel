"""破面值事件的后果 —— *ST华幸（600340.SH）当前所处位置的同类历史。

## 为什么用「首次收盘 < 1 元」而不是「同行业 + 相似股东结构」

行业分类（`data/ext_data/ext_hy_ths`）只覆盖**当前在册**的 5581 只；283 只已停止
交易的样本里房地产行业一行都没有。按"相似十大流通股东"筛更糟：top_float 只有
最近 8 期（2024-12 起），而退市股的股东数据根本没拉过。用这两个维度找"可比样本"，
得到的必然是**幸存者**——那正是最不能用来推断退市风险的一批。

破面值是个不需要行业、不需要股东数据的事件定义，且**它正是华幸今天的处境**
（2026-11-17 收 0.92，连续低于面值第 1 个交易日）。面值退市规则要求连续 20 个
交易日收盘低于 1 元，所以"事件后 20 日"是可判定的窗口。

## 样本

  主表 `kline_daily_enriched`  5573 只全部存活（100% 生存偏差，单用会严重高估反弹概率）
  gap 包 `backfill_20261117/stock_gap_daily`  306 只，其中 283 只已停止交易

两者合并才是无偏样本。价格口径统一用**不复权原始收盘**（主表 `raw_close`，
gap 包 `close`）—— 面值看的是真实报价，前复权价会把破面值这件事抹掉。

## 局限（必须随结论一起说）

  - gap 包可能漏掉部分退市股，也可能把长期停牌算成"停止交易"；
  - 2018 年以前面值退市几乎没有实际执行，样本起点定在 2018；
  - 一只票可能多次破面值，只取**首次**（华幸现在也是首次）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
START = "2018-01-01"
PAR = 1.0
HORIZONS = (5, 10, 20, 60)


def load_prices() -> pl.DataFrame:
    """统一口径的日收盘（原始价），主表 + 补全包。"""
    lf = pl.scan_parquet(
        str(DATA / "kline_daily_enriched" / "date=*" / "*.parquet"),
        extra_columns="ignore",
    )
    main = lf.select(
        ["symbol", "date", "close", "high", "low", "raw_close", "volume"]
    ).collect()
    main = main.with_columns(
        pl.coalesce(["raw_close", "close"]).alias("px")
    ).select(["symbol", "date", "px", "high", "low", "volume", "raw_close", "close"])

    gap = pl.read_parquet(DATA / "backfill_20261117" / "stock_gap_daily.parquet")
    gap = gap.select(
        ["symbol", "date", "close", "high", "low", "volume"]
    ).with_columns(pl.col("close").alias("px")).select(
        ["symbol", "date", "px", "high", "low", "volume"]
    )
    gap = gap.with_columns(pl.lit(None, dtype=pl.Float64).alias("raw_close"),
                           pl.col("px").alias("close"))

    df = pl.concat([main, gap], how="vertical_relaxed")
    df = df.filter(pl.col("date") >= pl.lit(START).str.to_date())
    # 去重：主表优先（它有 raw_close 真值）
    df = df.sort(["symbol", "date", "raw_close"], nulls_last=True).unique(
        subset=["symbol", "date"], keep="first"
    ).sort(["symbol", "date"])
    return df


def find_events(df: pl.DataFrame) -> pl.DataFrame:
    """每只票**首次**收盘 < 1 元的日期。若从未发生则无事件。"""
    below = df.filter((pl.col("px") > 0) & (pl.col("px") < PAR))
    if below.is_empty():
        return pl.DataFrame()
    first = below.group_by("symbol").agg(pl.col("date").min().alias("ev_date"))
    # 排除"上市首日就在 1 元以下"的异常（B 股/老股改），要求事件前至少 60 个交易日
    cnt = df.group_by("symbol").agg(pl.len().alias("n_all"))
    first = first.join(cnt, on="symbol")
    return first.filter(pl.col("n_all") >= 60)


def path_after(df: pl.DataFrame, ev: pl.DataFrame) -> pl.DataFrame:
    """事件日后各 horizon 的价格路径。严格按交易日序号（不数自然日）。"""
    d = df.join(ev, on="symbol", how="inner").sort(["symbol", "date"])
    d = d.with_columns(
        pl.col("date").rank("ordinal").over("symbol").cast(pl.Int32).alias("rn")
    )
    ev_rn = d.join(
        ev, on="symbol", how="inner"
    ).filter(pl.col("date") == pl.col("ev_date")).select(
        ["symbol", pl.col("rn").alias("ev_rn")]
    )
    d = d.join(ev_rn, on="symbol", how="left")
    d = d.with_columns((pl.col("rn") - pl.col("ev_rn")).alias("k"))

    base = d.filter(pl.col("k") == 0).select(
        ["symbol", pl.col("px").alias("px0"), pl.col("date").alias("ev_date")]
    )
    out = base
    # **停止交易必须当成结局，不能当缺失**：drop_nulls 会把它悄悄剔除，
    # 于是"60 日后收益 +28%"这种数字就出来了——那只是活下来的 70 只。
    last = d.group_by("symbol").agg(
        pl.col("k").max().alias("last_k"),
        pl.col("px").sort_by("k").last().alias("px_end"),
        pl.col("date").max().alias("last_date"),
    )
    out = out.join(last, on="symbol", how="left")
    for h in HORIZONS:
        # 第 h 个交易日仍在交易 → 取当日价；否则取最后成交价并标记 delisted
        lastp = d.filter(pl.col("k") == h).select(
            ["symbol", pl.col("px").alias(f"px{h}")]
        )
        out = out.join(lastp, on="symbol", how="left")
        out = out.with_columns(
            pl.when(pl.col(f"px{h}").is_not_null())
            .then(pl.col(f"px{h}"))
            .otherwise(pl.col("px_end"))
            .alias(f"r{h}_px"),
            (pl.col("last_k") < h).alias(f"gone{h}"),
        )
    # 窗口内的极值：华幸现在的决策是"未来 20 日会不会回到 1 元"
    win = d.filter((pl.col("k") > 0) & (pl.col("k") <= 20)).group_by("symbol").agg(
        pl.col("px").min().alias("min20"),
        pl.col("px").max().alias("max20"),
        pl.col("px").last().alias("px_last20"),
        pl.len().alias("n20"),
        # 一字板：当日 high==low，实际无法成交
        ((pl.col("high") == pl.col("low")) & (pl.col("high") > 0)).sum().alias("flat_days20"),
        ((pl.col("high") - pl.col("low")) / pl.col("px")).median().alias("amp_med20"),
    )
    out = out.join(win, on="symbol", how="left")
    return out


def summarize(ev: pl.DataFrame, label: str) -> None:
    n = ev.height
    if n == 0:
        print(f"\n--- {label}: 无样本")
        return
    alive = ev.filter(pl.col("last_date") >= pl.date(2026, 1, 1)).height
    print(f"\n=== {label} ===  样本 {n} 只（截至 2026 年初仍在交易 {alive} 只，"
          f"已停止交易 {n - alive} 只）")
    print(f"  {'horizon':<9}{'n':>5}{'仍能交易':>9}{'中位收益':>10}{'均值':>9}"
          f"{'胜率':>8}{'跌超30%':>9}{'腰斩':>8}")
    for h in HORIZONS:
        sub = ev.drop_nulls(["px0", f"r{h}_px"])
        if sub.height < 5:
            continue
        r = (sub[f"r{h}_px"] / sub["px0"] - 1).to_numpy()
        gone = float(sub[f"gone{h}"].mean())
        print(f"  {h:>2}日{'':<6}{sub.height:>5}{(1-gone):>8.1%}{np.median(r)*100:>+9.1f}%"
              f"{r.mean()*100:>+8.1f}%{(r > 0).mean():>7.1%}"
              f"{(r < -0.30).mean():>8.1%}{(r < -0.50).mean():>7.1%}")

    sub = ev.drop_nulls(["max20"])
    if sub.height:
        back = (sub["max20"] / sub["px0"] >= PAR / sub["px0"]).sum()
        # "20 日内摸到 1 元"= 从事件日的 0.9x 回到 1.0
        touched = sub.filter(pl.col("max20") >= PAR).height
        print(f"\n  20 日内最高价重新触及 1.00 元: {touched}/{sub.height} = "
              f"{touched / sub.height:.1%}")
        up = (sub["max20"] / sub["px0"] - 1).to_numpy()
        dn = (sub["min20"] / sub["px0"] - 1).to_numpy()
        print(f"  20 日内最大涨幅 中位 {np.median(up)*100:+.1f}%  "
              f"p90 {np.quantile(up,0.9)*100:+.1f}%  max {up.max()*100:+.1f}%")
        print(f"  20 日内最大跌幅 中位 {np.median(dn)*100:+.1f}%  "
              f"p10 {np.quantile(dn,0.1)*100:+.1f}%  max {dn.min()*100:+.1f}%")
        flat = sub["flat_days20"].to_numpy()
        print(f"  20 日内一字板天数 中位 {np.median(flat):.0f} 天  "
              f"均值 {flat.mean():.1f} 天  （一字板 = 当日无法成交）")
        amp = sub["amp_med20"].drop_nulls().to_numpy()
        if amp.size:
            print(f"  日均振幅 中位 {np.median(amp)*100:.1f}%")


def main() -> None:
    df = load_prices()
    print(f"价格样本 {df.height:,} 行 / {df['symbol'].n_unique()} 只 / "
          f"{df['date'].min()} ~ {df['date'].max()}")
    ev_dates = find_events(df)
    print(f"±事件：首次收盘 < 1.00 元  共 {ev_dates.height} 只")
    if ev_dates.is_empty():
        return
    ev = path_after(df, ev_dates)

    summarize(ev, "全部破面值样本")
    summarize(ev.filter(pl.col("ev_date") >= pl.date(2022, 1, 1)), "2022 年后破面值")
    summarize(ev.filter(pl.col("ev_date") < pl.date(2022, 1, 1)), "2022 年前破面值")

    # 华幸自身：事件日 = 2026-11-17，尚无可观察的未来
    hx = ev.filter(pl.col("symbol") == "600340.SH")
    if hx.height:
        r = hx.to_dicts()[0]
        print(f"\n=== *ST华幸 600340.SH ===")
        print(f"  事件日 {r['ev_date']}  收盘 {r['px0']:.2f}  "
              f"窗口内已完成 {r.get('n20') or 0} 个交易日")

    if "--write" in sys.argv:
        import json
        out = DATA / "case_lab" / "par_event.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {"event": "首次收盘价 < 1.00 元", "start": START,
                     "horizons": list(HORIZONS), "n": ev.height},
            "rows": ev.with_columns(pl.col("ev_date").cast(pl.Utf8),
                                    pl.col("last_date").cast(pl.Utf8)).to_dicts(),
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\nwritten -> {out}")


if __name__ == "__main__":
    main()
