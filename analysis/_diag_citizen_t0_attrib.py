"""阶段 3：归因与判据。**主口径 = 每笔交易（票日等权），不是日度等权。**

## 为什么必须钉死口径（这里两个口径给相反答案）

同一批数据，d=0.01 正T：
    日度等权  +9.7bp/日      （每天一个观测，票多的日子不更多算）
    票日等权  −8.9bp/笔      （每笔交易一个观测，这才是账户体验）

两者符号相反，因为 `corr(当日触发票数, 当日每笔收益) = −0.535`：
    触发票多 ⇒ 普跌日（2026-07-17 有 5232 只跌超 1%，每笔 −250bp）
    触发票少 ⇒ 分化日（2025-01-14 只有 223 只，每笔 +143bp）

账户实际下的是「每笔」，所以主口径取票日等权。日度等权只作为「机会密度」
的对照保留，不得用来下结论。

## 三个偏差修正

D-1 同时性：等级改用 T-1（底仓昨天就持有）。
D-2 重叠样本：per-trade 的 t 用**按日 bootstrap**（重抽交易日），
    有效样本 = 交易日数，不是票日数。
D-3 一字板：相对振幅 <0.2% 的 bar 标为不可成交，成交点只能落在非锁死 bar。

## 判据
C-1 per-trade t < 2.0 ⇒ 该格「做T不成立」。
C-2 等级差异须在振幅分层内保留。
C-3 延迟 1 根 bar 后保留率 < 0.5 ⇒ 赚的是精确踩点，实盘不可复制。
C-4 标签按日置换 200 次，实际极差须超 p95。
C-5 砍掉最好 5% 的票日后仍为正，才算不靠右尾。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
LEVELS = ["core", "active", "background", "silent"]
CN = {"core": "核心", "active": "活跃", "background": "背景", "silent": "沉默"}
B = 500


def load() -> pl.DataFrame:
    p = pl.read_parquet(OUT / "panel.parquet").sort(["symbol", "date"])
    p = p.with_columns([
        pl.col("level").shift(1).over("symbol").alias("level_prev"),
        pl.col("ampl").shift(1).over("symbol").alias("ampl_prev"),
    ])
    t = (pl.read_parquet(OUT / "trades.parquet").drop("level")
         .join(p.select(["symbol", "date", "level_prev", "ampl_prev"]),
               on=["symbol", "date"], how="inner")
         .rename({"level_prev": "level"}))
    return t.filter(pl.col("level").is_not_null())


def per_trade(df: pl.DataFrame, col: str, by: list[str], seed: int = 7) -> list[dict]:
    """票日等权的均值 + 按日 bootstrap 的 t。"""
    g = (df.filter(pl.col(col).is_finite())
           .group_by(["date"] + by)
           .agg([pl.col(col).sum().alias("s"), pl.len().alias("n")]))
    res = []
    keys = g.select(by).unique().sort(by).iter_rows(named=True)
    for k in keys:
        sub = g
        for c in by:
            sub = sub.filter(pl.col(c) == k[c])
        s, n = sub["s"].to_numpy(), sub["n"].to_numpy()
        if n.sum() < 100 or len(s) < 30:
            continue
        pt = s.sum() / n.sum()
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(s), (B, len(s)))
        bs = s[idx].sum(1) / n[idx].sum(1)
        se = float(bs.std())
        res.append({**k, "bp": pt * 1e4, "t": pt / se if se else float("nan"),
                    "lo": float(np.percentile(bs, 2.5)) * 1e4,
                    "hi": float(np.percentile(bs, 97.5)) * 1e4,
                    "trades": int(n.sum()), "days": len(s),
                    "per_day": float(n.mean())})
    return res


def main() -> None:
    t = load()
    print(f"trades {t.shape}  days {t['date'].n_unique()}")
    out: dict = {}

    # ============ A. 主结果：每笔净收益 ============
    print("\n" + "=" * 92)
    print("A. 每笔净收益（票日等权，12bp 成本已扣，t = 按 492 日 bootstrap）")
    print(f"{'d':>6}{'等级':>8}{'方向':>6}{'bp/笔':>9}{'t':>7}{'95%CI':>18}"
          f"{'笔数':>9}{'笔/日':>8}")
    for d_ in (0.005, 0.01, 0.02, 0.03):
        for dirn, tag in (("r1", "正T"), ("r2", "负T")):
            rows = per_trade(t.filter(pl.col("d") == d_), dirn, ["level", "d"])
            for r in sorted(rows, key=lambda x: x["level"]):
                print(f"{d_:>6.3f}{CN[r['level']]:>8}{tag:>6}{r['bp']:>9.1f}{r['t']:>7.2f}"
                      f"   [{r['lo']:+6.1f},{r['hi']:+6.1f}]{r['trades']:>9}{r['per_day']:>8.0f}")
                out[f"{dirn}_{d_}_{r['level']}"] = {
                    "bp": round(r["bp"], 1), "t": round(r["t"], 2),
                    "trades": r["trades"]}

    # ============ B. 口径对照：为什么不能用日度等权 ============
    print("\n" + "=" * 92)
    print("B. 口径对照（d=0.01 正T）：日均收益 vs 每笔收益，以及驱动因素")
    print(f"{'等级':>8}{'日度等权bp':>12}{'每笔bp':>10}{'差':>9}{'corr(n,日收益)':>16}")
    for lv in LEVELS:
        s = t.filter((pl.col("d") == 0.01) & (pl.col("level") == lv)
                     & pl.col("r1").is_finite())
        g = s.group_by("date").agg([pl.col("r1").mean().alias("m"),
                                    pl.len().alias("n")])
        pt = per_trade(s, "r1", ["level"])[0]["bp"]
        corr = float(np.corrcoef(g["n"], g["m"])[0, 1])
        print(f"{CN[lv]:>8}{g['m'].mean()*1e4:>12.1f}{pt:>10.1f}"
              f"{g['m'].mean()*1e4 - pt:>9.1f}{corr:>16.3f}")

    # ============ C. 过拟合检验：延后一根 bar ============
    print("\n" + "=" * 92)
    print("C. 延后 1 根 bar（5 分钟）后的每笔净收益。保留率 <0.5 ⇒ 靠精确踩点")
    print(f"{'d':>6}{'等级':>8}{'方向':>6}{'准点':>9}{'t':>7}{'延后':>9}{'t':>7}{'保留':>7}")
    for d_ in (0.01, 0.02):
        for dirn, tag in (("r1", "正T"), ("r2", "负T")):
            base = {r["level"]: r for r in per_trade(
                t.filter(pl.col("d") == d_), dirn, ["level", "d"])}
            lag = {r["level"]: r for r in per_trade(
                t.filter(pl.col("d") == d_), dirn + "_lag1", ["level", "d"])}
            for lv in LEVELS:
                if lv not in base or lv not in lag:
                    continue
                keep = lag[lv]["bp"] / base[lv]["bp"] if base[lv]["bp"] else float("nan")
                print(f"{d_:>6.3f}{CN[lv]:>8}{tag:>6}{base[lv]['bp']:>9.1f}"
                      f"{base[lv]['t']:>7.2f}{lag[lv]['bp']:>9.1f}{lag[lv]['t']:>7.2f}"
                      f"{keep:>7.2f}")

    # ============ D. 总账 ============
    print("\n" + "=" * 92)
    print("D. 总账（d=0.01）：底仓日内漂移 vs 做T增量。三者相加才是账户当日变化")
    print(f"{'等级':>8}{'底仓日内':>10}{'t':>7}{'正T增量':>9}{'t':>7}{'负T增量':>9}{'t':>7}{'最优方向':>10}")
    for lv in LEVELS:
        s = t.filter((pl.col("d") == 0.01) & (pl.col("level") == lv))
        a = {r["level"]: r for r in per_trade(s, "r3", ["level"])}[lv]
        b = {r["level"]: r for r in per_trade(s, "r1", ["level"])}[lv]
        c = {r["level"]: r for r in per_trade(s, "r2", ["level"])}[lv]
        best = "正T" if b["bp"] > c["bp"] else "负T"
        print(f"{CN[lv]:>8}{a['bp']:>10.1f}{a['t']:>7.2f}{b['bp']:>9.1f}{b['t']:>7.2f}"
              f"{c['bp']:>9.1f}{c['t']:>7.2f}{best:>10}")
        out[f"ledger_{lv}"] = {"base": round(a["bp"], 1), "r1": round(b["bp"], 1),
                               "r2": round(c["bp"], 1)}

    # ============ E. 成本敏感性 ============
    print("\n" + "=" * 92)
    print("E. 成本敏感性（d=0.01 正T，每笔）：每多 4bp 成本吃掉多少")
    print(f"{'等级':>8}{'12bp':>9}{'16bp':>9}{'20bp':>9}{'30bp':>9}")
    for lv in LEVELS:
        b = {r["level"]: r for r in per_trade(
            t.filter((pl.col("d") == 0.01) & (pl.col("level") == lv)), "r1", ["level"])}[lv]
        print(f"{CN[lv]:>8}" + "".join(f"{b['bp'] - (c - 12):>9.1f}" for c in (12, 16, 20, 30)))

    # ============ F. 右尾 ============
    print("\n" + "=" * 92)
    print("F. 右尾依赖（正T）：砍掉最好 5% 的票日后")
    for d_ in (0.01, 0.02):
        for lv in LEVELS:
            x = t.filter((pl.col("d") == d_) & (pl.col("level") == lv)
                         & pl.col("r1").is_finite())["r1"].to_numpy()
            if x.size < 500:
                continue
            cut = int(np.floor(0.95 * x.size))
            tr = np.sort(x)[:cut]
            print(f"d={d_:.2f} {CN[lv]:<4} raw={x.mean()*1e4:+7.1f} "
                  f"trim5={tr.mean()*1e4:+7.1f} hit={float((x>0).mean()):.3f} "
                  f"n={x.size}")

    # ============ G. 双排序 ============
    print("\n" + "=" * 92)
    print("G. 双排序（d=0.01 正T，每笔 bp / t）：同一昨日振幅档内比等级")
    s = (t.filter((pl.col("d") == 0.01) & pl.col("ampl_prev").is_not_null())
         .with_columns(
             (pl.col("ampl_prev").rank("ordinal").over("date") * 5
              // pl.col("ampl_prev").count().over("date")).alias("ab")))
    for b in range(5):
        line = f"振幅Q{b+1}"
        for lv in LEVELS:
            ss = s.filter((pl.col("ab") == b) & (pl.col("level") == lv))
            rows = per_trade(ss, "r1", ["level"])
            if not rows:
                line += f"  {CN[lv][:2]}=  n/a  "
                continue
            r = rows[0]
            line += f"  {CN[lv][:2]}={r['bp']:+6.1f}/{r['t']:+4.1f}"
        print(line)

    # ============ H. 新维度：盘中 10:00 可知的分化度 ============
    print("\n" + "=" * 92)
    print("H. 用「09:30-10:00 触发票占比」(early_share) 当开关 —— 盘中可知，无前视")
    print("   交易口径 r1_late：10:00 之后才允许买入（前 6 根 bar 不得成交）")
    s = t.filter(pl.col("d") == 0.01)
    ds = (s.group_by("date").agg([
        pl.col("early_share").first().alias("es"),
        pl.col("r1").is_finite().sum().alias("n_fill"),
        pl.len().alias("n_all")]))
    ds = ds.with_columns((pl.col("n_fill") / pl.col("n_all")).alias("full_share"))
    print(f"   early_share 与全天触发占比 相关性 = "
          f"{float(np.corrcoef(ds['es'], ds['full_share'])[0,1]):.3f}")
    q = ds["es"].quantile([0.2, 0.4, 0.6, 0.8])
    ds = ds.with_columns(pl.when(pl.col("es") < q[0]).then(0)
                         .when(pl.col("es") < q[1]).then(1)
                         .when(pl.col("es") < q[2]).then(2)
                         .when(pl.col("es") < q[3]).then(3)
                         .otherwise(4).alias("dq"))
    s2 = s.join(ds.select(["date", "dq", "es"]), on="date")
    print(f"   {'档':<6}{'early_share':>12}{'r1_late bp/笔':>15}{'t':>7}"
          f"{'r1 全时段':>11}{'t':>7}{'笔/日':>8}{'日数':>6}")
    for b in range(5):
        rows = per_trade(s2.filter(pl.col("dq") == b), "r1_late", ["dq"])
        rows2 = per_trade(s2.filter(pl.col("dq") == b), "r1", ["dq"])
        if not rows or not rows2:
            continue
        r, r2 = rows[0], rows2[0]
        es = float(ds.filter(pl.col("dq") == b)["es"].mean())
        print(f"   Q{b+1:<5}{es:>12.3f}{r['bp']:>15.1f}{r['t']:>7.2f}"
              f"{r2['bp']:>11.1f}{r2['t']:>7.2f}{r['per_day']:>8.0f}{r['days']:>6}")
        out[f"regime_Q{b+1}"] = {"es": round(es, 3), "bp": round(r["bp"], 1),
                                 "t": round(r["t"], 2), "days": r["days"]}

    # ============ J. 稳定性：前半 / 后半 ============
    print("\n" + "=" * 92)
    print("J. 稳定性：按日序切前后半（d=0.01 正T，每笔净 bp / t）")
    dates = sorted(t["date"].unique().to_list())
    mid = dates[len(dates) // 2]
    print(f"   前半 {dates[0]}~{dates[len(dates)//2-1]}  后半 {mid}~{dates[-1]}")
    print(f"{'等级':>8}{'H1':>9}{'t':>7}{'H2':>9}{'t':>7}{'方向一致':>9}")
    for lv in LEVELS:
        s = t.filter((pl.col("d") == 0.01) & (pl.col("level") == lv))
        a = per_trade(s.filter(pl.col("date") < mid), "r1", ["level"])
        b = per_trade(s.filter(pl.col("date") >= mid), "r1", ["level"])
        if not a or not b:
            continue
        same = "是" if (a[0]["bp"] > 0) == (b[0]["bp"] > 0) else "否"
        print(f"{CN[lv]:>8}{a[0]['bp']:>9.1f}{a[0]['t']:>7.2f}"
              f"{b[0]['bp']:>9.1f}{b[0]['t']:>7.2f}{same:>9}")
        out[f"stable_{lv}"] = {"h1": round(a[0]["bp"], 1), "h2": round(b[0]["bp"], 1)}

    # ============ K. 毛收益 vs 成本 ============
    print("\n" + "=" * 92)
    print("K. 毛收益 vs 成本（d=0.01 正T）：毛 = 净 + 12bp。毛 > 12 才有生存空间")
    print(f"{'等级':>8}{'毛收益':>9}{'净@12':>9}{'净@8':>9}{'净@6':>9}{'日均成交额':>12}")
    for lv in LEVELS:
        r = {x["level"]: x for x in per_trade(
            t.filter((pl.col("d") == 0.01) & (pl.col("level") == lv)), "r1", ["level"])}[lv]
        gross = r["bp"] + 12
        print(f"{CN[lv]:>8}{gross:>9.1f}{r['bp']:>9.1f}{gross-8:>9.1f}{gross-6:>9.1f}"
              f"{r['per_day']:>12.0f}笔/日")

    # ============ I. 置换负控 ============
    print("\n" + "=" * 92)
    print("I. 负控：等级标签按日整体置换 200 次（d=0.01 正T，每笔）")
    obs = {r["level"]: r["bp"] for r in per_trade(
        t.filter(pl.col("d") == 0.01), "r1", ["level"])}
    obs_range = max(obs.values()) - min(obs.values())
    s = t.filter(pl.col("d") == 0.01).select(["date", "level", "r1"])
    lv_np = s["level"].to_numpy().astype(object)
    r1_np = np.where(np.isfinite(s["r1"].to_numpy()), s["r1"].to_numpy(), np.nan)
    uniq, inv = np.unique(s["date"].to_numpy(), return_inverse=True)
    order = np.argsort(inv, kind="stable")
    bounds = np.searchsorted(inv[order], np.arange(len(uniq) + 1))
    rng = np.random.default_rng(20261118)
    ranges = []
    for _ in range(200):
        pl_ = lv_np.copy()
        for gi in range(len(uniq)):
            sl = order[bounds[gi]:bounds[gi + 1]]
            pl_[sl] = rng.permutation(lv_np[sl])
        m = []
        for lv in LEVELS:
            x = r1_np[pl_ == lv]
            x = x[np.isfinite(x)]
            m.append(x.mean() * 1e4 if x.size else np.nan)
        ranges.append(np.nanmax(m) - np.nanmin(m))
    ranges = np.array(ranges)
    print(f"实际极差 {obs_range:.1f}bp，置换 p50={np.percentile(ranges,50):.1f} "
          f"p95={np.percentile(ranges,95):.1f}  p={(ranges>=obs_range).mean():.3f}")
    print("  实观测: " + "  ".join(f"{CN[k]}={v:+.1f}" for k, v in obs.items()))
    out["perm"] = {"range": round(obs_range, 1),
                   "p95": round(float(np.percentile(ranges, 95)), 1),
                   "p": round(float((ranges >= obs_range).mean()), 3)}

    (OUT / "attrib.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT/'attrib.json'}")


if __name__ == "__main__":
    main()
