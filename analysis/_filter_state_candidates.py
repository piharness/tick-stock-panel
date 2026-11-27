"""从全量《权益变动报告书》索引里筛出「国资入股 / 纾困」候选。

三个动作：
  1. 去噪：把财务顾问核查意见、法律意见书、独立财务顾问报告从「报告书」里拆出来
     （它们共享同一个标题词，但一个事件会产生 3–6 条，直接按条数统计会虚高 3 倍）。
  2. 分桶：报告书标题里出现的主体词（国资/国有/纾困/财政/城投/无偿划转/汇金…）。
  3. 召回率反测：用已知的 23 条国资增持种子（seed）检查筛子漏不漏——
     这一步比筛出多少条更重要，因为大部分报告书标题是通用的「简式权益变动报告书」。

输出：
  data/.scratch/state/equity_change_docs.parquet      去噪后的文档表（带 doc_type / flag）
  data/.scratch/state/equity_change_candidates.parquet 候选（flag=True）
  data/.scratch/state/filter_report.json              统计与召回率

用法：
    backend/.venv/Scripts/python.exe analysis/_filter_state_candidates.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"

# 文档类型：顺序有意义，先判噪声再判报告书
NOISE = {
    "财务顾问核查意见": r"财务顾问(核查意见|报告)|核查意见",
    "法律意见书": r"法律意见书",
    "独立财务顾问": r"独立财务顾问",
    "问询回复": r"问询函|回复|关注函",
}
REPORT = {
    "详式报告书": r"详式",
    "简式报告书": r"简式",
    "收购报告书": r"收购报告书|要约收购报告书",
    "提示性公告": r"提示性公告|权益变动提示",
    "报告书(未标)": r"权益变动报告书",
}

# 主体词桶
BUCKETS = {
    "state": r"国资|国有|国资委|国有资本|国有资产|国有股权|国有股东|国有企业|央企|中央企业|省属|市属|区属|财政局|财政厅|人民政府|管委会",
    "rescue": r"纾困|救助|帮扶|解困",
    "platform": r"城投|城建|交投|产投|国投|投资控股|投资集团|资本运营|国资运营|控股集团",
    "transfer": r"无偿划转|划转|协议转让|表决权委托|表决权放弃|司法拍卖|重整|要约收购",
    "finance": r"汇金|中证金融|国新|诚通|中国国投",
}

# 候选判定：出现「明确国资主体词」或「纾困」或「金融救助平台」；
# 或出现「城投类平台词」；或「无偿划转」（国企内部重组，单列一档）
STRONG = ("state", "rescue", "finance")


def classify(title: str) -> str:
    t = title or ""
    for name, pat in NOISE.items():
        if re.search(pat, t):
            return name
    for name, pat in REPORT.items():
        if re.search(pat, t):
            return name
    return "其他"


def main() -> None:
    src = OUT / "equity_change_index.parquet"
    df = pd.read_parquet(src)
    df["title"] = df["title"].fillna("")
    df["doc_type"] = df["title"].map(classify)
    df["is_report"] = df["doc_type"].isin(["详式报告书", "简式报告书", "收购报告书", "报告书(未标)"])

    # 分桶
    for name, pat in BUCKETS.items():
        df["hit_" + name] = df["title"].str.contains(pat, regex=True, na=False)
    df["hit_any_strong"] = df[["hit_" + k for k in STRONG]].any(axis=1)
    df["flag"] = df["hit_any_strong"] | df["hit_platform"] | df["hit_transfer"]

    # 事件级去重：同一 code + 同一天的多份文件（报告书 + 核查意见 + 法律意见）算一个事件
    ev = (
        df[df["is_report"]]
        .groupby(["code", "date"], as_index=False)
        .agg(
            name=("name", "first"),
            title=("title", "first"),
            n_docs=("id", "size"),
            flag=("flag", "any"),
            hit_any_strong=("hit_any_strong", "any"),
            hit_transfer=("hit_transfer", "any"),
            doc_type=("doc_type", "first"),
            url=("url", "first"),
        )
    )

    df.to_parquet(OUT / "equity_change_docs.parquet", index=False)
    cand = df[df["flag"] & df["is_report"]].copy()
    cand.to_parquet(OUT / "equity_change_candidates.parquet", index=False)

    # 召回率：23 条种子，看 ±120 天内是否有候选 / 是否有任何报告书
    seed_path = OUT / "increase_events_seed.csv"
    recall = {"n": 0, "in_index": 0, "in_report": 0, "in_candidate": 0, "miss": []}
    if seed_path.exists():
        seed = pd.read_csv(seed_path, dtype={"code": str})
        seed["code"] = seed["code"].str.zfill(6)
        seed["date"] = pd.to_datetime(seed["date"])
        idx = df.copy()
        idx["date"] = pd.to_datetime(idx["date"])
        rep = ev.copy()
        rep["date"] = pd.to_datetime(rep["date"])
        for r in seed.itertuples():
            lo, hi = r.date - pd.Timedelta(days=120), r.date + pd.Timedelta(days=120)
            a = idx[(idx["code"] == r.code) & idx["date"].between(lo, hi)]
            b = rep[(rep["code"] == r.code) & rep["date"].between(lo, hi)]
            c = cand[(cand["code"] == r.code) & (pd.to_datetime(cand["date"]).between(lo, hi))]
            recall["n"] += 1
            recall["in_index"] += int(len(a) > 0)
            recall["in_report"] += int(len(b) > 0)
            recall["in_candidate"] += int(len(c) > 0)
            if len(c) == 0:
                recall["miss"].append(
                    {"code": r.code, "name": r.name, "date": str(r.date.date()), "docs": int(len(a)), "reports": int(len(b))}
                )

    report = {
        "docs": int(len(df)),
        "codes": int(df["code"].nunique()),
        "span": [str(df["date"].min()), str(df["date"].max())],
        "by_doc_type": {k: int(v) for k, v in df["doc_type"].value_counts().items()},
        "events": int(len(ev)),
        "event_span": [str(ev["date"].min()), str(ev["date"].max())],
        "candidates_docs": int(len(cand)),
        "candidates_events": int(ev["flag"].sum()),
        "candidate_codes": int(cand["code"].nunique()),
        "by_bucket": {k: int(df["hit_" + k].sum()) for k in BUCKETS},
        "recall": recall,
    }
    (OUT / "filter_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"文档 {report['docs']:,} 条 / {report['codes']:,} 只 / {report['span'][0]} ~ {report['span'][1]}")
    print("\n文档类型：")
    for k, v in sorted(report["by_doc_type"].items(), key=lambda x: -x[1]):
        print(f"  {k:>12} {v:>7,}")
    print(f"\n事件（code+日期去重，仅报告书）{report['events']:,} 个")
    print(f"候选文档 {report['candidates_docs']:,} 条 / 候选事件 {report['candidates_events']:,} 个 / {report['candidate_codes']:,} 只")
    print("\n分桶：")
    for k, v in report["by_bucket"].items():
        print(f"  {k:>10} {v:>7,}")
    if recall["n"]:
        print(f"\n种子召回（{recall['n']} 条，±120 天）：")
        print(f"  在索引里      {recall['in_index']}/{recall['n']}")
        print(f"  有报告书      {recall['in_report']}/{recall['n']}")
        print(f"  进候选        {recall['in_candidate']}/{recall['n']}")
        for m in recall["miss"][:10]:
            print(f"  漏：{m}")


if __name__ == "__main__":
    main()
