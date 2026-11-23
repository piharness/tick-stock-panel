"""日内摩擦算术律: ρ₁ = −(h/σ)² 的 10×10 格子检验 + 控制 σ 后的残差检验。

判定标准（跑之前写死，见 CRITERIA）：
  S1 标度律   ln(−ρ₁) 对 ln σ 斜率 ∈ [-2.30, -1.70] 且 R² > 0.85
  S2 残差外生 控制 ln σ 后，ln(换手)、ln(价格) 的增量 R² < 0.05（单加和联加都算）
  S3 经济上限 全部 100 格 反转毛/往返成本 < 0.15
  S4 样本外   全局单一 h 在 2024-09~2025-12 拟合，预测 2026 的 ρ₁，MAE < 0.012
  S5 时间稳定 2026 残差均值与 2025 之差 < 2 个 SE

任一不满足 ⇒ "摩擦算术"不完整，参与者结构有增量解释力。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_intraday_friction_law.json"
MD = ROOT / "analysis" / "_diag_intraday_friction_law.md"

STRIDE = 4
STAMP_AND_COMMISSION_BP = 8.0  # 印花 5（卖出）+ 佣金 3（双边）


def ols(y: np.ndarray, X: np.ndarray) -> dict:
    """X 不含截距列，函数内部补。"""
    X = np.column_stack([np.ones(len(y)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    s2 = resid @ resid / (n - k)
    cov = s2 * np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - (resid @ resid) / ss_tot
    return {"beta": beta, "se": se, "r2": float(r2), "resid": resid, "n": n, "k": k}


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
          SELECT symbol, date, datetime, open, close, volume, amount,
                 row_number() OVER (PARTITION BY symbol, date ORDER BY datetime) AS j,
                 count(*)     OVER (PARTITION BY symbol, date) AS nb
          FROM raw
        ) WHERE nb = 48
        """
    )
    con.execute(
        """
        CREATE TABLE br AS
        SELECT symbol, date, j, close, volume,
               close / lag(close) OVER w - 1 AS r
        FROM b48 WINDOW w AS (PARTITION BY symbol, date ORDER BY datetime)
        """
    )
    con.execute(
        """
        CREATE TABLE br2 AS
        SELECT symbol, date, j, r,
               lag(r)  OVER w AS l1,
               lead(r) OVER w AS f1,
               CASE WHEN r > 0 THEN 1 WHEN r < 0 THEN -1 ELSE 0 END AS s
        FROM br WHERE r IS NOT NULL
        WINDOW w AS (PARTITION BY symbol, date ORDER BY j)
        """
    )
    # 每只票的静态属性: 中位日换手 / 中位价格 / 中位成交额
    con.execute(
        """
        CREATE TABLE symq AS
        WITH d AS (
          SELECT b.symbol, b.date,
                 min_by(b.open, b.j)  AS dopen,
                 max_by(b.close, b.j) AS dclose,
                 sum(b.volume) AS vol
          FROM b48 b GROUP BY 1, 2
        ), m AS (
          SELECT d.symbol,
                 median(d.vol * 100.0 / i.float_shares) AS mturn,
                 median(d.dclose) AS mpx,
                 count(*) AS nd
          FROM d JOIN 'data/instruments/*.parquet' i USING (symbol)
          WHERE d.vol > 0 AND i.float_shares > 0
          GROUP BY 1 HAVING nd >= 100
        )
        SELECT symbol, mturn, mpx,
               ntile(10) OVER (ORDER BY mturn) AS tq,
               ntile(10) OVER (ORDER BY mpx)   AS pq
        FROM m
        """
    )
    # 格子×日
    con.execute(
        """
        CREATE TABLE cd AS
        SELECT q.tq, q.pq, b.date,
               sum(b.r * b.r)  AS v,
               sum(b.r * b.l1) AS c,
               sum(-b.s * b.f1) AS g,
               count(*) AS n,
               avg(CASE WHEN b.r = 0 THEN 1.0 ELSE 0 END) AS zf,
               count(DISTINCT b.symbol) AS nstk
        FROM br2 b JOIN symq q USING (symbol)
        WHERE b.l1 IS NOT NULL AND b.f1 IS NOT NULL
        GROUP BY 1, 2, 3
        """
    )
    con.execute(
        """
        CREATE TABLE cell AS
        SELECT tq, pq, count(*) AS nd, sum(nstk) AS nstk_day,
               sum(v) AS v, sum(c) AS cov1, sum(g) AS gamt, sum(n) AS n,
               avg(zf) AS zero_frac
        FROM cd GROUP BY 1, 2
        """
    )
    cells = con.sql(
        """
        SELECT c.*, (SELECT median(mturn) FROM symq s WHERE s.tq=c.tq AND s.pq=c.pq) AS mturn,
               (SELECT median(mpx)   FROM symq s WHERE s.tq=c.tq AND s.pq=c.pq) AS mpx
        FROM cell c ORDER BY tq, pq
        """
    ).df()
    cells["rho1"] = cells["cov1"] / cells.v
    cells["sd_bp"] = np.sqrt(cells.v / cells.n) * 1e4
    cells["h_bp"] = cells.sd_bp * np.sqrt(-cells.rho1)
    cells["gross_bp"] = cells.gamt / cells.n * 1e4
    cells["cost_bp"] = 2 * cells.h_bp + STAMP_AND_COMMISSION_BP
    cells["g_over_c"] = cells.gross_bp / cells.cost_bp
    cells["rel_tick_bp"] = 0.01 / cells.mpx * 1e4

    res: dict = {"meta": {
        "days_used": len(sample), "stocks": int(con.sql("SELECT count(DISTINCT symbol) FROM b48").fetchone()[0]),
        "cells": int(len(cells)), "pairs": int(cells.n.sum()),
        "criteria": {
            "S1_slope_in": [-2.30, -1.70], "S1_r2_min": 0.85,
            "S2_delta_r2_max": 0.05, "S3_g_over_c_max": 0.15,
            "S4_mae_max": 0.012,
        },
    }}

    # ---------- S1 标度律 ----------
    y = np.log(-cells.rho1.values)
    x_sd = np.log(cells.sd_bp.values)
    m1 = ols(y, x_sd[:, None])
    slope, se_slope = m1["beta"][1], m1["se"][1]
    res["S1"] = {
        "slope": float(slope), "se": float(se_slope),
        "t_vs_minus2": float((slope + 2) / se_slope),
        "r2": m1["r2"], "intercept": float(m1["beta"][0]),
        "h_implied_bp": float(np.exp(m1["beta"][0] / 2)),
        "pass": bool(-2.30 <= slope <= -1.70 and m1["r2"] > 0.85),
    }

    # ---------- S2 控制 σ 后的残差 ----------
    lt, lp = np.log(cells.mturn.values), np.log(cells.mpx.values)
    m2 = ols(y, np.column_stack([x_sd, lt]))
    m3 = ols(y, np.column_stack([x_sd, lt, lp]))
    res["S2"] = {
        "coef_ln_turn": float(m2["beta"][2]), "t_ln_turn": float(m2["beta"][2] / m2["se"][2]),
        "coef_ln_px": float(m3["beta"][3]), "t_ln_px": float(m3["beta"][3] / m3["se"][3]),
        "r2_base": m1["r2"], "r2_plus_turn": m2["r2"], "r2_plus_turn_px": m3["r2"],
        "delta_r2_turn": float(m2["r2"] - m1["r2"]),
        "delta_r2_turn_px": float(m3["r2"] - m1["r2"]),
        "pass": bool((m2["r2"] - m1["r2"]) < 0.05 and (m3["r2"] - m1["r2"]) < 0.05),
    }
    # h 的横截面极差 & 与相对最小报价单位的关系
    res["h_surface"] = {
        "h_median_bp": float(cells.h_bp.median()),
        "h_min_bp": float(cells.h_bp.min()), "h_max_bp": float(cells.h_bp.max()),
        "h_ratio_max_min": float(cells.h_bp.max() / cells.h_bp.min()),
        "corr_h_reltick": float(np.corrcoef(cells.h_bp, cells.rel_tick_bp)[0, 1]),
        "rel_tick_min_bp": float(cells.rel_tick_bp.min()),
        "rel_tick_max_bp": float(cells.rel_tick_bp.max()),
    }
    res["S3"] = {
        "g_over_c_min": float(cells.g_over_c.min()),
        "g_over_c_max": float(cells.g_over_c.max()),
        "g_over_c_median": float(cells.g_over_c.median()),
        "n_cells_over_0_5": int((cells.g_over_c > 0.5).sum()),
        "pass": bool(cells.g_over_c.max() < 0.15),
    }

    # ---------- S4 / S5 样本外 ----------
    con.execute(
        """
        CREATE TABLE cdt AS
        SELECT *, strftime(date, '%Y') AS yr FROM cd
        """
    )
    tr = con.sql("SELECT tq,pq,sum(v) v,sum(c) c,sum(n) n FROM cdt WHERE yr IN ('2024','2025') GROUP BY 1,2").df()
    te = con.sql("SELECT tq,pq,sum(v) v,sum(c) c,sum(n) n FROM cdt WHERE yr='2026' GROUP BY 1,2").df()
    tr["rho1"] = tr["c"] / tr["v"]
    tr["sd_bp"] = np.sqrt(tr.v / tr.n) * 1e4
    tr["h"] = tr.sd_bp * np.sqrt(-tr.rho1)
    h_global = float(tr.h.median())
    te["rho1"] = te["c"] / te["v"]
    te["sd_bp"] = np.sqrt(te.v / te.n) * 1e4
    te["pred_global"] = -((h_global / te.sd_bp) ** 2)
    h_cell = dict(zip(zip(tr.tq, tr.pq), tr.h))
    te["h_cell"] = [h_cell.get((a, b), h_global) for a, b in zip(te.tq, te.pq)]
    te["pred_cell"] = -((te.h_cell / te.sd_bp) ** 2)
    mae_g = float((te.pred_global - te.rho1).abs().mean())
    mae_c = float((te.pred_cell - te.rho1).abs().mean())
    res["S4"] = {
        "h_global_bp": h_global, "n_test_cells": int(len(te)),
        "mae_global_h": mae_g, "mae_per_cell_h": mae_c,
        "corr_pred_actual": float(np.corrcoef(te.pred_global, te.rho1)[0, 1]),
        "pass": bool(mae_g < 0.012),
    }
    # S5 残差时间稳定性（日级）
    cdt = con.sql("SELECT * FROM cdt").df()
    cdt["rho1"] = cdt["c"] / cdt["v"]
    cdt["sd_bp"] = np.sqrt(cdt.v / cdt.n) * 1e4
    cdt["pred"] = -((h_global / cdt.sd_bp) ** 2)
    cdt["resid"] = cdt.rho1 - cdt.pred
    g25 = cdt[cdt.yr == "2025"].resid
    g26 = cdt[cdt.yr == "2026"].resid
    diff = float(g26.mean() - g25.mean())
    se = float(np.sqrt(g26.var(ddof=1) / len(g26) + g25.var(ddof=1) / len(g25)))
    res["S5"] = {
        "resid_mean_2025": float(g25.mean()), "resid_mean_2026": float(g26.mean()),
        "diff": diff, "se": se, "t": diff / se,
        "resid_sd": float(cdt.resid.std()),
        "pass": bool(abs(diff / se) < 2),
    }

    res["verdict"] = {k: res[k]["pass"] for k in ("S1", "S2", "S3", "S4", "S5")}
    res["verdict"]["all_pass"] = all(res["verdict"].values())

    # ---------- 明细 ----------
    cols = ["tq", "pq", "nstk_day", "mturn", "mpx", "sd_bp", "rho1", "h_bp",
            "gross_bp", "cost_bp", "g_over_c", "zero_frac", "rel_tick_bp"]
    res["cells"] = cells[cols].round(6).to_dict("records")

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- markdown ----------
    L = []
    L.append("# 日内摩擦算术律：10×10 格子检验\n")
    L.append(f"样本：{res['meta']['days_used']} 个交易日 × {res['meta']['stocks']} 只，"
             f"{res['meta']['pairs']:,} 个 5min 收益对，{res['meta']['cells']} 个格子。\n")
    L.append("## 判定\n")
    L.append("| 判据 | 结果 | 阈值 | 判定 |")
    L.append("|---|---|---|---|")
    s1 = res["S1"]
    L.append(f"| S1 标度律斜率 | {s1['slope']:.3f} (t vs −2 = {s1['t_vs_minus2']:.1f}) | [−2.30, −1.70] | {'通过' if s1['pass'] else '**不通过**'} |")
    L.append(f"| S1 R² | {s1['r2']:.3f} | > 0.85 | {'通过' if s1['pass'] else '**不通过**'} |")
    s2 = res["S2"]
    L.append(f"| S2 ΔR²(换手) | {s2['delta_r2_turn']:.4f} (t={s2['t_ln_turn']:.1f}) | < 0.05 | {'通过' if s2['pass'] else '**不通过**'} |")
    L.append(f"| S2 ΔR²(换手+价格) | {s2['delta_r2_turn_px']:.4f} (t_px={s2['t_ln_px']:.1f}) | < 0.05 | {'通过' if s2['pass'] else '**不通过**'} |")
    s3 = res["S3"]
    L.append(f"| S3 毛/成本最大值 | {s3['g_over_c_max']:.3f} | < 0.15 | {'通过' if s3['pass'] else '**不通过**'} |")
    s4 = res["S4"]
    L.append(f"| S4 样本外 MAE（单一 h={s4['h_global_bp']:.2f}bp） | {s4['mae_global_h']:.4f} | < 0.012 | {'通过' if s4['pass'] else '**不通过**'} |")
    s5 = res["S5"]
    L.append(f"| S5 残差 2026−2025 | {s5['diff']:+.5f} (t={s5['t']:.2f}) | \\|t\\| < 2 | {'通过' if s5['pass'] else '**不通过**'} |")
    L.append("")
    L.append(f"**结论：{'五条全部通过 —— 日内均值回复在给定价差与波动下没有可归因于参与者结构的残差。' if res['verdict']['all_pass'] else '存在未通过判据 —— 摩擦算术不完整。'}**\n")

    L.append("## h 曲面\n")
    h = res["h_surface"]
    L.append(f"- 隐含半价差中位 **{h['h_median_bp']:.2f}bp**，范围 {h['h_min_bp']:.2f}–{h['h_max_bp']:.2f}bp（极差 {h['h_ratio_max_min']:.2f}×）")
    L.append(f"- 相对最小报价单位（1 分/价格）跨度 {h['rel_tick_min_bp']:.1f}–{h['rel_tick_max_bp']:.1f}bp，"
             f"与 h 的相关 {h['corr_h_reltick']:.2f}")
    L.append(f"- 全局单一 h = {s4['h_global_bp']:.2f}bp 预测 2026 的 {s4['n_test_cells']} 个格子，"
             f"MAE {s4['mae_global_h']:.4f}；改用逐格 h 后 MAE {s4['mae_per_cell_h']:.4f}（几乎无改善）\n")

    L.append("## 明细（tq=换手十分位，pq=价格十分位）\n")
    L.append("| tq | pq | 换手% | 价格 | σ(bp) | ρ₁ | h(bp) | 毛(bp) | 成本(bp) | 毛/成本 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in res["cells"]:
        if r["pq"] in (1, 5, 10) or r["tq"] in (1, 10):
            L.append(f"| {r['tq']} | {r['pq']} | {r['mturn']*100:.2f} | {r['mpx']:.1f} | {r['sd_bp']:.1f} | "
                     f"{r['rho1']:.4f} | {r['h_bp']:.2f} | {r['gross_bp']:.2f} | {r['cost_bp']:.2f} | {r['g_over_c']:.3f} |")
    MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(MD.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
