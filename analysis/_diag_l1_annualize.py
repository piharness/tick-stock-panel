"""L1 的下一格：把 round5 的 +44.5bp/回合 换成年化和绝对利润，看量级站不站得住。

触发问题（2026-11-11）：§9.3 把容量算在 mom_bottom20 的成交额上，L1 的下载量算在
round3/S5 的 cohort 上。两处都要改。但改完后有一个更要紧的量没算：
44.5bp/日 x 242 天 = 107.7%/年，是仓库历史最强候选（1.63%/年毛）的 66 倍。
这个量级在任何容量讨论之前就要能被否掉或确认。

本脚本只做五件事，全部本地日线，不联网：

  1. 复现 round5 <=-1% 桶：只-日 / 去重 symbol / 每日只数 / i1，与
     `asymmetry-round5-inventory.md` §2 表逐位对拍（主动 64.5 / 逐笔 118.9）。
  2. 分清两个数：**主动 i1**（对缺口条件化 + 换手十分位匹配的基准，属归因）
     与 **原始 i1**（买入 8.7 只、收盘卖出，属 P&L）。年化只能用后者。
  3. 日序列的 mean / sd / t(iid) / t(NW5) / 块自举 CI、两半、剔最好 5% cohort、
     以及复利后的实际年化（样本 367 日 = 1.517 年）。
  4. 把 P&L 拆成三段：换手同业漂移 / 缺口反转 / 动量溢价。前两段与选股器无关。
  5. A* 写成首根 bar 份额 s 的函数——A* 正比于 s，所以「不测 s 就没有 A*」与
     「不测 s 也不影响判决」不能同时成立。同时给出用全日成交额当分母的 A*，
     说明它为什么偏大 1/s 倍。

ASCII 输出（GBK 控制台）。只读分区。
"""
from __future__ import annotations

import glob
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_base_t0 import leg  # noqa: E402
from _diag_gaprev import N_DECILE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261111
GAP_MAX = -0.01          # round5 的「低开 <=1.0%」桶：gap <= -1%
COST_BP = 20.0
FIRST_BAR_SHARE = 0.0352
FIRST_BAR_SHARE_P10 = 0.0154
DAYS_PER_YEAR = 242.0
IMPACT_LINE = 0.05


def nw_t(x: np.ndarray, lag: int = 5) -> float:
    x = x[np.isfinite(x)]
    n = x.size
    e = x - x.mean()
    s = float(e @ e) / n
    for l in range(1, lag + 1):
        s += 2.0 * (1.0 - l / (lag + 1.0)) * float(e[l:] @ e[:-l]) / n
    return float(x.mean() / math.sqrt(s / n))


def block_ci(x: np.ndarray, block: int = 20, B: int = 2000) -> tuple[float, float]:
    rng = np.random.default_rng(SEED)
    x = x[np.isfinite(x)]
    n = x.size
    nb = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n]
    m = x[idx].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    d_map = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    c, o = mats["close"], mats["open"]
    tradable = np.isfinite(c) & (c > 0) & np.isfinite(o) & (o > 0)
    C2 = leg(mats, "c2")
    I1 = leg(mats, "i1")
    picks = ap.pick_daily(df, "mom20", ascending=False).select("date", "symbols").to_dicts()

    day_raw, day_act, day_uni, day_gapbench, day_list = [], [], [], [], []
    cells = []
    for r in picks:
        i0 = d_map[str(r["date"])]
        if i0 + 2 >= T:
            continue
        sel = np.array([s_map[x] for x in r["symbols"]], dtype=np.int64)
        c0 = c[i0]
        pool = tradable[i0] & np.isfinite(C2[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
        if pool.sum() < 200:
            continue
        lim = c0 * (1.0 - abs(GAP_MAX))
        fill = pool & tradable[i0 + 1] & (o[i0 + 1] <= lim) & (lim > 0)
        good = sel[fill[sel]]
        if good.size < 3:
            continue
        pool_idx = np.flatnonzero(pool)
        tv = turn[i0][pool_idx]
        order = np.argsort(tv, kind="stable")
        rk = np.empty(pool_idx.size, dtype=np.int64)
        rk[order] = np.arange(pool_idx.size)
        dec_full = np.full(S, -1, dtype=np.int64)
        dec_full[pool_idx] = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
        fpool = pool_idx[fill[pool_idx]]
        if fpool.size < 50:
            continue
        dsel = dec_full[good]
        cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
        if cnt.sum() <= 0:
            continue
        w = cnt / cnt.sum()
        pm = np.full(N_DECILE, np.nan)
        for d in range(N_DECILE):
            m = dec_full[fpool] == d
            if m.sum() >= 5:
                v = I1[i0][fpool[m]]
                v = v[np.isfinite(v)]
                if v.size >= 5:
                    pm[d] = float(v.mean())
        ok = np.isfinite(pm) & (w > 0)
        if not ok.any():
            continue
        bench = float(np.dot(w[ok] / w[ok].sum(), pm[ok]))
        r_i1 = I1[i0][good]
        r_i1 = r_i1[np.isfinite(r_i1)]
        if r_i1.size < 3:
            continue
        raw_m = float(r_i1.mean())
        uni_v = I1[i0][pool_idx]
        uni_v = uni_v[np.isfinite(uni_v)]
        day_raw.append(raw_m)
        day_act.append(raw_m - bench)
        day_gapbench.append(bench)
        day_uni.append(float(uni_v.mean()))
        day_list.append(str(dates[i0 + 1]))
        for j in good:
            cells.append((str(dates[i0 + 1]), symbols[j], float(I1[i0][j])))

    raw = np.array(day_raw) * 1e4
    act = np.array(day_act) * 1e4
    bench = np.array(day_gapbench) * 1e4
    uni = np.array(day_uni) * 1e4
    per_trade = np.array([x[2] for x in cells]) * 1e4
    n_day, n_cell = raw.size, len(cells)
    u_sym = len({x[1] for x in cells})
    span_years = T / DAYS_PER_YEAR
    sig_per_year = n_day / span_years

    parity = {
        "n_days": n_day,
        "n_cells": n_cell,
        "n_symbols": u_sym,
        "names_per_day": round(n_cell / n_day, 2),
        "active_bp": round(float(act.mean()), 1),
        "raw_bp": round(float(raw.mean()), 1),
        "per_trade_raw_bp": round(float(per_trade.mean()), 1),
        "uni_drift_bp": round(float(uni.mean()), 1),
        "gap_bench_bp": round(float(bench.mean()), 1),
    }

    sd = float(raw.std(ddof=1))
    net_raw = raw - COST_BP
    net_act = act - COST_BP
    trim = int(round(n_day * 0.05))
    order = np.argsort(-raw)
    keep = np.setdiff1d(np.arange(n_day), order[:trim])
    down = uni < 0

    def ann(x: np.ndarray) -> dict:
        eq = float(np.prod(1.0 + x / 1e4))
        return {
            "net_bp": round(float(x.mean()), 1),
            "ann_simple_x_obs": round(float(x.mean()) * sig_per_year / 1e4, 4),
            "ann_compound_realized": round(eq ** (1.0 / span_years) - 1.0, 4),
            "equity_multiple": round(eq, 3),
        }

    series = {
        "raw": {
            "mean_bp": round(float(raw.mean()), 1),
            "sd_bp": round(sd, 1),
            "t_iid": round(float(raw.mean() / (sd / math.sqrt(n_day))), 2),
            "t_nw5": round(nw_t(raw, 5), 2),
            "ci95_block20_bp": [round(v, 1) for v in block_ci(raw)],
        },
        "active": {
            "mean_bp": round(float(act.mean()), 1),
            "t_iid": round(float(act.mean() / (act.std(ddof=1) / math.sqrt(n_day))), 2),
            "ci95_block20_bp": [round(v, 1) for v in block_ci(act)],
        },
        "halves_raw_bp": [round(float(raw[: n_day // 2].mean()), 1),
                          round(float(raw[n_day // 2:].mean()), 1)],
        "trim_best5pct_raw_bp": round(float(raw[keep].mean()), 1),
        "span_trading_days": T,
        "span_years": round(span_years, 3),
        "signal_days": n_day,
        "signal_days_per_year": round(sig_per_year, 1),
        "ann_on_round_capital": {
            "raw_PL": ann(net_raw),
            "active_attribution": ann(net_act),
        },
        "ann_on_total_equity_inventory_equal": {
            "raw_PL": round(ann(net_raw)["ann_simple_x_obs"] / 2.0, 4),
            "active_attribution": round(ann(net_act)["ann_simple_x_obs"] / 2.0, 4),
        },
    }

    # 三段分解：P&L = 换手同业漂移 + 缺口反转 + 动量溢价
    drift = float(uni.mean())
    gap_eff = float(bench.mean()) - drift
    mom = float(raw.mean()) - float(bench.mean())
    decomp = {
        "peer_drift_bp": round(drift, 1),
        "gap_reversal_bp": round(gap_eff, 1),
        "momentum_premium_bp": round(mom, 1),
        "share_momentum": round(mom / float(raw.mean()), 3),
        "universe_down_days": {
            "n": int(down.sum()),
            "raw_bp": round(float(raw[down].mean()), 1) if down.any() else None,
            "active_bp": round(float(act[down].mean()), 1) if down.any() else None,
        },
        "universe_up_days": {
            "n": int((~down).sum()),
            "raw_bp": round(float(raw[~down].mean()), 1),
            "active_bp": round(float(act[~down].mean()), 1),
        },
    }

    # A* 与 s
    files = sorted(glob.glob(str(ROOT / "data" / "kline_daily_enriched" / "date=*" / "part.parquet")))
    src = pl.concat([pl.read_parquet(f, columns=["symbol", "date", "amount"]) for f in files],
                    how="vertical_relaxed")
    a_map = {(d, s): v for d, s, v in
             zip(src["date"].cast(pl.Utf8).to_list(), src["symbol"].to_list(),
                 src["amount"].to_numpy().astype(float))}
    a = np.array([a_map.get((x[0], x[1]), np.nan) for x in cells], dtype=float)
    a = a[np.isfinite(a) & (a > 0)]
    a_med = float(np.median(a))
    names = n_cell / n_day
    r_net = float(net_raw.mean()) / 1e4
    r_net_act = float(net_act.mean()) / 1e4

    def block(share: float) -> dict:
        a_round = names * IMPACT_LINE * share * a_med
        return {
            "share": share,
            "a_star_round_cny": round(a_round, 0),
            "a_star_total_equity_cny": round(a_round * 2, 0),
            "annual_profit_raw_cny": round(a_round * r_net * sig_per_year, 0),
            "annual_profit_active_cny": round(a_round * r_net_act * sig_per_year, 0),
        }

    cap = {
        "s_point_3.52pct": block(FIRST_BAR_SHARE),
        "s_p10_1.54pct": block(FIRST_BAR_SHARE_P10),
        "s_20pct": block(0.20),
        "share_implied_by_A_star_20m": round(
            2e7 / (names * IMPACT_LINE * a_med), 4),
        "full_day_denominator_a_star_round_cny": round(names * IMPACT_LINE * a_med, 0),
        "full_day_overstatement": round(1.0 / FIRST_BAR_SHARE, 1),
    }

    out = {
        "seed": SEED,
        "cell": "top20 mom20, gap<=-1.0%, open[d+1]->close[d+1]",
        "parity_vs_round5": parity,
        "series": series,
        "decomposition": decomp,
        "amount_cny": {"median": round(a_med, 0), "n": int(a.size)},
        "capacity_vs_share": cap,
        "elapsed_s": round(time.time() - t0, 1),
    }
    p = ROOT / "analysis" / "_diag_l1_annualize_result.json"
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"parity: {n_day} days / {n_cell} cells / {u_sym} symbols / {parity['names_per_day']}/day")
    print(f"  active {parity['active_bp']}bp  raw {parity['raw_bp']}bp  per-trade {parity['per_trade_raw_bp']}bp")
    print(f"  uni drift {parity['uni_drift_bp']}bp  gap-bench {parity['gap_bench_bp']}bp")
    print(f"raw: sd {sd:.1f} t {series['raw']['t_iid']} NW5 {series['raw']['t_nw5']} "
          f"CI {series['raw']['ci95_block20_bp']}  halves {series['halves_raw_bp']} "
          f"trim5% {series['trim_best5pct_raw_bp']}")
    print(f"  net {series['ann_on_round_capital']['raw_PL']['net_bp']}bp x {sig_per_year:.0f}/yr -> "
          f"{series['ann_on_round_capital']['raw_PL']['ann_simple_x_obs']*100:.0f}% "
          f"(compound {series['ann_on_round_capital']['raw_PL']['ann_compound_realized']*100:.0f}%)")
    print(f"  decomposition: drift {decomp['peer_drift_bp']}  gap {decomp['gap_reversal_bp']}  "
          f"mom {decomp['momentum_premium_bp']} ({decomp['share_momentum']*100:.0f}%)")
    print(f"  down days {decomp['universe_down_days']}")
    for k in ("s_point_3.52pct", "s_p10_1.54pct", "s_20pct"):
        v = cap[k]
        print(f"  {k}: A*round {v['a_star_round_cny']/1e4:.0f}wan  "
              f"profit {v['annual_profit_raw_cny']/1e4:.0f}wan/yr")
    print(f"  A* implied by 2000wan: share {cap['share_implied_by_A_star_20m']*100:.2f}%")
    print(f"elapsed {out['elapsed_s']}s")


if __name__ == "__main__":
    main()
