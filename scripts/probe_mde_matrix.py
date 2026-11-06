"""判定吞吐量矩阵：持有期 × 策略 × 基准，同一批探索日上逐个重结算。

## 这个脚本回答什么

「250 天确认窗能判出几个策略」这个问题，现有答案来自三张互不一致的表
（§6.1 开方外推、§6.2 块长扫描、§6.3 滚窗实测）。它们都固定在 `H=20` 上。
本脚本把 `H` 当成**策略参数**扫一遍，量一件事：

    在同一个 250 天日历里，哪些 (策略, 持有期, 基准) 组合的 MDE 能进门？

## 为什么 `H` 不是功效旋钮

把 `H` 从 20 改成 5 不是「把同一件事量得更准」，是换一个预测任务：信号
（20 日动量 / 换手率）不变，持有期变了，出场日变了，每笔的效应量与方差
一起变。所以每个 `H` 都必须用真实行情重新结算一遍，不能把同一份账本除以
不同的 `H`——那正是 `se ∝ n^{-1/2}` 外推在 §6.3/§6.6 被证伪的地方。

## 为什么并行持有期不是「n 乘 4」

同一天可以同时持有 5/10/20/40/60 日五条序列，但它们共享同一段日历：
H 日重叠收益的 ACF 在 lag≈H 处截断，五条序列两两高度相关。日历只走一遍，
信息量只能从「每天多少只」和「每天多少独立事件」里来，不能从「多开几条
序列」里来。本脚本把相邻 H 的逐日序列相关矩阵打出来，让这件事可检验，
而不是只写在文字里。

## 口径

- 信号：`mom = close_t / close_{t-20} - 1`（momentum / reversal 取两端），
  `turn = turnover_rate_t`（low / high 取两端）；TOP20 等权。
- 入场：`t+1` 收盘；出场：`t+1+H` 收盘（与 `probe_window.py` 同一条链）。
- 基准两种：`all` = 当日合格池等权均值（随机组合的期望值，零假设中心）；
  `cell` = 同花顺一级行业 × 流通市值三分位格内均值，格内 < 8 只回退行业均值
  （与 `probe_neutral.py` 逐字同口径）。
- 判定设备：`probe_net_diff.evaluate_series`，B=40、REPS=1000、SEED=0、
  MDE = 2.486·se_块；门 200bp（探索）/ 176bp（`CONFIRM_GATE`，确认段）。
- 250 天窗的 MDE **实测**（窗内 8–10 个起点取中位），不用开方外推；
  同时给 cohort 口径（每 H 天一个不重叠窗，k = 250//H）作为不依赖块长的复核。

## 边界

- 只读探索段：决策日 ≤ 2026-10-08，价格日历不截（t+1+H 的收益要用）。
  `require_exploration` 在这里再断言一次，不依赖上游截断。
- 不写任何已封签产物；新产物 `.probe_mde_matrix.json` 不在 `ARTIFACTS` 里，
  因此不参与 audit，也不受封签保护——它只是探索读数。
- 行业与股本是当前快照，跨 2025-06 起的回测有前视（与 `probe_neutral.py`
  同一条已知缺陷），所以本表只用于**比较配置之间的相对分辨率**，不用于
  给任何策略定价。

用法：

    backend/.venv/Scripts/python.exe scripts/probe_mde_matrix.py
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
sys.path.insert(0, str(ROOT / "backend"))

DATA = ROOT / "data"
TABLE = "kline_daily_enriched"
HY = DATA / "ext_data" / "ext_hy_ths" / "part.parquet"
INST = DATA / "instruments" / "instruments.parquet"
OUT = DATA / ".probe_mde_matrix.json"

LOOKBACK, TOP_N = 20, 20
MIN_CELL = 8
#: 扫的持有期。5/10/20 是「能不能靠缩短持有期买分辨率」的一端，
#: 40/60 是「拉长持有期能不能把成本摊薄」的一端。
HORIZONS = (5, 10, 20, 40, 60)
#: §4/§5 的确认窗长度（决策日）。
WINDOW = 250
#: §10 那条「≤80 个决策日出 1 条结论」的升级提议要的是这个长度。它必须**实测**：
#: 250→80 的开方外推在 §6.3/§6.6 上已经对不上（外推 148.8 / 实测 122.7），
#: 用同一套外推回答「80 天够不够」就是把被证伪的方法换个位置再用一次。
SMALL_WINDOW = 80
#: 滚窗起点步长，与 §6.3/§6.4 一致：250 天窗只容得下 70 天的端点变化。
STEP = 10
#: 宽度扫描。`TOP_N` 是 `(H, TOP_N, 基准)` 三元组里最容易被当成「调参」的那个，
#: 因为它对 MDE 的作用方向与「提高功效」**完全一致**，机制却相反：放宽宽度把
#: 组合推向基准，超额与方差同时降，MDE 跟着降，而信息量一点没增。只扫 H 不扫
#: 宽度，等于检查了三个自由度里最不危险的那一个。
#:
#: 只在这两个 H 上扫：5 是「缩短持有期」的一端，20 是现行预注册口径；其余 H
#: 的宽度行为由同一机制支配，扫全表只是把同一个数印五遍。
WIDTH_HORIZONS = (5, 20)
TOP_NS = (20, 50, 100, 200)

SIGNALS = {
    "momentum_20": ("mom", True),
    "reversal_20": ("mom", False),
    "low_turnover": ("turn", False),
    "high_turnover": ("turn", True),
}


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 判定设备与常数只有一处定义。本脚本不复制 `evaluate_series`——
#: 复制一份就等于给自己开一条可以「不小心更乐观」的通路。
NET = _load("probe_net_diff", "scripts/probe_net_diff.py")
GATE = _load("decision_gate", "scripts/decision_gate.py")
EVAL = NET.evaluate_series
COST_BP = NET.COST_BP
MDE_GATE = NET.MDE_GATE
CONFIRM_GATE = NET.CONFIRM_GATE


def sessions() -> list[str]:
    base = DATA / TABLE
    return sorted(c.name.split("=", 1)[1] for c in base.iterdir()
                  if c.is_dir() and c.name.startswith("date="))


def day_frame(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "raw_close", "turnover_rate", "amount"]).collect()


def industry() -> pl.DataFrame:
    df = pl.read_parquet(HY).select(["symbol", "所属同花顺行业"])
    return (df.with_columns(
                pl.col("所属同花顺行业").str.split("-").list.get(0).alias("ind"))
            .select(["symbol", "ind"])
            .filter(pl.col("ind").is_not_null() & (pl.col("ind") != ""))
            .unique("symbol"))


def shares() -> pl.DataFrame:
    df = pl.read_parquet(INST).select(["symbol", "float_shares"])
    return (df.filter(pl.col("float_shares") > 0)
            .with_columns(pl.col("float_shares").cast(pl.Float64)))


def _excess(base: pl.DataFrame) -> pl.DataFrame:
    """加 `exc_all` / `exc_cell` 两列，口径与 `probe_neutral.neutralize` 一致。

    每个聚合各占一次 `with_columns`：裸聚合与窗口聚合混写虽然在本机 Polars
    上能算出正确标量，但它把「全局中心」和「格内中心」放在同一个求值上下文
    里，读代码的人无法从形状上判断哪个是标量、哪个是广播向量。分开写是为了
    让 `bench_all` 一眼就是标量。
    """
    base = base.with_columns(
        (pl.col("raw_close") * pl.col("float_shares")).alias("float_mv"))
    base = base.with_columns(
        (pl.col("float_mv").log().rank("average") / pl.len() * 3)
        .floor().clip(0, 2).cast(pl.Int8).alias("size_q"))
    base = base.with_columns(
        (pl.col("ind") + "|" + pl.col("size_q").cast(pl.Utf8)).alias("cell"))
    base = base.with_columns(pl.col("fwd").mean().over("cell").alias("bench_cell"))
    base = base.with_columns(pl.len().over("cell").alias("n_cell"))
    base = base.with_columns(pl.col("fwd").mean().over("ind").alias("bench_ind"))
    #: 全样本均值走 Python 标量：它是零假设的中心，不能被任何窗口上下文碰到。
    base = base.with_columns(pl.lit(float(base["fwd"].mean())).alias("bench_all"))
    used = (pl.when(pl.col("n_cell") >= MIN_CELL)
            .then(pl.col("bench_cell")).otherwise(pl.col("bench_ind")))
    return base.with_columns(
        (pl.col("fwd") - pl.col("bench_all")).alias("exc_all"),
        (pl.col("fwd") - used).alias("exc_cell"),
        (pl.col("n_cell") < MIN_CELL).alias("cell_fallback"),
    )


def build(cal: list[str], frames: dict[str, pl.DataFrame],
          ind: pl.DataFrame, sh: pl.DataFrame, horizon: int,
          top_ns: tuple[int, ...] = (TOP_N,)) -> dict:
    """按 `horizon` 重结算一遍，返回 `{(策略, 基准): [(决策日, bp)]}`。

    决策日上界由 `t > EXPLORATION_END` 截断，**不用 `cap_calendar`**：本函数
    对每个 `H` 都要 `t+H` 的价格，`cap_calendar` 只按固定 `HORIZON=20` 补
    价格日，扫到 H=60 时会把尾部 40 天整片算成「不可算」。

    出场日 = `cal[i+H]`（不是 `i+1+H`）：`probe_window` / `probe_neutral` 的
    `H` 是**决策日到出场日的间隔**，入场在 `t+1`，所以实际持有 `H-1` 个交易日。
    这里沿用同一口径，否则对表会差一天，逐日读数差到几百 bp。

    `top_ns` 一次给多个宽度时，**只做一遍日历遍历、一遍中立化**，各宽度只共享
    切片的 `base`。单独为每个宽度再跑一遍不是「更干净」，而是把 join 与格内
    聚合重算四次，让四列读数出自四个不同的中间态——那正是宽度对比最不该有的
    噪声源。`series` 恒等于 `top_ns[0]` 那一份，主矩阵的调用点因此不受影响。
    """
    series = {tn: {(n, b): [] for n in SIGNALS for b in ("all", "cell")}
              for tn in top_ns}
    falls: list[float] = []
    pool: list[int] = []
    for i in range(LOOKBACK, len(cal) - horizon):
        t = cal[i]
        if t > GATE.EXPLORATION_END:
            break
        t0, a, b = cal[i - LOOKBACK], cal[i + 1], cal[i + horizon]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("raw_close"),
                                    pl.col("turnover_rate").alias("turn"),
                                    pl.col("amount")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .join(ind, on="symbol", how="left")
            .join(sh, on="symbol", how="left")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0)
                    & pl.col("amount").is_not_null()
                    & pl.col("turn").is_not_null()
                    & pl.col("ind").is_not_null()
                    & pl.col("float_shares").is_not_null())
        )
        if base.height < 200:
            continue
        base = base.with_columns(
            (pl.col("c1") / pl.col("c0") - 1.0).alias("mom"),
            (pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"),
        )
        base = _excess(base)
        pool.append(base.height)
        falls.append(float(base["cell_fallback"].mean()))
        for name, (col, desc) in SIGNALS.items():
            #: 排序只做一次，各宽度只切前缀：不同宽度看到的必须是同一个排名，
            #: 否则「只改宽度」这句就是假的。
            ranked = base.drop_nulls(col).sort(col, descending=desc)
            for tn in top_ns:
                sel = ranked.head(tn)
                if sel.height < tn:
                    continue
                series[tn][(name, "all")].append(
                    (t, float(sel["exc_all"].mean()) * 1e4))
                series[tn][(name, "cell")].append(
                    (t, float(sel["exc_cell"].mean()) * 1e4))
    meta = {"days": len(pool), "pool_mean": statistics.mean(pool) if pool else 0,
            "fallback": statistics.mean(falls) if falls else 0.0}
    return {"series": series[top_ns[0]], "by_top": series,
            "top_ns": tuple(top_ns), "meta": meta}


def cohort_mde(x: list[float], horizon: int) -> tuple[int, float | None]:
    """不重叠 cohort 口径的 MDE：每 `horizon` 天一个观测，k = n//H。

    它不需要块长，也不受 `b/n` 边界影响；代价是 k 小的时候 sd 本身噪声大。
    `k < 3` 时返回 `None`——两个点的 sd 不是估计量。
    """
    k = len(x) // horizon
    if k < 3:
        return k, None
    means = [statistics.mean(x[j * horizon:(j + 1) * horizon]) for j in range(k)]
    sd = statistics.stdev(means)
    return k, 2.486 * sd / math.sqrt(k)


def n_eff(x: list[float], se_block: float) -> int | None:
    """`n·(se_iid/se_块)²`，与 `settle.effective_sample` 同式。"""
    n = len(x)
    if n < 2 or se_block <= 0:
        return None
    se_iid = statistics.stdev(x) / math.sqrt(n)
    return max(1, min(n, int(round(n * (se_iid / se_block) ** 2))))


def spearman(a: list[float], b: list[float]) -> float | None:
    """秩相关（并列取平均秩）。

    用它而不是 Pearson，是因为要问的是**排序是否一致**：MDE 与 |t| 的量纲不同，
    Pearson 会被少数极端行拉走。任一序列全并列时相关无定义，返回 `None`——
    不能退化成 0.0，那会被读成「无关」而不是「问不出来」。
    """
    n = len(a)
    if n < 4 or len(b) != n:
        return None

    def rank(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1.0
            i = j + 1
        return r

    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra)
           * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else None


def window_mde(x: list[float], horizon: int,
               window: int = WINDOW) -> tuple[float | None, float | None, int]:
    """滚窗 MDE：返回（块重抽 MDE 中位、cohort MDE 中位、窗数）。

    `window` 默认 250（确认段长度）。把它开成参数是为了能在**同一个**函数上
    量 80 天：`MDE@80 = MDE@250×sqrt(250/80)` 是 §6.3/§6.6 已经证伪过的
    开方外推，用它去回答「80 天够不够」等于把被推翻的方法换个地方再用一遍。

    窗数少于 3 时中位数没有意义，返回 `None` 让上层打印 `—` 而不是一个
    看起来像读数的数。
    """
    if len(x) < window:
        return None, None, 0
    starts = list(range(0, len(x) - window + 1, STEP))
    if len(starts) < 3:
        return None, None, len(starts)
    blocks, coh = [], []
    for s in starts:
        w = x[s:s + window]
        blocks.append(EVAL(w)["mde"])
        _, c = cohort_mde(w, horizon)
        if c is not None:
            coh.append(c)
    return (statistics.median(blocks),
            statistics.median(coh) if coh else None, len(starts))


def fmt(v, width: int, nd: int = 1) -> str:
    if v is None:
        return f"{'—':>{width}}"
    return f"{v:>{width}.{nd}f}"


def main() -> None:
    cal = sessions()
    capped = GATE.cap_calendar(cal)
    GATE.require_exploration(GATE.decision_days(capped), "probe_mde_matrix",
                             sys.argv)
    frames = {}
    for d in cal:                       # 价格日历不截：出场日要走到 t+1+H
        f = day_frame(d)
        if not f.is_empty():
            frames[d] = f
    ind, sh = industry(), shares()
    print(f"价格日历 {len(cal)} 天（{cal[0]} … {cal[-1]}），"
          f"分析窗决策日 ≤ {GATE.EXPLORATION_END}，确认窗 {WINDOW} 天")
    print(f"成本 {COST_BP:.0f}bp/笔（常数），门 200bp（探索）/ "
          f"{CONFIRM_GATE:.0f}bp（确认段），B={NET.BLOCK}、reps={NET.REPS}\n")

    results = []
    built = {}
    for horizon in HORIZONS:
        #: 宽度扫描只在两个 H 上开。其余 H 传单宽度，`series` 仍取 `top_ns[0]`，
        #: 所以第一节到第六节的读数与「不扫宽度」逐位相同——这一点由测试钉住。
        b = build(cal, frames, ind, sh, horizon,
                  TOP_NS if horizon in WIDTH_HORIZONS else (TOP_N,))
        built[horizon] = b
        print(f"H={horizon:>2}：{b['meta']['days']} 个决策日"
              f"（全截面≈{b['meta']['pool_mean']:.0f} 只/日，"
              f"格内回退 {b['meta']['fallback']:.1%}）")
    print()

    for horizon in HORIZONS:
        b = built[horizon]
        for (name, bench), rows in b["series"].items():
            if len(rows) < WINDOW:
                continue
            days = [d for d, _ in rows]
            x = [v for _, v in rows]
            r = EVAL(x)
            w_mde, c_mde, nw = window_mde(x, horizon)
            w80, c80, nw80 = window_mde(x, horizon, window=SMALL_WINDOW)
            k, _ = cohort_mde(x, horizon)
            #: 最后 250 天窗的完整判定：MDE 只说「能不能判」，这一列说「判成
            #: 什么」。两者缺一，读者会把「可判定」当成「会通过」。
            tail = EVAL(x[-WINDOW:])
            results.append({
                "horizon": horizon, "strategy": name, "bench": bench,
                "days": len(x), "first": days[0], "last": days[-1],
                "mu": r["mu"], "se_block": r["sd_b"], "mde_full": r["mde"],
                "n_eff": n_eff(x, r["sd_b"]), "cohort_k": k,
                "mde_win": w_mde, "mde_win_cohort": c_mde, "n_windows": nw,
                "mu_250": tail["mu"], "net_lo_250": tail["net_lo"],
                "net_hi_250": tail["net_hi"], "p_250": tail["p"],
                "verdict_250": tail["verdict"],
                "mde_80": w80, "mde_80_cohort": c80, "n_windows_80": nw80,
            })

    # ------------------------------------------------------------------
    print("一、每笔分辨率（bp）。`MDE@250` 是 250 天滚窗实测中位，不是开方外推")
    print("   `k` = 250//H，250 天里真正不重叠的 cohort 数；`cohort` 列是不依赖"
          "块长的复核，两列分歧 >30% 的行不可引用。")
    print(f"{'H':>3} {'策略':<14}{'基准':<6}{'天数':>5}{'每笔bp':>8}"
          f"{'n_eff':>6}{'k':>4}{'MDE@250':>9}{'cohort':>8}{'比':>6}"
          f"   {'最后 250 天窗':<13}{'净区间':>18}")
    verds = {200.0: [], 176.0: []}
    shaky = []
    for r in results:
        m = r["mde_win"]
        p200 = "✓" if (m is not None and m <= 200.0) else "✗"
        p176 = "✓" if (m is not None and m <= CONFIRM_GATE) else "✗"
        ratio = (r["mde_win_cohort"] / m
                 if (m and r["mde_win_cohort"]) else None)
        unstable = ratio is not None and not 0.7 <= ratio <= 1.3
        if unstable:
            shaky.append((r["horizon"], r["strategy"], r["bench"], ratio))
        if m is not None and m <= 200.0:
            verds[200.0].append((r["horizon"], r["strategy"], r["bench"], m))
        if m is not None and m <= CONFIRM_GATE:
            verds[176.0].append((r["horizon"], r["strategy"], r["bench"], m))
        v = r["verdict_250"]
        #: 只有跨线格才问分辨率（§4.1）；ECON± 的方向已由区间定下，不打门标。
        verdict = v if v.startswith("ECON") else v + (
            "✓" if m is not None and m <= 200 else "✗")
        print(f"{r['horizon']:>3} {r['strategy']:<14}{r['bench']:<6}"
              f"{r['days']:>5}{r['mu']:>+8.1f}"
              f"{fmt(r['n_eff'], 6, 0)}{r['cohort_k']:>4}{fmt(m, 9)}"
              f"{fmt(r['mde_win_cohort'], 8)}{fmt(ratio, 6, 2)}"
              f"   {verdict:<13}"
              f"[{r['net_lo_250']:>7.1f},{r['net_hi_250']:>7.1f}]"
              + ("  ！" if unstable else ""))

    print(f"\n   250 天窗内 ≤200bp：{len(verds[200.0])} 组"
          f"（{len(results)} 组配置）；≤{CONFIRM_GATE:.0f}bp：{len(verds[176.0])} 组")
    for gate, hits in verds.items():
        if hits:
            print(f"   门 {gate:.0f}：" + "、".join(
                f"H={h} {s}|{b}（{m:.0f}）" for h, s, b, m in hits))
    if shaky:
        print("   ！两种估计量分歧 >30%（块重抽在块数不足时偏窄）：" + "、".join(
            f"H={h} {s}|{b}（cohort/块={v:.2f}）" for h, s, b, v in shaky))
        print("     ——这些行的 MDE 只能当量级，不能当读数；判定以 cohort 列为准。")

    # ------------------------------------------------------------------
    print("\n二、年化口径：缩短持有期买到的分辨率，要按笔数付回去")
    print("   每笔 MDE 与固定 200bp 比，对 H≠20 不公平：5 日策略的每笔效应量"
          "本来就只有 20 日的 ~1/4。")
    print("   等效门 = 19 + (200−19)×H/20：成本按笔、超额按持有期比例，年化后"
          "与 20 日门同义。")
    print(f"{'H':>3} {'策略':<14}{'基准':<6}{'MDE@250':>9}{'等效门':>8}"
          f"{'MDE/门':>8}{'年化成本':>9}{'年化MDE':>9}{'cohort/门':>10}")
    for r in results:
        if r["mde_win"] is None:
            continue
        ann_cost = COST_BP * 250.0 / r["horizon"]
        ann_mde = r["mde_win"] * 250.0 / r["horizon"]
        eq_gate = COST_BP + (MDE_GATE - COST_BP) * r["horizon"] / 20.0
        cob = r["mde_win_cohort"]
        print(f"{r['horizon']:>3} {r['strategy']:<14}{r['bench']:<6}"
              f"{r['mde_win']:>9.0f}{eq_gate:>8.0f}"
              f"{r['mde_win'] / eq_gate:>8.2f}{ann_cost:>9.0f}{ann_mde:>9.0f}"
              f"{fmt(cob / eq_gate if cob else None, 10, 2)}")

    #: 这一节最容易读错的一行是「等效门只有谁过」。块口径在 H=5 上给出的是
    #: 45/56（反转过、低换手不过），而这两行正是第五节标记 cohort/块 = 1.52、
    #: 1.54 的行——脚本文档自己写着「这些行判定以 cohort 列为准」。按 cohort
    #: 重算，反转变 1.07/1.34（不过），低换手|格内变 0.93（过）。**结论翻转**，
    #: 所以过门表必须成对打印，只印块口径等于把最不稳的两个数当结论。
    print("\n   同一条等效门，按 cohort 列（不稳行的唯一合法读数）复核：")
    for horizon in HORIZONS:
        eq_gate = COST_BP + (MDE_GATE - COST_BP) * horizon / 20.0
        hit = [(r["strategy"], r["bench"], r["mde_win_cohort"])
               for r in results
               if r["horizon"] == horizon and r["mde_win_cohort"]]
        hit = [(s, b, c) for s, b, c in hit if c <= eq_gate]
        print(f"   H={horizon:>2}（门 {eq_gate:.0f}）：" + (
            "、".join(f"{s}|{b}（{c:.0f}）" for s, b, c in hit) or "0 组"))

    # ------------------------------------------------------------------
    #: 上一轮提出的升级验收：单位结论 ≤80 个决策日、12 个月内 ≥3 条。
    #: 这条能不能成立只能实测——把窗长换成 80 重跑一遍，不用开方外推。
    print(f"\n三、升级目标「{SMALL_WINDOW} 个决策日 · ≥3 条可判定结论」在现有设计下"
          "能否成立")
    print(f"   {SMALL_WINDOW} 天窗 < 3×块长({NET.BLOCK}) → 块重抽只剩 nb="
          f"{math.ceil(SMALL_WINDOW / NET.BLOCK)} 块，**80 天窗上的块口径一律不可用**；"
          "唯一合法读数是 cohort（§6.4），")
    print(f"   而 cohort 只在 {SMALL_WINDOW}//H≥3（H≤20）时存在。"
          "`外推` = MDE@250×sqrt(250/80)，只用来量外推本身错多少。")
    print(f"   门取确认段 {CONFIRM_GATE:.0f}bp。")
    print(f"{'H':>3} {'策略':<14}{'基准':<6}{'cohort@80':>10}{'k':>4}"
          f"{'外推@80':>9}{'外推/cohort':>12}  判定")
    for r in results:
        if r["mde_80"] is None:
            continue
        h = r["horizon"]
        k80 = SMALL_WINDOW // h
        ext = r["mde_win"] * math.sqrt(250 / SMALL_WINDOW)
        cob = r["mde_80_cohort"]
        if cob is None:
            verdict = "—（k<3，问不出来）"
            ratio = "—"
        else:
            verdict = "✓" if cob <= CONFIRM_GATE else "✗"
            ratio = f"{ext / cob:>12.2f}"
        print(f"{h:>3} {r['strategy']:<14}{r['bench']:<6}"
              f"{fmt(cob, 10)}{k80:>4}{ext:>9.0f}{ratio:>12}  {verdict}")
    for horizon in HORIZONS:
        rows = [r for r in results if r["horizon"] == horizon
                and r["mde_80"] is not None]
        if not rows:
            continue
        if SMALL_WINDOW // horizon < 3:
            print(f"   H={horizon:>2}：无法测量（{SMALL_WINDOW} 天里凑不满 3 个"
                  f"不重叠 {horizon} 日块）")
            continue
        ok = [(r["strategy"], r["bench"], r["mde_80_cohort"]) for r in rows
              if r["mde_80_cohort"] and r["mde_80_cohort"] <= CONFIRM_GATE]
        print(f"   H={horizon:>2}：{len(ok)}/{len(rows)} 组在 {SMALL_WINDOW} 天内可判定"
              + ("　" + "、".join(f"{s}|{b}（{m:.0f}）" for s, b, m in ok)
                 if ok else ""))

    # ------------------------------------------------------------------
    print("\n四、并行持有期不是独立样本：同策略、同基准、跨 H 的逐日相关")
    print("   同一天可以同时持有五个 H，但日历只走一遍。相关系数接近 1 意味着"
          "「n 乘 5」不存在。")
    for strat in ("momentum_20", "low_turnover"):
        for bench in ("all", "cell"):
            cols = {}
            for h in HORIZONS:
                rows = built[h]["series"][(strat, bench)]
                cols[h] = dict(rows)
            common = sorted(set.intersection(*(set(c) for c in cols.values())))
            if len(common) < 60:
                continue
            print(f"   {strat}|{bench}（{len(common)} 个共同决策日）")
            print("        " + "".join(f"{h:>8}" for h in HORIZONS))
            for h1 in HORIZONS:
                line = f"   H={h1:>2} "
                for h2 in HORIZONS:
                    a = [cols[h1][d] for d in common]
                    b = [cols[h2][d] for d in common]
                    c = (statistics.correlation(a, b)
                         if len(set(a)) > 1 and len(set(b)) > 1 else float("nan"))
                    line += f"{c:>8.2f}"
                print(line)

    # ------------------------------------------------------------------
    #: 与封签 SERIES 对表：H=20 的四列必须逐日复现，否则本脚本的结算链
    #: 与 `probe_neutral` 不是同一条，上面所有新读数都不能引用。
    #:
    #: **判据是逐日差，不是 MDE 差。** `EVAL` 的 MDE 是重抽均值的 2.486 倍 sd，
    #: 对常数平移完全不变：把某一列的基准中心挪 100bp，每一天都加同一个数，
    #: 重抽分布整体平移、sd 一模一样、MDE 差恰好 0.0。而「基准中心错了」正是
    #: 换了一条结算链最可能的形态。所以拿 MDE 相等去证明同链，是一个结构上
    #: 看不见主要故障模式的检验——Δ=0.0 既不能证真也不能证伪。
    series_path = GATE.NEUTRAL_CACHE
    if series_path.exists():
        print("\n五、对表：本脚本 H=20 的逐日序列 vs 封签 SERIES")
        sealed = json.loads(series_path.read_text(encoding="utf-8"))
        print(f"{'列':<22}{'天数':>5}{'缺失':>5}{'均值Δ':>8}{'max|日差|':>10}")
        worst = 0.0
        for name in SIGNALS:
            for bench in ("all", "cell"):
                key = f"{name}|{bench}"
                if key not in sealed:
                    continue
                mine = dict(built[20]["series"][(name, bench)])
                common = [d for d in sorted(sealed[key]) if d in mine]
                if len(common) < WINDOW:
                    continue
                a = [mine[d] for d in common]
                b = [float(sealed[key][d]) for d in common]
                dmax = max(abs(x - y) for x, y in zip(a, b))
                worst = max(worst, dmax)
                print(f"{key:<22}{len(common):>5}"
                      f"{len(sealed[key]) - len(common):>5}"
                      f"{sum(a) / len(a) - sum(b) / len(b):>+8.2f}{dmax:>10.2f}")
        print(f"   逐日最大偏差 {worst:.2f}bp —— "
              + ("同一条结算链（逐日逐位复现，不只是分布同形）" if worst <= 0.05 else
                 "⚠ 不是同一条链，上面三节的新读数不可引用"))

    # ------------------------------------------------------------------
    #: 第五节回答「链对不对」，这一节回答「同一个策略换个 H 还算不算同一个问题」。
    #: 两段式漏斗（H=5 识别 → H=20 确认）默认两段测的是同一效应的不同精度。
    #: 若逐 H 的点估计换号，漏斗第二段就不是「确认一个更粗的读数」，而是
    #: 「确认另一个东西」——那时「晋级」这个动作没有定义。
    print("\n六、跨 H 的点估计：同一信号在不同持有期是不是同一个效应（bp）")
    print(f"{'策略':<15}{'基准':<6}" + "".join(f"{'H=' + str(h):>9}"
                                              for h in HORIZONS))
    flips = []
    for strat in SIGNALS:
        for bench in ("all", "cell"):
            mus = []
            for h in HORIZONS:
                row = [r for r in results
                       if r["horizon"] == h and r["strategy"] == strat
                       and r["bench"] == bench]
                mus.append(row[0]["mu"] if row else None)
            print(f"{strat:<15}{bench:<6}" + "".join(fmt(m, 9) for m in mus))
            v = [m for m in mus if m is not None]
            if v and max(v) > 0 > min(v) and max(v) - min(v) > 100:
                flips.append((strat, bench, max(v), min(v)))
    if flips:
        print("   换号且幅度 >100bp（这些行不能「先在一个 H 上识别、再到另一个"
              " H 上确认」）：")
        for s, b, hi, lo in flips:
            print(f"     {s}|{b}：最大 {hi:+.0f}bp vs 最小 {lo:+.0f}bp")

    #: t 单位是唯一真正 horizon-invariant 的读法：年化净超额 / 年化 se。
    #: 若换 H 只是换精度，t 应当同号、同量级；t 随 H 单调漂移说明换 H 换了问题。
    print("\n   同一批数据的 t = 2.486·mu/MDE（全样本）。H 若只是精度的旋钮，"
          "这一行应当基本不动：")
    print(f"{'策略':<15}{'基准':<6}" + "".join(f"{'H=' + str(h):>9}"
                                              for h in HORIZONS))
    for strat in SIGNALS:
        for bench in ("all", "cell"):
            ts = []
            for h in HORIZONS:
                row = [r for r in results
                       if r["horizon"] == h and r["strategy"] == strat
                       and r["bench"] == bench]
                ts.append(2.486 * row[0]["mu"] / row[0]["mde_full"]
                          if row and row[0]["mde_full"] else None)
            print(f"{strat:<15}{bench:<6}" + "".join(fmt(t, 9, 2) for t in ts))

    # ------------------------------------------------------------------
    #: 第六节回答「换个 H 还是不是同一个效应」，这一节回答「换个宽度呢」。
    #: 两者是同级自由度，而危险方向相反：H 拉长会同时放大 μ 与方差，MDE 变差，
    #: 一眼看得出不划算；宽度放宽只**稀释**，MDE 变好，看起来像提高了功效。
    #: 门是绝对 bp，所以稀释能直接把 UNDERPOWERED 刷成 FAIL：判据没变，问题没答。
    #: 区分这两种「MDE 下降」只能用刻度无关量，即 t。
    print("\n七、宽度：TOP_N 是三元组的第三个自由度，而它对 MDE 的作用是稀释")
    print("   同一批 base、同一份排名、同一批决策日，只切不同前缀。")
    print("   「MDE 变小」有两种成因：分辨率真的提高，或效应被稀释。")
    print("   `t = 2.486·μ/MDE` 与刻度无关——稀释不改变它，分辨率提高才改变它。")
    width_cells: dict[tuple[int, str, str], dict] = {}
    width_out: list[dict] = []
    for horizon in WIDTH_HORIZONS:
        eq_gate = COST_BP + (MDE_GATE - COST_BP) * horizon / 20.0
        by_top = built[horizon]["by_top"]
        for strat in SIGNALS:
            for bench in ("all", "cell"):
                for tn in TOP_NS:
                    rows = by_top[tn][(strat, bench)]
                    if len(rows) < WINDOW:
                        continue
                    x = [v for _, v in rows]
                    r = EVAL(x)
                    m, cob, _ = window_mde(x, horizon)
                    cell = {"mu": r["mu"], "mde": m, "mde_full": r["mde"],
                            "cohort": cob,
                            "t": (2.486 * r["mu"] / r["mde"]) if r["mde"] else None}
                    width_cells.setdefault((horizon, strat, bench), {})[tn] = cell
                    width_out.append({
                        "horizon": horizon, "strategy": strat, "bench": bench,
                        "top_n": tn, **cell,
                    })
        print(f"\n   H={horizon}　等效门 {eq_gate:.0f}bp"
              f"（MDE 取 250 窗中位，t 取全样本）")
        print("   " + f"{'策略|基准':<20}"
              + "".join(f"{'N=' + str(tn):>9}" for tn in TOP_NS))
        print("   " + "MDE@250（bp）——越小越「像能判」，但见下面 t 行")
        for strat in SIGNALS:
            for bench in ("all", "cell"):
                cells = width_cells[(horizon, strat, bench)]
                print("   " + f"{strat + '|' + bench:<20}" + "".join(
                    fmt(cells[tn]["mde"], 9) if tn in cells else f"{'—':>9}"
                    for tn in TOP_NS))
        print("   " + "t = 2.486·μ/MDE——刻度无关；稀释不改变它")
        for strat in SIGNALS:
            for bench in ("all", "cell"):
                cells = width_cells[(horizon, strat, bench)]
                print("   " + f"{strat + '|' + bench:<20}" + "".join(
                    fmt(cells[tn]["t"], 9, 2) if tn in cells else f"{'—':>9}"
                    for tn in TOP_NS))
        counts = {tn: 0 for tn in TOP_NS}
        gained = []
        for strat in SIGNALS:
            for bench in ("all", "cell"):
                cells = width_cells[(horizon, strat, bench)]
                for tn in TOP_NS:
                    cell = cells.get(tn)
                    if cell and cell["mde"] is not None and cell["mde"] <= eq_gate:
                        counts[tn] += 1
                a, z = cells.get(TOP_NS[0]), cells.get(TOP_NS[-1])
                if (a and z and a["mde"] and z["mde"]
                        and a["mde"] > eq_gate >= z["mde"]):
                    gained.append((strat, bench, a, z))
        print("   过等效门组数：" + "、".join(f"N={tn} → {counts[tn]}/8"
                                            for tn in TOP_NS))
        #: 「过门组数变多」不是自证的好事，它也可能是稀释换来的。判据是 t：
        #: 只看块口径会得到「宽度提高了可判性」，只看 μ 会得到「宽度毁掉了信号」，
        #: 两者都对，说的却是同一件事的两面——必须成对看。
        if gained:
            print("   新增过门的配置（N=20 不过 → 最宽档过）逐条看 t：")
            for strat, bench, a, z in gained:
                print(f"     {strat + '|' + bench:<20}"
                      f"　μ {a['mu']:+8.1f} → {z['mu']:+8.1f}"
                      f"　MDE {a['mde']:>6.0f} → {z['mde']:>6.0f}"
                      f"　t {a['t']:+.2f} → {z['t']:+.2f}")
            flat = sum(1 for _, _, a, z in gained if abs(z["t"]) <= abs(a["t"]))
            print(f"   其中 |t| 未上升的 {flat}/{len(gained)}"
                  " —— 这些组买到的是「更安静」，不是「更强的证据」。")

    #: 这一节最该被记住的不是「宽度能刷门」，而是「MDE 的排序与证据强度无关」。
    #: 若 ρ 接近 0，任何「按 MDE 挑配置」的动作——包括把 MDE 当两段式漏斗第一段
    #: 的晋级判据——都不比随机好，而且会系统性偏向「安静的空壳」：MDE 小恰恰
    #: 常常因为 μ 本来就接近 0，不是因为量得准。
    print("\n   ρ(MDE@250, |t|)：MDE 小 ≠ 证据强。接近 0 表示按 MDE 排序≈随机排列。")
    print("   " + f"{'':<10}" + "".join(f"{'N=' + str(tn):>9}" for tn in TOP_NS))
    rhos: dict[str, float | None] = {}
    for horizon in WIDTH_HORIZONS:
        vals = []
        for tn in TOP_NS:
            mdes, ts = [], []
            for strat in SIGNALS:
                for bench in ("all", "cell"):
                    cell = width_cells.get((horizon, strat, bench), {}).get(tn)
                    if cell and cell["mde"] is not None and cell["t"] is not None:
                        mdes.append(cell["mde"])
                        ts.append(abs(cell["t"]))
            rho = spearman(mdes, ts)
            vals.append(rho)
            rhos[f"H{horizon}_N{tn}"] = rho
        print(f"   H={horizon:<8}" + "".join(
            f"{v:>+9.2f}" if v is not None else f"{'—':>9}" for v in vals))
    pair = [(r["mde_win"], abs(2.486 * r["mu"] / r["mde_full"]))
            for r in results if r["mde_win"] and r["mde_full"]]
    all_rho = spearman([p[0] for p in pair], [p[1] for p in pair])
    rhos["all"] = all_rho
    print(f"   全 {len(pair)} 行（H × 策略 × 基准）：ρ = {all_rho:+.2f}"
          if all_rho is not None else f"   全 {len(pair)} 行：ρ 无定义")

    OUT.write_text(json.dumps({
        "exploration_end": GATE.EXPLORATION_END,
        "window": WINDOW, "step": STEP, "block": NET.BLOCK, "reps": NET.REPS,
        "cost_bp": COST_BP, "gate": MDE_GATE, "confirm_gate": CONFIRM_GATE,
        "horizons": list(HORIZONS),
        "top_ns": list(TOP_NS),
        "rows": [{k: (round(v, 4) if isinstance(v, float) else v)
                  for k, v in r.items()} for r in results],
        "width": [{k: (round(v, 4) if isinstance(v, float) else v)
                   for k, v in r.items()} for r in width_out],
        "rho_mde_t": rhos,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物 → {OUT.relative_to(ROOT)}（不在 ARTIFACTS/封签范围内，仅探索读数）")


if __name__ == "__main__":
    main()
