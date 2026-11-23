"""审计 `docs/structural-edge/stock-dip-repair.md` 的四个承重结论。

不是复现，是压力测试。每一项都对准一个「如果它不成立，结论就要改写」的点：

  AUD-0 基线 −42bp 是什么：exc20 已在同日横截面去均值，全集均值应为 0；
        那么 −42bp 只能来自「筛选后的子universe 跑输等权市场」+ 成本 + 月度等权。
        §2 把它叫「全市场基线」，措辞与含义不一致。
  AUD-1 随机零分布：§7 的「heat 比随机差 61bp」只用了**一次**伪随机抽样。
        单次抽样的抽样误差可能有 ±50bp ⇒ 必须给分布，而不是给一个点。
  AUD-2 未来跳变剔除的代价：`clean` 要求 [t+1,t+21] 无 |ret1|>25% 跳变。
        这是在**用未来信息筛样本**。被剔掉的那批（停牌复牌/重组）exc20 是多少？
        若显著为正，则 −112bp 有相当一部分是这条规则造出来的。
  AUD-3 D2 池子口径：§8「观察池 × 市场状态」用的是 p_amt>=0.75 代理池
        （2.92M 行），不是 heat>=0.75（1.12M 行）。唯一为正的 +34.3bp 出自代理池。
  AUD-4 heat 分量留一：§7 说「换手与成交额是主要拖累」是从单键排序**推断**的，
        不是分解。真正的分解是留一法，且必须区分「只换排序键」与「换公式后重建池」。
  AUD-5 regime 时点化：mvol 分位用了全样本排名（含未来）。改成扩展窗口后，
        「下行·高波 × 深跌 = −2.9bp」还在不在。

用法：python analysis/_diag_stock_dip_audit.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _diag_stock_dip_repair as R  # noqa: E402  复用同一套 load/base/summ，保证口径同源

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/_diag_stock_dip_audit_result.json"
K_DRAW = 500
SEED = 20261123
COST = R.COST

KEEP = [
    "symbol", "date", "exc20", "exc5", "ret1", "ret20", "ret60", "dd52",
    "amt_ratio", "amt20", "p_fcap", "p_amt", "p_vol", "p_amp", "p_turn_med",
    "heat", "lu20", "clean", "consecutive_limit_ups", "consecutive_limit_downs",
]
RES: dict = {}


def h(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def pool_mask() -> pl.Expr:
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    return cold & (pl.col("ret60") >= -0.10) & (pl.col("heat") >= 0.75) & (pl.col("consecutive_limit_ups") == 0)


# ------------------------------------------------------------------ AUD-0
def aud0(df: pl.DataFrame) -> None:
    h("AUD-0 「全市场基线 −42bp」到底是什么")
    d = df.filter(pl.col("exc20").is_not_null())
    rows = []

    def m(x: pl.DataFrame, lab: str) -> None:
        if x.height == 0:
            return
        daily = float(x["exc20"].mean()) * 1e4 - COST * 1e4
        mon = (x.with_columns(pl.col("date").dt.truncate("1mo").alias("ym"))
               .group_by("ym").agg(pl.col("exc20").mean())["exc20"].mean()) * 1e4 - COST * 1e4
        rows.append({"layer": lab, "n": x.height, "daily_bp": round(daily, 1), "monthly_bp": round(mon, 1)})

    m(d, "① 面板全部（未筛 clean / 未筛流动性）")
    m(d.filter(pl.col("clean")), "② 仅 clean（剔跳变窗）")
    m(d.filter(pl.col("amt20") >= R.MIN_AMT), "③ 仅流动性 ≥3000万")
    m(d.filter(pl.col("clean") & (pl.col("amt20") >= R.MIN_AMT)), "④ clean × 流动性（= base）")
    for r in rows:
        print(f"{r['layer']:<34}{r['n']:>10}{r['daily_bp']:>10.1f}{r['monthly_bp']:>10.1f}")
    print("\n提示：exc20 已是同日横截面去均值 ⇒ ① 应接近 −12bp（只有成本）。"
          "\n      ②③④ 与 ① 的差 = 「被筛掉的那批票相对等权市场的超额」，不是成本。")
    RES["AUD0_baseline_layers"] = rows


# ------------------------------------------------------------------ AUD-1
def aud1(df: pl.DataFrame) -> None:
    h("AUD-1 heat 排序 vs 随机 15 只：给分布，不给一个点")
    d = R.base(df).filter(pl.col("exc20").is_not_null() & pl.col("ret60").is_not_null())
    pool = d.filter(pool_mask())
    days = pool.group_by("date").agg(pl.col("exc20").alias("v")).sort("date")
    arrs = [np.asarray(a, dtype=np.float64) for a in days["v"].to_list()]
    dates = days["date"].to_list()
    n_days = len(arrs)
    print(f"池 {pool.height} 行 / {n_days} 天 / 日均 {pool.height/n_days:.0f} 只")

    rng = np.random.default_rng(SEED)
    dm = np.zeros((n_days, K_DRAW), dtype=np.float64)
    for i, a in enumerate(arrs):
        n = len(a)
        k = min(15, n)
        if n <= 15:
            dm[i, :] = a.mean()
            continue
        r = rng.random((K_DRAW, n))
        idx = np.argpartition(r, k, axis=1)[:, :k]
        dm[i, :] = a[idx].mean(axis=1)

    ym = pl.Series("d", dates).dt.truncate("1mo")
    codes = ym.to_physical().to_numpy()
    uniq, inv = np.unique(codes, return_inverse=True)
    sums = np.zeros((len(uniq), K_DRAW))
    np.add.at(sums, inv, dm)
    cnt = np.bincount(inv, minlength=len(uniq)).astype(float)
    mon = sums / cnt[:, None] - COST
    mu = mon.mean(axis=0) * 1e4
    sd = mon.std(axis=0, ddof=1)
    t = mu / (sd * 1e4 / np.sqrt(mon.shape[0]))   # mu 已乘 1e4，sd 未乘

    heat = R._daily_top(pool, pl.col("heat"), True)
    small = R._daily_top(pool, pl.col("p_fcap"), False)
    quiet = R._daily_top(pool, pl.col("amt_ratio"), False)
    print(f"随机 15 只（{K_DRAW} 次）：均值 {mu.mean():.1f}bp，SD {mu.std(ddof=1):.1f}bp，"
          f"5% {np.percentile(mu,5):.1f} / 95% {np.percentile(mu,95):.1f}")
    print(f"随机 t 分布：均值 {t.mean():.2f}，5% {np.percentile(t,5):.2f}")
    for lab, r in [("heat 降序", heat), ("小市值优先", small), ("缩量优先", quiet)]:
        pct = float((mu <= r["m_ev_bp"]).mean())  # 随机抽样里比它更差的占比
        print(f"{lab:<12}{r['m_ev_bp']:>9.1f}bp  在随机分布的第 {pct*100:>5.1f} 百分位"
              f"（更低 = 比随机更差）  z={(r['m_ev_bp']-mu.mean())/mu.std(ddof=1):>5.2f}")
    RES["AUD1_null"] = {
        "draws": K_DRAW, "pool_rows": pool.height, "days": n_days,
        "rand_mean_bp": round(float(mu.mean()), 1), "rand_sd_bp": round(float(mu.std(ddof=1)), 1),
        "rand_p05": round(float(np.percentile(mu, 5)), 1),
        "rand_p95": round(float(np.percentile(mu, 95)), 1),
        "heat_bp": heat["m_ev_bp"], "heat_pct": round(float((mu <= heat["m_ev_bp"]).mean()) * 100, 1),
        "heat_z": round(float((heat["m_ev_bp"] - mu.mean()) / mu.std(ddof=1)), 2),
        "small_bp": small["m_ev_bp"], "small_pct": round(float((mu <= small["m_ev_bp"]).mean()) * 100, 1),
        "quiet_bp": quiet["m_ev_bp"], "quiet_pct": round(float((mu <= quiet["m_ev_bp"]).mean()) * 100, 1),
        "hist_bp": [round(float(x), 0) for x in np.percentile(mu, np.arange(0, 101, 5))],
    }


# ------------------------------------------------------------------ AUD-2
def aud2(df: pl.DataFrame) -> None:
    h("AUD-2 `clean` 剔掉的「未来跳变」样本，本身是什么")
    d = df.sort(["symbol", "date"]).with_columns(
        ((pl.col("ret1").abs() > 0.25)
         & (pl.col("consecutive_limit_ups") == 0)
         & (pl.col("consecutive_limit_downs") == 0)).fill_null(False).alias("_j")
    ).with_columns(
        pl.col("_j").shift(-21).rolling_sum(22, min_samples=1).over("symbol").alias("_jf")
    )
    d = d.filter(pl.col("exc20").is_not_null())
    print(f"面板可评估行 {d.height}，其中前向窗口内出现跳变 {d['_jf'].gt(0).sum()} 行 "
          f"({d['_jf'].gt(0).mean()*100:.2f}%)")
    rows = []
    for lo, hi, lab in [(-9.0, -0.35, "深跌 dd52≤-35%"), (-0.35, -0.20, "-35~-20%"),
                        (-0.20, 0.0, "-20~0%")]:
        s = d.filter((pl.col("dd52") >= lo) & (pl.col("dd52") < hi))
        for flag, tag in [(False, "纳入（无未来跳变）"), (True, "被剔（前向有跳变）")]:
            x = s.filter(pl.col("_jf") > 0 if flag else pl.col("_jf") == 0)
            if x.height < 200:
                continue
            mon = (x.with_columns(pl.col("date").dt.truncate("1mo").alias("ym"))
                   .group_by("ym").agg(pl.col("exc20").mean())["exc20"].mean()) * 1e4 - COST * 1e4
            rows.append({"bucket": lab, "group": tag, "n": x.height,
                         "monthly_bp": round(float(mon), 1),
                         "daily_bp": round(float(x["exc20"].mean()) * 1e4 - COST * 1e4, 1),
                         "share_of_bucket_pct": round(x.height / s.height * 100, 2)})
    for r in rows:
        print(f"{r['bucket']:<18}{r['group']:<22}{r['n']:>9}{r['share_of_bucket_pct']:>7.2f}%"
              f"{r['monthly_bp']:>10.1f}bp")
    RES["AUD2_jump_exclusion"] = rows


# ------------------------------------------------------------------ AUD-3
def aud3(df: pl.DataFrame) -> None:
    h("AUD-3 §8 的「观察池 × 市场状态」：heat 池 vs p_amt 代理池")
    d = R.base(df).filter(pl.col("exc20").is_not_null())
    d = d.join(R._market_state(df), on="date", how="left").filter(pl.col("m_dir").is_not_null())
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    pools = {
        "heat≥0.75（真池）": d.filter(cold & (pl.col("ret60") >= -0.10) & (pl.col("heat") >= 0.75)
                                 & (pl.col("consecutive_limit_ups") == 0)),
        "p_amt≥0.75（旧代理）": d.filter(cold & (pl.col("ret60") >= -0.10) & (pl.col("p_amt") >= 0.75)
                                    & (pl.col("consecutive_limit_ups") == 0)),
    }
    rows = []
    for name, p in pools.items():
        print(f"\n{name}: {p.height} 行，日均 {p.height/p['date'].n_unique():.0f} 只")
        for md in ["下行", "上行"]:
            for mv in ["高波", "低波"]:
                s = p.filter((pl.col("m_dir") == md) & (pl.col("m_vol") == mv))
                r = R.summ(s, "exc20", f"{name} × {md}·{mv}")
                if r:
                    print(f"  {md}·{mv:<4}{r['n']:>9}{r['m_ev_bp']:>9.1f}bp  t={r['t_month']:>6.2f}")
                    rows.append({"pool": name, "state": f"{md}·{mv}", **{k: r[k] for k in
                                ("n", "months", "m_ev_bp", "t_month", "m_pos")}})
    RES["AUD3_pool_state"] = rows


# ------------------------------------------------------------------ AUD-4
def aud4(df: pl.DataFrame) -> None:
    h("AUD-4 heat 分量留一：只换排序键 vs 换公式重建池")
    d = R.base(df).filter(pl.col("exc20").is_not_null() & pl.col("ret60").is_not_null()
                          & pl.col("heat").is_not_null())
    cold = (pl.col("lu20") == 0) & (pl.col("ret20") > -0.20) & (pl.col("ret20") < 0.15)
    d = d.with_columns([(1.0 - pl.col("p_fcap")).alias("_psc")])
    variants = {
        "H4 现行（4 项等权）": (pl.col("p_amp") + pl.col("p_turn_med") + pl.col("_psc") + pl.col("p_amt")) / 4,
        "H3 去换手": (pl.col("p_amp") + pl.col("_psc") + pl.col("p_amt")) / 3,
        "H3 去成交额": (pl.col("p_amp") + pl.col("p_turn_med") + pl.col("_psc")) / 3,
        "H3 去振幅": (pl.col("p_turn_med") + pl.col("_psc") + pl.col("p_amt")) / 3,
        "H3 去市值(1-p_scale)": (pl.col("p_amp") + pl.col("p_turn_med") + pl.col("p_amt")) / 3,
        "H2 去换手+去成交额": (pl.col("p_amp") + pl.col("_psc")) / 2,
        "H1 只用小市值": (1.0 - pl.col("p_fcap")),
    }
    cut = pl.date(2019, 1, 1)
    periods = [("全期", d), ("2013-18", d.filter(pl.col("date") < cut)), ("2019-26", d.filter(pl.col("date") >= cut))]
    rows_a, rows_b = [], []

    def fmt(x) -> str:
        return f"{x:>8.1f}" if isinstance(x, (int, float)) else f"{'—':>8}"

    print("\n(A) 池子固定为 heat≥0.75，只换排序键")
    base_pool = d.filter(cold & (pl.col("ret60") >= -0.10) & (pl.col("heat") >= 0.75)
                         & (pl.col("consecutive_limit_ups") == 0))
    for lab, expr in variants.items():
        line = []
        for tag, _ in periods:
            sub = base_pool if tag == "全期" else base_pool.filter(
                pl.col("date") < cut) if tag == "2013-18" else base_pool.filter(pl.col("date") >= cut)
            r = R._daily_top(sub, expr, True)
            line.append(r["m_ev_bp"] if r else None)
        print(f"  {lab:<22}全期 {fmt(line[0])}  2013-18 {fmt(line[1])}  2019-26 {fmt(line[2])}")
        rows_a.append({"variant": lab, "full": line[0], "p1": line[1], "p2": line[2]})

    print("\n(B) 换公式 → 池子成员也变（阈值仍 0.75），池 + 排序一起换")
    for lab, expr in variants.items():
        line, sizes = [], []
        for tag in ["全期", "2013-18", "2019-26"]:
            dd = d if tag == "全期" else d.filter(pl.col("date") < cut) if tag == "2013-18" else d.filter(pl.col("date") >= cut)
            p = dd.filter(cold & (pl.col("ret60") >= -0.10) & (expr >= 0.75)
                          & (pl.col("consecutive_limit_ups") == 0))
            r = R._daily_top(p, expr, True)
            line.append(r["m_ev_bp"] if r else None)
            sizes.append(round(p.height / p["date"].n_unique(), 0))
        print(f"  {lab:<22}全期 {fmt(line[0])}  2013-18 {fmt(line[1])}  2019-26 {fmt(line[2])}"
              f"   日均池 {sizes[0]:.0f} 只")
        rows_b.append({"variant": lab, "full": line[0], "p1": line[1], "p2": line[2], "pool_daily": sizes[0]})
    RES["AUD4_loo_fixed_pool"] = rows_a
    RES["AUD4_loo_new_pool"] = rows_b


# ------------------------------------------------------------------ AUD-5
def aud5(df: pl.DataFrame) -> None:
    h("AUD-5 regime 分位时点化（扩展窗口，不用全样本排名）")
    m = (df.filter(pl.col("amt20") >= R.MIN_AMT).group_by("date")
         .agg(pl.col("ret1").mean().alias("mret"), pl.col("ret1").std().alias("mxs")).sort("date"))
    m = m.with_columns((1 + pl.col("mret").fill_null(0)).cum_prod().alias("lvl"))
    m = m.with_columns(pl.col("lvl").rolling_mean(60, min_samples=40).alias("ma60"),
                       pl.col("mxs").rolling_mean(20, min_samples=10).alias("mvol"))
    mv = m["mvol"].to_numpy()
    pit = np.full(len(mv), np.nan)
    for i in range(len(mv)):
        lo = max(0, i - 1250)
        w = mv[lo:i + 1]
        w = w[~np.isnan(w)]
        if len(w) >= 250 and not np.isnan(mv[i]):
            pit[i] = (w <= mv[i]).mean()
    m = m.with_columns(pl.Series("mvol_pit", pit)).with_columns(
        pl.when(pl.col("mvol_pit") >= 0.66).then(pl.lit("高波")).otherwise(pl.lit("低波")).alias("m_vol_pit"),
        pl.when(pl.col("lvl") > pl.col("ma60")).then(pl.lit("上行")).otherwise(pl.lit("下行")).alias("m_dir"),
    ).filter(pl.col("mvol_pit").is_not_null())

    d = R.base(df).filter(pl.col("exc20").is_not_null())
    d = d.join(m.select("date", "m_dir", "m_vol_pit"), on="date", how="left").filter(
        pl.col("m_vol_pit").is_not_null())
    rows_old, rows_new = [], []
    for md in ["下行", "上行"]:
        for mv_ in ["高波", "低波"]:
            s = d.filter((pl.col("m_dir") == md) & (pl.col("m_vol_pit") == mv_))
            r = R.summ(s.filter(pl.col("dd52") <= -0.35), "exc20", f"PIT {md}·{mv_} × 深跌")
            r2 = R.summ(s, "exc20", f"PIT {md}·{mv_} × 全体")
            if r:
                print(f"  {md}·{mv_:<4}深跌 {r['m_ev_bp']:>8.1f}bp t={r['t_month']:>6.2f} n={r['n']:>8}"
                      f"   全体 {r2['m_ev_bp'] if r2 else float('nan'):>8.1f}bp")
                rows_new.append({"state": f"{md}·{mv_}", "deep_bp": r["m_ev_bp"], "t": r["t_month"],
                                 "n": r["n"], "all_bp": r2["m_ev_bp"] if r2 else None})
    print("\n对照：全样本分位（原口径）")
    d2 = d.join(R._market_state(df), on="date", how="left").filter(pl.col("m_dir").is_not_null())
    for md in ["下行", "上行"]:
        for mv_ in ["高波", "低波"]:
            s = d2.filter((pl.col("m_dir") == md) & (pl.col("m_vol") == mv_))
            r = R.summ(s.filter(pl.col("dd52") <= -0.35), "exc20", f"{md}·{mv_} × 深跌")
            if r:
                print(f"  {md}·{mv_:<4}深跌 {r['m_ev_bp']:>8.1f}bp t={r['t_month']:>6.2f} n={r['n']:>8}")
                rows_old.append({"state": f"{md}·{mv_}", "deep_bp": r["m_ev_bp"], "t": r["t_month"], "n": r["n"]})
    RES["AUD5_regime"] = {"full_sample_pct": rows_old, "point_in_time": rows_new}


def main() -> None:
    df = R.load()
    print(f"panel {df.shape} clean {df['clean'].sum()} {df['date'].min()}~{df['date'].max()}")
    df = df.select([c for c in KEEP if c in df.columns])
    R.BASE_M = -42.0
    aud0(df)
    aud1(df)
    aud2(df)
    aud3(df)
    aud4(df)
    aud5(df.select([c for c in KEEP if c in df.columns]))
    OUT.write_text(json.dumps(RES, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
