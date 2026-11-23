"""α 为什么是 0.45 而不是 0.50？两个候选解释必须分开：

H1 5 分钟收盘随机采样价差弹跳 ⇒ 换手越活跃（bar 内成交笔数越多）α 越低，这是机械衰减。
H2 真实全价差 < 1 tick（不可能，但若 α 与换手无关则需要别的解释）。

做法：在 tick 主导的价格段（¥0-9）内，按「价格桶 × 换手五分位」交叉，看 h_tick 是否随换手下降。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_5min"
OUT = ROOT / "analysis" / "_diag_alpha_turnover.json"
STRIDE = 4
BINS = [0, 2, 3, 4, 5, 6, 7, 8, 9]


def main() -> None:
    days = sorted(p.name.split("=", 1)[1] for p in SRC.iterdir() if p.is_dir())
    sample = days[::STRIDE]
    files = [str((SRC / f"date={d}" / "part.parquet").as_posix()) for d in sample]
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet({files!r})")
    con.execute("""CREATE TABLE b48 AS SELECT * FROM (
        SELECT symbol,date,close,volume,row_number() OVER (PARTITION BY symbol,date ORDER BY datetime) j,
               count(*) OVER (PARTITION BY symbol,date) nb FROM raw) WHERE nb=48""")
    con.execute("""CREATE TABLE t1 AS
        SELECT symbol,date,j,close/lag(close) OVER w-1 rr
        FROM b48 WINDOW w AS (PARTITION BY symbol,date ORDER BY j)""")
    con.execute("""CREATE TABLE td AS
        SELECT symbol,date,j,rr,lead(rr) OVER (PARTITION BY symbol,date ORDER BY j) rn FROM t1""")
    con.execute("""
        CREATE TABLE sd AS
        SELECT b.symbol,b.date,max_by(b.close,b.j) mpx,sum(b.volume)*100.0/any_value(i.float_shares) turn
        FROM b48 b JOIN 'data/instruments/*.parquet' i USING(symbol)
        WHERE i.float_shares>0 AND b.volume>0 GROUP BY 1,2
    """)
    con.execute(f"""CREATE TABLE g AS
        SELECT * FROM (
          SELECT symbol,date,mpx,turn,
                 CASE WHEN mpx>={BINS[0]} AND mpx<{BINS[1]} THEN 0
                      WHEN mpx>={BINS[1]} AND mpx<{BINS[2]} THEN 1
                      WHEN mpx>={BINS[2]} AND mpx<{BINS[3]} THEN 2
                      WHEN mpx>={BINS[3]} AND mpx<{BINS[4]} THEN 3
                      WHEN mpx>={BINS[4]} AND mpx<{BINS[5]} THEN 4
                      WHEN mpx>={BINS[5]} AND mpx<{BINS[6]} THEN 5
                      WHEN mpx>={BINS[6]} AND mpx<{BINS[7]} THEN 6
                      WHEN mpx>={BINS[7]} AND mpx<{BINS[8]} THEN 7 END AS pb,
                 ntile(5) OVER (PARTITION BY CASE WHEN mpx>=0 AND mpx<2 THEN 0
                      WHEN mpx>=2 AND mpx<3 THEN 1 WHEN mpx>=3 AND mpx<4 THEN 2
                      WHEN mpx>=4 AND mpx<5 THEN 3 WHEN mpx>=5 AND mpx<6 THEN 4
                      WHEN mpx>=6 AND mpx<7 THEN 5 WHEN mpx>=7 AND mpx<8 THEN 6
                      WHEN mpx>=8 AND mpx<9 THEN 7 END ORDER BY turn) AS tq
          FROM sd) WHERE pb IS NOT NULL AND tq IS NOT NULL
    """)
    cells = con.sql("""
        SELECT g.pb, g.tq, count(*) n_pair,
               avg(t.rr*t.rr)*1e8 v, avg(t.rr*t.rn)*1e8 c,
               sum(t.rr*t.rr*g.mpx)/sum(t.rr*t.rr) p,
               median(g.mpx) p_med, median(g.turn) mturn,
               count(DISTINCT t.symbol) nsym
        FROM td t JOIN g USING(symbol,date)
        WHERE t.rr IS NOT NULL AND t.rn IS NOT NULL
        GROUP BY 1,2 ORDER BY 1,2
    """).df()
    cells["rho1"] = cells.c / cells.v
    cells["sd_bp"] = np.sqrt(cells.v)   # v 已是 avg(r²)×1e8 = bp²
    cells["tick_bp"] = 0.01 / cells.p * 1e4
    cells["h_tick"] = cells.sd_bp * np.sqrt(-cells.rho1) / cells.tick_bp
    cells["sd_tick"] = cells.sd_bp / cells.tick_bp
    BETA = 0.18
    ex = cells.h_tick ** 2 - (BETA * cells.sd_tick) ** 2
    cells["alpha_eq"] = np.sqrt(np.maximum(ex, 0.0))   # 扣掉冲击项后的纯 tick 系数
    cells = cells[cells.n_pair >= 20000]

    OUT.write_text(json.dumps({"cells": cells.round(6).to_dict("records")}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("h_tick（= 等价 α）按「价格桶 × 换手五分位」；若 5min 收盘欠采样成立，应随换手单调下降")
    print(f"{'价格':>7} " + "".join(f"{f'q{q}':>9}" for q in range(1, 6)) + "   跨度")
    for pb in sorted(cells.pb.unique()):
        g = cells[cells.pb == pb].sort_values("tq")
        if len(g) < 5:
            continue
        row = "".join(f"{v:9.3f}" for v in g.alpha_eq)
        print(f"{BINS[pb]}-{BINS[pb+1]:>4} " + row + f"   {g.alpha_eq.max()-g.alpha_eq.min():.3f}")
        if pb == 3:
            print("        换手中位: " + "".join(f"{v*100:8.2f}%" for v in g.mturn))

    print("\n同价格桶内 q5−q1 的 α 差")
    diffs = []
    for pb in sorted(cells.pb.unique()):
        g = cells[cells.pb == pb].sort_values("tq")
        if len(g) == 5:
            diffs.append((pb, float(g.alpha_eq.iloc[-1] - g.alpha_eq.iloc[0])))
    for pb, d in diffs:
        print(f"  {BINS[pb]}-{BINS[pb+1]:>3}: {d:+.3f}")
    print(f"  平均 {np.mean([d for _, d in diffs]):+.3f}   全段 α 中位 {cells.alpha_eq.median():.3f}  "
          f"范围 {cells.alpha_eq.min():.3f}–{cells.alpha_eq.max():.3f}")


if __name__ == "__main__":
    main()
