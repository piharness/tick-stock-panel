"""α 的日级 bootstrap 置信区间：0.47 与理论 0.50 是否可区分。

n_eff = 交易日数（不是 pair 数），所以按日重采样。产出每桶的 α 点估计、SE、95%CI，
以及 2024Q4 / 2025 / 2026 分年的 α（散户涌入期的负控）。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_lattice_boot.json"
CACHE = ROOT / "analysis" / "_cache_bin_day.parquet"
STRIDE = 4
BINS = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 17, 20, 25, 30, 40, 55, 75, 100, 1000]


def prep(cd):
    cd = cd.copy()
    cd["rho1"] = cd["cov"] / cd["v"]
    cd["sd_bp"] = np.sqrt(cd.v / cd.n) * 1e4
    return cd


def cells_from(cd):
    d = cd.assign(wp=cd.v * cd.p_varw)
    g = d.groupby("b", as_index=False).agg(n=("n", "sum"), v=("v", "sum"), cov=("cov", "sum"),
                                           wp=("wp", "sum"))
    g = g[g.n >= 3000]
    g["p_varw"] = g.wp / g.v          # 代表价必须按 r² 加权，不能取 days 里的第一个
    g["tick_bp"] = 0.01 / g.p_varw * 1e4
    return prep(g)


def fit(cd, low_only=False):
    g = cells_from(cd)
    if low_only:
        g = g[g.b <= 3]
    if len(g) < 3:
        return np.nan, np.nan, np.nan
    tick, sd, y = g.tick_bp.values, g.sd_bp.values, g.rho1.values
    best = (1e18, 0.0, 0.0)
    for a in np.arange(0.30, 0.721, 0.01):
        for b in np.arange(0.04, 0.361, 0.004):
            h = np.sqrt((a * tick) ** 2 + (b * sd) ** 2)
            s = float(((-((h / sd) ** 2) - y) ** 2).sum())
            if s < best[0]:
                best = (s, a, b)
    return best[1], best[2], g


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
        SELECT symbol,date,j,close/lag(close) OVER w-1 rr
        FROM b48 WINDOW w AS (PARTITION BY symbol,date ORDER BY j)""")
    con.execute("""CREATE TABLE td AS
        SELECT symbol,date,j,rr,lead(rr) OVER (PARTITION BY symbol,date ORDER BY j) rn FROM t1""")
    cases = " ".join(f"WHEN mpx>={BINS[i]} AND mpx<{BINS[i+1]} THEN {i}" for i in range(len(BINS) - 1))
    con.execute(f"""CREATE TABLE bk AS
        SELECT symbol,date,mpx,CASE {cases} END b FROM
        (SELECT symbol,date,max_by(close,j) mpx FROM b48 GROUP BY 1,2) WHERE mpx<1000""")
    con.execute("""CREATE TABLE tj AS SELECT t.*,k.b,k.mpx FROM td t JOIN bk k USING(symbol,date)
        WHERE k.b IS NOT NULL AND t.rr IS NOT NULL AND t.rn IS NOT NULL""")
    con.execute("""
        CREATE TABLE cdb AS
        SELECT b, date, count(*) n, sum(rr*rr) v, sum(rr*rn) cov,
               sum(rr*rr*mpx)/sum(rr*rr) p_varw,
               0.01 / (sum(rr*rr*mpx)/sum(rr*rr)) * 1e4 AS tick_bp,
               strftime(date,'%Y') yr
        FROM tj GROUP BY b, date ORDER BY b, date
    """)
    con.execute(f"COPY cdb TO '{CACHE.as_posix()}' (FORMAT PARQUET, OVERWRITE)")
    cd = con.sql("SELECT * FROM cdb").df()

    a0, b0, g0 = fit(cd)
    a_low, _, _ = fit(cd, low_only=True)
    print(f"点估计：α={a0:.3f}  β={b0:.3f}   低价4桶 α={a_low:.3f}   (桶数 {len(g0)})")
    print("残差（α 自由）：")
    tick, sd, y = g0.tick_bp.values, g0.sd_bp.values, g0.rho1.values
    pr = -((np.sqrt((a0 * tick) ** 2 + (b0 * sd) ** 2) / sd) ** 2)
    for i, r in g0.reset_index(drop=True).iterrows():
        print(f"  b={int(r.b):>2} p={r.p_varw:7.2f} σ={r.sd_bp:5.1f} h_tick={r.sd_bp*np.sqrt(-r.rho1)/r.tick_bp:5.3f} "
              f"实测={r.rho1:+.4f} 预测={pr[i]:+.4f} 残差={r.rho1-pr[i]:+.4f}")

    rng = np.random.default_rng(20261121)
    dates = np.array(sorted(cd.date.unique()))
    B = 400
    A, Bl, Al = [], [], []
    for _ in range(B):
        pick = rng.choice(dates, size=len(dates), replace=True)
        idx = np.concatenate([np.where(cd.date.values == d)[0] for d in pick])
        sub = cd.iloc[idx]
        a, b, _ = fit(sub)
        A.append(a); Bl.append(b)
        al, _, _ = fit(sub, low_only=True)
        Al.append(al)
    A, Bl, Al = map(np.array, (A, Bl, Al))

    out = {"point": {"alpha": a0, "beta": b0, "alpha_low": a_low},
           "boot": {"days": int(len(dates)), "B": B,
                    "alpha_mean": float(A.mean()), "alpha_sd": float(A.std()),
                    "alpha_ci": [float(np.percentile(A, 2.5)), float(np.percentile(A, 97.5))],
                    "beta_mean": float(Bl.mean()), "beta_sd": float(Bl.std()),
                    "beta_ci": [float(np.percentile(Bl, 2.5)), float(np.percentile(Bl, 97.5))],
                    "alpha_low_mean": float(Al.mean()), "alpha_low_sd": float(Al.std()),
                    "alpha_low_ci": [float(np.percentile(Al, 2.5)), float(np.percentile(Al, 97.5))],
                    "p_alpha_gt_0_5": float((A > 0.5).mean()),
                    "p_low_gt_0_5": float((Al > 0.5).mean())}}
    # 分年
    per = {}
    for yr in sorted(cd.yr.unique()):
        a, b, _ = fit(cd[cd.yr == yr])
        al, _, _ = fit(cd[cd.yr == yr], low_only=True)
        per[yr] = {"alpha": float(a), "beta": float(b), "alpha_low": float(al),
                   "days": int(cd[cd.yr == yr].date.nunique())}
    out["per_year"] = per
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n日级 bootstrap（{len(dates)} 天，B={B}）")
    print(f"  α   = {A.mean():.3f} ± {A.std():.3f}   95%CI [{np.percentile(A,2.5):.3f}, {np.percentile(A,97.5):.3f}]"
          f"   P(α>0.5)={out['boot']['p_alpha_gt_0_5']:.2f}")
    print(f"  β   = {Bl.mean():.3f} ± {Bl.std():.3f}   95%CI [{np.percentile(Bl,2.5):.3f}, {np.percentile(Bl,97.5):.3f}]")
    print(f"  α_low = {Al.mean():.3f} ± {Al.std():.3f}  95%CI [{np.percentile(Al,2.5):.3f}, {np.percentile(Al,97.5):.3f}]"
          f"   P(α_low>0.5)={out['boot']['p_low_gt_0_5']:.2f}")
    print("\n分年：")
    for yr, v in per.items():
        print(f"  {yr}: α={v['alpha']:.3f} β={v['beta']:.3f} α_low={v['alpha_low']:.3f} 天数={v['days']}")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
