"""持有人面板：把「筹码」从价格分布，扩成「谁持有、带什么约束、和谁重叠」。

只做描述，不做判据。输入 data/shareholder/top_float（十大流通股东，季度），
输出：
  - holder_panel.parquet：股票 × 季度的持有人结构
  - market_holder.json：最新季度的市场级中位数、类型构成、网络统计

口径与边界：
  - top_float 只覆盖前十大流通股东，不是全流通盘；比例是「占总股本 %」。
  - holder_type 是数据源分类，按语义归并成 8 组；无法归类的进 corporate。
  - 网络是「共同持有人」的粗代理：holder_degree = 该持有人在本季度持有多少只股票；
    shared_score = Σ (degree_h - 1) * ratio_h。它不是完整的基金持仓网络。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "shareholder" / "top_float" / "part.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
OUT = ROOT / "data" / ".scratch" / "chip_age"
OUT.mkdir(parents=True, exist_ok=True)

GROUP = {
    "保险产品": "insurance",
    "保险公司": "insurance",
    "证券投资基金": "fund",
    "基金管理公司": "fund",
    "基金资产管理计划": "fund",
    "QFII": "qfii",
    "全国社保基金": "social",
    "基本养老基金": "social",
    "企业年金": "social",
    "私募基金": "private",
    "集合理财计划": "private",
    "信托计划": "private",
    "其他理财产品": "private",
    "证券公司": "broker",
    "个人": "individual",
}

# 托管/结算/证金类账户不是「一个决策人」：它们把几千只股票机械地连在一起。
# 不剔除，共同持有人网络会退化成「所有股票都连着香港中央结算」。
NOMINEE_PAT = (
    "香港中央结算|HKSCC|中央结算|中国证券金融|中央汇金|证金公司|"
    "高盛|UBS|MORGAN|J.P.|JPMORGAN|BARCLAYS|MERRILL|CITIGROUP|"
    "STATE STREET|BNP|DEUTSCHE|CREDIT SUISSE|UBS AG|"
    "中信证券资产管理(香港)|客户资金"
)


def main() -> None:
    df = pd.read_parquet(SRC)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["holder_type"] = df["holder_type"].fillna("其它").astype(str)
    df["group"] = df["holder_type"].map(GROUP).fillna("corporate")
    df["ratio"] = pd.to_numeric(df["ratio"], errors="coerce").fillna(0.0).clip(0, 100)
    df["is_nominee"] = df["holder_name"].astype(str).str.contains(NOMINEE_PAT, case=False, na=False)

    inst = pd.read_parquet(INST)[["code", "symbol", "name"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    df = df.merge(inst, on="code", how="left")

    # 股票 × 季度聚合
    def agg_one(g: pd.DataFrame) -> pd.Series:
        top = g["ratio"].sum()
        nominee = g.loc[g["is_nominee"], "ratio"].sum()
        real = g[~g["is_nominee"]]
        real_top = real["ratio"].sum()
        out = {
            "n_top": len(g),
            "top10_ratio": top,
            "nominee_ratio": float(nominee),
            "real_top10_ratio": float(real_top),
            "top1_ratio": g["ratio"].max(),
            "hhi": float(((g["ratio"] / 100.0) ** 2).sum()),
            "n_org": int((g["is_org"].astype(str) == "1").sum()),
        }
        for k in ["insurance", "fund", "qfii", "social", "private", "broker", "individual", "corporate"]:
            v = real.loc[real["group"] == k, "ratio"].sum()
            out[f"{k}_ratio"] = float(v)
            out[f"{k}_share"] = float(v / real_top) if real_top > 0 else np.nan
        return pd.Series(out)

    panel = (
        df.groupby(["period_end", "code", "symbol", "name"], dropna=False)
        .apply(agg_one, include_groups=False)
        .reset_index()
    )

    # 网络：只对「真实持有人」算度；托管/结算账户单独放，不参与共同持有人
    real = df[~df["is_nominee"]].copy()
    deg = (
        real.groupby(["period_end", "holder_name"])["code"]
        .nunique()
        .rename("holder_degree")
        .reset_index()
    )
    real = real.merge(deg, on=["period_end", "holder_name"], how="left")
    real["shared_weight"] = np.maximum(real["holder_degree"] - 1, 0) * real["ratio"]
    net = (
        real.groupby(["period_end", "code"])
        .agg(
            shared_score=("shared_weight", "sum"),
            n_shared_holders=("holder_degree", lambda x: int((x > 1).sum())),
            max_holder_degree=("holder_degree", "max"),
        )
        .reset_index()
    )
    panel = panel.merge(net, on=["period_end", "code"], how="left")
    panel.to_parquet(OUT / "holder_panel.parquet", index=False)

    # 市场级：最新季度
    latest = panel[panel["period_end"] == panel["period_end"].max()].copy()
    latest = latest[latest["top10_ratio"] > 0]
    q = lambda s, p: float(s.quantile(p)) if len(s) else np.nan

    def med(s):
        return float(s.median()) if len(s) else np.nan

    # 按季度加总的比例（全市场、剔除托管/结算后的 top10 口径）
    trend = []
    for pe, g in panel.groupby("period_end"):
        real_top = g["real_top10_ratio"].sum()
        row = {
            "period_end": str(pe.date()),
            "n": int(len(g)),
            "top10_ratio_median": round(med(g["top10_ratio"]), 2),
            "nominee_ratio_median": round(med(g["nominee_ratio"]), 2),
        }
        for k in ["insurance", "fund", "qfii", "social", "private", "broker", "individual", "corporate"]:
            row[f"{k}_share"] = round(float(g[f"{k}_ratio"].sum() / real_top), 4) if real_top > 0 else np.nan
        trend.append(row)

    corp = latest["corporate_ratio"].sum()
    real_top_all = latest["real_top10_ratio"].sum()
    summary = {
        "as_of": str(panel["period_end"].max().date()),
        "n_stocks": int(len(latest)),
        "top10_ratio_median": round(med(latest["top10_ratio"]), 2),
        "nominee_ratio_median": round(med(latest["nominee_ratio"]), 2),
        "real_top10_ratio_median": round(med(latest["real_top10_ratio"]), 2),
        "top1_ratio_median": round(med(latest["top1_ratio"]), 2),
        "hhi_median": round(med(latest["hhi"]), 4),
        "n_org_median": round(med(latest["n_org"]), 1),
        "fund_share_median": round(med(latest["fund_share"]), 4),
        "insurance_share_median": round(med(latest["insurance_share"]), 4),
        "individual_share_median": round(med(latest["individual_share"]), 4),
        "qfii_share_median": round(med(latest["qfii_share"]), 4),
        "fund_share_of_active": round(float(latest["fund_ratio"].sum() / max(real_top_all - corp, 1)), 4),
        "insurance_share_of_active": round(float(latest["insurance_ratio"].sum() / max(real_top_all - corp, 1)), 4),
        "individual_share_of_active": round(float(latest["individual_ratio"].sum() / max(real_top_all - corp, 1)), 4),
        "shared_score_median": round(med(latest["shared_score"]), 2),
        "shared_score_p90": round(q(latest["shared_score"], 0.90), 2),
        "n_shared_holders_median": round(med(latest["n_shared_holders"]), 1),
        "n_shared_holders_p90": round(q(latest["n_shared_holders"], 0.90), 1),
        "max_holder_degree_median": round(med(latest["max_holder_degree"]), 1),
        "trend": trend,
        "top_shared": [
            {"symbol": r["symbol"], "name": r["name"], "shared_score": round(float(r["shared_score"]), 1),
             "n_shared": int(r["n_shared_holders"]), "max_degree": int(r["max_holder_degree"])}
            for _, r in latest.nlargest(10, "shared_score").iterrows()
        ],
        "top_holders": [
            {"holder": r["holder_name"], "degree": int(r["holder_degree"])}
            for _, r in deg[deg["period_end"] == deg["period_end"].max()].nlargest(10, "holder_degree").iterrows()
        ],
    }
    (OUT / "market_holder.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("trend", "top_shared", "top_holders")},
                     ensure_ascii=False, indent=2))
    print("trend:", json.dumps(summary["trend"], ensure_ascii=False))
    print("top shared:", json.dumps(summary["top_shared"], ensure_ascii=False))
    print("top holders:", json.dumps(summary["top_holders"], ensure_ascii=False))


if __name__ == "__main__":
    main()
