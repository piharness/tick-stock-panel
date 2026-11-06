"""全截面 IC：宽度能不能把每笔 MDE 从 200bp 压到 50bp？不能，原因不是样本量。

`probe_net_diff.py` 的 MDE 是 202–588bp，成本 19bp。怀疑的瓶颈是「每天只用
20 只，扔掉了 99% 的截面」。本脚本用同一批决策日、同一份 enriched 表，把信号
放到全市场（约 5,500 只）上重算，四件事并排：

  1. **秩 IC**：全截面的 Spearman，方差稳定、抗尾部，是「信号有没有排序能力」
     的高功效检验；
  2. **同口径宽度**：top10%（约 540 只）对中位数，和 20 只口径同一个泛函，
     只差宽度；
  3. **十分位多空**：top10% − bottom10%，市场和规模中性，是另一个泛函；
  4. **IC 映射**：α_t = IC_t × σ_fwd,t × z̄_top20,t，把秩相关按线性刻度折回 bp。

三张派生表决定这四个数能不能互相替代：

- **换算率**：把实际 bp 对 IC 做无截距回归。斜率应等于 σ·z̄≈2,300bp，四个
  策略各估一次；差几倍就说明 IC 与「20 只等权均值」不是同一个泛函。
- **尾差**：同日配对，top-20 减 top10%。极端 0.4% 若系统性差于顶层 10%，
  宽度收益就不能搬回产品口径。
- **十分位形状**：去当日截面均值后的十层收益，看单调性；只在头部翘起说明
  线性映射失效。

⚠️ 五条边界：

- 325 天已经看过（`docs/decision-target-prereg.md` §5），本脚本读数只能用于
  **下一段样本的设计**，不能当确认结论。
- `z̄` 用标准化秩，不用正态分数。正态分数把 top-20 的 z̄ 从 ≈1.73 抬到 ≈2.6，
  bp 读数跟着涨 50%；两者都只是线性预测器的刻度，选一个写死，不事后挑。
- IC 是**线性于秩**的度量。信号若只在尾部有效（top-20 有效、中间无效），IC
  会低估产品口径的 alpha。此时「IC 映射 < 实际」是信息，不是错。
- `exc` 的水平被右偏抬高：全样本「均值 − 中位数」约 +200bp，所以十分位表必须
  去均值，否则十层全正、看不出形状。
- 决策日列表取自 `.probe_per_day_exc.json`，与 `probe_net_diff.py --nmax=325`
  同源同序。缓存缺日直接报错，不静默跳过——跳一天就会变成另一个样本。

用法：
    python scripts/probe_cs_ic.py [--nmax=325] [--rebuild]
"""
from __future__ import annotations

import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TABLE = "kline_daily_enriched"
CACHE = DATA / ".probe_cs_ic.json"
EXC = DATA / ".probe_per_day_exc.json"

HORIZON, LOOKBACK, TOP_N = 20, 20, 20
DECILE = 0.1
BLOCK, REPS, SEED = 40, 1000, 0
#: 与 `probe_net_diff.py` / `pwin._MDE_Z` 同源，契约测试钉住。
MDE_Z = 2.486
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)
REBUILD = "--rebuild" in sys.argv

#: 带方向的信号：一律「取排序后的前 20」，方向进符号，出表时 IC 正 = 策略有效。
SIGNALS = {
    "momentum_20": ("mom", 1.0),
    "reversal_20": ("mom", -1.0),
    "low_turnover": ("turn", -1.0),
    "high_turnover": ("turn", 1.0),
}


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ppd = _load("probe_paired_diff", "scripts/probe_paired_diff.py")
se_hac, se_block = _ppd.se_hac, _ppd.se_block


def sessions() -> list[str]:
    return sorted(c.name.split("=", 1)[1] for c in (DATA / TABLE).iterdir()
                  if c.is_dir() and c.name.startswith("date="))


def day_frame(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "turnover_rate", "amount"]).collect()


def _rank_z(col: str, n: int) -> pl.Expr:
    """标准化秩。分母用秩的标准差，使 z 的截面 sd = 1，IC 与 OLS 斜率同刻度。"""
    return (pl.col(col).rank("average") - (n + 1) / 2) / math.sqrt((n * n - 1) / 12)


def _spearman(d: pl.DataFrame, x: str, y: str) -> float:
    """秩相关 = 标准化秩的 Pearson 相关。单列实现，避免两处刻度不一致。"""
    n = d.height
    return float(d.select(pl.corr(_rank_z(x, n), _rank_z(y, n))).item())


def _quintile_ic(base: pl.DataFrame) -> float:
    """成交额五分位内的秩相关，按组内样本量加权。

    这是 `probe_selection_tilt.py` 的「同层基准」在高分辨率下的版本：
    同一件事（控规模/流动性），20 只对同层中位数 → 1,100 只对同层做相关。
    """
    g = (base.with_columns(
            (pl.col("amount").rank("average") / pl.len() * 5).floor()
            .clip(0, 4).alias("q"))
        .with_columns(
            pl.col("s").rank("average").over("q").alias("rs"),
            pl.col("fwd").rank("average").over("q").alias("rf"))
        .group_by("q")
        .agg(pl.corr("rs", "rf").alias("ic"), pl.len().alias("m")))
    g = g.drop_nulls("ic")
    if g.is_empty():
        return float("nan")
    return float((g["ic"] * g["m"]).sum() / g["m"].sum())


def build(rebuild: bool = False) -> dict[str, dict[str, dict]]:
    if CACHE.exists() and not rebuild:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    cal = sessions()
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    out: dict[str, dict[str, dict]] = {k: {} for k in SIGNALS}
    for i in range(LOOKBACK, len(cal) - HORIZON):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("turnover_rate").alias("turn"),
                                    pl.col("amount")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0)
                    & pl.col("amount").is_not_null()
                    & pl.col("turn").is_not_null())
        )
        if base.height < 200:
            continue
        base = base.with_columns(
            (pl.col("c1") / pl.col("c0") - 1.0).alias("mom"),
            (pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"),
        )
        n = base.height
        med = float(base["fwd"].median())
        sigma_bp = float(base["fwd"].std()) * 1e4
        for name, (col, sign) in SIGNALS.items():
            d = base.with_columns(
                (pl.col(col) * sign).alias("s"),
                (pl.col("fwd") - med).alias("exc"),
            )
            d = d.with_columns(_rank_z("s", n).alias("zs"))
            ic = _spearman(d, "s", "fwd")
            picks = d.sort("s", descending=True).head(TOP_N)
            if picks.height != TOP_N:
                continue
            k = max(1, int(round(n * DECILE)))
            lo = d.sort("s", descending=False).head(k)
            wide = d.sort("s", descending=True).head(k)
            #: 分位表去**当日截面均值**，不是中位数：`exc` 的水平被右偏拖到
            #: +200bp，不去均值的话十层全是正数，看不出形状。
            dec = (d.with_columns(
                       (pl.col("s").rank("average") / pl.len() * 10).floor()
                       .clip(0, 9).cast(pl.Int8).alias("dec"),
                       (pl.col("fwd") - pl.col("fwd").mean()).alias("excm"))
                   .group_by("dec").agg(pl.col("excm").mean().alias("m"))
                   .sort("dec"))
            out[name][t] = {
                "n": n,
                "ic": ic,
                "ic_q": _quintile_ic(d),
                "zbar": float(picks["zs"].mean()),
                "sigma_bp": sigma_bp,
                "top_bp": float(picks["exc"].mean()) * 1e4,
                "pick_sd_bp": float(picks["exc"].std()) * 1e4,
                #: 同口径（对中位数）但宽度 ×28：把「放宽持仓」和「换估计量」
                #: 两件事分开，否则宽度收益会被长短腿的方差盖掉。
                "wide_bp": (float(wide["fwd"].mean()) - med) * 1e4,
                "spread_bp": (float(picks["fwd"].mean())
                              - float(lo["fwd"].mean())) * 1e4,
                #: 十分位相对收益（bp，去当日截面均值）。判断「top-20 vs IC」
                #: 的分歧是不是尾部非线性：单调则线性映射成立，头部翘起则
                #: 映射会低估。只读形状，不读水平。
                "dec_bp": [float(v) * 1e4 for v in dec["m"].to_list()],
            }
    CACHE.write_text(json.dumps(out), encoding="utf-8")
    return out


def main() -> None:
    if not EXC.exists():
        raise SystemExit(f"缺少 {EXC}；先跑 `python scripts/probe_window.py --rebuild`")
    days = sorted(set.intersection(
        *(set(v) for v in json.loads(EXC.read_text(encoding="utf-8")).values())))
    if NMAX:
        days = days[:NMAX]
    raw = build(rebuild=REBUILD)
    missing = [d for d in days if d not in raw["momentum_20"]]
    if missing:
        raise SystemExit(f"缓存缺 {len(missing)} 个决策日（首个 {missing[0]}）；"
                         f"加 `--rebuild` 重算，不要跳过")
    n = len(days)
    print(f"样本 {n} 个决策日（{days[0]} … {days[-1]}），全截面 ≈{raw['momentum_20'][days[-1]]['n']} 只/日")
    print("映射：α_t = IC_t × σ_fwd,t × z̄_top20,t（bp/笔，H=20）")
    print("se：40 天循环块重抽，1000 次，种子 0；HAC(lag=40) 只作对照")
    print("⚠️ 325 天已看过，以下只用于设计下一段样本，不作确认\n")

    rows = []
    for name in SIGNALS:
        rec = raw[name]
        ic = [rec[d]["ic"] for d in days]
        icq = [rec[d]["ic_q"] for d in days]
        alpha = [rec[d]["ic"] * rec[d]["sigma_bp"] * rec[d]["zbar"] for d in days]
        top = [rec[d]["top_bp"] for d in days]
        spread = [rec[d]["spread_bp"] for d in days]
        wide = [rec[d]["wide_bp"] for d in days]
        sig = statistics.mean(rec[d]["sigma_bp"] for d in days)
        zbar = statistics.mean(rec[d]["zbar"] for d in days)
        rows.append({
            "name": name,
            "ic": ic, "icq": icq, "alpha": alpha, "top": top, "spread": spread,
            "wide": wide,
            "ic_mu": statistics.mean(ic), "ic_se_h": se_hac(ic),
            "ic_se_b": se_block(ic), "icq_mu": statistics.mean(icq),
            "icq_se": se_block(icq), "sigma": sig, "zbar": zbar,
            "a_mu": statistics.mean(alpha), "a_se": se_block(alpha),
            "t_mu": statistics.mean(top), "t_se": se_block(top),
            "s_mu": statistics.mean(spread), "s_se": se_block(spread),
            "w_mu": statistics.mean(wide), "w_se": se_block(wide),
            #: 同日配对：极端 0.4%（20 只）相对顶层 10% 的溢价。为负说明
            #: 极端尾部比同方向的大组合更差——宽度不能替代极端尾部。
            "d_mu": statistics.mean(a - b for a, b in zip(top, wide)),
            "d_se": se_block([a - b for a, b in zip(top, wide)]),
            "pick_sd": statistics.mean(rec[d]["pick_sd_bp"] for d in days),
            "n_pool": statistics.mean(rec[d]["n"] for d in days),
            "dec": [statistics.mean(rec[d]["dec_bp"][i] for d in days)
                    for i in range(10)],
        })

    print("一、全截面 IC（H=20，负号=信号反向）")
    print(f"{'策略':<15}{'IC':>8}{'se_HAC':>8}{'se_块':>7}{'t_块':>7}"
          f"{'MDE_IC':>8}{'层内IC':>8}{'层内t':>7}{'σ20(bp)':>9}{'z̄':>6}{'N':>7}")
    for r in rows:
        t_ic = r["ic_mu"] / r["ic_se_b"]
        t_q = r["icq_mu"] / r["icq_se"]
        print(f"{r['name']:<15}{r['ic_mu']:>8.4f}{r['ic_se_h']:>8.4f}"
              f"{r['ic_se_b']:>7.4f}{t_ic:>7.2f}{MDE_Z * r['ic_se_b']:>8.4f}"
              f"{r['icq_mu']:>8.4f}{t_q:>7.2f}{r['sigma']:>9.0f}{r['zbar']:>6.2f}"
              f"{r['n_pool']:>7.0f}")

    print("\n二、四个估计量：都是每笔 20 日 bp，只差「用多少只」和「用什么函数」")
    print(f"{'策略':<15}{'估计量':<20}{'均值':>8}{'se_块':>7}{'t':>6}"
          f"{'MDE':>7}{'MDE/19bp':>9}")
    for r in rows:
        for label, mu, se in (
                ("实际 20 只（产品）", r["t_mu"], r["t_se"]),
                ("top10% vs 中位数", r["w_mu"], r["w_se"]),
                ("十分位多空", r["s_mu"], r["s_se"]),
                ("IC×σ×z̄（秩映射）", r["a_mu"], r["a_se"])):
            mde = MDE_Z * se
            print(f"{r['name'] if label.startswith('实际') else '':<15}{label:<20}"
                  f"{mu:>8.1f}{se:>7.1f}{mu / se:>6.2f}{mde:>7.1f}{mde / 19.0:>9.1f}")
        print(f"{'':<15}{'尾差（20只−10%）':<20}{r['d_mu']:>8.1f}{r['d_se']:>7.1f}"
              f"{r['d_mu'] / r['d_se']:>6.2f}"
              f"{MDE_Z * r['d_se']:>7.1f}{MDE_Z * r['d_se'] / 19.0:>9.1f}")
        print()

    print("三、换算率：IC 的 1 个单位值多少 bp")
    print(f"{'策略':<15}{'实际 bp/IC':>12}{'σ·z̄ 理论':>11}{'比值':>7}"
          f"{'corr(bp,IC)':>13}{'R²':>6}")
    for r in rows:
        sxx = sum(v * v for v in r["ic"])
        slope = sum(a * b for a, b in zip(r["ic"], r["top"])) / sxx
        theo = r["sigma"] * r["zbar"]
        rho = statistics.correlation(r["ic"], r["top"])
        print(f"{r['name']:<15}{slope:>12.0f}{theo:>11.0f}{slope / theo:>7.2f}"
              f"{rho:>13.2f}{rho * rho:>6.2f}")
    print("比值为 1、R² 高，IC 的 MDE 才能折算成 bp 的 MDE。四个策略各估一次，")
    print("若比值差几倍，说明 IC 与「20 只等权均值」不是同一个泛函，折算不成立。")

    print("\n四、噪声从哪来：日内离散度 vs 日间波动")
    print(f"{'策略':<15}{'日内sd/只':>10}{'sd/√20':>8}{'se_块(20只)':>12}"
          f"{'se_块(10%)':>11}{'n_eff':>7}{'corr(IC映射,实际)':>18}")
    for r in rows:
        naive = r["pick_sd"] / math.sqrt(TOP_N)
        rho = statistics.correlation(r["alpha"], r["top"])
        #: n_eff = n·(se_iid/se_块)² = (sd/se_块)²，恒 ≤ n。写成 n·(sd/se)²
        #: 会把 n 乘两次，得到大于 n 的「独立日数」——这个错不报错。
        n_eff = (statistics.stdev(r["alpha"]) / r["a_se"]) ** 2
        print(f"{r['name']:<15}{r['pick_sd']:>10.0f}{naive:>8.0f}{r['t_se']:>12.0f}"
              f"{r['w_se']:>11.0f}{n_eff:>7.0f}{rho:>18.2f}")
    print(f"\n读法：se_块/(sd/√20) 远小于 1 说明 20 只高度同向，有效宽度远小于 20；")
    print(f"n_eff 是等价独立日数（上限 n={n}），{n} / n_eff 就是 20 日重叠折掉多少。")
    print("宽度 ×28 后 se 若几乎不降，说明瓶颈不在横截面，在时间序列的因子路径。")

    print("\n五、块长敏感性：IC 的 MDE（IC 单位，不折 bp）")
    print(f"{'策略':<15}" + "".join(f"{'B=' + str(b):>10}" for b in (20, 40, 60, 80)))
    for r in rows:
        cells = "".join(f"{MDE_Z * se_block(r['ic'], block=b):>10.3f}"
                        for b in (20, 40, 60, 80))
        print(f"{r['name']:<15}{cells}")
    print("预注册 §2 要求报四档；判定用 B=40。四档同号才是稳态。")

    print("\n六、十分位形状：每层 20 日收益（bp，已去当日截面均值），1=最弱 10=最强")
    print(f"{'策略':<15}" + "".join(f"{str(i):>7}" for i in range(1, 11)))
    for r in rows:
        cells = "".join(f"{v:>7.0f}" for v in r["dec"])
        print(f"{r['name']:<15}{cells}")
    print("读法：1→10 单调上升 = 线性于秩，IC 映射可信；只有 10 翘起 = 尾部效应，")
    print("IC 会低估 top-20，且 top-20 的读数不可由 IC 外推。")

    print("\n七、天数预算（bp 口径）：要达到某个 MDE 还差多少天（se ∝ n^-1/2）")
    print(f"{'策略':<15}{'估计量':<20}{'均值':>8}{'MDE@325':>9}{'MDE@575':>9}"
          f"{'确认19bp':>9}{'确认观测值':>10}")
    for r in rows:
        for label, mu, se in (
                ("实际 20 只（产品）", r["t_mu"], r["t_se"]),
                ("top10% vs 中位数", r["w_mu"], r["w_se"]),
                ("十分位多空", r["s_mu"], r["s_se"])):
            mde = MDE_Z * se
            need_cost = n * (mde / 19.0) ** 2
            need_obs = n * (mde / abs(mu)) ** 2 if mu else float("inf")
            print(f"{r['name'] if label.startswith('实际') else '':<15}{label:<20}"
                  f"{mu:>8.1f}{mde:>9.1f}{mde * math.sqrt(n / (n + 250)):>9.1f}"
                  f"{need_cost:>9,.0f}{need_obs:>10,.0f}")
        print()
    print("  IC 口径的 MDE 是 "
          + " / ".join(f"{MDE_Z * r['ic_se_b']:.3f}" for r in rows)
          + "（IC 单位），不折算成 bp：第三张表说明换算率不是常数。")
    print("  天数只能当量级：它假设块结构与效应量都不变，而 325 天里两者都在动。")


if __name__ == "__main__":
    main()
