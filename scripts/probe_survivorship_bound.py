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

本脚本把它换成三个可复现的东西：
  A 机制梯：给定「缺口组的层内 name-day 权重 W」与「缺口组相对层同侪的偏离 G」，
    偏差 = W × G。给一张表，让读者自己代入假设，而不是替读者选一个。
  B 两种锚，摆在同一个 `closed_form` 里，但**不是同一个对象**：
      `all_present_pool_mean_bp` —— K 只全在场、收益 = 当日池均值，**构造面板后真跑
        一遍 `layer_premium`**。它是模拟，不是算术；只是没有抽样误差。
      `displacement_identity_bp` —— 按实现的 20% 分位定义推导：注入后层大小变成
        ceil(0.2(N+K))，原层只留下成交额最低的 ceil(0.2(N+K))−K 只，其余被挤出。
        这一条是算术，不经过抽样。
      `identity_bp` —— −K/(层内只数+K) × 溢价。它假设层线性长到 N+K 且原成员全留，
        与实现不符，只作一阶参照，**不许**拿它当「闭式锚」，也不许拿它校验别的数。
    模拟里每只合成标的在随机时点被截断，任一决策日只有约四成在场，所以 0% 崩盘档
    **必然**比「全在场」小一个量级——这两个数不是矛盾，是两个对象。
  C 合成注入：从真实面板随机抽 K 只**存活**标的，把他们的历史在随机时点截断，
    末段 h 日按线性斜坡崩到 −L，当作「退市股」补回面板。两种选择模式：
      random —— 保留被抽中标的自己的成交额（按无条件比例进层）⇒ 下界
      forced —— 成交额压到 5%（**近似**必然进层，实际入层率约九成）⇒ 上界
    真值在两者之间。

三个口径约束（2026-11-08 第二次复核后加）：
  * **抽样必须由种子钉死。** `pl.DataFrame.unique()` 每次调用顺序不同，直接把它喂给
    `rng.choice` 会让 `SEED` 变成装饰品：同一个种子两次运行抽到不同标的。这里先
    `np.sort` 再抽。`test_survivorship_draw_is_deterministic` 钉住这一点。
  * **崩盘档之间必须配对。** 同一 rep 的标的选择与截断时点，在全部 crash 档与两种
    模式下复用（common random numbers）。否则「崩盘效应」的方差里混着「这次抽了谁」。
  * **crash=0 不是纯稀释。** 斜坡在 crash=0 时恒为 0，但 `np.minimum(v, 0)` 仍然把
    末段 60 行的正收益削平。纯稀释看 `truncate_only` 档：同一批标的、同一切断点，
    只把 `clip` 关掉（`clip=False`），与 `injection[forced, crash=0]` 的差就是削平
    这一项自己的贡献。两个档共用 chosen，所以差不是抽样噪声。

对象：层溢价（低换手 20% 层 − 池，对 mom 无依赖）与篮子溢价（层内按 mom 降序取
前 100 − 层均值，产品实际定义）。**篮子的偏差不可识别**：注入标的的 mom 是借来的，
而退市股真实的 mom 分布本地没有；`mom_sensitivity` 把这条不确定性摊开。所以
「层溢价上的 −12bp」不能直接搬到「篮子溢价 +87bp」上——两者连符号都可能不同。

不做的事：不去补 155 只的真实价格（本地 312/313 无副本，见 universe_gap.json），
所以这不是「重跑 235bp」，是「给 235bp 一个用真实面板算出来的括号」。

输出：data/.scratch/survivorship_bound.json
"""
from __future__ import annotations

import json
import math
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
REPS = 20
K_GRID = (155, 313)
CRASH = (0.0, 0.4, 1.0)
FORCED_SCALE = 0.05
BASKET_N = 100


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


def eligible(df: pl.DataFrame) -> pl.DataFrame:
    """层/池/篮子的共同定义域：mom 与 fwd 可用、成交额为正。"""
    return df.filter(pl.col("mom").is_not_null() & pl.col("fwd60").is_not_null()
                     & (pl.col("turnover") > 0))


def _layer_flag(d: pl.DataFrame) -> pl.DataFrame:
    return d.with_columns(
        (((pl.col("turnover").rank("ordinal").over("date") - 1)
          / pl.col("turnover").count().over("date")) < FRAC).alias("in_b"))


def layer_premium(df: pl.DataFrame) -> tuple[float, int]:
    """低成交额 20% 层的层溢价（bp）与决策日数。口径与 probe_width_frontier 一致。"""
    g = (_layer_flag(eligible(df)).group_by("date").agg(
        pl.col("fwd60").filter(pl.col("in_b")).mean().alias("bmu"),
        pl.col("fwd60").mean().alias("pmu")).sort("date"))
    lay = (g["bmu"] - g["pmu"]).to_numpy()
    fin = lay[np.isfinite(lay)]
    return float(1e4 * fin.mean()), int(len(fin))


def basket_premium(df: pl.DataFrame) -> tuple[float, int]:
    """层内按 mom 降序取前 100 的组合均值 − 层均值（产品定义，见 build_bucket）。"""
    d = _layer_flag(eligible(df)).filter(pl.col("in_b")).with_columns(
        pl.col("mom").rank("ordinal", descending=True).over("date").alias("rk"))
    g = (d.group_by("date").agg(
        pl.col("fwd60").filter(pl.col("rk") <= BASKET_N).mean().alias("b"),
        pl.col("fwd60").mean().alias("l"),
        pl.len().alias("n")).sort("date"))
    a = (g["b"] - g["l"]).to_numpy()
    fin = a[np.isfinite(a)]
    return float(1e4 * fin.mean()), int(len(fin))


def layer_size(df: pl.DataFrame) -> float:
    d = _layer_flag(eligible(df))
    return float(d.filter(pl.col("in_b")).group_by("date").agg(
        pl.len().alias("k"))["k"].median())


def index_panel(base: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {k[0]: v for k, v in base.partition_by("thscode", as_dict=True).items()}


def draw(idx: dict[str, pl.DataFrame], k: int, rng) -> list[tuple[str, int]]:
    """抽 k 只有足够历史的标的并各给一个截断点。**排序后再抽**，否则种子无效。"""
    syms = np.sort(np.fromiter(idx.keys(), dtype="U16"))
    picked = rng.choice(syms, size=min(k, len(syms)), replace=False)
    out = []
    for s in picked:
        n = idx[s].height
        if n < 3 * H:
            continue
        out.append((str(s), int(rng.integers(2 * H, n))))
    return out


def block(idx, chosen, crash: float, mode: str, clip: bool) -> pl.DataFrame:
    """把抽中的标的截断成「退市股」。clip=False 时不动 fwd，只截断。"""
    parts = []
    for i, (s, cut) in enumerate(chosen):
        d = idx[s].head(cut)
        rows = d.height
        v = d["fwd60"].to_numpy().copy()
        if clip:
            # 距末日 j 行时为 −crash×(1−j/H)，j=0 正好是 −crash；只压不抬。
            j = rows - 1 - np.arange(rows)
            m = j < H
            v[m] = np.minimum(v[m], -crash * (1.0 - j[m] / H))
        tu = d["turnover"].to_numpy()
        if mode == "forced":
            tu = tu * FORCED_SCALE
        parts.append(pl.DataFrame({
            "thscode": [f"SYN{i:04d}"] * rows,
            "date": d["date"].to_list(),
            "mom": d["mom"].to_list(),
            "fwd60": v.tolist(),
            "turnover": tu.tolist(),
            "volume": d["volume"].to_list()}))
    return pl.concat(parts, how="vertical_relaxed") if parts else None


def measure(base: pl.DataFrame, syn: pl.DataFrame | None) -> dict:
    aug = base if syn is None else pl.concat([base, syn], how="vertical_relaxed")
    lp, ld = layer_premium(aug)
    bp, bd = basket_premium(aug)
    out = {"layer_bp": lp, "layer_days": ld, "basket_bp": bp, "basket_days": bd}
    if syn is not None:
        d = _layer_flag(eligible(aug)).filter(pl.col("thscode").str.starts_with("SYN"))
        out["synth_rows"] = d.height
        out["synth_in_layer"] = float(d["in_b"].mean()) if d.height else 0.0
        per = (aug.filter(pl.col("thscode").str.starts_with("SYN"))
               .group_by("date").agg(pl.len().alias("k")))
        out["presence"] = float(per["k"].mean() / max(1, syn["thscode"].n_unique()))
    return out


def closed_form(base: pl.DataFrame, k: int) -> dict:
    """纯稀释的算术锚：K 只**全在场**、收益 = 当日池均值 / 0，成交额压到最底。"""
    mu = (base.filter(pl.col("fwd60").is_not_null())
          .group_by("date").agg(pl.col("fwd60").mean().alias("mu")).sort("date"))
    dates = mu["date"].to_numpy()
    vals = mu["mu"].to_numpy()
    n = len(dates)
    ln = layer_size(base)

    def make(fill: np.ndarray) -> pl.DataFrame:
        return pl.DataFrame({
            "thscode": np.repeat([f"SYN{i:04d}" for i in range(k)], n),
            "date": np.tile(dates, k),
            "mom": np.zeros(k * n),
            "fwd60": np.tile(fill, k),
            "turnover": np.full(k * n, 1e-6),
            "volume": np.full(k * n, 1e5)})

    base_lp, _ = layer_premium(base)
    out = {"k": k, "layer_n_med": ln,
           "identity_bp": float(-k / (ln + k) * base_lp)}
    for tag, fill in (("pool_mean", vals), ("zero", np.zeros_like(vals))):
        aug = pl.concat([base, make(fill)], how="vertical_relaxed")
        lp, _ = layer_premium(aug)
        out[f"all_present_{tag}_bp"] = float(lp - base_lp)
    return out


def displacement_identity(base: pl.DataFrame, k: int) -> dict:
    """按实现的 20% 分位定义推导的**解析**锚——`identity_bp` 不是这个。

    实现里层是「当日成交额最低 FRAC 分位」（`_layer_flag`）。注入 k 只
    turnover≈0、fwd = 当日池均值的合成票之后：
      新池 N+k ⇒ 新层 = ceil(FRAC×(N+k))；合成票占掉最底部 k 个位，
      原层只留下成交额最低的 ceil(FRAC×(N+k)) − k 只，其余被挤出。
    合成票的 fwd 等于池均值 ⇒ 新池均值不变 ⇒ 逐日
      Δ = (k·μ_P + Σ_{留下的原层成员} fwd) / 新层大小 − 原层均值。
    与 `identity_bp` 的差全部来自「层只同比增长、且要挤出原成员」这一项。
    """
    d = _layer_flag(eligible(base))
    #: 层 = 当日成交额升序前 ceil(FRAC×N) 个，所以直接按 turnover 排序取前缀，
    #: 不必先用 `in_b` 过滤——过滤后长度变了，`sort_by` 的 by 列对不上组长度。
    g = d.group_by("date").agg(
        pl.len().alias("n"),
        pl.col("fwd60").mean().alias("mu"),
        pl.col("fwd60").sort_by("turnover").alias("fs"),
    ).sort("date")
    out = []
    for n, mu, fs in zip(g["n"], g["mu"], g["fs"]):
        vals = fs.to_list() if hasattr(fs, "to_list") else list(fs)
        arr = np.asarray(vals, dtype=float)
        if mu is None or arr.size == 0:
            continue
        n_layer = math.ceil(round(FRAC * n, 9))
        bmu = float(arr[:n_layer].mean())
        nl = math.ceil(round(FRAC * (n + k), 9))
        keep = max(0, nl - k)
        s = float(arr[:keep].sum())
        out.append((k * mu + s) / nl - bmu)
    a = np.asarray(out, dtype=float)
    a = a[np.isfinite(a)]
    return {"k": k, "days": int(a.size), "delta_bp": float(1e4 * a.mean())}


def mom_sensitivity(base: pl.DataFrame, idx, chosen, forced_bp: float) -> list[dict]:
    """篮子的偏差取决于退市股的 mom——本地不知道，所以给一条带子而不是一个点。"""
    syn = block(idx, chosen, 0.0, "forced", clip=True)
    rows = []
    for tag, val in (("donor", None), ("zero", 0.0), ("top", 0.30), ("low", -0.30)):
        s = syn
        if val is not None:
            s = syn.with_columns(pl.lit(val).alias("mom"))
        bp, _ = basket_premium(pl.concat([base, s], how="vertical_relaxed"))
        rows.append({"mom_assumption": tag, "basket_delta_bp": float(bp - forced_bp),
                     "mom_value": val})
    return rows


def main() -> None:
    cen = census()
    print(f"宇宙 {cen['instruments']} 只，dump {cen['in_dump']} 只，缺口 {cen['missing']}"
          f"（其中名字带「退」{cen['delisted']} 只，ST 族 {cen['st_like']} 只，"
          f"其他 {cen['other']} 只）")
    print(f"逐只最后交易日 ≠ {cen['max_last_date']} 的只有 {cen['n_ended_before_max']} 只"
          f" → 这是存活快照，不是 PIT 面板")

    base = P.load_panel().select(["thscode", "date", "mom", "fwd60", "turnover", "volume"])
    idx = index_panel(base)
    base_lp, days = layer_premium(base)
    base_bk, bdays = basket_premium(base)
    ln = layer_size(base)
    print(f"\n基线：层溢价 {base_lp:+.2f}bp（{days} 日，层内 {ln:.0f} 只）"
          f"　篮子溢价 {base_bk:+.2f}bp（{bdays} 日）")

    cf = {}
    for k in K_GRID:
        v = closed_form(base, k)
        v["displacement_identity_bp"] = displacement_identity(base, k)["delta_bp"]
        cf[k] = v
    for k, v in cf.items():
        print(f"闭式锚 K={k}：全在场·池均值 {v['all_present_pool_mean_bp']:+.2f}bp"
              f"　分位解析 {v['displacement_identity_bp']:+.2f}bp"
              f"　一阶参照 {v['identity_bp']:+.2f}bp"
              f"　全在场·零收益 {v['all_present_zero_bp']:+.2f}bp")

    rng = np.random.default_rng(SEED)
    acc: dict[tuple, list] = {}
    bk_acc: dict[tuple, list] = {}
    trunc_acc: dict[int, list] = {}
    trunc_pres: dict[int, list] = {}
    pres: dict[int, list] = {}
    inlay: dict[tuple, list] = {}
    mom_rows = None
    for rep in range(REPS):
        for k in K_GRID:
            chosen = draw(idx, k, rng)
            for mode in ("random", "forced"):
                for crash in CRASH:
                    syn = block(idx, chosen, crash, mode, clip=True)
                    m = measure(base, syn)
                    acc.setdefault((mode, k, crash), []).append(m["layer_bp"] - base_lp)
                    pres.setdefault(k, []).append(m["presence"])
                    inlay.setdefault((mode, k, crash), []).append(m["synth_in_layer"])
                    if mode == "forced" and k == 155:
                        bk_acc.setdefault(crash, []).append(m["basket_bp"] - base_bk)
                if mode == "forced":
                    #: 配对档：同一 chosen、同一在场率，只把「削平」关掉。
                    syn_t = block(idx, chosen, 0.0, "forced", clip=False)
                    mt = measure(base, syn_t)
                    trunc_acc.setdefault(k, []).append(mt["layer_bp"] - base_lp)
                    trunc_pres.setdefault(k, []).append(mt["presence"])
                if mode == "forced" and mom_rows is None:
                    mom_rows = mom_sensitivity(base, idx, chosen, base_bk)
                    print(f"  篮子 mom 敏感性（K=155，crash=0）："
                          f"{[(r['mom_assumption'], round(r['basket_delta_bp'], 1)) for r in mom_rows]}")
        if (rep + 1) % 5 == 0:
            print(f"  rep {rep + 1}/{REPS} 完成", flush=True)

    rows = []
    for (mode, k, crash), v in sorted(acc.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        a = np.array(v)
        rows.append({"mode": mode, "k": k, "crash": crash, "reps": len(a),
                     "delta_bp_mean": float(a.mean()),
                     "delta_bp_p05": float(np.quantile(a, 0.05)),
                     "delta_bp_p95": float(np.quantile(a, 0.95)),
                     "delta_bp_min": float(a.min()), "delta_bp_max": float(a.max()),
                     "level_bp": float(base_lp + a.mean()),
                     "synth_in_layer": float(np.median(inlay[(mode, k, crash)]))})
        print(f"  {mode:6s} k={k:3d} 崩 {crash:.0%} → Δ {a.mean():+7.2f}bp"
              f"（p05 {np.quantile(a, 0.05):+.1f} … p95 {np.quantile(a, 0.95):+.1f}）")

    bk_rows = []
    for crash, v in sorted(bk_acc.items()):
        a = np.array(v)
        bk_rows.append({"mode": "forced", "k": 155, "crash": crash, "reps": len(a),
                        "delta_bp_mean": float(a.mean()),
                        "delta_bp_p05": float(np.quantile(a, 0.05)),
                        "delta_bp_p95": float(np.quantile(a, 0.95))})
        print(f"  篮子 forced k=155 崩 {crash:.0%} → Δ {a.mean():+7.2f}bp")

    trunc_rows = []
    for k, v in sorted(trunc_acc.items()):
        a = np.array(v)
        trunc_rows.append({
            "k": k, "crash": 0.0, "clip": False, "reps": len(a),
            "delta_bp_mean": float(a.mean()),
            "delta_bp_p05": float(np.quantile(a, 0.05)),
            "delta_bp_p95": float(np.quantile(a, 0.95)),
            "delta_bp_min": float(a.min()), "delta_bp_max": float(a.max()),
            "presence_med": float(np.median(trunc_pres[k]))})
        ref = next(r["delta_bp_mean"] for r in rows
                   if r["mode"] == "forced" and r["k"] == k and r["crash"] == 0.0)
        print(f"  纯截断 k={k:3d}（crash=0, clip=False）→ Δ {a.mean():+7.2f}bp"
              f"（p05 {np.quantile(a, 0.05):+.1f} … p95 {np.quantile(a, 0.95):+.1f}）"
              f"　削平贡献 {ref - a.mean():+.2f}bp")

    pres_med = {k: float(np.median(v)) for k, v in pres.items()}
    rep = {"h": H, "frac": FRAC, "cost_bp": COST_BP, "seed": SEED, "reps": REPS,
           "protocol": {
               "draw": "np.sort(unique symbols) → rng.choice → 每只 rng.integers(2H, n) 作截断点",
               "pairing": "同一 rep 的 (标的, 截断点) 在两种模式、全部 crash 档复用（CRN）",
               "crash=0": "仍是 clip：末段 60 行 fwd 被削到 ≤0；纯稀释见 truncate_only",
               "forced": f"turnover ×{FORCED_SCALE}，入层率见 injection[].synth_in_layer",
               "basket": f"层内 mom 降序前 {BASKET_N} 的 fwd 均值 − 层均值"},
           "census": cen, "baseline_bp": base_lp, "baseline_days": days,
           "baseline_basket_bp": base_bk, "baseline_basket_days": bdays,
           "layer_n_med": ln,
           "presence_med": pres_med,
           "closed_form": list(cf.values()),
           "mechanism_ladder": mechanism_ladder(), "injection": rows,
           "truncate_only": trunc_rows,
           "basket_injection": bk_rows, "mom_sensitivity": mom_rows or [],
           "definitions": {
               "census.n_ended_before_max": "dump 里最后交易日早于全局末日的标的数。0 附近 ⇒ 快照",
               "mechanism_ladder": "bias_bp = layer_weight × gap_bp；W 是缺口组的层内 name-day 权重",
               "closed_form.all_present_pool_mean_bp": "K 只全在场、收益=当日池均值，构造面板后**重算**（模拟，无抽样误差；不是算术）",
               "closed_form.displacement_identity_bp": "按实现的 20% 分位推导的解析锚：注入后层=ceil(0.2(N+K))，原层只留最低的 ceil(0.2(N+K))−K 只，其余被挤出",
               "closed_form.identity_bp": "−K/(层内只数+K) × 基线层溢价：假设层线性长到 N+K 且原成员全留，与实现不符，只作一阶参照",
               "truncate_only": "crash=0 且 clip=False：只截断、不削平。与 injection[forced, crash=0] 共用同一批标的与切断点，差值即「削平」的贡献",
               "presence_med": "合成标的在任一决策日的平均在场只数 / K；0.42 ⇒ 等效 K 只有四成",
               "injection.delta_bp_mean": "注入后层溢价 − 基线层溢价；负值表示存活偏差高估了溢价",
               "injection.mode=forced": "成交额×0.05 ⇒ 近似全部进层，真实入层率见 injection[].synth_in_layer；上界",
               "injection.mode=random": "注入标的保留自身成交额 ⇒ 按无条件比例进层（下界）",
               "basket_injection": "同一注入换成产品口径（层内 mom 前 100）。**不可识别**：依赖退市股 mom",
               "mom_sensitivity": "篮子偏差对「注入标的 mom」的敏感性；本地没有退市股 mom，只能给带子"}}
    (OUT / "survivorship_bound.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/survivorship_bound.json")


if __name__ == "__main__":
    sys.exit(main())
