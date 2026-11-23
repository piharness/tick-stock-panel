"""EXP-LEG2-DIVIDEND-01：红利真实总回报核验。

背景：`leg2-screen.md` 里红利的 7.32% 有 4pct 是**假设股息**，不能作为准入依据。
本轮把假设换成真实数据：

- 数据源：天天基金 `pingzhongdata/<code>.js`（原始文件已存 `data/etf_watch/_fund_nav/`）；
- 全收益：用**累计净值（复权）**日收益连乘。分红日单位净值会掉、累计净值不掉，
  所以累计净值的日收益就是「分红再投资」后的总回报；
- 现金：用 511990 华宝添益的**万份收益**逐日复利，替代 2%/年 的假设；
- 国债：511010 同样用累计净值。

三条纪律：
1. 不把价格收益与全收益相加（全收益已经是含分红的总回报）；
2. 不做事后选比例：A/B/C/D 的权重先写死，D 的两种读法都给；
3. 不改 `portfolio.json` 的既有基准，结果另存 `leg2_dividend.json`。

用法：
    python analysis/etf_watch_dividend.py            # 用已缓存 js
    python analysis/etf_watch_dividend.py --refresh  # 重新下载
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402
import etf_watch_experiments as X  # noqa: E402
import etf_watch_portfolio as pf  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

RAW = ROOT / "data/etf_watch/_fund_nav"
OUT = ROOT / "data/etf_watch/leg2_dividend.json"
COST = pf.COST
BASE_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"

FUNDS = {
    "510880": "红利ETF华泰柏瑞（上证红利）",
    "515080": "中证红利ETF招商",
    "515180": "红利ETF易方达（中证红利）",
    "511010": "国债ETF国泰",
    "511990": "华宝添益ETF（货币）",
}


def _grab(js: str, name: str):
    m = re.search(name + r"\s*=\s*(\[.*?\]);", js, re.S)
    return json.loads(m.group(1)) if m else None


def fetch(code: str, refresh: bool = False) -> dict:
    p = RAW / f"{code}.js"
    if refresh or not p.exists():
        RAW.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BASE_URL.format(code=code), timeout=30) as r:
            p.write_bytes(r.read())
    js = p.read_text(encoding="utf-8", errors="ignore")
    nav = _grab(js, "Data_netWorthTrend")
    ac = _grab(js, "Data_ACWorthTrend")
    inc = _grab(js, "Data_millionCopiesIncome")
    # 东财的净值日期比交易日早一天（2020-01-01 出现的是 01-02 的净值），+1 天对齐；
    # 实测 +1 天后与交易日历 2106/2106 完全重合，不偏移会让整条收益错一天。
    shift = pd.Timedelta(days=1)
    out = {"file": p, "nav": None, "ac": None, "income": None}
    if nav:
        out["nav"] = pd.Series({pd.Timestamp(x["x"], unit="ms").normalize() + shift: float(x["y"]) for x in nav}).sort_index()
    if ac:
        out["ac"] = pd.Series({pd.Timestamp(x[0], unit="ms").normalize() + shift: float(x[1]) for x in ac}).sort_index()
    if inc:
        out["income"] = pd.Series({pd.Timestamp(x[0], unit="ms").normalize(): float(x[1]) for x in inc}).sort_index()
    return out


def cash_daily(income: pd.Series, sessions) -> pd.Series:
    """货币基金万份收益 → 每个交易日应计的复利。

    511990 的序列含周末（约 297 点/年），直接把每个观测乘进累计值即可；
    观测缺失的日子按无收益处理（宁可少算，也不外推）。
    """
    s = income.sort_index()
    level = (1 + s / 10000.0).cumprod()
    start = min(s.index[0], pd.DatetimeIndex(sessions)[0])
    cal = pd.date_range(start, s.index[-1], freq="D")
    cum = level.reindex(cal).ffill().fillna(1.0)
    idx = pd.DatetimeIndex([t for t in sessions if t in cum.index])
    return cum.loc[idx].pct_change().reindex(pd.DatetimeIndex(sessions)).fillna(0.0)


def dividends_from_ac(nav: pd.Series, ac: pd.Series) -> pd.Series:
    """逐笔分红：D_t = Δ(AC − NAV)。

    已验证 AC = NAV + 累计分红（差值单调不减、只在除息日跳；510880/515080/515180 全部成立）。
    这是「逐笔还原」：每一笔分红对应一个正跳变，不掺任何假设。
    """
    j = pd.concat([nav, ac], axis=1, keys=["nav", "ac"]).dropna()
    d = (j.ac - j.nav).diff()
    # 阈值 0.003 元/份：净值 4 位小数，(AC−NAV) 有 ±0.0001 舍入噪声；真实分红 ≥0.01 元。
    # 只有「单调不减 + 跳变次数少」才说明 AC = NAV + 累计分红；否则返回 None（如 510880 的
    # 2007 年份额折算、511010 的 AC 是归一化净值），不能拿它逐笔还原。
    pos = int((d > 0.003).sum())
    neg = int((d < -0.003).sum())
    if pos > 40 or neg > 0:
        return None
    return d.where(d > 0.003, 0.0)


def tr_from_nav_div(nav: pd.Series, div: pd.Series) -> pd.Series:
    """严格总收益：r_t = (NAV_t − NAV_{t−1} + D_t) / NAV_{t−1}，再连乘（分红按净值再投资）。

    分母必须是 NAV（价格），不是 AC（净值+累计分红）——用 AC 当分母会低估总收益，
    而且低估幅度随分红多少变化，两只跟踪同一指数的基金因此能差出 2pct。
    """
    j = pd.concat([nav, div], axis=1, keys=["nav", "div"]).dropna()
    r = (j.nav.diff() + j["div"]) / j.nav.shift(1)
    return (1 + r.fillna(0)).cumprod()


def total_return(f: dict) -> tuple[pd.Series, str]:
    """优先累计净值（分红再投资）；货币基金用万份收益。"""
    if f["ac"] is not None and len(f["ac"]) > 100:
        r = f["ac"].pct_change().dropna()
        if float(r.abs().max()) > 0.15:
            raise SystemExit(f"累计净值出现 |日收益|>15%（{r.abs().idxmax()}），先查份额折算")
        return r, "累计净值（复权）日收益"
    if f["income"] is not None:
        return (f["income"] / 10000.0), "万份收益/10000"
    raise SystemExit("既无累计净值也无万份收益")


def raw_close(symbol: str) -> pd.Series:
    """交易所收盘（份额折算已摊平，不含分红）——只用来算价格收益。"""
    d = pd.read_parquet(core.ETF_FILE, columns=["symbol", "date", "close"])
    x = d[d.symbol == symbol].sort_values("date")
    if x.empty:
        raise SystemExit(f"{symbol} 不在 ETF 日线里")
    px = core._dejump(x["close"].reset_index(drop=True))
    return pd.Series(px.to_numpy(), index=pd.DatetimeIndex(x["date"]))


def stats(eq: pd.Series) -> dict:
    eq = eq.dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    return {
        "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
        "years": round(yrs, 2),
        "total": round(float(eq.iloc[-1] / eq.iloc[0] - 1), 4),
        "ann": round(float((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1), 4),
        "mdd": round(float(dd.min()), 4),
        "worst20": round(float(eq.pct_change(20).min()), 4),
        "vol": round(float(eq.pct_change().std() * np.sqrt(252)), 4),
        "yearly": {str(k.year): round(float(v), 4) for k, v in
                   (eq.resample("YE").last().pct_change().items())},
    }


def portfolio(r1: pd.Series, rx: pd.Series, w1: float, cost: float = COST) -> pd.Series:
    """月度再平衡的 两腿组合；w1 是腿1权重，其余给 x。"""
    df = pd.concat([r1, rx], axis=1, keys=["a", "b"]).dropna()
    val = np.array([w1, 1 - w1], float)
    eq = []
    prev = None
    for t, a, b in zip(df.index, df["a"].to_numpy(), df["b"].to_numpy()):
        m = t.to_period("M")
        if prev is not None and m != prev:      # 月初先回到目标权重，再吃当日收益
            tot = val.sum()
            tgt = np.array([w1, 1 - w1]) * tot
            turn = np.abs(tgt - val).sum()
            val = tgt * ((tot - turn * cost) / tot)
        prev = m
        val = val * np.array([1 + a, 1 + b])
        eq.append(val.sum())
    return pd.Series(eq, index=df.index)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="重新下载基金净值 js")
    args = ap.parse_args()

    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)
    status, dd, sym_mat = X.daily_matrices(m)["status"], X.daily_matrices(m)["dd"], X.daily_matrices(m)["symbol"]
    sessions = list(status.index)
    ev = X.build_events(status, dd)
    prices = X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()})
    vols = {s: pf.vol20(p) for s, p in prices.items()}

    raw = {c: fetch(c, args.refresh) for c in FUNDS}
    tr = {}
    tr_fix = {}
    divs = {}
    how = {}
    for c, f in raw.items():
        if c == "511990":
            how[c] = "万份收益（逐日复利）"
            continue
        r, why = total_return(f)
        tr[c] = (1 + r).cumprod()
        how[c] = why
        tr_fix[c] = tr[c]                     # 默认：AC 口径
        divs[c] = None
        if f["nav"] is not None and f["ac"] is not None:
            dv = dividends_from_ac(f["nav"], f["ac"])
            if dv is not None:
                tr_fix[c] = tr_from_nav_div(f["nav"], dv)
                divs[c] = dv[dv > 0]

    # 现金：511990 的逐日真实收益（不是 2% 假设）
    cash_r = cash_daily(raw["511990"]["income"], sessions)
    leg1 = pf.replay(ev, sessions, prices, vols, sym_mat, "six_slots", cash_yield=cash_r)
    eq1, expo = leg1["equity"], leg1["exposure"]
    r1 = eq1.pct_change().dropna()
    idle = expo.reindex(r1.index) == 0
    active = ~idle

    base = eq1.index[0]
    win = slice(base, eq1.index[-1])
    out = {"meta": {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [str(base.date()), str(eq1.index[-1].date())],
        "sources": {c: {"name": FUNDS[c], "url": BASE_URL.format(code=c),
                        "raw": str(raw[c]["file"].relative_to(ROOT)), "method": how[c]} for c in FUNDS},
        "rebalance": "月度再平衡（月初回到目标权重），6bp/边",
        "cash": "腿1内部现金按 511990 万份收益逐日计息，不再是 2% 假设",
        "note": "仅核验数据，不设计红利择时规则、不接生产、不改 portfolio.json",
    }, "funds": {}, "leg1": {}, "portfolios": {}, "dividend": {}}

    # 单资产指标
    for c in ("510880", "515080", "515180", "511010"):
        t = tr[c].reindex(r1.index)                    # AC 口径（官方指数已证明它与费率相符）
        t_pd = tr_fix[c].reindex(r1.index)             # 逐笔口径（仅作诊断）
        out["funds"][c] = {
            "ann": stats(t)["ann"], "mdd": stats(t)["mdd"], "worst20": stats(t)["worst20"],
            "vol": stats(t)["vol"], "yearly": stats(t)["yearly"],
            "per_div_ann": stats(t_pd)["ann"],
            "idle_ann": round(float((1 + t.pct_change()[idle].dropna()).prod()
                                    ** (252 / max(idle.sum(), 1)) - 1), 4),
            "corr_active": round(float(r1[active].corr(t.pct_change()[active])), 3),
        }
    # 股息贡献 = 全收益 - 价格收益（价格用本地 ETF 收盘，份额折算已摊平）
    out["dividend"] = {}
    for code, sym in (("510880", "510880.SH"), ("515080", "515080.SH"), ("515180", "515180.SH")):
        pr = raw_close(sym).pct_change().dropna()
        p_ann = float((1 + pr).prod() ** (252 / len(pr)) - 1)
        out["dividend"][code] = {
            "price_ann": round(p_ann, 4),
            "total_ann": out["funds"][code]["ann"],
            "ac_method_ann": out["funds"][code]["ann"],
            "per_div_ann": out["funds"][code]["per_div_ann"] if divs[code] is not None else None,
            "n_div": int(len(divs[code])) if divs[code] is not None else None,
            "per_div_ok": divs[code] is not None,
            "dividend_contrib": round(out["funds"][code]["ann"] - p_ann, 4),
            "window": [str(pr.index[0].date()), str(pr.index[-1].date())],
        }

    l1 = stats(eq1)
    out["leg1"] = {"ann": l1["ann"], "mdd": l1["mdd"], "worst20": l1["worst20"],
                   "idle_days": int(idle.sum()), "sessions": int(len(idle)),
                   "cash_ann": round(float((1 + cash_r).prod() ** (252 / len(cash_r)) - 1), 4)}

    # 组合：A/B/C/D（D 两种读法）
    x_div = tr_fix["515180"].reindex(r1.index)     # 逐笔口径：与官方指数差 −2bp，已验证
    x_div_hi = tr_fix["515080"].reindex(r1.index)  # 与官方指数差 +200bp ⇒ 数据缺陷，仅留作对照
    x_sh = tr_fix["510880"].reindex(r1.index)
    x_bond = tr_fix["511010"].reindex(r1.index)
    combos = {
        "A_leg1_only": (None, 1.0),
        "B_50_50_div_low": (x_div, 0.5),
        "B2_50_50_div_high": (x_div_hi, 0.5),
        "C_50_50_bond": (x_bond, 0.5),
        "D1_leg1_75_div_25": (x_div, 0.75),
        "D2_leg1_25_div_75": (x_div, 0.25),
        "E_50_50_shdiv": (x_sh, 0.5),
    }
    # 中证红利两只 ETF 都从 2019-11-29 起，组合必须统一到共同窗口，否则 A 会拿 8.4 年比 7 年
    div_start = max(tr["515080"].index.min(), tr["515180"].index.min())
    div_start = r1.index[r1.index >= div_start][0]

    # 官方中证红利全收益（H00922）：裁决两只 ETF 谁可信
    off = pd.read_parquet(ROOT / "data/etf_watch/_index_tr/H00922.parquet")
    off_s = pd.Series(off["close"].to_numpy(), index=pd.DatetimeIndex(off["date"])).sort_index()
    off_s = off_s.loc[off_s.index >= div_start]
    off_ann = float((off_s.iloc[-1] / off_s.iloc[0]) ** (365.25 / (off_s.index[-1] - off_s.index[0]).days) - 1)
    out["official"] = {"code": "H00922", "name": "中证红利全收益",
                       "ann": round(off_ann, 4),
                       "window": [str(off_s.index[0].date()), str(off_s.index[-1].date())],
                       "source": "https://www.csindex.com.cn/csindex-home/perf/index-perf"}
    # 每只基金必须与**它自己的窗口**对齐，否则又犯了跨期比较的错
    for c in ("515080", "515180"):
        fw = tr[c].dropna().index
        o = off_s.loc[off_s.index >= fw[0]]
        o_ann_c = float((o.iloc[-1] / o.iloc[0]) ** (365.25 / (o.index[-1] - o.index[0]).days) - 1)
        base_ann = out["funds"][c].get("per_div_ann") or out["funds"][c]["ann"]
        out["funds"][c]["official_ann"] = round(o_ann_c, 4)
        out["funds"][c]["vs_official_bp"] = round((base_ann - o_ann_c) * 1e4, 1)
        out["funds"][c]["data_defect"] = bool(abs(out["funds"][c]["vs_official_bp"]) > 100)
    for k, (x, w) in combos.items():
        if x is None:
            eq_a = (1 + r1.loc[div_start:]).cumprod()
            st = stats(eq_a)
            out["portfolios"][k] = {kk: st[kk] for kk in ("ann", "mdd", "worst20", "vol", "total", "yearly")}
            out["portfolios"][k]["window"] = [str(div_start.date()), str(eq1.index[-1].date())]
            continue
        eq = portfolio(r1, x.pct_change().fillna(0), w)
        st = stats(eq)
        out["portfolios"][k] = {kk: st[kk] for kk in ("ann", "mdd", "worst20", "vol", "total", "yearly")}
        out["portfolios"][k]["window"] = [str(eq.index[0].date()), str(eq.index[-1].date())]

    # 压力窗口
    w20 = eq1.pct_change(20)
    t20 = w20.idxmin()
    i20 = eq1.index.get_loc(t20)
    peak = eq1.cummax()
    trough = (eq1 / peak - 1).idxmin()
    peak_t = eq1.loc[:trough].idxmax()
    out["windows"] = {"worst20": [str(eq1.index[max(0, i20 - 20)].date()), str(t20.date())],
                      "mdd": [str(peak_t.date()), str(trough.date())]}
    for c in ("510880", "515080", "515180", "511010"):
        s = tr[c].reindex(r1.index)
        out["funds"][c]["worst20_window"] = round(float((1 + s.pct_change().loc[
            out["windows"]["worst20"][0]:out["windows"]["worst20"][1]]).prod() - 1), 4)
        out["funds"][c]["mdd_window"] = round(float((1 + s.pct_change().loc[
            out["windows"]["mdd"][0]:out["windows"]["mdd"][1]]).prod() - 1), 4)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    print(f"窗口 {out['meta']['window'][0]} → {out['meta']['window'][1]}")
    print(f"现金（511990 万份收益）年化 {out['leg1']['cash_ann']:+.2%}")
    print(f"腿1（真实现金）年化 {l1['ann']:+.2%}  MDD {l1['mdd']:+.2%}  最差20日 {l1['worst20']:+.2%}")
    for c, d in out["dividend"].items():
        per = (f"逐笔 {d['per_div_ann']:+.2%}，{d['n_div']} 笔）" if d["per_div_ok"] else "逐笔不可用）")
        print(f"\n红利 {c}（{FUNDS[c]}）：价格 {d['price_ann']:+.2%} + 分红 "
              f"{d['dividend_contrib']:+.2%} = 全收益 {d['total_ann']:+.2%}"
              f"（AC 口径 {d['ac_method_ann']:+.2%}，{per}"
              f"  窗口 {d['window'][0]}→{d['window'][1]}")
    print(f"  空仓日年化 {out['funds']['510880']['idle_ann']:+.2%}  条件相关 {out['funds']['510880']['corr_active']:+.2f}"
          f"  最差20日窗口 {out['funds']['510880']['worst20_window']:+.2%}"
          f"  回撤窗口 {out['funds']['510880']['mdd_window']:+.2%}")
    print(f"国债：全收益 {out['funds']['511010']['ann']:+.2%}  相关 {out['funds']['511010']['corr_active']:+.2f}"
          f"  最差20日窗口 {out['funds']['511010']['worst20_window']:+.2%}"
          f"  回撤窗口 {out['funds']['511010']['mdd_window']:+.2%}")
    print(f"\n官方中证红利全收益 H00922：{off_ann:+.2%}（{out['official']['window'][0]}→{out['official']['window'][1]}）")
    for c in ("515080", "515180"):
        f_ = out["funds"][c]
        print(f"  {c}：官方 {f_['official_ann']:+.2%}，逐笔 {f_['per_div_ann']:+.2%}，差 {f_['vs_official_bp']:+.0f}bp"
              + ("  ⇒ 数据缺陷" if f_["data_defect"] else "  ⇒ 通过"))
    print("\n组合（月度再平衡，6bp/边）:")
    for k, v in out["portfolios"].items():
        print(f"  {k:22s} 年化 {v['ann']:+.2%}  MDD {v['mdd']:+.2%}  最差20日 {v['worst20']:+.2%}"
              f"  收益/回撤 {v['ann'] / abs(v['mdd']):.2f}")
    print(f"\n写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
