"""散户级择时三问的实测：抄底行业 / 成交额极值 / 降息事件。

口径铁律（本项目母题）:
- 「跌很多」必须在**同一天横截面**上比较, 否则拿到的是熊市 beta, 不是抄底 alpha。
- 「下次牛市止盈」是事后规则; 可执行的替代是移动止盈, 两者必须分别报。
- 所有择时策略的对手盘是「一直持有」和「定投」, 不比这两个就是自欺。
- 成交额极值事件高度聚簇 ⇒ 有效样本是「事件段」不是「交易日」, 必须报事件数与 bootstrap CI。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
OUT = Path(__file__).resolve().parent / "_diag_dip_buy_result.json"

IDX = DATA / "backfill_20261117" / "index_daily_full.parquet"
SH, SZ = "000001.SH", "399001.SZ"
GEM, STAR, HS300 = "399006.SZ", "000688.SH", "000300.SH"


def load_wide(col: str = "close") -> tuple[np.ndarray, list[str], np.ndarray]:
    df = pl.read_parquet(IDX, columns=["symbol", "date", col])
    df = df.unique(subset=["symbol", "date"], keep="last").sort(["date", "symbol"])
    wide = df.pivot(index="date", on="symbol", values=col, aggregate_function="last").sort("date")
    dates = wide["date"].to_list()
    syms = [c for c in wide.columns if c != "date"]
    mat = wide.select(syms).to_numpy().astype(float)
    return dates, syms, mat


def rolling_max(a: np.ndarray, w: int) -> np.ndarray:
    """逐列 rolling max (窗口 w, 忽略 nan, 要求窗口内至少 w//2 个有效)。"""
    n = len(a)
    out = np.full_like(a, np.nan)
    for j in range(a.shape[1]):
        col = a[:, j]
        valid = np.isfinite(col)
        # 用 pandas 的语义会更快, 但这里只要 593 列 × 8765 行, 朴素做法够
        from collections import deque
        dq: deque[int] = deque()
        cnt = 0
        for i in range(n):
            if valid[i]:
                while dq and col[dq[-1]] <= col[i]:
                    dq.pop()
                dq.append(i)
                cnt += 1
            if dq and dq[0] <= i - w:
                dq.popleft()
            if i - w >= 0 and valid[i - w]:
                cnt -= 1
            if cnt >= w // 2 and dq:
                out[i, j] = col[dq[0]]
    return out


def fwd(a: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    out[:-h] = a[h:] / a[:-h] - 1.0
    return out


def block_ci(x: np.ndarray, block: int = 40, n_boot: int = 2000, seed: int = 7) -> tuple[float, float]:
    x = x[np.isfinite(x)]
    if len(x) < block * 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(len(x) / block))
    starts = rng.integers(0, len(x) - block, size=(n_boot, nb))
    means = np.array([np.concatenate([x[s : s + block] for s in row]).mean() for row in starts])
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def evaluate(dates, syms, close, amount) -> dict:
    out: dict = {}
    iG, iS, iH = syms.index(GEM), syms.index(STAR), syms.index(HS300)

    # ---------- A. 回撤分桶 → 未来 250 日 ----------
    rmax = rolling_max(close, 500)
    dd = close / rmax - 1.0
    f250 = fwd(close, 250)
    f60 = fwd(close, 60)

    # 横截面同日基准（等权全指数, 剔除 nan）
    xs250 = np.nanmean(f250, axis=1, keepdims=True)
    xs60 = np.nanmean(f60, axis=1, keepdims=True)
    ex250 = f250 - xs250
    ex60 = f60 - xs60

    buckets = [(-1.0, -0.5), (-0.5, -0.4), (-0.4, -0.3), (-0.3, -0.2), (-0.2, -0.1), (-0.1, 0.0)]
    rows = []
    for lo, hi in buckets:
        m = np.isfinite(dd) & np.isfinite(f250) & (dd > lo) & (dd <= hi)
        n = int(m.sum())
        if n < 200:
            rows.append({"dd": f"({lo:.0%},{hi:.0%}]", "n": n})
            continue
        # 250 日窗完全不重叠才有独立观测; 用 block=250 的日块 bootstrap 给 CI
        lo250, hi250 = block_ci(np.asarray(ex250[m]).ravel(), block=250, n_boot=400)
        m60 = m & np.isfinite(f60)
        ex60v = np.asarray(ex60[m60]).ravel()
        rows.append({
            "dd": f"({lo:.0%},{hi:.0%}]",
            "n": n,
            "n_days": int(np.unique(np.where(m)[0]).size),
            "fwd250_mean": round(float(f250[m].mean()) * 100, 2),
            "fwd250_med": round(float(np.median(f250[m])) * 100, 2),
            "win": round(float((f250[m] > 0).mean()), 3),
            "ex250_mean": round(float(ex250[m].mean()) * 100, 2),
            "ex250_ci": [round(lo250 * 100, 2), round(hi250 * 100, 2)],
            "ex60_mean": round(float(ex60v.mean()) * 100, 2),
            "ex60_t_naive": round(float(ex60v.mean() / (ex60v.std(ddof=1) / math.sqrt(len(ex60v)))), 2),
        })
    out["A_dd_buckets"] = rows

    # 深跌（<= -50%）按年份看是否只是某几轮
    deep = np.isfinite(dd) & (dd <= -0.5) & np.isfinite(f250)
    yr = np.array([d.year for d in dates])
    yrows = []
    for y in np.unique(yr[deep.any(axis=1)]):
        m = deep & (yr == y)[:, None]
        if m.sum() < 20:
            continue
        yrows.append({
            "year": int(y),
            "n": int(m.sum()),
            "n_sym": int(np.unique(np.where(m)[1]).size),
            "fwd250_mean": round(float(f250[m].mean()) * 100, 2),
            "ex250_mean": round(float(ex250[m].mean()) * 100, 2),
        })
    out["A2_deep_by_year"] = yrows

    # ---------- B. 全市场成交额极值 → 宽基 ----------
    amt_mkt = np.nansum(np.stack([amount[:, syms.index(SH)], amount[:, syms.index(SZ)]]), axis=0)
    amt_mkt = np.where(amt_mkt > 0, amt_mkt, np.nan)
    s = pl.Series(amt_mkt)
    ma20 = s.rolling_mean(20).to_numpy()
    pct = np.full_like(ma20, np.nan)
    w = 750
    for i in range(len(ma20)):
        if not np.isfinite(ma20[i]) or i < w:
            continue
        win = ma20[i - w : i + 1]
        win = win[np.isfinite(win)]
        if len(win) < w // 2:
            continue
        pct[i] = float((win <= ma20[i]).mean())

    signals = {}
    for name, thr in (("pct<2%", 0.02), ("pct<5%", 0.05), ("pct<10%", 0.10)):
        sig = np.isfinite(pct) & (pct < thr)
        # 事件段: 连续信号只取第一天
        first = sig & ~np.r_[False, sig[:-1]]
        ev = []
        for i in np.where(first)[0]:
            row = {"date": str(dates[i])}
            for tname, j in (("gem", iG), ("star", iS), ("hs300", iH)):
                for h in (60, 250):
                    v = close[i : i + h + 1, j]
                    if len(v) == h + 1 and np.isfinite(v).all() and v[0] > 0:
                        row[f"{tname}_{h}d"] = round(float(v[-1] / v[0] - 1.0) * 100, 2)
            ev.append(row)
        # 全部信号日的统计（含重叠）
        stats = {}
        for tname, j in (("gem", iG), ("star", iS), ("hs300", iH)):
            for h in (60, 250):
                f = fwd(close[:, j], h)
                v = f[sig]
                v = v[np.isfinite(v)]
                base = f[np.isfinite(f)]
                if len(v) < 30:
                    continue
                lo, hi = block_ci(v)
                stats[f"{tname}_{h}d"] = {
                    "n": int(len(v)),
                    "mean": round(float(v.mean()) * 100, 2),
                    "med": round(float(np.median(v)) * 100, 2),
                    "win": round(float((v > 0).mean()), 3),
                    "ci95": [round(lo * 100, 2), round(hi * 100, 2)],
                    "base_mean": round(float(base.mean()) * 100, 2),
                    "base_win": round(float((base > 0).mean()), 3),
                }
        signals[name] = {"n_events": int(first.sum()), "events": ev, "stats": stats}
    out["B_turnover_extreme"] = signals

    # ---------- C. 策略对照: 信号 vs 一直持有 vs 定投 ----------
    def run_signal(px: np.ndarray, sig: np.ndarray, trail: float | None = 0.15, target: float | None = None,
                   cash_y: float = 0.015, cost: float = 0.0006) -> dict:
        """信号次日收盘买入; trail=从最高价回撤多少清仓; target=涨多少止盈; 两者都为 None 则永不卖。"""
        n = len(px)
        eq = np.zeros(n)
        v = 1.0
        pos = 0.0
        peak = np.nan
        entry = np.nan
        in_pos = 0
        trades = 0
        daily_cash = (1.0 + cash_y) ** (1.0 / 244.0) - 1.0
        for i in range(n):
            if not np.isfinite(px[i]):
                eq[i] = eq[i - 1] if i > 0 else 1.0
                continue
            holding = pos > 0
            sell = False
            if holding:
                peak = max(peak, px[i]) if np.isfinite(peak) else px[i]
                if trail is not None and px[i] <= peak * (1.0 - trail):
                    sell = True
                if target is not None and px[i] >= entry * (1.0 + target):
                    sell = True
            if sell:
                v = pos * px[i] * (1.0 - cost)
                pos = 0.0
                peak = np.nan
                entry = np.nan
                trades += 1
                holding = False
            if not holding and sig[i]:
                pos = v * (1.0 - cost) / px[i]
                v = 0.0
                peak = px[i]
                entry = px[i]
                trades += 1
                holding = True
            if holding:
                in_pos += 1
                eq[i] = pos * px[i]
            else:
                v *= (1.0 + daily_cash)
                eq[i] = v
        ret = eq[-1] / eq[0] - 1.0
        ok = np.where(np.isfinite(px))[0]
        yrs = (ok[-1] - ok[0] + 1) / 244.0
        curve = eq
        peakc = np.maximum.accumulate(np.where(np.isfinite(curve), curve, np.nan))
        mdd = float(np.nanmin(curve / peakc - 1.0))
        return {
            "total": round(float(ret) * 100, 1),
            "cagr": round(float((1.0 + ret) ** (1.0 / yrs) - 1.0) * 100, 2) if ret > -1 else -100.0,
            "mdd": round(mdd * 100, 1),
            "in_market": round(in_pos / n, 3),
            "trades": trades,
        }

    def run_hold(px: np.ndarray) -> dict:
        v = px[np.isfinite(px)]
        ret = v[-1] / v[0] - 1.0
        yrs = len(v) / 244.0
        peak = np.maximum.accumulate(v)
        return {
            "total": round(float(ret) * 100, 1),
            "cagr": round(float((1.0 + ret) ** (1.0 / yrs) - 1.0) * 100, 2),
            "mdd": round(float(np.min(v / peak - 1.0)) * 100, 1),
            "in_market": 1.0,
            "trades": 1,
        }

    def run_dca(px: np.ndarray, cash_y: float = 0.015) -> dict:
        """每月首个交易日投入 1 单位; 返回资金加权年化 (IRR)。"""
        n = int(np.where(np.isfinite(px))[0][-1]) + 1
        daily_cash = (1.0 + cash_y) ** (1.0 / 244.0) - 1.0
        v = 0.0
        shares = 0.0
        contrib = []
        prev_m = None
        for i in range(n):
            if not np.isfinite(px[i]):
                continue
            m = dates[i].month if i < len(dates) else None
            if m != prev_m:
                v += 1.0
                contrib.append(i)
                prev_m = m
            v *= (1.0 + daily_cash)
        # 第二遍: 真实持仓 (上面只是把现金记账, 这里重放买入)
        shares = 0.0
        cash = 0.0
        prev_m = None
        total_in = 0.0
        for i in range(n):
            if not np.isfinite(px[i]):
                continue
            m = dates[i].month
            if m != prev_m:
                cash += 1.0
                total_in += 1.0
                prev_m = m
            if cash > 0:
                shares += cash / px[i]
                cash = 0.0
        final = shares * px[np.where(np.isfinite(px))[0][-1]]
        # IRR: Σ (1+irr)^((n-i)/244) = final  (终值口径, 指数递增)
        def f(irr: float) -> float:
            acc = 0.0
            for i in contrib:
                acc += (1.0 + irr) ** ((n - i) / 244.0)
            return acc - final
        lo, hi = -0.9, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if f(mid) < 0:
                lo = mid
            else:
                hi = mid
        return {
            "irr": round(((lo + hi) / 2) * 100, 2),
            "total_in": round(total_in, 0),
            "final_multiple": round(final / total_in, 2),
        }

    # ---------- D. 完美择时上限 & 独立牛熊轮次 ----------
    def perfect_timing(px: np.ndarray) -> dict:
        """每年买在年内最低收盘、卖在年内最高收盘 (含未来信息, 只是上限)。"""
        mult = 1.0
        for y in sorted({d.year for d in dates}):
            idx = [i for i in range(len(px)) if dates[i].year == y and np.isfinite(px[i])]
            if len(idx) < 20:
                continue
            vals = px[idx]
            mult *= float(vals.max() / vals.min())
        ok = np.where(np.isfinite(px))[0]
        yrs = (ok[-1] - ok[0] + 1) / 244.0
        return {"cagr": round((mult ** (1.0 / yrs) - 1.0) * 100, 1)}

    def swings(px: np.ndarray, thr: float = 0.2) -> dict:
        """20% 规则数牛熊: 返回轮次数与每段起止。"""
        v = px[np.isfinite(px)]
        idx = np.where(np.isfinite(px))[0]
        segs = []
        start = 0
        direction = 0
        ext_i = 0
        for i in range(1, len(v)):
            if direction >= 0 and v[i] > v[ext_i]:
                if direction == 0:
                    direction, ext_i = 1, i
                else:
                    ext_i = i
            elif direction >= 0 and v[i] <= v[ext_i] * (1 - thr):
                if direction == 1:
                    segs.append(("bull", idx[start], idx[ext_i]))
                direction, ext_i, start = -1, i, ext_i
            elif direction <= 0 and v[i] < v[ext_i]:
                if direction == 0:
                    direction, ext_i = -1, i
                else:
                    ext_i = i
            elif direction <= 0 and v[i] >= v[ext_i] * (1 + thr):
                if direction == -1:
                    segs.append(("bear", idx[start], idx[ext_i]))
                direction, ext_i, start = 1, i, ext_i
        bulls = [s for s in segs if s[0] == "bull"]
        bears = [s for s in segs if s[0] == "bear"]
        return {
            "n_bull": len(bulls),
            "n_bear": len(bears),
            "bulls": [(str(dates[a]), str(dates[b]), round(float(px[b] / px[a] - 1) * 100, 1)) for _, a, b in bulls],
            "bears": [(str(dates[a]), str(dates[b]), round(float(px[b] / px[a] - 1) * 100, 1)) for _, a, b in bears],
        }

    def run_add(px: np.ndarray, sig: np.ndarray, base: float = 0.5, high: float = 1.0,
                cash_y: float = 0.015, cost: float = 0.0006) -> dict:
        """仓位控制版: 底仓 base 常驻, 信号期间加到 high, 其余时间回落到 base。"""
        n = len(px)
        eq = np.zeros(n)
        w = base
        val = 1.0
        daily_cash = (1.0 + cash_y) ** (1.0 / 244.0) - 1.0
        peak = np.nan
        mdd = 0.0
        in_high = 0
        for i in range(n):
            if not np.isfinite(px[i]):
                eq[i] = eq[i - 1] if i > 0 else 1.0
                continue
            ret = px[i] / px[i - 1] - 1.0 if i > 0 and np.isfinite(px[i - 1]) else 0.0
            target = high if sig[i] else base
            if target > w:
                in_high += 1
            turnover = abs(target - w)
            val *= 1.0 + w * ret + (1.0 - w) * daily_cash - turnover * cost
            w = target
            eq[i] = val
            peak = max(peak, val) if np.isfinite(peak) else val
            mdd = min(mdd, val / peak - 1.0)
        ok = np.where(np.isfinite(px))[0]
        yrs = (ok[-1] - ok[0] + 1) / 244.0
        ret = eq[-1] / eq[0] - 1.0
        return {
            "total": round(float(ret) * 100, 1),
            "cagr": round(float((1.0 + ret) ** (1.0 / yrs) - 1.0) * 100, 2) if ret > -1 else -100.0,
            "mdd": round(mdd * 100, 1),
            "in_market": round(in_high / n, 3),
            "trades": 0,
        }

    strat = {}
    for tname, j in (("gem", iG), ("hs300", iH)):
        px = close[:, j]
        sig = np.isfinite(pct) & (pct < 0.05)
        sig = np.r_[False, sig[:-1]]  # 次日执行
        strat[tname] = {
            "hold": run_hold(px),
            "dca": run_dca(px),
            "signal_hold": run_signal(px, sig, trail=None, target=None),
            "signal_tp100": run_signal(px, sig, trail=None, target=1.0),
            "signal_tp50": run_signal(px, sig, trail=None, target=0.5),
            "signal_trail15": run_signal(px, sig, trail=0.15),
            "signal_trail25": run_signal(px, sig, trail=0.25),
            "add_50_to_100": run_add(px, sig, base=0.5, high=1.0),
            "add_30_to_80": run_add(px, sig, base=0.3, high=0.8),
            "perfect": perfect_timing(px),
            "swings": swings(px),
        }
    out["C_strategy"] = strat

    # ---------- E. 美联储首次降息（事件研究, N 极小, 只为给量级） ----------
    # 只列可确证的"一轮宽松周期的首次降息"; 2025-2026 无本地事件日历, 不编造。
    FED_FIRST_CUT = ["2001-01-03", "2007-09-18", "2019-07-31", "2020-03-03", "2024-09-18"]
    ev_rows = []
    for ds in FED_FIRST_CUT:
        di = next((i for i, d in enumerate(dates) if str(d) >= ds), None)
        if di is None:
            continue
        row = {"fed_date": ds, "trade_date": str(dates[di])}
        for tname, j in (("gem", iG), ("hs300", iH)):
            for h in (20, 60, 250):
                if di + h < len(close) and np.isfinite(close[di : di + h + 1, j]).all():
                    row[f"{tname}_{h}d"] = round(float(close[di + h, j] / close[di, j] - 1) * 100, 2)
        ev_rows.append(row)
    out["E_fed_first_cut"] = ev_rows
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    dates, syms, close = load_wide("close")
    _, _, amount = load_wide("amount")
    print(f"index panel: {close.shape[0]} 日 × {close.shape[1]} 只  {dates[0]}..{dates[-1]}", flush=True)

    res = evaluate(dates, syms, close, amount)

    print("\n== A 回撤分桶 → 未来 250 日（同日期横截面去均值, 250日块 bootstrap） ==")
    print(f'{"dd":>14} {"N":>7} {"日数":>5} {"fwd250":>8} {"中位":>7} {"胜率":>6} {"超额":>7} {"CI95(超额)":>18} {"ex60":>7}')
    for r in res["A_dd_buckets"]:
        if "fwd250_mean" not in r:
            print(f'{r["dd"]:>14} {r["n"]:>7}  样本不足')
            continue
        print(f'{r["dd"]:>14} {r["n"]:>7} {r["n_days"]:>5} {r["fwd250_mean"]:>8.2f} {r["fwd250_med"]:>7.2f} '
              f'{r["win"]:>6.1%} {r["ex250_mean"]:>7.2f} '
              f'[{r["ex250_ci"][0]:>6.2f},{r["ex250_ci"][1]:>6.2f}] {r["ex60_mean"]:>7.2f}')

    print("\n== A2 dd<=-50% 分年（看是不是只有几轮） ==")
    for r in res["A2_deep_by_year"]:
        print(f'{r["year"]}  n={r["n"]:>5} 指数{r["n_sym"]:>3}只  fwd250 {r["fwd250_mean"]:>8.2f}%  超额 {r["ex250_mean"]:>7.2f}%')

    print("\n== B 成交额极度萎缩（ma20 分位） ==")
    for name, blk in res["B_turnover_extreme"].items():
        print(f'-- {name}: 事件段 {blk["n_events"]} 次  日期: {[e["date"] for e in blk["events"]]}')
        for k, v in blk["stats"].items():
            print(f'   {k:<10} n={v["n"]:>5} mean={v["mean"]:>7.2f}% med={v["med"]:>7.2f}% '
                  f'win={v["win"]:.1%} CI[{v["ci95"][0]:>7.2f},{v["ci95"][1]:>7.2f}] '
                  f'| 基准 {v["base_mean"]:>7.2f}% win {v["base_win"]:.1%}')

    print("\n== C 策略对照（起点=各自指数上市日, 现金年化 1.5%, 单边 3bp） ==")
    for tname, blk in res["C_strategy"].items():
        print(f"-- {tname}")
        for k in ("hold", "dca", "signal_hold", "signal_tp50", "signal_tp100", "signal_trail15",
                  "signal_trail25", "add_50_to_100", "add_30_to_80"):
            v = blk[k]
            if k == "dca":
                print(f'   {k:<16} IRR={v["irr"]:>6.2f}%  投入{v["total_in"]:>4.0f}份 终值{v["final_multiple"]:>5.2f}x')
            else:
                print(f'   {k:<16} 总收益{v["total"]:>8.1f}%  年化{v["cagr"]:>6.2f}%  最大回撤{v["mdd"]:>6.1f}%  '
                      f'在场{v["in_market"]:.1%}  交易{v["trades"]:>3}次')
        print(f'   {"完美择时上限":<14} 年化{blk["perfect"]["cagr"]:>6.1f}%')
        print(f'   20%规则: 牛{blk["swings"]["n_bull"]}段 熊{blk["swings"]["n_bear"]}段')
        print(f'   牛市段: {blk["swings"]["bulls"]}')
        print(f'   熊市段: {blk["swings"]["bears"]}')

    print("\n== E 美联储首次降息（N=5, 只给量级） ==")
    for r in res["E_fed_first_cut"]:
        print(f'  {r["fed_date"]} (交易日 {r["trade_date"]})  '
              f'沪深300 20/60/250日: {r.get("hs300_20d","-")}/{r.get("hs300_60d","-")}/{r.get("hs300_250d","-")}%  '
              f'创业板 20/60/250日: {r.get("gem_20d","-")}/{r.get("gem_60d","-")}/{r.get("gem_250d","-")}%')

    if args.write:
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {OUT}")
    if args.check:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        drift = 0.0
        for a, b in zip(prev["A_dd_buckets"], res["A_dd_buckets"]):
            for k in ("ex250_mean", "ex60_mean"):
                if k in a and k in b and a[k]:
                    drift = max(drift, abs(a[k] - b[k]) / max(abs(a[k]), 1e-9))
        print(f"\ncheck drift={drift:.4%}")
        raise SystemExit(0 if drift < 0.05 else 1)


if __name__ == "__main__":
    main()
