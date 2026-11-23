"""验证用户命题：「每次做T赚1%-2%，积少成多，成本降为负数」。

数据：data/_probe_t0/trades.parquet（2.6M 票日 / 1,061 万笔，2024-11-06~2026-11-16，492 日）
口径：底仓 T+0 等量往返；正T=跌破锚 d% 买、回锚卖；负T=冲高 d% 卖、回锚买；14:55 强制回锚。
      成交只用 bar 的 open/close，不用 high/low。r1/r2 为**净**收益（已扣 12bp 往返）。

A 振幅分布      「每天都有 1-2% 空间」在振幅层面是否成立
B 胜率与赔率    「赚1%」那一半多大，另一半亏多少 —— 这是命题真正的断点
C 抓取率 rho    实际吃掉振幅的百分之几
D 弹性          选「波动足够」的票，笔毛是否放大（事后分档，不是信号）
E 上限与实现    完美做T vs 可执行规则，差多少倍
F 负成本算术    需要多少次、多高胜率
G 底仓对比      做T把账户收益变成多少
H 卖飞的代价    主升浪日做负T的结果（满仓做T的唯一方向）

ASCII only (GBK console).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
COST_BP = 12.0
RESULT = {}


def q(x, ps=(1, 5, 10, 25, 50, 75, 90, 95, 99)) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {}
    return {f"p{p}": float(np.percentile(x, p)) for p in ps}


def main() -> None:
    t = pl.read_parquet(OUT / "trades.parquet")
    print(f"[load] {t.height:,} rows / {t['date'].n_unique()} days / "
          f"{t['symbol'].n_unique():,} symbols / d={sorted(t['d'].unique().to_list())}")

    # ================= A =================
    a = (t.filter(pl.col("d") == 0.01)
         .select(["symbol", "date", "ampl"])
         .filter(pl.col("ampl").is_finite() & (pl.col("ampl") > 0))
         .unique(subset=["symbol", "date"]))
    ampl = a["ampl"].to_numpy() * 100.0
    print(f"\n[A] 日内振幅分布   n={ampl.size:,} 票日 / {a['date'].n_unique()} 日")
    print(f"    均值 {ampl.mean():.2f}%   中位 {np.median(ampl):.2f}%")
    dq = q(ampl)
    print("    " + "  ".join(f"{k}={v:.2f}%" for k, v in dq.items()))
    thresh = {}
    print(f"\n    {'阈值':<10}{'票日占比':>10}{'合每年':>12}")
    for th in (1.0, 2.0, 3.0, 4.0, 5.0, 7.0):
        sh = float((ampl >= th).mean())
        thresh[f"{th}"] = sh
        print(f"    >= {th:>4.1f}%{sh:>10.1%}{sh*242:>10.0f} 天")
    RESULT["A_ampl"] = {"n": int(ampl.size), "mean": float(ampl.mean()),
                        "median": float(np.median(ampl)), "quantiles": dq,
                        "share_ge": thresh}

    # ================= B =================
    print("\n[B] 触发后的两半（净额已扣 12bp；盈亏平衡胜率 = |输均值|/(赢均值+|输均值|)）")
    print(f"    {'方向':<9}{'d':>5}{'笔数':>10}{'触发':>7}{'胜率':>7}{'赢均':>8}"
          f"{'输均':>8}{'盈亏比':>7}{'平衡胜率':>9}{'笔净':>8}{'笔毛':>7}{'p1':>7}")
    btab = {}
    for col, name, fc in (("r1", "正T低吸", "f1"), ("r2", "负T高抛", "f2")):
        for dd in (0.005, 0.01, 0.02, 0.03):
            tot = t.filter(pl.col("d") == dd).height
            s = t.filter((pl.col("d") == dd) & pl.col(fc) & pl.col(col).is_finite())
            r = s[col].to_numpy() * 1e4
            win = r > 0
            hit = float(win.mean())
            wm = float(r[win].mean())
            lm = float(r[~win].mean())
            be = abs(lm) / (wm + abs(lm))
            print(f"    {name:<9}{dd*100:>4.1f}%{r.size:>10,}{s.height/tot:>7.1%}{hit:>7.1%}"
                  f"{wm:>+8.0f}{lm:>+8.0f}{abs(wm/lm):>7.2f}{be:>9.1%}"
                  f"{r.mean():>+8.1f}{r.mean()+COST_BP:>+7.1f}{np.percentile(r,1):>+7.0f}")
            btab[f"{col}_{dd}"] = {"n": int(r.size), "fill": s.height / tot, "hit": hit,
                                   "win_mean_bp": wm, "loss_mean_bp": lm,
                                   "breakeven_hit": be, "net_bp": float(r.mean()),
                                   "gross_bp": float(r.mean()) + COST_BP,
                                   "p1_bp": float(np.percentile(r, 1)),
                                   "min_bp": float(r.min())}
    RESULT["B_payoff"] = btab

    # ================= C =================
    s = (t.filter((pl.col("d") == 0.01) & pl.col("f1") & pl.col("r1").is_finite()
                  & pl.col("ampl").is_finite() & (pl.col("ampl") > 0.002))
         .select(["symbol", "date", "r1", "ampl"])
         .with_columns(((pl.col("r1") * 1e4 + COST_BP) / (pl.col("ampl") * 1e4)).alias("rho"),
                       (pl.col("ampl") * 100).alias("ap")))
    rho = s["rho"].to_numpy()
    print(f"\n[C] 抓取率 rho = 笔毛 / 当日振幅（d=1%，正T，n={rho.size:,}）")
    print(f"    中位 {np.median(rho):.3f}   均值 {rho.mean():.3f}   "
          f"{'  '.join(f'{k}={v:.3f}' for k, v in q(rho, (5, 25, 50, 75, 95)).items())}")
    print(f"    若「每次赚 1%」，需要 rho = (1%+0.12%)/振幅：振幅3.5%时需 0.320，"
          f"实测 {np.median(rho):.3f}")
    print(f"\n    {'振幅档':<12}{'笔数':>9}{'振幅均':>9}{'笔毛':>8}{'rho':>8}")
    ctab = {}
    for lo, hi in ((0.2, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 100)):
        g = s.filter((pl.col("ap") >= lo) & (pl.col("ap") < hi))
        if g.height < 500:
            continue
        am, gr, gm = float(g["ap"].mean()), float(g["r1"].mean() * 1e4 + COST_BP), float(g["rho"].mean())
        print(f"    {lo:.1f}~{hi if hi < 100 else '+'}{'%':<6}{'':<2}{g.height:>9,}"
              f"{am:>8.2f}%{gr:>+8.1f}{gm:>8.3f}")
        ctab[f"{lo}-{hi}"] = {"n": int(g.height), "ampl": am, "gross_bp": gr, "rho": gm}
    RESULT["C_rho"] = {"n": int(rho.size), "median": float(np.median(rho)),
                       "mean": float(rho.mean()), "by_ampl": ctab}

    # ================= D =================
    s2 = (t.filter((pl.col("d") == 0.01) & pl.col("f1") & pl.col("r1").is_finite()
                   & pl.col("ampl").is_finite() & (pl.col("ampl") > 0.002))
          .select(["symbol", "date", "r1", "ampl"])
          .with_columns((pl.col("r1") * 1e4 + COST_BP).alias("g")))
    s2 = s2.with_columns([
        (pl.col("g") - pl.col("g").mean().over("date")).alias("g_dm"),
        (pl.col("ampl") - pl.col("ampl").mean().over("date")).alias("a_dm"),
    ]).with_columns(
        (pl.col("ampl").rank("ordinal").over("date")
         / pl.col("ampl").count().over("date") * 10).ceil().clip(1, 10).cast(pl.Int32).alias("dec"))
    print("\n[D] 事后按当日振幅十分位（不是信号，是「波动大是否更好做」的直接回答）")
    print(f"    {'分位':<7}{'振幅':>8}{'笔毛':>8}{'rho':>8}{'笔净':>8}{'n':>10}")
    dtab = {}
    for r in s2.group_by("dec").agg([pl.col("ampl").mean().alias("a"),
                                     pl.col("g").mean().alias("gb"),
                                     pl.len().alias("n")]).sort("dec").iter_rows(named=True):
        print(f"    Q{int(r['dec']):<6}{r['a']*100:>7.2f}%{r['gb']:>+8.1f}"
              f"{r['gb']/(r['a']*1e4):>8.3f}{r['gb']-COST_BP:>+8.1f}{r['n']:>10,}")
        dtab[f"Q{int(r['dec'])}"] = {"ampl": float(r["a"]), "gross_bp": float(r["gb"]),
                                     "n": int(r["n"])}
    b = float(np.polyfit(s2["a_dm"].to_numpy(), s2["g_dm"].to_numpy(), 1)[0])
    print(f"    回归：同日振幅每高 1.00pct ⇒ 笔毛 {b/100:+.2f}bp（每分钟一档的斜率）")
    RESULT["D_elasticity"] = {"slope_bp_per_pct": b / 100, "deciles": dtab}

    # ================= E =================
    print("\n[E] 上限 vs 实现（单笔/单日，不复利外推）")
    ev = (t.filter((pl.col("d") == 0.01) & pl.col("ampl").is_finite() & (pl.col("ampl") > 0.002))
          .select(["symbol", "date", "ampl"]).unique(subset=["symbol", "date"])["ampl"].to_numpy())
    perfect = float(ev.mean() * 1e4)
    print(f"    完美做T（每日买最低、卖最高，吃满振幅）  {perfect:>8.1f} bp/日  "
          f"⇒ 242 日单利 {perfect*242/100:>7.0f}%")
    for lab, dd in (("正T d=0.5%", 0.005), ("正T d=1.0%", 0.01),
                    ("正T d=2.0%", 0.02), ("正T d=3.0%", 0.03)):
        tot = t.filter(pl.col("d") == dd).height
        s3 = t.filter((pl.col("d") == dd) & pl.col("f1") & pl.col("r1").is_finite())
        pd_ = float(s3["r1"].sum() / tot) * 1e4
        print(f"    {lab:<38}{pd_:>8.1f} bp/日  ⇒ 242 日单利 {pd_*242/100:>7.0f}%  "
              f"(触发 {s3.height/tot:.0%})")
    print(f"    ⇒ 完美/实现 的比值 = {perfect/abs(-4.4):.0f} 倍；这 100 倍就是「择时精度」的价格")
    RESULT["E_ceiling"] = {"perfect_bp_per_day": perfect}

    # ================= F =================
    print("\n[F] 「成本降为负」的算术（底仓成本归一为 100%）")
    print(f"    {'每次赚':>7}{'做T仓位':>9}{'需要笔数':>10}{'按每年242次':>13}")
    ftab = {}
    for pct in (1.0, 2.0):
        for f in (1.0, 0.5, 0.3):
            need = 100.0 / (pct * f)
            print(f"    {pct:>6.1f}%{f:>9.0%}{need:>10.0f}{need/242:>11.1f} 年")
            ftab[f"{pct}_{f}"] = need
    print(f"    但以上要求「零亏损」。按实测 d=1% 正T：胜率 51.4%、赢 +125bp、输 -151bp")
    print(f"      期望 = 0.514*125 - 0.486*151 = {0.514*125-0.486*151:+.1f}bp/笔 = "
          f"{(0.514*125-0.486*151)/1e4*100:+.3f}%/笔")
    print(f"      100% 成本 / 0.031% 笔毛 = {100/0.031:.0f} 笔 = {100/0.031/242:.0f} 年（且笔毛必须恒定为正）")
    RESULT["F_negative_cost"] = ftab

    # 胜率敏感度
    print(f"\n    胜率敏感度（赔率 125:151 固定，242 笔/年复利）：")
    for p in (0.514, 0.547, 0.55, 0.57, 0.60, 0.65):
        ev_bp = p * 125 - (1 - p) * 151
        ann = ((1 + ev_bp / 1e4) ** 242 - 1) * 100
        print(f"      胜率 {p:.1%} ⇒ 笔期望 {ev_bp:>+6.1f}bp ⇒ 年化 {ann:>+8.1f}%")
    RESULT["F_hit_sensitivity"] = {str(p): float(p * 125 - (1 - p) * 151)
                                   for p in (0.514, 0.547, 0.55, 0.57, 0.60)}

    # ================= G =================
    dg = (t.filter(pl.col("d") == 0.01).select(["symbol", "date", "i1"])
          .unique(subset=["symbol", "date"]).group_by("date")
          .agg(pl.col("i1").mean()).sort("date"))
    m = dg["i1"].to_numpy()
    n = len(m)
    hold = (np.prod(1 + m) - 1) * 100
    hold_t = (np.prod(1 + m - 4.5 / 1e4) - 1) * 100
    print("\n[G] 底仓 vs 做T（同期 492 日，全市场等权，日内开→收）")
    print(f"    死拿（无T）                  日均 {m.mean()*1e4:>+6.1f}bp   累计 {hold:>+7.1f}%")
    print(f"    每日做一次正T（d=1%，全仓）  日均 {m.mean()*1e4-4.4:>+6.1f}bp   累计 {hold_t:>+7.1f}%")
    print(f"    ⇒ 做T 从账户拿走 {(hold-hold_t):.1f} 个百分点")
    RESULT["G_base"] = {"eq_intraday_bp": float(m.mean() * 1e4), "hold_pct": float(hold),
                        "hold_with_t_pct": float(hold_t), "days": n}

    # ================= H =================
    print("\n[H] 卖飞的代价：按当日日内方向 i1 分档，负T（满仓做T唯一可行的方向）")
    s4 = (t.filter((pl.col("d") == 0.01) & pl.col("f2") & pl.col("r2").is_finite()
                   & pl.col("i1").is_finite())
          .select(["symbol", "date", "r2", "i1"])
          .with_columns((pl.col("i1") * 100).alias("ip")))
    print(f"    {'当日涨幅':<14}{'笔数':>9}{'负T笔净':>10}{'胜率':>7}{'笔毛':>8}")
    htab = {}
    for lo, hi in ((-100, -3), (-3, -1), (-1, 0), (0, 1), (1, 3), (3, 100)):
        g = s4.filter((pl.col("ip") >= lo) & (pl.col("ip") < hi))
        if g.height < 500:
            continue
        r = g["r2"].to_numpy() * 1e4
        lab = f"{lo if lo > -100 else '<'}~{hi if hi < 100 else '>'}%"
        print(f"    {lab:<14}{r.size:>9,}{r.mean():>+10.1f}{(r>0).mean():>7.1%}{r.mean()+COST_BP:>+8.1f}")
        htab[lab] = {"n": int(r.size), "net_bp": float(r.mean()), "hit": float((r > 0).mean())}
    RESULT["H_sell_fly"] = htab

    # ================= I =================
    # 「做T赚的钱」是不是「股票涨的钱」的伪装？
    # 按 symbol 汇总：累计做T净收益 vs 同期累计日内收益（i1 之和）
    print("\n[I] 横截面：做T累计收益 与 该股同期涨幅 的关系")
    per = (t.filter(pl.col("d") == 0.01)
           .select(["symbol", "date", "r1", "i1"])
           .group_by("symbol")
           .agg([pl.col("r1").fill_nan(0.0).sum().alias("t_sum"),
                 pl.col("i1").fill_nan(0.0).sum().alias("px_sum"),
                 pl.len().alias("n")])
           .filter(pl.col("n") >= 200))
    tx, px = per["t_sum"].to_numpy() * 1e4, per["px_sum"].to_numpy() * 100
    ok = np.isfinite(tx) & np.isfinite(px)
    tx, px = tx[ok], px[ok]
    rho_s = float(np.corrcoef(tx, px)[0, 1])
    print(f"    n={tx.size:,} 只（>=200 日）  累计做T收益 中位 {np.median(tx):>+8.0f}bp  "
          f"累计涨幅 中位 {np.median(px):>+6.1f}%")
    print(f"    相关系数 Pearson={rho_s:+.3f}")
    dec = np.percentile(px, [0, 20, 40, 60, 80, 100])
    print(f"    {'该股累计涨幅档':<20}{'只数':>7}{'做T累计bp':>12}{'中位':>10}")
    itab = {}
    for lo, hi in zip(dec[:-1], dec[1:]):
        m = (px >= lo) & (px <= hi)
        lab = f"{lo:+.0f}~{hi:+.0f}%"
        print(f"    {lab:<20}{m.sum():>7,}{tx[m].mean():>+12.0f}{np.median(tx[m]):>+10.0f}")
        itab[lab] = {"n": int(m.sum()), "t_mean_bp": float(tx[m].mean())}
    RESULT["I_beta_disguise"] = {"pearson": rho_s, "by_px": itab}

    # ================= J =================
    # 按实测经验分布 bootstrap 10 年（2520 笔），看终值分布
    print("\n[J] 按实测经验分布 bootstrap：10 年（242 日/年，每日至多 1 笔，未触发记 0）")
    s5 = (t.filter((pl.col("d") == 0.01) & pl.col("r1").is_finite())
          .select(["symbol", "date", "r1"]))
    obs = s5["r1"].to_numpy()
    # 票日级日净分布：未触发日 = 0（不是 -12bp），按 date 聚合出「随机抽一个票日」的分布
    day_pool = (t.filter(pl.col("d") == 0.01)
                .select(pl.col("r1").fill_nan(0.0))["r1"].to_numpy())
    rng = np.random.default_rng(20261122)
    B, Y = 5000, 10
    draws = rng.choice(day_pool, size=(B, 242 * Y), replace=True)
    nav = np.cumprod(1 + draws, axis=1)[:, -1]
    print(f"    日净池：n={day_pool.size} 日均 {day_pool.mean()*1e4:+.1f}bp "
          f"（{(day_pool > 0).mean():.1%} 的天为正）")
    print(f"    10 年终值  P5={np.percentile(nav,5):.2f}x  P25={np.percentile(nav,25):.2f}x  "
          f"中位={np.median(nav):.2f}x  P75={np.percentile(nav,75):.2f}x  "
          f"P95={np.percentile(nav,95):.2f}x")
    print(f"    亏损概率 {float((nav < 1).mean()):.1%}   "
          f"翻倍概率 {float((nav > 2).mean()):.1%}")
    print(f"    （不含底仓自身涨跌；只算做T增量）")
    RESULT["J_bootstrap"] = {"p5": float(np.percentile(nav, 5)),
                             "median": float(np.median(nav)),
                             "p95": float(np.percentile(nav, 95)),
                             "p_loss": float((nav < 1).mean())}

    (Path(__file__).parent / "_diag_t0_thesis.json").write_text(
        json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[done] -> analysis/_diag_t0_thesis.json")


if __name__ == "__main__":
    main()
