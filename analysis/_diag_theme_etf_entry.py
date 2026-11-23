"""六个主题 ETF 的入场检验：位置 / 共同因子 / 事件条件 / 与「等降息」的对照。

口径不新造：close_adj / dd52 / pos250 / vol20 / amt20 全部来自
`_diag_industry_etf_asym.load`（与研究段同一份代码），只把 symbol 换成这六个。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import _diag_industry_etf_asym as core  # noqa: E402
import etf_watch as W  # noqa: E402

OUT = ROOT / "data" / ".scratch" / "theme_etf_entry.json"
COST = 0.0006          # 双边 6bp，与研究段一致
HOLD = 20
LOW, DEEP = -0.35, -0.50
VOL_HI = 0.66

SYMS = {
    "588170.SH": "科创半导体ETF华夏",
    "512710.SH": "军工龙头ETF富国",
    "159387.SZ": "创业板新能源ETF国泰",
    "159869.SZ": "游戏ETF华夏",
    "159363.SZ": "创业板人工智能ETF华宝",
    "159530.SZ": "机器人ETF易方达",
}


def build() -> pd.DataFrame:
    etf, _ = W.fresh_etf()
    d, _ = core.load(etf_raw=etf)
    d = d[d["symbol"].isin(SYMS)].copy()
    g = d.groupby("symbol", group_keys=False)
    # 单票 500 日波动分位（与研究段 market_regime 同法：rolling rank）
    d["vol_pct"] = g["vol20"].apply(lambda s: s.rolling(500, min_periods=120).rank(pct=True))
    d["fwd20"] = g["close_adj"].apply(lambda s: s.shift(-1 + HOLD) / s.shift(-1) - 1)
    d["dd_bin"] = pd.cut(
        d["dd52"], [-1.01, -0.50, -0.35, -0.20, -0.10, 0.001],
        labels=["−50%↓", "−50~−35", "−35~−20", "−20~−10", "−10~0"],
    )
    return d.sort_values(["symbol", "date"])


def main() -> None:
    d = build()
    idx, imeta = W.fresh_index()
    reg = core.market_regime(idx_raw=idx).set_index("date")
    asof = d["date"].max()
    mkt = reg.loc[asof]
    print(f"asof {asof.date()} | 市场 {mkt['state']} | HS300 {mkt['idx']:.1f} / MA60 {mkt['ma60']:.1f} "
          f"| 市场波动分位 {mkt['vol_pct']:.0%}")

    # ---------- 1. 当前位置 ----------
    rows = []
    for sym, name in SYMS.items():
        x = d[d.symbol == sym]
        if x.empty:
            continue
        r = x.iloc[-1]
        rows.append({
            "symbol": sym, "name": name,
            "first": x.date.min().strftime("%Y-%m-%d"), "n": int(len(x)),
            "close": round(float(r.close_adj), 4),
            "dd52": round(float(r.dd52), 4),
            "pos250": round(float(r.pos250), 3) if pd.notna(r.pos250) else None,
            "vol_pct": round(float(r.vol_pct), 3) if pd.notna(r.vol_pct) else None,
            "amt20_yi": round(float(r.amt20) / 1e8, 2),
            "ma60_above": bool(r.close_adj > x["close_adj"].rolling(60, min_periods=40).mean().iloc[-1]),
        })
    pos = pd.DataFrame(rows)
    print("\n=== 当前位置（asof）===")
    print(pos.to_string(index=False))

    # ---------- 2. 六只是一注还是一篮 ----------
    px = d.pivot(index="date", columns="symbol", values="close_adj").dropna()
    ret = px.pct_change().dropna().tail(250)
    cm = ret.corr()
    print("\n=== 近 250 日收益相关矩阵 ===")
    print(cm.round(2).to_string())
    iu = np.triu_indices(len(cm), 1)
    pair = cm.values[iu]
    # 第一主成分解释比例（标准化后）
    z = (ret - ret.mean()) / ret.std()
    ev = np.linalg.eigvalsh(np.cov(z.T.values))
    pc1 = float(ev[-1] / ev.sum())

    hs = idx[idx.symbol == "000300.SH"].set_index("date")["close"].pct_change().reindex(ret.index)
    betas = {s: round(float(np.cov(ret[s], hs.dropna().reindex(ret.index).fillna(0))[0, 1] /
                            np.var(hs.dropna().reindex(ret.index).fillna(0))), 2)
             for s in ret.columns}

    # 对照：20 个行业 ETF 等权净值的同期相关（用研究段面板）
    ind_panel = core.aggregate(d) if False else None
    print(f"\n六只两两相关 中位 {np.median(pair):.2f} · 最低 {pair.min():.2f} · 最高 {pair.max():.2f}")
    print(f"第一主成分解释 {pc1:.0%} 的方差 | beta(HS300) {betas}")

    # ---------- 3. 事件条件（本地历史）----------
    ev_rows = []
    for sym, name in SYMS.items():
        x = d[d.symbol == sym].copy()
        band = x[(x.dd52 >= DEEP) & (x.dd52 <= LOW) & x.vol_pct.notna()]
        hi = band[band.vol_pct > VOL_HI]
        lo = band[band.vol_pct <= VOL_HI]
        for lab, s in (("高波砸盘", hi), ("低波阴跌", lo), ("区间合计", band)):
            v = s["fwd20"].dropna()
            ev_rows.append({
                "name": name, "bin": lab, "n": int(len(v)),
                "ev_bp": round(float(v.mean()) * 1e4, 1) if len(v) else None,
                "med_bp": round(float(v.median()) * 1e4, 1) if len(v) else None,
                "win": round(float((v > 0).mean()) * 100, 1) if len(v) else None,
            })
    ev = pd.DataFrame(ev_rows)
    print("\n=== 跌破 35~50% 后的 20 日（次日进场，扣 6bp）===")
    print(ev.to_string(index=False))

    # 位置梯度（六只合并，按 dd 分档）
    grad = []
    for b, g in d.groupby("dd_bin", observed=True):
        v = g["fwd20"].dropna()
        if len(v) < 30:
            continue
        grad.append({"bin": str(b), "n": int(len(v)),
                     "ev_bp": round(float(v.mean() - COST) * 1e4, 1),
                     "med_bp": round(float(v.median() - COST) * 1e4, 1),
                     "win": round(float((v > 0).mean()) * 100, 1)})
    print("\n=== 位置梯度（六只合并，含成本）===")
    print(pd.DataFrame(grad).to_string(index=False))

    # ---------- 4. 一年内的最大回撤经历 ----------
    ddmax = {}
    for sym, name in SYMS.items():
        x = d[d.symbol == sym].set_index("date")["close_adj"]
        x1 = x[x.index >= x.index.max() - pd.Timedelta(days=365)]
        ddmax[name] = round(float((x1 / x1.cummax() - 1).min()), 4)
    print("\n=== 近一年内最大回撤 ===")
    print(ddmax)

    res = {
        "meta": {
            "asof": str(asof.date()),
            "market": {k: (float(v) if isinstance(v, (int, float, np.floating)) else str(v))
                       for k, v in mkt.items()},
            "cost": COST, "hold": HOLD, "band": [DEEP, LOW], "vol_hi": VOL_HI,
            "note": "fwd20 = 次日收盘进 → 第 21 个交易日收盘出；单票 vol_pct 为 500 日滚动分位",
        },
        "position": rows,
        "corr": {a: {b: round(float(cm.loc[a, b]), 3) for b in cm.columns} for a in cm.index},
        "corr_median": round(float(np.median(pair)), 3),
        "pc1": round(pc1, 3),
        "beta": betas,
        "events": ev_rows,
        "gradient": grad,
        "mdd_1y": ddmax,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
