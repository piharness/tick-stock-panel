"""行业 ETF 非对称：价格位置 × 成交结构 × 市场环境 → 前瞻收益分布。

设计要点（每条都对应一次踩过的坑）：
- 单位是「行业×日」，同指数多只 ETF 按成交额加权合并（N 不是 ETF 数，是行业-日）。
- 信号只用 t 日及之前的数据；收益从 t+1 收盘进场（不能用 t 日收盘价成交）。
- 成本默认双边 6bp（ETF 无印花税，佣金万 1.5 + 点差 2bp + 冲击 1bp），另做 0/15bp 敏感性。
- 分层统计同时报 mean/median/p10/p90/偏度/上下不对称比，不只报 mean（一阶矩看不见尾部）。
- 显著性用按自然月 block bootstrap，不用逐日 t（日频强相关会把 t 打虚高）。
- 排除跨境/债券/货币/商品/宽基，只留 A 股行业 ETF；剔除 |ret|>30% 的除权异常。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ETF_FILE = ROOT / "data/backfill_20261117/etf_daily_full.parquet"
INST_FILE = ROOT / "data/instruments_etf/instruments_etf.parquet"
IDX_FILE = ROOT / "data/backfill_20261117/index_daily_full.parquet"
OUT = ROOT / "analysis/_diag_industry_etf_asym_result.json"

START = pd.Timestamp("2015-01-01")
MIN_AMT = 1.5e7  # 20 日均成交额门槛，低于此的点差会吞掉全部信号

# ---------- 分类 ----------
EXCLUDE = [
    "债", "短融", "添益", "日利", "快线", "现金", "理财", "国开", "政金",
    "信用", "转债", "黄金", "白银", "原油", "豆粕", "商品",
    "港股", "香港", "恒生", "H股", "中概", "纳斯达克", "纳指", "标普", "道琼斯",
    "日经", "德国", "法国", "英国", "东南亚", "越南", "沙特", "亚太",
    "全球", "海外", "QDII", "中韩", "中日",
    "300", "500", "1000", "2000", "上证50", "科创50", "科创100", "A500",
    "A50", "深证100", "MSCI", "全指", "等权", "红利", "低波", "价值",
    "成长", "质量", "增强", "综指", "综", "大盘", "中盘", "小盘",
]

# 顺序敏感：先具体后宽泛
INDUSTRY = [
    ("券商", ["证券", "券商"]),
    ("银行", ["银行"]),
    ("医药", ["医药", "医疗", "生物", "创新药", "疫苗", "CXO", "中药", "器械", "健康"]),
    ("电子", ["半导体", "芯片", "集成电路", "消费电子", "电子", "面板", "PCB", "光刻"]),
    ("计算机", ["计算机", "软件", "信创", "网络安全", "云计算", "大数据", "数字经济",
                "人工智能", "算力", "数据", "AIGC", "科技"]),
    ("通信", ["通信", "5G", "光模块", "光通信", "卫星"]),
    ("传媒", ["传媒", "游戏", "影视", "动漫", "广告", "出版", "文娱"]),
    ("军工", ["军工", "国防", "航空航天", "船舶"]),
    ("电新", ["新能源", "光伏", "储能", "电池", "锂电", "风电", "氢能", "太阳能"]),
    ("汽车", ["汽车", "智能车", "车"]),
    ("消费", ["白酒", "食品", "饮料", "消费", "家电", "家居", "旅游", "酒店",
              "零售", "商贸", "纺织", "服装", "美容", "品牌"]),
    ("农业", ["农业", "养殖", "畜牧", "粮食", "种业", "饲料", "猪"]),
    ("有色", ["有色", "稀土", "稀有金属", "铜", "铝", "锂矿", "钴", "镍", "金属"]),
    ("煤炭钢铁", ["煤炭", "钢铁", "焦炭"]),
    ("化工", ["化工", "化学", "材料", "新材料", "石化"]),
    ("地产基建", ["地产", "房地产", "建筑", "建材", "基建", "水泥"]),
    ("公用环保", ["电力", "公用", "水务", "环保", "电网", "燃气", "核电"]),
    ("交运", ["交通运输", "物流", "港口", "航空", "航运", "快递", "机场"]),
    ("机械", ["机械", "机器人", "机床", "工业母机"]),
    ("石油", ["石油", "油气", "能源"]),
]


def classify(name: str) -> str | None:
    for kw in EXCLUDE:
        if kw in name:
            return None
    for ind, kws in INDUSTRY:
        for kw in kws:
            if kw in name:
                return ind
    return None


SPLIT_THR = 0.25     # ETF 单日涨跌幅上限 10%/20% ⇒ 超过 25% 只可能是份额折算/合并


def _dejump(px: pd.Series, thr: float = SPLIT_THR) -> pd.Series:
    """把份额折算/合并的跳跃摊平，返回可与末日真实价衔接的连续价格。

    从后往前递推 f_i = f_{i+1}·(1+r_i)：跳跃点两侧 q=p·f 相等（折算的经济收益为 0），
    末日 f=1 所以最新报价仍是市场真实价。摊平后不得再有 |单日| > thr 的残跳。
    """
    p = px.to_numpy(float)
    if len(p) < 2:
        return px.astype(float)
    r = np.diff(p) / p[:-1]
    f = np.ones(len(p))
    for i in range(len(p) - 2, -1, -1):
        f[i] = f[i + 1] * (1 + r[i]) if abs(r[i]) > thr else f[i + 1]
    return pd.Series(p * f, index=px.index)


def load(
    etf_raw: pd.DataFrame | None = None,
    inst_raw: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """读 ETF 日线并算全部特征。

    `etf_raw` / `inst_raw` 仅供 `analysis/etf_watch.py` 注入「backfill + 增量分区」的合并帧，
    不改变默认口径 —— 扫描端必须与本节数字同源，所以公式只在这里定义一次。
    """
    d = pd.read_parquet(ETF_FILE) if etf_raw is None else etf_raw.copy()
    d["date"] = pd.to_datetime(d["date"])
    inst = pd.read_parquet(INST_FILE)[["symbol", "name"]] if inst_raw is None else inst_raw[["symbol", "name"]].copy()
    d = d.merge(inst, on="symbol", how="left")
    d["industry"] = d["name"].map(lambda x: classify(str(x)))
    d = d[d["industry"].notna() & (d["date"] >= START)].copy()
    d = d.sort_values(["symbol", "date"])

    # 份额折算/合并必须先摊平再算任何收益率：A 股 ETF 单日涨跌幅受 10%/20% 限制，
    # |单日| > 25% 只可能是份额折算（如 588170 于 2026-07-06 的 1:3，−66.7%）。
    # 若不摊平，`dd52` 会在折算后 250 日内恒显示 −70%~−83% 的假深跌，`fwd20` 也会
    # 在折算前 20 日的样本里带上 −66% 的假亏损 —— 两端都污染，且恰好落在深跌档。
    g = d.groupby("symbol", sort=False)
    d["px"] = g["close"].transform(_dejump)
    g = d.groupby("symbol", sort=False)
    d["ret"] = g["px"].pct_change()
    d["close_adj"] = g["px"].transform(lambda s: (1 + s.ffill().pct_change().fillna(0)).cumprod())
    # 盘中极值：日内 high/low 折成与 close_adj 同一单位（份额折算已由 _dejump 摊平）。
    # 折的是「净值/价格」这个比值，不是 close_adj 本身 —— 直接拿 raw low 除 close_adj 会把
    # 价格和归一化净值混在一起，凭空造出 −69% 的假盘中回撤（已踩）。
    _k = d["close_adj"] / d["close"].where(d["close"] > 0)
    d["hi_adj"] = d["high"] * _k
    d["lo_adj"] = d["low"] * _k

    g = d.groupby("symbol", sort=False)
    d["hi250"] = g["close_adj"].transform(lambda s: s.rolling(250, min_periods=200).max())
    d["ma250"] = g["close_adj"].transform(lambda s: s.rolling(250, min_periods=200).mean())
    d["ma60"] = g["close_adj"].transform(lambda s: s.rolling(60, min_periods=40).mean())
    d["ma250_slope"] = g["ma250"].transform(lambda s: s / s.shift(20) - 1)
    d["vol20"] = g["ret"].transform(lambda s: s.rolling(20, min_periods=15).std())
    d["amt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    d["amt250"] = g["amount"].transform(lambda s: s.rolling(250, min_periods=60).mean())
    # 位置：距一年高点的回撤（<=0）；分位用过去 250 日 close 的排名
    d["dd52"] = d["close_adj"] / d["hi250"] - 1
    d["pos250"] = g["close_adj"].transform(
        lambda s: s.rolling(250, min_periods=200).apply(lambda w: (w[-1] >= w).mean(), raw=True)
    )
    # 成交结构：相对自身 20 日均额的放量倍数、相对 250 日的活跃度
    d["vr"] = d["amount"] / d["amt20"]
    d["act"] = d["amt20"] / d["amt250"]

    # 前瞻：t+1 收盘进场，持有 H 日后收盘卖出
    for h in (5, 20, 60):
        d[f"fwd{h}"] = g["close_adj"].shift(-(h + 1)) / g["close_adj"].shift(-1) - 1

    d["liquid"] = d["amt20"] >= MIN_AMT
    return d, inst


def aggregate(d: pd.DataFrame, require_fwd: bool = True) -> pd.DataFrame:
    """同行业同日多只 ETF → 按成交额加权，得到行业-日面板。

    `require_fwd=False` 只去掉「未来 20 日收益存在」这一条（扫描端最新一天必然没有），
    其余过滤口径不变。
    """
    x = d[d["liquid"] & d["dd52"].notna()]
    if require_fwd:
        x = x[x["fwd20"].notna()]
    x = x.copy()
    x["w"] = x["amount"]
    rows = []
    for (ind, dt), grp in x.groupby(["industry", "date"], sort=True):
        w = grp["w"].to_numpy(float)
        w = w / w.sum()
        r = {"industry": ind, "date": dt, "n_etf": len(grp), "amt": float(grp["amount"].sum())}
        ret = grp["ret"].to_numpy(float)
        m = ~np.isnan(ret)
        r["ind_ret"] = float(np.sum(ret[m] * w[m]) / w[m].sum()) if m.any() else 0.0
        for c in ("dd52", "pos250", "vr", "act", "vol20", "ma250_slope", "fwd5", "fwd20", "fwd60"):
            v = grp[c].to_numpy(float)
            m = ~np.isnan(v)
            r[c] = float(np.sum(v[m] * w[m]) / w[m].sum()) if m.any() else np.nan
        r["above_ma250"] = float(np.sum((grp["close_adj"] > grp["ma250"]).to_numpy(float) * w) / w.sum())
        r["above_ma60"] = float(np.sum((grp["close_adj"] > grp["ma60"]).to_numpy(float) * w) / w.sum())
        rows.append(r)
    return pd.DataFrame(rows)


def industry_nav(panel: pd.DataFrame) -> pd.DataFrame:
    """行业等权净值（同一行业多只 ETF 的日收益等权平均），用于月度回测与事件研究。"""
    nav = panel.pivot(index="date", columns="industry", values="ind_ret").sort_index()
    nav = (1 + nav.fillna(0)).cumprod()
    return nav


def market_regime(idx_raw: pd.DataFrame | None = None) -> pd.DataFrame:
    """沪深300 的趋势 × 波动四分状态；`idx_raw` 同 `load` 的注入用途。"""
    idx = pd.read_parquet(IDX_FILE, columns=["symbol", "date", "close"]) if idx_raw is None else idx_raw.copy()
    idx["date"] = pd.to_datetime(idx["date"])
    hs = idx[idx["symbol"] == "000300.SH"].sort_values("date").set_index("date")["close"]
    out = pd.DataFrame(index=hs.index)
    out["idx"] = hs
    out["ma60"] = hs.rolling(60, min_periods=40).mean()
    out["slope60"] = out["ma60"] / out["ma60"].shift(20) - 1
    out["vol20"] = hs.pct_change().rolling(20, min_periods=15).std()
    out["vol_pct"] = out["vol20"].rolling(500, min_periods=200).rank(pct=True)
    out["trend_up"] = (hs > out["ma60"]) & (out["slope60"] > 0)
    out["trend_dn"] = (hs < out["ma60"]) & (out["slope60"] < 0)
    out["hi_vol"] = out["vol_pct"] > 0.5
    out["state"] = np.select(
        [out["trend_up"] & ~out["hi_vol"], out["trend_up"] & out["hi_vol"],
         out["trend_dn"] & ~out["hi_vol"], out["trend_dn"] & out["hi_vol"]],
        ["上行·低波", "上行·高波", "下行·低波", "下行·高波"], default="过渡",
    )
    return out.reset_index()


def stats(v: np.ndarray, cost: float = 0.0006) -> dict:
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return {}
    net = v - cost
    gain, loss = net[net > 0], net[net < 0]
    return {
        "n": int(len(v)),
        "ev_bp": round(float(np.mean(net)) * 1e4, 1),
        "med_bp": round(float(np.median(net)) * 1e4, 1),
        "p10_bp": round(float(np.percentile(net, 10)) * 1e4, 1),
        "p90_bp": round(float(np.percentile(net, 90)) * 1e4, 1),
        "win": round(float((net > 0).mean()) * 100, 1),
        "skew": round(float(pd.Series(net).skew()), 2),
        "updn": round(float(gain.mean() / abs(loss.mean())), 2) if len(loss) else None,
        "p_dn10": round(float((net < -0.10).mean()) * 100, 1),
        "p_up10": round(float((net > 0.10).mean()) * 100, 1),
    }


def block_boot(panel: pd.DataFrame, mask, col="fwd20", cost=0.0006, n_boot=2000, seed=7) -> dict:
    """按自然月分块重采样（保留月内相关性），返回 EV 的 90% 区间。"""
    x = panel.loc[mask, ["date", col]].dropna().copy()
    if len(x) < 30:
        return {}
    x["ym"] = x["date"].dt.to_period("M").astype(str)
    monthly = x.groupby("ym")[col].mean().to_numpy() - cost
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(monthly), size=(n_boot, len(monthly)))
    means = monthly[idx].mean(axis=1)
    return {
        "months": int(len(monthly)),
        "ev_bp": round(float(monthly.mean()) * 1e4, 1),
        "lo_bp": round(float(np.percentile(means, 5)) * 1e4, 1),
        "hi_bp": round(float(np.percentile(means, 95)) * 1e4, 1),
        "p_neg": round(float((means < 0).mean()) * 100, 1),
    }


def main() -> None:
    d, inst = load()
    cls = (
        d.groupby("industry")["symbol"].nunique().rename("etf")
        .to_frame()
        .join(d.groupby("industry").size().rename("rows"))
        .sort_values("rows", ascending=False)
    )
    print("=== 分类概览 ===")
    print(cls.to_string())

    panel = aggregate(d)
    panel = panel.merge(market_regime(), on="date", how="left")
    panel = panel[panel["state"].notna()]
    print(f"\n行业-日面板 {len(panel)} 行，{panel.industry.nunique()} 行业，"
          f"{panel.date.min().date()} → {panel.date.max().date()}")

    res: dict = {"meta": {
        "panel_rows": int(len(panel)),
        "industries": int(panel.industry.nunique()),
        "etf_symbols": int(d.symbol.nunique()),
        "start": str(panel.date.min().date()),
        "end": str(panel.date.max().date()),
        "cost_oneway_bp": 3,
        "min_amount_20d": MIN_AMT,
    }}

    # 0) 基准：无条件
    res["baseline"] = {h: stats(panel[f"fwd{h}"].to_numpy()) for h in (5, 20, 60)}

    # 1) 价格位置（距一年高点回撤）
    bins = [-1.01, -0.50, -0.35, -0.20, -0.10, -0.03, 0.01]
    labels = ["−50%↓", "−50~−35", "−35~−20", "−20~−10", "−10~−3", "−3~0"]
    panel["dd_bin"] = pd.cut(panel["dd52"], bins=bins, labels=labels)
    res["by_dd"] = []
    for lab in labels:
        m = panel["dd_bin"] == lab
        row = {"bin": str(lab), **stats(panel.loc[m, "fwd20"].to_numpy())}
        row["boot"] = block_boot(panel, m)
        res["by_dd"].append(row)

    # 2) 成交结构（放量倍数 vr）× 位置（低位/高位）
    panel["vr_bin"] = pd.cut(panel["vr"], [-0.01, 0.5, 0.8, 1.2, 1.8, 3, 1e9],
                             labels=["<0.5缩", "0.5-0.8", "0.8-1.2", "1.2-1.8", "1.8-3放", ">3暴"])
    panel["pos2"] = np.where(panel["dd52"] <= -0.25, "低位", np.where(panel["dd52"] >= -0.05, "高位", "中位"))
    res["dd_x_vr"] = []
    for p in ("低位", "中位", "高位"):
        for v in ["<0.5缩", "0.5-0.8", "0.8-1.2", "1.2-1.8", "1.8-3放", ">3暴"]:
            m = (panel["pos2"] == p) & (panel["vr_bin"] == v)
            if m.sum() < 40:
                continue
            res["dd_x_vr"].append({"pos": p, "vr": v, **stats(panel.loc[m, "fwd20"].to_numpy())})

    # 3) 市场环境
    res["by_state"] = []
    for st, grp in panel.groupby("state"):
        res["by_state"].append({"state": st, **stats(grp["fwd20"].to_numpy()),
                                "boot": block_boot(panel, panel["state"] == st)})

    # 4) 环境 × 位置（低吸在哪种环境下成立）
    res["state_x_pos"] = []
    for st in ["上行·低波", "上行·高波", "下行·低波", "下行·高波", "过渡"]:
        for p in ("低位", "中位", "高位"):
            m = (panel["state"] == st) & (panel["pos2"] == p)
            if m.sum() < 40:
                continue
            res["state_x_pos"].append({"state": st, "pos": p, **stats(panel.loc[m, "fwd20"].to_numpy()),
                                       "boot": block_boot(panel, m)})

    # 5) 趋势破坏：跌破年线 + 年线下行（低吸的失效边界）
    panel["broken"] = (panel["above_ma250"] < 0.5) & (panel["ma250_slope"] < 0)
    res["broken"] = []
    for lab, m in (("趋势完好", ~panel["broken"]), ("趋势破坏", panel["broken"])):
        for p in ("低位", "高位"):
            mm = m & (panel["pos2"] == p)
            if mm.sum() < 40:
                continue
            res["broken"].append({"trend": lab, "pos": p, **stats(panel.loc[mm, "fwd20"].to_numpy()),
                                  "boot": block_boot(panel, mm)})

    # 6) 分年（低位 20 日，扣成本）
    res["by_year"] = []
    for y, grp in panel.groupby(panel["date"].dt.year):
        lo = grp[grp["pos2"] == "低位"]["fwd20"].to_numpy()
        allv = grp["fwd20"].to_numpy()
        if len(lo) < 100:
            continue
        res["by_year"].append({"year": int(y), "low": stats(lo), "all": stats(allv)})

    # 7) 成本敏感性
    lo = panel.loc[panel["pos2"] == "低位", "fwd20"].to_numpy()
    res["cost_sens"] = {f"{c}bp": stats(lo, cost=c / 1e4)["ev_bp"] for c in (0, 3, 6, 10, 15, 25)}

    # 8) 行业异质性（低位 20 日）
    res["by_industry"] = []
    for ind, grp in panel[panel["pos2"] == "低位"].groupby("industry"):
        if len(grp) < 150:
            continue
        res["by_industry"].append({"industry": ind, **stats(grp["fwd20"].to_numpy())})
    res["by_industry"].sort(key=lambda r: -r["ev_bp"])

    # 9) 60 日（更慢的持有）
    res["dd60"] = []
    for lab in labels:
        res["dd60"].append({"bin": str(lab), **stats(panel.loc[panel["dd_bin"] == lab, "fwd60"].to_numpy())})

    # 10) 保存面板，供 step2（事件级 / 月度回测）复用
    tmp = ROOT / "analysis/_etf_asym_tmp"
    tmp.mkdir(exist_ok=True)
    panel.to_parquet(tmp / "panel.parquet", index=False)
    industry_nav(panel).to_parquet(tmp / "nav.parquet")

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {OUT}")

    def show(title, rows, keys):
        print(f"\n=== {title} ===")
        print(pd.DataFrame(rows)[keys].to_string(index=False))

    show("基准（扣 6bp）", [{"h": k, **v} for k, v in res["baseline"].items()],
         ["h", "n", "ev_bp", "med_bp", "p10_bp", "p90_bp", "win", "updn", "p_dn10", "p_up10"])
    show("按价格位置（20日）", res["by_dd"],
         ["bin", "n", "ev_bp", "med_bp", "p10_bp", "p90_bp", "win", "updn", "skew"])
    show("位置×成交结构（20日）", res["dd_x_vr"], ["pos", "vr", "n", "ev_bp", "win", "updn", "p_dn10"])
    show("市场环境（20日）", res["by_state"], ["state", "n", "ev_bp", "med_bp", "win", "updn", "p_dn10"])
    show("环境×位置（20日）", res["state_x_pos"], ["state", "pos", "n", "ev_bp", "win", "updn", "boot"])
    show("趋势破坏（20日）", res["broken"], ["trend", "pos", "n", "ev_bp", "win", "p_dn10", "boot"])
    show("分年低位（20日）", [{"y": r["year"], **r["low"]} for r in res["by_year"]],
         ["y", "n", "ev_bp", "med_bp", "win", "p_dn10"])
    show("行业异质性（低位20日）", res["by_industry"],
         ["industry", "n", "ev_bp", "med_bp", "win", "updn"])
    print("\n成本敏感性:", res["cost_sens"])


if __name__ == "__main__":
    main()
