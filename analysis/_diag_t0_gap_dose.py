"""低开→开盘买收盘卖 的剂量反应与容量

## 已确认
T 日 gap 最低 20%：开盘买、收盘卖，日净 **+21.8bp**（12bp 已扣），t=2.98；
按日 demean 后 +22.0bp t=8.68（不是市场反弹）；3 年同号；5 档单调。
65% 的行情在首根 5 分钟 bar 内完成。

## 本轮要排除的四种「假」
1. **不是剂量反应** ⇒ 只是分位数切法的偶然。→ 按 gap 绝对幅度分 10 档看 r3。
2. **容量不够** ⇒ 只在没人要的票上成立。→ 按成交额五分位、按阶层分组。
3. **制度伪影** ⇒ 只来自 20% 涨跌幅的创业板/科创板，或跌停不可成交。
   → 按板块拆开；剔除 |gap|>9.5%（可能跌停/一字）。
4. **不可执行的夏普** ⇒ 日度波动太大，均值靠少数日。→ 日度序列的 Sharpe、
   最大单日贡献占比、去掉最好 5 天的均值。

## 口径
r3 = 收盘/开盘 − 1 − 12bp。gap = 开盘/昨收 − 1（09:25 可知）。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
B = 1000
COST = 12.0 / 1e4


def dstat(v: np.ndarray, seed: int = 11) -> dict:
    if len(v) < 30:
        return {}
    rng = np.random.default_rng(seed)
    bs = v[rng.integers(0, len(v), (B, len(v)))].mean(1)
    return {"bp": float(v.mean() * 1e4), "t": float(v.mean() / bs.std()),
            "sd": float(v.std() * 1e4), "days": int(len(v))}


def main() -> None:
    pan = pl.read_parquet(OUT / "panel.parquet",
                          columns=["symbol", "date", "gap", "level", "damt"])
    pan = pan.with_columns(pl.col("date").cast(pl.String))
    tr = (pl.read_parquet(OUT / "trades.parquet", columns=["symbol", "date", "d", "r3"])
            .filter(pl.col("d") == 0.01).drop("d"))
    df = pan.join(tr, on=["symbol", "date"], how="inner").filter(
        pl.col("gap").is_finite(), pl.col("r3").is_finite())
    print(f"[load] {df.height:,} 票日 / {df['date'].n_unique()} 天")
    out: dict = {"rows": int(df.height)}

    # ---------- 1 剂量反应 ----------
    print("\n" + "=" * 100)
    print("[1] 剂量反应：按 T 日 gap 绝对幅度分 10 档")
    df = df.with_columns(pl.col("gap").qcut(
        10, labels=[f"D{i}" for i in range(1, 11)], allow_duplicates=True).alias("gd"))
    print(f"    {'gap档':<8}{'gap中位':>10}{'开盘->收盘净':>13}{'毛':>8}{'t':>7}{'n':>9}")
    dose = []
    for dv in [f"D{i}" for i in range(1, 11)]:
        s = df.filter(pl.col("gd") == dv)
        g = s.group_by("date").agg(pl.col("r3").mean().alias("a")).sort("date")
        st = dstat(g["a"].to_numpy())
        med = float(s["gap"].median()) * 1e4
        print(f"    {dv:<8}{med:>+10.0f}{st['bp']:>+13.1f}{st['bp'] + 12:>+8.1f}"
              f"{st['t']:>7.2f}{s.height:>9}")
        dose.append({"d": dv, "gap_bp": round(med), "bp": round(st["bp"], 1),
                     "t": round(st["t"], 2)})
    out["dose"] = dose

    # ---------- 2 容量 ----------
    print("\n" + "=" * 100)
    print("[2] 容量：只取 gap 最低 20%，再按成交额/阶层/板块拆")
    s1 = df.filter(pl.col("gd") == "D1")
    s1 = s1.with_columns([
        pl.col("damt").qcut(5, labels=[f"A{i}" for i in range(1, 6)],
                            allow_duplicates=True).over("date").alias("aq"),
        pl.when(pl.col("symbol").str.starts_with("30")).then(pl.lit("创业板"))
        .when(pl.col("symbol").str.starts_with("68")).then(pl.lit("科创板"))
        .when(pl.col("symbol").str.starts_with("8") | pl.col("symbol").str.starts_with("4"))
        .then(pl.lit("北交所"))
        .otherwise(pl.lit("主板")).alias("board")])
    print(f"    {'维度':<22}{'净bp':>9}{'t':>7}{'n':>9}")
    cut = {}
    for q in [f"A{i}" for i in range(1, 6)]:
        s = s1.filter(pl.col("aq") == q)
        st = dstat(s.group_by("date").agg(pl.col("r3").mean().alias("a"))["a"].to_numpy())
        med = float(s["damt"].median()) / 1e8
        print(f"    成交额 {q}（中位{med:.1f}亿）{'':<4}{st['bp']:>+9.1f}{st['t']:>7.2f}"
              f"{s.height:>9}")
        cut[f"amt_{q}"] = {"bp": round(st["bp"], 1), "t": round(st["t"], 2),
                           "med_yi": round(med, 2)}
    for lv in ("core", "active", "background", "silent"):
        s = s1.filter(pl.col("level") == lv)
        if s.height < 2000:
            continue
        st = dstat(s.group_by("date").agg(pl.col("r3").mean().alias("a"))["a"].to_numpy())
        print(f"    阶层 {lv:<16}{st['bp']:>+9.1f}{st['t']:>7.2f}{s.height:>9}")
        cut[f"lv_{lv}"] = {"bp": round(st["bp"], 1), "t": round(st["t"], 2)}
    for b in ("主板", "创业板", "科创板", "北交所"):
        s = s1.filter(pl.col("board") == b)
        if s.height < 2000:
            continue
        st = dstat(s.group_by("date").agg(pl.col("r3").mean().alias("a"))["a"].to_numpy())
        print(f"    板块 {b:<16}{st['bp']:>+9.1f}{st['t']:>7.2f}{s.height:>9}")
        cut[f"bd_{b}"] = {"bp": round(st["bp"], 1), "t": round(st["t"], 2)}
    out["capacity"] = cut

    print("\n    剔除 |gap|>9.5%（可能跌停/一字）后：")
    s2 = s1.filter(pl.col("gap").abs() < 0.095)
    st = dstat(s2.group_by("date").agg(pl.col("r3").mean().alias("a"))["a"].to_numpy())
    print(f"    净 {st['bp']:+.1f}bp t={st['t']:+.2f} n={s2.height}（原 {s1.height}，"
          f"剔除 {1 - s2.height / s1.height:.1%}）")
    out["ex_limit"] = {"bp": round(st["bp"], 1), "t": round(st["t"], 2),
                       "dropped": round(1 - s2.height / s1.height, 4)}

    # ---------- 3 日度序列 ----------
    print("\n" + "=" * 100)
    print("[3] 作为组合的日度序列（每日等权买 gap 最低 20%，收盘卖出）")
    g = s1.group_by("date").agg(
        [pl.col("r3").mean().alias("a"), pl.len().alias("n")]).sort("date")
    v = g["a"].to_numpy()
    st = dstat(v)
    sharpe = v.mean() / v.std() * np.sqrt(245)
    print(f"    日净 {st['bp']:+.1f}bp  日波动 {st['sd']:.0f}bp  "
          f"年化 Sharpe {sharpe:.2f}  持有票数 {g['n'].mean():.0f}")
    top = np.sort(v)[-5:]
    print(f"    去掉最好 5 天 {((v.sum() - top.sum()) / (len(v) - 5)) * 1e4:+.1f}bp/日"
          f"（原 {st['bp']:+.1f}）")
    dd = np.cumsum(v)
    print(f"    累计 {dd[-1] * 1e4:.0f}bp  峰值回撤 {((np.maximum.accumulate(dd) - dd).max()) * 1e4:.0f}bp")
    out["portfolio"] = {"bp": round(st["bp"], 1), "t": round(st["t"], 2),
                        "sd": round(st["sd"], 1), "sharpe": round(float(sharpe), 2),
                        "cum_bp": round(float(dd[-1] * 1e4)), "n_med": int(g["n"].median())}

    p = ROOT / "analysis" / "_diag_t0_gap_dose.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
