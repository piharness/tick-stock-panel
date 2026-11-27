"""从全量解析结果里列出「国资披露人」事件。

做四件事：
  1. 分类：state / platform / finance / person / corp（复用解析脚本的规则）
  2. 归省：从披露人名称里抽省/直辖市/计划单列市，央企单列
  3. 去重：同一 code + 同一披露人，180 天内只留最早一条（修订稿/更新稿会重复披露）
  4. 导出：CSV（全量）+ 控制台摘要（按年 / 按省 / 按方式）

用法：
    backend/.venv/Scripts/python.exe analysis/_list_state_events.py
    backend/.venv/Scripts/python.exe analysis/_list_state_events.py --platform   # 含模糊平台
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
from _parse_equity_change_full import classify  # noqa: E402

OUT = ROOT / "data" / ".scratch" / "state"

PROVINCES = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽",
    "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西",
    "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆",
]
CITIES = ["深圳", "青岛", "大连", "宁波", "厦门", "苏州", "无锡", "佛山", "东莞", "温州", "泉州", "烟台"]
CENTRAL = re.compile(
    r"国务院|中央|中国国新|诚通|国家电网|南方电网|中国石油|中国石化|中海油|中国移动|中国电信|"
    r"中国联通|中国烟草|中国铁路|国铁|华润|招商局|中粮|保利|中船|兵器|航天|航空工业|中国电子|"
    r"中电科|中电太极|中核|中广核|大唐|华能|华电|国家电投|中国三峡|三峡集团|中车|中国建筑|中国铁建|中国交建|中国电建|"
    r"中国能建|中国化学|中国黄金|中国铝业|五矿|中钢|中煤|神华|国家能源|中远海运|中国国航|"
    r"东方航空|南方航空|中国商飞|中国船舶|中国重工|中国中冶|华侨城|中国旅游|汇金|中证金融|财政部|"
    r"国有资本风险投资|国投集团|国家开发投资"
)


def tier(name: str) -> str:
    if CENTRAL.search(name):
        return "央企/部委"
    if any(p in name for p in PROVINCES):
        return "省/直辖市"
    if any(c in name for c in CITIES):
        return "计划单列/地市"
    return "地市/区县"


def province(name: str) -> str:
    for p in PROVINCES:
        if p in name:
            return p
    for c in CITIES:
        if c in name:
            return c
    if CENTRAL.search(name):
        return "中央"
    return "未识别"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", action="store_true", help="把模糊平台也算进来")
    args = ap.parse_args()

    d = pd.read_parquet(OUT / "equity_change_parsed.parquet")
    d = d[d["ok"]].copy()

    PRI = {"state": 5, "platform": 4, "finance": 3, "person": 2, "corp": 1, "unknown": 0}

    def best(row) -> str:
        raw = row.get("disclosers")
        if not isinstance(raw, str) or not raw.strip():
            raw = row.get("discloser")
        if not isinstance(raw, str) or not raw.strip():
            return "unknown"
        parts = [x for x in raw.split("|") if x.strip()]
        return max((classify(x.strip()) for x in parts), key=lambda t: PRI.get(t, 0))

    d["atype"] = d.apply(best, axis=1)
    keep = ["state", "platform"] if args.platform else ["state"]
    s = d[d["atype"].isin(keep)].copy()
    s["date"] = pd.to_datetime(s["date"])
    s = s.sort_values(["code", "date"])

    # 同一 code + 同一披露人，180 天内只留最早（修订稿会重复）
    s["_who"] = s["discloser"].fillna("").str.slice(0, 12)
    s["_gap"] = s.groupby(["code", "_who"])["date"].diff().dt.days
    s = s[(s["_gap"].isna()) | (s["_gap"] > 180)].copy()

    # 当前第一大流通股东：判断「新进」还是「存量股东增减」
    tf = pd.read_parquet(sorted(glob.glob(str(ROOT / "data/shareholder/top_float/*.parquet")))[0],
                         columns=["code", "period_end", "holder_name", "rank"])
    tf = tf[tf["rank"] <= 10]
    latest = tf.groupby("code")["period_end"].transform("max")
    tf = tf[tf["period_end"] == latest]
    holder = tf.groupby(tf["code"].astype(str).str.zfill(6))["holder_name"].apply(list).to_dict()

    def existing(row) -> bool:
        a = str(row["discloser"] or "")[:6]
        if not a:
            return False
        return any(a in str(h) or str(h)[:6] in str(row["discloser"]) for h in holder.get(str(row["code"]).zfill(6), []))

    s["n_holders"] = s["code"].map(lambda c: len(holder.get(str(c).zfill(6), [])))
    s["is_current_holder"] = s.apply(existing, axis=1)
    s["control_change"] = s["controller_after"].notna() & (s["controller_after"] != "未变更")

    POLICY = re.compile(r"中央汇金|中国证券金融|财政部|国务院国资委|国有资产监督管理委员会$")

    def event_class(row) -> str:
        m = str(row.get("method") or "")
        a = str(row.get("discloser") or "")
        if m in ("无偿划转", "国有股行政划转") and POLICY.search(a):
            return "政策性划转"
        if m in ("无偿划转", "国有股行政划转"):
            return "无偿划转"
        if row.get("is_current_holder"):
            return "存量股东增减"
        return "新进"

    s["event_class"] = s.apply(event_class, axis=1)
    s["prov"] = s["discloser"].fillna("").map(province)
    s["tier"] = s["discloser"].fillna("").map(tier)
    s.loc[s["tier"] == "央企/部委", "prov"] = "中央"
    s["year"] = s["date"].dt.year

    cols = ["date", "code", "name", "discloser", "disclosers", "atype", "event_class", "tier", "prov",
            "is_current_holder", "control_change", "method", "direction", "controller_after",
            "doc_type", "title", "url"]
    out = s[cols].sort_values("date", ascending=False)
    path = OUT / ("state_events.csv" if not args.platform else "state_events_with_platform.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")

    print(f"事件 {len(out):,}（{'含平台' if args.platform else '仅强国资'}）· 股票 {out['code'].nunique():,}")
    print(f"区间 {out['date'].min().date()} ~ {out['date'].max().date()}")
    print(f"\n按年：")
    print(out.groupby(out["date"].dt.year).size().to_string())
    print(f"\n事件类型：")
    print(out["event_class"].value_counts().to_string())
    print(f"\n新进 vs 存量（对当前前十大股东）：")
    print(out["is_current_holder"].map({True: "前十大股东之一", False: "不在前十大"}).value_counts().to_string())
    print(f"\n控制权变更标记：{int(out['control_change'].sum()):,} 条")
    print(f"\n按层级：")
    print(out["tier"].value_counts().to_string())
    print(f"\n按省：")
    print(out["prov"].value_counts().head(15).to_string())
    print(f"\n按方式：")
    print(out["method"].value_counts(dropna=False).head(8).to_string())
    fresh = out[~out["is_current_holder"]]
    print(f"\n最近 20 条「不在前十大股东」：")
    print(fresh.head(20)[["date", "code", "name", "discloser", "tier", "control_change"]].to_string(index=False))
    print(f"\n→ {path}")

    # Markdown 全表（按年倒序，便于在预览面板里读）
    md = ROOT / "docs" / "structural-edge" / "state-events-list.md"
    lines = [
        f"# 国资披露人事件清单（{len(out):,} 个）\n",
        f"> 来源：巨潮《权益变动报告书》全量 {len(d):,} 个事件解析；"
        f"强国资 {len(d[d['atype'] == 'state']):,} 条 → 同股同人 180 天去重后 {len(out):,} 个。\n"
        f"> 区间 {out['date'].min().date()} ~ {out['date'].max().date()}，涉及 {out['code'].nunique():,} 只股票。\n",
        "## 分层\n",
        "| 类型 | 数量 |\n|---|---:|",
        *[f"| {k} | {v} |" for k, v in out["event_class"].value_counts().items()],
        "\n## 全部事件\n",
        "| 日期 | 代码 | 简称 | 披露人 | 类型 | 层级 | 省 | 方式 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in out.itertuples():
        lines.append(
            f"| {r.date.date()} | {r.code} | {r.name} | {str(r.discloser)[:38]} | {r.event_class} | "
            f"{r.tier} | {r.prov} | {r.method if isinstance(r.method, str) else '—'} |"
        )
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {md}")


if __name__ == "__main__":
    main()
