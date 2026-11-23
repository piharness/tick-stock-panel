"""做T x 图形特征：定稿口径（修正成本归属）

## 修正了什么

前两版用 `毛 = 净 + 12bp` 换算毛收益。**这个换算是错的**：
12bp 只在**触发成交**的票日才付，未触发的票日收益是 0 而不是 −12bp。
正确关系是

    per_day_net   = fill * per_trade_net
    per_day_gross = fill * per_trade_gross = per_day_net + fill * 12bp

错口径把毛收益虚增 `12*(1-fill)`。由于 fill 随阈值 d 单调下降（0.67→0.14），
错口径会造出「d 越大毛收益越高」的**假单调**。本脚本全部重算。

## 四组指标，不得混用
  `pt_net` / `pt_gross`   触发成交的票日等权（"这一笔划不划算"）
  `pd_net` / `pd_gross`   全部候选票日等权、未触发计 0（"这套规则全年赚不赚"）
  `fill`                  触发率
  `t`                     按交易日 bootstrap（B=1000），有效样本=交易日数

## 判据
F1 pd_net > 0 且 t > 2 ⇒ 该格可作为策略。
F2 分年同号为正 ⇒ 不是单期驱动。
F3 均值 ≤ 中位（不是靠右尾）。
F4 毛收益须在**真实成本**下仍成立：给出 breakeven 往返成本 = pd_gross/fill。
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
LV = {"core": "核心", "active": "活跃", "background": "背景", "silent": "沉默"}


def block(df: pl.DataFrame, net: str, flag: str, seed: int = 11) -> dict:
    """net 列已扣成本（未触发为 0），flag 为布尔触发标记。"""
    d = df.filter(pl.col(net).is_finite())
    if d.height < 2000:
        return {}
    fill = float(d[flag].fill_null(False).mean())
    g = d.group_by("date").agg([pl.col(net).sum().alias("s"), pl.len().alias("n")]).sort("date")
    if g.height < 30:
        return {}
    s, n = g["s"].to_numpy(), g["n"].to_numpy()
    pd_net = s.sum() / n.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(s), (B, len(s)))
    bs = s[idx].sum(1) / n[idx].sum(1)
    se = float(bs.std())

    tr = d.filter(pl.col(flag).fill_null(False))
    pt_net = float(tr[net].mean()) if tr.height else float("nan")
    return {"fill": fill, "pd_net": pd_net * 1e4, "pd_gross": pd_net * 1e4 + fill * COST_BP,
            "pt_net": pt_net * 1e4 if np.isfinite(pt_net) else float("nan"),
            "pt_gross": pt_net * 1e4 + COST_BP if np.isfinite(pt_net) else float("nan"),
            "t": pd_net / se if se else float("nan"), "days": int(g.height), "n": int(n.sum()),
            "breakeven": (pd_net * 1e4 / fill + COST_BP) if fill > 0 else float("nan")}


def hdr(t: str) -> None:
    print(f"\n    {'':<22}{'fill':>6}{'笔净':>8}{'笔毛':>8}{'日净':>8}{'日毛':>8}"
          f"{'t':>7}{'盈亏平衡':>9}{'n':>9}")


def row(nm: str, r: dict) -> None:
    if not r:
        print(f"    {nm:<22}{'样本不足':>20}")
        return
    print(f"    {nm:<22}{r['fill']:>6.3f}{r['pt_net']:>+8.1f}{r['pt_gross']:>+8.1f}"
          f"{r['pd_net']:>+8.1f}{r['pd_gross']:>+8.1f}{r['t']:>7.2f}"
          f"{r['breakeven']:>+9.0f}{r['n']:>9}")


def main() -> None:
    sh = pl.read_parquet(OUT / "shape.parquet")
    pan = (pl.read_parquet(OUT / "panel.parquet",
                           columns=["symbol", "date", "gap", "level"])
             .with_columns(pl.col("date").cast(pl.String)))
    sh = sh.join(pan, on=["symbol", "date"], how="inner").sort(["symbol", "date"])
    sh = sh.with_columns(
        [pl.col(c).shift(1).over("symbol").alias(f"L_{c}") for c in PRE]
        + [pl.col("level").shift(1).over("symbol").alias("L_level")])
    tr = pl.read_parquet(OUT / "trades.parquet",
                         columns=["symbol", "date", "d", "r1", "r2", "f1", "f2"])
    df = sh.join(tr, on=["symbol", "date"], how="inner").sort(["symbol", "date", "d"])
    df = df.with_columns([
        pl.when(pl.col("f1").fill_null(False)).then(pl.col("r1")).otherwise(0.0).alias("p1"),
        pl.when(pl.col("f2").fill_null(False)).then(pl.col("r2")).otherwise(0.0).alias("p2"),
    ])
    out: dict = {"meta": {"rows": int(df.height), "days": int(df["date"].n_unique()),
                          "cost_bp": COST_BP,
                          "note": "pd_gross = pd_net + fill*12；旧版 pd_net+12 为错口径"}}

    # ---------------- 0 基准 + d 曲线 ----------------
    print("=" * 104)
    print("[0] 基准与 d 曲线（正T = 跌破锚 d 买入、回锚卖出；锚=09:35 首根收盘）")
    hdr("")
    base = {}
    for dd in sorted(df["d"].unique().to_list()):
        s = df.filter(pl.col("d") == dd)
        r1, r2 = block(s, "p1", "f1"), block(s, "p2", "f2")
        row(f"d={dd:.3f} 正T", r1)
        row(f"d={dd:.3f} 负T", r2)
        base[f"{dd:.3f}"] = {"r1": {k: round(v, 2) for k, v in r1.items()},
                             "r2": {k: round(v, 2) for k, v in r2.items()}}
    out["d_curve"] = base

    # ---------------- 1 T-1 特征五分位 ----------------
    print("\n" + "=" * 104)
    print("[1] 按 T-1 特征五分位（d=0.01），日净 / 日毛")
    s1 = df.filter(pl.col("d") == 0.01)
    grid = []
    for c in PRE:
        sub = s1.filter(pl.col(f"L_{c}").is_finite())
        sub = sub.with_columns(pl.col(f"L_{c}").qcut(
            5, labels=[f"Q{i}" for i in range(1, 6)], allow_duplicates=True).over("date").alias("_q"))
        line1, line2 = [], []
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            ss = sub.filter(pl.col("_q") == q)
            for net, flag, tag in (("p1", "f1", "正"), ("p2", "f2", "负")):
                r = block(ss, net, flag)
                if not r:
                    continue
                grid.append({"feat": c, "cn": CN[c], "dir": tag, "q": q,
                             "fill": round(r["fill"], 3), "pd_net": round(r["pd_net"], 1),
                             "pd_gross": round(r["pd_gross"], 1),
                             "t": round(r["t"], 2), "n": r["n"]})
                (line1 if tag == "正" else line2).append(
                    f"{q}:{r['pd_net']:+6.1f}")
        print(f"  {CN[c]:<14}正 " + " ".join(line1) + "   负 " + " ".join(line2))
    out["grid_q5"] = grid
    mx = max(grid, key=lambda z: z["pd_net"])
    print(f"\n    日净最高格：{mx['cn']} {mx['dir']}T {mx['q']} 日净 {mx['pd_net']:+.1f}bp "
          f"日毛 {mx['pd_gross']:+.1f}bp t={mx['t']:+.2f}")
    out["grid_max"] = mx
    pos = [g for g in grid if g["pd_net"] > 0]
    print(f"    日净 > 0 的格：{len(pos)}/{len(grid)}   "
          f"t>2 且日净>0 的格：{sum(1 for g in grid if g['pd_net'] > 0 and g['t'] > 2)}")
    out["grid_n_pos"] = [len(pos), len(grid)]

    # ---------------- 2 高振幅 x d x 方向 ----------------
    print("\n" + "=" * 104)
    print("[2] T-1 振幅 Q5（高振幅票）x d x 方向 —— 空间是唯一可预测的杠杆")
    hi = s1.filter(pl.col("L_ampl_o").is_finite())
    hi = hi.with_columns(pl.col("L_ampl_o").qcut(
        5, labels=[f"Q{i}" for i in range(1, 6)], allow_duplicates=True).over("date").alias("aq"))
    cell = {}
    hdr("")
    for dd in sorted(df["d"].unique().to_list()):
        ss = df.filter(pl.col("d") == dd, pl.col("L_ampl_o").is_finite())
        ss = ss.with_columns(pl.col("L_ampl_o").qcut(
            5, labels=[f"Q{i}" for i in range(1, 6)], allow_duplicates=True).over("date").alias("aq"))
        ss = ss.filter(pl.col("aq") == "Q5")
        r1, r2 = block(ss, "p1", "f1"), block(ss, "p2", "f2")
        row(f"振幅Q5 d={dd:.3f} 正T", r1)
        row(f"振幅Q5 d={dd:.3f} 负T", r2)
        cell[f"{dd:.3f}"] = {"r1": {k: round(v, 2) for k, v in r1.items()},
                             "r2": {k: round(v, 2) for k, v in r2.items()}}
    out["amplQ5_d"] = cell

    # ---------------- 3 唯一候选的稳定性 ----------------
    print("\n" + "=" * 104)
    print("[3] 候选格稳定性：振幅Q5 + 负T + d=0.03")
    ss = df.filter(pl.col("d") == 0.03, pl.col("L_ampl_o").is_finite())
    ss = ss.with_columns(pl.col("L_ampl_o").qcut(
        5, labels=[f"Q{i}" for i in range(1, 6)], allow_duplicates=True).over("date").alias("aq"))
    ss = ss.filter(pl.col("aq") == "Q5")
    full = block(ss, "p2", "f2")
    ds = sorted(ss["date"].unique().to_list())
    print(f"    全样本 日净 {full['pd_net']:+.1f}bp t={full['t']:+.2f} fill={full['fill']:.3f}")
    tr_ = ss.filter(pl.col("f2").fill_null(False))["r2"]
    print(f"    笔净 均值 {tr_.mean() * 1e4:+.1f}bp 中位 {tr_.median() * 1e4:+.1f}bp "
          f"胜率 {(tr_ > 0).mean():.3f}  p1 {np.percentile(tr_.to_numpy(), 1) * 1e4:+.0f}bp")
    print(f"    {'区间':<12}{'日净':>9}{'t':>7}{'fill':>7}{'days':>7}")
    parts = {}
    for nm, i0, i1 in (("前半", 0, len(ds) // 2), ("后半", len(ds) // 2, len(ds))):
        r = block(ss.filter(pl.col("date").is_in(ds[i0:i1])), "p2", "f2")
        print(f"    {nm:<12}{r['pd_net']:>+9.1f}{r['t']:>7.2f}{r['fill']:>7.3f}{r['days']:>7}")
        parts[nm] = {"pd_net": round(r["pd_net"], 1), "t": round(r["t"], 2)}
    for y in ("2024", "2025", "2026"):
        r = block(ss.filter(pl.col("date").str.slice(0, 4) == y), "p2", "f2")
        if not r:
            continue
        print(f"    {y:<12}{r['pd_net']:>+9.1f}{r['t']:>7.2f}{r['fill']:>7.3f}{r['days']:>7}")
        parts[y] = {"pd_net": round(r["pd_net"], 1), "t": round(r["t"], 2)}
    print(f"    同号为正 {sum(1 for v in parts.values() if v['pd_net'] > 0)}/{len(parts)}")
    out["cand"] = {"full": {k: round(v, 2) for k, v in full.items()},
                   "mean_bp": round(float(tr_.mean() * 1e4), 1),
                   "med_bp": round(float(tr_.median() * 1e4), 1),
                   "hit": round(float((tr_ > 0).mean()), 3), "parts": parts}

    # ---------------- 4 阶层 x 振幅 ----------------
    print("\n" + "=" * 104)
    print("[4] 阶层(T-1) x 振幅(T-1)，d=0.01，正T 日净 bp")
    s4 = df.filter(pl.col("d") == 0.01, pl.col("L_ampl_o").is_finite(),
                   pl.col("L_level").is_not_null())
    s4 = s4.with_columns(pl.col("L_ampl_o").qcut(
        3, labels=["小", "中", "大"], allow_duplicates=True).over("date").alias("aq3"))
    print(f"    {'阶层':<8}" + "".join(f"{q + '振幅':>16}" for q in ("小", "中", "大")))
    lv = {}
    for k in ("core", "active", "background", "silent"):
        vals = []
        for q in ("小", "中", "大"):
            r = block(s4.filter((pl.col("L_level") == k) & (pl.col("aq3") == q)), "p1", "f1")
            if not r:
                vals.append(f"{'-':>16}")
                continue
            vals.append(f"{r['pd_net']:>+9.1f}({r['t']:>3.1f})")
            lv[f"{k}|{q}"] = {"pd_net": round(r["pd_net"], 1), "t": round(r["t"], 2),
                              "fill": round(r["fill"], 3), "n": r["n"]}
        print(f"    {LV[k]:<8}" + "".join(vals))
    out["level_x_ampl"] = lv

    # ---------------- 5 走前式多变量 ----------------
    print("\n" + "=" * 104)
    print("[5] 走前式多变量线性模型（16 pre 特征 + gap + 阶层），d=0.01")
    feat = [f"L_{c}" for c in PRE] + ["gap"]
    s5 = df.filter(pl.col("d") == 0.01).sort(["date", "symbol"])
    s5 = s5.with_columns(
        [((pl.col(c).rank() / pl.len().over("date")) - 0.5).alias(f"z_{c}") for c in feat]
        + [(pl.col("L_level") == "core").cast(pl.Float64).alias("z_core"),
           (pl.col("L_level") == "active").cast(pl.Float64).alias("z_active"),
           (pl.col("L_level") == "background").cast(pl.Float64).alias("z_bg")])
    zc = [f"z_{c}" for c in feat] + ["z_core", "z_active", "z_bg"]
    s5 = s5.drop_nulls(zc + ["p1", "p2", "f1", "f2"])
    days = sorted(s5["date"].unique().to_list())
    start, step = int(len(days) * 0.4), 20
    folds = []
    for i in range(start, len(days), step):
        a = s5.filter(pl.col("date").is_in(days[:i]))
        b = s5.filter(pl.col("date").is_in(days[i:i + step]))
        if b.height < 1000:
            continue
        X = a.select(zc).to_numpy()
        mu, sd = X.mean(0), X.std(0) + 1e-9
        Xs = (X - mu) / sd
        Xd = np.c_[np.ones(len(b)), (b.select(zc).to_numpy() - mu) / sd]
        cols = {}
        for tgt in ("p1", "p2"):
            beta, *_ = np.linalg.lstsq(np.c_[np.ones(len(Xs)), Xs],
                                       a[tgt].to_numpy(), rcond=None)
            cols[tgt] = pl.Series(f"hat_{tgt}", Xd @ beta)
        folds.append(b.select(["date", "p1", "p2", "f1", "f2"]).with_columns(
            [cols["p1"], cols["p2"]]))
    pj = pl.concat(folds).sort("date")
    print(f"    测试 {pj.height:,} 票日 / {pj['date'].n_unique()} 天 "
          f"（前 40% 起、每 20 天重估、只用当日之前数据）")
    print(f"    {'分位':<10}{'正T日净':>10}{'t':>7}{'正T日毛':>10}{'负T日净':>10}{'t':>7}"
          f"{'负T日毛':>10}{'n':>9}")
    rec = {}
    for tgt, flag, tag in (("p1", "f1", "正"), ("p2", "f2", "负")):
        pj2 = pj.with_columns(pl.col(f"hat_{tgt}").qcut(
            10, labels=[f"D{i}" for i in range(1, 11)], allow_duplicates=True).alias("dec"))
        rr = {}
        for dv in [f"D{i}" for i in range(1, 11)]:
            r = block(pj2.filter(pl.col("dec") == dv), tgt, flag)
            if r:
                rr[dv] = r
        rec[tag] = {k: {"pd_net": round(v["pd_net"], 1), "pd_gross": round(v["pd_gross"], 1),
                        "t": round(v["t"], 2), "fill": round(v["fill"], 3)}
                    for k, v in rr.items()}
    for dv in [f"D{i}" for i in range(1, 11)]:
        a, b = rec["正"].get(dv, {}), rec["负"].get(dv, {})
        print(f"    {dv:<10}{a.get('pd_net', float('nan')):>+10.1f}"
              f"{a.get('t', float('nan')):>7.2f}{a.get('pd_gross', float('nan')):>+10.1f}"
              f"{b.get('pd_net', float('nan')):>+10.1f}"
              f"{b.get('t', float('nan')):>7.2f}{b.get('pd_gross', float('nan')):>+10.1f}")
    out["ols"] = rec

    p = ROOT / "analysis" / "_diag_t0_shape_final.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
