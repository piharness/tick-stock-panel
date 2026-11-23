"""行业 ETF 非对称 step2：独立事件口径、相对收益（剔 beta）、月度可执行回测。

step1 的逐日分组有三个自欺风险，这里逐个关掉：
1. 逐日重叠 → 独立事件（dd 首次跌破阈值），N 是事件数；
2. 市场 beta → 相对收益（减当月全行业等权），检验是不是纯行业选择 alpha；
3. 逐日进出不可执行 → 月末调仓，换手才收成本。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "analysis/_etf_asym_tmp"
OUT = ROOT / "analysis/_diag_industry_etf_asym_bt_result.json"

LOW, DEEP = -0.35, -0.50     # 低吸区间：跌 35~50%
HIGH = -0.05                  # 高位：距一年新高 5% 以内
COST = 0.0006                 # 每次调仓双边


def load():
    panel = pd.read_parquet(TMP / "panel.parquet")
    nav = pd.read_parquet(TMP / "nav.parquet")
    panel["date"] = pd.to_datetime(panel["date"])
    nav.index = pd.to_datetime(nav.index)
    return panel, nav


def nav_features(nav: pd.DataFrame) -> pd.DataFrame:
    """行业净值口径的位置（比 ETF 加权更干净，且与回测收益同源）。"""
    hi = nav.rolling(250, min_periods=200).max()
    ma = nav.rolling(250, min_periods=200).mean()
    vol = nav.pct_change().rolling(20, min_periods=15).std()
    dd = nav / hi - 1
    out = pd.DataFrame({"dd": dd.stack(), "ma_dev": (nav / ma - 1).stack(), "vol": vol.stack()})
    out.index.names = ["date", "industry"]
    return out.reset_index()


def month_ends(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(idx, index=idx)
    return pd.DatetimeIndex(s.groupby([idx.year, idx.month]).max().values)


def backtest(nav: pd.DataFrame, cost: float = COST) -> dict:
    me = month_ends(nav.index)
    me = me[me >= pd.Timestamp("2016-01-01")]
    sub = nav.loc[me]
    fwd = sub.shift(-1) / sub - 1          # 下月收益
    hi = nav.rolling(250, min_periods=200).max()
    # 位置用「上月末」的读数（t-1 月末收盘可知），本月持有 ⇒ 无前视
    dd = (nav / hi - 1).loc[me].shift(1)

    mkt = fwd.mean(axis=1)                  # 全行业等权 = 基准
    groups = {
        "低位 −35~−50%": (dd <= LOW) & (dd > DEEP),
        "深跌 −50%↓": dd <= DEEP,
        "中位 −10~−35%": (dd <= -0.10) & (dd > LOW),
        "高位 −5~0%": dd > HIGH,
        "全行业等权": pd.DataFrame(True, index=dd.index, columns=dd.columns),
    }
    res, curves = {}, {}
    for name, mask in groups.items():
        r = fwd.where(mask).mean(axis=1)     # 组内行业等权（缺失不参与）
        cnt = mask.sum(axis=1)
        r = r[cnt > 0]
        if len(r) < 12:
            res[name] = {"months": int(len(r)), "note": "样本不足"}
            continue
        r = r - cost
        rel = r - mkt.reindex(r.index)
        navc = (1 + r).cumprod()
        mdd = (navc / navc.cummax() - 1).min()
        n = len(r)
        res[name] = {
            "months": int(n),
            "avg_ret_bp": round(float(r.mean()) * 1e4, 1),
            "avg_rel_bp": round(float(rel.mean()) * 1e4, 1),
            "t_rel": round(float(rel.mean() / (rel.std(ddof=1) / np.sqrt(len(rel)))), 2) if n > 24 else None,
            "win": round(float((r > 0).mean()) * 100, 1),
            "ann_pct": round(float(navc.iloc[-1] ** (12 / n) - 1) * 100, 1),
            "mdd_pct": round(float(mdd) * 100, 1),
            "sharpe": round(float(r.mean() / r.std(ddof=1) * np.sqrt(12)), 2),
            "avg_names": round(float(cnt[cnt > 0].mean()), 1),
            "first": str(r.index[0].date()),
        }
        curves[name] = [round(float(x), 4) for x in navc.to_numpy()]
    return {"stats": res, "curve_dates": [str(d.date()) for d in nav.loc[me][me >= pd.Timestamp("2016-01-01")].index[:len(curves["全行业等权"])]], "curves": curves}


def event_study(nav: pd.DataFrame, rel_base: pd.Series) -> dict:
    """独立事件：dd 首次跌破 −35%（且未破 −50%），持有 20/60 日，相对全行业等权。"""
    hi = nav.rolling(250, min_periods=200).max()
    dd = nav / hi - 1
    flag = (dd <= LOW) & (dd > DEEP)
    # 注意：bool 列 shift 后是 object，`~True` 得 −2，这里实际标的是「段内第 2 天起」。
    # 归档结果未重跑；新代码统一走 etf_watch.run_starts()（见 holding-and-exit.md §5）。
    enter = flag & ~flag.shift(1).fillna(False)

    rows = []
    volz = nav.pct_change().rolling(20, min_periods=15).std()
    volpct = volz.rolling(500, min_periods=250).rank(pct=True)
    for ind in nav.columns:
        dates = enter.index[enter[ind].fillna(False)]
        last = -999
        for d in dates:
            i = nav.index.get_loc(d)
            if i - last < 60:      # 冷却期：同行业 60 交易日内不重复计事件
                continue
            last = i
            vp = float(volpct[ind].iloc[i]) if not np.isnan(volpct[ind].iloc[i]) else np.nan
            for h in (20, 60):
                if i + 1 + h >= len(nav):
                    continue
                # 触发日收盘才知道信号 ⇒ 次日收盘进场
                r = nav[ind].iloc[i + 1 + h] / nav[ind].iloc[i + 1] - 1
                b = rel_base.iloc[i + 1 + h] / rel_base.iloc[i + 1] - 1
                rows.append({"industry": ind, "date": d, "h": h, "ret": r, "rel": r - b,
                             "dd": float(dd[ind].iloc[i]), "volpct": vp})
    ev = pd.DataFrame(rows)
    # 事件在时间上高度聚簇（同月多行业同时触发）⇒ 以「月」为独立观测重算 t
    ev["ym"] = ev.date.dt.to_period("M").astype(str)
    out = {}
    for h in (20, 60):
        x = ev[ev.h == h]
        monthly = x.groupby("ym")[["ret", "rel"]].mean()
        t = float(monthly.rel.mean() / (monthly.rel.std(ddof=1) / np.sqrt(len(monthly))))
        out[f"h{h}"] = {
            "events": int(len(x)),
            "indep_months": int(len(monthly)),
            "ev_bp": round(float(x.ret.mean() - COST) * 1e4, 1),
            "med_bp": round(float(x.ret.median() - COST) * 1e4, 1),
            "rel_bp": round(float(x.rel.mean()) * 1e4, 1),
            "t_rel_day": round(float(x.rel.mean() / (x.rel.std(ddof=1) / np.sqrt(len(x)))), 2),
            "t_rel_month": round(t, 2),
            "win": round(float((x.ret > 0).mean()) * 100, 1),
            "rel_win": round(float((x.rel > 0).mean()) * 100, 1),
            "p10_bp": round(float(np.percentile(x.ret, 10)) * 1e4, 1),
            "p90_bp": round(float(np.percentile(x.ret, 90)) * 1e4, 1),
        }
    # 分年
    ev["year"] = ev.date.dt.year
    out["by_year"] = [
        {"year": int(y), "h": int(h), "n": int(len(g)),
         "ev_bp": round(float(g.ret.mean() - COST) * 1e4, 1),
         "rel_bp": round(float(g.rel.mean()) * 1e4, 1),
         "win": round(float((g.ret > 0).mean()) * 100, 1)}
        for (y, h), g in ev.groupby(["year", "h"]) if len(g) >= 8
    ]
    # 离新高越深越好？事件按 dd 深度分组
    ev["dbin"] = pd.cut(ev.dd, [-1, -0.45, -0.40, -0.35], labels=["−45%↓", "−40~−45", "−35~−40"])
    out["by_depth"] = [
        {"bin": str(b), "h": int(h), "n": int(len(g)),
         "ev_bp": round(float(g.ret.mean() - COST) * 1e4, 1),
         "rel_bp": round(float(g.rel.mean()) * 1e4, 1)}
        for (b, h), g in ev.groupby(["dbin", "h"], observed=True) if len(g) >= 15
    ]
    # 关键判别：这笔跌是「高波动砸出来的」还是「低波动阴跌出来的」
    ev["vbin"] = pd.cut(ev.volpct, [0, 0.33, 0.66, 1.01], labels=["低波阴跌", "中波", "高波砸盘"])
    out["by_vol"] = []
    for (b, h), g in ev.groupby(["vbin", "h"], observed=True):
        if len(g) < 12:
            continue
        monthly = g.groupby("ym")[["ret", "rel"]].mean()
        out["by_vol"].append({
            "bin": str(b), "h": int(h), "n": int(len(g)), "months": int(len(monthly)),
            "ev_bp": round(float(g.ret.mean() - COST) * 1e4, 1),
            "med_bp": round(float(g.ret.median() - COST) * 1e4, 1),
            "rel_bp": round(float(g.rel.mean()) * 1e4, 1),
            "t_month": round(float(monthly.rel.mean() / (monthly.rel.std(ddof=1) / np.sqrt(len(monthly)))), 2) if len(monthly) > 6 else None,
            "p10_bp": round(float(np.percentile(g.ret, 10)) * 1e4, 1),
            "p90_bp": round(float(np.percentile(g.ret, 90)) * 1e4, 1),
            "win": round(float((g.ret > 0).mean()) * 100, 1),
        })
    out["_events_sample"] = ev[ev.h == 20].tail(5)[["industry", "date", "dd"]].astype(str).to_dict("records")
    return out


def subsample(panel: pd.DataFrame) -> dict:
    out = []
    for lab, (a, b) in {"2015-2021": ("2015-01-01", "2021-12-31"),
                        "2022-2026": ("2022-01-01", "2026-12-31")}.items():
        x = panel[(panel.date >= pd.Timestamp(a)) & (panel.date <= pd.Timestamp(b))]
        for name, m in {"低位": (x.dd52 <= LOW) & (x.dd52 > DEEP),
                        "高位": x.dd52 > HIGH,
                        "深跌": x.dd52 <= DEEP}.items():
            v = x.loc[m, "fwd20"].dropna().to_numpy() - COST
            if len(v) < 100:
                continue
            out.append({"period": lab, "bucket": name, "n": int(len(v)),
                        "ev_bp": round(float(v.mean()) * 1e4, 1),
                        "med_bp": round(float(np.median(v)) * 1e4, 1),
                        "p10_bp": round(float(np.percentile(v, 10)) * 1e4, 1),
                        "p90_bp": round(float(np.percentile(v, 90)) * 1e4, 1),
                        "win": round(float((v > 0).mean()) * 100, 1)})
    return out


def main() -> None:
    panel, nav = load()
    bt = backtest(nav)
    ev = event_study(nav, nav.mean(axis=1))
    sub = subsample(panel)

    res = {"backtest": bt, "events": ev, "subsample": sub}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 月度回测（月末调仓，扣 6bp/次）===")
    print(pd.DataFrame(bt["stats"]).T.to_string())
    print("\n=== 独立事件（dd 首次跌破 −35%）===")
    for k in ("h20", "h60"):
        print(k, ev[k])
    print("\n=== 事件分年 ===")
    print(pd.DataFrame(ev["by_year"]).to_string(index=False))
    print("\n=== 事件按深度 ===")
    print(pd.DataFrame(ev["by_depth"]).to_string(index=False))
    print("\n=== 子样本 ===")
    print(pd.DataFrame(sub).to_string(index=False))


if __name__ == "__main__":
    main()
