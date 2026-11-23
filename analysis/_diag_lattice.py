"""饱和项检验：低价股的 ρ₁ = −0.27 到底是「额外价差」还是「离散格点算术」？

三个模型无关的判别：
  A. 格点模拟。给定实测 Var(Δ)（单位：报价单位 tick），模拟一个**完全没有价差**的
     整数格点随机漫步 Y = round(X)，反解潜变量方差使模拟 Var(ΔY) 等于实测值，
     再看模拟 ρ₁ 能否复现实测 ρ₁。零自由参数。若复现 → 量化算术；若远不够 → 真实价差。
  B. 转移矩阵 P(Δ_{t+1} | Δ_t)，按价格分组。看弹回深度：+1 tick 是纯弹回还是延续。
  C. 隐含半价差（以 tick 计）随价格的轨迹：h_tick = sd_tick·sqrt(−ρ₁)。常数 0.5 = 理论。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_lattice.json"
STRIDE = 4

BINS = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 17, 20, 25, 30, 40, 55, 75, 100, 1000]


def bin_label(b: int) -> str:
    lo, hi = BINS[b], BINS[b + 1]
    return f"{lo}-{hi}" if hi < 1000 else f"{lo}+"


def rounded_rw_rho1(var_target: float, rng, n_paths: int = 40000, n_bars: int = 48):
    """零价差格点漫步：Y=round(X)。反解 latent var 使 Var(ΔY)=var_target。
    返回 (rho1_sim, latent_sd, var_sim, zero_frac_sim)。"""
    grid = np.exp(np.linspace(np.log(var_target) - 1.5, np.log(var_target) + 1.5, 80))
    rows = []
    for v in grid:
        x = np.cumsum(rng.normal(0.0, np.sqrt(v), size=(n_paths, n_bars)), axis=1)
        d = np.diff(np.rint(x), axis=1)
        v_sim = float((d * d).mean())
        num = float((d[:, :-1] * d[:, 1:]).mean())
        rows.append((v, v_sim, num / v_sim if v_sim > 0 else 0.0, float((d == 0).mean())))
    arr = np.array(rows)
    i = int(np.argsort(np.abs(arr[:, 1] - var_target))[0])
    return float(arr[i, 2]), float(np.sqrt(arr[i, 0])), float(arr[i, 1]), float(arr[i, 3])


def main() -> None:
    days = sorted(p.name.split("=", 1)[1] for p in SRC.iterdir() if p.is_dir())
    sample = days[::STRIDE]
    files = [str((SRC / f"date={d}" / "part.parquet").as_posix()) for d in sample]
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet({files!r})")
    con.execute(
        """
        CREATE TABLE b48 AS
        SELECT * FROM (
          SELECT symbol, date, datetime, close,
                 row_number() OVER (PARTITION BY symbol, date ORDER BY datetime) AS j,
                 count(*)     OVER (PARTITION BY symbol, date) AS nb
          FROM raw) WHERE nb = 48
        """
    )
    # 以「报价单位」计的整数步长 + 下一根
    con.execute(
        """
        CREATE TABLE t1 AS
        SELECT symbol, date, j, round((close - lag(close) OVER w) * 100) AS d,
               close / lag(close) OVER w - 1 AS rr
        FROM b48
        WINDOW w AS (PARTITION BY symbol, date ORDER BY j)
        """
    )
    con.execute(
        """
        CREATE TABLE td AS
        SELECT symbol, date, j, d, rr,
               lead(d) OVER (PARTITION BY symbol, date ORDER BY j) AS dn,
               lead(rr) OVER (PARTITION BY symbol, date ORDER BY j) AS rn
        FROM t1
        """
    )
    con.execute("CREATE TABLE tdx AS SELECT * FROM td WHERE d IS NOT NULL AND abs(d) <= 25")

    cases = " ".join(f"WHEN mpx >= {BINS[i]} AND mpx < {BINS[i+1]} THEN {i}" for i in range(len(BINS) - 1))
    con.execute(
        f"""
        CREATE TABLE bk AS
        SELECT symbol, date, mpx,
               CASE {cases} END AS b
        FROM (SELECT symbol, date, max_by(close, j) AS mpx FROM b48 GROUP BY 1, 2)
        WHERE mpx < 1000
        """
    )
    con.execute("CREATE TABLE tj AS SELECT t.*, k.b, k.mpx FROM tdx t JOIN bk k USING (symbol, date) WHERE k.b IS NOT NULL")

    cells = con.sql(
        """
        SELECT b, count(*) AS n_pair,
               avg(d*d)   AS var_tick,
               avg(d*dn)  AS cov_tick,
               avg(rr*rr) * 1e8 AS var_bp2,
               avg(rr*rn) * 1e8 AS cov_bp2,
               avg(CASE WHEN d=0 THEN 1.0 ELSE 0 END) AS zf,
               avg(CASE WHEN abs(d)=1 THEN 1.0 ELSE 0 END) AS p1,
               avg(CASE WHEN abs(d)=2 THEN 1.0 ELSE 0 END) AS p2,
               avg(CASE WHEN abs(d)>=3 THEN 1.0 ELSE 0 END) AS p3,
               median(mpx) AS mpx,
               sum(mpx * d * d) / sum(d * d) AS mpx_varw,
               count(DISTINCT symbol) AS nsym,
               count(DISTINCT date) AS nday
        FROM tj GROUP BY 1 ORDER BY 1
        """
    ).df()
    cells["rho1"] = cells.cov_tick / cells.var_tick
    cells["rho1_bp"] = cells.cov_bp2 / cells.var_bp2
    cells["sd_tick"] = np.sqrt(cells.var_tick)
    cells["sd_bp"] = np.sqrt(cells.var_bp2)
    cells["tick_bp"] = 0.01 / cells.mpx * 1e4
    cells["sd_bp_t"] = cells.sd_tick * cells.tick_bp      # 由 tick 单位折算的 bp 波动
    cells["h_bp"] = cells.sd_bp * np.sqrt(-cells.rho1_bp)
    cells["h_tick"] = cells.h_bp / cells.tick_bp
    cells["h_bp_lin"] = 0.5 * cells.tick_bp               # α=0.5 线性模型给的低价端预测

    # ---------- A：零价差格点模拟 ----------
    rng = np.random.default_rng(20261121)
    rho_s, lat_s, zf_s = [], [], []
    for _, r in cells.iterrows():
        if r.n_pair < 20000:
            rho_s.append(np.nan); lat_s.append(np.nan); zf_s.append(np.nan); continue
        a, b_, c, d_ = rounded_rw_rho1(float(r.var_tick), rng)
        rho_s.append(a); lat_s.append(b_); zf_s.append(d_)
    cells["rho1_lattice"] = rho_s
    cells["latent_sd_tick"] = lat_s
    cells["zf_lattice"] = zf_s
    cells["resid_lattice"] = cells.rho1 - cells.rho1_lattice

    # ---------- B：转移矩阵 ----------
    tm = con.sql(
        """
        SELECT b, d, dn, count(*) AS n FROM tj
        WHERE d BETWEEN -3 AND 3 AND dn BETWEEN -3 AND 3
        GROUP BY 1,2,3 ORDER BY 1,2,3
        """
    ).df()
    trans = {}
    for b, g in tm.groupby("b"):
        piv = g.pivot_table(index="d", columns="dn", values="n", aggfunc="sum").fillna(0)
        trans[int(b)] = (piv.div(piv.sum(axis=1), axis=0)).round(4)

    out = {
        "meta": {"days": len(sample),
                 "bars": int(con.sql("SELECT count(*) FROM tj").fetchone()[0]),
                 "bins": [(bin_label(int(r.b)), float(r.mpx)) for _, r in cells.iterrows()]},
        "cells": cells.replace({np.nan: None}).round(6).to_dict("records"),
        "transition": {str(k): v.to_dict() for k, v in trans.items()},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    f = lambda v, w=9, p=4: (f"{v:{w}.{p}f}" if np.isfinite(v) else " " * (w - 1) + "-")
    print(f"{'价格桶':>9} {'中位价':>7} {'方差权价':>8} {'只数':>5} {'n_pair':>9} {'sd_bp':>6} {'sd_tick':>7} "
          f"{'rho1':>8} {'rho1_bp':>8} {'格点':>8} {'残差':>8} {'h_tick':>7} {'零占比':>6} {'格点零':>7}")
    for _, r in cells.iterrows():
        print(f"{bin_label(int(r.b)):>9} {r.mpx:7.2f} {r.mpx_varw:8.2f} {int(r.nsym):5d} {int(r.n_pair):9d} "
              f"{r.sd_bp:6.1f} {r.sd_tick:7.3f} {r.rho1:8.4f} {r.rho1_bp:8.4f} {f(r.rho1_lattice,8)} {f(r.resid_lattice,8)} "
              f"{r.h_tick:7.3f} {r.zf*100:5.1f}% {f(r.zf_lattice*100,7,1)}")
    print("\n== 转移矩阵  P(下一根 = k' | 本根 = k)，单位 tick")
    for b in sorted(trans):
        p = trans[b]
        print(f"\n-- 价格桶 {bin_label(b)}   (n={int(tm[tm.b==b].n.sum()):,})")
        print("          " + "".join(f"{c:>8}" for c in p.columns))
        for i in p.index:
            print(f"  d={i:>3}   " + "".join(f"{v*100:7.2f}%" for v in p.loc[i]))
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
