"""日内结构的可分解性：把日內路径拆成 季节性 / 买卖价差跳动 / 瞬时冲击 / 永久成分。

用法: python analysis/_diag_intraday_retail.py
输出: analysis/_diag_intraday_retail.json
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_intraday_retail.json"

STRIDE = 4  # 每 4 个交易日取 1 天，控制内存


def main() -> None:
    days = sorted(p.name.split("=", 1)[1] for p in SRC.iterdir() if p.is_dir())
    sample = days[::STRIDE]
    files = [str((SRC / f"date={d}" / "part.parquet").as_posix()) for d in sample]
    con = duckdb.connect()
    con.execute("SET threads=8")

    con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet({files!r})")
    con.execute(
        """
        CREATE TABLE bars AS
        SELECT symbol, date, datetime, open, high, low, close, volume, amount,
               row_number() OVER (PARTITION BY symbol, date ORDER BY datetime) AS j,
               count(*)     OVER (PARTITION BY symbol, date) AS nb
        FROM raw
        """
    )
    n_bars = con.sql("SELECT count(*) FROM bars").fetchone()[0]
    # 只保留整日 48 根的股票-日，停牌半日会污染形状与自相关
    con.execute("CREATE TABLE b48 AS SELECT * FROM bars WHERE nb = 48")
    n_b48 = con.sql("SELECT count(*) FROM b48").fetchone()[0]

    con.execute(
        """
        CREATE TABLE br AS
        SELECT symbol, date, j, close, high, low, volume, amount,
               -- 同日内 上一根收盘 -> 本根收盘，用于自相关/反转
               CASE WHEN j >= 2 THEN close / lag(close) OVER w - 1 END AS r_cc,
               -- 含首根的 bar 收益（首根用 open->close），用于形状
               CASE WHEN j >= 2 THEN close / lag(close) OVER w - 1
                    ELSE close / open - 1 END AS r_bar
        FROM b48
        WINDOW w AS (PARTITION BY symbol, date ORDER BY datetime)
        """
    )

    con.execute(
        """
        CREATE TABLE dayagg AS
        SELECT symbol, date,
               min_by(open, j)  AS dopen,
               max_by(close, j) AS dclose,
               sum(volume)      AS vol,
               sum(amount)      AS amt
        FROM b48 GROUP BY 1, 2
        """
    )
    con.execute(
        "CREATE TABLE dr AS SELECT date, row_number() OVER (ORDER BY date) AS rn FROM (SELECT DISTINCT date FROM dayagg)"
    )
    con.execute(
        """
        CREATE TABLE dayret AS
        WITH a AS (SELECT d.*, r.rn FROM dayagg d JOIN dr r USING (date))
        SELECT symbol, date, rn, dopen, dclose, vol, amt,
               lag(dclose) OVER w AS prev_close,
               lag(rn)     OVER w AS prev_rn
        FROM a WINDOW w AS (PARTITION BY symbol ORDER BY date)
        """
    )
    con.execute(
        """
        CREATE TABLE dayret2 AS
        SELECT *, dclose / prev_close - 1 AS r_cc,
                  dopen  / prev_close - 1 AS r_on,
                  dclose / dopen - 1      AS r_id
        FROM dayret
        WHERE prev_close IS NOT NULL AND rn - prev_rn = 1      -- 日历相邻，排除跨停牌
          AND prev_close > 0
        """
    )
    res: dict = {"meta": {"days_total": len(days), "days_used": len(sample),
                          "bars_rows": n_bars, "bars_rows_48": n_b48,
                          "stocks": con.sql("SELECT count(DISTINCT symbol) FROM b48").fetchone()[0],
                          "stock_days": con.sql("SELECT count(*) FROM dayret2").fetchone()[0]}}

    # ---------- 1. 日内形状：方差份额 + 成交额份额 ----------
    shape = con.sql(
        """
        SELECT j,
               avg(r_bar * r_bar)                     AS var_bar,
               sum(amount)                            AS amt,
               avg(abs(r_bar))                        AS absret,
               avg(CASE WHEN r_bar = 0 THEN 1.0 ELSE 0 END) AS zero_frac
        FROM br GROUP BY j ORDER BY j
        """
    ).df()
    shape["var_share"] = shape.var_bar / shape.var_bar.sum()
    shape["amt_share"] = shape.amt / shape.amt.sum()
    res["shape"] = shape[["j", "var_share", "amt_share", "absret", "zero_frac"]].round(6).to_dict("records")
    # 首尾各 30 分钟 vs 中间
    res["shape_summary"] = {
        "var_share_first6": float(shape.var_share.iloc[:6].sum()),
        "var_share_last6": float(shape.var_share.iloc[-6:].sum()),
        "var_max_over_min": float(shape.var_bar.max() / shape.var_bar.min()),
        "amt_share_first6": float(shape.amt_share.iloc[:6].sum()),
        "amt_share_last6": float(shape.amt_share.iloc[-6:].sum()),
        "midday_amt_share_20_28": float(shape.amt_share.iloc[19:28].sum()),
        "peak_bar": int(shape.loc[shape.var_bar.idxmax(), "j"]),
    }

    # ---------- 2. 隔夜 vs 日内 方差分解 ----------
    dec = con.sql(
        """
        SELECT count(*) AS n,
               sum(r_on * r_on) AS von, sum(r_id * r_id) AS vid,
               sum(r_cc * r_cc) AS vcc, sum(r_on * r_id) AS covoi,
               avg(r_on) AS m_on, avg(r_id) AS m_id
        FROM dayret2
        WHERE abs(r_on) < 0.35 AND abs(r_id) < 0.35      -- 去掉复牌/一字板异常
        """
    ).df().iloc[0]
    tot = dec.von + dec.vid + 2 * dec.covoi
    res["overnight_intraday"] = {
        "n": int(dec.n),
        "var_on_share": float(dec.von / tot),
        "var_id_share": float(dec.vid / tot),
        "var_cc": float(dec.vcc),
        "mean_on_bps": float(dec.m_on * 1e4),
        "mean_id_bps": float(dec.m_id * 1e4),
        "sd_on_pct": float(np.sqrt(dec.von / dec.n) * 100),
        "sd_id_pct": float(np.sqrt(dec.vid / dec.n) * 100),
    }

    # ---------- 3. 一阶自相关（合并）+ Roll 隐含价差 ----------
    ac = con.sql(
        """
        WITH x AS (
          SELECT r_cc, lag(r_cc) OVER (PARTITION BY symbol, date ORDER BY j) AS l1
          FROM br WHERE r_cc IS NOT NULL
        )
        SELECT sum(r_cc * l1) / count(*) AS cov1,
               sum(r_cc * r_cc) / count(*) AS var1,
               count(*) AS n
        FROM x WHERE l1 IS NOT NULL
        """
    ).df().iloc[0]
    rho1 = ac.cov1 / ac.var1
    var1 = ac.var1
    res["acf1"] = {"rho1": float(rho1), "var1": float(var1), "n": int(ac.n),
                   "sd_bps": float(np.sqrt(var1) * 1e4),
                   "roll_halfspread_bps": float(np.sqrt(max(-ac.cov1, 0)) * 1e4)}

    # ---------- 4. 方差比 VR(6) ----------
    vr = con.sql(
        """
        WITH x AS (
          SELECT close / lag(close, 6) OVER (PARTITION BY symbol, date ORDER BY j) - 1 AS r6
          FROM b48
        )
        SELECT avg(r6 * r6) AS v6, count(*) AS n6 FROM x WHERE r6 IS NOT NULL
        """
    ).df().iloc[0]
    res["vr6"] = {"vr": float(vr.v6 / (6 * var1)), "n": int(vr.n6)}

    # ---------- 4b. 方差比期限结构 + 路径效率 ----------
    vrs = []
    for k in (1, 2, 3, 6, 12, 24, 48):
        if k == 1:
            vrs.append({"k": 1, "vr": 1.0, "n": int(ac.n), "var_k": float(var1)})
            continue
        row = con.sql(
            f"""
            WITH x AS (SELECT close / lag(close, {k}) OVER (PARTITION BY symbol, date ORDER BY j) - 1 AS rk
                       FROM b48)
            SELECT avg(rk*rk) AS vk, count(*) AS nk FROM x WHERE rk IS NOT NULL
            """
        ).df().iloc[0]
        vrs.append({"k": k, "vr": float(row.vk / (k * var1)), "n": int(row.nk), "var_k": float(row.vk)})
    res["vr_term"] = vrs
    # 会话总方差 vs 各 bar 方差之和（一致性检查）
    conv = con.sql(
        """
        WITH s AS (SELECT date, sum(abs(r_bar)) AS path_len, count(*) AS nb FROM br GROUP BY date)
        SELECT (SELECT avg(r_id*r_id) FROM dayret2 WHERE abs(r_id)<0.35) AS var_session,
               (SELECT avg(v) FROM (SELECT date, sum(r_bar*r_bar) AS v FROM br GROUP BY date)) AS var_sumbar,
               (SELECT avg(abs(r_id)) FROM dayret2 WHERE abs(r_id)<0.35) AS abs_net,
               (SELECT avg(path_len) FROM s) AS abs_path
        """
    ).df().iloc[0]
    res["path_efficiency"] = {
        "sd_session_bps": float(np.sqrt(conv.var_session) * 1e4),
        "sum_bar_var_bp2": float(conv.var_sumbar * 1e8),
        "session_var_bp2": float(conv.var_session * 1e8),
        "abs_net_bps": float(conv.abs_net * 1e4),
        "abs_path_bps": float(conv.abs_path * 1e4),
        "path_over_net": float(conv.abs_path / conv.abs_net),
    }
    rev = con.sql(
        """
        WITH x AS (
          SELECT r_cc, sign(r_cc) AS s,
                 lead(r_cc) OVER (PARTITION BY symbol, date ORDER BY j) AS f1
          FROM br WHERE r_cc IS NOT NULL
        )
        SELECT avg(-s * f1) * 1e4 AS gross_bps,
               avg(-s * f1) * 1e4 / (avg(abs(r_cc)) * 1e4) AS edge_per_turn,
               count(*) AS n
        FROM x WHERE f1 IS NOT NULL
        """
    ).df().iloc[0]
    res["reversal"] = {"gross_bps_per_bar": float(rev.gross_bps), "n": int(rev.n)}

    # 按 |ret| 条件分组看反转强度（零售处置效应：上涨反转 vs 下跌反转）
    sig = float(np.sqrt(var1))
    rev_cond = con.sql(
        f"""
        WITH x AS (
          SELECT r_cc, lead(r_cc) OVER (PARTITION BY symbol, date ORDER BY j) AS f1
          FROM br WHERE r_cc IS NOT NULL
        ), y AS (
          SELECT CASE WHEN r_cc >=  3*{sig} THEN 'up3'
                      WHEN r_cc >=  1*{sig} THEN 'up1'
                      WHEN r_cc <= -3*{sig} THEN 'dn3'
                      WHEN r_cc <= -1*{sig} THEN 'dn1'
                      ELSE 'mid' END AS bucket,
                 r_cc, f1
          FROM x WHERE f1 IS NOT NULL
        )
        SELECT bucket, count(*) AS n,
               avg(r_cc) * 1e4 AS r_bps,
               avg(f1)   * 1e4 AS f1_bps,
               avg(f1)   * 1e4 / (abs(avg(r_cc)) * 1e4) AS rev_ratio
        FROM y GROUP BY 1 ORDER BY 1
        """
    ).df()
    res["reversal_conditional"] = rev_cond.round(4).to_dict("records")

    # ---------- 6. 按流动性十分位（成交额中位）的梯度 ----------
    con.execute(
        """
        CREATE TABLE symq AS
        WITH m AS (SELECT symbol, median(amt) AS mamt, median(dclose) AS mpx, count(*) AS nd
                   FROM dayagg GROUP BY 1 HAVING nd >= 100)
        SELECT symbol, mamt, mpx, ntile(10) OVER (ORDER BY mamt) AS dq,
               ntile(10) OVER (ORDER BY mpx)  AS pq
        FROM m
        """
    )
    grad = con.sql(
        """
        WITH x AS (
          SELECT b.symbol, b.r_cc,
                 sign(b.r_cc) AS s,
                 lead(b.r_cc) OVER (PARTITION BY b.symbol, b.date ORDER BY b.j) AS f1
          FROM br b WHERE b.r_cc IS NOT NULL
        ), a AS (
          SELECT q.dq, q.mamt, q.mpx, x.r_cc, x.s, x.f1
          FROM x JOIN symq q USING (symbol)
        )
        SELECT dq, count(*) AS n, 
               avg(mamt)/1e6 AS mamt_mn, median(mpx) AS med_px,
               avg(r_cc*r_cc) AS var1,
               sum(r_cc*f1)/sum(r_cc*r_cc) AS rho1,
               avg(CASE WHEN r_cc = 0 THEN 1.0 ELSE 0 END) AS zero_frac,
               avg(CASE WHEN f1 IS NOT NULL THEN -s*f1 END)*1e4 AS rev_gross_bps
        FROM a GROUP BY dq ORDER BY dq
        """
    ).df()
    res["liquidity_gradient"] = grad.round(6).to_dict("records")

    # ---------- 6b. 按换手率十分位（「散户化」的标准代理变量） ----------
    con.execute(
        """
        CREATE TABLE symt AS
        WITH m AS (
          SELECT d.symbol,
                 median(d.vol * 100.0 / i.float_shares) AS mturn,
                 median(d.amt) AS mamt, count(*) AS nd
          FROM dayagg d JOIN 'data/instruments/*.parquet' i USING (symbol)
          WHERE d.vol > 0 AND i.float_shares > 0
          GROUP BY 1 HAVING nd >= 100
        )
        SELECT symbol, mturn, mamt, ntile(10) OVER (ORDER BY mturn) AS tq,
               ntile(10) OVER (ORDER BY mamt) AS dq2
        FROM m
        """
    )
    gt = con.sql(
        """
        WITH x AS (
          SELECT b.symbol, b.r_cc, sign(b.r_cc) AS s,
                 lead(b.r_cc) OVER (PARTITION BY b.symbol, b.date ORDER BY b.j) AS f1
          FROM br b WHERE b.r_cc IS NOT NULL
        ), v AS (
          SELECT t.tq, x.r_cc, x.s, x.f1
          FROM x JOIN symt t USING (symbol)
        )
        SELECT tq, count(*) AS n,
               avg(r_cc*r_cc)*1e8 AS var1_bp2,
               sum(r_cc*f1)/sum(r_cc*r_cc) AS rho1,
               avg(CASE WHEN r_cc = 0 THEN 1.0 ELSE 0 END) AS zero_frac,
               avg(-s*f1)*1e4 AS rev_gross_bps
        FROM v GROUP BY tq ORDER BY tq
        """
    ).df()
    ov = con.sql(
        """
        SELECT t.tq,
               sum(d.r_on*d.r_on) / (sum(d.r_on*d.r_on)+sum(d.r_id*d.r_id)+2*sum(d.r_on*d.r_id)) AS var_on_share
        FROM dayret2 d JOIN symt t USING (symbol)
        WHERE abs(d.r_on) < 0.35 AND abs(d.r_id) < 0.35
        GROUP BY 1 ORDER BY 1
        """
    ).df()
    turn = con.sql(
        """
        SELECT tq, median(mturn)*100 AS turn_pct, median(mamt)/1e6 AS amt_mn FROM symt GROUP BY 1 ORDER BY 1
        """
    ).df()
    gt = gt.merge(turn, on="tq").merge(ov, on="tq")
    res["turnover_gradient"] = gt.round(6).to_dict("records")

    # ---------- 7. 日内收益能否预测次日 ----------
    pred = con.sql(
        """
        WITH x AS (
          SELECT symbol, date, r_id, r_on, r_cc,
                 lead(r_cc) OVER (PARTITION BY symbol ORDER BY date) AS nxt
          FROM dayret2 WHERE abs(r_cc) < 0.35
        )
        SELECT count(*) AS n,
               corr(r_id, nxt) AS c_id_nxt,
               corr(r_on, nxt) AS c_on_nxt,
               corr(r_cc, nxt) AS c_cc_nxt
        FROM x WHERE nxt IS NOT NULL
        """
    ).df().iloc[0]
    res["predict_next_day"] = {k: float(pred[k]) for k in ("c_id_nxt", "c_on_nxt", "c_cc_nxt")}
    res["predict_next_day"]["n"] = int(pred.n)

    # ---------- 8. 日级推断（n_eff = 交易日数，不是 bar 数） ----------
    daylvl = con.sql(
        """
        WITH x AS (
          SELECT date, r_cc, sign(r_cc) AS s,
                 lag(r_cc) OVER (PARTITION BY symbol, date ORDER BY j) AS l1,
                 lead(r_cc) OVER (PARTITION BY symbol, date ORDER BY j) AS f1
          FROM br WHERE r_cc IS NOT NULL
        )
        SELECT date,
               sum(r_cc * l1) / sum(r_cc * r_cc) AS rho1,
               avg(-s * f1) * 1e4 AS rev_bps,
               count(*) AS n
        FROM x WHERE l1 IS NOT NULL AND f1 IS NOT NULL
        GROUP BY date ORDER BY date
        """
    ).df()

    def tstat(v) -> tuple[float, float, int]:
        v = np.asarray(v, dtype=float)
        v = v[np.isfinite(v)]
        k = len(v)
        return float(v.mean()), float(v.mean() / (v.std(ddof=1) / np.sqrt(k))), int(k)

    m_rho, t_rho, k_rho = tstat(daylvl.rho1)
    m_rev, t_rev, k_rev = tstat(daylvl.rev_bps)
    res["day_level"] = {
        "n_days": k_rho,
        "rho1_mean": m_rho, "rho1_t": t_rho,
        "rev_bps_mean": m_rev, "rev_bps_t": t_rev,
        "rev_bps_pos_days": float((daylvl.rev_bps > 0).mean()),
        "rho1_neg_days": float((daylvl.rho1 < 0).mean()),
    }

    # ---------- 9. 逐年 隔夜/日内 漂移（均值易被尾部污染，并列中位） ----------
    yr = con.sql(
        """
        SELECT year(date) AS y, count(*) AS n,
               avg(r_on)*1e4 AS m_on, median(r_on)*1e4 AS md_on,
               avg(r_id)*1e4 AS m_id, median(r_id)*1e4 AS md_id,
               avg(r_cc)*1e4 AS m_cc
        FROM dayret2 WHERE abs(r_cc) < 0.35 GROUP BY 1 ORDER BY 1
        """
    ).df()
    res["by_year"] = yr.round(3).to_dict("records")

    # ---------- 10. 成本核算：反转 alpha vs 一次往返摩擦 ----------
    half_spread = np.sqrt(max(-ac.cov1, 0)) * 1e4
    res["cost_accounting"] = {
        "rev_gross_bps_per_bar": float(rev.gross_bps),
        "roll_half_spread_bps": float(half_spread),
        "round_trip_spread_bps": float(2 * half_spread),
        "stamp_duty_bps_sell": 5.0,
        "commission_bps_round_trip": 3.0,
        "total_round_trip_cost_bps": float(2 * half_spread + 5.0 + 3.0),
        "gross_over_cost": float(rev.gross_bps / (2 * half_spread + 5.0 + 3.0)),
    }

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "shape"}, ensure_ascii=False, indent=2)[:6000])


if __name__ == "__main__":
    main()
