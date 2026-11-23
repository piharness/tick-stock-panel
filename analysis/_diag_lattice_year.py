"""函数形式比较 + α 的时间稳定性（散户涌入期的负控）。

1. 20 个绝对价格桶上比较 5 种 h 的函数形式，看「冲击项」到底是常数还是剖面。
2. 按年拟合 (α, β)：若 α 在 2024Q4（散户涌入）与 2025/2026 之间不动，
   则「tick 系数」与参与者结构无关——这是本地能做的最干净的负控。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_lattice_year.json"
STRIDE = 4
BINS = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 17, 20, 25, 30, 40, 55, 75, 100, 1000]


def lab(b):
    lo, hi = BINS[b], BINS[b + 1]
    return f"{lo}-{hi}" if hi < 1000 else f"{lo}+"


def r2(p, y):
    return float(1 - ((p - y) ** 2).sum() / ((y - y.mean()) ** 2).sum())


def main() -> None:
    days = sorted(p.name.split("=", 1)[1] for p in SRC.iterdir() if p.is_dir())
    sample = days[::STRIDE]
    files = [str((SRC / f"date={d}" / "part.parquet").as_posix()) for d in sample]
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet({files!r})")
    con.execute("""CREATE TABLE b48 AS SELECT * FROM (
        SELECT symbol,date,close,row_number() OVER (PARTITION BY symbol,date ORDER BY datetime) j,
               count(*) OVER (PARTITION BY symbol,date) nb FROM raw) WHERE nb=48""")
    con.execute("""CREATE TABLE t1 AS
        SELECT symbol,date,j,round((close-lag(close) OVER w)*100) d, close/lag(close) OVER w-1 rr
        FROM b48 WINDOW w AS (PARTITION BY symbol,date ORDER BY j)""")
    con.execute("""CREATE TABLE td AS
        SELECT symbol,date,j,d,rr,lead(rr) OVER (PARTITION BY symbol,date ORDER BY j) rn FROM t1""")
    cases = " ".join(f"WHEN mpx>={BINS[i]} AND mpx<{BINS[i+1]} THEN {i}" for i in range(len(BINS) - 1))
    con.execute(f"""CREATE TABLE bk AS
        SELECT symbol,date,mpx,CASE {cases} END b FROM
        (SELECT symbol,date,max_by(close,j) mpx FROM b48 GROUP BY 1,2) WHERE mpx<1000""")
    con.execute("""CREATE TABLE tj AS SELECT t.*,k.b,k.mpx,strftime(t.date,'%Y') yr
        FROM td t JOIN bk k USING(symbol,date) WHERE k.b IS NOT NULL AND t.rr IS NOT NULL AND t.rn IS NOT NULL""")

    cells = con.sql("""
        SELECT b, yr, count(*) n,
               avg(rr*rr)*1e8 var_bp2, avg(rr*rn)*1e8 cov_bp2,
               sum(rr*rr*mpx)/sum(rr*rr) p_varw, median(mpx) p_med,
               avg(CASE WHEN d=0 THEN 1.0 ELSE 0 END) zf
        FROM tj GROUP BY 1,2 ORDER BY 1,2
    """).df()
    cells["rho1"] = cells.cov_bp2 / cells.var_bp2
    cells["sd_bp"] = np.sqrt(cells.var_bp2)
    cells["tick_bp"] = 0.01 / cells.p_varw * 1e4
    cells["tick_bp_med"] = 0.01 / cells.p_med * 1e4

    def M1(df, a, b):      # 正交叠加（1 tick 项 + 冲击项）
        return np.sqrt((a * df.tick_bp) ** 2 + (b * df.sd_bp) ** 2)

    def M2(df, a, g, b):   # 冲击项带指数
        return np.sqrt((a * df.tick_bp) ** 2 + (b * df.sd_bp ** g) ** 2)

    def M3(df, a, b):      # 线性相加
        return a * df.tick_bp + b * df.sd_bp

    def M4(df, a, b):      # 取大
        return np.maximum(a * df.tick_bp, b * df.sd_bp)

    def sse_of(fn, y):
        return float(((fn - y) ** 2).sum())

    # ---- 1. 函数形式比较（全样本 20 桶）----
    cells["wv"] = cells.var_bp2 * cells.n
    cells["wc"] = cells.cov_bp2 * cells.n
    pooled = cells.groupby("b", as_index=False).agg(
        n=("n", "sum"), wv=("wv", "sum"), wc=("wc", "sum"),
        tick_bp=("tick_bp", "first"), p_varw=("p_varw", "first"), zf=("zf", "first"))
    pooled = pooled[pooled.n >= 5000].sort_values("b").reset_index(drop=True)
    pooled["var_bp2"] = pooled.wv / pooled.n
    pooled["cov_bp2"] = pooled.wc / pooled.n
    pooled["rho1"] = pooled.cov_bp2 / pooled.var_bp2
    pooled["sd_bp"] = np.sqrt(pooled.var_bp2)
    y = pooled.rho1.values

    forms = {}
    best = (1e9, 0, 0)
    for a in np.arange(0.30, 0.71, 0.01):
        for b in np.arange(0.05, 0.31, 0.002):
            s = sse_of(-((M1(pooled, a, b) / pooled.sd_bp.values) ** 2), y)
            if s < best[0]:
                best = (s, a, b)
    forms["M1 正交(α,β)"] = {"a": best[1], "b": best[2],
                          "r2": r2(-((M1(pooled, best[1], best[2]) / pooled.sd_bp.values) ** 2), y)}
    best = (1e9, 0)
    for b in np.arange(0.05, 0.31, 0.002):
        s = sse_of(-((M1(pooled, 0.5, b) / pooled.sd_bp.values) ** 2), y)
        if s < best[0]:
            best = (s, b)
    forms["M1 α=0.5 固定"] = {"a": 0.5, "b": best[1],
                          "r2": r2(-((M1(pooled, 0.5, best[1]) / pooled.sd_bp.values) ** 2), y)}
    best = (1e9, 0, 0, 0)
    for a in np.arange(0.35, 0.66, 0.01):
        for g in np.arange(0.6, 1.41, 0.02):
            for b in np.arange(0.2, 3.01, 0.05):
                s = sse_of(-((M2(pooled, a, g, b) / pooled.sd_bp.values) ** 2), y)
                if s < best[0]:
                    best = (s, a, g, b)
    forms["M2 冲击带指数"] = {"a": best[1], "g": best[2], "b": best[3],
                        "r2": r2(-((M2(pooled, best[1], best[2], best[3]) / pooled.sd_bp.values) ** 2), y)}
    best = (1e9, 0, 0)
    for a in np.arange(0.35, 0.71, 0.01):
        for b in np.arange(0.05, 0.31, 0.002):
            s = sse_of(-((M3(pooled, a, b) / pooled.sd_bp.values) ** 2), y)
            if s < best[0]:
                best = (s, a, b)
    forms["M3 线性相加"] = {"a": best[1], "b": best[2],
                       "r2": r2(-((M3(pooled, best[1], best[2]) / pooled.sd_bp.values) ** 2), y)}
    best = (1e9, 0, 0)
    for a in np.arange(0.35, 0.71, 0.01):
        for b in np.arange(0.05, 0.31, 0.002):
            s = sse_of(-((M4(pooled, a, b) / pooled.sd_bp.values) ** 2), y)
            if s < best[0]:
                best = (s, a, b)
    forms["M4 取大"] = {"a": best[1], "b": best[2],
                     "r2": r2(-((M4(pooled, best[1], best[2]) / pooled.sd_bp.values) ** 2), y)}

    # ---- 2. 分年 α ----
    per_year = {}
    for yr in sorted(cells.yr.unique()):
        g = cells[cells.yr == yr].sort_values("b").reset_index(drop=True)
        g = g[g.n >= 5000].reset_index(drop=True)
        yy = g.rho1.values
        bb = (1e9, 0, 0)
        for a in np.arange(0.30, 0.81, 0.01):
            for b in np.arange(0.03, 0.35, 0.002):
                s = sse_of(-((M1(g, a, b) / g.sd_bp.values) ** 2), yy)
                if s < bb[0]:
                    bb = (s, a, b)
        # α 单独（自由 β）之外，也给出「仅用 4 个最便宜桶」的 α（tick 主导区）
        low = g[g.b <= 2]
        if len(low) >= 2:
            la = (1e9, 0)
            for a in np.arange(0.30, 0.81, 0.005):
                s = sse_of(-(((a * low.tick_bp) / low.sd_bp) ** 2), low.rho1.values)
                if s < la[0]:
                    la = (s, a)
            a_low = float(la[1])
        else:
            a_low = np.nan
        per_year[yr] = {"n": int(g.n.sum()), "bins": int(len(g)),
                        "alpha": float(bb[1]), "beta": float(bb[2]),
                        "alpha_lowprice": a_low,
                        "r2": r2(-((M1(g, bb[1], bb[2]) / g.sd_bp.values) ** 2), yy)}

    out = {"forms": forms, "per_year": per_year,
           "pooled": pooled.round(6).to_dict("records"),
           "cells_by_year": cells.round(6).to_dict("records")}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("== 函数形式比较（20 个绝对价格桶，ρ₁ 由 h 决定）")
    for k, v in forms.items():
        extra = f" g={v['g']:.2f}" if "g" in v else ""
        print(f"  {k:>16}: α={v['a']:.2f} β={v['b']:.3f}{extra}  R²={v['r2']:.4f}")
    print("\n== 残差符号（M1，α=0.5）")
    pr = -((M1(pooled, 0.5, forms['M1 α=0.5 固定']['b']) / pooled.sd_bp.values) ** 2)
    for i, r in pooled.iterrows():
        print(f"  {lab(int(r.b)):>9} p={r.p_varw:7.2f} σ={r.sd_bp:5.1f} 实测={r.rho1:+.4f} "
              f"预测={pr[i]:+.4f} 残差={r.rho1-pr[i]:+.4f}")
    print("\n== 分年（负控：α 是否随散户涌入变动）")
    print(f"{'年':>6} {'n_pair':>10} {'桶':>3} {'α':>6} {'β':>6} {'α(低价4桶)':>10} {'R²':>7}")
    for yr, v in per_year.items():
        print(f"{yr:>6} {v['n']:10d} {v['bins']:3d} {v['alpha']:6.3f} {v['beta']:6.3f} "
              f"{v['alpha_lowprice']:10.3f} {v['r2']:7.4f}")


if __name__ == "__main__":
    main()
