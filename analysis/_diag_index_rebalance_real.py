#!/usr/bin/env python3
"""_diag_index_rebalance_real.py — 指数调样「名单驱动」事件研究（真实名单）

数据源：中证指数公司官网调样结果公告附件 PDF（csindex.com.cn），已下载到 data/_csindex/。
覆盖 4 次定期调样：2024-06 / 2024-12 / 2025-06 / 2025-12（沪深300/中证500/中证1000/A50/A100/A500）。

这是账本第 1 类被迫流的「正确打开方式」：不再用异常换手近似，直接用真实调入/调出名单，
测「价格压力 → 反转」（Shleifer 1986 / Harris-Gurel 1986）：
  调出股（被动基金被迫卖出）→ 生效日附近砸坑 → 之后回补
  调入股（被动基金被迫买入）→ 生效日附近抬价 → 之后回落

窗口（T = 生效日收市）：
  pre20  = T-20→T      （调样前的预期/抢跑）
  ret_0  = T→T+1       （生效后次日反应）
  fwd20  = T+1→T+20    （反转窗口）
  fwd60  = T+1→T+60
超额 = 个股 - 当日全市场中位数（等权）。

产出：data/_csindex/rebalance_events.parquet + analysis/_diag_index_rebalance_real_result.json
运行：backend/.venv/Scripts/python.exe analysis/_diag_index_rebalance_real.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import polars as pl

CSINDEX = Path("data/_csindex")
ENR = Path("data/kline_daily_enriched")
OUT_JSON = Path(__file__).with_name("_diag_index_rebalance_real_result.json")
OUT_EV = Path("data/_csindex/rebalance_events.parquet")

EFFECTIVE = {
    "202406": "2024-06-14",
    "202412": "2024-12-13",
    "202506": "2025-06-13",
    "202512": "2025-12-12",
}

INDEX_MARKERS = [
    ("沪深 300", "沪深300"), ("中证 500", "中证500"), ("中证 1000", "中证1000"),
    ("中证 A50", "中证A50"), ("中证 A100", "中证A100"), ("中证 A500", "中证A500"),
]


def normalize_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    return code


def parse_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # 去掉换页符，统一空白
    text = text.replace("\u000c", " ")
    rows: list[dict] = []
    for marker, idx_name in INDEX_MARKERS:
        # 定位该指数的调整名单段
        m = re.search(re.escape(marker) + r"\s*指数样本调整名单", text)
        if not m:
            continue
        # 段结束 = 下一个指数标记 或 备选名单
        seg_start = m.end()
        next_markers = [re.escape(x) + r"\s*指数" for x, _ in INDEX_MARKERS] + [r"备选名单"]
        seg_end = len(text)
        for nm in next_markers:
            nm_pos = text.find(nm if nm != r"备选名单" else "备选名单", seg_start + 5)
            # 简化：找所有后续 section 头，取最近
        # 更稳的切段：找「备选名单」或下一个「指数样本调整名单」
        alt = []
        for x, _ in INDEX_MARKERS:
            for mm in re.finditer(re.escape(x) + r"\s*指数样本调整名单", text):
                if mm.start() > seg_start:
                    alt.append(mm.start())
        for mm in re.finditer(r"备选名单", text):
            if mm.start() > seg_start:
                alt.append(mm.start())
        if alt:
            seg_end = min(alt)
        seg = text[seg_start:seg_end]
        # 提取 (代码, 名称) 对。注意：不能用 \b（Python3 中 \b 把中文字符当 \w，
        # 紧跟中文的代码会被漏掉），改用前后非数字断言。
        codes = [(mm.group(1), mm.start(), mm.end()) for mm in re.finditer(r"(?<!\d)(\d{6})(?!\d)", seg)]
        pairs = []
        for i in range(len(codes)):
            code = codes[i][0]
            end = codes[i][2]
            nxt = codes[i + 1][1] if i + 1 < len(codes) else len(seg)
            name = re.sub(r"\s+", "", seg[end:nxt])
            if name:
                pairs.append((code, name))
        # 调出 = 偶数位，调入 = 奇数位（PDF 表格按行「调出|调入」交错）
        for i, (code, name) in enumerate(pairs):
            action = "调出" if i % 2 == 0 else "调入"
            rows.append({"index": idx_name, "action": action, "code": code, "name": name})
    return rows


def load_panel() -> pl.DataFrame:
    df = (
        pl.scan_parquet(str(ENR / "**" / "*.parquet"), extra_columns="ignore")
        .select(["symbol", "date", "close"])
        .collect()
    )
    return df.unique(subset=["symbol", "date"], keep="last").sort(["symbol", "date"])


def add_ret_features(panel: pl.DataFrame) -> pl.DataFrame:
    panel = panel.with_columns(
        pl.col("close").shift(20).over("symbol").alias("px_pre20"),
        pl.col("close").shift(-1).over("symbol").alias("px_1"),
        pl.col("close").shift(-20).over("symbol").alias("px_f20"),
        pl.col("close").shift(-60).over("symbol").alias("px_f60"),
    )
    panel = panel.with_columns(
        (pl.col("close") / pl.col("px_pre20") - 1).alias("pre20"),
        (pl.col("px_1") / pl.col("close") - 1).alias("ret_0"),
        (pl.col("px_f20") / pl.col("px_1") - 1).alias("fwd20"),
        (pl.col("px_f60") / pl.col("px_1") - 1).alias("fwd60"),
    )
    return panel


def main() -> None:
    # 1) 解析真实名单
    all_rows: list[dict] = []
    for key, eff in EFFECTIVE.items():
        txt = CSINDEX / f"adjust_{key}.txt"
        if not txt.exists():
            continue
        for r in parse_file(txt):
            r["effective"] = eff
            r["symbol"] = normalize_symbol(r["code"])
            all_rows.append(r)
    events = pl.DataFrame(all_rows)
    print(f"解析事件总数: {events.height}")
    print(events.group_by(["index", "action"]).agg(pl.len().alias("n")).sort(["index", "action"]))

    # 2) 载入行情，算收益特征
    panel = add_ret_features(load_panel())
    events = events.with_columns(pl.col("effective").cast(pl.Date).alias("eff_date"))
    # 3) join 事件到生效日的行情
    ev = events.join(
        panel.select(["symbol", "date", "close", "pre20", "ret_0", "fwd20", "fwd60"]),
        left_on=["symbol", "eff_date"], right_on=["symbol", "date"], how="left",
    )
    matched = ev.filter(pl.col("close").is_not_null())
    print(f"匹配到行情的事件: {matched.height} / {events.height}")

    # 4) 市场基准：每日 fwd20/fwd60 中位数
    mkt = panel.group_by("date").agg(
        pl.col("fwd20").median().alias("mkt_fwd20"),
        pl.col("fwd60").median().alias("mkt_fwd60"),
        pl.col("ret_0").median().alias("mkt_ret0"),
    )
    matched = matched.join(mkt, left_on="eff_date", right_on="date", how="left")
    matched = matched.with_columns(
        (pl.col("fwd20") - pl.col("mkt_fwd20")).alias("ex_fwd20"),
        (pl.col("fwd60") - pl.col("mkt_fwd60")).alias("ex_fwd60"),
        (pl.col("ret_0") - pl.col("mkt_ret0")).alias("ex_ret0"),
    )
    matched.write_parquet(OUT_EV)

    # 5) 汇总
    result = []
    for action in ["调入", "调出"]:
        g = matched.filter(pl.col("action") == action)
        n = g.height
        if n == 0:
            continue
        result.append({
            "action": action, "n": n,
            "pre20_mean_bp": round(g["pre20"].mean() * 1e4, 1),
            "ret0_mean_bp": round(g["ret_0"].mean() * 1e4, 1),
            "ret0_t": round(g["ret_0"].mean() / (g["ret_0"].std() / n**0.5), 2),
            "ex_fwd20_mean_bp": round(g["ex_fwd20"].mean() * 1e4, 1),
            "ex_fwd20_median_bp": round(g["ex_fwd20"].median() * 1e4, 1),
            "ex_fwd20_t": round(g["ex_fwd20"].mean() / (g["ex_fwd20"].std() / n**0.5), 2),
            "ex_fwd60_mean_bp": round(g["ex_fwd60"].mean() * 1e4, 1),
            "ex_fwd60_t": round(g["ex_fwd60"].mean() / (g["ex_fwd60"].std() / n**0.5), 2),
        })
    OUT_JSON.write_text(json.dumps({"events": events.height, "matched": matched.height, "result": result},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 90)
    print("指数调样「名单驱动」事件研究 — 生效日附近反应与反转（bp）")
    print("=" * 90)
    print(f"{'方向':<6}{'n':>6}{'pre20':>9}{'次日ret0':>10}{'次日t':>7}{'ex_fwd20':>10}{'中位':>8}{'t':>6}{'ex_fwd60':>10}{'t':>6}")
    for r in result:
        print(f"{r['action']:<6}{r['n']:>6}{r['pre20_mean_bp']:>9}{r['ret0_mean_bp']:>10}{r['ret0_t']:>7}"
              f"{r['ex_fwd20_mean_bp']:>10}{r['ex_fwd20_median_bp']:>8}{r['ex_fwd20_t']:>6}"
              f"{r['ex_fwd60_mean_bp']:>10}{r['ex_fwd60_t']:>6}")

    # 6) 分指数看调出反转
    print("\n分指数（调出股 20 日超额反转）:")
    for idx in ["沪深300", "中证500", "中证1000"]:
        for action in ["调出", "调入"]:
            g = matched.filter((pl.col("index") == idx) & (pl.col("action") == action))
            if g.height == 0:
                continue
            t = g["ex_fwd20"].mean() / (g["ex_fwd20"].std() / g.height**0.5) if g["ex_fwd20"].std() > 0 else 0
            print(f"  {idx} {action}: n={g.height:>3}  ex_fwd20={g['ex_fwd20'].mean()*1e4:>7.1f}bp  t={t:.2f}")

    print(f"\n事件明细: {OUT_EV}")
    print(f"JSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
