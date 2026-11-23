"""用纯日线口径重算 gap，复核「低开→开盘买收盘卖」

## 为什么要重算
`panel.parquet` 的 `gap = 5min首根open / 日线前收 - 1`。而 `data/kline_5min`
有 12% 的交易日存在「当日逐标的常数缩放」（MEMORY 已记录，最严重 2026-10-23 达 97.4%）。
缩放因子 c 标的不一 ⇒ 这些日子 gap 被 c 污染，D10 的中位 gap 被推到 +33%，
在 10%/20% 涨跌停制度下**不可能** ⇒ 分位数两端混进了纯噪声。

## 干净口径
`gap_d = 日线 open / 日线前收 - 1`（两列都来自 kline_daily_enriched，原始价）
`r3    = 5min 末根close / 5min 首根open - 1`（日内比值，缩放因子约掉）
两者相乘/相除无跨日跨源混用。

## 判据
若低开档的 +36.7bp 在干净 gap 下显著萎缩 ⇒ 原结论是 gap 测量噪声挑出来的伪影。
若维持 ⇒ 低开反转成立，且与 5min 数据缺陷无关。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_probe_t0"
B = 1000
COST = 12.0 / 1e4


def dstat(v: np.ndarray, seed: int = 11) -> dict:
    if len(v) < 30:
        return {}
    rng = np.random.default_rng(seed)
    bs = v[rng.integers(0, len(v), (B, len(v)))].mean(1)
    return {"bp": float(v.mean() * 1e4), "t": float(v.mean() / bs.std()),
            "sd": float(v.std() * 1e4), "days": int(len(v))}


def build() -> pl.DataFrame:
    f = OUT / "panel_daily_gap.parquet"
    if f.exists():
        print(f"[cache] {f}")
        return pl.read_parquet(f)
    rows = []
    for d in sorted(p.name.split("=", 1)[1] for p in (DATA / "kline_daily_enriched").iterdir()
                    if p.name.startswith("date=")):
        if d < "2024-11-06":
            continue
        p = DATA / "kline_daily_enriched" / f"date={d}" / "part.parquet"
        if not p.exists():
            continue
        rows.append(pl.read_parquet(p).select(["symbol", "open", "close", "amount"])
                    .with_columns(pl.lit(d, dtype=pl.String).alias("date")))
    dl = pl.concat(rows).sort(["symbol", "date"])
    dl = dl.with_columns([
        pl.col("close").shift(1).over("symbol").alias("pc"),
        pl.col("open").alias("do"), pl.col("close").alias("dc")])
    dl = dl.drop_nulls("pc").filter(pl.col("pc") > 0)
    # 日历相邻性：跨停牌（上一行不是上一交易日）剔除
    dl = dl.with_columns(pl.col("date").str.to_date().alias("dt"))
    cal = sorted(dl["dt"].unique().to_list())
    rank = {d: i for i, d in enumerate(cal)}
    dl = dl.with_columns(pl.col("dt").replace_strict(rank, return_dtype=pl.Int32).alias("rn"))
    dl = dl.with_columns(pl.col("rn").shift(1).over("symbol").alias("rn1"))
    dl = dl.filter(pl.col("rn1").is_not_null(), (pl.col("rn") - pl.col("rn1")) == 1)
    out = dl.with_columns(
        (pl.col("do") / pl.col("pc") - 1).alias("gap_d")).select(
        ["symbol", "date", "gap_d", "amount"])
    out.write_parquet(f)
    print(f"[build] {f} {out.shape}")
    return out


def main() -> None:
    gd = build()
    tr = (pl.read_parquet(OUT / "trades.parquet", columns=["symbol", "date", "d", "r3"])
            .filter(pl.col("d") == 0.01).drop("d"))
    pan = pl.read_parquet(OUT / "panel.parquet", columns=["symbol", "date", "gap"])
    pan = pan.with_columns(pl.col("date").cast(pl.String))
    df = (gd.join(tr, on=["symbol", "date"], how="inner")
            .join(pan, on=["symbol", "date"], how="left")
            .filter(pl.col("gap_d").is_finite(), pl.col("r3").is_finite()))
    print(f"[load] {df.height:,} 票日 / {df['date'].n_unique()} 天")

    print("\n" + "=" * 100)
    print("[1] 旧 gap 与 干净 gap 的差异（差多少 = 5min 缩放污染多少）")
    both = df.drop_nulls("gap").with_columns((pl.col("gap") - pl.col("gap_d")).alias("err"))
    print(f"    相关系数 {both.select(pl.corr('gap', 'gap_d')).item():.4f}   "
          f"偏差中位 {both['err'].median() * 1e4:+.1f}bp  "
          f"|偏差|>1% 占 {(both['err'].abs() > 0.01).mean():.2%}")
    print(f"    旧 gap p1/p50/p99 "
          f"{np.percentile(both['gap'].to_numpy(), [1, 50, 99]) * 100}")
    print(f"    新 gap p1/p50/p99 "
          f"{np.percentile(both['gap_d'].to_numpy(), [1, 50, 99]) * 100}")

    BINS = [(-9.99, -0.05), (-0.05, -0.03), (-0.03, -0.02), (-0.02, -0.01),
            (-0.01, -0.005), (-0.005, -0.0001), (-0.0001, 0.0001), (0.0001, 0.005),
            (0.005, 0.01), (0.01, 0.02), (0.02, 0.20), (0.20, 99.0)]
    NM = ["<=-5%", "-5~-3", "-3~-2", "-2~-1", "-1~-0.5", "-0.5~0", "=0",
          "0~0.5", "0.5~1", "1~2", ">=2%", ">20%"]

    def bkt(col: str = "gap_d") -> pl.Expr:
        e = pl.when(pl.col(col) <= BINS[0][1]).then(pl.lit(NM[0]))
        for (a, b), nm in zip(BINS[1:], NM[1:]):
            e = e.when((pl.col(col) > a) & (pl.col(col) <= b)).then(pl.lit(nm))
        return e.otherwise(pl.lit(None))

    print("\n" + "=" * 100)
    print("[2] 干净 gap 显式分档 x 开盘->收盘（r3 已扣 12bp）")
    df = df.with_columns(bkt().alias("gb"))
    print(f"    {'档':<10}{'gap中位':>10}{'净bp':>9}{'毛':>8}{'t':>7}{'n':>9}")
    dose = []
    for nm in NM:
        s = df.filter(pl.col("gb") == nm)
        if s.height < 2000:
            continue
        g = s.group_by("date").agg(pl.col("r3").mean().alias("a")).sort("date")
        st = dstat(g["a"].to_numpy())
        med = float(s["gap_d"].median()) * 1e4
        print(f"    {nm:<10}{med:>+10.0f}{st['bp']:>+9.1f}{st['bp'] + 12:>+8.1f}"
              f"{st['t']:>7.2f}{s.height:>9}")
        dose.append({"bin": nm, "gap_bp": round(med), "bp": round(st["bp"], 1),
                     "t": round(st["t"], 2), "n": s.height})
    out = {"dose": dose}

    for THR in (-0.02, -0.03):
        print("\n" + "=" * 100)
        print(f"[3] 组合口径：gap_d <= {THR:.0%} 的全部股票，每日等权买开盘卖收盘")
        s1 = df.filter(pl.col("gap_d") <= THR)
        g = s1.group_by("date").agg([pl.col("r3").mean().alias("a"),
                                     pl.len().alias("n")]).sort("date")
        v = g["a"].to_numpy()
        st = dstat(v)
        print(f"    日净 {st['bp']:+.1f}bp  sd {st['sd']:.0f}bp  t={st['t']:+.2f}  "
              f"Sharpe {v.mean() / v.std() * np.sqrt(245):.2f}  "
              f"持仓中位 {g['n'].median():.0f} 只  交易 {s1.height:,} 票日")
        ds = g["date"].to_list()
        print(f"    {'区间':<10}{'净bp':>9}{'t':>7}{'days':>7}")
        for nm, sub in (("前半", ds[:len(ds) // 2]), ("后半", ds[len(ds) // 2:]),
                        ("2024", [d for d in ds if d.startswith("2024")]),
                        ("2025", [d for d in ds if d.startswith("2025")]),
                        ("2026", [d for d in ds if d.startswith("2026")])):
            z = g.filter(pl.col("date").is_in(sub))
            if z.height < 20:
                continue
            s2 = dstat(z["a"].to_numpy())
            print(f"    {nm:<10}{s2['bp']:>+9.1f}{s2['t']:>7.2f}{s2['days']:>7}")
        out[f"port_{THR}"] = {"bp": round(st["bp"], 1), "t": round(st["t"], 2),
                              "sd": round(st["sd"], 1), "n_day": int(g["n"].median())}
        print("    按成交额五分位：")
        s1b = s1.with_columns(pl.col("amount").qcut(
            5, labels=[f"A{i}" for i in range(1, 6)],
            allow_duplicates=True).over("date").alias("aq"))
        for q in [f"A{i}" for i in range(1, 6)]:
            z = s1b.filter(pl.col("aq") == q)
            if z.height < 1000:
                continue
            s2 = dstat(z.group_by("date").agg(pl.col("r3").mean().alias("a"))["a"].to_numpy())
            print(f"      {q}（中位 {z['amount'].median() / 1e8:.1f}亿）  {s2['bp']:>+7.1f}bp "
                  f"t={s2['t']:>5.2f}  n={z.height}")

    print("\n" + "=" * 100)
    print("[4] 同一批样本：旧 gap 与干净 gap 的同一档位对比")
    for col, nm in (("gap", "旧 gap(5min)"), ("gap_d", "干净 gap(日线)")):
        z = df.drop_nulls(col).with_columns(bkt(col).alias("gg"))
        z = z.filter(pl.col("gg") == "<=-5%")
        if z.height < 500:
            continue
        s2 = dstat(z.group_by("date").agg(pl.col("r3").mean().alias("a"))["a"].to_numpy())
        print(f"    {nm:<18}gap<=-5%  {s2['bp']:>+9.1f}bp  t={s2['t']:>5.2f}  n={z.height}")

    p = ROOT / "analysis" / "_diag_t0_gap_clean.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
