#!/usr/bin/env python3
"""_diag_moat_filter.py — 结构优势 moat 过滤器

回答一个具体问题：我发现的这条 edge，凭什么轮到我接，而不是更早/更快/更便宜的人？

四层淘汰链（每层代表「另一类人先把这段吃掉」）：
  速度层 — 日内/回转的 edge → 高频与量化吃掉（T+0 回转、horizon_sessions==0 的当日回合）
  容量层 — 能容纳大资金的 edge → 机构吃掉（中位市值/流动性足够大）
  舒适层 — 标的「干净」的 edge → 追基准的公募/被动也敢买，把溢价买贵
  成本层 — 纯量价公开信号 → 量化可廉价复制（半衰期短，靠监控退役）

对抗性总闸：若一只 5000 万规模的量化基金能廉价算出该信号并以更低成本执行 → 归它，不归你。

判定（自解释）：
  日内        → 归高频/量化，不是你的
  容量足够+干净 → 归机构/被动，不是你的
  慢+小+丑+公开 → 归你，但半衰期短（公开信号），必须监控衰减并配 regime 条件

纯 stdlib，不依赖 polars/后端，可直接 backend/.venv/Scripts/python.exe 运行。

数据来源：既有判据级结论均引自 docs/ 与项目长期记忆（见每条 source 字段）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

OUT_JSON = Path(__file__).with_name("_diag_moat_filter_result.json")
OUT_MD = Path(__file__).with_name("_diag_moat_filter_findings.md")


@dataclass
class Edge:
    id: str
    name: str
    source: str
    horizon: str
    is_intraday: bool        # 日内/回转 → 高频吃掉
    capacity_limited: bool   # 容量受限（小盘/低流动性/塞不进大资金）
    is_ugly: bool            # ST/退市/低流动性/争议标的 → 追基准的钱不敢碰
    signal_public: bool      # 纯量价公开信号 → 量化可廉价复制
    regime_sensitive: bool   # 是否已被观测到随 regime 翻转（如 2026 小市值溢价失效）
    alpha_bp: str
    t_stat: str
    status: str              # 已作废 / 被证伪 / 暴露 / 部分有效


@dataclass
class Verdict:
    edge_id: str
    verdict: str
    owner: str
    layer_results: list = field(default_factory=list)
    regime_note: str = ""


def moat_filter(e: Edge) -> Verdict:
    layers: list[tuple[str, str, str]] = []

    if e.is_intraday:
        layers.append(("速度层", "X", "日内/回转 → 高频与量化吃掉，轮不到你"))
    else:
        layers.append(("速度层", "O", "慢钱（≥1 日持有），高频拿不住"))

    if e.capacity_limited:
        layers.append(("容量层", "O", "容量受限（小盘/低流动性），大资金塞不进"))
    else:
        layers.append(("容量层", "X", "容量足够 → 机构吃掉"))

    if e.is_ugly:
        layers.append(("舒适层", "O", "标的丑/低流动性 → 追基准的公募/被动不敢碰"))
    else:
        layers.append(("舒适层", "X", "标的干净 → 被动/公募也敢买，溢价被买贵"))

    if e.signal_public:
        layers.append(("成本层", "!", "量价公开信号，量化可廉价复制 → 半衰期短，靠监控退役"))
    else:
        layers.append(("成本层", "O", "信息壁垒，量化难以廉价复制"))

    # 对抗性总闸 + 判定
    if e.is_intraday:
        verdict, owner = "归高频/量化，不是你的", "更快的人"
    elif not e.capacity_limited and not e.is_ugly:
        verdict, owner = "归机构/被动，不是你的", "更大/更便宜的人"
    elif e.signal_public:
        verdict, owner = "归你，但半衰期短（公开信号）", "你（需监控衰减）"
    else:
        verdict, owner = "归你（信息壁垒）", "你"

    regime_note = ""
    if e.regime_sensitive:
        regime_note = "regime 敏感：必须外挂条件门，否则 2026 式翻转会把「归你的慢钱」变成亏损"

    return Verdict(e.id, verdict, owner, layers, regime_note)


EDGES = [
    Edge(
        "l1_gap_reversal", "L1 跳空反转（20 日持有）",
        "docs/l1-gap-reversal.md · gross +96.02bp / t=20.49 / 22/22 年正",
        "20 日持有", False, True, False, True, True,
        "+96.02", "20.49", "部分有效",
    ),
    Edge(
        "silent_liquidity", "silent +606bp（流动性溢价承保暴露）",
        "docs/silent-excess-return-attribution.md · 归因后 = 小盘+低流动性投影，非沉默 alpha",
        "20 日持有", False, True, True, True, True,
        "+606（乐观上限）", "投影", "被证伪为投影 / 仍可作暴露",
    ),
    Edge(
        "t2_rebound", "T+2 跌后反弹（当日回合）",
        "docs/hotmoney-quant-game.md:85 · horizon_sessions=0，T+1 不可执行，已作废",
        "日内（T+2 开买→T+2 收卖）", True, False, False, True, False,
        "+0.85（口径作废）", "12.9→−5.95(改T+3)", "已作废",
    ),
    Edge(
        "dragon_tiger", "龙虎榜上榜 → 事后均值回归",
        "docs/dragon-tiger-org-vs-hotmoney.md · 上榜股 H20 −400~−500bp，不可提前交易",
        "20 日（负向）", False, False, False, True, False,
        "−400~−500（负向暴露）", "−7~−8", "负向暴露读数",
    ),
    Edge(
        "limitup_ignition", "涨停点火 → 4 板预测",
        "docs/limitup-citizenship-ignition.md · 点火不预测 4 板，是「小盘+10%制度」投影",
        "数日", False, False, False, True, False,
        "无 alpha", "AUC≈0.57", "被证伪",
    ),
]


def main() -> None:
    rows = []
    for e in EDGES:
        v = moat_filter(e)
        rows.append((e, v))

    # 控制台
    print("=" * 92)
    print("结构优势 moat 过滤器 — 每条 edge「凭什么轮到我」判定")
    print("=" * 92)
    for e, v in rows:
        print(f"\n[{e.id}] {e.name}")
        print(f"  来源: {e.source}")
        print(f"  判定: {v.verdict}")
        for layer, mark, why in v.layer_results:
            print(f"    [{mark}] {layer}: {why}")
        if v.regime_note:
            print(f"    ⚠ {v.regime_note}")

    # JSON
    out_json = []
    for e, v in rows:
        out_json.append({
            "id": e.id, "name": e.name, "source": e.source, "status": e.status,
            "verdict": v.verdict, "owner": v.owner,
            "layers": [{"layer": l, "mark": m, "why": w} for l, m, w in v.layer_results],
            "regime_note": v.regime_note,
        })
    OUT_JSON.write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown
    md = ["# 结构优势 moat 过滤器 — 判定结果", ""]
    md.append("> 一句话：只有「慢 + 小 + 丑 + 容量小」的 edge 才轮得到你，而这类 edge 天然 regime 敏感。")
    md.append("> 你的可持续性不来自 edge 本身，来自 regime 监控 + 快速退役。\n")
    md.append("| edge | 状态 | 判定 | 归谁 |")
    md.append("|---|---|---|---|")
    for e, v in rows:
        md.append(f"| {e.name} | {e.status} | {v.verdict} | {v.owner} |")
    md.append("")
    md.append("## 逐条")
    for e, v in rows:
        md.append(f"\n### {e.name}")
        md.append(f"- 来源：{e.source}")
        md.append(f"- 判定：**{v.verdict}**")
        for layer, mark, why in v.layer_results:
            md.append(f"- `[{mark}]` {layer}：{why}")
        if v.regime_note:
            md.append(f"- ⚠ {v.regime_note}")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    print(f"\n输出: {OUT_JSON}")
    print(f"输出: {OUT_MD}")


if __name__ == "__main__":
    main()
