"""验证特征清单：三种独立检验，而不是「找几个已打板的票看看像不像」。

为什么不能只看个案：
  已打板的票是条件在「打板成功」上的样本，任何特征都能事后找到解释（幸存者偏差）。
  必须做「样本外 + 排序 + 负控」三件事，个案只用来读机制。

本脚本做四件事：
  V1 个案解剖：最近 N 个「游资打板」事件，在 T-1 的特征分位（是否真的落在高档）
  V2 样本外：2025-11~2026-05 训练 logistic，2026-06~2026-11 测试 AUC
  V3 precision@K：每日按分数取前 K，看未来 5 日打板率 vs 基准（这才是「能不能用」）
  V4 负控：取分数最低的一组，看是否显著低于基准（不低则清单无区分度）
  V5 今日在榜：用最新一日特征输出 top20，并检查它们的 ret20（防止「清单=追高」）

标签定义（收紧到「游资打板」，不是「游资上榜」）：
  board_t = (consecutive_limit_ups >= 1) AND (hm_net > 0)
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

pl.Config.set_tbl_rows(45)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)

FEATS = ["amp20", "turn20", "log_amt20", "ret120", "n_lu_60d", "log_ffcap"]
LAG = ["amp20", "turn20", "ret120", "n_lu_60d", "amt20", "ffcap", "raw_close"]


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 2.0, iters: int = 40) -> np.ndarray:
    n, p = X.shape
    w = np.zeros(p)
    for _ in range(iters):
        z = X @ w
        mu = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        W = np.maximum(mu * (1.0 - mu), 1e-9)
        grad = X.T @ (y - mu) - l2 * w
        H = (X * W[:, None]).T @ X + l2 * np.eye(p)
        step = np.linalg.solve(H, grad)
        w = w + step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w


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
        pl.col("hm_net").is_not_null().alias("y_hm"),
        (pl.col("hm_net").fill_null(0) > 0).alias("y_hm_buy"),
        pl.col("limit_reason"),
    ])
    d = df.join(dr.select(["symbol", "date", "y_hm", "y_hm_buy", "hm_net", "limit_reason"]),
                on=["symbol", "date"], how="left")
    d = d.with_columns([
        pl.col("y_hm").fill_null(False), pl.col("y_hm_buy").fill_null(False),
        (pl.col("consecutive_limit_ups") >= 1).alias("y_lu"),
    ]).with_columns(
        (pl.col("y_lu") & pl.col("y_hm_buy")).alias("y_board")
    )

    # 未来 5 日打板（用于 precision@K，严格在 T 之后）
    d = d.with_columns(
        pl.col("y_board").cast(pl.Int32).shift(-1).rolling_max(5).over("symbol").alias("f_board5")
    )

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
    print(f"全样本 {panel.height:,} / {panel['symbol'].n_unique()} 只 / 游资打板 {int(panel['y_board'].sum())} 次 "
          f"/ 基准 {panel['y_board'].mean()*1e4:.2f}bp", flush=True)

    res: dict = {}

    # ---------- V1 个案解剖 ----------
    print("\n=== V1 最近 30 个「游资打板」事件在 T-1 的分位 ===", flush=True)
    pct = panel.with_columns([
        (pl.col("amp20").rank().over("date") / pl.len().over("date")).alias("pct_amp"),
        (pl.col("turn20").rank().over("date") / pl.len().over("date")).alias("pct_turn"),
        (pl.col("log_amt20").rank().over("date") / pl.len().over("date")).alias("pct_amt"),
    ])
    ev = pct.filter(pl.col("y_board")).sort("date", descending=True).head(30).select([
        "symbol", "date", "amp20", "pct_amp", "turn20", "pct_turn", "amt20", "pct_amt", "ret120", "limit_reason"
    ]).with_columns([
        (pl.col("amp20") * 100).round(2).alias("amp%"),
        (pl.col("turn20")).round(2).alias("turn%"),
        (pl.col("amt20") / 1e4).round(0).alias("amt20万"),
        (pl.col("ret120") * 100).round(1).alias("r120%"),
        (pl.col("pct_amp") * 100).round(0).alias("振幅分位"),
        (pl.col("pct_turn") * 100).round(0).alias("换手分位"),
    ]).select(["date", "symbol", "amp%", "振幅分位", "turn%", "换手分位", "amt20万", "r120%", "limit_reason"])
    print(ev)
    res["v1_events"] = ev.to_dicts()
    q = pct.filter(pl.col("y_board")).select([
        pl.col("pct_amp").median().alias("amp_med"),
        (pl.col("pct_amp") > 0.7).mean().alias("amp_top30_share"),
        pl.col("pct_turn").median().alias("turn_med"),
        (pl.col("pct_turn") > 0.7).mean().alias("turn_top30_share"),
    ]).to_dicts()[0]
    print(f"\n  事件集 T-1 振幅分位中位 {q['amp_med']:.2f}，振幅进全市场前 30% 的比例 {q['amp_top30_share']:.1%}")
    print(f"  事件集 T-1 换手分位中位 {q['turn_med']:.2f}，换手进前 30% 的比例 {q['turn_top30_share']:.1%}")
    res["v1_summary"] = q

    # ---------- V2 样本外 ----------
    print("\n=== V2 样本外：训练 2025-11~2026-05 / 测试 2026-06~2026-11 ===", flush=True)
    tr = panel.filter(pl.col("date") < pl.date(2026, 6, 1))
    te = panel.filter(pl.col("date") >= pl.date(2026, 6, 1))
    Xtr = tr.select(FEATS).to_numpy().astype(np.float64)
    ytr = tr["y_board"].to_numpy().astype(np.float64)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    Ztr = (Xtr - mu) / sd
    Ztr = np.hstack([np.ones((len(Ztr), 1)), Ztr])
    w = fit_logit(Ztr, ytr)

    Xte = te.select(FEATS).to_numpy().astype(np.float64)
    yte = te["y_board"].to_numpy().astype(np.float64)
    Zte = np.hstack([np.ones((len(Xte), 1)), (Xte - mu) / sd])
    s_te = Zte @ w
    s_tr = Ztr @ w

    a_tr, a_te = auc(s_tr, ytr), auc(s_te, yte)
    print(f"  训练 n={len(ytr):,} pos={int(ytr.sum())} AUC={a_tr:.4f}")
    print(f"  测试 n={len(yte):,} pos={int(yte.sum())} AUC={a_te:.4f}  (基准 {yte.mean()*1e4:.2f}bp)")
    coef = dict(zip(["intercept"] + FEATS, [float(x) for x in w]))
    print("  系数（标准化后）:", json.dumps({k: round(v, 3) for k, v in coef.items()}, ensure_ascii=False))
    res["v2"] = {"auc_train": a_tr, "auc_test": a_te, "n_train": len(ytr), "n_test": len(yte),
                 "pos_test": int(yte.sum()), "coef": coef}

    # 逐特征独立 AUC（测试集）
    print("\n  测试集逐特征 AUC（单变量，看清单是否冗余）:")
    for f in FEATS:
        x = te[f].to_numpy().astype(np.float64)
        print(f"    {f:12s} {auc(x, yte):.4f}")

    # 测试集分 5 档 lift
    te2 = te.with_columns(pl.Series("s", s_te)).with_columns(
        pl.col("s").qcut(5, labels=[f"Q{i}" for i in range(1, 6)]).alias("q")
    )
    gl = te2.group_by("q").agg([
        pl.len().alias("n"), pl.col("y_board").sum().alias("hit"),
        (pl.col("y_board").mean() * 1e4).alias("bp"),
    ]).with_columns((pl.col("bp") / (yte.mean() * 1e4)).alias("lift")).sort("q")
    print("\n  测试集五分档（样本外）:")
    print(gl)
    res["v2_quintile"] = gl.to_dicts()

    # ---------- V3 precision@K ----------
    print("\n=== V3 precision@K：每日取分数前 K，看未来 5 日打板率 ===", flush=True)
    p3 = panel.filter(pl.col("date") >= pl.date(2026, 6, 1)).with_columns(
        pl.Series("s", np.hstack([np.ones((panel.filter(pl.col("date") >= pl.date(2026, 6, 1)).height, 1)),
                                  (panel.filter(pl.col("date") >= pl.date(2026, 6, 1))
                                   .select(FEATS).to_numpy().astype(np.float64) - mu) / sd]) @ w)
    )
    p3 = p3.filter(pl.col("f_board5").is_not_null())
    base5 = float(p3["f_board5"].mean() * 1e4)
    print(f"  测试期基准：未来 5 日打板 {base5:.1f}bp")

    rows = []
    for K in [20, 50, 100, 300]:
        top = p3.with_columns(pl.col("s").rank(descending=True).over("date").alias("rk")) \
                .filter(pl.col("rk") <= K)
        bot = p3.with_columns(pl.col("s").rank(descending=True).over("date").alias("rk")) \
                .filter(pl.col("rk") > pl.col("s").rank(descending=True).over("date").max() - K)
        rows.append({
            "K": K, "top_n": top.height, "top_hit": int(top["f_board5"].sum()),
            "top_bp": float(top["f_board5"].mean() * 1e4),
            "top_lift": float(top["f_board5"].mean() * 1e4 / base5),
            "bot_bp": float(bot["f_board5"].mean() * 1e4),
            "bot_lift": float(bot["f_board5"].mean() * 1e4 / base5),
        })
    pt = pl.DataFrame(rows)
    print(pt)
    res["v3_precision"] = pt.to_dicts()

    # 检查 top20 是不是「追高」：看它们的 ret20 分布
    top20 = p3.with_columns(pl.col("s").rank(descending=True).over("date").alias("rk")) \
              .filter(pl.col("rk") <= 20)
    print(f"\n  top20 的 ret20 中位 {top20['ret20'].median()*100:.1f}% / "
          f"已涨停占比 {top20['y_lu'].mean()*100:.1f}% / "
          f"n_lu_20d>0 占比 {(top20['n_lu_20d'].fill_null(0) > 0).mean()*100:.1f}%")
    res["v3_top20_profile"] = {
        "ret20_med": float(top20["ret20"].median()),
        "already_lu_share": float(top20["y_lu"].mean()),
        "had_lu20_share": float((top20["n_lu_20d"].fill_null(0) > 0).mean()),
    }

    # ---------- V3b 冷启动：剔除已涨停后，清单还有没有预测力 ----------
    print("\n=== V3b 冷启动（过去 20 日无涨停 且 |ret20|<15%）：清单还剩多少预测力 ===", flush=True)
    p3c = p3.filter((pl.col("n_lu_20d").fill_null(0) == 0) & (pl.col("ret20").abs() < 0.15))
    base5c = float(p3c["f_board5"].mean() * 1e4)
    print(f"  冷启动基准：未来 5 日打板 {base5c:.1f}bp（n={p3c.height:,}）")
    rows = []
    for K in [20, 50, 100, 300]:
        p3c = p3c.with_columns(pl.col("s").rank(descending=True).over("date").alias("rk2"))
        top = p3c.filter(pl.col("rk2") <= K)
        rows.append({
            "K": K, "top_n": top.height, "top_hit": int(top["f_board5"].sum()),
            "top_bp": float(top["f_board5"].mean() * 1e4),
            "top_lift": float(top["f_board5"].mean() * 1e4 / base5c) if base5c > 0 else None,
        })
    ptc = pl.DataFrame(rows)
    print(ptc)
    res["v3b_cold_precision"] = ptc.to_dicts()
    if p3c.height:
        t20c = p3c.filter(pl.col("rk2") <= 20)
        print(f"  冷启动 top20：已涨停占比 {t20c['y_lu'].mean()*100:.1f}% / "
              f"ret20 中位 {t20c['ret20'].median()*100:.1f}%")
        res["v3b_top20"] = {"already_lu": float(t20c["y_lu"].mean()),
                            "ret20_med": float(t20c["ret20"].median())}

    # ---------- V2b 去共线性：turn20 ≈ amt20/ffcap，三者不能同时进 ----------
    print("\n=== V2b 去共线性重拟合（去掉 turn20）===", flush=True)
    F2 = ["amp20", "log_amt20", "ret120", "n_lu_60d", "log_ffcap"]
    X2 = tr.select(F2).to_numpy().astype(np.float64)
    m2, s2 = X2.mean(0), X2.std(0) + 1e-9
    w2 = fit_logit(np.hstack([np.ones((len(X2), 1)), (X2 - m2) / s2]), ytr)
    Zte2 = np.hstack([np.ones((len(Xte), 1)), (te.select(F2).to_numpy().astype(np.float64) - m2) / s2])
    a2 = auc(Zte2 @ w2, yte)
    coef2 = dict(zip(["intercept"] + F2, [float(x) for x in w2]))
    print(f"  测试 AUC={a2:.4f}（含 turn20 的模型 0.7145）")
    print("  系数:", json.dumps({k: round(v, 3) for k, v in coef2.items()}, ensure_ascii=False))
    res["v2b"] = {"auc_test": a2, "coef": coef2}

    # ---------- V5 今日在榜 ----------
    print("\n=== V5 最新一日打分 top20（2026-11-20 状态，供下周观察）===", flush=True)
    last = d.filter(pl.col("date") == d["date"].max()).filter(
        pl.col("amp20").is_not_null() & pl.col("ffcap").is_not_null()
        & pl.col("amt20").is_not_null() & pl.col("ret120").is_not_null()
        & (pl.col("n_bars_so_far") > 250)
    ).with_columns([
        pl.col("amt20").log().alias("log_amt20"),
        pl.col("ffcap").log().alias("log_ffcap"),
        pl.col("n_lu_60d").fill_null(0),
    ])
    Xl = last.select(FEATS).to_numpy().astype(np.float64)
    sl = np.hstack([np.ones((len(Xl), 1)), (Xl - mu) / sd]) @ w
    pick = last.with_columns(pl.Series("s", sl)).sort("s", descending=True).head(20).select([
        "symbol", "name", "close", "s", "amp20", "turn20", "amt20", "ffcap", "ret20", "ret120", "n_lu_20d"
    ]).with_columns([
        (pl.col("amp20") * 100).round(2).alias("amp%"),
        pl.col("turn20").round(2),
        (pl.col("amt20") / 1e4).round(0).alias("amt20万"),
        (pl.col("ffcap") / 1e8).round(1).alias("ff亿"),
        (pl.col("ret20") * 100).round(1).alias("r20%"),
        (pl.col("ret120") * 100).round(1).alias("r120%"),
        pl.col("s").round(3),
    ]).select(["symbol", "name", "close", "s", "amp%", "turn20", "amt20万", "ff亿", "r20%", "r120%", "n_lu_20d"])
    print(pick)
    res["v5_today_top20"] = pick.to_dicts()
    print(f"\n  top20 的 r20 中位 {pick['r20%'].median():.1f}% / r120 中位 {pick['r120%'].median():.1f}%")

    # 冷启动版：这才是「还没炒、但结构上够格」的名单
    print("\n=== V5b 冷启动名单（过去 20 日无涨停 且 |ret20|<15%）===", flush=True)
    cold_last = last.filter(
        (pl.col("n_lu_20d").fill_null(0) == 0) & (pl.col("ret20").abs() < 0.15)
        & (pl.col("amt20") > 3e7)          # 成交额至少 3000 万，否则出不来
    )
    Xc = cold_last.select(FEATS).to_numpy().astype(np.float64)
    sc = np.hstack([np.ones((len(Xc), 1)), (Xc - mu) / sd]) @ w
    pickc = cold_last.with_columns(pl.Series("s", sc)).sort("s", descending=True).head(15).select([
        "symbol", "name", "close", "s", "amp20", "turn20", "amt20", "ffcap", "ret20", "ret120", "n_lu_20d"
    ]).with_columns([
        (pl.col("amp20") * 100).round(2).alias("amp%"),
        pl.col("turn20").round(2),
        (pl.col("amt20") / 1e4).round(0).alias("amt20万"),
        (pl.col("ffcap") / 1e8).round(1).alias("ff亿"),
        (pl.col("ret20") * 100).round(1).alias("r20%"),
        (pl.col("ret120") * 100).round(1).alias("r120%"),
        pl.col("s").round(2),
    ]).select(["symbol", "name", "close", "s", "amp%", "turn20", "amt20万", "ff亿", "r20%", "r120%"])
    print(pickc)
    print(f"  候选池 {cold_last.height} 只 / 平均 amp {cold_last['amp20'].mean()*100:.2f}% / "
          f"平均 turn {cold_last['turn20'].mean():.2f}%")
    res["v5b_cold_top15"] = pickc.to_dicts()
    res["v5b_pool"] = cold_last.height

    with open(OUT / "youzi_validate.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写出 {OUT / 'youzi_validate.json'}")


if __name__ == "__main__":
    main()
