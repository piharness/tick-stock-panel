"""做T × 阶层迁移：两组必须的对照

## 为什么必须做

B 层的 Δ = 迁移组 − **目标等级停留者**，显著为正。但这个对照有两个致命混杂：

1. **新老混杂**：迁移组昨天必然在别的等级，它和「目标等级老居民」之间的差，
   混了「新进者 vs 老居民」，不纯是迁移。
   ⇒ 补 Δ_source = 迁移组 − **源等级停留者**。若 Δ_source 也正，说明升格股
   同时跑赢旧同伴和新同伴，这才是真的迁移效应。

2. **流动性混杂**：升格 = 关注度上升 = 成交额上升 = 摩擦下降。
   做T 净收益对冲击项 β 敏感，β 与流动性有关。
   ⇒ 在**同成交额五分位内**重算 Δ。若 Δ 消失，升格只是「变活跃了」的代理。

## 判据
R1 Δ_source > 0 且 t > 2 ⇒ 迁移效应不是新老差异。
R2 同成交额分位内 Δ 中位仍 > 0 且 5 个分位里至少 4 个为正 ⇒ 不是流动性投影。
两者任一不过，则「升格改善做T」降级为「升格股本来就更好做」。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
LEVELS = ["core", "active", "background", "silent"]
CN = {"core": "核心", "active": "活跃", "background": "背景", "silent": "沉默"}
B = 500
MIGS = ["background>active", "active>core", "silent>background", "silent>active",
        "active>background", "core>active", "background>silent", "background>core"]


def build() -> pl.DataFrame:
    p = (pl.read_parquet(OUT / "panel.parquet",
                         columns=["symbol", "date", "level", "ampl", "amt"])
         .sort(["symbol", "date"]))
    p = p.with_columns([
        pl.col("level").shift(1).over("symbol").alias("l1"),
        pl.col("level").shift(2).over("symbol").alias("l2"),
        pl.col("ampl").shift(1).over("symbol").alias("ampl1"),
        pl.col("amt").shift(1).over("symbol").alias("amt1"),
    ])
    p = p.with_columns(
        pl.when(pl.col("l1").is_not_null() & pl.col("l2").is_not_null())
          .then(pl.concat_str([pl.col("l2"), pl.col("l1")], separator=">"))
          .otherwise(None).alias("tr"))
    return (pl.read_parquet(OUT / "trades.parquet").drop("level")
            .join(p.select(["symbol", "date", "tr", "ampl1", "amt1"]),
                  on=["symbol", "date"], how="inner"))


def delta(df: pl.DataFrame, col: str, tr: str, base: str, seed: int = 7):
    a = df.filter((pl.col("tr") == tr) & pl.col(col).is_finite())
    b = df.filter((pl.col("tr") == base) & pl.col(col).is_finite())
    if a.height < 100 or b.height < 100:
        return None
    ga = a.group_by("date").agg([pl.col(col).sum().alias("s"), pl.len().alias("n")])
    gb = b.group_by("date").agg([pl.col(col).sum().alias("s"), pl.len().alias("n")])
    j = ga.join(gb, on="date", how="inner", suffix="_b").sort("date")
    if j.height < 30:
        return None
    sa, na = j["s"].to_numpy(), j["n"].to_numpy()
    sb, nb = j["s_b"].to_numpy(), j["n_b"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(j), (B, len(j)))
    d = (sa[idx].sum(1) / na[idx].sum(1) - sb[idx].sum(1) / nb[idx].sum(1)) * 1e4
    pa, pb = sa.sum() / na.sum() * 1e4, sb.sum() / nb.sum() * 1e4
    se = float(d.std())
    return {"bp_move": pa, "bp_base": pb, "delta": pa - pb,
            "t": (pa - pb) / se if se else float("nan"),
            "n_move": int(na.sum()), "days": int(j.height)}


def main() -> None:
    t = build()
    ok = t.filter(pl.col("tr").is_not_null())
    out: dict = {}
    for dd in (0.01, 0.02):
        s = ok.filter(pl.col("d") == dd)

        print("=" * 96)
        print(f"R1  迁移组 vs 源等级停留者（新老混杂检验）  d={dd}  正T(r1)")
        print(f"  {'迁移':>22}{'迁移组':>9}{'源停留':>9}{'目标停留':>10}"
              f"{'Δ源':>8}{'t源':>7}{'Δ目标':>9}{'t目标':>8}")
        rec = []
        for mv in MIGS:
            src, tgt = mv.split(">")
            ds = delta(s, "r1", mv, f"{src}>{src}")
            dt_ = delta(s, "r1", mv, f"{tgt}>{tgt}")
            if not ds or not dt_:
                continue
            print(f"  {mv.replace('>', '→'):>22}{ds['bp_move']:>9.1f}{ds['bp_base']:>9.1f}"
                  f"{dt_['bp_base']:>10.1f}{ds['delta']:>8.1f}{ds['t']:>7.2f}"
                  f"{dt_['delta']:>9.1f}{dt_['t']:>8.2f}")
            rec.append({"tr": mv, "d_src": round(ds["delta"], 1), "t_src": round(ds["t"], 2),
                        "d_tgt": round(dt_["delta"], 1), "t_tgt": round(dt_["t"], 2),
                        "n": ds["n_move"]})
        out[f"R1_d{dd}"] = rec

        # R2：同成交额五分位内的 Δ_target
        a = s.filter(pl.col("amt1").is_not_null(), pl.col("amt1") > 0)
        a = a.with_columns(pl.col("amt1").qcut(5, labels=[f"A{i}" for i in range(1, 6)],
                                               allow_duplicates=True).alias("aq"))
        print(f"\nR2  同成交额五分位内的 Δ_target（控制流动性混杂）  d={dd}  正T")
        print(f"  {'迁移':>22}" + "".join(f"{'A' + str(i):>9}" for i in range(1, 6))
              + f"{'为正数':>7}")
        rec2 = {}
        for mv in MIGS:
            tgt = mv.split(">")[1]
            vals, pos = [], 0
            for i in range(1, 6):
                r = delta(a.filter(pl.col("aq") == f"A{i}"), "r1", mv, f"{tgt}>{tgt}")
                if r:
                    vals.append(r["delta"])
                    pos += 1 if r["delta"] > 0 else 0
                else:
                    vals.append(float("nan"))
            print(f"  {mv.replace('>', '→'):>22}"
                  + "".join(f"{v:>9.1f}" if np.isfinite(v) else f"{' ':>9}" for v in vals)
                  + f"{pos:>7}")
            rec2[mv] = [round(v, 1) if np.isfinite(v) else None for v in vals]
        out[f"R2_d{dd}"] = rec2
        print()

    p = ROOT / "analysis" / "_diag_citizen_t0_transition_ctrl.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {p}")


if __name__ == "__main__":
    main()
