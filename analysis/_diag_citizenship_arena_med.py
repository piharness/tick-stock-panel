"""诊断四：滚动中位版主场的跨期风险分层 + 前端常量的重算入口。

中位版把日间切换率从 9.35% 降到 1.37%（见 _diag_citizenship_arena_stability2）。
但稳定性可能是用信息换来的：若中位版的分层消失，那它就只是「更稳定的噪声」。

本脚本一次性构建 2024-01..2026-11 面板，算 20 日滚动中位 → 主场，
再算 fwd20 的 dd 概率与中位收益。

两种用法：
    python analysis/_diag_citizenship_arena_med.py           # 打印完整分层
    python analysis/_diag_citizenship_arena_med.py --check   # 只校验后端常量

`--check` 把后端 `citizenship.ARENA_RISK` 的 dd20 / med_ret20 与本脚本重算值
对照，任一格漂移 > 5% 即退出 1。这是那组常数的唯一重算入口 —— 没有它，
页面上的「左尾 9.89%」就是一个没人能复核的装饰数字。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from _diag_citizenship_arena import ENRICHED, load_inst  # noqa: E402

LOOKBACK = 20
HORIZON = 20
# 与后端 ARENA_ORDER 同名，但这里独立写出：校验脚本不能从被测对象取值，
# 否则常量改了校验也跟着改，校验就失效了。
ARENA_KEYS = ("institution", "quant", "hotmoney", "nowhere")
DRIFT_LIMIT = 0.05


def build_panel() -> pl.DataFrame:
    inst = load_inst().select(["symbol", "float_shares"])
    days = sorted(p.name[5:] for p in ENRICHED.glob("date=*"))
    start = next(i for i, d in enumerate(days) if d >= "2023-11-01")
    use = days[start:]
    print(f"读取 {len(use)} 个分区：{use[0]} .. {use[-1]}")

    frames = []
    for i, d in enumerate(use):
        if i % 200 == 0:
            print(f"  {i}/{len(use)}")
        frames.append(
            pl.read_parquet(ENRICHED / f"date={d}" / "part.parquet").select(
                ["symbol", "close", "amount", "turnover_rate", "date"]
            )
        )
    panel = pl.concat(frames, how="vertical_relaxed")
    print(f"面板 {panel.height} 行")

    panel = (
        panel.join(inst, on="symbol", how="left")
        .filter(
            (pl.col("close") > 0) & (pl.col("amount") > 0)
            & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
        )
        .with_columns((pl.col("close") * pl.col("float_shares")).alias("mcap"))
        .sort(["symbol", "date"])
        .with_columns([
            pl.col("turnover_rate").rolling_median(LOOKBACK, min_samples=1).over("symbol").alias("turn_med"),
            pl.col("mcap").rolling_median(LOOKBACK, min_samples=1).over("symbol").alias("mcap_med"),
        ])
    )

    n = pl.col("mcap_med").count().over("date") - 1
    panel = panel.with_columns([
        ((pl.col(c).rank("ordinal").over("date") - 1) / n.clip(1, None)).alias(f"_p_{c}")
        for c in ("mcap_med", "turn_med")
    ]).with_columns(
        pl.when((pl.col("_p_mcap_med") >= 0.50) & (pl.col("_p_turn_med") >= 0.75)).then(pl.lit("quant"))
        .when((pl.col("_p_mcap_med") >= 0.50) & (pl.col("_p_turn_med") < 0.75)).then(pl.lit("institution"))
        .when((pl.col("_p_mcap_med") < 0.50) & (pl.col("_p_turn_med") >= 0.75)).then(pl.lit("hotmoney"))
        .otherwise(pl.lit("nowhere")).alias("arena")
    )

    # 前瞻 20 日收益：symbol 内 shift 数行（panel 已过滤停牌，行即交易日）
    panel = panel.sort(["symbol", "date"]).with_columns(
        (pl.col("close").shift(-HORIZON).over("symbol") / pl.col("close") - 1).alias("ret")
    )
    return panel


def main() -> int:
    check = "--check" in sys.argv
    panel = build_panel()
    days = sorted(panel["date"].unique().to_list())

    # 基期：2024-01 起每月第一个交易日，预留 HORIZON 天
    bases, seen = [], set()
    for d in days:
        ds = d.isoformat()
        if ds < "2024-01" or d > days[-1 - HORIZON]:
            continue
        if ds[:7] not in seen:
            seen.add(ds[:7])
            bases.append(d)

    sub = panel.filter(pl.col("date").is_in(bases)).drop_nulls("ret")
    print(f"\n基期 {len(bases)} 个，样本 {sub.height} 条\n")

    agg = sub.group_by("arena").agg([
        pl.len().alias("N"),
        (pl.col("ret") < -0.2).mean().alias("dd20"),
        pl.col("ret").median().alias("med_ret20"),
        (pl.col("ret") > 0).mean().alias("win"),
    ]).sort("dd20")
    print("=== 按 arena：跨期 dd / 中位收益 ===")
    print(agg)

    per = sub.group_by(["date", "arena"]).agg((pl.col("ret") < -0.2).mean().alias("dd"))
    print("\n=== 排序稳定性（每期 dd 升序秩）===")
    print(
        per.with_columns(pl.col("dd").rank("ordinal").over("date").alias("rk"))
        .group_by("arena").agg([
            pl.col("rk").mean().round(2).alias("平均秩(1=最安全)"),
            pl.col("rk").std().round(2).alias("秩标准差"),
        ]).sort("平均秩(1=最安全)")
    )

    measured = {r["arena"]: r for r in agg.iter_rows(named=True)}

    if not check:
        print("\n（加 --check 可校验后端 ARENA_RISK 常量是否漂移）")
        return 0

    # ===== 校验后端常量 =====
    from app.services import citizenship  # noqa: PLC0415

    print("\n=== 校验 backend ARENA_RISK（漂移 > 5% 退出 1）===")
    bad = 0
    for key in ARENA_KEYS:
        m = measured.get(key)
        if m is None:
            print(f"  {key}: 本脚本无此格，无法校验")
            bad += 1
            continue
        for field in ("dd20", "med_ret20"):
            actual = float(m[field])
            expected = float(citizenship.ARENA_RISK[key][field])
            denom = abs(expected) if abs(expected) > 1e-9 else 1.0
            drift = abs(actual - expected) / denom
            flag = "OK " if drift <= DRIFT_LIMIT else "DRIFT"
            if drift > DRIFT_LIMIT:
                bad += 1
            print(
                f"  {flag} {key:12s} {field:10s} 后端 {expected:+.4f} 实测 {actual:+.4f} "
                f"漂移 {drift*100:5.1f}%"
            )
    if bad:
        print(f"\n{bad} 项漂移超限：请更新 app/services/citizenship.py 的 ARENA_RISK")
        return 1
    print("\n全部在容差内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
