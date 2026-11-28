"""叙事的账已经付过了 —— /narrative 页卷二的核心检验。

问题：市场每天给出一份「主线榜」（涨停梯队聚合，`market_mainline`）。读者把它当
「明天买什么」。本文问：这份榜单定价的是**未来**，还是**已经付掉的过去**？

定律（由 /chip 卷五迁移）：一笔约束要产生漂移，必须同时有「未结算的余额」与
「结算周期 > 0」。叙事的结算周期是 0 —— 涨停板在当日收盘就完成撮合，溢价是
**预付**的。所以剩下的不是漂移，是回吐。

三个读数：
  A 剂量反应：按概念当日涨停家数分档，看未来 5/20 日相对同日全体概念中位的超额。
    单调下降 ⇒ 温度越高，未来越差（不是「无效」，是「负」）。
  B 配对安慰剂：主线榜前 30 与「同日随机抽 30 个概念」逐日配对做差，NW(5) t。
    配对消掉了市场共同因子，比裸 t 更干净。
  C 方差：未来 20 日已实现波动 / 过去 20 日。若热度也不定价方差，则「叙事只能
    解释波动」这条也要撤回。

口径与边界：
  - 概念成分为当前快照（`ext_gn_ths`），无历史版本 ⇒ 归属漂移，只做 2025-05 起。
  - 概念日收益 = 成员等权（当日有行情的成员），要求 ≥5 只。
  - fwd = Π(1+r[t+1..t+k]) − 1（概念等权净值），基准 = 同日全体概念中位。
  - t 值按日聚簇：先取当日截面均值，再对日序列做 NW(lag=5)。
  - 涨停家数取 `consecutive_limit_ups >= 1`（enriched 列，与 regime 同源）。

输出：data/.scratch/narrative_heat.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT = ROOT / "data" / ".scratch" / "narrative_heat.json"
ENRICHED = ROOT / "data" / "kline_daily_enriched"
MAINLINE = ROOT / "data" / "mainline_history" / "part.parquet"
GN = ROOT / "data" / "ext_data" / "ext_gn_ths" / "part.parquet"

MIN_MEMBERS = 5
TOP_N = 30
N_PLACEBO = 200
BINS = [(-1, 0, "0"), (0, 1, "1"), (1, 2, "2"), (2, 4, "3-4"), (4, 9, "5-9"), (9, 10**6, "≥10")]


def _partitions(start: str, end: str) -> list[Path]:
    out = []
    for p in sorted(ENRICHED.glob("date=*")):
        d = p.name.split("=", 1)[1]
        if len(d) == 10 and start <= d <= end:
            f = p / "part.parquet"
            if f.exists():
                out.append(f)
    return out


def load_panel(start: str, end: str) -> pd.DataFrame:
    import polars as pl

    files = [str(f) for f in _partitions(start, end)]
    if not files:
        raise SystemExit("没有分区")
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "consecutive_limit_ups"])
        .collect()
        .to_pandas()
    )
    df["date"] = pd.to_datetime(df["date"])
    df["lu"] = (df["consecutive_limit_ups"].fillna(0) >= 1).astype(int)
    return df.sort_values(["symbol", "date"])


def concept_map() -> dict[str, list[str]]:
    import polars as pl

    df = pl.read_parquet(GN).select(["symbol", "所属概念"]).to_pandas()
    out: dict[str, list[str]] = {}
    for sym, raw in zip(df["symbol"], df["所属概念"]):
        if isinstance(raw, str):
            cs = [c.strip() for c in raw.split(";") if c.strip()]
            if cs:
                out[sym] = cs
    return out


def nw_t(x: np.ndarray, lag: int = 5) -> float:
    x = np.asarray([v for v in x if np.isfinite(v)], dtype=float)
    n = len(x)
    if n < 8:
        return float("nan")
    mu = x.mean()
    e = x - mu
    var = float((e ** 2).mean())
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float((e[k:] * e[:-k]).mean())
    se = np.sqrt(max(var, 1e-30) / n)
    return mu / se


def main() -> None:
    import polars as pl

    ml = pl.read_parquet(MAINLINE).filter(pl.col("kind") == "concept").to_pandas()
    ml["date"] = pd.to_datetime(ml["date"])
    ml = ml[ml["rank"] <= TOP_N]
    d0, d1 = ml["date"].min(), ml["date"].max()

    start = (d0 - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    end = (d1 + pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    print(f"主线 {d0.date()} ~ {d1.date()} | panel {start} ~ {end}")

    panel = load_panel(start, end)
    cmap = concept_map()
    panel["ret"] = panel.groupby("symbol", sort=False)["close"].pct_change()

    mem = pd.DataFrame([(s, c) for s, cs in cmap.items() for c in cs], columns=["symbol", "concept"])
    m = panel.merge(mem, on="symbol", how="inner")

    agg = (
        m.groupby(["concept", "date"], sort=False)
        .agg(ret=("ret", "mean"), lu=("lu", "sum"), n=("ret", "size"))
        .reset_index()
    )
    agg = agg[agg["n"] >= MIN_MEMBERS]
    print(f"概念×日 {len(agg):,} 行 | 概念 {agg['concept'].nunique()} 个")

    wide = agg.pivot(index="date", columns="concept", values="ret").sort_index()
    heat = agg.pivot(index="date", columns="concept", values="lu").reindex(
        index=wide.index, columns=wide.columns
    )
    days = wide.index
    mat = wide.to_numpy()
    n_d, n_c = mat.shape

    fwd5 = np.full_like(mat, np.nan)
    fwd20 = np.full_like(mat, np.nan)
    vol_n = np.full_like(mat, np.nan)
    vol_t = np.full_like(mat, np.nan)
    for j in range(n_c):
        s = pd.Series(mat[:, j])
        fwd5[:, j] = (1 + s).rolling(5).apply(np.prod, raw=True).shift(-5) - 1
        fwd20[:, j] = (1 + s).rolling(20).apply(np.prod, raw=True).shift(-20) - 1
        vol_n[:, j] = s.rolling(20).std().shift(-20) * np.sqrt(250)
        vol_t[:, j] = s.rolling(20).std() * np.sqrt(250)

    F5 = pd.DataFrame(fwd5, index=days, columns=wide.columns)
    F20 = pd.DataFrame(fwd20, index=days, columns=wide.columns)
    VR = (pd.DataFrame(vol_n, index=days, columns=wide.columns)
          / pd.DataFrame(vol_t, index=days, columns=wide.columns)).where(
        pd.DataFrame(vol_t, index=days, columns=wide.columns) > 1e-9)

    base5, base20 = F5.median(axis=1), F20.median(axis=1)

    # ---- 长表：一次摊平，后面全部复用 ----
    long = F5.stack().rename("f5").to_frame()
    long["f20"] = F20.stack()
    long["vr"] = VR.stack()
    long["lu"] = heat.stack()
    long = long.reset_index()
    long.columns = ["date", "concept", "f5", "f20", "vr", "lu"]
    long["x5"] = (long["f5"] - long["date"].map(base5)) * 1e4
    long["x20"] = (long["f20"] - long["date"].map(base20)) * 1e4
    long["year"] = long["date"].dt.year

    def bucket_of(v: float) -> str | None:
        if not np.isfinite(v):
            return None
        for lo, hi, lab in BINS:
            if lo < v <= hi:
                return lab
        return "0"

    long["bucket"] = long["lu"].map(bucket_of)
    win = long[long["date"].between(d0, d1)].copy()
    by_day_all = {d: g.set_index("concept")[["x5", "x20", "vr"]]
                  for d, g in win.groupby("date", sort=False)}

    # ---- A 剂量反应 ----
    print("\n== A 剂量反应（概念当日涨停家数 → 未来超额，基准=同日全概念中位）")
    dose = {}
    for lab in [b[2] for b in BINS]:
        g = win[win["bucket"] == lab]
        by = g.dropna(subset=["x5"]).groupby("date").agg(
            x5=("x5", "mean"), x20=("x20", "mean"), vr=("vr", "median"), n=("x5", "size"))
        dose[lab] = {
            "n": int(len(g)), "days": int(by.shape[0]),
            "x5": round(float(by["x5"].mean()), 1), "x5_t": round(nw_t(by["x5"].to_numpy()), 2),
            "x20": round(float(by["x20"].mean()), 1), "x20_t": round(nw_t(by["x20"].to_numpy()), 2),
            "vr": round(float(by["vr"].median()), 3),
        }
        d = dose[lab]
        print(f"  {lab:<6} n={d['n']:>6} x5 {d['x5']:>+7.1f} (t {d['x5_t']:>5.2f})  "
              f"x20 {d['x20']:>+7.1f} (t {d['x20_t']:>5.2f})  vr {d['vr']:.3f}")

    # 年分段（2025 / 2026）
    by_year = {}
    for y, gy in win.groupby("year"):
        row = {}
        for lab in [b[2] for b in BINS]:
            g = gy[gy["bucket"] == lab].dropna(subset=["x20"])
            row[lab] = round(float(g["x20"].mean()), 1) if len(g) else None
        by_year[int(y)] = row
    print("  年分段 x20：", by_year)

    # ---- B 配对安慰剂 ----
    rng = np.random.default_rng(20261128)
    close_days = [d for d in ml["date"].unique() if d in by_day_all]
    rank_day = {d: g.set_index("member") for d, g in ml.groupby("date") if d in by_day_all}

    def real_minus_placebo(col: str, top: int = TOP_N, draws: int = 50) -> dict:
        """逐日配对：真实前 top 的均值 − 同日随机 top 的均值（随机取 draws 次的均值，压低抽样噪声）。"""
        diffs, reals, placs = [], [], []
        for d in close_days:
            tab = by_day_all[d]
            rk = rank_day[d]
            names = [c for c in rk.index[:top] if c in tab.index]
            if len(names) < 5:
                continue
            real = float(np.nanmean(tab.loc[names, col].to_numpy(dtype=float)))
            n = min(top, tab.shape[0])
            plac = float(np.nanmean([
                np.nanmean(tab.loc[rng.choice(tab.index.to_numpy(), size=n, replace=False), col]
                           .to_numpy(dtype=float))
                for _ in range(draws)
            ]))
            diffs.append(real - plac)
            reals.append(real)
            placs.append(plac)
        diffs = np.array(diffs)
        return {
            "days": int(len(diffs)),
            "real": round(float(np.nanmean(reals)), 1),
            "placebo": round(float(np.nanmean(placs)), 1),
            "diff": round(float(np.nanmean(diffs)), 1),
            "t": round(nw_t(diffs), 2),
            "win": round(float(np.nanmean(diffs < 0)) * 100, 1),
        }

    print("\n== B 主线榜前 30 vs 同日随机 30（逐日配对差）")
    pair5 = real_minus_placebo("x5")
    pair20 = real_minus_placebo("x20")
    pairv = real_minus_placebo("vr")
    for key, v in [("x5", pair5), ("x20", pair20), ("vr", pairv)]:
        print(f"  {key:<4} real {v['real']:>+7.1f} | random {v['placebo']:>+7.1f} | "
              f"diff {v['diff']:>+7.1f} (t {v['t']:>5.2f}) | 负向日 {v['win']:.0f}%")

    # 名次分档
    print("\n== B2 名次分档（前 3 / 4-10 / 11-30 vs 同日随机 30）")
    tiers = {}
    for lab, lo, hi in [("1-3", 1, 3), ("4-10", 4, 10), ("11-30", 11, 30)]:
        diffs = []
        for d in close_days:
            tab = by_day_all[d]
            rk = rank_day[d]
            names = [c for c in rk.index if lo <= int(rk.loc[c, "rank"]) <= hi and c in tab.index]
            if len(names) < 3:
                continue
            real = float(np.nanmean(tab.loc[names, "x20"].to_numpy(dtype=float)))
            pick = rng.choice(tab.index.to_numpy(), size=min(TOP_N, tab.shape[0]), replace=False)
            diffs.append(real - float(np.nanmean(tab.loc[pick, "x20"].to_numpy(dtype=float))))
        diffs = np.array(diffs)
        tiers[lab] = {"diff": round(float(np.nanmean(diffs)), 1), "t": round(nw_t(diffs), 2),
                      "days": int(len(diffs))}
        print(f"  {lab:<6} diff {tiers[lab]['diff']:>+7.1f}bp  t {tiers[lab]['t']:>5.2f}  days {tiers[lab]['days']}")

    result = {
        "meta": {
            "mainline": f"{d0.date()} ~ {d1.date()}",
            "concepts": int(wide.shape[1]),
            "min_members": MIN_MEMBERS,
            "top_n": TOP_N,
            "placebo_n": N_PLACEBO,
            "note": "概念成分=当前快照(ext_gn_ths)，无历史版本；fwd=等权概念净值 Π(1+r)−1，"
                    "基准=同日全体概念中位；t 按日聚簇 NW(5)；安慰剂=同日随机 N 个概念。",
        },
        "dose": dose,
        "by_year": by_year,
        "pair": {"x5": pair5, "x20": pair20, "vr": pairv},
        "tiers": tiers,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
