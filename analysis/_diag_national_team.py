"""把上证指数当股票、国家队当主力：这套操盘论能不能被数据支持。

三个可检验命题
  P1 主力可观测：大额申购会在 ETF 上留下溢价指纹（ETF 涨幅 > 跟踪指数涨幅）
  P2 相对低点可利用：指纹出现后的收益显著高于普通大跌日
  P3 行业周期可择时：行业指数存在可交易的中期动量/反转

口径要点
  - ETF 相对指数的日超额 = ETF 日收益 − 跟踪指数日收益。ETF 分红/费率会造成长期漂移，
    但日频上分红日是稀疏负跳，用中位数与正比例读，不用均值单点。
  - 「大跌日」按上证指数日收益分组，所有分组都报，避免只挑有利的桶。
  - 结果变量一律从**次日收盘**起算（当日收盘才知道信号）。
用法  python analysis/_diag_national_team.py [--write|--check]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
IDX = DATA / "backfill_20261117" / "index_daily_full.parquet"
ETF = DATA / "backfill_20261117" / "etf_daily_full.parquet"
OUT_JSON = ROOT / "analysis" / "_diag_national_team_result.json"

SH = "000001.SH"

# 主力宽基（汇金系公开持仓集中的品种）+ 一个负控（行业 ETF，汇金基本不买）
PAIRS = [
    ("510300.SH", "000300.SH", "沪深300ETF"),
    ("510050.SH", "000016.SH", "上证50ETF"),
    ("510500.SH", "000905.SH", "中证500ETF"),
    ("159915.SZ", "399006.SZ", "创业板ETF"),
    ("588000.SH", "000688.SH", "科创50ETF"),
    ("512480.SH", "000688.SH", "半导体ETF(负控:错绑科创50,只看相对自身)"),
]
# 负控单独处理：半导体 ETF 对应指数在本地没有，改用「与上证的超额」只做方向比较
NEG_CTRL = ("512480.SH", "半导体ETF")

# 公开可查的国家队入场/表态日（收盘后或盘中公告，次日才可交易）
POLICY_DAYS = [
    ("2008-09-18", "汇金宣布增持三大行"),
    ("2011-10-10", "汇金增持四大行"),
    ("2012-10-10", "汇金增持四大行"),
    ("2013-06-20", "汇金首持ETF(147亿)"),
    ("2015-07-08", "证金公司入场"),
    ("2023-10-11", "汇金增持四大行+ETF"),
    ("2024-02-06", "汇金扩大ETF增持"),
    ("2024-09-24", "一揽子政策"),
    ("2025-04-07", "汇金明确类平准基金定位、增持ETF"),
]


def load_series(syms: list[str]) -> dict[str, pl.DataFrame]:
    """ETF 与指数在两个文件里, 分别扫描后合并。返回 {symbol: DataFrame(date, close, amount)}。"""
    want = set(syms)
    out: dict[str, pl.DataFrame] = {}
    for path in (IDX, ETF):
        df = (
            pl.scan_parquet(path)
            .filter(pl.col("symbol").is_in(list(want)))
            .select(["symbol", "date", "close", "amount"])
            .collect()
        )
        for s in df["symbol"].unique().to_list():
            if s in out:
                continue
            d = df.filter(pl.col("symbol") == s).select(["date", "close", "amount"]).sort("date")
            d = d.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
            out[s] = d
    return out


def align(base: pl.DataFrame, other: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """以 base 的日期为轴左连, 缺失保留 null。"""
    return base.join(other.select(["date"] + cols), on="date", how="left")


def to_np(df: pl.DataFrame, col: str) -> np.ndarray:
    return df[col].to_numpy().astype(float)


def fwd_ret(px: np.ndarray, i0: int, h: int) -> float:
    """从 i0+1 收盘到 i0+1+h 收盘的收益（次日收盘起算，避免用当日不可知信息）。"""
    a = i0 + 1
    b = i0 + 1 + h
    if b >= len(px):
        return np.nan
    if not (np.isfinite(px[a]) and np.isfinite(px[b])) or px[a] <= 0:
        return np.nan
    return px[b] / px[a] - 1.0


def bucket_stats(vals: np.ndarray, base_mask: np.ndarray) -> dict:
    v = vals[np.isfinite(vals)]
    if v.size < 30:
        return {"n": int(v.size)}
    se = v.std(ddof=1) / math.sqrt(v.size)
    lo, hi = v.mean() - 1.96 * se, v.mean() + 1.96 * se
    base = vals[base_mask]
    base = base[np.isfinite(base)]
    return {
        "n": int(v.size),
        "mean_bp": round(float(v.mean()) * 1e4, 1),
        "med_bp": round(float(np.median(v)) * 1e4, 1),
        "t": round(float(v.mean() / se), 2) if se > 0 else None,
        "ci95_bp": [round(float(lo) * 1e4, 1), round(float(hi) * 1e4, 1)],
        "pos_frac": round(float((v > 0).mean()), 3),
        "base_mean_bp": round(float(base.mean()) * 1e4, 1) if base.size else None,
    }


def part_a_premium_fingerprint(series: dict[str, pl.DataFrame]) -> dict:
    """P1: 大跌日 ETF 是否相对跟踪指数走强（= 申购溢价指纹）。"""
    sh = series[SH].sort("date")
    sh = sh.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("r_sh"))
    out = {"pairs": {}, "note": "excess = ETF 日收益 − 跟踪指数日收益；分红日为稀疏负跳, 读中位与正比例"}

    for etf_sym, idx_sym, label in PAIRS:
        if etf_sym not in series or idx_sym not in series:
            continue
        e = series[etf_sym].sort("date").rename({"close": "c_etf"})
        i = series[idx_sym].sort("date").rename({"close": "c_idx"})
        j = sh.select(["date", "r_sh"]).join(e, on="date", how="inner").join(i, on="date", how="inner")
        j = j.filter(pl.col("c_etf").is_not_null() & pl.col("c_idx").is_not_null()).sort("date")
        if j.height < 300:
            out["pairs"][label] = {"n": j.height, "skip": "样本不足"}
            continue
        r_etf = to_np(j, "c_etf")
        r_idx = to_np(j, "c_idx")
        ex = np.full(len(r_etf), np.nan)
        ex[1:] = (r_etf[1:] / r_etf[:-1] - 1.0) - (r_idx[1:] / r_idx[:-1] - 1.0)
        rs = to_np(j, "r_sh")
        finite = np.isfinite(rs) & np.isfinite(ex)
        allmask = finite.copy()

        rows = {}
        for name, lo, hi in [
            ("大跌<=-1.5%", -1.0, -0.015),
            ("跌-1.5~-0.5%", -0.015, -0.005),
            ("平-0.5~0.5%", -0.005, 0.005),
            ("涨0.5~1.5%", 0.005, 0.015),
            ("大涨>=1.5%", 0.015, 1.0),
        ]:
            m = finite & (rs > lo) & (rs <= hi)
            rows[name] = bucket_stats(ex, allmask) | {"n_group": int(m.sum())} if m.sum() >= 30 else {"n_group": int(m.sum())}
            if m.sum() >= 30:
                v = ex[m]
                v = v[np.isfinite(v)]
                se = v.std(ddof=1) / math.sqrt(v.size)
                rows[name] = {
                    "n_group": int(m.sum()),
                    "excess_mean_bp": round(float(v.mean()) * 1e4, 1),
                    "excess_med_bp": round(float(np.median(v)) * 1e4, 1),
                    "t_vs_zero": round(float(v.mean() / se), 2) if se > 0 else None,
                    "pos_frac": round(float((v > 0).mean()), 3),
                }
        out["pairs"][label] = {
            "n_days": j.height,
            "start": str(j["date"][0]),
            "excess_all_mean_bp": round(float(np.nanmean(ex)) * 1e4, 2),
            "excess_all_med_bp": round(float(np.nanmedian(ex)) * 1e4, 2),
            "by_sh_return": rows,
        }

    # 负控: 行业 ETF 与上证的超额, 看方向是否与宽基相反
    if NEG_CTRL[0] in series:
        e = series[NEG_CTRL[0]].sort("date").rename({"close": "c"})
        j = sh.select(["date", "r_sh"]).join(e, on="date", how="inner").sort("date")
        c = to_np(j, "c")
        ex = np.full(len(c), np.nan)
        ex[1:] = (c[1:] / c[:-1] - 1.0) - to_np(j, "r_sh")[1:]
        rs = to_np(j, "r_sh")
        m = np.isfinite(ex) & np.isfinite(rs) & (rs <= -0.015)
        v = ex[m]
        out["neg_ctrl_semiconductor"] = {
            "label": NEG_CTRL[1],
            "n_days": j.height,
            "excess_on_big_down_days_mean_bp": round(float(np.nanmean(v)) * 1e4, 1),
            "pos_frac": round(float((v > 0).mean()), 3),
            "note": "行业 ETF 与上证超额本身含行业风格, 只作方向参照",
        }
    return out


def part_a2_cross_check(series: dict[str, pl.DataFrame]) -> dict:
    """A 的负控：同指数三只 ETF 的超额若高度同向, 则它是『指数 vs ETF 收盘口径』的公共成分,
    不是某只 ETF 被大额申购。只有扣掉公共成分后仍显著, 才算资金痕迹。"""
    etfs = ["510300.SH", "159919.SZ", "510330.SH"]
    base = series["000300.SH"].sort("date").select(["date", "close"]).rename({"close": "c_idx"})
    sh = series[SH].sort("date").select(["date", "close"]).rename({"close": "c_sh"})
    sh = sh.with_columns((pl.col("c_sh") / pl.col("c_sh").shift(1) - 1.0).alias("r_sh"))
    base = base.join(sh.select(["date", "r_sh"]), on="date", how="left")
    for s in etfs:
        d = series[s].sort("date").select(["date", "close"]).rename({"close": f"c_{s}"})
        base = base.join(d, on="date", how="left")
    base = base.drop_nulls().sort("date")
    if base.height < 500:
        return {"skip": f"共同日数仅 {base.height}"}
    c_idx = to_np(base, "c_idx")
    ex = {}
    for s in etfs:
        c = to_np(base, f"c_{s}")
        x = np.full(len(c), np.nan)
        x[1:] = (c[1:] / c[:-1] - 1.0) - (c_idx[1:] / c_idx[:-1] - 1.0)
        ex[s] = x
    rs = to_np(base, "r_sh")
    M = np.column_stack([ex[s] for s in etfs])
    common = np.nanmean(M, axis=1)

    corr = {}
    for i in range(len(etfs)):
        for k in range(i + 1, len(etfs)):
            m = np.isfinite(M[:, i]) & np.isfinite(M[:, k])
            if m.sum() > 100:
                corr[f"{etfs[i]}_vs_{etfs[k]}"] = round(float(np.corrcoef(M[m, i], M[m, k])[0, 1]), 3)

    big = np.isfinite(rs) & (rs <= -0.015)
    rows = []
    for i, s in enumerate(etfs):
        tot = M[:, i][big]
        tot = tot[np.isfinite(tot)]
        own = (M[:, i] - common)[big]
        own = own[np.isfinite(own)]
        se = own.std(ddof=1) / math.sqrt(own.size) if own.size > 10 else float("nan")
        rows.append({
            "etf": s,
            "n_big_down": int(tot.size),
            "excess_total_bp": round(float(tot.mean()) * 1e4, 1) if tot.size else None,
            "excess_total_med_bp": round(float(np.median(tot)) * 1e4, 1) if tot.size else None,
            "excess_own_bp": round(float(own.mean()) * 1e4, 1) if own.size else None,
            "excess_own_t": round(float(own.mean() / se), 2) if np.isfinite(se) and se > 0 else None,
            "excess_own_pos_frac": round(float((own > 0).mean()), 3) if own.size else None,
        })
    return {
        "n_days": base.height,
        "span": [str(base["date"][0]), str(base["date"][-1])],
        "corr_same_index_etfs": corr,
        "big_down": rows,
        "verdict": "特有超额=总超额−三只均值；若 t 不显著, 则溢价指纹是指数口径的公共成分, 不能当作申购痕迹",
    }


def part_b_signal_tradability(series: dict[str, pl.DataFrame]) -> dict:
    """P2: 用『大跌 + ETF 溢价为正』当信号, 之后买宽基, 收益是否优于普通大跌日。"""
    sh = series[SH].sort("date")
    sh = sh.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("r_sh"))
    e = series["510300.SH"].sort("date").rename({"close": "c_etf"})
    i = series["000300.SH"].sort("date").rename({"close": "c_idx"})
    j = sh.select(["date", "r_sh"]).join(e, on="date", how="inner").join(i, on="date", how="inner")
    j = j.filter(pl.col("c_etf").is_not_null() & pl.col("c_idx").is_not_null()).sort("date")

    # 目标：从 000300 自身价格读结果（避开 ETF 跟踪误差）
    px300 = to_np(j, "c_idx")
    px_sh = to_np(series[SH].sort("date"), "close")
    c_etf = to_np(j, "c_etf")
    c_idx = to_np(j, "c_idx")
    rs = to_np(j, "r_sh")
    ex = np.full(len(c_etf), np.nan)
    ex[1:] = (c_etf[1:] / c_etf[:-1] - 1.0) - (c_idx[1:] / c_idx[:-1] - 1.0)

    # 5 日累计超额更稳健（单日 ETF 收盘价噪声大）
    ex5 = np.full(len(ex), np.nan)
    for k in range(len(ex)):
        w = ex[max(0, k - 4) : k + 1]
        if np.isfinite(w).sum() >= 3:
            ex5[k] = np.nansum(w)

    rows = []
    for h in (20, 60, 250):
        fwd = np.array([fwd_ret(px300, k, h) for k in range(len(px300))])
        groups = {
            "全样本": np.ones(len(px300), bool),
            "大跌<-1.5%": rs <= -0.015,
            "大跌 且 5日超额>0": (rs <= -0.015) & (ex5 > 0),
            "大跌 且 5日超额<=0": (rs <= -0.015) & (ex5 <= 0),
            "大跌 且 当日超额>+0.30%": (rs <= -0.015) & (ex > 0.003),
            "非大跌 且 5日超额>0": (rs > -0.015) & (ex5 > 0),
        }
        for name, m in groups.items():
            v = fwd[m & np.isfinite(fwd)]
            if v.size < 20:
                rows.append({"h": h, "group": name, "n": int(v.size)})
                continue
            se = v.std(ddof=1) / math.sqrt(v.size)
            rows.append({
                "h": h,
                "group": name,
                "n": int(v.size),
                "mean_pct": round(float(v.mean()) * 100, 2),
                "med_pct": round(float(np.median(v)) * 100, 2),
                "win": round(float((v > 0).mean()), 3),
                "t": round(float(v.mean() / se), 2) if se > 0 else None,
                "ci95_pct": [round(float((v.mean() - 1.96 * se)) * 100, 2), round(float((v.mean() + 1.96 * se)) * 100, 2)],
            })
    return {"by_horizon": rows, "n_days": int(len(px300))}


def part_c_signal_mdd(series: dict[str, pl.DataFrame]) -> dict:
    """大跌+溢价信号进场后, 250 日内的最大回撤（托底论的真正考验：会不会被套）。"""
    sh = series[SH].sort("date")
    sh = sh.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("r_sh"))
    e = series["510300.SH"].sort("date").rename({"close": "c_etf"})
    i = series["000300.SH"].sort("date").rename({"close": "c_idx"})
    j = sh.select(["date", "r_sh"]).join(e, on="date", how="inner").join(i, on="date", how="inner")
    j = j.filter(pl.col("c_etf").is_not_null() & pl.col("c_idx").is_not_null()).sort("date")
    c_etf, c_idx = to_np(j, "c_etf"), to_np(j, "c_idx")
    rs = to_np(j, "r_sh")
    ex = np.full(len(c_etf), np.nan)
    ex[1:] = (c_etf[1:] / c_etf[:-1] - 1.0) - (c_idx[1:] / c_idx[:-1] - 1.0)
    ex5 = np.full(len(ex), np.nan)
    for k in range(len(ex)):
        w = ex[max(0, k - 4) : k + 1]
        if np.isfinite(w).sum() >= 3:
            ex5[k] = np.nansum(w)

    def mdd_after(idx_list, horizon=250):
        res = []
        for k in idx_list:
            t0 = k + 1
            t1 = min(k + 1 + horizon, len(c_idx))
            if t0 >= len(c_idx) or t1 - t0 < 20:
                continue
            seg = c_idx[t0:t1]
            if not np.isfinite(seg[0]) or seg[0] <= 0:
                continue
            run = np.fmax.accumulate(seg)
            res.append(float(np.nanmin(seg / run - 1.0)))
        return res

    sig = np.where((rs <= -0.015) & (ex5 > 0))[0]
    plain = np.where(rs <= -0.015)[0]
    # 去重：信号日按 20 日间隔抽稀（避免同一段连续触发被当成多次独立样本）
    def thin(x, gap=20):
        out = []
        last = -10**9
        for k in x:
            if k - last >= gap:
                out.append(int(k))
                last = int(k)
        return out

    a, b = thin(sig), thin(plain)
    va, vb = mdd_after(a), mdd_after(b)
    return {
        "n_signal_thinned": len(a),
        "n_plain_down_thinned": len(b),
        "signal_mdd_med": round(float(np.median(va)) * 100, 2) if va else None,
        "signal_mdd_p10": round(float(np.percentile(va, 10)) * 100, 2) if va else None,
        "plain_mdd_med": round(float(np.median(vb)) * 100, 2) if vb else None,
        "plain_mdd_p10": round(float(np.percentile(vb, 10)) * 100, 2) if vb else None,
        "signal_dates": [str(j["date"][k]) for k in a],
    }


def part_d_policy_days(series: dict[str, pl.DataFrame]) -> dict:
    """政策日之后：买宽基拿 250 日的收益与最深浮亏（是否跌破当日收盘）。"""
    px = {}
    for s in ("000300.SH", "399006.SZ", "000688.SH", SH):
        if s in series:
            d = series[s].sort("date")
            px[s] = (d["date"].to_numpy(), to_np(d, "close"))
    rows = []
    for ds, label in POLICY_DAYS:
        row = {"date": ds, "event": label}
        for s, (dates, v) in px.items():
            d64 = dates.astype("datetime64[D]")
            ev = np.datetime64(ds)
            # 事件日必须落在该指数有效区间内, 否则会取到"上市首日"当日并静默造出假收益
            if d64[0] > ev + np.timedelta64(30, "D") or d64[-1] < ev:
                row[s] = None
                continue
            idx = np.where(d64 >= ev)[0]
            if idx.size == 0:
                row[s] = None
                continue
            k = int(idx[0])
            t0 = k + 1
            t1 = min(k + 1 + 250, len(v))
            seg = v[t0:t1]
            seg = seg[np.isfinite(seg)]
            if seg.size < 20:
                row[s] = None
                continue
            row[s] = {
                "fwd250_pct": round(float(seg[-1] / seg[0] - 1.0) * 100, 1),
                "mdd_pct": round(float(np.min(seg / np.fmax.accumulate(seg) - 1.0)) * 100, 1),
                "broke_entry": bool(np.min(seg) < seg[0]),
            }
        rows.append(row)
    return {"rows": rows, "note": "次日收盘起算 250 日；broke_entry = 期间最低是否低于入场价"}


def part_e_industry_cycle(series: dict[str, pl.DataFrame]) -> dict:
    """P3: 行业指数能否靠中期动量/反转择时（决定『按行业周期止盈』可不可能）。"""
    # 去重后的行业集合：原表里 地产×2 / 医药×3 / 煤炭×2 / 证券×2，横截面不独立
    INDUSTRY = {
        "000006.SH": "地产", "000949.SH": "农业", "399810.SZ": "传媒", "399811.SZ": "电子",
        "399975.SZ": "证券", "399358.SZ": "环保", "399998.SZ": "煤炭", "399997.SZ": "白酒",
        "399368.SZ": "军工", "000933.SH": "医药", "399395.SZ": "有色",
    }
    have = {s: n for s, n in INDUSTRY.items() if s in series and series[s].height > 1500}
    if len(have) < 4:
        return {"skip": "行业指数不足"}
    # 统一日期轴
    base = series[SH].sort("date").select(["date"])
    px = {}
    for s in have:
        d = series[s].sort("date").select(["date", "close"]).rename({"close": s})
        base = base.join(d, on="date", how="left")
    dates = base["date"].to_numpy()
    M = np.column_stack([to_np(base, s) for s in have])
    R = np.full_like(M, np.nan)
    R[1:] = M[1:] / M[:-1] - 1.0

    def cum(k: int) -> np.ndarray:
        """过去 k 日累计收益（含当日），忽略 NaN。"""
        out = np.full_like(M, np.nan)
        for t in range(k, len(M)):
            w = M[t - k : t + 1]
            ok = np.isfinite(w).all(axis=0)
            if ok.sum() == 0:
                continue
            out[t, ok] = w[-1, ok] / w[0, ok] - 1.0
        return out

    rows = []
    for look, hold in ((20, 20), (60, 60), (120, 60), (250, 120)):
        past = cum(look)
        fwd = np.full_like(M, np.nan)
        for t in range(len(M) - hold):
            fwd[t] = M[t + hold] / M[t] - 1.0
        # 逐日横截面：按过去收益分高/低两半
        diffs, hi_abs, lo_abs = [], [], []
        for t in range(look, len(M) - hold):
            p, f = past[t], fwd[t]
            ok = np.isfinite(p) & np.isfinite(f)
            if ok.sum() < 4:
                continue
            pp, ff = p[ok], f[ok]
            hi = ff[pp >= np.median(pp)]
            lo = ff[pp < np.median(pp)]
            diffs.append(hi.mean() - lo.mean())
            hi_abs.append(hi.mean())
            lo_abs.append(lo.mean())
        d = np.array(diffs)
        d = d[np.isfinite(d)]
        if d.size < 50:
            rows.append({"look": look, "hold": hold, "n": int(d.size)})
            continue
        se = d.std(ddof=1) / math.sqrt(d.size)
        # 持有期重叠 ⇒ 日序列不独立。按 hold 间隔抽稀成不重叠期, 重算 t（这个才是可信的）
        thin = d[::hold]
        se_i = thin.std(ddof=1) / math.sqrt(thin.size) if thin.size > 5 else float("nan")
        ha, la = np.array(hi_abs), np.array(lo_abs)
        per_year = 244.0 / hold
        all_ann = ((1 + float(np.mean(np.concatenate([ha, la])))) ** per_year - 1) * 100
        rows.append({
            "look": look,
            "hold": hold,
            "n": int(d.size),
            "n_indep": int(thin.size),
            "hi_minus_lo_pct": round(float(d.mean()) * 100, 3),
            "t_overlap": round(float(d.mean() / se), 2) if se > 0 else None,
            "t_indep": round(float(thin.mean() / se_i), 2) if np.isfinite(se_i) and se_i > 0 else None,
            "ci95_indep_pct": [
                round(float(thin.mean() - 1.96 * se_i) * 100, 3),
                round(float(thin.mean() + 1.96 * se_i) * 100, 3),
            ] if np.isfinite(se_i) and se_i > 0 else None,
            "hi_abs_pct": round(float(ha.mean()) * 100, 3),
            "lo_abs_pct": round(float(la.mean()) * 100, 3),
            "hi_ann_pct": round(((1 + float(ha.mean())) ** per_year - 1) * 100, 2),
            "lo_ann_pct": round(((1 + float(la.mean())) ** per_year - 1) * 100, 2),
            "all_ann_pct": round(all_ann, 2),
            "note": "过去收益高的一半 − 低的一半 的未来 hold 日收益差；正=动量。t_overlap 被重叠样本高估, 以 t_indep 为准",
        })
    return {"n_industry": len(have), "names": list(have.values()), "grid": rows}


def main() -> int:
    syms = {SH, "000300.SH", "000016.SH", "000905.SH", "399006.SZ", "000688.SH"}
    syms |= {e for e, _, _ in PAIRS} | {NEG_CTRL[0]}
    syms |= {"159919.SZ", "510330.SH"}  # A2 交叉验证用的同指数 ETF
    syms |= {
        "000006.SH", "000949.SH", "399810.SZ", "399811.SZ", "399707.SZ", "399913.SZ",
        "399358.SZ", "399998.SZ", "399997.SZ", "000037.SH", "399368.SZ", "000933.SH",
        "399395.SZ", "399975.SZ", "399436.SZ", "399367.SZ",
    }
    print(f"loading {len(syms)} symbols ...", flush=True)
    series = load_series(sorted(syms))
    missing = [s for s in syms if series.get(s) is None or series[s].height == 0]
    if missing:
        print("missing:", missing, flush=True)

    res = {
        "generated": "2026-11-21",
        "A_premium_fingerprint": part_a_premium_fingerprint(series),
        "A2_cross_check": part_a2_cross_check(series),
        "B_signal_tradability": part_b_signal_tradability(series),
        "C_signal_mdd": part_c_signal_mdd(series),
        "D_policy_days": part_d_policy_days(series),
        "E_industry_cycle": part_e_industry_cycle(series),
    }

    if "--write" in sys.argv:
        OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"written {OUT_JSON}", flush=True)
        return 0
    if "--check" in sys.argv:
        if not OUT_JSON.exists():
            print("baseline missing, run --write first")
            return 1
        old = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        bad = []
        for k in ("A_premium_fingerprint", "A2_cross_check", "B_signal_tradability", "C_signal_mdd", "E_industry_cycle"):
            a, b = json.dumps(old.get(k), sort_keys=True), json.dumps(res.get(k), sort_keys=True)
            if a != b:
                bad.append(k)
        for k in bad:
            print(f"DRIFT {k}")
        print("check", "FAIL" if bad else "OK")
        return 1 if bad else 0

    print("\n== A 大跌日 ETF 相对跟踪指数的超额（溢价指纹） ==")
    for label, blk in res["A_premium_fingerprint"]["pairs"].items():
        if "by_sh_return" not in blk:
            print(f"{label}: {blk}")
            continue
        print(f"\n-- {label}  n={blk['n_days']} {blk['start']}..  全样本中位 {blk['excess_all_med_bp']}bp")
        for g, v in blk["by_sh_return"].items():
            if "excess_mean_bp" not in v:
                print(f"   {g:<16} n={v.get('n_group',0)}")
                continue
            print(f"   {g:<16} n={v['n_group']:>5} 均值{v['excess_mean_bp']:>7.1f}bp 中位{v['excess_med_bp']:>7.1f}bp "
                  f"t={v['t_vs_zero']:>6} 正比例{v['pos_frac']:.1%}")
    nc = res["A_premium_fingerprint"].get("neg_ctrl_semiconductor")
    if nc:
        print(f"\n[负控] {nc['label']}: 大跌日相对上证超额 {nc['excess_on_big_down_days_mean_bp']}bp, 正比例 {nc['pos_frac']:.1%}")

    a2 = res["A2_cross_check"]
    if "skip" not in a2:
        print(f'\n== A2 同指数三只 ETF 交叉验证（{a2["n_days"]} 日, {a2["span"][0]}..{a2["span"][1]}）==')
        print(f'  同日超额相关: {a2["corr_same_index_etfs"]}')
        print(f'  {"ETF":<12} {"n":>5} {"总超额bp":>9} {"中位":>7} {"特有超额bp":>10} {"t":>6} {"正比例":>7}')
        for r in a2["big_down"]:
            print(f'  {r["etf"]:<12} {r["n_big_down"]:>5} {r["excess_total_bp"]:>9.1f} {r["excess_total_med_bp"]:>7.1f} '
                  f'{r["excess_own_bp"]:>10.1f} {r["excess_own_t"]:>6} {r["excess_own_pos_frac"]:>7.1%}')
        print(f'  ⇒ {a2["verdict"]}')

    print("\n== B 信号（大跌 + 5日ETF溢价>0）后买沪深300 ==")
    print(f'{"h":>4} {"group":<22} {"n":>5} {"mean%":>7} {"med%":>7} {"win":>6} {"t":>6} {"CI95%":>18}')
    for r in res["B_signal_tradability"]["by_horizon"]:
        if "mean_pct" not in r:
            continue
        print(f'{r["h"]:>4} {r["group"]:<22} {r["n"]:>5} {r["mean_pct"]:>7.2f} {r["med_pct"]:>7.2f} '
              f'{r["win"]:>6.1%} {r["t"]:>6.2f}  [{r["ci95_pct"][0]:>6.2f},{r["ci95_pct"][1]:>6.2f}]')

    c = res["C_signal_mdd"]
    print(f'\n== C 信号后 250 日最深浮亏 ==')
    print(f'  信号日(抽稀){c["n_signal_thinned"]} 次: MDD 中位 {c["signal_mdd_med"]}% / p10 {c["signal_mdd_p10"]}%')
    print(f'  普通大跌日{c["n_plain_down_thinned"]} 次: MDD 中位 {c["plain_mdd_med"]}% / p10 {c["plain_mdd_p10"]}%')

    print("\n== D 政策日之后（次日收盘起算 250 日） ==")
    n_broke = 0
    n_tot = 0
    for r in res["D_policy_days"]["rows"]:
        a = r.get("000300.SH") or {}
        b = r.get("399006.SZ") or {}
        k = r.get("000688.SH") or {}
        if a:
            n_tot += 1
            n_broke += 1 if a.get("broke_entry") else 0
        print(f'  {r["date"]} {r["event"][:22]:<24} 沪深300 {a.get("fwd250_pct","-")}% (MDD {a.get("mdd_pct","-")}%) '
              f'创业板 {b.get("fwd250_pct","-")}% 科创50 {k.get("fwd250_pct","-")}%')
    print(f'  ⇒ 跌破入场价的次数: {n_broke}/{n_tot}（沪深300, 250 日内）')

    print("\n== E 行业周期（动量/反转横截面） ==")
    print(f'  行业指数 {res["E_industry_cycle"].get("n_industry")} 只: {res["E_industry_cycle"].get("names")}')
    for r in res["E_industry_cycle"].get("grid", []):
        if "hi_minus_lo_pct" not in r:
            print(f'  look={r["look"]} hold={r["hold"]} n={r["n"]} 样本不足')
            continue
        print(f'  look={r["look"]:>3} hold={r["hold"]:>3} 重叠n={r["n"]:>5} 独立n={r["n_indep"]:>4} '
              f'高低差 {r["hi_minus_lo_pct"]:>6.2f}% t重叠={r["t_overlap"]:>6} t独立={r["t_indep"]:>6} '
              f'CI(独立){r["ci95_indep_pct"]}')
        print(f'      高组年化 {r["hi_ann_pct"]:>6.2f}%  低组 {r["lo_ann_pct"]:>6.2f}%  全体等权 {r["all_ann_pct"]:>6.2f}%'
              f'  ⇒ 高组相对全体 {r["hi_ann_pct"] - r["all_ann_pct"]:>+6.2f}pct/年')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
