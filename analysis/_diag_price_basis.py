"""诊断: 分钟库的价格基准是「原始价」还是「前复权价」。

## 为什么需要这个量

`kline_minute` 与 `kline_daily` / `kline_daily_enriched` 的价格列必须同基, 否则
任何跨库比较 (分钟/日线、盘中/收盘) 都会在除权日静默偏差几个百分点。

已知事实 (本仓两套日线库):
  - `kline_daily.close`            = 原始价
  - `kline_daily_enriched.raw_close` = 原始价
  - `kline_daily_enriched.close`   = 前复权 (锚定最新分区日, 会随除权向后改写)
  - `k_daily := close / raw_close` 因此是**逐行可读**的复权系数 (11-12 全市场 = 1)

## 判据 (不需要除权表、不需要快照、不需要日历)

若成交额是真实成交额 (不随复权缩放), 则对任一根 bar:

    amount / (volume * 100)  ≈  原始成交均价 VWAP_raw
    VWAP_raw / close         =  1 / s        (s = 该 bar 价格的缩放系数)

即 `k_min := 1 / median(VWAP_raw / close)` 逐 (symbol, 交易日) 复现 s。
两个互斥假设给出两个可区分的读数:

  H_raw       分钟存原始价  ⇒ k_min ≈ 1     (与日线系数无关)
  H_adj       分钟存前复权  ⇒ k_min ≈ k_daily (复现日线的逐日系数)

当且仅当 k_daily ≈ 1 时两者重合 —— 此时**无判别力**, 必须报 exit 2,
不能因为「没红」就当通过。

## 校验链

  1. 标定: 在日线表上用 k_daily (已知真值) 回归 k_hat, 斜率应 ≈ -1, R² 应 > 0.9;
  2. 精度: 报逐 symbol-day k_min 的标准差, 这是可分辨下限;
  3. 负控: 向分钟 OHLC 注入已知缩放, 看 k_min 是否线性复现 (--inject);
  4. 裁决: 按 diag 计数, 只有 n_raw 或 n_adj 非零才算「测过」。

用法:
    backend/.venv/Scripts/python.exe analysis/_diag_price_basis.py [--inject] [--day D]

退出码: 0 = 有判别力且判据通过; 1 = 判据被打破 (两库不同基/工具失效);
        2 = 无判别力 (全部 symbol-day 退化), 不得视为通过。
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT / "backend"))

# 判据阈值必须与生产端同源, 不许在这里另写一套 —— 理由见 k_min_of 的 docstring。
from app.plugins.tdx.provider import _scale_from_amount  # noqa: E402

# k_daily 偏离 1 超过这个量才算「非退化样本」。
DEGEN = 1.0e-6


def k_from_events(as_of: str) -> pl.DataFrame:
    """从除权表算前复权系数: k(day) = Π_{事件日 > day} 1/ex_factor。

    存量的 `enriched.close/raw_close` 只对已写入的分区有效, 而且是「按写入时的
    最新日」锚定; 一旦新除权日进入事件表, 用它就能立刻判昨日口径, 不必等重算。
    例: 000001.SZ 09-23 = 1/1.022026 = 0.978448。
    """
    p = DATA / "adj_factor" / "all.parquet"
    if not p.exists():
        return pl.DataFrame()
    ev = pl.read_parquet(p, columns=["symbol", "trade_date", "ex_factor"]).filter(
        (pl.col("trade_date") > (as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))))
        & (pl.col("ex_factor") > 0)
    )
    if ev.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, "k_tab": pl.Float64})
    return ev.group_by("symbol").agg(
        (1.0 / pl.col("ex_factor")).product().alias("k_tab")
    )


def _ols(x: list[float], y: list[float]) -> tuple[float, float, int]:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return float("nan"), float("nan"), n
    return sxy / sxx, sxy * sxy / (sxx * syy), n


def k_min_of(df: pl.DataFrame) -> tuple[float, float, int, float]:
    """逐 (symbol, 交易日) 的 (k_min, 判别阈, 样本数, 中位价); df 需含 amount/volume/close。

    判别阈**直接取生产端 `_scale_from_amount` 的返回值**, 不在这里另写一套。
    2026-11-12 之前判据端是 max(3e-4, 半跳)、生产端是 max(3·se, 5e-4, 半跳):
    判据比动手线更严, 某只真 s=4bp 时生产端正确地不动手, 验收端却报红 ——
    按"任一红即回滚"就把对的撤了。同源之后: 红 ⟺ 生产端该动而没动。
    """
    d = df.filter((pl.col("volume") > 0) & (pl.col("amount") > 0) & (pl.col("close") > 0))
    if d.height == 0:
        return float("nan"), float("nan"), 0, float("nan")
    px = statistics.median(d["close"].to_list())
    est = _scale_from_amount(d.select(["amount", "volume", "close"]).to_dicts())
    if est is None:
        # 样本 < _SCALE_MIN_BARS: 生产端也不猜, 判据端同样无判别力。
        return float("nan"), float("nan"), 0, px
    scale, tol, n = est
    if not scale:
        return float("nan"), float("nan"), 0, px
    # 生产端的 scale 是"乘回原始价的系数"(= 1/s); k_min 沿用旧语义 (= s)。
    # 两者互为倒数, 小偏离下一阶等价, 阈值因此可以逐字共用。
    return 1.0 / scale, tol, n, px


def recalibrate_daily() -> pl.DataFrame:
    """全量日线: symbol/date/close/raw_close/amount/volume, 带 k_daily 与 k_hat。"""
    frames = []
    for p in sorted((DATA / "kline_daily_enriched").glob("date=*/part.parquet")):
        day = p.parent.name.split("=")[1]
        try:
            d = pl.read_parquet(
                p, columns=["symbol", "close", "raw_close", "amount", "volume"]
            )
        except Exception:
            continue
        frames.append(d.with_columns(pl.lit(day).str.to_date().alias("date")))
    if not frames:
        return pl.DataFrame()
    b = pl.concat(frames)
    return b.filter(
        (pl.col("close") > 0) & (pl.col("raw_close") > 0)
        & (pl.col("volume") > 0) & (pl.col("amount") > 0)
    ).with_columns(
        (pl.col("close") / pl.col("raw_close")).alias("k_daily"),
        (pl.col("amount") / (pl.col("volume") * 100.0) / pl.col("close")).alias("k_hat"),
    ).filter(
        (pl.col("k_daily") > 0.5) & (pl.col("k_daily") < 1.5)
        & (pl.col("k_hat") > 0.5) & (pl.col("k_hat") < 1.5)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", action="store_true", help="对当日分钟做注入负控")
    ap.add_argument("--day", default=None, help="只审这一天")
    args = ap.parse_args()

    # ---- 1. 标定: 日线表 k_daily 是真值 ----
    b = recalibrate_daily()
    if b.is_empty():
        print("日线表为空")
        return 2
    n_all = b.height
    adj = b.filter((pl.col("k_daily") - 1).abs() > DEGEN)
    xs = [math.log(v) for v in adj["k_daily"].to_list()]
    ys = [math.log(v) for v in adj["k_hat"].to_list()]
    slope, r2, n = _ols(xs, ys)
    print("== 1. 标定 (日线, k_daily 为真值) ==")
    print(f"  规模: {n_all:,} 行 / {n_all and adj.height:,} 行调权 ({adj.height / n_all * 100:.1f}%)")
    print(f"  k_hat ~ k_daily: slope={slope:.4f}  R2={r2:.4f}  (期望 slope=-1)")
    if not (0.9 < r2 < 1.0 and -1.1 < slope < -0.9):
        print("  ** 工具失效: 成交额/价格不再线性反映复权系数, 后面所有读数作废 **")
        return 1

    # ---- 2. 分钟库逐 symbol-day ----
    days = sorted(p.parent.name.split("=")[1]
                  for p in (DATA / "kline_minute").glob("date=*/part.parquet"))
    if args.day:
        days = [d for d in days if d == args.day]
    rows = []
    for day in days:
        p = DATA / "kline_minute" / f"date={day}" / "part.parquet"
        m = pl.read_parquet(p)
        for sym, sub in m.group_by("symbol"):
            km, tol, cnt, px = k_min_of(sub)
            if km != km:
                continue
            rows.append({"day": day, "symbol": sym[0], "k_min": km, "tol": tol, "n": cnt, "px": px})
    if not rows:
        print("分钟库为空")
        return 2
    g = pl.DataFrame(rows)

    join = b.select(["symbol", "date", "k_daily"]).rename({"date": "day"})
    g = g.with_columns(pl.col("day").str.to_date()).join(join, on=["symbol", "day"], how="left")
    # 事件表兜底: 新除权日一进入 adj_factor, 昨日口径立刻可判, 不必等 enriched 重算。
    tab = {d: {r["symbol"]: r["k_tab"] for r in k_from_events(d).iter_rows(named=True)}
           for d in g["day"].unique().to_list()}
    g = g.with_columns(
        pl.struct(["symbol", "day"]).map_elements(
            lambda s: tab.get(s["day"], {}).get(s["symbol"]), return_dtype=pl.Float64,
        ).alias("k_tab")
    )
    g = g.with_columns(
        # 事件表系数是「未来事件」的乘积, 正是通达信那种「锚定下一个除权日」的口径;
        # 没有未来事件时退回 enriched 的存量系数 (它反映的是写入时的锚定日)。
        pl.coalesce([pl.col("k_tab"), pl.col("k_daily"), pl.lit(1.0)]).alias("k_ref")
    )
    g = g.with_columns(
        # tol 由 k_min_of 从生产端带出 (max(3·se, 5e-4, 半跳)), 这里不再重算。
        ((pl.col("k_min") - 1).abs() <= pl.col("tol")).alias("is_raw"),
        ((pl.col("k_min") / pl.col("k_ref") - 1).abs() <= pl.col("tol")).alias("is_adj"),
        ((pl.col("k_ref") - 1).abs() <= pl.col("tol")).alias("degen"),
    )

    print("\n== 2. 分钟库裁决 ==")
    print(f"  symbol-day: {g.height}  只: {g['symbol'].n_unique()}  日: {g['day'].n_unique()}")
    print(f"  k_min: mean={g['k_min'].mean():.6f} std={g['k_min'].std():.6f} "
          f"min={g['k_min'].min():.6f} max={g['k_min'].max():.6f}")
    print(f"  判别阈 tol (生产端同源: max(3·se, 5e-4, 半跳)): "
          f"中位 {g['tol'].median():.6f}  最大 {g['tol'].max():.6f}")
    n_fb = g.filter((pl.col("k_daily") - 1).abs() <= DEGEN).filter(pl.col("k_tab").is_not_null()).height
    print(f"  事件表兜底命中: {n_fb} 个 symbol-day (k_daily 退化, 改用事件表系数)")

    n_degen = g.filter(pl.col("degen")).height
    n_testable = g.height - n_degen
    n_raw = g.filter(~pl.col("degen") & pl.col("is_raw")).height
    n_adj = g.filter(~pl.col("degen") & ~pl.col("is_raw") & pl.col("is_adj")).height
    n_bad = n_testable - n_raw - n_adj
    print(f"  退化 (前复权系数==1, 两假设重合): {n_degen}/{g.height}")
    print(f"  可判别样本: {n_testable}  ->  H_raw={n_raw}  H_adj={n_adj}  两不沾={n_bad}")

    worst = g.filter((pl.col("k_min") - 1).abs() > pl.col("tol")).sort(
        (pl.col("k_min") - 1).abs(), descending=True).head(5)
    if worst.height:
        print("  超出各自的 tol 的 symbol-day:")
        for r in worst.iter_rows(named=True):
            print(f"    {r['day']} {r['symbol']} k_min={r['k_min']:.6f} "
                  f"k_ref={r['k_ref']} px={r['px']:.2f} tol={r['tol']:.6f} n={r['n']}")

    # ---- 3. 注入负控 ----
    rc = 0
    if args.inject:
        day = args.day or days[-1]
        m = pl.read_parquet(DATA / "kline_minute" / f"date={day}" / "part.parquet")
        base, _, _, _ = k_min_of(m)
        print(f"\n== 3. 注入负控 ({day}) ==")
        print(f"  基线 k_min = {base:.6f}")
        for f in (0.9958, 0.9990, 0.9999):
            m2 = m.with_columns([(pl.col(c) * f).alias(c) for c in ("open", "high", "low", "close")])
            k2, _, _, _ = k_min_of(m2)
            exp = (k2 / base - 1) * 100
            print(f"  注入 x{f}: k_min={k2:.6f}  相对基线 {exp:+.4f}%  "
                  f"(期望 {(f - 1) * 100:+.4f}%)")
            if abs(exp - (f - 1) * 100) > 0.02:
                print("    ** 负控未复现: 判据对已知缩放不敏感 **")
                rc = 1

    # ---- 4. 结论 ----
    print("\n== 4. 结论 ==")
    if n_testable == 0:
        print(f"  无判别力: {g.height} 个 symbol-day 的前复权系数全为 1, 两假设读数相同。")
        print("  ** 这不是通过 **。要让它有样本, 只能等「次日除权」的标的进入采集宇宙:")
        print("  除权前一日 k_min 应等于 1/(1-d/P) 而不是 1。")
        mv = 8.0  # 全市场每日除权事件中位数 (adj_factor: 10-01 以来 343 个)
        print(f"  按每日 ~{mv:.0f} 个事件估算: 40 只宇宙命中率 {mv * 40 / 5571:.3f}/日 "
              f"(≈每 18 个交易日 1 个); 全市场宇宙 ≈{mv:.0f}/日。")
        return max(rc, 2)
    if n_bad:
        print(f"  ** 判据被打破: {n_bad} 个可判别样本既非原值也非前复权, 口径不一致 **")
        return 1
    if n_adj > n_raw:
        print(f"  分钟库为前复权 (k_min 复现 k_daily): H_adj={n_adj} vs H_raw={n_raw}")
        return 1
    print(f"  分钟库为原始价: H_raw={n_raw} vs H_adj={n_adj}")
    return max(rc, 0)


if __name__ == "__main__":
    sys.exit(main())
