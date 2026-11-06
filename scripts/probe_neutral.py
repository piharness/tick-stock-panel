"""行业 × 规模中性化：+185bp 里有多少是「选了哪个行业」而不是「选了什么股」。

## 为什么这是最后一个没被证伪的缺口

§9.29 把宽度、层内 IC、尾部溢价都量过了，但所有基准都只按**成交额**分层。
成交额是规模/流动性的代理，不是行业。低换手策略选出来的 20 只如果集中在
银行、公用事业、高速公路这些「天生低换手」的行业，那 +185bp 里就混着行业
beta——它对「换手率是不是信号」这件事一点证据都没有。

预注册 §2 把这条写成硬条件：**换成行业×规模双重分层前，任何对外表述都不
成立**。本脚本就是那一层。

## 五个基准，逐步加控制

    all      全样本中位数（§9.28 的主口径）
    amt5     成交额五分位内中位数（§9.28 的强制敏感性）
    ind      同行业（同花顺一级）内中位数
    cell     同行业 × 同规模三分位内中位数
    resid    对 log(流通市值) + 行业哑变量做横截面回归后的残差

前四个是同一个泛函（每笔减一个基准），只差基准多细；第五个换成残差，不再
是「超额收益」，而是「剔除了行业与规模线性暴露后的收益」。两者不可直接比
大小，但**符号必须一致**——不一致就说明效应本身依附于被控制的那一维。

## 三个必须一起读的诊断

1. **格子集中度**：top-20 落在几个格子里、最大格占几只。若 20 只里有 15 只
   在同一格，格内中位数由这 15 只自己决定，超额会被机械压向 0——这时候
   「中性化后归零」不是发现，是构造。
2. **回退率**：格子内样本 < 8 只时退回行业内中位数。回退率高说明 90×3 的
   切分对这份数据太细，读数要打折。
3. **行业集中度**：picks 的行业 HHI 与最大行业占比。这是「行业 beta」的
   直接读数，比任何 p 值都直观。

## 两个前视偏差，必须写在读数旁边

- 行业分类是 **2026-11-05 的快照**（同花顺一级，5,579 只）。回测跨 2025-06
  起，期间发生过的行业调整、重组、新股归类都没有历史版本。
- 流通股本是 **同一快照**。`float_mv = raw_close_t × float_shares` 用了当日
  价格，所以只有股本这一维是前视；增发/回购/解禁会让它偏离历史真值。

两者都只会让「中性化」变弱而不是变强：用错误的行业分类去控制，残差里会
留下本可以控掉的暴露，于是「控制后仍有超额」更容易成立。所以若本脚本给出
「控制后归零」，那个结论是稳的；若给出「控制后仍有」，要记住它可能偏高。

## 基准中心：中位数还是均值

四个「每笔减一个基准」的口径都以**格内中位数**为中心。中位数与均值的差不是
小数点后的事：`fwd` 右偏，均值 − 中位数 ≈ +200bp，而组合的 P&L 是均值。
所以 `exc_cell` 读作「跑赢同格中位数」，不是「跑赢同格平均持仓」。

    --basis=median  格内中位数（历史口径，§9.31 的 +99）
    --basis=mean    格内均值（与组合 P&L 同一个中心）

两者只差一个泛函，其余全部不变；读数差多少就是「基准选了中位数」的价钱。

## 确认段闸门（v12）

v11 之前本脚本是 §5.2 表里「其他探针：不拦」的一行，而它的循环边界是
`len(cal)-HORIZON`，没有日期上限。第 367 个分区（2026-11-06）一落盘，它算的
第一天就是 2026-10-09——按 §5.1 这是一次读取，而且只有一次机会，没有任何
东西会拦它。所以 v12 把 `decision_gate` 的两个入口接进来：

  * `cap_calendar()`：决策日截到 2026-10-08，保留 t+20 所需的价格；
  * `require_exploration()`：截断若被改坏就停；`--include-confirmation` 是
    唯一出口，它会在 `docs/decision-read-log.jsonl` 追加一行不可撤销的记录。

**`--nmax` 不是闸门。** `used` 数的是「算成了的天」，不是日期：任何一天因
`base.height < 200` 或四表不齐被 `continue` 跳过，第 327 个**算成**的天就可
能是 2026-10-09 而 `used` 仍等于 326。日期上界只能由 `cap_calendar` 给，
`--nmax=326` 只用来把 n 钉在文档引用的那个数上。

## 逐日序列落盘（v12）

v11 的 §6.1 有一行 `146`（low×格内，MDE@250），它由 128×√(325/250) 外推而
来，**没有序列可以重算**。本脚本现在把每个「策略×基准」的逐日读数写进
`data/.probe_neutral_per_day.json`，键是 `策略|基准`，值是 `{决策日: bp}`。
展平成两层是**故意的**：`decision_gate.artifact_dates()` 只认第二层的日期键，
扁平结构不用改闸门就能被 `audit` 扫到。

用法：
    python scripts/probe_neutral.py [--nmax=326] [--basis=median|mean]
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TABLE = "kline_daily_enriched"
#: 逐日读数。写盘是为了让 §6.1 的 146 从「外推出来的数」变成「能重算的数」，
#: 同时让它进入闸门的产物清单——不落盘的读数既不可复核，也不可审计。
SERIES = DATA / ".probe_neutral_per_day.json"
HY = DATA / "ext_data" / "ext_hy_ths" / "part.parquet"
INST = DATA / "instruments" / "instruments.parquet"

HORIZON, LOOKBACK, TOP_N = 20, 20, 20
MIN_CELL = 8          #: 格子内少于这个数就回退到行业内中位数
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)
#: 基准中心。`median` = 历史口径；`mean` = 与组合 P&L 同一个中心。
BASIS = next((a.split("=", 1)[1] for a in sys.argv[1:]
              if a.startswith("--basis=")), "median")
if BASIS not in ("median", "mean"):
    raise SystemExit(f"--basis 必须是 median 或 mean，收到 {BASIS!r}")

SIGNALS = {
    "momentum_20": ("mom", True),
    "reversal_20": ("mom", False),
    "low_turnover": ("turn", False),
    "high_turnover": ("turn", True),
}

_spec = importlib.util.spec_from_file_location(
    "probe_paired_diff", ROOT / "scripts" / "probe_paired_diff.py")
_ppd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ppd)
se_block = _ppd.se_block


def _load(name: str, rel: str):
    """与 `decision_gate` / `probe_net_diff` 同一加载方式。

    签名必须与它们逐字相同（模块名 + 相对路径两个参数）：封签的闭合性测试
    用正则在这份源码里找依赖，换个写法就等于把被导入的脚本留在封签门外。
    正文里也不要写那个调用式本身——测试扫的是全文，注释一样会被当成依赖。
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 闸门必须由**被检查的脚本**导入，而不是由调用者记得去调：`probe_neutral`
#: 就是那个「没人记得」的入口。
GATE = _load("decision_gate", "scripts/decision_gate.py")
INCLUDE_CONFIRM = "--include-confirmation" in sys.argv


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
    """symbol → 同花顺一级行业。`所属同花顺行业` 形如「种植业与林业-种子生产」。"""
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


def neutralize(base: pl.DataFrame, basis: str | None = None) -> pl.DataFrame:
    """在当日截面上加五个基准列。

    所有 `over` 都在当日截面内做，不跨日；这是「截面中性化」的定义。跨日
    去均值会把市场 beta 也控掉，那是另一个问题。

    `basis` 决定基准的**中心**：`median`（历史口径）或 `mean`（与组合 P&L
    同中心）。默认取模块级 `BASIS`（命令行 `--basis=`），显式传参是为了让
    两个口径能在同一份数据上被同时测——见 §9.32。
    """
    agg = "mean" if (basis or BASIS) == "mean" else "median"
    get_agg = getattr(pl.col("fwd"), agg)
    base = base.with_columns(
        (pl.col("raw_close") * pl.col("float_shares")).alias("float_mv"))
    base = base.with_columns(
        pl.col("float_mv").log().alias("logmv"),
        (pl.col("amount").rank("average") / pl.len() * 5).floor()
        .clip(0, 4).cast(pl.Int8).alias("amt_q"),
        (pl.col("float_mv").log().rank("average") / pl.len() * 3).floor()
        .clip(0, 2).cast(pl.Int8).alias("size_q"),
    )
    base = base.with_columns(
        (pl.col("ind") + "|" + pl.col("size_q").cast(pl.Utf8)).alias("cell"))

    med_all = float(getattr(base["fwd"], agg)())
    base = base.with_columns(pl.lit(med_all).alias("bench_all"))

    for key, name in (("amt_q", "bench_amt"), ("ind", "bench_ind"),
                      ("cell", "bench_cell")):
        base = base.with_columns(
            get_agg().over(key).alias(name),
            pl.len().over(key).alias(name.replace("bench", "n")),
        )
    #: 格子太小 ⇒ 退回行业内中位数。回退是**逐股**的，所以一只股票可能用
    #: 格内基准、也可能用行业基准，不能事后从平均 n 反推。
    base = base.with_columns(
        pl.when(pl.col("n_cell") >= MIN_CELL)
        .then(pl.col("bench_cell")).otherwise(pl.col("bench_ind"))
        .alias("bench_cell_used"),
        (pl.col("n_cell") < MIN_CELL).alias("cell_fallback"),
    )

    # FWL：y ~ 行业哑变量 + logmv 的残差 = 双去均值后对 logmv 回归的残差。
    base = base.with_columns(
        (pl.col("fwd") - pl.col("fwd").mean().over("ind")).alias("y_dm"),
        (pl.col("logmv") - pl.col("logmv").mean().over("ind")).alias("x_dm"),
    )
    sxx = float((base["x_dm"] ** 2).sum())
    sxy = float((base["x_dm"] * base["y_dm"]).sum())
    beta = sxy / sxx if sxx > 0 else 0.0
    base = base.with_columns(
        (pl.col("y_dm") - beta * pl.col("x_dm")).alias("resid"))
    #: 残差要**再减当日中位数**才能与上面四个基准比大小。OLS 残差天然以
    #: 均值为中心，而 `fwd` 右偏（均值 − 中位数 ≈ +200bp），不减这一下，
    #: `resid` 会系统性比 `exc_cell` 低 150–200bp，看起来像「控制后归零」，
    #: 其实只是换了个中心。符号不受影响，量级受影响。
    #: `--basis=mean` 时两边都用均值中心，这一步变成「减当日均值」——这样
    #: 残差列与格内列仍然同中心，差别只剩「饱和固定效应 vs 线性控制」。
    med_resid = float(getattr(base["resid"], agg)())
    base = base.with_columns((pl.col("resid") - med_resid).alias("resid"))

    return base.with_columns([
        (pl.col("fwd") - pl.col("bench_all")).alias("exc_all"),
        (pl.col("fwd") - pl.col("bench_amt")).alias("exc_amt"),
        (pl.col("fwd") - pl.col("bench_ind")).alias("exc_ind"),
        (pl.col("fwd") - pl.col("bench_cell_used")).alias("exc_cell"),
    ]).with_columns(pl.lit(beta).alias("beta"))


def main() -> None:
    cal = sessions()
    if not INCLUDE_CONFIRM:
        #: 只截**决策日**，价格日历仍到 2026-11-05——2026-10-08 的 20 日收益
        #: 要读到那一天，那是探索日决策的收益，不是确认段样本（§5）。
        cal = GATE.cap_calendar(cal)
    #: 截断是本脚本唯一的日期上界，所以再断言一次：`cap_calendar` 被改坏时
    #: 这里必须抛，而不是静默多算一天。`--nmax` 在这方面不顶用（见文件头）。
    GATE.require_exploration(GATE.decision_days(cal), "probe_neutral", sys.argv)
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    ind = industry()
    sh = shares()

    keys = ["all", "amt", "ind", "cell", "resid"]
    acc = {k: {b: [] for b in keys} for k in SIGNALS}
    #: 每个策略自己的决策日列表。四个策略在同一天通常都有 20 只，但
    #: `sel.height != TOP_N` 是逐策略的 `continue`，用共享的 `days_used` 去
    #: zip 会在缺一天时把整列错位——落盘之后这种错位不会被看出来。
    days_by: dict[str, list[str]] = {k: [] for k in SIGNALS}
    diag = {k: {"inds": [], "hhi": [], "max_ind": [], "cells": [],
                "max_cell": [], "ncell": [], "fallback": [], "mvpct": []}
            for k in SIGNALS}
    ind_cnt: dict[str, dict[str, float]] = {k: {} for k in SIGNALS}
    ind_days: dict[str, int] = {k: 0 for k in SIGNALS}
    used = 0
    betas: list[float] = []

    for i in range(LOOKBACK, len(cal) - HORIZON):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("raw_close"), pl.col("turnover_rate").alias("turn"),
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
        base = neutralize(base)
        betas.append(float(base["beta"][0]))
        used += 1

        for name, (col, desc) in SIGNALS.items():
            sel = base.drop_nulls(col).sort(col, descending=desc).head(TOP_N)
            if sel.height != TOP_N:
                continue
            for bkey in keys:
                src = "resid" if bkey == "resid" else f"exc_{bkey}"
                acc[name][bkey].append(float(sel[src].mean()) * 1e4)
            g = sel.group_by("ind").len().sort("len", descending=True)
            shares_ = g["len"].to_list()
            diag[name]["inds"].append(len(shares_))
            diag[name]["hhi"].append(sum((x / TOP_N) ** 2 for x in shares_))
            diag[name]["max_ind"].append(shares_[0] / TOP_N)
            cg = sel.group_by("cell").len().sort("len", descending=True)
            diag[name]["cells"].append(cg.height)
            diag[name]["max_cell"].append(cg["len"][0] / TOP_N)
            diag[name]["ncell"].append(float(sel["n_cell"].median()))
            diag[name]["fallback"].append(float(sel["cell_fallback"].mean()))
            diag[name]["mvpct"].append(float(
                (sel["size_q"].cast(pl.Float64) + 0.5).mean() / 3))
            ind_days[name] += 1
            for k, v in sel.group_by("ind").len().iter_rows():
                ind_cnt[name][k] = ind_cnt[name].get(k, 0.0) + v / TOP_N
            days_by[name].append(t)
        if NMAX and used >= NMAX:
            break

    #: 落盘。键是 `策略|基准`，值是 `{决策日: bp}`——两层而不是三层，因为
    #: `decision_gate.artifact_dates()` 只向下看一层。换成三层，这个产物就会
    #: 在 `audit` 里显示 0 天，于是「写得进确认段、扫不出来」。
    series = {f"{name}|{bkey}": {d: round(v, 4) for d, v in zip(days_by[name],
                                                              acc[name][bkey])}
              for name in SIGNALS for bkey in keys}
    SERIES.write_text(json.dumps(series, ensure_ascii=False, sort_keys=True),
                      encoding="utf-8")

    print(f"{used} 个决策日；全截面≈{base.height} 只/日；TOP{TOP_N}；H={HORIZON}")
    print(f"逐日序列 → {SERIES.relative_to(ROOT)}"
          f"（{len(series)} 列 × {len(days_by['low_turnover'])} 天，"
          f"末日 {days_by['low_turnover'][-1] if days_by['low_turnover'] else '—'}）")
    print(f"行业=同花顺一级（{ind['ind'].n_unique()} 个），规模=流通市值三分位；"
          f"格内<{MIN_CELL} 只回退行业内{'均值' if BASIS == 'mean' else '中位数'}；"
          f"基准中心={BASIS}")
    print(f"回归β(logmv)= {sum(betas) / len(betas):+.5f}/单位 log 市值（日均）；"
          f"残差 = 行业内去均值后再剔规模")
    print("⚠️ 行业与股本都是 2026-11-05 快照，跨 2025-06 起的回测有前视；"
          "方向见文件头。\n")

    print("一、五个基准下的每笔超额（bp）")
    print(f"{'策略':<15}{'基准':<10}{'均值':>8}{'se':>7}{'t':>7}{'位移':>8}")
    for name, d in acc.items():
        if not d["all"]:
            continue
        base_mean = sum(d["all"]) / len(d["all"])
        for bkey, label in (("all", "全样本"), ("amt", "成交额5"),
                            ("ind", "行业"), ("cell", "行业×规模"),
                            ("resid", "回归残差")):
            v = d[bkey]
            mu = sum(v) / len(v)
            se = se_block(v)
            t = mu / se if se > 0 else float("nan")
            print(f"{name if bkey == 'all' else '':<15}{label:<10}{mu:>8.1f}"
                  f"{se:>7.1f}{t:>7.2f}{mu - base_mean:>+8.1f}")
        ratio = (sum(d["cell"]) / len(d["cell"]) / base_mean
                 if base_mean else float("nan"))
        print(f"{'':<15}{'行业×规模 / 全样本':<10}{ratio:>8.2f}×")
        print()

    print("二、集中度：中性化归零时，先看它是不是被构造压平的")
    print(f"{'策略':<15}{'行业数':>7}{'最大行业':>9}{'HHI':>6}{'格子数':>7}"
          f"{'最大格':>7}{'格内n':>7}{'回退率':>8}{'规模分位':>9}")
    for name, d in diag.items():
        if not d["inds"]:
            continue
        m = lambda k: sum(d[k]) / len(d[k])  # noqa: E731
        print(f"{name:<15}{m('inds'):>7.1f}{m('max_ind'):>9.0%}{m('hhi'):>6.2f}"
              f"{m('cells'):>7.1f}{m('max_cell'):>7.0%}{m('ncell'):>7.0f}"
              f"{m('fallback'):>8.0%}{m('mvpct'):>9.2f}")
    print("\n读法：最大格 ≥60% 时，格内中位数由 picks 自己决定，"
          "「中性化后归零」是构造而非发现；")
    print("回退率 >30% 时 90×3 的切分过细，行业×规模那一列要打折；"
          "规模分位 0/0.5/1 = 小/中/大三档。")

    print("\n三、行业集中度前十（按平均持仓只数）")
    for name in ("low_turnover", "high_turnover"):
        n_day = ind_days[name]
        if not n_day:
            continue
        top = sorted(ind_cnt[name].items(), key=lambda kv: -kv[1])[:10]
        print(f"  {name}（{n_day} 天）: "
              + "，".join(f"{k} {v / n_day:.1%}" for k, v in top))

    #: 唯一出口的记账。**在落盘之后**写，哈希才覆盖住刚写出的那份序列；
    #: 写在计算之前会得到一条「读了确认段、但没记下产物」的日志。
    if INCLUDE_CONFIRM:
        bad = [d for d in days_by["low_turnover"] if d >= GATE.CONFIRM_START]
        if bad:
            row = GATE.log_read("probe_neutral", sys.argv, bad[0], bad[-1], [SERIES])
            print(f"\n⚠ 已把确认段 {bad[0]} … {bad[-1]}（{len(bad)} 天）写进 "
                  f"{SERIES.name}；读取日志第 {row['n']} 行（不可撤销）")


if __name__ == "__main__":
    main()
