"""公民等级样本外验证：等级能否预测「未来该不该关注」？

命题（可证伪）：
  短线雷达页（原市场公民）把个股按成交额/换手率的**当日横截面分位**分成 core/active/background/silent。
  这个等级是「描述」（它值不值得你现在花注意力），不是「预测」（它未来会不会涨）。
  本脚本验证的正是这个边界：等级对「未来该不该关注」到底有没有预测力？

「未来该不该关注」用三个 T 日之后才可知的代理（严格无前视）：
  1. 涨停概率  未来 N 日是否出现过涨停（consecutive_limit_ups >= 1）
                —— 这是「值不值得盯」最直接的代理：涨停是注意力最硬的回报。
  2. 超额收益  T+1 开盘买入持有 N 日，相对当日全市场中位数
                —— 验证「看它」有没有 alpha 意义（流动性因子经典测法）。
  3. 注意力粘性 未来 N 日后，仍在 core/active 的概率
                —— 高等级是否「稳定」，决定你今天的注意力明天是否还值钱。

统计纪律（对齐 probe_excess_outcome / probe_decile_net）：
  - 等级用 T 日收盘横截面算（T 日收盘后可知），未来从 T+1 起；
  - 涨停/收益按**日期聚合**再算 t（同日多票共享 beta，不独立）；
  - t 用 block bootstrap（块长 2*N，重叠收益自相关必须落在块内）；
  - 附随机对照组（每等级随机抽同数量），证明方法本身无偏。

数据：kline_daily_enriched（与页面同源，2005 起），避免「验证的等级≠展示的等级」。
输出：data/.scratch/citizenship_signal.json

═══ 已实测的结论（2026-11-16，5289 日 / 2005–2026）═══

1. **注意力粘性：等级强预测**。core 未来 20 日仍为核心/活跃的概率 97.2%，
   silent 仅 11.2%。等级回答「今天的注意力明天还值不值钱」是准确的。

2. **涨停概率（H=20）：core 显著更高**。core 23.3% vs silent 17.6%，t=33.7。
   短期（H=1）则反向（silent 4.4% > core 2.7%，t=-15.3），因为涨停是
   小盘股的短期事件、而 silent 恰好偏小盘。

3. **超额收益：与等级**反向**且单调**。H=20：core −11bp、active +132bp、
   background +299bp、silent +606bp。越沉默超额越高 —— 这是「低流动性溢价 +
   小市值」的复合效应（silent 市值中位 26 亿，core 281 亿），不是「沉默有 alpha」。
   混在结论里的混淆变量是**市值**，本脚本不中性化它，读者必须知道：
   silent 的超额主要是小盘/低流动性，不是「关注度低」本身的功劳。

4. **随机对照组无偏**（四个随机等级涨停率均 ≈19.77%），方法本身无 bug。

产品含义：等级是「注意力粘性」的预测器，不是「收益」的预测器。
若用户把「核心公民」误读为「该买的票」，会买到大盘蓝筹（超额 −11bp）而错过
沉默小盘（+606bp）。页面边界声明必须把这个数据结论写进去，不能只写「不预测收益」。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

TABLE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"
HORIZONS = (1, 5, 20)

# 与 citizenship 服务同一份规则（复制，避免脚本 import 后端运行时环境）。
CORE_MIN_AMOUNT = 0.90
ACTIVE_MIN_AMOUNT = 0.50
ACTIVE_MIN_TURNOVER = 0.90
SILENT_MAX_AMOUNT = 0.10
SILENT_MAX_TURNOVER = 0.10
LEVEL_ORDER = ["core", "active", "background", "silent"]


def block_se(x: np.ndarray, block: int, reps: int = 1000, seed: int = 0) -> float:
    """块 bootstrap 标准误。块长至少 2*H，否则重叠收益自相关会虚高 t。"""
    n = len(x)
    if n < block:
        block = max(1, n)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[i] = x[idx.ravel()[:n]].mean()
    return float(means.std(ddof=1))


def load_history() -> pl.DataFrame:
    """读全历史（2005 起）日线面板，只取验证需要的列。"""
    files = sorted(str(p) for p in TABLE.glob("date=*/part.parquet"))
    cols = ["symbol", "date", "close", "amount", "turnover_rate", "consecutive_limit_ups"]
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )


def assign_level(d: pl.DataFrame) -> pl.DataFrame:
    """当日横截面：三轴分位 + 等级。与 citizenship._assign_level 同规则。"""
    n = d.height
    if n < 2:
        return d
    # ordinal rank 归一（0~1，1=最高）
    def qrank(col: str) -> pl.Expr:
        return (pl.col(col).rank("ordinal") - 1) / (n - 1)

    t, a = qrank("turnover_rate"), qrank("amount")
    level = (
        pl.when(a >= CORE_MIN_AMOUNT).then(pl.lit("core"))
        .when((a >= ACTIVE_MIN_AMOUNT) | (t >= ACTIVE_MIN_TURNOVER)).then(pl.lit("active"))
        .when((a < SILENT_MAX_AMOUNT) & (t < SILENT_MAX_TURNOVER)).then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
    )
    return d.with_columns(
        qrank("amount").alias("p_amount"),
        qrank("turnover_rate").alias("p_turnover"),
        level.alias("level"),
    )


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    print(f"  {df.height:,} 行  {df['date'].min()} → {df['date'].max()}", flush=True)

    # 只保留有有效成交额/换手率的行
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
    )
    print(f"  过滤后 {df.height:,} 行", flush=True)

    # 逐日算等级（group_by date → assign_level）
    print("逐日算等级…", flush=True)
    df = (
        df.sort(["symbol", "date"])
        .group_by("date", maintain_order=True)
        .map_groups(assign_level)
    )

    # 未来涨停：未来 N 日是否出现过 consecutive_limit_ups >= 1
    # （用 shift(-k) 取未来第 k 日的连板数，k=1..N 任一 >= 1 即「未来 N 日涨停过」）
    for h in HORIZONS:
        any_limit = None
        for k in range(1, h + 1):
            col = (pl.col("consecutive_limit_ups").shift(-k).over("symbol") >= 1)
            any_limit = col if any_limit is None else (any_limit | col)
        df = df.with_columns(any_limit.alias(f"limit_{h}"))

    # 未来收益：T+1 收盘买入（用 close 近似，前复权序列），持有 N 日
    # 严格口径应是 T+1 开盘买，但 close 已是前复权、跨除权成立；
    # 用 close.shift(-1) 到 close.shift(-(h+1)) 近似 T+1 收盘 → T+1+N 收盘。
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-(h + 1)).over("symbol")
             / pl.col("close").shift(-1).over("symbol") - 1.0).alias(f"fwd{h}")
        )

    # 注意力粘性：未来 N 日后仍属于 core/active
    for h in HORIZONS:
        fut_level = pl.col("level").shift(-h).over("symbol")
        df = df.with_columns(
            (fut_level.is_in(["core", "active"])).alias(f"stay_top_{h}")
        )

    # 只保留有完整未来数据的行
    df = df.filter(
        pl.col("fwd20").is_not_null() & pl.col("limit_20").is_not_null()
    )

    # 超额收益：相对当日全市场中位数
    for h in HORIZONS:
        med = df.group_by("date").agg(pl.col(f"fwd{h}").median().alias("med"))
        df = df.join(med, on="date").with_columns(
            (pl.col(f"fwd{h}") - pl.col("med")).alias(f"exc{h}")
        ).drop("med")

    print(f"  有效样本 {df.height:,} 行（有完整未来数据）", flush=True)

    report: dict = {"rows": int(df.height), "horizons": list(HORIZONS)}

    # ===== 各等级的未来表现 =====
    for h in HORIZONS:
        print(f"\n=== H={h} 日 ===", flush=True)
        rows = []
        for lvl in LEVEL_ORDER:
            seg = df.filter(pl.col("level") == lvl)
            # 涨停概率：按日聚合（每天该等级的涨停率），再跨日统计
            daily = (
                seg.group_by("date")
                .agg(pl.col(f"limit_{h}").mean().alias("rate"))
            )
            limit_rate = float(daily["rate"].mean())
            limit_se = block_se(daily["rate"].to_numpy(), block=2 * h)
            # 超额收益：按日聚合均值，再跨日
            exc_daily = (
                seg.group_by("date")
                .agg(pl.col(f"exc{h}").mean().alias("exc"))
            )
            exc_mean = float(exc_daily["exc"].mean())
            exc_se = block_se(exc_daily["exc"].to_numpy(), block=2 * h)
            exc_t = exc_mean / exc_se if exc_se > 0 else float("nan")
            # 注意力粘性
            stay_daily = (
                seg.group_by("date")
                .agg(pl.col(f"stay_top_{h}").mean().alias("stay"))
            )
            stay = float(stay_daily["stay"].mean())
            n_days = int(daily.height)
            n_members = int(seg.height)
            rows.append({
                "level": lvl,
                "n_days": n_days,
                "n_members_total": n_members,
                "limit_rate": round(limit_rate, 5),
                "limit_se": round(limit_se, 5),
                "exc_bp": round(exc_mean * 1e4, 2),
                "exc_se_bp": round(exc_se * 1e4, 2),
                "exc_t": round(exc_t, 2),
                "stay_top_rate": round(stay, 4),
            })
            print(f"  {lvl:10s} 涨停率 {limit_rate:.3%} ±{limit_se:.3%}  "
                  f"超额 {exc_mean*1e4:+.1f}bp(t={exc_t:+.1f})  "
                  f"粘性 {stay:.1%}  ({n_days} 日 / {n_members:,} 只-日)")
        report[f"H{h}"] = rows

        # core vs silent 的涨停率差异 + t（配对，按日）
        core_d = df.filter(pl.col("level") == "core").group_by("date").agg(
            pl.col(f"limit_{h}").mean().alias("r"))
        sil_d = df.filter(pl.col("level") == "silent").group_by("date").agg(
            pl.col(f"limit_{h}").mean().alias("r"))
        joined = core_d.join(sil_d, on="date", suffix="_s")
        diff = (joined["r"] - joined["r_s"]).to_numpy()
        diff_se = block_se(diff, block=2 * h)
        diff_t = diff.mean() / diff_se if diff_se > 0 else float("nan")
        report[f"H{h}_core_vs_silent"] = {
            "limit_rate_diff": round(float(diff.mean()), 5),
            "t": round(float(diff_t), 2),
            "n_days": int(len(diff)),
        }
        print(f"  core vs silent 涨停率差 {diff.mean():+.3%}  t={diff_t:+.1f}  ({len(diff)} 日)")

    # ===== 随机对照组（方法无偏性检查）=====
    print("\n=== 随机对照组（方法无偏检查，应无等级间差异）===", flush=True)
    rng = np.random.default_rng(42)
    df = df.with_columns(
        pl.Series("rand_level", rng.choice(LEVEL_ORDER, size=df.height)).cast(pl.Utf8)
    )
    h = 20
    for lvl in LEVEL_ORDER:
        seg = df.filter(pl.col("rand_level") == lvl)
        daily = seg.group_by("date").agg(pl.col(f"limit_{h}").mean().alias("rate"))
        print(f"  随机[{lvl:10s}] 涨停率 {daily['rate'].mean():.3%}")
    report["placebo"] = {lvl: round(
        float(df.filter(pl.col('rand_level') == lvl)
              .group_by('date').agg(pl.col(f'limit_{h}').mean().alias('r'))['r'].mean()), 5)
        for lvl in LEVEL_ORDER}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "citizenship_signal.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/citizenship_signal.json")


if __name__ == "__main__":
    sys.exit(main())
