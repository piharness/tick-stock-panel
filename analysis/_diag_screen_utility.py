"""组合筛选的实用效用 —— 「少看 90% 的票」到底换来了什么。

用户的目标不是"多一个指标"，而是**降低注意力成本 + 提高命中率**。
所以判据不是单个因子的 t 值，而是：**按条件筛完之后，剩下的票收益是否显著更高、
且数量少到能真的看完。**

测三种筛法（都用观测日横截面，未来 20 日超额）：
  A 只看换手：to20 最低 1/3
  B 只看户数：chg_yoy 最低 2/5（户数没膨胀）
  C 两者同时：A ∩ B
对照：全市场等权（0 bp）
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_shareholder_shape import prep  # noqa: E402


def evaluate(df: pl.DataFrame, name: str, cond: pl.Expr) -> dict:
    rows = []
    for dt, g in df.group_by("date", maintain_order=True):
        mkt = g["fwd20"].mean()
        hit = g.filter(cond)
        rows.append({"date": dt, "ex": hit["fwd20"].mean() - mkt if hit.height >= 5 else None,
                     "n": hit.height, "tot": g.height})
    r = pl.DataFrame(rows).drop_nulls("ex")
    m = float(r["ex"].mean()) * 1e4
    sd = float(r["ex"].std() or 0.0)
    # 基准行（全市场）的超额恒为 0，std=0，此时 t 无定义
    t = float(r["ex"].mean() / (sd / len(r) ** 0.5)) if sd > 1e-12 else float("nan")
    keep = float(r["n"].mean() / r["tot"].mean())
    return {"name": name, "bp": m, "t": t, "avg_n": int(r["n"].mean()),
            "keep": keep, "win": float((r["ex"] > 0).mean())}


def main() -> None:
    df = prep()
    # 观测日横截面分位（用 over("date") 保证同日比较）
    df = df.with_columns(
        [
            (pl.col("to20").rank("average").over("date") / pl.col("to20").count().over("date")).alias("p_to"),
            (pl.col("chg_yoy").rank("average").over("date") / pl.col("chg_yoy").count().over("date")).alias("p_hy"),
            (pl.col("mcap").rank("average").over("date") / pl.col("mcap").count().over("date")).alias("p_mc"),
        ]
    )
    print(f"sample rows={df.height:,} dates={df['date'].n_unique()} "
          f"avg_stocks={int(df.height/df['date'].n_unique())}")

    cases = [
        ("基准：全市场", pl.lit(True)),
        ("A 低换手 1/3", pl.col("p_to") <= 1 / 3),
        ("A' 低换手 1/5", pl.col("p_to") <= 0.2),
        ("B 户数不膨胀(≤40%)", pl.col("p_hy") <= 0.4),
        ("B' 户数不膨胀(≤20%)", pl.col("p_hy") <= 0.2),
        ("C 低换手1/3 ∩ 户数≤40%", (pl.col("p_to") <= 1 / 3) & (pl.col("p_hy") <= 0.4)),
        ("D 低换手1/5 ∩ 户数≤20%", (pl.col("p_to") <= 0.2) & (pl.col("p_hy") <= 0.2)),
        ("E 小市值1/3 ∩ 户数≤20%", (pl.col("p_mc") <= 1 / 3) & (pl.col("p_hy") <= 0.2)),
        ("反例：高换手1/5 ∩ 户数>80%", (pl.col("p_to") >= 0.8) & (pl.col("p_hy") > 0.8)),
    ]
    print(f"\n{'策略':<28}{'20日超额':>10}{'t':>7}{'胜率':>7}{'留存比':>8}{'均只数':>8}")
    results = {}
    for name, cond in cases:
        r = evaluate(df, name, cond)
        if "基准" not in name:
            results[name.split()[0]] = r
        print(f"{r['name']:<28}{r['bp']:>+9.1f}bp{r['t']:>+7.2f}{r['win']:>7.2f}"
              f"{r['keep']*100:>7.0f}%{r['avg_n']:>8d}")

    # —— 2×2 象限（中位数切分，与页面用的判据一致）——
    quads = {
        "Q1": ("低换手 · 筹码集中", (pl.col("p_to") <= 0.5) & (pl.col("p_hy") <= 0.5)),
        "Q2": ("高换手 · 筹码集中", (pl.col("p_to") > 0.5) & (pl.col("p_hy") <= 0.5)),
        "Q3": ("低换手 · 筹码扩散", (pl.col("p_to") <= 0.5) & (pl.col("p_hy") > 0.5)),
        "Q4": ("高换手 · 筹码扩散", (pl.col("p_to") > 0.5) & (pl.col("p_hy") > 0.5)),
    }
    print(f"\n{'象限':<24}{'20日超额':>10}{'t':>7}{'胜率':>7}{'均只数':>8}")
    qout = {}
    for k, (lab, cond) in quads.items():
        r = evaluate(df, f"{k} {lab}", cond)
        qout[k] = {"label": lab, "bp": round(r["bp"], 1), "t": round(r["t"], 2),
                   "win": round(r["win"], 2), "avg_n": r["avg_n"]}
        print(f"{k} {lab:<20}{r['bp']:>+9.1f}bp{r['t']:>+7.2f}{r['win']:>7.2f}{r['avg_n']:>8d}")

    if "--write" in sys.argv:
        import json
        from pathlib import Path

        dates = sorted(df["date"].unique().to_list())
        payload = {
            "meta": {
                "sample": f"{dates[0]} ~ {dates[-1]}",
                "n_dates": len(dates),
                "avg_stocks_per_date": int(df.height / df["date"].n_unique()),
                "fwd_days": 20,
                "return_basis": "等权、相对全市场同期等权超额",
                "split": "换手率与户数同比均按观测日横截面中位数二分",
                "holder_source": "东财 RPT_HOLDERNUM_DET，按公告日对齐",
                "generated_by": "analysis/_diag_screen_utility.py --write",
            },
            "quadrants": qout,
            "filters": {k: {"bp": round(v["bp"], 1), "t": round(v["t"], 2),
                            "keep": round(v["keep"], 3), "avg_n": v["avg_n"]}
                        for k, v in results.items()},
        }
        out = Path(__file__).resolve().parents[1] / "data" / "shareholder" / "quadrant_stats.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten -> {out}")


if __name__ == "__main__":
    main()
