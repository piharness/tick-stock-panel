"""日内做T × 公民阶层迁移（升格/降格）

## 口径（先写，后跑）

**做T** = 底仓 T+0 日内等量往返，收盘股数不变。复用 `data/_probe_t0/trades.parquet`
的 `r1`（正T：跌 d 买、回锚卖）与 `r2`（负T：涨 d 卖、回锚买），**已扣 12bp 往返成本**
（佣金双边 + 印花 5bp + 滑点）。只取 bar 的 open/close 成交，不用 high/low。

**迁移必须 T 日开盘前可知**：`tr = level(T-2) -> level(T-1)`。
绝不用 `level(T)` —— 那是当日收盘才算出来的，用了就是未来函数。
事后版（T-1→T）单独作对照，只用于判断「事前版是不是靠当日价格反推出来的」。

**统计**：每笔（票日）等权；t 用按交易日 bootstrap（B=500），有效样本 = 交易日数。
**差分** = 迁移组 − **同目标等级的停留者**。这一项才是「迁移的边际」，
不减掉它就是「等级本身」的投影。

**毛收益** = 净 + 12bp。毛 ≤ 12bp ⇒ 不是做T赚钱，是成本没算够。

## 预注册判据
C1 任一迁移 × 任一方向，净 bp > 0 且 t > 2.0 ⇒ 该格「做T成立」。
C2 差分 Δ 须 t > 2.0，才算迁移有独立边际；否则是等级本身的投影。
C3 按日序 70%/30% 切分，Δ 符号一致。
C4 按日置换迁移标签 200 次，实际 Δ 极差 > p95。
C5 事后版显著而事前版不显著 ⇒ 只是当日价格的结果，不可用。
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
COST_BP = 12.0


def build() -> tuple[pl.DataFrame, pl.DataFrame]:
    p = (pl.read_parquet(OUT / "panel.parquet", columns=["symbol", "date", "level", "ampl"])
         .sort(["symbol", "date"]))
    p = p.with_columns([
        pl.col("level").shift(1).over("symbol").alias("l1"),
        pl.col("level").shift(2).over("symbol").alias("l2"),
        pl.col("ampl").shift(1).over("symbol").alias("ampl1"),
    ])
    # 事前：T 日开盘前可知
    p = p.with_columns(
        pl.when(pl.col("l1").is_not_null() & pl.col("l2").is_not_null())
          .then(pl.concat_str([pl.col("l2"), pl.col("l1")], separator=">"))
          .otherwise(None).alias("tr"),
        # 事后：同日收盘才知
        pl.concat_str([pl.col("l1"), pl.col("level")], separator=">").alias("tr_post"),
    )
    t = (pl.read_parquet(OUT / "trades.parquet")
         .drop("level")
         .join(p.select(["symbol", "date", "tr", "tr_post", "l1", "ampl1"]),
               on=["symbol", "date"], how="inner"))
    return t, p


def pt(df: pl.DataFrame, col: str, by: list[str], seed: int = 7) -> list[dict]:
    """每笔等权均值 + 按日 bootstrap 的 t。"""
    g = (df.filter(pl.col(col).is_finite())
           .group_by(["date"] + by)
           .agg([pl.col(col).sum().alias("s"), pl.len().alias("n")]))
    res = []
    for k in g.select(by).unique().sort(by).iter_rows(named=True):
        sub = g
        for c in by:
            sub = sub.filter(pl.col(c) == k[c])
        if sub.height < 30:
            continue
        s, n = sub["s"].to_numpy(), sub["n"].to_numpy()
        if n.sum() < 100:
            continue
        v = s.sum() / n.sum()
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(s), (B, len(s)))
        bs = s[idx].sum(1) / n[idx].sum(1)
        se = float(bs.std())
        res.append({**k, "bp": v * 1e4, "t": v / se if se else float("nan"),
                    "lo": float(np.percentile(bs, 2.5)) * 1e4,
                    "hi": float(np.percentile(bs, 97.5)) * 1e4,
                    "trades": int(n.sum()), "days": int(sub.height)})
    return res


def delta(df: pl.DataFrame, col: str, tr: str, target: str, seed: int = 7) -> dict | None:
    """迁移组 − 同目标等级停留者，配对差分（按日 bootstrap）。"""
    a = df.filter((pl.col("tr") == tr) & pl.col(col).is_finite())
    b = df.filter((pl.col("tr") == f"{target}>{target}") & pl.col(col).is_finite())
    if a.height < 100 or b.height < 100:
        return None
    ga = a.group_by("date").agg([pl.col(col).sum().alias("s"), pl.len().alias("n")])
    gb = b.group_by("date").agg([pl.col(col).sum().alias("s"), pl.len().alias("n")])
    j = ga.join(gb, on="date", how="inner", suffix="_b").sort("date")
    if j.height < 30:
        return None
    sa, na = j["s"].to_numpy(), j["n"].to_numpy()
    sb, nb = j["s_b"].to_numpy(), j["n_b"].to_numpy()
    # 按日重采样，两个组同一天一起抽 —— 保持同日市场状态可比
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(j), (B, len(j)))
    d_bp = (sa[idx].sum(1) / na[idx].sum(1) - sb[idx].sum(1) / nb[idx].sum(1)) * 1e4
    pt_a = sa.sum() / na.sum() * 1e4
    pt_b = sb.sum() / nb.sum() * 1e4
    se = float(d_bp.std())
    return {"tr": tr, "target": target, "bp_move": pt_a, "bp_stay": pt_b,
            "delta": pt_a - pt_b, "t": (pt_a - pt_b) / se if se else float("nan"),
            "lo": float(np.percentile(d_bp, 2.5)), "hi": float(np.percentile(d_bp, 97.5)),
            "days": int(j.height), "n_move": int(na.sum()), "n_stay": int(nb.sum())}


def main() -> None:
    t, panel = build()
    print(f"trades {t.height:,}  days {t['date'].n_unique()}  span {t['date'].min()}~{t['date'].max()}")
    ok = t.filter(pl.col("tr").is_not_null())
    print(f"有迁移标签 {ok.height:,}")

    out: dict = {"meta": {"days": int(t["date"].n_unique()), "cost_bp": COST_BP,
                          "n_tagged": int(ok.height)}}

    # ---------- A. 迁移分组：每笔净收益 ----------
    print("\n" + "=" * 100)
    print("A. 每笔净收益（12bp 已扣，票日等权，t=按日 bootstrap）   迁移 = level(T-2) -> level(T-1)")
    print(f"{'d':>6}{'迁移':>24}{'方向':>5}{'净bp':>8}{'毛bp':>8}{'t':>7}"
          f"{'95%CI':>18}{'笔数':>9}{'天数':>6}")
    cells = []
    for dd in (0.005, 0.01, 0.02, 0.03):
        for col, tag in (("r1", "正T"), ("r2", "负T")):
            rows = pt(ok.filter(pl.col("d") == dd), col, ["tr"])
            for r in sorted(rows, key=lambda x: -x["trades"]):
                gross = r["bp"] + COST_BP
                print(f"{dd:>6.3f}{r['tr'].replace('>', '→'):>24}{tag:>5}"
                      f"{r['bp']:>8.1f}{gross:>8.1f}{r['t']:>7.2f}"
                      f"   [{r['lo']:+6.1f},{r['hi']:+6.1f}]{r['trades']:>9}{r['days']:>6}")
                cells.append({"d": dd, "tr": r["tr"], "dir": col, "bp": round(r["bp"], 1),
                              "gross": round(gross, 1), "t": round(r["t"], 2),
                              "n": r["trades"], "days": r["days"]})
    out["cells"] = cells

    # ---------- B. 迁移的边际：− 同目标等级停留者 ----------
    print("\n" + "=" * 100)
    print("B. 差分 Δ = 迁移组 − 同目标等级『停留者』（拆出迁移的边际，不减掉就是等级本身的投影）")
    moves = [(a, b) for a in LEVELS for b in LEVELS if a != b]
    for dd in (0.005, 0.01, 0.02, 0.03):
        for col, tag in (("r1", "正T"), ("r2", "负T")):
            rows = []
            for a, b in moves:
                r = delta(ok.filter(pl.col("d") == dd), col, f"{a}>{b}", b)
                if r:
                    rows.append(r)
            if not rows:
                continue
            print(f"\n  d={dd}  {tag}")
            print(f"    {'迁移':>24}{'迁移组':>9}{'停留组':>9}{'Δ':>9}{'t':>7}"
                  f"{'95%CI':>18}{'笔数':>9}")
            for r in sorted(rows, key=lambda x: -abs(x["t"])):
                print(f"    {r['tr'].replace('>', '→'):>24}{r['bp_move']:>9.1f}"
                      f"{r['bp_stay']:>9.1f}{r['delta']:>9.1f}{r['t']:>7.2f}"
                      f"   [{r['lo']:+6.1f},{r['hi']:+6.1f}]{r['n_move']:>9}")
            out[f"delta_{col}_{dd}"] = [
                {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()}
                for r in rows]

    # ---------- C. 事前 vs 事后 ----------
    print("\n" + "=" * 100)
    print("C. 事前(tr: T-2→T-1) vs 事后(tr_post: T-1→T)，d=0.01 正T")
    sub = ok.filter(pl.col("d") == 0.01)
    for name, cn in (("tr", "事前"), ("tr_post", "事后")):
        rows = pt(sub, "r1", [name])
        best = sorted(rows, key=lambda x: -x["bp"])[:3]
        worst = sorted(rows, key=lambda x: x["bp"])[:2]
        print(f"  {cn}（{name}）极值：")
        for r in best + worst:
            print(f"    {r[name].replace('>', '→'):>24} 净{r['bp']:>7.1f}bp  t={r['t']:>6.2f}  n={r['trades']:>8}")
        out[f"t0_{name}"] = [{"tr": r[name], "bp": round(r["bp"], 1), "t": round(r["t"], 2),
                              "n": r["trades"]} for r in rows]

    # ---------- D. 振幅分层：迁移差是不是振幅的投影 ----------
    print("\n" + "=" * 100)
    print("D. 控制日内振幅后，Δ 是否还在（d=0.01，按 ampl1 五分位）")
    a = ok.filter(pl.col("d") == 0.01, pl.col("ampl1").is_not_null())
    a = a.with_columns(pl.col("ampl1").qcut(5, labels=[f"Q{i}" for i in range(1, 6)],
                                            allow_duplicates=True).alias("aq"))
    for col, tag in (("r1", "正T"), ("r2", "负T")):
        print(f"\n  {tag}")
        print(f"    {'迁移':>22}{'Q1':>8}{'Q2':>8}{'Q3':>8}{'Q4':>8}{'Q5':>8}")
        rec = {}
        for mv in ("background>active", "active>background", "silent>background",
                   "core>active", "active>core"):
            tt = mv.split(">")[1]
            vals = []
            for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                g = a.filter(pl.col("aq") == q)
                r = delta(g, col, mv, tt)
                vals.append(f"{r['delta']:+8.1f}" if r else "       -")
            print(f"    {mv.replace('>', '→'):>22}" + "".join(vals))
            rec[mv] = vals
        out[f"ampl_strat_{col}"] = rec

    # ---------- E. 前后半稳定性 ----------
    print("\n" + "=" * 100)
    print("E. 按日序 70%/30% 切分（d=0.01 正T，Δ）")
    days = sorted(ok["date"].unique().to_list())
    cut = days[int(len(days) * 0.7)]
    for col, tag in (("r1", "正T"), ("r2", "负T")):
        print(f"  {tag}  切点 {cut}")
        print(f"    {'迁移':>22}{'前70%':>10}{'后30%':>10}{'一致':>6}")
        rec = {}
        for mv in ("background>active", "active>background", "silent>background",
                   "core>active", "active>core"):
            tt = mv.split(">")[1]
            s1 = ok.filter(pl.col("d") == 0.01, pl.col("date") < cut)
            s2 = ok.filter(pl.col("d") == 0.01, pl.col("date") >= cut)
            r1_, r2_ = delta(s1, col, mv, tt), delta(s2, col, mv, tt)
            v1 = r1_["delta"] if r1_ else float("nan")
            v2 = r2_["delta"] if r2_ else float("nan")
            same = "Y" if (np.isfinite(v1) and np.isfinite(v2) and v1 * v2 > 0) else "N"
            print(f"    {mv.replace('>', '→'):>22}{v1:>10.1f}{v2:>10.1f}{same:>6}")
            rec[mv] = [v1, v2]
        out[f"half_{col}"] = rec

    # ---------- F. 置换负控 ----------
    print("\n" + "=" * 100)
    print("F. 置换负控：按日打乱迁移标签 200 次（d=0.01 正T），实际 Δ 极差须超 p95")
    s = ok.filter(pl.col("d") == 0.01, pl.col("r1").is_finite())
    real = [delta(s, "r1", mv, mv.split(">")[1]) for mv in
            ("background>active", "active>background", "silent>background",
             "core>active", "active>core")]
    real = [r["delta"] for r in real if r]
    rng = np.random.default_rng(20261121)
    perm = []
    for _ in range(200):
        sh = s.with_columns(
            pl.col("tr").shuffle(seed=int(rng.integers(0, 2**31))).over("date").alias("trp"))
        vals = []
        for mv in ("background>active", "active>background", "silent>background",
                   "core>active", "active>core"):
            rr = delta(sh.with_columns(pl.col("trp").alias("tr")), "r1", mv, mv.split(">")[1])
            if rr:
                vals.append(rr["delta"])
        if vals:
            perm.append(max(vals) - min(vals))
    if perm and real:
        ract = max(real) - min(real)
        p95 = float(np.percentile(perm, 95))
        print(f"  实际极差 {ract:.1f}bp   置换 p95 {p95:.1f}bp   "
              f"{'通过' if ract > p95 else '不通过'}")
        out["perm"] = {"real_range": round(ract, 1), "p95": round(p95, 1),
                       "pass": bool(ract > p95)}

    # ---------- G. 对照：等级本身的做T（不含迁移） ----------
    print("\n" + "=" * 100)
    print("G. 对照：按 T-1 等级（不看迁移），d=0.01")
    for col, tag in (("r1", "正T"), ("r2", "负T")):
        rows = pt(ok.filter(pl.col("d") == 0.01), col, ["l1"])
        print(f"  {tag}: " + "  ".join(
            f"{CN[r['l1']]} {r['bp']:+.1f}bp(t={r['t']:+.2f})" for r in sorted(rows, key=lambda x: x["l1"])))

    out_path = ROOT / "analysis" / "_diag_citizen_t0_transition.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {out_path}")


if __name__ == "__main__":
    main()
