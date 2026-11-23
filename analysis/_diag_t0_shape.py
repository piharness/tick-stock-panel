"""图形特征能否提高做T的胜率/收益？—— 事前一族 vs 同时一族

## 口径（先写，后跑）

**做T** 沿用 `data/_probe_t0/trades.parquet` 的 r1（正T：跌破锚 d 买入、回锚卖出）与
r2（负T：冲高 d 卖出、回锚买回），已扣 12bp 往返。锚 = 09:35 首根 bar 收盘。

**特征分两族，绝不混用**：
  `pre`  族 = T-1 收盘后可知：T-1 全天形状、T-1 的 ac1/eff/振幅、T 日 gap（09:25 集合
          竞价后可知）、T 日 f30（10:00 可知）。→ 只有这一族允许"选票"。
  `now`  族 = T 日全天形状（收盘才知）。**只用于归因**：主力做出来了什么。
          任何用 now 族分组得出的"做T收益"都是事后画图，必须标 `[NOW]`。

**两个收益口径，必须并列**（先前只报了前者，会系统性低估/高估）：
  `per_trade` = 触发成交的票日等权均值  → 衡量「这一笔划不划算」
  `per_day`   = 全部候选票日等权均值（未触发计 0）→ 衡量「这套选票规则全年赚不赚」
  二者之比就是 fill 率。做T的 alpha 有可能只在 fill 率而不在笔均。

**t 统计**：按交易日 bootstrap（B=500），有效样本 = 交易日数（492），不是票日数。

## 预注册判据

H1 图形可复现：corr(feat_t, feat_{t-1}) 日级均值显著 ≠ 0。若图形本身不可复现，
   一切「图形选票」在逻辑上即不成立。
H2 方向可预测：T-1 特征对 T 日 i1（日内方向）的日级 IC 显著且 t>2。
H3 做T增益：**pre 族**任一格 per_day 毛bp > 12 且 t>2。
H4 多重检验：网格全部格子里 max|t| 须超置换零分布的 p95（打乱特征标签 200 次）。
H5 博弈对称性：若「振幅可预测、方向不可预测」⇒ 主力控制的是频率不是赔率
   ⇒ 做T的边际只在选票（fill 率），不在方向。
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
D_MAIN = 0.01

PRE = ["i1", "f30", "pm", "last30", "eff", "zigzag", "ac1", "n_vwap_r",
       "upvol", "sh_up", "maxbar", "t_hi", "ampl_o", "dip", "rise", "dd_close"]
CN = {
    "i1": "T-1全天方向", "f30": "T-1早盘30min", "pm": "T-1下午", "last30": "T-1尾盘30min",
    "eff": "T-1路径效率", "zigzag": "T-1锯齿度", "ac1": "T-1收益自相关",
    "n_vwap_r": "T-1穿VWAP次数", "upvol": "T-1涨时量占比", "sh_up": "T-1上影线",
    "maxbar": "T-1单根集中度", "t_hi": "T-1高点时点", "ampl_o": "T-1振幅",
    "dip": "T-1开盘后回撤", "rise": "T-1开盘后上冲", "dd_close": "T-1收盘位置",
}


# ---------------------------------------------------------------- 数据
def load() -> pl.DataFrame:
    sh = pl.read_parquet(OUT / "shape.parquet")
    pan = (pl.read_parquet(OUT / "panel.parquet",
                           columns=["symbol", "date", "gap", "level", "prev_close"])
             .with_columns(pl.col("date").cast(pl.String)))
    sh = sh.join(pan, on=["symbol", "date"], how="inner").sort(["symbol", "date"])

    # T-1 特征
    lag = [pl.col(c).shift(1).over("symbol").alias(f"L_{c}") for c in PRE]
    sh = sh.with_columns(lag)
    # T-1 的阶层（事前可知）
    sh = sh.with_columns(pl.col("level").shift(1).over("symbol").alias("L_level"))

    tr = (pl.read_parquet(OUT / "trades.parquet",
                          columns=["symbol", "date", "d", "r1", "r2", "f1", "f2",
                                   "level", "ampl", "i1", "ac1"])
            .filter(pl.col("d") == D_MAIN).drop(["level", "ampl", "i1", "ac1"]))
    df = sh.join(tr, on=["symbol", "date"], how="inner")
    # per_day 口径：未触发计 0
    df = df.with_columns([
        pl.when(pl.col("f1").fill_null(False)).then(pl.col("r1")).otherwise(0.0).alias("p1"),
        pl.when(pl.col("f2").fill_null(False)).then(pl.col("r2")).otherwise(0.0).alias("p2"),
    ])
    return df.sort(["symbol", "date"])


# ---------------------------------------------------------------- 统计
def day_mean_t(df: pl.DataFrame, col: str) -> tuple[float, float, int, int]:
    """票日等权均值 + 按日 bootstrap 的 t。"""
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


def ic(df: pl.DataFrame, x: str, y: str, label: str = "") -> dict:
    """日级 Spearman IC（截面秩相关），t 按日。"""
    sub = df.filter(pl.col(x).is_finite(), pl.col(y).is_finite())
    if sub.height < 10000:
        return {}
    sub = sub.with_columns([
        pl.col(x).rank().over("date").alias("rx"),
        pl.col(y).rank().over("date").alias("ry")]).drop_nulls(["rx", "ry"])
    g = sub.group_by("date").agg([
        pl.len().alias("n"),
        pl.corr("rx", "ry").alias("ic")]).drop_nulls("ic").sort("date")
    v = g["ic"].to_numpy()
    t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 2 else float("nan")
    return {"label": label or CN.get(x, x), "x": x, "y": y, "ic": float(v.mean()),
            "t": float(t), "days": int(len(v)),
            "pos": float((v > 0).mean())}


def qcut5(df: pl.DataFrame, col: str) -> pl.DataFrame:
    return df.with_columns(
        pl.col(col).qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                         allow_duplicates=True).over("date").alias("_q"))


def by_q(df: pl.DataFrame, col: str, out: str, ycol: str) -> list[dict]:
    sub = df.filter(pl.col(col).is_finite())
    sub = qcut5(sub, col).drop_nulls("_q")
    res = []
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        s = sub.filter(pl.col("_q") == q)
        if s.height < 5000:
            continue
        v, t, n, nd = day_mean_t(s, ycol)
        res.append({"q": q, "bp": v * 1e4, "t": t, "n": n, "days": nd})
    return res


def main() -> None:
    df = load()
    days = df["date"].n_unique()
    print(f"[load] {df.height:,} 票日  {days} 交易日  "
          f"{df['date'].min()}~{df['date'].max()}")
    out: dict = {"meta": {"rows": df.height, "days": int(days), "cost_bp": COST_BP,
                          "d": D_MAIN}}

    # ---------------- 基准：无条件 ----------------
    print("\n" + "=" * 96)
    print("[0] 无条件基准（d=0.01，12bp 已扣）")
    base = {}
    for col, tag in (("r1", "正T"), ("r2", "负T"), ("p1", "正T/per_day"),
                     ("p2", "负T/per_day")):
        v, t, n, nd = day_mean_t(df, col)
        fill = (float(df["f1"].mean()) if col.startswith("r1")
                else float(df["f2"].mean()) if col.startswith("r2") else float("nan"))
        print(f"    {tag:<14} {v * 1e4:+7.1f}bp  t={t:+5.2f}  n={n:>8}  fill={fill:.3f}")
        base[tag] = {"bp": round(v * 1e4, 1), "t": round(t, 2), "n": n,
                     "fill": None if np.isnan(fill) else round(fill, 3)}
    out["base"] = base

    # ---------------- H1 图形可复现性 ----------------
    print("\n" + "=" * 96)
    print("[H1] 图形可复现吗：corr(feat_t, feat_{t-1})，日级均值 + t")
    h1 = []
    for c in PRE:
        sub = df.filter(pl.col(c).is_finite(), pl.col(f"L_{c}").is_finite())
        sub = sub.with_columns([
            pl.col(c).rank().over("date").alias("a"),
            pl.col(f"L_{c}").rank().over("date").alias("b")])
        g = sub.group_by("date").agg(pl.corr("a", "b").alias("cc")).drop_nulls("cc")
        v = g["cc"].to_numpy()
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
        lag1_pool = float(sub.select(pl.corr(c, f"L_{c}"))[0, 0])
        h1.append({"feat": c, "cn": CN[c], "persist": round(float(v.mean()), 4),
                   "t": round(float(t), 2), "pooled": round(lag1_pool, 4)})
        print(f"    {CN[c]:<16}{c:<10}日级 {v.mean():+.4f} t={t:+6.1f}   "
              f"混合 {lag1_pool:+.4f}")
    out["h1_persist"] = h1

    # ---------------- H2 方向 vs 振幅 可预测性 ----------------
    print("\n" + "=" * 96)
    print("[H2] pre 族对 T 日的预测力：方向 i1 vs 振幅 ampl_o vs 回撤 dip")
    h2 = {"i1": [], "ampl_o": [], "dip": []}
    for c in PRE + ["gap"]:
        for y in ("i1", "ampl_o", "dip"):
            r = ic(df, f"L_{c}" if c != "gap" else "gap", y, CN.get(c, "T日gap"))
            if r:
                h2[y].append(r)
    for y, nm in (("i1", "日内方向"), ("ampl_o", "日内振幅"), ("dip", "开盘后最深回撤")):
        print(f"\n  -- 预测 {nm}（{y}）--")
        print(f"    {'特征':<18}{'IC':>9}{'t':>8}{'同号日占比':>10}")
        for r in sorted(h2[y], key=lambda z: -abs(z["t"]))[:8]:
            print(f"    {r['label']:<18}{r['ic']:>+9.4f}{r['t']:>8.1f}{r['pos']:>10.1%}")
    out["h2_ic"] = h2

    # ---------------- H3 条件做T ----------------
    print("\n" + "=" * 96)
    print("[H3] 按 T-1 特征五分位做T（d=0.01，毛=净+12）")
    grid = []
    for c in PRE:
        for y, tag in (("r1", "正T"), ("r2", "负T"), ("p1", "正T/d"), ("p2", "负T/d")):
            rows = by_q(df, f"L_{c}", "x", y)
            for r in rows:
                grid.append({"feat": c, "cn": CN[c], "dir": tag, "q": r["q"],
                             "bp": round(r["bp"], 1), "gross": round(r["bp"] + COST_BP, 1),
                             "t": round(r["t"], 2), "n": r["n"]})
    out["h3_grid"] = grid
    for c in PRE:
        sub = [r for r in grid if r["feat"] == c and r["dir"] == "正T"]
        if not sub:
            continue
        line = "  ".join(f"{r['q']}:{r['bp']:+6.1f}({r['t']:+5.1f})" for r in sub)
        print(f"  {CN[c]:<16}{line}")

    print("\n  -- 只看 per_day 口径（未触发计 0）--")
    for c in PRE:
        sub = [r for r in grid if r["feat"] == c and r["dir"] in ("正T/d", "负T/d")]
        if not sub:
            continue
        s1 = "  ".join(f"{r['bp']:+6.1f}" for r in sub if r["dir"] == "正T/d")
        s2 = "  ".join(f"{r['bp']:+6.1f}" for r in sub if r["dir"] == "负T/d")
        print(f"  {CN[c]:<16}正 {s1}   |  负 {s2}")

    # 极值报告
    cand = [r for r in grid if r["gross"] > COST_BP and r["t"] > 2.0]
    print(f"\n  过 H3 门槛的格（毛>12bp 且 t>2）：{len(cand)}/{len(grid)}")
    for r in sorted(cand, key=lambda z: -z["gross"])[:12]:
        print(f"    {r['cn']:<16}{r['dir']:<6}{r['q']} 净{r['bp']:+7.1f} 毛{r['gross']:+7.1f} "
              f"t={r['t']:+5.2f} n={r['n']}")
    out["h3_pass"] = cand

    # ---------------- H4 多重检验 ----------------
    print("\n" + "=" * 96)
    print("[H4] 多重检验：网格 max|t| 对置换零分布")
    real_max = max(abs(r["t"]) for r in grid if np.isfinite(r["t"]))
    rng = np.random.default_rng(20261121)
    null = []
    for it in range(200):
        shuf = df.with_columns(
            pl.col("L_eff").shuffle(seed=int(rng.integers(0, 2 ** 31))).over("date"))
        rr = by_q(shuf, "L_eff", "x", "r1")
        if rr:
            null.append(max(abs(r["t"]) for r in rr))
    p95 = float(np.percentile(null, 95)) if null else float("nan")
    print(f"    实际 max|t| = {real_max:.2f}   置换 p95 = {p95:.2f}   "
          f"{'通过' if real_max > p95 else '不通过'}")
    print(f"    注：单特征 4×5 格；全网格 {len(grid)} 格，Bonferroni 5% 临界 |t|≈"
          f"{2.5 + 0.5 * np.log10(max(len(grid), 2)):.2f}（近似）")
    out["h4"] = {"real_max_t": round(real_max, 2), "perm_p95": round(p95, 2),
                 "grid": len(grid)}

    # ---------------- 四态标签转移 ----------------
    print("\n" + "=" * 96)
    print("[H5] 「低开高走 / 高开低走」四态转移（gap 符号 x 全天方向符号）")
    lab = (df.with_columns([
        pl.when((pl.col("gap") < 0) & (pl.col("i1") > 0)).then(pl.lit("低开高走"))
        .when((pl.col("gap") > 0) & (pl.col("i1") < 0)).then(pl.lit("高开低走"))
        .when((pl.col("gap") > 0) & (pl.col("i1") > 0)).then(pl.lit("高开高走"))
        .when((pl.col("gap") < 0) & (pl.col("i1") < 0)).then(pl.lit("低开低走"))
        .otherwise(None).alias("lab")])
        .with_columns(pl.col("lab").shift(1).over("symbol").alias("lab1"))
        .drop_nulls(["lab", "lab1"]))
    order = ["低开高走", "高开低走", "高开高走", "低开低走"]
    tab = (lab.group_by(["lab1", "lab"]).len()
              .pivot(on="lab", index="lab1", values="len").fill_null(0))
    tot = lab.group_by("lab1").len().rename({"len": "_n"})
    tab = tab.join(tot, on="lab1")
    print(f"    {'T-1\\T':<10}" + "".join(f"{c:>10}" for c in order) + f"{'合计':>9}")
    trans = {}
    for r in tab.iter_rows(named=True):
        cells = [r.get(c, 0) / r["_n"] for c in order]
        print(f"    {r['lab1']:<10}" + "".join(f"{x:>9.1%} " for x in cells)
              + f"{r['_n']:>9}")
        trans[r["lab1"]] = {c: round(float(x), 4) for c, x in zip(order, cells)}
    out["h5_transition"] = trans

    print("\n  T 日做T收益 按 (T-1 标签, T 日标签) 分组 —— [NOW] 组合里含事后标签，仅归因")
    print(f"    {'T-1标签':<10}{'T日标签':<10}{'正T净':>8}{'t':>6}{'负T净':>8}{'t':>6}"
          f"{'正T毛':>8}{'负T毛':>8}{'n':>9}")
    comb = []
    for a in order:
        for b in order:
            s = lab.filter((pl.col("lab1") == a) & (pl.col("lab") == b))
            if s.height < 5000:
                continue
            v1, t1, n1, _ = day_mean_t(s, "r1")
            v2, t2, _, _ = day_mean_t(s, "r2")
            print(f"    {a:<10}{b:<10}{v1 * 1e4:>+8.1f}{t1:>6.1f}{v2 * 1e4:>+8.1f}"
                  f"{t2:>6.1f}{v1 * 1e4 + 12:>+8.1f}{v2 * 1e4 + 12:>+8.1f}{s.height:>9}")
            comb.append({"lab1": a, "lab": b, "r1": round(v1 * 1e4, 1),
                         "t1": round(t1, 2), "r2": round(v2 * 1e4, 1),
                         "t2": round(t2, 2), "n": s.height})
    out["h5_comb"] = comb

    # ---------------- 方向选择策略 ----------------
    print("\n" + "=" * 96)
    print("[S] 用 T-1 标签择向：低开高走->次日做正T；高开低走->次日做负T")
    rule = {"低开高走": "p1", "高开低走": "p2", "高开高走": "p1", "低开低走": "p2"}
    parts = []
    for k, v in rule.items():
        parts.append(lab.filter(pl.col("lab1") == k).select(["date", pl.col(v).alias("rr")]))
    strat = pl.concat(parts)
    v, t, n, nd = day_mean_t(strat, "rr")
    print(f"    策略 per_day {v * 1e4:+7.1f}bp  t={t:+5.2f}  n={n}  days={nd}")
    print(f"    对照 全做正T {base['正T/per_day']['bp']:+.1f}bp / "
          f"全做负T {base['负T/per_day']['bp']:+.1f}bp")
    out["strat_dir"] = {"bp": round(v * 1e4, 1), "t": round(t, 2), "n": n}

    p = ROOT / "analysis" / "_diag_t0_shape.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {p}")


if __name__ == "__main__":
    main()
