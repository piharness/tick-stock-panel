"""T1 · 行业框架是否产生收益：同一评分、四个排序框架的对照。

预注册见 docs/t1-industry-frame-prereg.md（口径改动必须回填其 §7）。

四个臂（每臂只改"分组"这一操作）：
  all   全市场 score 前 20%
  ind   每个行业(同花顺 90 组)内前 20%，行业权重 = 基准行业权重
  inddm 行业内去均值后全市场前 20%
  rand  负控：按 md5(symbol) 切成规模分布与真实行业逐一对应的伪行业，组内前 20%

评分冻结为 footprint 2026-11-23 的 heat（7 项横截面 rank 等权），方向取 −heat（低热度为多头）。
输出 analysis/_diag_t1_industry_frame_result.json 与 _yearly.csv。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / ".scratch" / "footprint20d" / "panel.parquet"
OUT_JSON = ROOT / "analysis" / "_diag_t1_industry_frame_result.json"
OUT_CSV = ROOT / "analysis" / "_diag_t1_industry_frame_yearly.csv"

HEAT_COLS = ["turn20", "vol20", "ret20", "limit_up_cnt20", "range20", "turn_chg", "amt_chg"]
FRAC = 0.20
COST_MAIN = 0.0012
COST_ALT = 0.0020
BOOT = 1000
SEED = 20261125


def load() -> pd.DataFrame:
    p = pd.read_parquet(
        PANEL,
        columns=["symbol", "date", "industry", "ok", "anchor", "di", "fwd20"] + HEAT_COLS,
    )
    p = p[(p["ok"] == 1) & p["anchor"]].copy()
    p["date"] = pd.to_datetime(p["date"])
    # bench_mb 未落盘；按建构同式重算：当日 ok 池 fwd20 等权均值（anchor 是日期属性，不改变池）
    p["bench"] = p.groupby("date")["fwd20"].transform("mean")
    return p.sort_values(["date", "symbol"]).reset_index(drop=True)


def add_scores(p: pd.DataFrame) -> pd.DataFrame:
    for c in HEAT_COLS:
        p["z_" + c] = p.groupby("date")[c].rank(pct=True) * 2 - 1
    p["heat"] = p[["z_" + c for c in HEAT_COLS]].mean(axis=1)
    return p


def _hash_key(sym: str) -> str:
    return hashlib.md5(sym.encode()).hexdigest()


def random_groups(sub: pd.DataFrame) -> pd.Series:
    """把当日股票按 md5 排序，按真实行业规模（降序）逐段切成同样大小的伪行业。"""
    sizes = sub["industry"].value_counts().sort_values(ascending=False).to_numpy()
    order = sub["symbol"].map(_hash_key).sort_values().index
    groups = np.empty(len(order), dtype=object)
    pos = 0
    for gi, size in enumerate(sizes):
        idx = order[pos : pos + size]
        groups[sub.index.get_indexer(idx)] = f"r{gi:02d}"
        pos += size
    out = pd.Series(groups, index=sub.index)
    return out


def portfolio(sub: pd.DataFrame, score: str, arm: str) -> tuple[float, dict]:
    """返回 (组合毛收益, 诊断信息)。"""
    n = len(sub)
    k_all = max(1, int(round(FRAC * n)))
    diag = {"n": n, "k": k_all}
    if arm == "all":
        sel = sub.nlargest(k_all, score)
        return float(sel["fwd20"].mean()), diag
    if arm == "inddm":
        s = sub[score] - sub.groupby("industry")[score].transform("mean")
        sel = sub.assign(_s=s).nlargest(k_all, "_s")
        return float(sel["fwd20"].mean()), diag
    if arm == "contra":
        # 负控：全市场反向，只用于确认 all 臂的方向敏感
        sel = sub.nsmallest(k_all, score)
        return float(sel["fwd20"].mean()), diag
    # ind / rand：分行业配额 + 行业权重 = 基准行业权重
    gcol = "industry" if arm == "ind" else "_rg"
    ret = 0.0
    for g, gsub in sub.groupby(gcol):
        ng = len(gsub)
        kg = max(1, int(round(FRAC * ng)))
        sel = gsub.nlargest(kg, score)
        w = (ng / n) / kg
        ret += w * float(sel["fwd20"].mean())
    diag["groups"] = sub[gcol].nunique()
    return ret, diag


def build_cohorts(p: pd.DataFrame, score: str) -> pd.DataFrame:
    rows = []
    for d, sub in p.groupby("date", sort=True):
        sub = sub[sub["fwd20"].notna() & sub[score].notna()]
        if len(sub) < 200:
            continue
        sub = sub.copy()
        sub["_rg"] = random_groups(sub)
        rec = {"date": d, "bench": float(sub["bench"].iloc[0]), "n": len(sub)}
        rec["all"], diag = portfolio(sub, score, "all")
        rec["ind"], _ = portfolio(sub, score, "ind")
        rec["inddm"], _ = portfolio(sub, score, "inddm")
        rec["rand"], _ = portfolio(sub, score, "rand")
        # 反方向臂：score 最小 20%（run A=高热度，run B=低热度），检查方向是否真的反向
        rec["contra"], _ = portfolio(sub, score, "contra")
        rec["k"] = diag["k"]
        # ind 臂实际持股数（校验四臂持股数是否可比）
        rec["n_ind"] = int(sum(max(1, round(FRAC * len(g))) for _, g in sub.groupby("industry")))
        # all 臂的行业集中度与漂移方向
        w = sub.assign(w=1.0 / diag["k"]).groupby("industry")["w"].sum().sort_values(ascending=False)
        rec["all_top3_ind_w"] = float(w.head(3).sum())
        rec["all_top1"] = str(w.index[0])
        rec["all_top1_w"] = float(w.iloc[0])
        rec["all_ind_hhi"] = float((w**2).sum())
        # ind 臂隐含的行业权重应等于基准
        rows.append(rec)
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    out["di"] = np.arange(len(out))
    return out


def stats(x: np.ndarray, lag: int = 4) -> dict:
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 5:
        return {"n": n, "mean": None, "t": None}
    se = x.std(ddof=1) / np.sqrt(n)
    # Newey-West：锚点每 5 日一个、持有 20 日 ⇒ 重叠 4 期，滞后取 4
    d = x - x.mean()
    s0 = float(d @ d) / n
    var = s0
    for L in range(1, lag + 1):
        w = 1 - L / (lag + 1)
        var += 2 * w * float(d[L:] @ d[:-L]) / n
    var = max(var, 1e-12)
    nw_t = float(x.mean() / np.sqrt(var / n))
    return {"n": n, "mean": float(x.mean()), "se": float(se),
            "t": float(x.mean() / se) if se > 0 else None, "nw_t": nw_t}


def year_block_boot(df: pd.DataFrame, col: str) -> dict:
    rng = np.random.default_rng(SEED)
    years = sorted(df["year"].unique())
    means = df.groupby("year")[col].mean()
    draws = []
    for _ in range(BOOT):
        pick = rng.choice(years, size=len(years), replace=True)
        draws.append(float(means.loc[pick].mean()))
    return {"p05": float(np.percentile(draws, 5)), "p95": float(np.percentile(draws, 95)), "n_years": len(years)}


def summarize(df: pd.DataFrame, tag: str) -> dict:
    df = df.copy()
    df["year"] = df["date"].dt.year
    out = {"tag": tag, "cohorts": len(df), "date_min": str(df["date"].min().date()), "date_max": str(df["date"].max().date())}
    out["avg_n"] = float(df["n"].mean())
    out["avg_k"] = float(df["k"].mean())
    out["avg_all_top3_ind_w"] = float(df["all_top3_ind_w"].mean())
    out["avg_all_ind_hhi"] = float(df["all_ind_hhi"].mean())
    # 期间内组合水平（毛/净）
    for c in ["bench", "all", "ind", "inddm", "rand", "contra"]:
        out[f"gross_{c}"] = float(df[c].mean())
    # 臂间差
    for c in ["all-bench", "ind-bench", "inddm-bench", "rand-bench", "ind-all", "ind-rand", "rand-all", "inddm-all", "inddm-bench", "contra-all"]:
        a, b = c.split("-")
        df[c] = df[a] - df[b]
    nonov = df[df["di"] % 4 == 0]  # 锚点每 5 日 ⇒ di%4==0 即 20 日互不重叠
    for c in ["all-bench", "ind-bench", "rand-bench", "ind-all", "ind-rand", "rand-all", "inddm-all", "inddm-bench", "contra-all"]:
        out[c] = stats(df[c].to_numpy())
        out[c]["nonoverlap"] = stats(nonov[c].to_numpy())
        if c in ("ind-all", "ind-bench", "ind-rand", "inddm-bench", "inddm-all"):
            out[c]["boot"] = year_block_boot(df, c)
    # 净额
    for c in ["all", "ind", "rand", "inddm"]:
        out[f"net_{c}_12bp"] = out[f"gross_{c}"] - COST_MAIN
        out[f"net_{c}_20bp"] = out[f"gross_{c}"] - COST_ALT
    out["net_bench"] = out["gross_bench"]
    return out


def yearly(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["year"] = df["date"].dt.year
    for c in ["all-bench", "ind-bench", "rand-bench", "ind-all", "ind-rand", "rand-all", "inddm-all", "inddm-bench", "contra-all"]:
        a, b = c.split("-")
        df[c] = df[a] - df[b]
    out = df.groupby("year").agg(
        n_cohorts=("date", "count"),
        bench=("bench", "mean"),
        all=("all", "mean"),
        ind=("ind", "mean"),
        rand=("rand", "mean"),
        inddm=("inddm", "mean"),
        contra=("contra", "mean"),
        **{
            c: (c, "mean")
            for c in ["all-bench", "ind-bench", "rand-bench", "ind-all", "ind-rand", "rand-all", "inddm-all", "inddm-bench", "contra-all"]
        },
    )
    return out.reset_index()


def verdict(y: pd.DataFrame) -> dict:
    def count_pos(col: str, lo: int = 2018) -> dict:
        s = y[y["year"] >= lo].set_index("year")[col]
        s = s[s.index <= 2026]
        return {"years": len(s), "pos": int((s > 0).sum()), "detail": {int(k): round(float(v), 5) for k, v in s.items()}}

    return {
        "P1_ind_minus_all_2018plus": count_pos("ind-all"),
        "P2_ind_minus_bench_2018plus": count_pos("ind-bench"),
        "P3_ind_minus_rand_2018plus": count_pos("ind-rand"),
        "P1_ind_minus_all_full": count_pos("ind-all", 2010),
    }


def main() -> None:
    p = add_scores(load())
    p["neg"] = -p["heat"]  # 显式：高分 = 低热度 = 预期更好
    res = {"prereg": "docs/t1-industry-frame-prereg.md", "cost_main": COST_MAIN, "cost_alt": COST_ALT}

    coh = build_cohorts(p, "neg")
    res["primary_low_heat"] = summarize(coh, "score=-heat (low heat = long)")
    yl = yearly(coh)
    res["primary_yearly"] = yl.round(6).to_dict("records")
    res["primary_verdict"] = verdict(yl)

    hi = build_cohorts(p, "heat")
    res["control_high_heat"] = summarize(hi, "score=+heat (high heat = long)")
    yh = yearly(hi)
    res["control_yearly"] = yh.round(6).to_dict("records")
    res["control_verdict"] = verdict(yh)

    coh.to_parquet(ROOT / "data" / ".scratch" / "footprint20d" / "t1_cohorts.parquet", index=False)
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    yl.to_csv(OUT_CSV, index=False)
    for k in ("primary_low_heat", "control_high_heat", "primary_verdict"):
        print(k, json.dumps(res[k], ensure_ascii=False, indent=1)[:2600])


if __name__ == "__main__":
    main()
