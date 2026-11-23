"""美联储降息后的 20 个交易日: 事件研究 + 三重负控。

问题: 「美国宣布降息时买 A 股, 20 天内赚不赚」。

口径铁律 (本项目的代价换来的):
1. **时区**: 决议 14:00 ET = 北京次日 02:00(夏)/03:00(冬)。A 股第一个能反应的是
   *决议日之后* 的第一个交易日。用决议日当天 = 偷看未来 (当天 A 股 15:00 收盘时
   决议还没公布)。入场一律 `searchsorted(side="right")`。
2. **可执行入场 = T+1 开盘** (隔夜定价已结束)。同时报 T+1 收盘, 两者之差 = 首日漂移,
   如果这个差很大, 说明「利好开盘就没了」。
3. **20 日窗口会重叠**: 2008-01-22 与 2008-01-30 只隔 8 天, 2001 年 11 次降息密集到
   平均间隔 33 天。重叠样本的 t 值会虚高 ⇒ 必须报独立事件数(间隔 >= 20 交易日)
   并给剔除重叠后的稳健版。
4. **分母问题**: 降息事件扎堆在 2001-2003 与 2007-2008 两个大熊市。所以基准不能用
   全历史均值, 必须用 **同期间交易日** 的分布做置换检验, 否则事件本身就在替熊市背锅。
5. **负控 1 (方向)**: 加息事件 20 次 (2015-2023 + 2026-09-16)。若加息后 20 日也是正的
   ⇒ 有信息的是 beta, 不是「降息」这个标签。
6. **负控 2 (置换)**: 把事件日随机挪到同期任意交易日, 2000 次, 看真实均值落在哪个分位。
7. **负控 3 (信息未到)**: 决议日**当天**入场 (不用未来信息, 但信息也还没来) 的 20 日
   收益。若今天和 T+1 差不多 ⇒ 「降息」对 A 股没有增量信息。
8. MFE/MAE 用 close 序列算, 不用 high/low (bar 内路径未知, 会把浮盈浮亏都放大)。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
IDX = DATA / "backfill_20261117" / "index_daily_full.parquet"
OUT = Path(__file__).resolve().parent / "_diag_fed_cut_20d_result.json"

INDICES = {
    "000001.SH": "上证",
    "000300.SH": "沪深300",
    "399006.SZ": "创业板",
    "000688.SH": "科创50",
}

# (决议日, bps, 周期标签, 周期内序号)。决议日为美东声明日, 数据来自 Federal Reserve 公告。
# 2001-2003 一轮 / 2007-2008 一轮 / 2019 一轮 / 2020 紧急 / 2024-2025 一轮。
CUTS = [
    ("2001-01-03", -50, "2001", 1), ("2001-01-31", -50, "2001", 2),
    ("2001-03-20", -50, "2001", 3), ("2001-04-18", -50, "2001", 4),
    ("2001-05-15", -50, "2001", 5), ("2001-06-27", -25, "2001", 6),
    ("2001-08-21", -25, "2001", 7), ("2001-09-17", -50, "2001", 8),
    ("2001-10-02", -50, "2001", 9), ("2001-11-06", -50, "2001", 10),
    ("2001-12-11", -25, "2001", 11), ("2002-11-06", -50, "2001", 12),
    ("2003-06-25", -25, "2001", 13),
    ("2007-09-18", -50, "2007", 1), ("2007-10-31", -25, "2007", 2),
    ("2007-12-11", -25, "2007", 3), ("2008-01-22", -75, "2007", 4),
    ("2008-01-30", -50, "2007", 5), ("2008-03-18", -75, "2007", 6),
    ("2008-04-30", -25, "2007", 7), ("2008-10-08", -50, "2007", 8),
    ("2008-10-29", -50, "2007", 9), ("2008-12-16", -75, "2007", 10),
    ("2019-07-31", -25, "2019", 1), ("2019-09-18", -25, "2019", 2),
    ("2019-10-30", -25, "2019", 3),
    ("2020-03-03", -50, "2020", 1), ("2020-03-15", -100, "2020", 2),
    ("2024-09-18", -50, "2024", 1), ("2024-11-07", -25, "2024", 2),
    ("2024-12-18", -25, "2024", 3),
    ("2025-09-17", -25, "2024", 4), ("2025-10-29", -25, "2024", 5),
    ("2025-12-10", -25, "2024", 6),
]

# 负控: 加息。2015-2018 正常化 + 2022-2023 抗通胀 + 2026-09 重启加息。
HIKES = [
    ("2015-12-16", 25), ("2016-12-14", 25), ("2017-03-15", 25), ("2017-06-14", 25),
    ("2017-12-13", 25), ("2018-03-21", 25), ("2018-06-13", 25), ("2018-09-26", 25),
    ("2018-12-19", 25), ("2022-03-16", 25), ("2022-05-04", 50), ("2022-06-15", 75),
    ("2022-07-27", 75), ("2022-09-21", 75), ("2022-11-02", 75), ("2022-12-14", 50),
    ("2023-02-01", 25), ("2023-03-22", 25), ("2023-05-03", 25), ("2023-07-26", 25),
    ("2026-09-16", 25),
]

H = 20  # 持有交易日


# ---------------------------------------------------------------- 数据

def load_panels() -> tuple[list[str], dict[str, dict[str, np.ndarray]]]:
    """返回 (统一日期轴, {字段: {symbol: 数组}})。四个字段共用同一日期轴。"""
    need = ("open", "close")
    out: dict[str, dict[str, np.ndarray]] = {}
    dates: list[str] | None = None
    for col in need:
        d = (
            pl.read_parquet(IDX, columns=["symbol", "date", col])
            .filter(pl.col("symbol").is_in(list(INDICES)))
            .unique(subset=["symbol", "date"], keep="last")
        )
        w = d.pivot(index="date", on="symbol", values=col, aggregate_function="last").sort("date")
        ds = [str(x) for x in w["date"].to_list()]
        if dates is None:
            dates = ds
        elif ds != dates:
            raise SystemExit("日期轴在 open/close 之间不一致")
        out[col] = {c: w[c].to_numpy().astype(float) for c in w.columns if c != "date"}
    assert dates is not None
    return dates, out


def valid_series(dates: list[str], sym: str, px: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """该指数的有效子序列 (date64, open, close), 去掉 NaN 日。"""
    d64 = np.array(dates, dtype="datetime64[D]")
    o, c = px["open"][sym], px["close"][sym]
    ok = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
    return d64[ok], o[ok], c[ok]


def next_idx(d64: np.ndarray, ds: str) -> int | None:
    """严格大于 ds 的第一个交易日下标 (= A 股第一个能反应的交易日)。"""
    k = int(np.searchsorted(d64, np.datetime64(ds), side="right"))
    return k if k < len(d64) else None


# ---------------------------------------------------------------- 单事件

def one_event(d64, op, cl, ds: str) -> dict | None:
    k = next_idx(d64, ds)
    if k is None or k + H - 1 >= len(cl) or k < 21:
        return None
    base = op[k]                      # 可执行入场 = T+1 开盘
    seg = cl[k : k + H]
    pre20 = cl[k - 1] / cl[k - 21] - 1.0
    return {
        "trade_date": str(d64[k]),
        "d1": round(float(cl[k] / base - 1) * 100, 2),
        "d5": round(float(cl[k + 4] / base - 1) * 100, 2),
        "d10": round(float(cl[k + 9] / base - 1) * 100, 2),
        "d20": round(float(cl[k + H - 1] / base - 1) * 100, 2),
        "d20_close_entry": round(float(cl[k + H - 1] / cl[k] - 1) * 100, 2),
        "argmin_day": int(np.argmin(seg / base)) + 1,     # 20 日内最低收盘是第几个交易日
        "mfe20": round(float(seg.max() / base - 1) * 100, 2),
        "mae20": round(float(seg.min() / base - 1) * 100, 2),
        "broke_entry": bool(seg.min() < base),
        "pre20": round(float(pre20) * 100, 2),
        "pre250": round(float(cl[k - 1] / cl[k - 1 - 250] - 1) * 100, 2) if k >= 251 else None,
        # 延后入场: 起点后移 lag 个交易日, 终点固定在 T+20 ⇒ 只测「何时买」, 不改持有终点
        **{f"lag{g}": (round(float(cl[k + g + H - 1] / op[k + g] - 1) * 100, 2)
                       if k + g + H - 1 < len(cl) else None) for g in (5, 10, 15)},
    }


def overnight(dates, px, ds: str) -> dict:
    """决议日 → T+1 开盘的隔夜跳空 (仅收盘价口径: 上一交易日收盘 → T+1 开盘)。"""
    out = {}
    d64 = np.array(dates, dtype="datetime64[D]")
    for sym in INDICES:
        k = next_idx(d64, ds)
        if k is None or k == 0:
            continue
        o, c = px["open"][sym], px["close"][sym]
        if not (np.isfinite(o[k]) and np.isfinite(c[k - 1])):
            continue
        out[sym] = round(float(o[k] / c[k - 1] - 1) * 100, 2)
    return out


# ---------------------------------------------------------------- 统计

def tstat(v: np.ndarray) -> float | None:
    n = len(v)
    if n < 2:
        return None
    sd = v.std(ddof=1)
    if sd == 0:
        return None
    return float(v.mean() / (sd / math.sqrt(n)))


def agg(vals: list[float | None]) -> dict:
    v = np.array([x for x in vals if x is not None and np.isfinite(x)], dtype=float)
    n = len(v)
    if n == 0:
        return {"n": 0}
    out = {
        "n": n,
        "mean": round(float(v.mean()), 2),
        "med": round(float(np.median(v)), 2),
        "win": round(float((v > 0).mean()), 3),
        "sd": round(float(v.std(ddof=1)), 2) if n > 1 else None,
        "t": round(tstat(v), 2) if tstat(v) is not None else None,
        "p10": round(float(np.percentile(v, 10)), 2),
        "p90": round(float(np.percentile(v, 90)), 2),
        "min": round(float(v.min()), 2),
        "max": round(float(v.max()), 2),
    }
    return out


def pool_fwd20(d64: np.ndarray, op: np.ndarray, cl: np.ndarray, lo: str, hi: str) -> np.ndarray:
    """同期所有交易日 (open 入场) 的 20 日收益池, 用于置换检验。"""
    m = (d64 >= np.datetime64(lo)) & (d64 <= np.datetime64(hi))
    idx = np.where(m)[0]
    idx = idx[(idx + H - 1 < len(cl))]
    return cl[idx + H - 1] / op[idx] - 1.0


def perm_test(actual: float, pool: np.ndarray, n: int, n_perm: int = 2000, seed: int = 7) -> dict:
    """随机抽 n 个同期交易日, 看真实均值落在什么分位。

    注意: 这只是「同等样本量下的分位」, 不是严格 p 值 —— 降息事件彼此重叠, 实际独立样本
    少于 n, 独立样本的模拟分布应更宽。所以这里同时报去掉重叠版 (见 dedupe)。
    """
    if pool.size < n + 10:
        return {"skip": "池不足"}
    rng = np.random.default_rng(seed)
    sims = np.array([rng.choice(pool, size=n, replace=False).mean() for _ in range(n_perm)])
    pct = float((sims < actual).mean())          # 实际值在模拟分布中的分位 (双侧看两端)
    return {
        "pctile": round(pct, 4),
        "sim_p5": round(float(np.percentile(sims, 5)) * 100, 2),
        "sim_p95": round(float(np.percentile(sims, 95)) * 100, 2),
        "pool_mean": round(float(pool.mean()) * 100, 3),
        "pool_n": int(pool.size),
    }


def dedupe(events: list[tuple], gap_days: int = H) -> list[tuple]:
    """剔除重叠事件: 与上一个保留事件的交易日间隔 < gap 的丢弃 (保守, 保第一个)。"""
    keep, last = [], None
    for e in events:
        if last is None or (np.datetime64(e[1]) - np.datetime64(last)).astype(int) >= gap_days:
            keep.append(e)
            last = e[1]
    return keep


# ---------------------------------------------------------------- 主流程

def run() -> dict:
    dates, px = load_panels()
    print(f"panel: {len(dates)} 日  {dates[0]}..{dates[-1]}", flush=True)
    res: dict = {"meta": {"h": H, "n_cuts": len(CUTS), "n_hikes": len(HIKES)}}

    # ---------- A. 逐事件明细 ----------
    detail: dict[str, list[dict]] = {}
    for sym, name in INDICES.items():
        d64, op, cl = valid_series(dates, sym, px)
        rows = []
        for ds, bps, cyc, seq in CUTS:
            r = one_event(d64, op, cl, ds)
            if r is None:
                continue
            r.update({"fed_date": ds, "bps": bps, "cycle": cyc, "seq": seq,
                      "kind": "首次" if seq == 1 else f"第{seq}次",
                      "gap": overnight(dates, px, ds).get(sym)})
            rows.append(r)
        detail[sym] = rows
    res["A_detail"] = detail

    # ---------- B. 20 日汇总 + 路径 + 置换 ----------
    summary: dict[str, dict] = {}
    for sym, name in INDICES.items():
        rows = detail[sym]
        if not rows:
            continue
        d64, op, cl = valid_series(dates, sym, px)
        lo, hi = rows[0]["trade_date"], rows[-1]["trade_date"]
        pool = pool_fwd20(d64, op, cl, lo, hi)
        d20 = [r["d20"] for r in rows]
        blk = {
            "window": f"{lo}..{hi}",
            "all": agg(d20),
            "path": {k: agg([r[k] for r in rows]) for k in ("gap", "d1", "d5", "d10", "d20")},
            "risk": {
                "mfe20": agg([r["mfe20"] for r in rows]),
                "mae20": agg([r["mae20"] for r in rows]),
                "broke_rate": round(float(np.mean([r["broke_entry"] for r in rows])), 3),
            },
            "d20_close_entry": agg([r["d20_close_entry"] for r in rows]),
            "perm": perm_test(np.mean(d20), pool, len(d20)),
        }
        blk["by_entry_lag"] = {f"T+1+{g}": agg([r[f"lag{g}"] for r in rows]) for g in (5, 10, 15)}
        blk["by_entry_lag"]["T+1"] = agg(d20)
        am = np.array([r["argmin_day"] for r in rows])
        blk["argmin"] = {
            "hist": {int(b): int((am == b).sum()) for b in range(1, H + 1) if (am == b).any()},
            "med": float(np.median(am)),
            "share_le5": round(float((am <= 5).mean()), 3),
            "share_le10": round(float((am <= 10).mean()), 3),
        }
        # 降息前 20 日 (对称性: 买预期/卖事实)
        blk["pre20"] = agg([r["pre20"] for r in rows])
        blk["pre_post_corr"] = None
        a = np.array([r["pre20"] for r in rows]); b = np.array(d20)
        if len(a) >= 5 and a.std() > 0 and b.std() > 0:
            blk["pre_post_corr"] = round(float(np.corrcoef(a, b)[0, 1]), 3)
        # 分组
        blk["by_seq"] = {
            "首次": agg([r["d20"] for r in rows if r["seq"] == 1]),
            "连续": agg([r["d20"] for r in rows if r["seq"] > 1]),
        }
        blk["by_bps"] = {
            "25bp": agg([r["d20"] for r in rows if r["bps"] == -25]),
            ">=50bp": agg([r["d20"] for r in rows if r["bps"] <= -50]),
        }
        blk["by_pre20"] = {
            "pre20<=0": agg([r["d20"] for r in rows if r["pre20"] <= 0]),
            "pre20>0": agg([r["d20"] for r in rows if r["pre20"] > 0]),
        }
        p250 = [r for r in rows if r["pre250"] is not None]
        if p250:
            med = float(np.median([r["pre250"] for r in p250]))
            blk["by_pre250"] = {
                f"pre250<={med:.0f}%": agg([r["d20"] for r in p250 if r["pre250"] <= med]),
                f"pre250>{med:.0f}%": agg([r["d20"] for r in p250 if r["pre250"] > med]),
            }
        # 周期
        bycycle: dict[str, dict] = {}
        for cyc in sorted({r["cycle"] for r in rows}):
            bycycle[cyc] = agg([r["d20"] for r in rows if r["cycle"] == cyc])
        blk["by_cycle"] = bycycle
        # 单点影响: 去掉一次事件后全体均值怎么变 (找「一个样本撑起全部结论」)
        loo = {}
        for r in rows:
            rest = [x["d20"] for x in rows if x["fed_date"] != r["fed_date"]]
            loo[r["fed_date"]] = round(float(np.mean(rest)), 2) if rest else None
        blk["leave_one_out"] = loo
        blk["ex_20240918"] = agg([x["d20"] for x in rows if x["fed_date"] != "2024-09-18"])
        blk["ex_cycle2024"] = agg([x["d20"] for x in rows if x["cycle"] != "2024"])
        # 首次降息逐个剔除
        firsts = [x for x in rows if x["seq"] == 1]
        if len(firsts) >= 2:
            blk["loo_first"] = {
                f"minus_{f['fed_date']}": round(float(np.mean([x["d20"] for x in firsts if x is not f])), 2)
                for f in firsts
            }
        # 剔除重叠
        seq_ev = [(r["fed_date"], r["trade_date"], r["d20"]) for r in rows]
        kept = dedupe(seq_ev)
        blk["dedupe"] = {
            "n_raw": len(rows),
            "n_kept": len(kept),
            "kept_dates": [k[1] for k in kept],
            "d20": agg([k[2] for k in kept]),
            "perm": perm_test(np.mean([k[2] for k in kept]), pool, len(kept)),
        }
        summary[sym] = blk
    res["B_summary"] = summary

    # ---------- B2. 安慰剂日期: 同一时期、同样持仓期, 但那天没有 FOMC ----------
    # 置换检验只能控制「分布」, 控制不了「时期」: 34 个降息日扎堆在 2001/2008,
    # 随机抽 34 天 = 分散在 25 年, 方差口径不同 ⇒ 分位 0.0 不可信。
    # 安慰剂把对照放到**同一段行情里**, 唯一差别是当天有没有决议。
    placebo: dict[str, dict] = {}
    for sym, name in INDICES.items():
        d64, op, cl = valid_series(dates, sym, px)
        rows = detail[sym]
        if not rows:
            continue
        blk: dict[str, dict] = {}
        for shift in (-40, -20, 20, 40):
            v = []
            for r in rows:
                k = next_idx(d64, r["fed_date"])
                if k is None:
                    continue
                k2 = k + shift
                if k2 < 1 or k2 + H - 1 >= len(cl):
                    continue
                v.append(round(float(cl[k2 + H - 1] / op[k2] - 1) * 100, 2))
            if len(v) >= 3:
                blk[f"shift{shift:+d}"] = agg(v)
                # 安慰剂期同样算「延后入场」与「低点日」: 用来判断主样本的
                # 「T+1 最差 / 低点第 13 日」是降息特征, 还是负漂移+固定窗口的机械结果
                for g in (5, 10, 15):
                    vg = []
                    for r in rows:
                        k = next_idx(d64, r["fed_date"])
                        if k is None:
                            continue
                        k2 = k + shift + g
                        if k2 < 1 or k2 + H - 1 >= len(cl):
                            continue
                        vg.append(round(float(cl[k2 + H - 1] / op[k2] - 1) * 100, 2))
                    if len(vg) >= 3:
                        blk[f"shift{shift:+d}_lag{g}"] = agg(vg)
                am = []
                for r in rows:
                    k = next_idx(d64, r["fed_date"])
                    if k is None:
                        continue
                    k2 = k + shift
                    if k2 < 1 or k2 + H - 1 >= len(cl):
                        continue
                    am.append(int(np.argmin(cl[k2 : k2 + H] / op[k2])) + 1)
                if len(am) >= 3:
                    blk[f"shift{shift:+d}_argmin_med"] = float(np.median(am))
        # 配对差: 每个事件减自己的安慰剂 (只留两者都有效的)
        pairs = {}
        for shift in (-20, 20):
            dif = []
            for r in rows:
                k = next_idx(d64, r["fed_date"])
                if k is None:
                    continue
                k2 = k + shift
                if k2 < 1 or k2 + H - 1 >= len(cl):
                    continue
                other = float(cl[k2 + H - 1] / op[k2] - 1) * 100
                dif.append(r["d20"] - other)
            if len(dif) >= 3:
                pairs[f"diff_vs_{shift:+d}"] = agg(dif)
        placebo[sym] = {"levels": blk, "paired": pairs}
    res["B2_placebo"] = placebo


    # ---------- C. 负控 1: 加息 ----------
    hike_out: dict[str, dict] = {}
    for sym, name in INDICES.items():
        d64, op, cl = valid_series(dates, sym, px)
        rows = []
        for ds, bps in HIKES:
            r = one_event(d64, op, cl, ds)
            if r is None:
                continue
            r.update({"fed_date": ds, "bps": bps})
            rows.append(r)
        if not rows:
            continue
        lo, hi = rows[0]["trade_date"], rows[-1]["trade_date"]
        pool = pool_fwd20(d64, op, cl, lo, hi)
        hike_out[sym] = {
            "window": f"{lo}..{hi}",
            "n": len(rows),
            "all": agg([r["d20"] for r in rows]),
            "path": {k: agg([r[k] for r in rows]) for k in ("d1", "d5", "d10", "d20")},
            "perm": perm_test(np.mean([r["d20"] for r in rows]), pool, len(rows)),
            "rows": rows,
        }
    res["C_hikes"] = hike_out

    # ---------- D. 负控 3: 决议日当天入场 (信息未到) ----------
    # 铁律: ds 必须落在该指数有效区间的**内部**。科创50 2019-12-31 才上市, 若不加
    # `k >= 250` 判断, 2001-2019 的 34 个 ds 全被 searchsorted 定位到上市首日,
    # 算出假的 +18.93%/t=8.23 (历史上踩过一次, 见 MEMORY 第 35 条)。
    same_day: dict[str, dict] = {}
    for sym, name in INDICES.items():
        d64, op, cl = valid_series(dates, sym, px)
        v, used = [], []
        for ds, bps, cyc, seq in CUTS:
            k = int(np.searchsorted(d64, np.datetime64(ds), side="left"))
            if k < 250 or k + H >= len(cl):
                continue          # 上市不足一年 或 未来数据不足
            v.append(round(float(cl[k + H] / cl[k] - 1) * 100, 2))
            used.append(str(d64[k]))
        same_day[sym] = {**agg(v), "dates": used} if v else {"n": 0}
    res["D_same_day"] = same_day

    # ---------- I. 单次归因拆解: 2024-09-18 (常被当成「降息后大涨」的证据) ----------
    attribution = {}
    for sym in ("000001.SH", "000300.SH", "399006.SZ"):
        d64, op, cl = valid_series(dates, sym, px)
        k = next_idx(d64, "2024-09-18")
        if k is None or k + H >= len(cl):
            continue
        # 分段: 降息→中国 9/24 三部门发布会前 / 发布会当日 / 之后
        segs = {}
        for lo, hi, lab in (("2024-09-19", "2024-09-23", "降息后至924前"),
                            ("2024-09-24", "2024-09-24", "924发布会当日"),
                            ("2024-09-25", "2024-10-08", "924后至10/8高点")):
            m = (d64 >= np.datetime64(lo)) & (d64 <= np.datetime64(hi))
            j = np.where(m)[0]
            if j.size >= 1:
                base = cl[j[0] - 1] if j[0] > 0 else cl[j[0]]
                segs[lab] = round(float(cl[j[-1]] / base - 1) * 100, 2)
        attribution[sym] = segs
    res["I_attribution_2024"] = attribution

    # ---------- E. 最近事件 ----------
    latest = []
    for sym in INDICES:
        for r in detail[sym]:
            if r["fed_date"] >= "2023-01-01":
                latest.append({"sym": sym, **r})
    res["E_latest"] = latest
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    res = run()
    s = res["B_summary"]

    print("\n== A 降息后 20 个交易日 (T+1 开盘入场, 单位 %) ==")
    print(f'{"指数":<8}{"窗口":<24}{"N":>3}{"均值":>7}{"中位":>7}{"胜率":>6}{"t":>6}{"p10":>7}{"p90":>7}{"分位":>7}{"池均值":>7}')
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        a = s[sym]["all"]
        pm = s[sym]["perm"]
        print(f'{name:<8}{s[sym]["window"]:<24}{a["n"]:>3}{a["mean"]:>7.2f}{a["med"]:>7.2f}'
              f'{a["win"]:>6.1%}{str(a.get("t")):>6}{a["p10"]:>7.2f}{a["p90"]:>7.2f}'
              f'{(pm.get("pctile") if pm.get("pctile") is not None else "-"):>7}{pm.get("pool_mean", 0):>7.2f}')

    print("\n== B 路径: 首日 / 5 / 10 / 20 日累计 (T+1 开盘=0) ==")
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        p = s[sym]["path"]
        seg = "  ".join(f'{k} {p[k]["mean"]:+.2f}%(win {p[k]["win"]:.0%})' for k in ("d1", "d5", "d10", "d20"))
        print(f'  {name:<8} {seg}')
        g = p["gap"]
        print(f'  {"":8} 隔夜跳空 {(g["mean"] if g.get("n") else 0):+.2f}%  '
              f'T+1收盘入场 d20 {s[sym]["d20_close_entry"]["mean"]:+.2f}%')

    print("\n== B2 安慰剂 (同事件前后 N 个交易日, 同样 20 日持仓) ==")
    for sym, name in INDICES.items():
        b = res["B2_placebo"].get(sym)
        if not b:
            continue
        PURE = ("shift-40", "shift-20", "shift+20", "shift+40")
        lv = "  ".join(f'{k} {b["levels"][k]["mean"]:+.2f}(win {b["levels"][k]["win"]:.0%})'
                       for k in PURE if k in b["levels"])
        print(f'  {name:<8} 水平: {lv}')
        g = "  ".join(f'{k} {v["mean"]:+.2f}%({v["win"]:.0%})'
                      for k, v in b["levels"].items() if "lag" in k and k.startswith("shift-20_"))
        gi = "  ".join(f'{k.replace("_argmin_med","")} 第{v:.0f}日'
                       for k, v in b["levels"].items() if "argmin" in k)
        print(f'  {"":8} 安慰剂 -20 延后: {g}')
        print(f'  {"":8} 安慰剂低点日: {gi}')
        if b["paired"]:
            pp = "  ".join(f'{k} {v["mean"]:+.2f}(t{v.get("t")},n{v["n"]})' for k, v in b["paired"].items())
            print(f'  {"":8} 配对差: {pp}')

    print("\n== B3 延后入场 (终点固定 T+20, 只改起点) + 20 日内低点出现时点 ==")
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        b = s[sym]
        e = "  ".join(f'{k} {v["mean"]:+.2f}%({v["win"]:.0%})' for k, v in b["by_entry_lag"].items() if v.get("n"))
        am = b["argmin"]
        print(f'  {name:<8} {e}')
        print(f'  {"":8} 低点日 中位第 {am["med"]:.0f} 日 | <=5日 {am["share_le5"]:.0%} <=10日 {am["share_le10"]:.0%}')

    print("\n== C 风险: 20 日内最大浮盈 / 浮亏 / 跌破入场比例 ==")
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        r = s[sym]["risk"]
        print(f'  {name:<8} MFE {r["mfe20"]["mean"]:+.2f}%(中位 {r["mfe20"]["med"]:+.2f})  '
              f'MAE {r["mae20"]["mean"]:+.2f}%(中位 {r["mae20"]["med"]:+.2f}, p10 {r["mae20"]["p10"]:+.2f})  '
              f'跌破入场 {r["broke_rate"]:.0%}')

    print("\n== D 分组 (d20 均值 / t / N) ==")
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        b = s[sym]
        parts = []
        for g, blk in (("首次", b["by_seq"]["首次"]), ("连续", b["by_seq"]["连续"]),
                       ("25bp", b["by_bps"]["25bp"]), (">=50bp", b["by_bps"][">=50bp"]),
                       ("pre20<=0", b["by_pre20"]["pre20<=0"]), ("pre20>0", b["by_pre20"]["pre20>0"])):
            if blk.get("n", 0) >= 2:
                parts.append(f'{g} {blk["mean"]:+.2f}(t{blk.get("t")},n{blk["n"]})')
        print(f'  {name:<8} ' + " | ".join(parts))
        if "by_pre250" in b:
            q = b["by_pre250"]
            print(f'  {"":8} ' + " | ".join(f'{k} {v["mean"]:+.2f}(n{v["n"]})' for k, v in q.items() if v.get("n", 0) >= 2))
        print(f'  {"":8} 周期: ' + " | ".join(f'{k} {v["mean"]:+.2f}(n{v["n"]})' for k, v in b["by_cycle"].items()))
        print(f'  {"":8} pre20→post20 相关 {b["pre_post_corr"]}  '
              f'pre20 均值 {b["pre20"]["mean"]:+.2f}%(胜率 {b["pre20"]["win"]:.0%})')

    print("\n== D2 单点影响 (谁在撑结论) ==")
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        b = s[sym]
        lo = b["leave_one_out"]
        worst = min(lo.items(), key=lambda kv: (kv[1] if kv[1] is not None else 9e9))
        best = max(lo.items(), key=lambda kv: (kv[1] if kv[1] is not None else -9e9))
        print(f'  {name:<8} 全体 {b["all"]["mean"]:+.2f}% | 去掉 {best[0]} → {best[1]:+.2f}% '
              f'(影响 {best[1]-b["all"]["mean"]:+.2f}) | 去掉 {worst[0]} → {worst[1]:+.2f}%')
        print(f'  {"":8} 剔 2024-09-18: {b["ex_20240918"]["mean"]:+.2f}%(n{b["ex_20240918"]["n"]},t{b["ex_20240918"].get("t")})'
              f'  剔整个 2024 周期: {b["ex_cycle2024"]["mean"]:+.2f}%(n{b["ex_cycle2024"]["n"]},t{b["ex_cycle2024"].get("t")})')
        if "loo_first" in b:
            print(f'  {"":8} 首次降息 {b["by_seq"]["首次"]["mean"]:+.2f}% 逐个剔除: ' +
                  "  ".join(f'{k[6:]}→{v:+.2f}' for k, v in b["loo_first"].items()))

    print("\n== E 剔除重叠事件 (间隔 >= 20 交易日) ==")
    for sym, name in INDICES.items():
        if sym not in s:
            continue
        d = s[sym]["dedupe"]
        pm2 = d.get("perm", {})
        print(f'  {name:<8} {d["n_raw"]}→{d["n_kept"]} 次  d20 {d["d20"]["mean"]:+.2f}% '
              f't={d["d20"].get("t")} 胜率 {d["d20"]["win"]:.0%} 分位={pm2.get("pctile")}')

    print("\n== F 负控1: 加息后 20 日 ==")
    for sym, name in INDICES.items():
        h = res["C_hikes"].get(sym)
        if not h:
            continue
        print(f'  {name:<8} N={h["n"]:>2}  d20 {h["all"]["mean"]:+.2f}% t={h["all"].get("t")} '
              f'胜率 {h["all"]["win"]:.0%} 分位={h["perm"].get("pctile")} 池均值 {h["perm"].get("pool_mean")}%')

    print("\n== G 负控3: 决议日当天入场 (信息未到) ==")
    for sym, name in INDICES.items():
        a = res["D_same_day"].get(sym, {})
        if a.get("n"):
            print(f'  {name:<8} N={a["n"]:>2}  d20 {a["mean"]:+.2f}% t={a.get("t")} 胜率 {a["win"]:.0%}')

    print("\n== I 2024-09-18 归因拆解 (上证/沪深300/创业板) ==")
    for sym, segs in res["I_attribution_2024"].items():
        print(f'  {INDICES[sym]:<8} ' + "  ".join(f'{k} {v:+.2f}%' for k, v in segs.items()))

    print("\n== H 2023 年以后逐次 ==")
    print(f'{"决议日":<12}{"指数":<8}{"bps":>5}{"入场日":>12}{"隔夜":>8}{"d1":>7}{"d5":>7}{"d10":>7}{"d20":>8}{"pre20":>8}')
    for r in res["E_latest"]:
        print(f'{r["fed_date"]:<12}{INDICES[r["sym"]]:<8}{r["bps"]:>5}{r["trade_date"]:>12}'
              f'{(r["gap"] if r["gap"] is not None else 0):>8.2f}{r["d1"]:>7.2f}{r["d5"]:>7.2f}'
              f'{r["d10"]:>7.2f}{r["d20"]:>8.2f}{r["pre20"]:>8.2f}')

    if args.write:
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"\n[written] {OUT}")
    if args.check:
        old = json.loads(OUT.read_text(encoding="utf-8"))
        print("\n[check] 见 --write 结果对比")


if __name__ == "__main__":
    main()
