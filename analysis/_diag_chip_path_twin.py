"""路径孪生：同一段价格路径，一张有账单、一张没有。

卷五的核心命题是「价格路径可以完全相同，而应付表完全不同」。
这句话此前只是叙事：H13e 匹配的是**特征**（ret250/vol20/turn20/size），
不是**路径**。特征相同不等于路径相同；如果贴线组的路径形状本身不同
（例如更平滑的阴跌），那 +291bp 可能只是「某种路径形状」的溢价。

本检验把匹配向量换成**过去 60 日的整段路径**：
  对每个月末的贴线股（wdist ≥ 0.9，簿加权平仓线距现价 ≤ 11%），
  在同日、同规模带、同换手/波动的**有簿但远离平仓线**（wdist ≤ 0.6）的股票里，
  找过去 60 日对数价格路径最接近的一只，取 f60 之差。

三条读数：
  A. near − random_far ：不做路径匹配的基准（应接近历史 +291bp 量级）
  B. near − path_twin  ：路径匹配后的差（本检验的主角）
  C. B / A             ：路径解释掉了多少

判据（写于跑数之前）：
  - 若 B 的 |t| < 1.5 且 |B| < 0.4·A ⇒ 「路径就够了」，卷五的账单读法降级为路径形状；
  - 若 B 与 A 同号且 t ≥ 2 ⇒ 同一段路径上，账单仍有独立信息；
  - 若 B 与 A 反号 ⇒ 匹配把样本选到了另一群人，检验无效，先查匹配质量。

口径：
  - f60 = close[t+61]/open[t+1] − 1（与 constraint_book 面板一致）；
  - 路径 = 过去 60 日对数收盘在 6 个等距点上的增量，逐月做横截面 robust z；
  - 距离 = 6 段路径 z 的平方和 + 0.5·(size z)² + 0.5·(turn z)² + 0.5·(vol z)²；
  - 只保留 |size z| ≤ 1 的配对；距离上限 6.0（约 6 个标准化单位的平方和）。
  - 随机对照从同一规模带内抽，固定种子；按月聚簇 NW(6)。

输出：data/.scratch/leverage/path_twin.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
PANEL = OUT / "constraint_book_panel.parquet"
KLINE = ROOT / "data" / "kline_daily_enriched"
RNG = np.random.default_rng(20261128)
VARIANT = sys.argv[1] if len(sys.argv) > 1 else "wdist"


def nw_t(x: np.ndarray, lag: int = 6) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    e = x - x.mean()
    s = float((e * e).mean())
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float((e[l:] * e[:-l]).mean())
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def rz(s: pd.Series) -> pd.Series:
    v = s.astype(float)
    med = v.median()
    q1, q3 = v.quantile(0.25), v.quantile(0.75)
    scale = max(float(q3 - q1) / 1.349, 1e-9)
    return ((v - med) / scale).clip(-6, 6)


def main() -> None:
    p = pd.read_parquet(PANEL)
    p["date"] = pd.to_datetime(p["date"])
    p = p[p["ok"]].copy()
    lc = pd.read_parquet(OUT / "largest_contract_panel.parquet")
    lc["date"] = pd.to_datetime(lc["date"])
    p = p.merge(lc[["symbol", "date", "big_ratio", "max_ratio", "big_tsr"]], on=["symbol", "date"], how="left")

    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "close", "turnover_rate"],
        filter=ds.field("date") >= pd.Timestamp("2019-01-01").date(),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    px["symbol"] = px["symbol"].astype("string")
    C = px.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    T = px.pivot_table(index="date", columns="symbol", values="turnover_rate", aggfunc="last").sort_index()
    T = (T / 100.0).clip(0, 0.9)
    C = C.where(C > 0)
    di = C.index
    cols = np.array(C.columns)

    # 面板行 → 日线矩阵位置
    rows = di.searchsorted(p["date"].values)
    jj = pd.Index(cols).get_indexer(p["symbol"].values)
    keep = (rows < len(di) - 61) & (rows >= 60) & (jj >= 0) & p["f60"].notna().values
    p = p[keep].reset_index(drop=True)
    rows, jj = rows[keep], jj[keep]

    logc = np.log(C.values)
    # 6 段路径：t-60 → t，等距 7 点
    offs = np.array([-60, -50, -40, -30, -20, -10, 0])
    path = np.full((len(p), 6), np.nan)
    for k in range(6):
        a = logc[rows + offs[k], jj]
        b = logc[rows + offs[k + 1], jj]
        path[:, k] = b - a
    # 60 日均换手（日线矩阵先滚动，再按面板行取样）
    T60 = T.rolling(60, min_periods=30).mean()
    p["turn60_path"] = T60.values[rows, jj]
    p["ret250d"] = C.values[rows, jj] / C.values[np.maximum(rows - 250, 0), jj] - 1.0
    for k in range(6):
        p[f"path{k}"] = path[:, k]

    # 逐月横截面标准化
    feat = [f"path{k}" for k in range(6)] + ["size", "turn60_path", "vol20", "ret250d"]
    z = p.groupby("ym")[feat].transform(lambda s: rz(s))
    z.columns = [f"z_{c}" for c in feat]
    p = pd.concat([p, z], axis=1)

    # 月度系统状态：贴线密度（有簿股票中 wdist≥0.9 的占比）与市场 60 日涨跌
    bk = p[p["has_book"]]
    stress = bk.groupby("ym").apply(lambda g: float((g["wdist"] >= 0.9).mean()), include_groups=False)
    mkt60 = p.groupby("ym")["ret60"].median()

    # 每个月末：near / far 两个池
    rec_rows = []
    match_quality = []
    for ym, g in p.groupby("ym", sort=True):
        need = ["wdist", "near_share"] if VARIANT in ("wdist", "share") else [("big_ratio" if VARIANT == "big" else "max_ratio")]
        g = g.dropna(subset=[f"z_path{k}" for k in range(6)] + ["z_size", "z_turn60_path", "z_vol20", "z_ret250d", "f60"] + need)
        if len(g) < 400:
            continue
        if VARIANT == "share":
            near = g[(g["near_share"] >= 0.5) & g["f60"].notna()]
            far = g[(g["wdist"] <= 0.6) & g["f60"].notna()]
        elif VARIANT == "big":
            near = g[(g["big_ratio"] >= 0.8) & (g["big_ratio"] <= 1.2) & g["f60"].notna()]
            far = g[(g["wdist"] <= 0.6) & g["f60"].notna()]
        elif VARIANT == "max":
            near = g[(g["max_ratio"] >= 0.8) & (g["max_ratio"] <= 1.2) & g["f60"].notna()]
            far = g[(g["wdist"] <= 0.6) & g["f60"].notna()]
        else:
            near = g[(g["wdist"] >= 0.9) & g["f60"].notna()]
            far = g[(g["wdist"] <= 0.6) & g["f60"].notna()]
        if len(near) < 10 or len(far) < 100:
            continue
        F = far[[f"z_path{k}" for k in range(6)] + ["z_size", "z_turn60_path", "z_vol20", "z_ret250d"]].to_numpy(float)
        fy = far["f60"].to_numpy(float)
        fs = far["z_size"].to_numpy(float)
        N = near[[f"z_path{k}" for k in range(6)] + ["z_size", "z_turn60_path", "z_vol20", "z_ret250d"]].to_numpy(float)
        ny = near["f60"].to_numpy(float)
        ns = near["z_size"].to_numpy(float)

        # 随机对照：同规模带内随机抽
        rand_idx = []
        for i in range(len(near)):
            cand = np.where(np.abs(fs - ns[i]) <= 1.0)[0]
            if len(cand) == 0:
                rand_idx.append(-1)
                continue
            rand_idx.append(int(RNG.choice(cand)))
        rand_idx = np.array(rand_idx)

        # 最近邻：路径 + 规模 + 换手 + 波动
        for i in range(len(near)):
            diff = F - N[i]
            ok = np.abs(diff[:, 6]) <= 1.0
            if ok.sum() == 0:
                continue
            dist = (diff[:, :6] ** 2).sum(axis=1) + 0.5 * (diff[:, 6] ** 2) + 0.5 * (diff[:, 7] ** 2) + 0.5 * (diff[:, 8] ** 2) + 0.5 * (diff[:, 9] ** 2)
            dist = np.where(ok, dist, np.inf)
            j = int(np.argmin(dist))
            if not np.isfinite(dist[j]) or dist[j] > 6.0:
                continue
            k = rand_idx[i]
            rec_rows.append({
                "ym": ym,
                "stress": float(stress.get(ym, np.nan)),
                "mkt60": float(mkt60.get(ym, np.nan)),
                "y_twin": float(ny[i] - fy[j]),
                "y_rand": float(ny[i] - fy[k]) if k >= 0 else np.nan,
                "dist": float(dist[j]),
                "near_f60": float(ny[i]),
                "twin_f60": float(fy[j]),
                "rand_f60": float(fy[k]) if k >= 0 else np.nan,
                "size_z": float(ns[i]),
                "ncon": float(near["ncon"].to_numpy(float)[i]),
                "book": float(near["book"].to_numpy(float)[i]),
                "ret250_diff": float(far["z_ret250d"].to_numpy(float)[j] - near["z_ret250d"].to_numpy(float)[i]),
            })
        match_quality.append({"ym": ym, "near": int(len(near)), "far": int(len(far))})

    r = pd.DataFrame(rec_rows).dropna(subset=["y_twin"])
    out: dict = {"pairs": int(len(r)), "months": int(r["ym"].nunique())}
    out["match"] = {
        "dist_med": round(float(r["dist"].median()), 2),
        "dist_p90": round(float(r["dist"].quantile(0.9)), 2),
        "size_absz_med": round(float(r["size_z"].abs().median()), 2),
        "near_per_month": round(float(np.mean([m["near"] for m in match_quality])), 1),
        "ret250_absdiff_med": round(float(r["ret250_diff"].abs().median()), 2),
        "far_per_month": round(float(np.mean([m["far"] for m in match_quality])), 1),
    }

    def agg(col: str) -> dict:
        g = r.groupby("ym")[col].mean()
        s = r[col]
        return {
            "bp": round(float(s.mean() * 1e4), 1),
            "t": round(nw_t(g.to_numpy()), 2),
            "median_bp": round(float(s.median() * 1e4), 1),
            "hit": round(float((s > 0).mean() * 100), 1),
            "sd_bp": round(float(s.std() * 1e4), 0),
            "skew": round(float(s.skew()), 2),
            "p10_bp": round(float(s.quantile(0.1) * 1e4), 0),
            "p90_bp": round(float(s.quantile(0.9) * 1e4), 0),
        }

    out["rand"] = agg("y_rand")
    out["twin"] = agg("y_twin")
    out["path_share"] = round(1 - out["twin"]["bp"] / out["rand"]["bp"], 3) if out["rand"]["bp"] else None
    r["year"] = pd.to_datetime(r["ym"]).dt.year
    out["twin_by_year"] = [
        {"year": int(y), "bp": round(float(g["y_twin"].mean() * 1e4), 1), "n": int(len(g))}
        for y, g in r.groupby("year") if len(g) >= 30
    ]
    # 合约粗细：ncon 多的簿，near_share 由一堆小合约堆出来
    r["nband"] = pd.qcut(r["ncon"], 2, labels=False, duplicates="drop")
    out["twin_by_ncon"] = [
        {"q": int(q), "bp": round(float(g["y_twin"].mean() * 1e4), 1), "n": int(len(g)),
         "ncon": round(float(g["ncon"].median()), 1), "book": round(float(g["book"].median()), 1)}
        for q, g in r.groupby("nband")
    ]

    # 系统状态：贴线密度三分位 × 市场 60 日涨跌
    r["sband"] = pd.qcut(r["stress"], 3, labels=False, duplicates="drop")
    out["twin_by_stress"] = [
        {"q": int(q), "bp": round(float(g["y_twin"].mean() * 1e4), 1), "n": int(len(g)),
         "stress": round(float(g["stress"].median()), 3),
         "t": round(nw_t(g.groupby("ym")["y_twin"].mean().to_numpy()), 2)}
        for q, g in r.groupby("sband")
    ]
    mm = r.groupby("ym").agg(y=("y_twin", "mean"), sb=("sband", "first"))
    lo, hi = mm[mm["sb"] == 0]["y"], mm[mm["sb"] == 2]["y"]
    delta = float(hi.mean() - lo.mean())
    se = float(np.sqrt(hi.var(ddof=1) / len(hi) + lo.var(ddof=1) / len(lo)))
    out["stress_delta"] = {
        "bp": round(delta * 1e4, 1),
        "t": round(delta / se, 2) if se > 0 else None,
        "months_lo": int(len(lo)), "months_hi": int(len(hi)),
        "hi_years": sorted({int(pd.Timestamp(x).year) for x in r[r["sband"] == 2]["ym"].unique()}),
    }
    out["twin_by_mkt"] = [
        {"side": str(k), "bp": round(float(g["y_twin"].mean() * 1e4), 1), "n": int(len(g)),
         "mkt60": round(float(g["mkt60"].median()), 3)}
        for k, g in r.assign(side=np.where(r["mkt60"] >= 0, "up", "down")).groupby("side")
    ]

    # 分路径距离：路径越像，账单差是否越小？
    r["dband"] = pd.qcut(r["dist"], 4, labels=False, duplicates="drop")
    out["twin_by_dist"] = [
        {"q": int(q), "bp": round(float(g["y_twin"].mean() * 1e4), 1), "n": int(len(g)),
         "dist": round(float(g["dist"].median()), 2)}
        for q, g in r.groupby("dband")
    ]
    # 分布形状：near 与 twin 的原始 f60（同月去均值后）
    for nm, col in [("near", "near_f60"), ("twin", "twin_f60"), ("rand_f60", "rand_f60")]:
        v = r[col] - r.groupby("ym")[col].transform("mean")
        out[f"shape_{nm}"] = {
            "sd_bp": round(float(v.std() * 1e4), 0),
            "skew": round(float(v.skew()), 2),
            "p10_bp": round(float(v.quantile(0.1) * 1e4), 0),
            "p90_bp": round(float(v.quantile(0.9) * 1e4), 0),
        }

    out["variant"] = VARIANT
    name = {"wdist": "path_twin.json", "share": "path_twin_share.json",
            "big": "path_twin_big.json", "max": "path_twin_max.json"}.get(VARIANT, f"path_twin_{VARIANT}.json")
    (OUT / name).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
