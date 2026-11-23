"""分制度重估：主板 / 创业板 / 科创板 / 北交所 / ST 各自的系数与样本外 AUC。

要回答的是「清单是不是四个模型的平均」：
  若各板块标准化系数方向相反或量级差 2 倍以上，混拟合就是在拟合平均值，
  对任何单一板块都不是最优。

口径：
  制度分组用 symbol 前缀 + 当前简称含 ST（ST 用当前名有前视，仅作分层标签，不用于交易）。
  每板块内各自标准化（均值 0/方差 1）再拟合 ⇒ 系数可直接横向比较（1 个标准差的影响）。
  训练 2025-11-12~2026-05-31，测试 2026-06-01~2026-11-19。
标签：y_board = 当日涨停 且 龙虎榜游资净买入 > 0。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

from _diag_youzi_features import add_features, attach_ownership, load_dragon, load_panel  # noqa: E402
from _diag_youzi_features3 import auc  # noqa: E402
from _diag_youzi_validate import LAG, fit_logit  # noqa: E402

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)

FEATS = ["amp20", "log_amt20", "ret120", "n_lu_60d", "log_ffcap"]


def regime(sym: str, name: str | None) -> str:
    n = (name or "").upper().replace(" ", "")
    if "ST" in n:
        return "ST"
    if sym.endswith(".BJ") or sym[:1] in ("8", "4") or sym.startswith("920"):
        return "BSE"
    if sym.startswith("688") or sym.startswith("689"):
        return "STAR"
    if sym.startswith("300") or sym.startswith("301"):
        return "GEM"
    return "MAIN"


def fit_eval(tr: pl.DataFrame, te: pl.DataFrame, feats: list[str]) -> dict | None:
    if tr.height < 5000 or te.height < 2000:
        return None
    ytr = tr["y_board"].to_numpy().astype(np.float64)
    if ytr.sum() < 30 or te["y_board"].sum() < 10:
        return None
    Xtr = tr.select(feats).to_numpy().astype(np.float64)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = fit_logit(np.hstack([np.ones((len(Xtr), 1)), (Xtr - mu) / sd]), ytr)
    Zte = np.hstack([np.ones((te.height, 1)), (te.select(feats).to_numpy().astype(np.float64) - mu) / sd])
    s = Zte @ w
    yte = te["y_board"].to_numpy().astype(np.float64)
    # 测试集五分档 lift
    q = np.quantile(s, [0.2, 0.4, 0.6, 0.8])
    top = s >= q[2]
    return {
        "n_train": tr.height, "pos_train": int(ytr.sum()),
        "n_test": te.height, "pos_test": int(yte.sum()),
        "base_test_bp": float(yte.mean() * 1e4),
        "auc_test": auc(s, yte),
        "top20_lift": float(yte[top].mean() / yte.mean()) if yte.mean() > 0 else None,
        "coef": {k: round(float(v), 3) for k, v in zip(["intercept"] + feats, w)},
    }


def main() -> None:
    df = add_features(load_panel())
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).truediv(pl.col("close")).rolling_mean(20).over("symbol").alias("amp20"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int32).shift(1).over("symbol")
            .rolling_sum(60).alias("n_lu_60d"),
    ])
    df = attach_ownership(df)
    df = df.with_columns([pl.col(c).shift(1).over("symbol").alias(c) for c in LAG])

    dr = load_dragon().with_columns([
        (pl.col("hm_net").fill_null(0) > 0).alias("y_hm_buy"),
    ])
    d = df.join(dr.select(["symbol", "date", "y_hm_buy"]), on=["symbol", "date"], how="left")
    d = d.with_columns([
        pl.col("y_hm_buy").fill_null(False),
        (pl.col("consecutive_limit_ups") >= 1).alias("y_lu"),
    ]).with_columns((pl.col("y_lu") & pl.col("y_hm_buy")).alias("y_board"))

    panel = d.filter(
        (pl.col("date") >= pl.date(2025, 11, 12)) & (pl.col("date") <= pl.date(2026, 11, 19))
        & (pl.col("n_bars_so_far") > 250)
        & pl.col("amp20").is_not_null() & pl.col("ffcap").is_not_null()
        & pl.col("amt20").is_not_null() & pl.col("ret120").is_not_null()
    ).with_columns([
        pl.col("amt20").log().alias("log_amt20"),
        pl.col("ffcap").log().alias("log_ffcap"),
        pl.col("n_lu_60d").fill_null(0),
    ])
    panel = panel.with_columns(
        pl.struct(["symbol", "name"]).map_elements(
            lambda s: regime(s["symbol"], s["name"]), return_dtype=pl.Utf8
        ).alias("reg")
    )

    print("=== 各制度样本分布 ===", flush=True)
    dist = panel.group_by("reg").agg([
        pl.len().alias("n"), pl.col("symbol").n_unique().alias("symbols"),
        pl.col("y_board").sum().alias("pos"),
        (pl.col("y_board").mean() * 1e4).alias("base_bp"),
        (pl.col("amp20").median() * 100).alias("amp_med%"),
        pl.col("turn20").median().alias("turn_med%"),
        (pl.col("amt20").median() / 1e4).alias("amt_med_wan"),
    ]).sort("n", descending=True)
    print(dist)

    tr = panel.filter(pl.col("date") < pl.date(2026, 6, 1))
    te = panel.filter(pl.col("date") >= pl.date(2026, 6, 1))
    res: dict = {"dist": dist.to_dicts(), "pooled": None, "by_regime": {}}

    print("\n=== 混拟合（全样本一套系数）===", flush=True)
    pooled = fit_eval(tr, te, FEATS)
    print(json.dumps(pooled, ensure_ascii=False, indent=1))
    res["pooled"] = pooled

    print("\n=== 分制度各拟合一套 ===", flush=True)
    order = ["MAIN", "GEM", "STAR", "BSE", "ST"]
    for r in order:
        tr_r, te_r = tr.filter(pl.col("reg") == r), te.filter(pl.col("reg") == r)
        out = fit_eval(tr_r, te_r, FEATS)
        res["by_regime"][r] = out
        if out is None:
            print(f"\n{r}: 样本不足（train {tr_r.height}, test {te_r.height}, pos {int(tr_r['y_board'].sum())}）")
            continue
        print(f"\n{r}: train {out['n_train']:,}(pos {out['pos_train']}) / test {out['n_test']:,}(pos {out['pos_test']}) "
              f"/ base {out['base_test_bp']:.1f}bp / AUC {out['auc_test']:.4f} / top20 {out['top20_lift']:.2f}x")
        print("   系数:", json.dumps(out["coef"], ensure_ascii=False))

    # 交叉应用：混拟合模型在各制度上的 AUC vs 各自模型
    print("\n=== 交叉表：混拟合 vs 分制度（AUC）===", flush=True)
    Xtr = tr.select(FEATS).to_numpy().astype(np.float64)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = fit_logit(np.hstack([np.ones((len(Xtr), 1)), (Xtr - mu) / sd]), tr["y_board"].to_numpy().astype(np.float64))
    rows = []
    for r in order:
        te_r = te.filter(pl.col("reg") == r)
        if te_r.height < 2000:
            continue
        y = te_r["y_board"].to_numpy().astype(np.float64)
        Z = np.hstack([np.ones((te_r.height, 1)), (te_r.select(FEATS).to_numpy().astype(np.float64) - mu) / sd])
        a_pooled = auc(Z @ w, y)
        a_own = (res["by_regime"].get(r) or {}).get("auc_test")
        rows.append({"reg": r, "n": te_r.height, "pos": int(y.sum()),
                     "auc_pooled": a_pooled, "auc_own": a_own,
                     "delta": (a_own - a_pooled) if a_own is not None else None})
    ct = pl.DataFrame(rows)
    print(ct)
    res["cross"] = ct.to_dicts()

    with open(OUT / "youzi_by_regime.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写出 {OUT / 'youzi_by_regime.json'}")


if __name__ == "__main__":
    main()
