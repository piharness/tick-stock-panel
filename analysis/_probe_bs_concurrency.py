"""实测 baostock 并发扩展性：同一批全历史标的在不同 worker 数下的吞吐。

退市股批次 3 workers 实测 20.1s/只（壁钟），等于单连接 ~60s/只的 1/3 线性扩展。
本脚本直接测 w=8 / w=12，判断「2 天」是真上限还是串行假设的产物。

不写 state、不落盘，纯测速。用法:
    python analysis/_probe_bs_concurrency.py --workers 8 --n 12
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(r"D:/DSH/tick-stock-panel/data/_probe_t0")))
sys.path.insert(0, str(Path(r"D:/DSH/tick-stock-panel/data/_probe_t0/_extlib")))

import polars as pl  # noqa: E402
from _fixed_bs import FixedBaostock  # noqa: E402

DATA = Path(r"D:/DSH/tick-stock-panel/data")
SYMS = ["000001.SZ", "600519.SH", "000002.SZ", "600036.SH", "601318.SH",
        "000858.SZ", "002415.SZ", "300750.SZ", "600030.SH", "601888.SH",
        "000651.SZ", "600887.SH"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--start", default="2020-01-02")
    ap.add_argument("--end", default="2024-11-06")
    a = ap.parse_args()

    syms = SYMS[: a.n]
    socket.setdefaulttimeout(60)
    # 先单连接测一只作为基线
    c0 = FixedBaostock()
    assert c0.login(), "login 失败"
    t = time.time()
    r = c0.query_k(syms[0].split(".")[1].lower() + "." + syms[0].split(".")[0],
                   "date,time,open,high,low,close,volume", a.start, a.end,
                   frequency="5", adjustflag="3")
    single = time.time() - t
    nrow = len(r.get("rows", []))
    print(f"单连接基线 {syms[0]}: {single:.1f}s / {nrow} 行 "
          f"({nrow/max(single,1e-9):.0f} 行/s)", flush=True)
    c0.logout()

    clients = [FixedBaostock() for _ in range(a.workers)]
    ok = []
    lk = threading.Lock()

    def login(c):
        if c.login():
            with lk:
                ok.append(1)

    ths = [threading.Thread(target=login, args=(c,)) for c in clients]
    for x in ths:
        x.start()
    for x in ths:
        x.join()
    print(f"login ok x{len(ok)} / {a.workers}", flush=True)

    from queue import Queue
    q = Queue()
    for s in syms:
        q.put(s)
    res, lock = [], threading.Lock()

    def worker(c, wid):
        while True:
            try:
                s = q.get_nowait()
            except Exception:
                return
            code = s.split(".")[1].lower() + "." + s.split(".")[0]
            t0 = time.time()
            try:
                r = c.query_k(code, "date,time,open,high,low,close,volume",
                              a.start, a.end, frequency="5", adjustflag="3")
                n = len(r.get("rows", []))
                st = "ok" if r.get("error_code") == "0" else f"err{r.get('error_code')}"
            except Exception as e:  # noqa: BLE001
                n, st = 0, f"exc {type(e).__name__}"
            dt = time.time() - t0
            with lock:
                res.append((s, dt, n, st))
            print(f"  {s} {dt:.1f}s {n} rows {st} (w{wid})", flush=True)

    t_all = time.time()
    ws = [threading.Thread(target=worker, args=(c, i)) for i, c in enumerate(clients)]
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    wall = time.time() - t_all
    for c in clients:
        try:
            c.logout()
        except Exception:  # noqa: BLE001
            pass

    df = pl.DataFrame(res, schema=["symbol", "secs", "rows", "status"], orient="row")
    print(f"\n=== w={a.workers}  n={df.height}  wall={wall:.1f}s ===")
    print(f"  单只 secs: median={df['secs'].median():.1f} "
          f"mean={df['secs'].mean():.1f} max={df['secs'].max():.1f}")
    print(f"  吞吐: {df.height/wall*60:.2f} 只/分钟 ⇒ "
          f"5572 只需 {5572/(df.height/wall)/3600:.1f} 小时")
    print(f"  行数 median={df['rows'].median():.0f} 状态={df['status'].to_list()}")


if __name__ == "__main__":
    main()
