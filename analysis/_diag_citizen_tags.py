"""诊断：白马/蓝筹/龙头/妖股 四个标签是否只是「博弈角色主场」换了个说法。

用户要求新增分类体系。这四个词的市场语义**不在同一维度**：
    蓝筹 = 规模 + 稳定        白马 = 盈利质量 + 成长
    龙头 = 行业内相对地位      妖股 = 波动 + 投机
所以它们必然可以共现（一只票能既是蓝筹又是白马又是龙头）。
若做成互斥分组，就是把四个正交概念硬压成一个，页面会给出错误断言。

本脚本回答：
  1. 四标签的覆盖率与共现结构（证明它们不是互斥分类）；
  2. 与 arena（机构/量化/游资/无人区）的重叠率 —— 若 >80% 则标签只是翻译层；
  3. fwd20 收益/左尾的区分度（外部验证：标签是否真的分开不同东西）。

只读本地 data/，不写生产数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"

N_DAYS = 60          # 波动率与涨停统计窗口
HORIZON = 20         # 前瞻


def load_inst() -> pl.DataFrame:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet").select(
        ["symbol", "name", "float_shares"]
    )
    hy = pl.read_parquet(DATA / "ext_data" / "ext_hy_ths" / "part.parquet").select(
        ["symbol", "所属同花顺行业"]
    ).rename({"所属同花顺行业": "industry"})
    # 一级行业：格式「汽车零部件-底盘与发动机系统」
    hy = hy.with_columns(
        pl.col("industry").str.split("-").list.first().alias("industry_l1")
    )
    return inst.join(hy, on="symbol", how="left")


def panel(days: list[str]) -> pl.DataFrame:
    frames = []
    for d in days:
        f = ENRICHED / f"date={d}" / "part.parquet"
        if f.exists():
            frames.append(
                pl.read_parquet(f, columns=[
                    "symbol", "date", "close", "amount",
                    "turnover_rate", "consecutive_limit_ups",
                ])
            )
    return pl.concat(frames, how="vertical_relaxed")


def main() -> int:
    days = sorted(p.name[5:] for p in ENRICHED.glob("date=*"))
    as_of = days[-1 - HORIZON]      # 让 fwd20 有数据
    fwd_day = days[-1]
    win = [d for d in days if d <= as_of][-N_DAYS:]
    print(f"as_of={as_of}  fwd={fwd_day}  窗口 {len(win)} 日")

    inst = load_inst()
    pan = panel(win)

    # ---- 结构量：60 日收益波动率、20 日涨幅、涨停次数、换手中位 ----
    pan = pan.sort(["symbol", "date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret1")
    )
    agg = pan.group_by("symbol").agg([
        pl.col("turnover_rate").median().alias("turn_med"),
        pl.col("ret1").std().alias("vol60"),
        pl.col("consecutive_limit_ups").max().alias("max_boards"),
        pl.col("consecutive_limit_ups").gt(0).sum().alias("lu_days"),
    ])
    close_now = pan.filter(pl.col("date") == pl.lit(as_of).str.to_date()).select(
        ["symbol", "close", "amount"]
    )
    close_20 = pan.filter(pl.col("date") == pl.lit(win[-HORIZON]).str.to_date()).select(
        ["symbol", pl.col("close").alias("close_20")]
    )
    close_fwd = pl.read_parquet(ENRICHED / f"date={fwd_day}" / "part.parquet").select(
        ["symbol", pl.col("close").alias("close_fwd")]
    )

    df = (
        close_now.join(inst, on="symbol", how="left")
        .join(agg, on="symbol", how="left")
        .join(close_20, on="symbol", how="left")
        .join(close_fwd, on="symbol", how="left")
        .filter((pl.col("close") > 0) & (pl.col("float_shares") > 0) & (pl.col("amount") > 0))
        .with_columns([
            (pl.col("close") * pl.col("float_shares")).alias("mcap"),
            (pl.col("close") / pl.col("close_20") - 1).alias("ret20"),
            (pl.col("close_fwd") / pl.col("close") - 1).alias("ret_fwd"),
        ])
    )

    # ---- 财务（单期，最新报告期；announce_date 已在 as_of 之前）----
    fin = pl.read_parquet(DATA / "financials" / "metrics" / "part.parquet").select([
        "symbol", "period_end", "announce_date", "roe", "revenue_yoy",
        "net_income_yoy", "gross_margin", "net_margin", "debt_to_asset_ratio",
    ])
    print(f"财务报告期 {fin['period_end'].max()}，announce 最晚 {fin['announce_date'].max()}")
    # 关键：最新一期财报的公告日（2026-11-13）晚于验证基期（2026-10-23）。
    # 在这张表上做「白马股」的 fwd20 回测就是用未来数据 ⇒ 只报当日特征，不报历史收益。
    pit_ok = str(fin["announce_date"].max()) <= as_of
    print(f"  财务 PIT 可用：{pit_ok}（False ⇒ 白马的 fwd 收益不可作历史结论）")
    df = df.join(fin, on="symbol", how="left")

    # ---- 分位 ----
    def p(col: str) -> str:
        return f"_p_{col}"

    df = df.with_columns([
        (pl.col(c).rank("ordinal") / pl.col(c).count()).alias("_p_" + c)
        for c in ("mcap", "turn_med", "vol60")
    ])
    # 行业内分位
    for c in ("mcap", "ret20"):
        df = df.with_columns(
            (pl.col(c).rank("ordinal").over("industry_l1") / pl.col(c).count().over("industry_l1"))
            .alias(f"_pi_{c}")
        )
    # 毛利率的行业中位
    df = df.with_columns(
        pl.col("gross_margin").median().over("industry_l1").alias("_gm_ind")
    )

    # ---- 四标签 ----
    df = df.with_columns([
        # 蓝筹：规模前 20% + 换手后 50% + 波动后 50%（大而稳，不是"大"）
        (
            (pl.col("_p_mcap") >= 0.80)
            & (pl.col("_p_turn_med") <= 0.50)
            & (pl.col("_p_vol60") <= 0.50)
        ).alias("t_bluechip"),
        # 白马：ROE≥12% + 营收与净利同比均≥0 + 毛利率不低于行业中位
        #       + 资产负债率≤60% + 规模中位以上
        (
            (pl.col("roe") >= 12.0)
            & (pl.col("revenue_yoy") >= 0)
            & (pl.col("net_income_yoy") >= 0)
            & (pl.col("gross_margin") >= pl.col("_gm_ind"))
            & (pl.col("debt_to_asset_ratio") <= 60.0)
            & (pl.col("_p_mcap") >= 0.50)
        ).fill_null(False).alias("t_whitehorse"),
        # 龙头：行业内市值前 20% 且 行业内 20 日涨幅前 20%（大且强）
        (
            (pl.col("_pi_mcap") >= 0.80)
            & (pl.col("_pi_ret20") >= 0.80)
            & pl.col("industry_l1").is_not_null()
        ).fill_null(False).alias("t_leader"),
        # 妖股：20 日内 ≥3 个涨停，或（20 日涨幅≥50% 且 换手前 10%）
        (
            (pl.col("lu_days") >= 3)
            | ((pl.col("ret20") >= 0.50) & (pl.col("_p_turn_med") >= 0.90))
        ).fill_null(False).alias("t_yaogu"),
    ])

    # ---- arena（复刻后端口径）----
    df = df.with_columns([
        (pl.col("mcap").rank("ordinal") / pl.col("mcap").count()).alias("_pa"),
        (pl.col("turn_med").rank("ordinal") / pl.col("turn_med").count()).alias("_pt"),
    ]).with_columns(
        pl.when((pl.col("_pa") >= 0.50) & (pl.col("_pt") >= 0.75)).then(pl.lit("quant"))
        .when((pl.col("_pa") >= 0.50) & (pl.col("_pt") < 0.75)).then(pl.lit("institution"))
        .when((pl.col("_pa") < 0.50) & (pl.col("_pt") >= 0.75)).then(pl.lit("hotmoney"))
        .otherwise(pl.lit("nowhere")).alias("arena")
    )

    n = df.height
    tags = ["t_bluechip", "t_whitehorse", "t_leader", "t_yaogu"]
    names = ["蓝筹", "白马", "龙头", "妖股"]
    print(f"\n样本 N={n}")
    print("\n=== 1. 覆盖率 ===")
    for t, nm in zip(tags, names):
        c = int(df[t].sum())
        print(f"  {nm:4s} {c:5d}  {c/n*100:5.1f}%")

    print("\n=== 2. 共现结构（行=标签，列=同时命中的标签数）===")
    df = df.with_columns(pl.sum_horizontal([pl.col(t).cast(pl.Int8) for t in tags]).alias("n_tags"))
    print(df.group_by("n_tags").len().sort("n_tags"))
    print("\n两两共现（对角线=自身只数）：")
    for i, (a, na) in enumerate(zip(tags, names)):
        row = []
        for b, nb in zip(tags, names):
            both = int((df[a] & df[b]).sum())
            row.append(f"{nb}:{both:5d}")
        print(f"  {na:4s} " + "  ".join(row))

    print("\n=== 3. 与 arena 的重叠（标签内 arena 最大占比）===")
    for t, nm in zip(tags, names):
        sub = df.filter(pl.col(t))
        if sub.height == 0:
            continue
        vc = sub.group_by("arena").len().sort("len", descending=True)
        top = vc.row(0)
        share = top[1] / sub.height
        print(f"  {nm:4s} n={sub.height:5d}  最大格={top[0]:12s} {share*100:5.1f}%  "
              f"分布={dict(zip(vc['arena'].to_list(), vc['len'].to_list()))}")
    print("\n基线（全市场 arena 占比）：")
    base = df.group_by("arena").len().sort("len", descending=True)
    print("  " + str({r[0]: round(r[1]/n*100, 1) for r in base.iter_rows()}))

    print("\n=== 4. 外部验证：fwd20 ===")
    print("（PIT ✓ = 纯价量，基期可得；✗ = 依赖财报，基期尚未公告，只能当当日快照）")
    pit = {"t_bluechip": True, "t_whitehorse": pit_ok, "t_leader": True, "t_yaogu": True}
    print(f"{'标签':6s} {'PIT':>4s} {'n':>6s} {'中位%':>8s} {'均值%':>8s} {'胜率':>7s} {'跌超20%':>8s}")
    print(f"{'全市场':6s} {'-':>4s} {n:6d} {df['ret_fwd'].median()*100:8.2f} "
          f"{df['ret_fwd'].mean()*100:8.2f} {(df['ret_fwd']>0).mean()*100:7.1f} "
          f"{(df['ret_fwd']<-0.2).mean()*100:8.2f}")
    for t, nm in zip(tags, names):
        sub = df.filter(pl.col(t)).drop_nulls("ret_fwd")
        mark = "✓" if pit[t] else "✗"
        if sub.height < 5:
            print(f"{nm:6s} {mark:>4s} n<5 跳过")
            continue
        print(f"{nm:6s} {mark:>4s} {sub.height:6d} {sub['ret_fwd'].median()*100:8.2f} "
              f"{sub['ret_fwd'].mean()*100:8.2f} {(sub['ret_fwd']>0).mean()*100:7.1f} "
              f"{(sub['ret_fwd']<-0.2).mean()*100:8.2f}")
    for a in ("institution", "quant", "hotmoney", "nowhere"):
        sub = df.filter(pl.col("arena") == a).drop_nulls("ret_fwd")
        print(f"[{a[:9]:9s}] {'✓':>4s} {sub.height:6d} {sub['ret_fwd'].median()*100:8.2f} "
              f"{sub['ret_fwd'].mean()*100:8.2f} {(sub['ret_fwd']>0).mean()*100:7.1f} "
              f"{(sub['ret_fwd']<-0.2).mean()*100:8.2f}")

    # ---- 标签 × arena 的互信息（信息量是否被 arena 吃掉）----
    print("\n=== 5. 标签的条件熵 / arena 能解释多少 ===")
    for t, nm in zip(tags, names):
        ptag = df[t].mean()
        if ptag in (0, 1):
            continue
        h_tag = -ptag * np.log2(ptag) - (1 - ptag) * np.log2(1 - ptag)
        # H(tag|arena)
        cond = 0.0
        for a in df["arena"].unique().to_list():
            sub = df.filter(pl.col("arena") == a)
            pa = sub.height / n
            pt = sub[t].mean()
            if 0 < pt < 1:
                cond += pa * (-pt * np.log2(pt) - (1 - pt) * np.log2(1 - pt))
        print(f"  {nm:4s} H={h_tag:.3f} bit  H|arena={cond:.3f} bit  "
              f"arena 已解释 {(1-cond/h_tag)*100:5.1f}%")

    # ---- 各标签的行业集中度（龙头的定义应该"分布均匀"）----
    print("\n=== 6. 行业集中度（top3 行业占比）===")
    for t, nm in zip(tags, names):
        sub = df.filter(pl.col(t))
        if sub.height == 0:
            continue
        vc = sub.group_by("industry_l1").len().sort("len", descending=True)
        top3 = vc.head(3)
        s = sum(r[1] for r in top3.iter_rows()) / sub.height
        print(f"  {nm:4s} top3={[r[0] for r in top3.iter_rows()]} 合计 {s*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
