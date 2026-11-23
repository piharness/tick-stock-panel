"""做T的「毛收益天花板」：d 曲线、多变量模型、阶层 x 振幅二维

## 为什么还要跑这一轮

第一轮 320 个 pre 族格**无一格毛收益超过 12bp**，最高 11.5bp（T-1 振幅 Q5 正T）。
但"最高 11.5 < 12"这个结论有三个可能的漏洞，必须逐个堵：

1. **d 选错了**：主口径只跑 d=0.01。若 d 更大时毛收益上升，那结论就只是"阈值没调好"。
   ⇒ A. d 曲线（0.005/0.01/0.02/0.03），单变量与最佳条件并排。
2. **单变量不够**：「变量太少」的正当反驳是多变量。⇒ B. 走前式（walk-forward）
   线性模型，16 个 pre 特征 + gap + 阶层哑变量，按预测值分层看 out-of-sample 收益。
   这是对「参与筛选的变量太少」唯一公平的回答。
3. **没按资金体量分**：主力的存在与成交额高度共线。⇒ C. 阶层 x 振幅二维，
   检验「空间来自主力、胜率来自散户」这个张力是否真实存在。

## 口径
沿用 `_diag_t0_shape.py`：r1/r2 已扣 12bp；`p1/p2` 是 per_day（未触发计 0）。
`pre` 族只用 T-1 及更早 + T 日 gap（09:25 可知）。t 一律按交易日 bootstrap。

## 预注册判据
K1 d 曲线单调或存在内点最优且毛 > 12bp ⇒ 阈值是主因。
K2 走前式模型的 top decile per_day 毛 > 12bp 且 t > 2 ⇒ 多变量有增量。
K3 阶层 x 振幅二维里，「高振幅 + 高成交额」与「高振幅 + 低成交额」的毛收益差
   须显著；若低成交额反而更好 ⇒ 张力成立（空间在主力、胜率在散户）。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
B = 500
COST_BP = 12.0
PRE = ["i1", "f30", "pm", "last30", "eff", "zigzag", "ac1", "n_vwap_r",
       "upvol", "sh_up", "maxbar", "t_hi", "ampl_o", "dip", "rise", "dd_close"]
CN = {
    "i1": "T-1全天方向", "f30": "T-1早盘30min", "pm": "T-1下午", "last30": "T-1尾盘",
    "eff": "T-1路径效率", "zigzag": "T-1锯齿度", "ac1": "T-1自相关",
    "n_vwap_r": "T-1穿VWAP", "upvol": "T-1涨量占比", "sh_up": "T-1上影线",
    "maxbar": "T-1单根集中", "t_hi": "T-1高点时点", "ampl_o": "T-1振幅",
    "dip": "T-1回撤", "rise": "T-1上冲", "dd_close": "T-1收盘位置",
}


def day_mean_t(df: pl.DataFrame, col: str) -> tuple[float, float, int, int]:
    g = df.filter(pl.col(col).is_finite()).group_by("date").agg(
        [pl.col(col).sum().alias("s"), pl.len().alias("n")]).sort("date")
    if g.height < 30:
        return float("nan"), float("nan"), 0, 0
    s, n = g["s"].to_numpy(), g["n"].to_numpy()
    v = s.sum() / n.sum()
    rng = np.random.default_rng(7)
    idx = rng.integers(0, len(s), (B, len(s)))
    bs = s[idx].sum(1) / n[idx].sum(1)
    se = float(bs.std())
    return v, (v / se if se else float("nan")), int(n.sum()), int(g.height)


def load_multi_d() -> pl.DataFrame:
    sh = pl.read_parquet(OUT / "shape.parquet")
    pan = (pl.read_parquet(OUT / "panel.parquet",
                           columns=["symbol", "date", "gap", "level", "p_amount", "p_turn"])
             .with_columns(pl.col("date").cast(pl.String)))
    sh = sh.join(pan, on=["symbol", "date"], how="inner").sort(["symbol", "date"])
    sh = sh.with_columns(
        [pl.col(c).shift(1).over("symbol").alias(f"L_{c}") for c in PRE]
        + [pl.col("level").shift(1).over("symbol").alias("L_level")])
    tr = pl.read_parquet(OUT / "trades.parquet",
                         columns=["symbol", "date", "d", "r1", "r2", "f1", "f2"])
    df = sh.join(tr, on=["symbol", "date"], how="inner")
    return df.with_columns([
        pl.when(pl.col("f1").fill_null(False)).then(pl.col("r1")).otherwise(0.0).alias("p1"),
        pl.when(pl.col("f2").fill_null(False)).then(pl.col("r2")).otherwise(0.0).alias("p2"),
    ]).sort(["symbol", "date", "d"])


def main() -> None:
    df = load_multi_d()
    print(f"[load] {df.height:,} 行  天数 {df['date'].n_unique()}  d={sorted(df['d'].unique().to_list())}")
    out: dict = {"meta": {"rows": df.height, "days": int(df["date"].n_unique()),
                          "cost_bp": COST_BP}}

    # ===================== A. d 曲线 =====================
    print("\n" + "=" * 96)
    print("[A] d 曲线：阈值越大毛收益会上升吗（无条件下 + 最佳条件下）")
    print(f"    {'d':>6}{'正T净':>9}{'正T毛':>8}{'t':>7}{'fill':>7}"
          f"{'正T/d净':>9}{'正T/d毛':>9}{'负T毛':>8}")
    cur = []
    for dd in sorted(df["d"].unique().to_list()):
        s = df.filter(pl.col("d") == dd)
        v1, t1, n1, _ = day_mean_t(s, "r1")
        v2, t2, _, _ = day_mean_t(s, "r2")
        w1, _, _, _ = day_mean_t(s, "p1")
        fill = float(s["f1"].mean())
        print(f"{dd:>6.3f}{v1 * 1e4:>+9.1f}{v1 * 1e4 + COST_BP:>+8.1f}{t1:>7.2f}"
              f"{fill:>7.3f}{w1 * 1e4:>+9.1f}{w1 * 1e4 + COST_BP:>+9.1f}"
              f"{v2 * 1e4 + COST_BP:>+8.1f}")
        cur.append({"d": dd, "r1": round(v1 * 1e4, 1), "r1_gross": round(v1 * 1e4 + COST_BP, 1),
                    "t": round(t1, 2), "fill": round(fill, 3), "p1": round(w1 * 1e4, 1),
                    "p1_gross": round(w1 * 1e4 + COST_BP, 1),
                    "r2_gross": round(v2 * 1e4 + COST_BP, 1)})
    out["d_curve"] = cur

    print("\n    最佳条件（T-1 振幅 Q5）上的 d 曲线：")
    top = df.filter(pl.col("L_ampl_o").is_finite())
    top = top.with_columns(
        pl.col("L_ampl_o").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                                allow_duplicates=True).over("date").alias("aq"))
    top = top.filter(pl.col("aq") == "Q5")
    print(f"    {'d':>6}{'正T毛/d':>10}{'t':>7}{'负T毛/d':>10}{'t':>7}")
    cur2 = []
    for dd in sorted(df["d"].unique().to_list()):
        s = top.filter(pl.col("d") == dd)
        w1, t1, n1, nd = day_mean_t(s, "p1")
        w2, t2, _, _ = day_mean_t(s, "p2")
        print(f"{dd:>6.3f}{w1 * 1e4 + COST_BP:>+10.1f}{t1:>7.2f}"
              f"{w2 * 1e4 + COST_BP:>+10.1f}{t2:>7.2f}")
        cur2.append({"d": dd, "p1_gross": round(w1 * 1e4 + COST_BP, 1), "t1": round(t1, 2),
                     "p2_gross": round(w2 * 1e4 + COST_BP, 1), "t2": round(t2, 2),
                     "n": n1, "days": nd})
    out["d_curve_amplQ5"] = cur2

    # ===================== B. gap 条件 =====================
    print("\n" + "=" * 96)
    print("[B] T 日 gap（09:25 可知）：高开做负T、低开做正T")
    print(f"    {'gap档':<14}{'正T净':>9}{'t':>7}{'负T净':>9}{'t':>7}{'n':>9}")
    s0 = df.filter(pl.col("d") == 0.01, pl.col("gap").is_finite())
    s0 = s0.with_columns(pl.col("gap").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                                            allow_duplicates=True).over("date").alias("gq"))
    gp = []
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        s = s0.filter(pl.col("gq") == q)
        v1, t1, n1, _ = day_mean_t(s, "r1")
        v2, t2, _, _ = day_mean_t(s, "r2")
        med = float(s["gap"].median()) * 1e4
        print(f"    {q}({med:+6.0f}bp){v1 * 1e4:>+9.1f}{t1:>7.2f}{v2 * 1e4:>+9.1f}"
              f"{t2:>7.2f}{n1:>9}")
        gp.append({"q": q, "gap_med_bp": round(med, 0), "r1": round(v1 * 1e4, 1),
                   "t1": round(t1, 2), "r2": round(v2 * 1e4, 1), "t2": round(t2, 2)})
    out["gap_q"] = gp

    # ===================== C. 阶层 x 振幅 =====================
    print("\n" + "=" * 96)
    print("[C] 阶层(T-1) x 振幅(T-1) 二维，d=0.01，正T per_day 毛收益")
    s1 = df.filter(pl.col("d") == 0.01, pl.col("L_ampl_o").is_finite(),
                   pl.col("L_level").is_not_null())
    s1 = s1.with_columns(pl.col("L_ampl_o").qcut(3, labels=["小", "中", "大"],
                                                 allow_duplicates=True).over("date").alias("aq3"))
    LV = ["core", "active", "background", "silent"]
    CNC = {"core": "核心", "active": "活跃", "background": "背景", "silent": "沉默"}
    print(f"    {'阶层':<8}" + "".join(f"{q + '振幅':>14}" for q in ("小", "中", "大")))
    cell = {}
    for lv in LV:
        vals = []
        for q in ("小", "中", "大"):
            s = s1.filter((pl.col("L_level") == lv) & (pl.col("aq3") == q))
            if s.height < 3000:
                vals.append(f"{'-':>14}")
                continue
            v, t, n, _ = day_mean_t(s, "p1")
            vals.append(f"{v * 1e4 + COST_BP:>+9.1f}({t:>3.1f})")
            cell[f"{lv}|{q}"] = {"p1_gross": round(v * 1e4 + COST_BP, 1),
                                 "t": round(t, 2), "n": n}
        print(f"    {CNC[lv]:<8}" + "".join(vals))
    out["level_x_ampl"] = cell

    # ===================== D. 走前式多变量模型 =====================
    print("\n" + "=" * 96)
    print("[D] 走前式（walk-forward）多变量线性模型：16 pre 特征 + gap + 阶层")
    s2 = df.filter(pl.col("d") == 0.01).sort(["date", "symbol"])
    feat = [f"L_{c}" for c in PRE] + ["gap"]
    # 日级秩标准化（截面可比，且天然抗极值）
    s2 = s2.with_columns(
        [((pl.col(c).rank() / pl.len().over("date")) - 0.5).alias(f"z_{c}")
         for c in feat]
        + [(pl.col("L_level") == "core").cast(pl.Float64).alias("z_core"),
           (pl.col("L_level") == "active").cast(pl.Float64).alias("z_active"),
           (pl.col("L_level") == "background").cast(pl.Float64).alias("z_bg")])
    zc = [f"z_{c}" for c in feat] + ["z_core", "z_active", "z_bg"]
    s2 = s2.drop_nulls(zc + ["p1", "p2"])

    days = sorted(s2["date"].unique().to_list())
    start, step = int(len(days) * 0.4), 20
    folds = []
    for i in range(start, len(days), step):
        tr_days = days[:i]
        te_days = days[i:i + step]
        a = s2.filter(pl.col("date").is_in(tr_days))
        b = s2.filter(pl.col("date").is_in(te_days))
        if b.height < 1000:
            continue
        X = a.select(zc).to_numpy()
        mu, sd = X.mean(0), X.std(0) + 1e-9
        Xs = (X - mu) / sd
        Xb = (b.select(zc).to_numpy() - mu) / sd
        Xd = np.c_[np.ones(len(Xb)), Xb]
        cols = {}
        for tgt in ("p1", "p2"):
            beta, *_ = np.linalg.lstsq(np.c_[np.ones(len(Xs)), Xs],
                                       a[tgt].to_numpy(), rcond=None)
            cols[f"hat_{tgt}"] = pl.Series(f"hat_{tgt}", Xd @ beta)
        folds.append(b.select(["date", "p1", "p2"]).with_columns(
            [cols[k] for k in ("hat_p1", "hat_p2")]))
    if not folds:
        print("    样本不足")
        return
    pj = pl.concat(folds).sort("date")
    print(f"    测试集 {pj.height:,} 票日  {pj['date'].n_unique()} 天 "
          f"（走前式：前 40% 起每 20 天重估一次，只用当日之前的数据）")
    print(f"    {'方向/分位':<12}{'毛bp':>9}{'t':>7}{'n':>10}")
    rec = {}
    for tgt, tag in (("p1", "正T"), ("p2", "负T")):
        pj2 = pj.with_columns(pl.col(f"hat_{tgt}").qcut(
            10, labels=[f"D{i}" for i in range(1, 11)], allow_duplicates=True).alias("dec"))
        rows = {}
        for dv in [f"D{i}" for i in range(1, 11)]:
            s = pj2.filter(pl.col("dec") == dv)
            if s.height < 500:
                continue
            v, t, n, _ = day_mean_t(s, tgt)
            rows[dv] = (v * 1e4 + COST_BP, t, n)
            print(f"    {tag + ' ' + dv:<12}{v * 1e4 + COST_BP:>+9.1f}{t:>7.2f}{n:>10}")
        rec[tag] = {k: {"gross": round(v[0], 1), "t": round(v[1], 2)}
                    for k, v in rows.items()}
        v, t, n, _ = day_mean_t(pj2, tgt)
        print(f"    {tag + ' 全体':<12}{v * 1e4 + COST_BP:>+9.1f}{t:>7.2f}{n:>10}")
        rec[tag]["ALL"] = {"gross": round(v * 1e4 + COST_BP, 1), "t": round(t, 2)}
    out["ols_walkforward"] = rec

    p = ROOT / "analysis" / "_diag_t0_shape2.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
