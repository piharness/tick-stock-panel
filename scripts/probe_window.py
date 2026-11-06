"""实证五：胜率是非平稳的 —— 那「带多少样本」就是一个窗口长度问题。

上一轮发现：p_hist 在 [0.171, 0.500] 之间漂移，扩展窗口（全部历史）对弱信号有害。
本轮扫描窗口长度 W，并做时间上的样本外切分：

  训练段（前 60%）选 W*，验证段（后 40%）只评估，不再调参。
  如果 W* 在验证段的 skill 仍 > 0，说明"滚动窗口"是真的，不是拟合出来的。

缓存两份：`.probe_per_day.json` 存 0/1 命中（胜率口径），
`.probe_per_day_exc.json` 存连续 `exc`（净收益口径）。两者同序同批 picks。
改过计算逻辑后加 `--rebuild` 重算，否则直接读缓存。

## 基准中心（v5）

`exc = fwd − 中心`。中心有三种，代码里只差一个泛函：

    --basis=median  全样本中位数（v1–v4 的口径，legacy 文件名不变）
    --basis=mean    全样本等权均值（随机组合的期望值，即零假设的中心）
    --basis=mcap    流通市值加权均值（「买得起的市场组合」的代理）

中位数不是零假设的中心：随机抽 20 只的期望是**均值**，所以减中位数会给
每个多头组合一个 `均值−中位数` 的常数（325 天实测 +195.7bp/日）。市值加权
同样不是：随机 20 只相对它带一个规模溢价。**只有等权均值满足
`E[exc | 无能力] = 0`**，这不是偏好，是随机化推断的要求。见 §9.33。

胜率标签（0/1 缓存）**恒按中位数**生成，与 `--basis` 无关：`outcome_med` 是
「跑赢典型股票」的 50/50 分类，中位数是它的自然零假设，偏度不进标签。
换中心只影响 bp 口径，不影响 p_win。

## 确认段闸门（v5）

默认把决策日截到 `EXPLORATION_END`（2026-10-08）。这不是防御性编程：本脚本的
循环边界是 `len(cal)-HORIZON`，**没有日期上限**，而 2026-10-09 的 t+20 在
2026-11-06 收盘后齐备——从那天起一次例行 `--rebuild` 就会把第一个确认日写进
缓存，且不打印任何提示。`--include-confirmation` 是唯一出口，它会往
`docs/decision-read-log.jsonl` 追加一行不可撤销的记录。见 `decision_gate.py`。
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import polars as pl


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "decision_gate", Path(__file__).resolve().with_name("decision_gate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load_gate()
#: 确认段只读一次；这个开关就是那一次，代价是日志里多一行。
INCLUDE_CONFIRM = "--include-confirmation" in sys.argv

DATA = Path("data")
CACHE = Path("data/.probe_per_day.json")
#: 连续 `exc`（对基准中心的 20 日超额收益，小数）。0/1 缓存只够算胜率，
#: 净收益与 bp/trade 的 MDE 必须用连续值，两者同序、同一天同一批 picks。
#: `median` 沿用旧文件名，其余口径加后缀——`probe_cs_ic.py`、
#: `probe_incremental_size.py` 与 `test_decision_cs_ic.py` 都按旧名读。
BASIS = next((a.split("=", 1)[1] for a in sys.argv[1:]
              if a.startswith("--basis=")), "median")
if BASIS not in ("median", "mean", "mcap"):
    raise SystemExit(f"--basis 必须是 median/mean/mcap，收到 {BASIS!r}")
EXC_CACHE = (Path("data/.probe_per_day_exc.json") if BASIS == "median"
             else Path(f"data/.probe_per_day_exc_{BASIS}.json"))
REBUILD = "--rebuild" in sys.argv
TABLE = "kline_daily_enriched"
HORIZON = 20
LOOKBACK = 20
TOP_N = 20
KAPPA = 20.0
WINDOWS = (40, 80, 160, 320, 10_000)


def sessions() -> list[str]:
    base = DATA / TABLE
    return sorted(
        c.name.split("=", 1)[1]
        for c in base.iterdir()
        if c.is_dir() and c.name.startswith("date=")
    )


def day_frame(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "raw_close", "turnover_rate"]
    ).collect()


def shares() -> pl.DataFrame:
    df = pl.read_parquet(DATA / "instruments" / "instruments.parquet")
    return (df.select(["symbol", "float_shares"])
            .filter(pl.col("float_shares") > 0)
            .with_columns(pl.col("float_shares").cast(pl.Float64)))


def center_of(base: pl.DataFrame, basis: str) -> float:
    """当日截面的基准中心。三种口径只差一个泛函，其余全部相同。"""
    if basis == "median":
        return float(base["fwd"].median())
    if basis == "mean":
        return float(base["fwd"].mean())
    w = base["mv"]
    tot = float(w.sum())
    if tot <= 0:
        return float("nan")
    return float((base["fwd"] * w).sum()) / tot


SIGNALS = {
    "momentum_20": ("mom", True),
    "reversal_20": ("mom", False),
    "low_turnover": ("turn", False),
    "high_turnover": ("turn", True),
}


def build(rebuild: bool = False) -> dict:
    #: 胜率缓存与 bp 缓存必须同时存在才算命中：换 `--basis` 后 bp 缓存是新的，
    #: 此时要重算（胜率缓存会被写成同样的内容，标签恒按中位数）。
    if CACHE.exists() and EXC_CACHE.exists() and not rebuild:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    cal = sessions()
    if not INCLUDE_CONFIRM:
        #: 截断只作用于**决策日**：cal 仍保留 t+20 所需的价格，
        #: 那是探索日决策的收益，不是确认段样本。
        cal = GATE.cap_calendar(cal)
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    out: dict[str, dict[str, list[int]]] = {k: {} for k in SIGNALS}
    exc: dict[str, dict[str, list[float]]] = {k: {} for k in SIGNALS}
    sh = shares() if BASIS == "mcap" else None
    for i in range(LOOKBACK, len(cal) - HORIZON):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("raw_close"),
                                    pl.col("turnover_rate").alias("turn")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0))
        )
        if base.height < 200:
            continue
        if sh is not None:
            #: 缺股本的股票留在池子里（否则三口径的可选池不同，picks 会漂），
            #: 只是对市值加权基准贡献 0 权重。
            base = base.join(sh, on="symbol", how="left").with_columns(
                (pl.col("raw_close") * pl.col("float_shares"))
                .fill_null(0.0).alias("mv"))
        base = base.with_columns(
            (pl.col("c1") / pl.col("c0") - 1.0).alias("mom"),
            (pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"),
        )
        med = float(base["fwd"].median())
        base = base.with_columns(
            (pl.col("fwd") - center_of(base, BASIS)).alias("exc"),
            #: 胜率标签的零假设是「跑赢典型股票」，恒用中位数，不随 `--basis` 变。
            (pl.col("fwd") - med).alias("exc_med"),
        )
        for name, (col, desc) in SIGNALS.items():
            ranked = base.drop_nulls(col).sort(col, descending=desc).head(TOP_N)
            if ranked.height == TOP_N:
                out[name][t] = [1 if x > 0 else 0 for x in ranked["exc_med"].to_list()]
                exc[name][t] = [round(float(x), 6) for x in ranked["exc"].to_list()]
    CACHE.write_text(json.dumps(out), encoding="utf-8")
    EXC_CACHE.write_text(json.dumps(exc), encoding="utf-8")
    if INCLUDE_CONFIRM:
        dec = GATE.decision_days(cal)
        if dec and dec[-1] >= GATE.CONFIRM_START:
            row = GATE.log_read("probe_window", sys.argv, dec[0], dec[-1],
                                [CACHE, EXC_CACHE])
            print(f"⚠ 已把确认段写进缓存，末日 {dec[-1]}；"
                  f"读取日志第 {row['n']} 行（不可撤销）")
    return out


def evaluate(days: list[str], per_day: dict[str, list[int]],
             window: int) -> list[tuple[float, list[int]]]:
    """返回 [(p_win, outcomes)]，只用 t 时刻已结算的样本。"""
    pos = {d: i for i, d in enumerate(days)}
    recs = []
    for d in days:
        cutoff = pos[d] - HORIZON
        past = [x for x in days if pos[x] <= cutoff][-window:]
        if not past:
            continue
        wins = sum(sum(per_day[x]) for x in past)
        n = sum(len(per_day[x]) for x in past)
        p = (wins + KAPPA * 0.5) / (n + KAPPA)
        recs.append((p, per_day[d]))
    return recs


def skill(recs: list[tuple[float, list[int]]]) -> tuple[float, float, float, int]:
    if not recs:
        return 0.0, 0.0, 0.0, 0
    flat = [(p, y) for p, outs in recs for y in outs]
    N = len(flat)
    b_a = sum((0.5 - y) ** 2 for _, y in flat) / N
    b_b = sum((p - y) ** 2 for p, y in flat) / N
    daily = []
    for p, outs in recs:
        ba = sum((0.5 - y) ** 2 for y in outs) / len(outs)
        bb = sum((p - y) ** 2 for y in outs) / len(outs)
        daily.append(ba - bb)
    m = sum(daily) / len(daily)
    sd = math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1)) if len(daily) > 1 else 0
    return 1 - b_b / b_a, m, sd / math.sqrt(len(daily)), N


def main() -> None:
    data = build(rebuild=REBUILD)
    print("=" * 78)
    print("窗口长度扫描  |  训练段选 W*，验证段只评估")
    print("=" * 78)

    for name, per_day in data.items():
        days = sorted(per_day)
        if len(days) < 100:
            continue
        split = int(len(days) * 0.6)
        train, valid = days[:split], days[split:]

        print(f"\n【{name}】  总决策日 {len(days)}  训练 {len(train)}  验证 {len(valid)}")

        best_w, best_m = None, -9.9
        for w in WINDOWS:
            recs = evaluate(train, per_day, w)
            s, m, se, N = skill(recs)
            flag = ""
            if m - 1.96 * se > 0:
                flag = " *"
            print(f"  W={w:<6} 训练: skill={s:+7.2%}  改善={m:+.5f}±{1.96*se:.5f}{flag}")
            if m > best_m:
                best_m, best_w = m, w

        recs = evaluate(valid, per_day, best_w)
        s, m, se, N = skill(recs)
        print(f"  --> 选 W*={best_w}，验证段: skill={s:+7.2%}  "
              f"改善={m:+.5f}±{1.96*se:.5f}  N={N}")
        verdict = ("SIGNIFICANT+" if m - 1.96 * se > 0 else
                   "NEGATIVE" if m + 1.96 * se < 0 else "NOT_SIG")
        print(f"      验证判定: {verdict}")

        # 对照：验证段用全历史
        recs_all = evaluate(valid, per_day, 10_000)
        s2, m2, se2, _ = skill(recs_all)
        print(f"      对照 全历史: skill={s2:+7.2%} 改善={m2:+.5f}±{1.96*se2:.5f}")


if __name__ == "__main__":
    main()
