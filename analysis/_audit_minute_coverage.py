"""5min 回补的覆盖率审计：分母必须含退市股，否则「100% 覆盖」只覆盖幸存者。

为什么不能复用 `_verify_bs_minute_batch.py`
------------------------------------------
那个脚本的分母只有 `stock_gap_daily`（306 只缺口股），它能验证「退市股下得准不准」，
但验证不了「退市股有没有被漏掉」——分母本身就是被检验的对象时，覆盖率恒等于 1。

这里的做法是**两套独立分母**：
  - 存续股：`data/archive/kline_daily_qfq`（前复权日K归档）在窗口内有成交的交易日；
  - 缺口股：`backfill_20261117/stock_gap_daily.parquet`（原始价日K）同期有成交的日子。
两只分母不相交（实测 gap ∩ enriched = 0），所以合起来就是「2020 年后真实交易过的
全部 只-日」，覆盖率才有意义。

三档输出
--------
1. 覆盖率：matched / expected，按 cohort（退市 / ST / 其他缺口 / 存续）分开。
   **留存股与退市股必须分别报**：合并报数会让 8% 的退市股被 92% 的存续股稀释，
   看起来 99.9%，实际上退市股可能只到 60%。
2. 成交量单位：每日 sum(volume_raw)/100 ÷ 日线 volume，应 ≈1。这是「同源」的证据，
   不是「差不多」的证据——比值系统性偏离 1 说明单位或拆股口径错。
3. 缺失明细：按票列出缺失日数与缺失日期区间，落 `_coverage.parquet` 供复查。

用法
----
    python analysis/_audit_minute_coverage.py            # 全量
    python analysis/_audit_minute_coverage.py --cohort gap
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

ROOT = Path(r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
F5 = DATA / "backfill_bs_minute" / "f5"
BS_START = "2020-01-02"
SURVIVOR_END = "2024-11-06"      # 存续股交给 baostock 的末日


def gap_symbols() -> set[str]:
    p = DATA / "backfill_20261117/stock_gap_daily.parquet"
    return set(pl.read_parquet(p, columns=["symbol"])["symbol"].unique().to_list())


def expected_days(gaps: set[str]) -> pl.DataFrame:
    """窗口内「日线有成交」的 (symbol, date) 全集（两套分母拼接）。

    返回逐 只-日 而不是逐票计数：计数相减会把「f5 多出的停牌占位日」抵消掉
    真实缺失（实测漏 3 只-日被 22 个多余日掩盖），而且 u32 相减会回绕成 42 亿。
    """
    arch = (pl.scan_parquet(DATA / "archive/kline_daily_qfq/**/*.parquet",
                            extra_columns="ignore")
            .filter(pl.col("date") >= pl.lit(BS_START).str.to_date())
            .filter(pl.col("date") <= pl.lit(SURVIVOR_END).str.to_date())
            .filter(pl.col("volume") > 0)
            .select(["symbol", "date"])
            .collect())
    arch = arch.filter(~pl.col("symbol").is_in(sorted(gaps)))

    gap = (pl.read_parquet(DATA / "backfill_20261117/stock_gap_daily.parquet",
                           columns=["symbol", "date", "volume"])
           .filter(pl.col("date") >= pl.lit(BS_START).str.to_date())
           .filter(pl.col("volume") > 0)
           .select(["symbol", "date"]))
    return pl.concat([arch, gap]).unique()


def got_days(files: list[Path]) -> pl.DataFrame:
    """f5 侧「真实有成交」的 只-日。

    **必须按当日成交量过滤**，不能只看日期存不存在：baostock 对停牌日会返回
    1 根 `volume=0` 的占位 bar，它和「一字板」长得一模一样。只按日期计数会把
    这些日子算成「多出」（实测 1,223 个），看起来像数据多了，实际是停牌占位，
    而 merge 早就把它们剔掉了。分母侧本来就 filter(volume>0)，两侧必须同口径。
    """
    rows = []
    for f in files:
        d = pl.read_parquet(f, columns=["datetime", "volume_raw"])
        if d.height == 0:
            continue
        col = "volume_raw" if "volume_raw" in d.columns else "volume"
        rows.append(
            d.with_columns(pl.col("datetime").dt.date().alias("date"))
             .group_by("date").agg(pl.col(col).sum().alias("_v"))
             .filter(pl.col("_v") > 0)
             .select("date")
             .with_columns(pl.lit(f.stem, dtype=pl.String).alias("symbol")))
    if not rows:
        return pl.DataFrame(schema={"date": pl.Date, "symbol": pl.String})
    return pl.concat(rows).unique()


def cohort_of(sym: str, name: str | None) -> str:
    if name and "退" in name:
        return "delisted"
    return "gap_other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", choices=["all", "gap"], default="all")
    a = ap.parse_args()

    gaps = gap_symbols()
    files = sorted(F5.glob("*.parquet"))
    if not files:
        print("f5 为空")
        return 1
    print(f"f5 {len(files)} 只；分母：归档日K（存续） + gap 日K（缺口 {len(gaps)} 只）",
          flush=True)

    exp = expected_days(gaps)
    got = got_days(files)
    # 只审计已下载的票（分母仍取全窗口，否则未下载票会把覆盖率拖成 8%）
    got_syms = got["symbol"].unique()
    exp = exp.filter(pl.col("symbol").is_in(got_syms))

    miss = exp.join(got, on=["symbol", "date"], how="anti")
    # 「多出」必须限定在研究窗口内：缺口股/ST 股的 baostock 段补到**真实末根日**
    # （52 只延伸到 2026，实测窗口外 57 万行），那是设计要求，不是多出来的脏数据。
    # 不切窗口会把 1,166 个合法的窗口外 只-日 报成「f5 多出」，读起来像数据错。
    raw_extra = got.join(exp, on=["symbol", "date"], how="anti")
    extra = raw_extra.filter(pl.col("date") <= pl.lit(SURVIVOR_END).str.to_date())
    out_win = raw_extra.height - extra.height

    m = (exp.group_by("symbol").len().rename({"len": "exp"})
         .join(got.group_by("symbol").len().rename({"len": "got"}), on="symbol")
         .join(miss.group_by("symbol").len().rename({"len": "miss"}),
               on="symbol", how="left")
         .with_columns(pl.col("miss").fill_null(0)))

    inst = pl.read_parquet(DATA / "instruments/instruments.parquet",
                           columns=["symbol", "name"])
    m = m.join(inst, on="symbol", how="left")
    m = m.with_columns(
        pl.when(pl.col("name").fill_null("").str.contains("退"))
          .then(pl.lit("delisted"))
          .when(pl.col("symbol").is_in(sorted(gaps)))
          .then(pl.lit("gap_other"))
          .otherwise(pl.lit("survivor"))
          .alias("cohort"))

    print("\n=== 覆盖率（分母 = 窗口内真实有成交的 只-日）===")
    print(f"合计   {exp.height:,} 应覆盖 / 缺 {miss.height:,} / f5 多出 {extra.height:,} "
          f"⇒ {1 - miss.height / exp.height:.6f}")
    if extra.height:
        # 「多出」按日期聚一下就能分辨两种成因：**集中在少数几天** ⇒ 日线侧缺分区
        # （那天全市场都没有日线，分钟自然全成"多出"）；**散布在全年** ⇒ 才是个股口径差。
        ed = extra.group_by("date").agg(pl.len().alias("n")).sort("n", descending=True)
        print(f"       多出的日期分布 top5：{ed.head(5).to_dicts()}")
        print(f"       涉及 {ed.height} 个交易日，中位 {int(ed['n'].median())} 只/日")
        es = extra.group_by("symbol").agg(pl.len().alias("n")).sort("n", descending=True)
        print(f"       集中在哪些票 top5：{es.head(5).to_dicts()}（共 {es.height} 只）")
    if out_win:
        print(f"       另有 {out_win:,} 个 只-日 在窗口外（缺口股补到真实末根日，设计如此）")

    for c in ["delisted", "gap_other", "survivor"]:
        s = m.filter(pl.col("cohort") == c)
        if s.height == 0:
            continue
        se, sm = int(s["exp"].sum()), int(s["miss"].sum())
        perfect = s.filter(pl.col("miss") <= 0).height
        print(f"{c:<10} {(se-sm)/se:.6f}  缺 {sm:,}/{se:,} 只-日  "
              f"n={s.height}  零缺失 {perfect} 只 ({perfect/s.height:.1%})")

    worst = m.filter(pl.col("miss") > 0).sort("miss", descending=True).head(10)
    if worst.height:
        print("\n缺失最多的 10 只：")
        for r in worst.iter_rows(named=True):
            print(f"  {r['symbol']} {str(r['name'])[:8]:<8} exp={r['exp']} "
                  f"got={r['got']} miss={r['miss']}")

    out = DATA / "backfill_bs_minute" / "_coverage.parquet"
    m.write_parquet(out)
    print(f"\n逐票明细 → {out}")

    check_universe_daily()
    return 0


def check_universe_daily() -> None:
    """任务池里每只票，必须能在（归档日K ∪ 缺口日K）找到日线。

    为什么单列一条：覆盖率审计的分母来自日K，**分母里没有的票它永远不会报**。
    2026-11-21 实测抓到两类：
    1. **真缺口** 002731.SZ（萃华退）在窗口内正常交易 1,166 天却无日线，已补入 gap；
    2. **换码** 300114.SZ（中航电测）其实是 302132.SZ（中航成飞）的旧代码——日线
       在新码下、旧码查是「缺」，实为同一实体。换码不能补数据（会双计），要 resolve。
    """
    import contextlib
    import io

    sys.path.insert(0, str(ROOT / "analysis"))
    import backfill_bs_minute as bsm
    from code_alias import resolve

    with contextlib.redirect_stdout(io.StringIO()):
        tl = bsm.tasks()
    gaps = set(bsm.gap_symbols())

    ip = DATA / "backfill_bs_minute" / "_inplay.parquet"
    have = set(pl.read_parquet(ip, columns=["symbol"])["symbol"].to_list()) | gaps
    # 先 resolve 换码再判存在性：300114 → 302132 命中 enriched，不算缺失。
    no_daily: list[str] = []
    aliased: list[tuple[str, str]] = []
    for t in tl:
        s = t["symbol"]
        if s in have:
            continue
        if resolve(s) in have:
            aliased.append((s, resolve(s)))
            continue
        no_daily.append(s)

    print(f"\n=== 任务池日线存在性（分母之外的一层）===")
    print(f"任务 {len(tl):,} 只；日线侧找不到 {len(no_daily)} 只；"
          f"换码后命中 {len(aliased)} 只")
    if aliased:
        print("  换码命中（日线在新码下，非缺失）：")
        for old, new in aliased:
            print(f"    {old} → {new}")
    if no_daily:
        inst = pl.read_parquet(DATA / "instruments/instruments.parquet",
                               columns=["symbol", "name"])
        nm = {r["symbol"]: r["name"] for r in inst.iter_rows(named=True)}
        mini = DATA / "backfill_bs_minute" / "f5"
        for s in no_daily:
            got = "有分钟" if (mini / f"{s}.parquet").exists() else "无分钟"
            print(f"  {s}  {str(nm.get(s,''))[:10]:<10} {got}")
        print("  ⇒ 有分钟的那几只必须补日线，否则研究侧会因为取不到 raw_close 而静默丢弃它们")


if __name__ == "__main__":
    sys.exit(main())
