"""核验：低价端的「饱和失配」是不是 pk 分位用「中位价」代表整格的产物？

若成立：把代表价从 median(mpx) 换成「按 r² 加权的调和价」∑w·p/∑w（w=∑r²），
pq1 的预测应当从 −0.196 移到 −0.27 附近，残差消失，无需饱和项。
同时给出逐价格桶的隐式 β 剖面（检验 β 是否真是常数）。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_price_rep.json"
STRIDE = 4
ALPHA = 0.5


def main() -> None:
    days = sorted(p.name.split("=", 1)[1] for p in SRC.iterdir() if p.is_dir())
    sample = days[::STRIDE]
    files = [str((SRC / f"date={d}" / "part.parquet").as_posix()) for d in sample]
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet({files!r})")
    con.execute("""
        CREATE TABLE b48 AS SELECT * FROM (
          SELECT symbol, date, datetime, close, volume,
                 row_number() OVER (PARTITION BY symbol, date ORDER BY datetime) AS j,
                 count(*)     OVER (PARTITION BY symbol, date) AS nb
          FROM raw) WHERE nb = 48
    """)
    con.execute("""
        CREATE TABLE br AS
        SELECT symbol, date, j, close / lag(close) OVER w - 1 AS r
        FROM b48 WINDOW w AS (PARTITION BY symbol, date ORDER BY datetime)
    """)
    con.execute("""
        CREATE TABLE br2 AS
        SELECT symbol, date, j, r, lag(r) OVER w AS l1, lead(r) OVER w AS f1,
               CASE WHEN r>0 THEN 1 WHEN r<0 THEN -1 ELSE 0 END AS s
        FROM br WHERE r IS NOT NULL
        WINDOW w AS (PARTITION BY symbol, date ORDER BY j)
    """)
    con.execute("""
        CREATE TABLE symq AS
        WITH d AS (SELECT symbol, date, max_by(close,j) AS dclose, sum(volume) AS vol
                   FROM b48 GROUP BY 1,2),
        m AS (SELECT d.symbol, median(d.vol*100.0/i.float_shares) AS mturn,
                     median(d.dclose) AS mpx, count(*) AS nd
              FROM d JOIN 'data/instruments/*.parquet' i USING (symbol)
              WHERE d.vol>0 AND i.float_shares>0 GROUP BY 1 HAVING nd>=100)
        SELECT symbol, mturn, mpx,
               ntile(10) OVER (ORDER BY mturn) AS tq,
               ntile(10) OVER (ORDER BY mpx)   AS pq
        FROM m
    """)
    # 每只股-日的 r² 权重（bp² 口径的自然权重）
    con.execute("""
        CREATE TABLE w AS
        SELECT symbol, date, sum(r*r) AS w, count(*) AS n
        FROM br2 WHERE l1 IS NOT NULL AND f1 IS NOT NULL GROUP BY 1,2
    """)
    # 每格：中位价 vs r² 加权价
    rep = con.sql("""
        SELECT q.pq, count(DISTINCT q.symbol) AS nsym,
               median(q.mpx) AS p_med,
               sum(w.w * q.mpx) / sum(w.w) AS p_varw,
               quantile_cont(q.mpx, 0.10) AS p10,
               quantile_cont(q.mpx, 0.90) AS p90
        FROM symq q JOIN w USING (symbol) GROUP BY 1 ORDER BY 1
    """).df()

    # 每格的 rho1 / sd
    con.execute("""
        CREATE TABLE cd AS
        SELECT q.tq, q.pq, b.date, strftime(b.date,'%Y') AS yr,
               sum(b.r*b.r) AS v, sum(b.r*b.l1) AS c1, count(*) AS n
        FROM br2 b JOIN symq q USING (symbol)
        WHERE b.l1 IS NOT NULL AND b.f1 IS NOT NULL
        GROUP BY 1,2,3,4
    """)
    g = con.sql("""
        SELECT tq, pq, strftime(date,'%Y') AS yr,
               sum(v) AS v, sum(c1) AS c1, sum(n) AS n FROM cd
        GROUP BY 1,2,3
    """).df()
    g["rho1"] = g.c1 / g.v
    g["sd_bp"] = np.sqrt(g.v / g.n) * 1e4
    pm = dict(zip(rep.pq, rep.p_med))
    pv = dict(zip(rep.pq, rep.p_varw))
    g["p_med"] = [pm[b] for b in g.pq]
    g["p_varw"] = [pv[b] for b in g.pq]
    g["tick_med"] = 0.01 / g.p_med * 1e4
    g["tick_varw"] = 0.01 / g.p_varw * 1e4

    tr = g[g.yr.isin(["2024", "2025"])]
    te = g[g.yr == "2026"]
    allc = g.groupby(["tq", "pq"], as_index=False).agg(v=("v", "sum"), c1=("c1", "sum"), n=("n", "sum"),
                                                        p_med=("p_med", "first"), p_varw=("p_varw", "first"))
    allc["rho1"] = allc.c1 / allc.v
    allc["sd_bp"] = np.sqrt(allc.v / allc.n) * 1e4

    def fit_beta(df, pcol):
        tick = 0.01 / df[pcol].values * 1e4
        best = None
        for b in np.arange(0.03, 0.401, 0.0005):
            h = np.sqrt((ALPHA * tick) ** 2 + (b * df.sd_bp.values) ** 2)
            pr = -((h / df.sd_bp.values) ** 2)
            sse = float(((pr - df.rho1.values) ** 2).sum())
            if best is None or sse < best[0]:
                best = (sse, b)
        return best[1]

    def pred(df, pcol, b):
        tick = 0.01 / df[pcol].values * 1e4
        h = np.sqrt((ALPHA * tick) ** 2 + (b * df.sd_bp.values) ** 2)
        return -((h / df.sd_bp.values) ** 2)

    def r2(p, y):
        return float(1 - ((p - y) ** 2).sum() / ((y - y.mean()) ** 2).sum())

    res = {}
    for tag, pcol in (("median", "p_med"), ("varw", "p_varw")):
        trc = tr.groupby(["tq", "pq"], as_index=False).agg(v=("v", "sum"), c1=("c1", "sum"), n=("n", "sum"),
                                                           p_med=("p_med", "first"), p_varw=("p_varw", "first"))
        trc["rho1"] = trc.c1 / trc.v
        trc["sd_bp"] = np.sqrt(trc.v / trc.n) * 1e4
        b = fit_beta(trc, pcol)
        pa = pred(allc, pcol, b)
        # 2026：按格聚合
        tec = te.groupby(["tq", "pq"], as_index=False).agg(v=("v", "sum"), c1=("c1", "sum"), n=("n", "sum"),
                                                           p_med=("p_med", "first"), p_varw=("p_varw", "first"))
        tec["rho1"] = tec.c1 / tec.v
        tec["sd_bp"] = np.sqrt(tec.v / tec.n) * 1e4
        pt = pred(tec, pcol, b)
        resid = allc.rho1.values - pa
        res[tag] = {
            "beta": float(b), "r2_all": r2(pa, allc.rho1.values), "r2_test_2026": r2(pt, tec.rho1.values),
            "resid_by_pq": {int(k): float(v) for k, v in
                            zip(allc.pq, np.round(pd_mean(resid, allc.pq.values), 5))},
            "resid_sd": float(resid.std()),
            "pq1_pred": float(pa[allc.pq.values == 1].mean()),
            "pq1_obs": float(allc.rho1.values[allc.pq.values == 1].mean()),
        }

    # 逐价格桶的隐式 β 剖面
    lat = json.loads((ROOT / "analysis" / "_diag_lattice.json").read_text(encoding="utf-8"))
    prof = []
    for c in lat["cells"]:
        h = c["h_bp"]; tick = c["tick_bp"]; sd = c["sd_bp"]
        ex = h * h - (ALPHA * tick) ** 2
        prof.append({"bin": c["b"], "mpx": c["mpx"], "sd_bp": sd, "rho1_bp": c["rho1_bp"],
                     "h_bp": h, "tick_bp": tick, "h_tick": h / tick,
                     "beta_eff": (np.sqrt(ex) / sd if ex > 0 else 0.0)})

    out = {"rep": rep.round(4).to_dict("records"), "models": res, "beta_profile": prof}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("== 代表价：中位 vs r² 加权（分位格）")
    print(f"{'pq':>3} {'只数':>5} {'中位价':>8} {'r²加权价':>9} {'p10':>7} {'p90':>7}")
    for r in out["rep"]:
        print(f"{int(r['pq']):>3} {int(r['nsym']):5d} {r['p_med']:8.2f} {r['p_varw']:9.2f} "
              f"{r['p10']:7.2f} {r['p90']:7.2f}")
    print("\n== 两种代表价下的拟合")
    for tag in ("median", "varw"):
        m = res[tag]
        print(f"  {tag:>7}: β={m['beta']:.4f}  R²={m['r2_all']:.4f}  2026={m['r2_test_2026']:.4f}  "
              f"pq1 实测 {m['pq1_obs']:+.4f} / 预测 {m['pq1_pred']:+.4f}")
        print(f"           残差按 pq: " + ", ".join(f"{k}:{v:+.4f}" for k, v in m["resid_by_pq"].items()))
    print("\n== 隐式 β 剖面（h²=(0.5·tick)²+(β·σ)² 解出 β）")
    print(f"{'价格桶':>9} {'中位价':>7} {'sd_bp':>7} {'rho1_bp':>8} {'h_bp':>7} {'h_tick':>7} {'β_eff':>7}")
    for r in prof:
        print(f"{str(r['bin']):>9} {r['mpx']:7.2f} {r['sd_bp']:7.1f} {r['rho1_bp']:8.4f} "
              f"{r['h_bp']:7.2f} {r['h_tick']:7.3f} {r['beta_eff']:7.3f}")


def pd_mean(x, g):
    import collections
    d = collections.defaultdict(list)
    for a, b in zip(g, x):
        d[int(b)].append(a)
    return np.array([np.mean(d[k]) for k in sorted(d)])


if __name__ == "__main__":
    main()
