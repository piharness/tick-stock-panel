# -*- coding: utf-8 -*-
"""交叉检验：深跌质押股 ∩ 买点六问通过集 = ？

问题（prereg-pledge-drift.md §8.3 写死的升级前置）：
    深跌质押股的「深跌反弹」(30.7%, 平均 exc60 +1473bp) 有多少本来会通过买点六问？
    若从不通过 → 误杀机会成本 = 0 → 可升级硬排除。

买点六问（frontend/src/pages/structural-edge/data.ts, buyQuestions）：
    第 1 问「载体：行业 ETF，不买个股」——载体是硬性排除，个股连第 1 问都过不了。

本脚本不做「历史事件逐条重跑」，因为载体差异是**规则定义级**的：
    买点六问的载体 ∈ {行业 ETF}，深跌质押股 ∈ {A 股个股}，两个证券代码空间不相交。
这里只用数据把两个集合的实际代码前缀摆出来，证明交集为空，并把结论固化。
"""
import json
from pathlib import Path

import pandas as pd
import numpy as np

OUT = Path("data/.scratch/leverage")
OUT.mkdir(parents=True, exist_ok=True)

# ---------- 1) 深跌质押股集合 ----------
ev = pd.read_parquet(OUT / "pledge_drift_events.parquet")
deep = ev[ev["drift"] < -0.30].copy()
deep_syms = set(deep["symbol"].astype(str).unique())
all_syms = set(ev["symbol"].astype(str).unique())

def prefix_dist(syms):
    d = {}
    for s in syms:
        p = s[:3]
        d[p] = d.get(p, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))

def is_etf(s: str) -> bool:
    # A 股 ETF 代码：51xxxx.SH / 15xxxx.SZ / 58xxxx.SH / 159xxx.SZ / 56xxxx.SH 等
    code = s.split(".")[0]
    return code.startswith(("51", "15", "56", "58", "159"))

# ---------- 2) 买点六问通过集载体 ----------
# 通过集载体 = 行业 ETF 池（replay.json 的实际载体 + 页面声明的载体空间）
rep = json.load(open("data/etf_watch/replay.json", encoding="utf-8"))
rep_s = json.dumps(rep, ensure_ascii=False)
import re
etf_syms = set(re.findall(r'"symbol"\s*:\s*"([^"]+)"', rep_s))

# 页面声明的行业 ETF 池：position.sample = "1,720 只 ETF（712 只归 20 个行业）"
# 用数据里的 ETF 载体（replay）作为「通过集实际载体」的代表即可，因为第 1 问已把载体钉死为 ETF。

result = {
    "deep_pledge": {
        "n_events": int(len(deep)),
        "n_symbols": int(len(deep_syms)),
        "prefix_dist": prefix_dist(deep_syms),
        "n_etf": int(sum(is_etf(s) for s in deep_syms)),
    },
    "all_pledge": {
        "n_symbols": int(len(all_syms)),
        "n_etf": int(sum(is_etf(s) for s in all_syms)),
    },
    "buy_set_carrier": {
        "replay_etf_symbols": sorted(etf_syms),
        "n_etf": int(len(etf_syms)),
        "n_stock": int(sum(not is_etf(s) for s in etf_syms)),
    },
    "rule_level": {
        "buy_q1": "载体：行业 ETF，不买个股（data.ts buyQuestions[0]）",
        "verdict": "结构性空集：买点六问载体=ETF，深跌质押股=个股，两代码空间不相交",
    },
    "overlap": {
        "n_intersection": int(len(deep_syms & etf_syms)),
        "opportunity_cost_of_miss": "= 0（结构性为零，非『可能有限』）",
    },
    "conclusion": (
        "深跌质押股 100% 是 A 股个股（0 只 ETF）；买点六问第 1 问把载体钉死为行业 ETF。"
        "两者交集恒为空，不是历史巧合，是规则定义。因此『误杀 +1473bp』从不构成机会成本——"
        "那 30.7% 的深跌反弹牛股本来就不会进入买点池。升级条件满足：可升级为硬排除，"
        "但硬排除对当前 ETF 买点的增量约束 = 0（个股本就不买）；其真实定位是『若未来买点扩展到个股』"
        "才生效的硬排除规则 + 独立的个股风险研究结论。"
    ),
}

(OUT / "pledge_buyset_overlap.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False, indent=2))
