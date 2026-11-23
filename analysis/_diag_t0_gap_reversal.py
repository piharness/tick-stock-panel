"""低开→开盘买收盘卖 的 +21bp 是市场反弹还是个股信号？走完四道关

## 待检对象
`_diag_t0_open_anchor.py` 的 G1：T 日 gap 五分位 Q1（中位 −67bp）上
「开盘买、收盘卖」日净 **+21.4bp**，t=**2.98**，且五分位近乎单调（+21.4/+1.4/−5.8/−10.4/−9.2）。

## 四道关（任一不过 ⇒ 该 +21bp 不可用）
1. **市场中性**：按日 demean（减去当日等权 r3）。若 Q1 的超额消失 ⇒ 赚的是
   「低开日的市场反弹」，是 **n_eff = 低开日数** 的择时押注，不是个股信号。
2. **时点分解**：r3 = 开盘→收盘。拆成 09:30→09:35（首根 bar 内）与 09:35→收盘。
   若九成落在首根 bar 内 ⇒ 要求「开盘价成交 + 5 分钟内卖出」，实盘不可复制。
3. **分年/分半年同号**：效应衰减到 0 的序列不能当策略。
4. **少数日驱动**：看日度收益分布的 p5/中位/正日占比，而不只是均值。
   （均值 ≫ 中位 ⇒ 少数日）。

## 口径
r3 = 收盘/开盘 − 1 − 12bp（12bp 往返成本已扣）。成交价：开盘 = 首根 bar 的 open，
收盘 = 末根 bar 的 close。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_probe_t0"
B = 1000
COST = 12.0 / 1e4
START = "2024-11-06"


def build_fb() -> pl.DataFrame:
    """首根 bar 的 收盘/开盘 （09:30->09:35）。"""
    f = OUT / "first_bar.parquet"
    if f.exists():
        return pl.read_parquet(f)
    days = sorted(d.split("=", 1)[1] for d in os.listdir(DATA / "kline_5min")
                  if d.startswith("date=") and d.split("=", 1)[1] >= START)
    fr = []
    for d in days:
        p = DATA / "kline_5min" / f"date={d}" / "part.parquet"
        if not p.exists():
            continue
        m = (pl.read_parquet(p, columns=["symbol", "datetime", "open", "close", "volume"])
             .filter(pl.col("volume") > 0, pl.col("close") > 0)
             .sort(["symbol", "datetime"]))
        g = m.group_by("symbol").agg([
            pl.col("close").first().alias("fc"),
            pl.col("open").first().alias("fo"),
            pl.len().alias("nbar")]).filter(pl.col("nbar") >= 47)
        fr.append(g.with_columns(pl.lit(d, dtype=pl.String).alias("date")))
    df = pl.concat(fr).with_columns(
        (pl.col("fc") / pl.col("fo") - 1).alias("fb")).drop(["fc", "fo", "nbar"])
    df.write_parquet(f)
    print(f"[fb] wrote {f} {df.shape}")
    return df


def stat(v: np.ndarray, seed: int = 11) -> dict:
    if len(v) < 30:
        return {}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(v), (B, len(v)))
    bs = v[idx].mean(1)
    return {"bp": float(v.mean() * 1e4), "t": float(v.mean() / bs.std()),
            "days": int(len(v)), "p5": float(np.percentile(v, 5) * 1e4),
            "med": float(np.median(v) * 1e4), "pos": float((v > 0).mean())}


def main() -> None:
    fb = build_fb()
    pan = (pl.read_parquet(OUT / "panel.parquet",
                           columns=["symbol", "date", "gap", "level"])
             .with_columns(pl.col("date").cast(pl.String)))
    tr = (pl.read_parquet(OUT / "trades.parquet", columns=["symbol", "date", "d", "r3"])
            .filter(pl.col("d") == 0.01).drop("d"))
    df = (pan.join(tr, on=["symbol", "date"], how="inner")
             .join(fb, on=["symbol", "date"], how="inner")
             .filter(pl.col("gap").is_finite(), pl.col("r3").is_finite()))
    # 09:35 -> 收盘（r3 的剩余部分）
    df = df.with_columns(
        ((1 + pl.col("r3") + COST) / (1 + pl.col("fb")) - 1 - COST).alias("rest"))
    df = df.with_columns(
        pl.col("gap").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                           allow_duplicates=True).over("date").alias("gq"),
        # 当日等权 r3（市场口径）
        pl.col("r3").mean().over("date").alias("mkt"))
    df = df.with_columns((pl.col("r3") - pl.col("mkt")).alias("exc"))
    print(f"[load] {df.height:,} 票日 / {df['date'].n_unique()} 天   "
          f"首根 bar 均值 {df['fb'].mean() * 1e4:+.1f}bp")

    out: dict = {"rows": int(df.height)}
    print("\n" + "=" * 104)
    print("[1] 市场中性：Q1 的 +21bp 有多少是当日市场反弹")
    print(f"    {'gap档':<14}{'原始':>9}{'t':>7}{'日超额':>9}{'t':>7}{'市场':>8}"
          f"{'n':>9}")
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        s = df.filter(pl.col("gq") == q)
        g = s.group_by("date").agg([pl.col("r3").mean().alias("a"),
                                    pl.col("exc").mean().alias("b"),
                                    pl.col("mkt").first().alias("m")]).sort("date")
        a, b = stat(g["a"].to_numpy()), stat(g["b"].to_numpy())
        print(f"    {q:<14}{a['bp']:>+9.1f}{a['t']:>7.2f}{b['bp']:>+9.1f}{b['t']:>7.2f}"
              f"{g['m'].mean() * 1e4:>+8.1f}{a['days']:>9}")
        out[q] = {"raw": a, "exc": b, "mkt": float(g["m"].mean() * 1e4)}

    print("\n" + "=" * 104)
    print("[2] 时点分解：Q1 的行情发生在 09:30-09:35 内，还是 09:35 之后")
    print(f"    {'gap档':<14}{'开盘->09:35':>13}{'09:35->收盘':>13}{'合计(毛)':>10}")
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        s = df.filter(pl.col("gq") == q)
        print(f"    {q:<14}{s['fb'].mean() * 1e4:>+13.1f}{s['rest'].mean() * 1e4:>+13.1f}"
              f"{(s['r3'].mean() + COST) * 1e4:>+10.1f}")
    out["decomp"] = {q: {"fb": float(df.filter(pl.col("gq") == q)["fb"].mean() * 1e4),
                         "rest": float(df.filter(pl.col("gq") == q)["rest"].mean() * 1e4)}
                     for q in ("Q1", "Q2", "Q3", "Q4", "Q5")}

    print("\n" + "=" * 104)
    print("[3] Q1 稳定性 + 少数日驱动")
    s1 = df.filter(pl.col("gq") == "Q1")
    g = s1.group_by("date").agg([pl.col("r3").mean().alias("a"),
                                 pl.col("exc").mean().alias("b")]).sort("date")
    for nm in ("a", "b"):
        v = g[nm].to_numpy()
        st = stat(v)
        print(f"    {nm}  日净 {st['bp']:+.1f}bp t={st['t']:+.2f} 中位 {st['med']:+.1f} "
              f"p5 {st['p5']:+.0f} 正日 {st['pos']:.1%}")
    ds = g["date"].to_list()
    print(f"    {'区间':<12}{'原始':>9}{'t':>7}{'超额':>9}{'t':>7}{'days':>7}")
    segs = [("前半", ds[:len(ds) // 2]), ("后半", ds[len(ds) // 2:])]
    for y in ("2024", "2025", "2026"):
        segs.append((y, [d for d in ds if d.startswith(y)]))
    parts = {}
    for nm, sub in segs:
        z = g.filter(pl.col("date").is_in(sub))
        if z.height < 20:
            continue
        a, b = stat(z["a"].to_numpy()), stat(z["b"].to_numpy())
        print(f"    {nm:<12}{a['bp']:>+9.1f}{a['t']:>7.2f}{b['bp']:>+9.1f}{b['t']:>7.2f}"
              f"{a['days']:>7}")
        parts[nm] = {"raw": round(a["bp"], 1), "t_raw": round(a["t"], 2),
                     "exc": round(b["bp"], 1), "t_exc": round(b["t"], 2)}
    out["q1_parts"] = parts

    # 去掉最极端的 1% 日
    v = g["a"].to_numpy()
    lo, hi = np.percentile(v, [1, 99])
    print(f"    1/99 缩尾日度均值 {np.clip(v, lo, hi).mean() * 1e4:+.1f}bp "
          f"（原始 {v.mean() * 1e4:+.1f}bp）")

    p = ROOT / "analysis" / "_diag_t0_gap_reversal.json"
    p.write_text(__import__("json").dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
