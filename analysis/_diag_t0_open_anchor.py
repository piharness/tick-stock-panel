"""「高开低走」能不能在开盘那一刻做：锚改用 09:30 开盘价

## 为什么必须单独测

`r1/r2` 的锚是 **09:35 首根 bar 的收盘**。若主力的行为发生在「开盘→收盘」
这一段（高开就出货、低开就吸筹），那么用 09:35 做锚时，这段行情在锚成立前
已经走完，**结构性看不见**。`trades.parquet` 的 r3/r4 恰好是

    r3 = 收盘/开盘 - 1 - 12bp       （开盘买、收盘卖）
    r4 = -(收盘/开盘 - 1) - 12bp    （开盘卖、收盘买回）  = 负T 的开盘锚版本

r4 就是「高开日开盘卖出、收盘买回」的直接实现，集合竞价可成交。

## 判据
G1 按 T 日 gap 五分位，r4 日净 > 0 且 t > 2（高开档）⇒ 「高开低走」可交易。
G2 按 T-1 特征五分位，r3/r4 有格 日净 > 0 且 t > 2 ⇒ 图形可预测开盘→收盘。
G3 与 09:35 锚版本（r1/r2）比较：若开盘锚显著而 09:35 锚不显著 ⇒ 差异来自
   开盘后那 5 分钟，说明信息在 09:30-09:35 兑现。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
B = 1000
COST_BP = 12.0
PRE = ["i1", "f30", "pm", "last30", "eff", "zigzag", "ac1", "n_vwap_r",
       "upvol", "sh_up", "maxbar", "t_hi", "ampl_o", "dip", "rise", "dd_close"]
CN = {"i1": "T-1全天方向", "f30": "T-1早盘", "pm": "T-1下午", "last30": "T-1尾盘",
      "eff": "T-1路径效率", "zigzag": "T-1锯齿度", "ac1": "T-1自相关",
      "n_vwap_r": "T-1穿VWAP", "upvol": "T-1涨量占比", "sh_up": "T-1上影线",
      "maxbar": "T-1单根集中", "t_hi": "T-1高点时点", "ampl_o": "T-1振幅",
      "dip": "T-1回撤", "rise": "T-1上冲", "dd_close": "T-1收盘位置"}


def stat(df: pl.DataFrame, col: str, seed: int = 11) -> dict:
    g = (df.filter(pl.col(col).is_finite())
           .group_by("date").agg([pl.col(col).sum().alias("s"), pl.len().alias("n")])
           .sort("date"))
    if g.height < 30:
        return {}
    s, n = g["s"].to_numpy(), g["n"].to_numpy()
    v = s.sum() / n.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(s), (B, len(s)))
    bs = s[idx].sum(1) / n[idx].sum(1)
    return {"bp": float(v * 1e4), "t": float(v / bs.std()), "days": int(g.height),
            "n": int(n.sum())}


def main() -> None:
    sh = pl.read_parquet(OUT / "shape.parquet")
    pan = (pl.read_parquet(OUT / "panel.parquet",
                           columns=["symbol", "date", "gap", "level"])
             .with_columns(pl.col("date").cast(pl.String)))
    sh = sh.join(pan, on=["symbol", "date"], how="inner").sort(["symbol", "date"])
    sh = sh.with_columns([pl.col(c).shift(1).over("symbol").alias(f"L_{c}") for c in PRE])
    tr = (pl.read_parquet(OUT / "trades.parquet",
                          columns=["symbol", "date", "d", "r3", "r4"])
            .filter(pl.col("d") == 0.01).drop("d"))
    df = sh.join(tr, on=["symbol", "date"], how="inner")
    out: dict = {"cost_bp": COST_BP, "rows": int(df.height)}

    print("=" * 100)
    print("[0] 基准：开盘->收盘（r3 开盘买收盘卖 / r4 开盘卖收盘买回），12bp 已扣")
    for c, nm in (("r3", "开买收卖 正T"), ("r4", "开卖收买 负T")):
        r = stat(df, c)
        print(f"    {nm:<16} 日净 {r['bp']:+7.1f}bp  日毛 {r['bp'] + COST_BP:+7.1f}bp  "
              f"t={r['t']:+5.2f}  n={r['n']}")
        out[nm] = r

    print("\n" + "=" * 100)
    print("[G1] 按 T 日 gap 五分位（09:25 集合竞价后可知）")
    s = df.filter(pl.col("gap").is_finite())
    s = s.with_columns(pl.col("gap").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                                          allow_duplicates=True).over("date").alias("gq"))
    print(f"    {'gap档':<18}{'开买收卖':>10}{'t':>7}{'开卖收买':>10}{'t':>7}{'n':>9}")
    rows = []
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        ss = s.filter(pl.col("gq") == q)
        a, b = stat(ss, "r3"), stat(ss, "r4")
        med = float(ss["gap"].median()) * 1e4
        print(f"    {q + f'({med:+.0f}bp)':<18}{a['bp']:>+10.1f}{a['t']:>7.2f}"
              f"{b['bp']:>+10.1f}{b['t']:>7.2f}{a['n']:>9}")
        rows.append({"q": q, "gap_bp": round(med), "r3": round(a["bp"], 1),
                     "t3": round(a["t"], 2), "r4": round(b["bp"], 1), "t4": round(b["t"], 2)})
    out["gap_q"] = rows

    print("\n" + "=" * 100)
    print("[G2] 按 T-1 特征五分位：开盘->收盘")
    grid = []
    print(f"    {'特征':<14}" + "".join(f"{k:>8}" for k in
                                       ("开买收卖", "t", "开卖收买", "t")))
    for c in PRE:
        ss = df.filter(pl.col(f"L_{c}").is_finite())
        ss = ss.with_columns(pl.col(f"L_{c}").qcut(
            5, labels=[f"Q{i}" for i in range(1, 6)], allow_duplicates=True).over("date").alias("_q"))
        line = []
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            z = ss.filter(pl.col("_q") == q)
            a, b = stat(z, "r3"), stat(z, "r4")
            if a and b:
                line.append(f"{a['bp']:>+8.1f}{a['t']:>8.1f}")
                grid.append({"feat": c, "q": q, "r3": round(a["bp"], 1), "t3": round(a["t"], 2),
                             "r4": round(b["bp"], 1), "t4": round(b["t"], 2),
                             "n": a["n"]})
            else:
                line.append(f"{'-':>16}")
        print(f"    {CN[c]:<14}" + "".join(line))
    out["grid"] = grid

    passes = [g for g in grid if (g["r3"] > 0 and g["t3"] > 2) or (g["r4"] > 0 and g["t4"] > 2)]
    print(f"\n    过 G2（日净>0 且 t>2）的格：{len(passes)}/{len(grid)}")
    for g in sorted(passes, key=lambda z: -max(z["r3"], z["r4"]))[:10]:
        print(f"      {CN[g['feat']]:<14}{g['q']}  开买收卖 {g['r3']:+7.1f}(t={g['t3']:+.2f})  "
              f"开卖收买 {g['r4']:+7.1f}(t={g['t4']:+.2f})  n={g['n']}")
    out["passes"] = passes

    p = ROOT / "analysis" / "_diag_t0_open_anchor.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
