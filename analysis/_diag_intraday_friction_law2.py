"""修订版：两区制摩擦律  ρ₁ = −min( (α·相对最小报价单位/σ)², β² )

原假设（单一常数 h）已被 S1 证伪：h 横截面跨度 3.64×（5.04–18.34bp），与相对报价单位相关 0.86。
本脚本检验替代模型并用「量化指纹」做机制归因，最后检验换手（参与者结构代理）是否还有残差解释力。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_intraday_friction_law2.json"
MD = ROOT / "analysis" / "_diag_intraday_friction_law2.md"
STRIDE = 4
COST_FIXED_BP = 8.0


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
          SELECT symbol, date, datetime, open, close, volume,
                 row_number() OVER (PARTITION BY symbol, date ORDER BY datetime) AS j,
                 count(*)     OVER (PARTITION BY symbol, date) AS nb
          FROM raw) WHERE nb = 48
        """
    )
    con.execute(
        """
        CREATE TABLE br AS
        SELECT symbol, date, j, close, close/lag(close) OVER w - 1 AS r
        FROM b48 WINDOW w AS (PARTITION BY symbol, date ORDER BY datetime)
        """
    )
    con.execute(
        """
        CREATE TABLE br2 AS
        SELECT symbol, date, j, r, lag(r) OVER w AS l1, lead(r) OVER w AS f1,
               CASE WHEN r>0 THEN 1 WHEN r<0 THEN -1 ELSE 0 END AS s
        FROM br WHERE r IS NOT NULL
        WINDOW w AS (PARTITION BY symbol, date ORDER BY j)
        """
    )
    con.execute(
        """
        CREATE TABLE symq AS
        WITH d AS (
          SELECT b.symbol, b.date, max_by(b.close,b.j) AS dclose, sum(b.volume) AS vol
          FROM b48 b GROUP BY 1,2),
        m AS (
          SELECT d.symbol, median(d.vol*100.0/i.float_shares) AS mturn,
                 median(d.dclose) AS mpx, count(*) AS nd
          FROM d JOIN 'data/instruments/*.parquet' i USING (symbol)
          WHERE d.vol>0 AND i.float_shares>0 GROUP BY 1 HAVING nd>=100)
        SELECT symbol, mturn, mpx,
               ntile(10) OVER (ORDER BY mturn) AS tq,
               ntile(10) OVER (ORDER BY mpx)   AS pq
        FROM m
        """
    )
    con.execute(
        """
        CREATE TABLE cd AS
        SELECT q.tq, q.pq, b.date, strftime(b.date,'%Y') AS yr,
               sum(b.r*b.r) AS v, sum(b.r*b.l1) AS c1, sum(-b.s*b.f1) AS g,
               count(*) AS n, avg(CASE WHEN b.r=0 THEN 1.0 ELSE 0 END) AS zf
        FROM br2 b JOIN symq q USING (symbol)
        WHERE b.l1 IS NOT NULL AND b.f1 IS NOT NULL
        GROUP BY 1,2,3,4
        """
    )
    # 量化指纹：|r| / (tick/price) 偏离最近整数的平均距离。全量化时→0。
    con.execute(
        """
        CREATE TABLE quant AS
        SELECT q.pq, count(*) AS n,
               avg(abs(abs(b.r) / (0.01/q.mpx) - round(abs(b.r) / (0.01/q.mpx)))) AS dev,
               avg(CASE WHEN b.r = 0 THEN 1.0 ELSE 0 END) AS zf,
               median(q.mpx) AS px
        FROM br2 b JOIN symq q USING (symbol)
        WHERE b.l1 IS NOT NULL AND abs(b.r) < 0.2
        GROUP BY 1 ORDER BY 1
        """
    )
    quant = con.sql("SELECT * FROM quant ORDER BY pq").df()

    def agg(where: str = "TRUE"):
        return con.sql(
            f"""
            SELECT tq, pq, count(*) AS nd, sum(v) AS v, sum(c1) AS c1, sum(g) AS g, sum(n) AS n,
                   avg(zf) AS zf
            FROM cd WHERE {where} GROUP BY 1,2
            """
        ).df()

    def prep(df):
        df["rho1"] = df["c1"] / df["v"]
        df["sd_bp"] = np.sqrt(df["v"] / df["n"]) * 1e4
        df["h_bp"] = df.sd_bp * np.sqrt(-df.rho1)
        df["gross_bp"] = df.g / df.n * 1e4
        df["mturn"] = [float(con.sql(f"SELECT median(mturn) FROM symq WHERE tq={a} AND pq={b}").fetchone()[0])
                       for a, b in zip(df.tq, df.pq)]
        df["mpx"] = [float(con.sql(f"SELECT median(mpx) FROM symq WHERE tq={a} AND pq={b}").fetchone()[0])
                     for a, b in zip(df.tq, df.pq)]
        df["tick_bp"] = 0.01 / df.mpx * 1e4
        df["h_tick"] = 0.5 * df.tick_bp
        return df

    cells = prep(agg())
    tr = prep(agg("yr IN ('2024','2025')"))
    te = prep(agg("yr='2026'"))

    def fit_ab(df):
        best = None
        for a in np.arange(0.4, 2.61, 0.02):
            for b in np.arange(0.08, 0.321, 0.002):
                h = np.maximum(a * df.tick_bp.values, b * df.sd_bp.values)
                pred = -((h / df.sd_bp.values) ** 2)
                sse = float(((pred - df.rho1.values) ** 2).sum())
                if best is None or sse < best[0]:
                    best = (sse, a, b)
        return best[1], best[2], best[0]

    a_hat, b_hat, sse = fit_ab(tr)
    def predict(df, a, b):
        h = np.maximum(a * df.tick_bp.values, b * df.sd_bp.values)
        return -((h / df.sd_bp.values) ** 2)

    pred_tr = predict(tr, a_hat, b_hat)
    r2_tr = 1 - ((pred_tr - tr.rho1.values) ** 2).sum() / ((tr.rho1 - tr.rho1.mean()) ** 2).sum()
    pred_all = predict(cells, a_hat, b_hat)
    r2_all = 1 - ((pred_all - cells.rho1.values) ** 2).sum() / ((cells.rho1 - cells.rho1.mean()) ** 2).sum()
    pred_te = predict(te, a_hat, b_hat)
    r2_te = 1 - ((pred_te - te.rho1.values) ** 2).sum() / ((te.rho1 - te.rho1.mean()) ** 2).sum()

    cells["h_pred"] = np.maximum(a_hat * cells.tick_bp, b_hat * cells.sd_bp)
    cells["pred"] = pred_all
    cells["resid"] = cells.rho1 - cells.pred
    cells["cost_bp"] = 2 * cells.h_bp + COST_FIXED_BP
    cells["g_over_c"] = cells.gross_bp / cells.cost_bp
    cells["binding"] = np.where(a_hat * cells.tick_bp > b_hat * cells.sd_bp, "tick", "impact")

    # 参与者结构是否还有残差解释力
    lturn = np.log(cells.mturn.values)
    X = np.column_stack([np.ones(len(cells)), lturn])
    beta, *_ = np.linalg.lstsq(X, cells.resid.values, rcond=None)
    res_s = cells.resid.values - X @ beta
    s2 = res_s @ res_s / (len(cells) - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    t_turn = float(beta[1] / se[1])
    r2_resid_turn = float(1 - (res_s @ res_s) / ((cells.resid - cells.resid.mean()) ** 2).sum())

    con.execute("COPY cd TO 'analysis/_cache_cd.parquet' (FORMAT PARQUET, OVERWRITE)")
    out = {
        "meta": {"days_used": len(sample), "cells": int(len(cells)), "pairs": int(cells.n.sum()),
                 "stocks": int(con.sql("SELECT count(DISTINCT symbol) FROM b48").fetchone()[0])},
        "law": {"alpha_tick": float(a_hat), "beta_impact": float(b_hat),
                "form": "rho1 = -min(alpha*tick_bp, beta*sd_bp)^2 / sd_bp^2",
                "r2_train": float(r2_tr), "r2_all": float(r2_all), "r2_test_2026": float(r2_te),
                "resid_sd": float(cells.resid.std()),
                "resid_max_abs": float(cells.resid.abs().max()),
                "binding_tick_cells": int((cells.binding == "tick").sum()),
                "h_floor_frac": float(b_hat)},
        "participant_structure": {
            "t_resid_on_ln_turn": t_turn, "r2_resid_on_ln_turn": r2_resid_turn,
            "resid_by_tq": cells.groupby("tq").resid.mean().round(5).to_dict(),
            "resid_by_pq": cells.groupby("pq").resid.mean().round(5).to_dict(),
        },
        "economics": {"g_over_c_min": float(cells.g_over_c.min()),
                      "g_over_c_max": float(cells.g_over_c.max()),
                      "g_over_c_median": float(cells.g_over_c.median()),
                      "cost_bp_min": float(cells.cost_bp.min()),
                      "cost_bp_max": float(cells.cost_bp.max())},
        "quantization": quant.round(6).to_dict("records"),
        "cells": cells.round(6).to_dict("records"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    L = ["# 修订：两区制摩擦律\n",
         f"样本 {out['meta']['days_used']} 日 / {out['meta']['stocks']} 只 / {out['meta']['pairs']:,} 个收益对 / {out['meta']['cells']} 格\n",
         "## 拟合", "",
         f"- 形式 `ρ₁ = −min(α·tick/price, β·σ)² / σ²`，α = **{a_hat:.2f}**，β = **{b_hat:.3f}**",
         f"- R²：训练 {r2_tr:.3f} / 全样本 {r2_all:.3f} / **2026 样本外 {r2_te:.3f}**",
         f"- 残差 sd {cells.resid.std():.5f}，最大绝对残差 {cells.resid.abs().max():.5f}",
         f"- tick 绑定（量化区）的格子：{int((cells.binding=='tick').sum())}/100\n",
         "## 参与者结构是否还有增量解释力", "",
         f"- 残差对 ln(日换手中位) 回归：t = **{t_turn:.2f}**，R² = {r2_resid_turn:.4f}",
         f"- 残差按换手十分位均值：" + ", ".join(f"{k}:{v:+.4f}" for k, v in
                                            cells.groupby('tq').resid.mean().round(4).items()),
         f"- 残差按价格十分位均值：" + ", ".join(f"{k}:{v:+.4f}" for k, v in
                                            cells.groupby('pq').resid.mean().round(4).items()), "",
         "## 经济上限", "",
         f"- 毛/成本：中位 {cells.g_over_c.median():.3f}，范围 {cells.g_over_c.min():.3f}–{cells.g_over_c.max():.3f}",
         f"- 往返成本（含 2h）：{cells.cost_bp.min():.1f}–{cells.cost_bp.max():.1f}bp\n",
         "## 量化指纹（|r|/(tick/px) 到最近整数的平均距离，0=完全量化）", "",
         "| 价格十分位 | 中位价 | 偏离 | 零收益占比 |", "|---|---|---|---|"]
    for r in out["quantization"]:
        L.append(f"| {r['pq']} | {r['px']:.1f} | {r['dev']:.3f} | {r['zf']*100:.1f}% |")
    MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(MD.read_text(encoding="utf-8"))
    print("\n绑定区分布：")
    print(cells.groupby("binding").size().to_string())
    print("\n各格 alpha/beta 生效：")
    print(cells[["tq", "pq", "mpx", "sd_bp", "rho1", "h_pred", "h_bp", "resid", "binding", "g_over_c"]]
          .head(12).to_string())


if __name__ == "__main__":
    main()
