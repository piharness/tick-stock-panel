"""诊断：四个属性标签的多期外部验证（给页面常量背书）。

单期结果（2026-10-23 → 11-20，一路下跌）不能当规律：那一期全市场中位 −16.2%，
任何「抗跌」标签都会显得很强。必须跨期看排序是否同向。

只验证三个**纯价量**标签（蓝筹 / 龙头 / 妖股）：
白马依赖财报，而 metrics 只有最新一期（2026-09-30，公告 11-13），
在历史基期上用它就是前视 —— 这一条不验证，也不给页面常量。

窗口口径：每期用基期当日可算的 60 日滚动统计分类，fwd20 = 基期收盘持有 20 日。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"

LOOKBACK = 60
TURN_WIN = 20
HORIZON = 20
TAGS = ("bluechip", "leader", "yaogu")


def build_panel() -> tuple[pl.DataFrame, list[str]]:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet").select(
        ["symbol", "float_shares"]
    )
    hy = pl.read_parquet(DATA / "ext_data" / "ext_hy_ths" / "part.parquet").select(
        ["symbol", "所属同花顺行业"]
    ).rename({"所属同花顺行业": "industry"})
    hy = hy.with_columns(
        pl.col("industry").str.split("-").list.first().alias("industry_l1")
    )

    days = sorted(p.name[5:] for p in ENRICHED.glob("date=*"))
    start = next(i for i, d in enumerate(days) if d >= "2023-10-01")
    use = days[start:]
    print(f"读取 {len(use)} 个分区：{use[0]} .. {use[-1]}")

    frames = []
    for i, d in enumerate(use):
        if i % 200 == 0:
            print(f"  {i}/{len(use)}")
        frames.append(
            pl.read_parquet(ENRICHED / f"date={d}" / "part.parquet").select(
                ["symbol", "close", "amount", "turnover_rate", "consecutive_limit_ups", "date"]
            )
        )
    pan = pl.concat(frames, how="vertical_relaxed")
    pan = (
        pan.join(inst, on="symbol", how="left")
        .join(hy, on="symbol", how="left")
        .filter(
            (pl.col("close") > 0) & (pl.col("amount") > 0)
            & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
        )
        .with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
        .sort(["symbol", "date"])
    )
    # 逐票滚动统计（panel 已剔除停牌行，行即交易日）
    pan = pan.with_columns([
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_r1"),
        pl.col("turnover_rate").rolling_median(TURN_WIN, min_samples=1).over("symbol").alias("turn_med"),
        pl.col("mcap").rolling_median(TURN_WIN, min_samples=1).over("symbol").alias("mcap_med"),
        pl.col("consecutive_limit_ups").gt(0).cast(pl.Int8)
          .rolling_sum(LOOKBACK, min_samples=1).over("symbol").alias("lu_days"),
    ]).with_columns(
        pl.col("_r1").rolling_std(LOOKBACK, min_samples=10).over("symbol").alias("vol60"),
        (pl.col("close") / pl.col("close").shift(TURN_WIN).over("symbol") - 1).alias("ret20"),
        (pl.col("close").shift(-HORIZON).over("symbol") / pl.col("close") - 1).alias("ret_fwd"),
    )
    return pan, use


def classify(day_df: pl.DataFrame) -> pl.DataFrame:
    """在单日横截面上打分位并派生标签（口径与后端一致）。"""
    n = pl.col("mcap_med").count()
    df = day_df.with_columns([
        (pl.col("mcap_med").rank("ordinal") / n).alias("_p_mcap"),
        (pl.col("turn_med").rank("ordinal") / n).alias("_p_turn"),
        (pl.col("vol60").rank("ordinal") / n).alias("_p_vol"),
        (pl.col("mcap_med").rank("ordinal").over("industry_l1")
         / pl.col("mcap_med").count().over("industry_l1")).alias("_pi_mcap"),
        (pl.col("ret20").rank("ordinal").over("industry_l1")
         / pl.col("ret20").count().over("industry_l1")).alias("_pi_ret"),
    ])
    df = df.with_columns(
        pl.when((pl.col("_p_mcap") >= 0.50) & (pl.col("_p_turn") >= 0.75)).then(pl.lit("quant"))
        .when((pl.col("_p_mcap") >= 0.50) & (pl.col("_p_turn") < 0.75)).then(pl.lit("institution"))
        .when((pl.col("_p_mcap") < 0.50) & (pl.col("_p_turn") >= 0.75)).then(pl.lit("hotmoney"))
        .otherwise(pl.lit("nowhere")).alias("arena")
    ).with_columns([
        ((pl.col("_p_mcap") >= 0.80) & (pl.col("_p_turn") <= 0.50) & (pl.col("_p_vol") <= 0.50))
        .fill_null(False).alias("bluechip"),
        ((pl.col("_pi_mcap") >= 0.80) & (pl.col("_pi_ret") >= 0.80)
         & pl.col("industry_l1").is_not_null()).fill_null(False).alias("leader"),
        ((pl.col("lu_days") >= 3) | ((pl.col("ret20") >= 0.50) & (pl.col("_p_turn") >= 0.90)))
        .fill_null(False).alias("yaogu"),
    ])
    return df


def main() -> int:
    pan, use = build_panel()
    print(f"面板 {pan.height} 行")

    days = sorted(pan["date"].unique().to_list())
    bases, seen = [], set()
    for d in days:
        ds = d.isoformat()
        if ds < "2024-01" or d > days[-1 - HORIZON]:
            continue
        if ds[:7] not in seen:
            seen.add(ds[:7])
            bases.append(d)
    print(f"基期 {len(bases)} 个")

    rows = []
    for b in bases:
        cur = pan.filter(pl.col("date") == b).drop_nulls(["vol60", "ret20", "ret_fwd"])
        if cur.is_empty():
            continue
        cur = classify(cur)
        for t in TAGS:
            sub = cur.filter(pl.col(t))
            if sub.height >= 20:
                rows.append({"base": b.isoformat(), "key": t, "n": sub.height,
                             "dd": float((sub["ret_fwd"] < -0.2).mean()),
                             "med": float(sub["ret_fwd"].median())})
        sub = cur
        rows.append({"base": b.isoformat(), "key": "ALL", "n": sub.height,
                     "dd": float((sub["ret_fwd"] < -0.2).mean()),
                     "med": float(sub["ret_fwd"].median())})
        for a in ("institution", "quant", "hotmoney", "nowhere"):
            s2 = cur.filter(pl.col("arena") == a)
            if s2.height >= 20:
                rows.append({"base": b.isoformat(), "key": a, "n": s2.height,
                             "dd": float((s2["ret_fwd"] < -0.2).mean()),
                             "med": float(s2["ret_fwd"].median())})
        print(f"  {b.isoformat()} ok")

    panel = pl.DataFrame(rows)
    print("\n=== 跨期 dd20 / 中位收益 ===")
    agg = panel.group_by("key").agg([
        pl.len().alias("期数"),
        (pl.col("n").mean()).round(0).alias("平均只数"),
        pl.col("dd").mean().round(4).alias("dd均值"),
        pl.col("dd").std().round(4).alias("dd标准差"),
        pl.col("med").mean().round(4).alias("中位均值"),
        ((pl.col("med") > 0).mean()).round(3).alias("正收益期占比"),
    ]).sort("dd均值")
    print(agg)

    print("\n=== 排序稳定性（每期 dd 升序秩，含 ALL 与四格）===")
    print(
        panel.with_columns(pl.col("dd").rank("ordinal").over("base").alias("rk"))
        .group_by("key").agg([
            pl.col("rk").mean().round(2).alias("平均秩(1=最安全)"),
            pl.col("rk").std().round(2).alias("秩标准差"),
        ]).sort("平均秩(1=最安全)")
    )

    print("\n=== 逐期 dd（看是否单调）===")
    wide = panel.pivot(on="key", index="base", values="dd").sort("base")
    print(wide.fill_null(float("nan")))

    if "--check" not in sys.argv:
        print("\n（加 --check 可校验后端 TAG_RISK / MARKET_RISK 常量是否漂移）")
        return 0

    # ===== 校验后端常量 =====
    sys.path.insert(0, str(ROOT / "backend"))
    from app.services import citizenship  # noqa: PLC0415

    measured = {r["key"]: r for r in agg.iter_rows(named=True)}
    print("\n=== 校验 backend TAG_RISK（漂移 > 5% 退出 1）===")
    bad = 0
    for key in ("bluechip", "leader", "yaogu"):
        m = measured.get(key)
        if m is None:
            print(f"  {key}: 本脚本无此格")
            bad += 1
            continue
        for fld, col in (("dd20", "dd均值"), ("med_ret20", "中位均值")):
            actual = float(m[col])
            expected = float(citizenship.TAG_RISK[key][fld])
            denom = abs(expected) if abs(expected) > 1e-9 else 1.0
            drift = abs(actual - expected) / denom
            if drift > 0.05:
                bad += 1
            print(f"  {'OK ' if drift <= 0.05 else 'DRIFT'} {key:10s} {fld:10s} "
                  f"后端 {expected:+.4f} 实测 {actual:+.4f} 漂移 {drift*100:5.1f}%")

    m = measured.get("ALL")
    if m is not None:
        for fld, col in (("dd20", "dd均值"), ("med_ret20", "中位均值")):
            actual = float(m[col])
            expected = float(citizenship.MARKET_RISK[fld])
            denom = abs(expected) if abs(expected) > 1e-9 else 1.0
            drift = abs(actual - expected) / denom
            if drift > 0.05:
                bad += 1
            print(f"  {'OK ' if drift <= 0.05 else 'DRIFT'} {'MARKET':10s} {fld:10s} "
                  f"后端 {expected:+.4f} 实测 {actual:+.4f} 漂移 {drift*100:5.1f}%")

    if bad:
        print(f"\n{bad} 项漂移超限：请更新 app/services/citizenship.py 的 TAG_RISK / MARKET_RISK")
        return 1
    print("\n全部在容差内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
