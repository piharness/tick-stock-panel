"""把「变动前民营 → 国资取得控制权」的候选分成两档导出。

背景：详式报告的前后持股表只写**信息披露义务人自己**的持股变化，不写原控制人。
全量 5,265 个详式事件里，只有 4% 明写了上市公司原控制人（`报告明写` 213 条）。
所以按证据强度分两档：

  A 档（严格）：买方强国资 · 取得控制权 · 非无偿划转 · 报告未称「未变更」
  B 档（宽松）：A 档 + 名称含「投资控股/投资集团/产业投资」的平台类买方
                （地方国资平台常这么命名，但民企集团也这么命名，精度约五成）

输出：
  data/.scratch/state/prior_private_strict.csv
  data/.scratch/state/prior_private_broad.csv
  docs/structural-edge/prior-private-list.md
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"
TRANSFER = ("无偿划转", "国有股行政划转")


def main() -> None:
    p = pd.read_parquet(OUT / "prior_controller.parquet")
    p["method"] = p["method"].astype("string").fillna("")
    for c in ["acquires_control", "no_change"]:
        p[c] = p[c].fillna(False).astype(bool)
    p["date"] = pd.to_datetime(p["date"])

    cols = ["date", "code", "name", "discloser", "buyer_type", "prior_ctrl", "prior_type",
            "prior_basis", "new_ctrl", "new_holder", "seller", "pre_pct", "post_pct",
            "method", "doc_type", "title", "url"]

    base = p[(p["acquires_control"]) & (~p["no_change"]) & (~p["method"].isin(TRANSFER))]
    strict = base[base["buyer_type"] == "state"].copy()
    broad = base[base["buyer_type"].isin(["state", "platform"])].copy()

    strict["tier"] = "A 严格"
    broad["tier"] = "B 含平台"
    # 同一公司多次披露（分批/修订）只保留最早一次控制权变更
    strict = strict.sort_values("date").drop_duplicates(subset=["code"], keep="first").sort_values("date", ascending=False)
    broad = broad.sort_values("date").drop_duplicates(subset=["code"], keep="first").sort_values("date", ascending=False)

    strict[cols + ["tier"]].to_csv(OUT / "prior_private_strict.csv", index=False, encoding="utf-8-sig")
    broad[cols + ["tier"]].to_csv(OUT / "prior_private_broad.csv", index=False, encoding="utf-8-sig")

    print(f"严格（买方强国资）  {len(strict):>4} 个 / {strict['code'].nunique():>3} 只（按公司去重）")
    print(f"宽松（含平台）      {len(broad):>4} 个 / {broad['code'].nunique():>3} 只（按公司去重）")
    print("\n严格档按年：")
    print(strict.groupby(strict["date"].dt.year).size().to_string())
    print("\n严格档最近 15 条：")
    print(strict.head(15)[["date", "code", "name", "discloser", "new_ctrl", "pre_pct", "post_pct"]].to_string(index=False))

    md = ROOT / "docs" / "structural-edge" / "prior-private-list.md"
    lines = [
        f"# 变动前民营 → 国资取得控制权：候选清单\n",
        "> 口径：详式权益变动报告书 · 买方为国资 · 报告称取得控制权 · 非无偿划转 · 未称「控制人未变更」。\n",
        f"> 全量 5,265 个详式事件中，严格档 {len(strict)} 个 / {strict['code'].nunique()} 只；"
        f"含平台档 {len(broad)} 个 / {broad['code'].nunique()} 只。\n",
        "> **重要限制**：报告的前后持股表只写信息披露义务人自己的持股，不写原控制人；"
        "全量仅 4% 明写原控制人。所以本表是「国资取得控制权」，不等于「原控制人确为民营」。\n",
        "\n## 严格档（买方为国资委/财政/央企/城投类）\n",
        "| 日期 | 代码 | 简称 | 买方 | 新控制人 | 变动前% | 变动后% |",
        "|---|---|---|---|---|---:|---:|",
    ]
    for r in strict.itertuples():
        lines.append(
            f"| {r.date.date()} | {r.code} | {r.name} | {str(r.discloser)[:30]} | "
            f"{str(r.new_ctrl)[:18] if isinstance(r.new_ctrl, str) else '—'} | "
            f"{'' if pd.isna(r.pre_pct) else r.pre_pct} | {'' if pd.isna(r.post_pct) else r.post_pct} |"
        )
    lines += [
        "\n## 含平台档（另加名称含「投资控股/投资集团/产业投资」的买方）\n",
        "| 日期 | 代码 | 简称 | 买方 | 新控制人 |",
        "|---|---|---|---|---|",
    ]
    for r in broad[~broad["code"].isin(strict["code"])].itertuples():
        lines.append(
            f"| {r.date.date()} | {r.code} | {r.name} | {str(r.discloser)[:30]} | "
            f"{str(r.new_ctrl)[:18] if isinstance(r.new_ctrl, str) else '—'} |"
        )
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {OUT / 'prior_private_strict.csv'}")
    print(f"→ {OUT / 'prior_private_broad.csv'}")
    print(f"→ {md}")


if __name__ == "__main__":
    main()
