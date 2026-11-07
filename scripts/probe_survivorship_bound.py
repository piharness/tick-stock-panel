"""生存偏差的**可执行**上界：用真实面板做合成退市注入，而不是猜一个 −60bp。

背景（2026-11-08 复核）：
  `data/cache/fuyao/daily_k__20261104.parquet` 的 5577 只里，5571 只的最后交易日
  就是 dump 末日；`instruments.parquet`（as_of 2026-11-05）里 155 只名字带「退」
  的标的一只价格都没有。也就是说：**样本是「活到 2026-11-04 的快照」**，
  低换手/小盘角落正是退市最密集的地方。

上一轮把这个缺口记成一个点估计（「250 只退市、50% 落进低换手层、末日 −40%
⇒ 偏差约 −60bp/60d」）。这个数有两个毛病：
  1. 它把「末日 −40%」当成了整段生命的平均收益。层溢价是**逐日截面的时序均值**，
     一只票一生只有最后一个 60 日块是崩的，前面几十块不是。
  2. 它没有说权重。缺口的权重是 name-day 占比，不是标的数占比。

本脚本把它换成两个可复现的东西：
  A 机制梯：给定「缺口组的层内 name-day 权重 W」与「缺口组相对层同侪的偏离 G」，
    偏差 = W × G。给一张表，让读者自己代入假设，而不是替读者选一个。
  B 合成注入：从真实面板随机抽 K 只**存活**标的，把他们的历史在随机时点截断，
    末段 h 日按线性斜坡崩到 −L，当作「退市股」补回面板，重算层溢价。
    跑 R 次取分布。两种选择模式：
      random —— 保留被抽中标的自己的成交额（按无条件比例进层）⇒ 下界
      forced —— 成交额压到 5%（必然进层）⇒ 上界
    真值在两者之间。

不做的事：不去补 155 只的真实价格（本地 312/313 无副本，见 universe_gap.json），
所以这不是「重跑 235bp」，是「给 235bp 一个用真实面板算出来的括号」。

输出：data/.scratch/survivorship_bound.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_decile_net as P  # noqa: E402

ROOT = P.ROOT
INST = ROOT / "data/instruments/instruments.parquet"
OUT = P.OUT
H = 60
FRAC = 0.2
COST_BP = 38.0
SEED = 0


def census() -> dict:
    """样本是不是「存活快照」：逐只的最后交易日分布。"""
    g = (pl.scan_parquet(P.DUMP).select(["thscode", "date_ms"])
         .group_by("thscode").agg(pl.col("date_ms").max().alias("last")).collect())
    mx = int(g["last"].max())
    late = g.filter(pl.col("last") < mx)
    inst = pl.read_parquet(INST).select(["symbol", "name"])
    gap = inst.join(g.select("thscode"), left_on="symbol", right_on="thscode", how="anti")
    def classify(name: str) -> str:
        if "退" in name:
            return "delisted"
        if name.startswith("S*ST"):
            return "s_star_st"
        if name.startswith("*ST"):
            return "star_st"
        if name.startswith(("ST", "SST")):
            return "st"
        return "other"

    cls = gap.with_columns(
        pl.col("name").map_elements(classify, return_dtype=pl.Utf8).alias("cls"))
    return {"instruments": inst.height, "in_dump": g.height, "missing": gap.height,
            "delisted": int((cls["cls"] == "delisted").sum()),
            "st_like": int(cls["cls"].is_in(["st", "star_st", "s_star_st"]).sum()),
            "other": int((cls["cls"] == "other").sum()),
            "n_ended_before_max": late.height,
            # date_ms 是 UTC 零点；+8h 才是交易日的本地日期（口径与其余探针一致）
            "max_last_date": str(np.datetime64(mx + 8 * 3600 * 1000, "ms").astype("datetime64[D]")),
            "missing_weight": gap.height / inst.height}


def mechanism_ladder() -> list[dict]:
    """偏差 = W × G，W 是缺口组的层内 name-day 权重。表的读法见 docstring。"""
    out = []
    for w in (0.01, 0.03, 0.05, 0.10, 0.20):
        for gap in (-50, -100, -200, -400, -800):
            out.append({"layer_weight": w, "gap_bp": gap, "bias_bp": round(w * gap, 2)})
    return out


def bucket_premium(df: pl.DataFrame) -> tuple[float, int]:
    """低成交额 20% 层的层溢价（bp）与决策日数。口径与 probe_width_frontier 一致。"""
    fwd = f"fwd{H}"
    d = df.filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                  & (pl.col("turnover") > 0))
    d = d.with_columns(
        (((pl.col("turnover").rank("ordinal").over("date") - 1)
          / pl.col("turnover").count().over("date")) < FRAC).alias("in_b"))
    g = (d.group_by("date").agg(
        pl.col(fwd).filter(pl.col("in_b")).mean().alias("bmu"),
        pl.col(fwd).mean().alias("pmu")).sort("date"))
    lay = (g["bmu"] - g["pmu"]).to_numpy()
    fin = lay[np.isfinite(lay)]
    return float(1e4 * fin.mean()), int(len(fin))


def inject(base: pl.DataFrame, k: int, crash: float, mode: str,
           rng: np.random.Generator) -> pl.DataFrame:
    """造 k 只「退市股」：抽真实标的的历史，截断，末段 h 日线性崩到 crash。

    只用面板已有的列（mom/fwd60/turnover/volume），不碰价格——层只需要这些。
    """
    syms = base["thscode"].unique().to_numpy()
    pick = rng.choice(syms, size=k, replace=False)
    fwd = f"fwd{H}"
    donors = base.filter(pl.col("thscode").is_in(pick)).sort(["thscode", "date"])
    by = {key[0]: v for key, v in donors.partition_by("thscode", as_dict=True).items()}
    parts = []
    for i, s in enumerate(pick):
        d = by.get(s)
        if d is None or d.height < 3 * H:
            continue
        n = d.height
        cut = int(rng.integers(2 * H, n))          # 在 cut 处「退市」
        d = d.head(cut)
        rows = d.height
        v = d[fwd].to_numpy().copy()
        # 末段：距末日 k 行时为 −crash×(1−k/H)，k=0 正好是 −crash。
        # 只压不抬（np.minimum），所以 donor 本来就跌得更狠时保留原值。
        kk = rows - 1 - np.arange(rows)
        mask = kk < H
        v[mask] = np.minimum(v[mask], -crash * (1.0 - kk[mask] / H))
        tu = d["turnover"].to_numpy()
        if mode == "forced":
            tu = tu * 0.05
        parts.append(pl.DataFrame({
            "thscode": [f"SYN{i:04d}"] * rows,
            "date": d["date"].to_list(),
            "mom": d["mom"].to_list(),
            fwd: v.tolist(),
            "turnover": tu.tolist(),
            "volume": d["volume"].to_list()}))
    if not parts:
        return base
    return pl.concat([base] + parts, how="vertical_relaxed")


def main() -> None:
    rng = np.random.default_rng(SEED)
    cen = census()
    print(f"宇宙 {cen['instruments']} 只，dump {cen['in_dump']} 只，缺口 {cen['missing']}"
          f"（其中名字带「退」{cen['delisted']} 只，ST 族 {cen['st_like']} 只，"
          f"其他 {cen['other']} 只）")
    print(f"逐只最后交易日 ≠ {cen['max_last_date']} 的只有 {cen['n_ended_before_max']} 只"
          f" → 这是存活快照，不是 PIT 面板")

    base = P.load_panel().select(["thscode", "date", "mom", f"fwd{H}", "turnover", "volume"])
    base_bp, days = bucket_premium(base)
    print(f"\n基线（未注入）：层溢价 {base_bp:+.1f}bp，{days} 个决策日")

    rows = []
    for mode in ("random", "forced"):
        for k in (155, 250, 400):
            for crash in (0.0, 0.2, 0.4, 0.6, 1.0):
                deltas = []
                for rep in range(3):
                    aug = inject(base, k, crash, mode, rng)
                    bp, _ = bucket_premium(aug)
                    deltas.append(bp - base_bp)
                d = np.array(deltas)
                rows.append({"mode": mode, "k": k, "crash": crash,
                             "delta_bp_mean": float(d.mean()),
                             "delta_bp_min": float(d.min()),
                             "delta_bp_max": float(d.max()),
                             "level_bp": float(base_bp + d.mean())})
                print(f"  {mode:6s} k={k:3d} 末日崩 {crash:.0%} → Δ {d.mean():+7.1f}bp"
                      f"（{d.min():+.1f}…{d.max():+.1f}）  水平 {base_bp + d.mean():+7.1f}bp")

    rep = {"h": H, "frac": FRAC, "cost_bp": COST_BP, "seed": SEED, "reps": 3,
           "census": cen, "baseline_bp": base_bp, "baseline_days": days,
           "mechanism_ladder": mechanism_ladder(), "injection": rows,
           "definitions": {
               "census.n_ended_before_max": "dump 里最后交易日早于全局末日的标的数。0 附近 ⇒ 快照",
               "mechanism_ladder": "bias_bp = layer_weight × gap_bp；W 是缺口组的层内 name-day 权重",
               "injection.mode=random": "注入标的保留自身成交额 ⇒ 按无条件比例进层（下界）",
               "injection.mode=forced": "成交额 ×0.05 ⇒ 必然进层（上界）",
               "delta_bp": "注入后层溢价 − 基线层溢价；负值表示存活偏差高估了溢价"}}
    (OUT / "survivorship_bound.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/survivorship_bound.json")


if __name__ == "__main__":
    sys.exit(main())
