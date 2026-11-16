"""分钟 K 全量验收 — 三项判据, 每条都能证伪「下完了」这句话。

1. 规模: 行数 / 交易日数 / 标的数是否与 99 交易日 × 5500 只 × 240 根同量级
2. 唯一性: (symbol, datetime) 必须零重复 —— 同一根 bar 出现两次, 任何日内因子都会双计
3. 边界连续: 缺口段最后一天与已有段第一天之间, 标的集合与帧数不能断层
   (2026-10-07 → 2026-10-08 是本次回补与存量数据的接缝)

用法: backend/.venv/Scripts/python.exe analysis/_minute_verify.py [--seam 2026-10-08]
"""
from __future__ import annotations

import argparse
import duckdb

GLOB = "data/kline_minute/**/*.parquet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seam", default="2026-10-08", help="接缝日: 该日及之后是存量数据")
    a = ap.parse_args()

    con = duckdb.connect()
    con.execute(f"CREATE VIEW m AS SELECT * FROM read_parquet('{GLOB}')")

    rows, days, syms = con.execute(
        "SELECT count(*), count(DISTINCT datetime::DATE), count(DISTINCT symbol) FROM m"
    ).fetchone()
    print(f"行数 {rows:,} | 交易日 {days} | 标的 {syms}")
    print(f"预期量级: {days} × {syms} × 240 ≈ {days * syms * 240:,}")

    dup = con.execute(
        "SELECT count(*) FROM (SELECT symbol, datetime, count(*) c FROM m GROUP BY 1,2 HAVING c > 1)"
    ).fetchone()[0]
    print(f"(symbol,datetime) 重复组: {dup}  {'OK' if dup == 0 else 'FAIL'}")

    print("\n每日标的数(首尾各 5 天):")
    per_day = con.execute(
        "SELECT datetime::DATE d, count(DISTINCT symbol) s, count(*) n FROM m GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for d, s, n in per_day[:5] + per_day[-5:]:
        print(f"  {d}  标的 {s:5d}  行 {n:>10,}  根/只 {n/s:.0f}")

    thin = [(d, s) for d, s, _ in per_day if s < syms * 0.8]
    print(f"标的数 <80% 的交易日: {len(thin)} 天 {[str(d) for d, _ in thin[:5]]}")

    seam = a.seam
    print(f"\n接缝 {seam}:")
    prev = con.execute(
        f"SELECT max(datetime::DATE) FROM m WHERE datetime::DATE < DATE '{seam}'"
    ).fetchone()[0]
    nxt = con.execute(
        f"SELECT min(datetime::DATE) FROM m WHERE datetime::DATE >= DATE '{seam}'"
    ).fetchone()[0]
    for label, d in (("缺口段末日", prev), ("存量段首日", nxt)):
        if d is None:
            print(f"  {label}: 无数据")
            continue
        s, n, last_t = con.execute(
            f"SELECT count(DISTINCT symbol), count(*), max(datetime) FROM m WHERE datetime::DATE = DATE '{d}'"
        ).fetchone()
        print(f"  {label} {d}: 标的 {s}, 行 {n:,}, 末帧 {last_t}")

    # 末帧/首帧时刻分布: A 股 15:00 收盘, 09:31 开盘。断层时这里会露出来。
    print("\n最后一日的帧时刻分布(前 3 / 后 3):")
    for r in con.execute(
        "SELECT datetime::TIME t, count(*) c FROM m WHERE datetime::DATE = (SELECT max(datetime::DATE) FROM m) "
        "GROUP BY 1 ORDER BY 1 LIMIT 3"
    ).fetchall():
        print(f"  {r[0]}  {r[1]:,}")
    for r in con.execute(
        "SELECT datetime::TIME t, count(*) c FROM m WHERE datetime::DATE = (SELECT max(datetime::DATE) FROM m) "
        "GROUP BY 1 ORDER BY 1 DESC LIMIT 3"
    ).fetchall():
        print(f"  {r[0]}  {r[1]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
