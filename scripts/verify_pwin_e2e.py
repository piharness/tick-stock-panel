"""端到端验证：真实行情 + 隔离目录，跑「建卡 → 结算 → 两口径计分 → p_win 自举」。

三件事必须被证明，否则 pwin 只是纸面代码：

  1. 结算真的写出了 `outcome_med`（不是只有单元测试里的 dict）；
  2. 冷启动时 p_win 回退到 0.5，随结算累积逐步收敛到真实胜率；
  3. abs 口径与 median 口径给出**不同**的样本量含义（同一批数据，两个 n）。

不碰 `data/user_data/`：所有写入落在临时目录，行情按需硬链接。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.decision import bars, ledger, pwin, settle  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "data"
TABLE = "kline_daily_enriched"
INDEX = "kline_index_enriched"
HORIZON = 20
LOOKBACK = 20
TOP_N = 10
DAYS = 40


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build_workspace(tmp: Path, need: list[str]) -> None:
    for day in need:
        (tmp / "kline_daily" / f"date={day}").mkdir(parents=True, exist_ok=True)
        for table in (TABLE, INDEX):
            src_dir = SRC / table / f"date={day}"
            if not src_dir.is_dir():
                continue
            for f in src_dir.glob("*.parquet"):
                _link(f, tmp / table / f"date={day}" / f.name)
    src_dir = SRC / "instruments"
    if src_dir.is_dir():
        shutil.copytree(src_dir, tmp / "instruments", dirs_exist_ok=True)


def day_close(day: str) -> pl.DataFrame:
    part = SRC / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "turnover_rate"]).collect()


def main() -> None:
    cal = bars.sessions(SRC)
    print(f"交易日 {len(cal)}（{cal[0]} .. {cal[-1]}）")

    idx = list(range(LOOKBACK, len(cal) - HORIZON))[-DAYS:]
    need = sorted({cal[i] for i in idx}
                  | {cal[i + HORIZON] for i in idx}
                  | {cal[i + 1] for i in idx}
                  | {cal[i - LOOKBACK] for i in idx})
    print(f"决策日 {len(idx)}，涉及行情 {len(need)} 天")

    frames = {d: day_close(d) for d in need}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    signals: list[dict] = []
    for i in idx:
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("turnover_rate").alias("turn")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0))
        )
        if base.height < 200:
            continue
        top = base.drop_nulls("turn").sort("turn", descending=True).head(TOP_N)
        if top.height < TOP_N:
            continue
        signals.append({
            "date": t,
            "symbols": top["symbol"].to_list(),
            "prices": [float(x) for x in top["c1"].to_list()],
        })

    print(f"可用决策日 {len(signals)}")

    tmp = Path(tempfile.mkdtemp(prefix="tsp-decision-e2e-"))
    try:
        build_workspace(tmp, need)
        print(f"隔离工作区 {tmp}")

        trace: list[dict] = []
        for sig in signals:
            rows = ledger.load_settlements(tmp)
            est = pwin.suggest(rows, strategy_id="high_turnover",
                               horizon=HORIZON, basis=settle.BASIS_MEDIAN,
                               allow_prior=True)
            p = float(est["p_win"])
            picks = [
                {"symbol": s, "limit_price": round(px * 1.05, 4), "p_win": round(p, 6)}
                for s, px in zip(sig["symbols"], sig["prices"])
            ]
            card = ledger.build_card(
                as_of=sig["date"], picks=picks, horizon_sessions=HORIZON,
                strategy_id="high_turnover", gates={"decision": "GO"},
                # 与 API 的 `_resolve_picks` 一致：出处进 forecast ⇒ 进承诺哈希。
                # 不写这一段，结算就分不清 p 是账本算的还是先验常数。
                forecast={"pwin": {k: est.get(k) for k in
                                   ("source", "basis", "n", "hits", "days", "kappa",
                                    "prior_weight", "prior_weight_effective",
                                    "reliable", "verdict", "oos_skill", "oos_days")}},
            )
            ledger.save_card(tmp, card)
            settle.settle(tmp)          # 结算所有已到期的卡
            trace.append({"date": sig["date"], "p": p, "source": est["source"],
                          "n": est["n"], "reliable": est["reliable"]})

        print(f"\n建卡 {len(trace)} 张，p_win 演化：")
        for row in trace[:: max(1, len(trace) // 8)]:
            print(f"  {row['date']}  p={row['p']:.3f}  n={row['n']:<4} "
                  f"source={row['source']:<7} reliable={row['reliable']}")

        rows = ledger.load_settlements(tmp)
        covered = sum(1 for r in rows if r.get("outcome_med") is not None)
        print(f"\n结算 {len(rows)} 条，outcome_med 覆盖 {covered}")

        sample = next(r for r in rows if r.get("outcome_med") is not None)
        print("样本结算记录：")
        for key in ("as_of", "symbol", "p_win", "median_ret", "excess_med",
                    "outcome", "outcome_med"):
            print(f"  {key:<12} {sample.get(key)}")

        for basis in (settle.BASIS_ABS, settle.BASIS_MEDIAN):
            s = settle.score(rows, basis=basis)
            print(f"\n[{basis}] n={s['n']} days={s['days']} "
                  f"hit={s['hit_rate']} brier={s['brier']} "
                  f"const={s['brier_const']} skill={s['skill']} "
                  f"lcb={s['skill_lcb']} sig={s['significance']}")
            # 读数与结论分开打：lcb>0 只是读数，significance 才是结论。
            print(f"      lcb_positive={s['skill_lcb_positive']} "
                  f"significant={s['significant']}")
            # 有效样本：days 是决策日数，n 是观测数，都不是「几个独立样本」。
            print(f"      n_eff={s.get('n_eff')} lag={s.get('hac_lag')} "
                  f"reliable={s.get('n_eff_reliable')}")
            # 三个基线分开打：`const` 是实际用的分母，另外两个是对照。
            # abs 的 const 是展开 climatology；insample 只做诊断，不能当分母。
            print(f"      baselines: random={s.get('brier_random')} "
                  f"clim={s.get('brier_clim')} insample={s.get('brier_insample')} "
                  f"skill_vs_clim={s.get('skill_vs_clim')}")
            print(f"      {s['verdict']} — {s['reason']}")
            # 冷启动期 p_win 来自 prior(0.5)，账本期来自 ledger。混在一起看会把
            # 「没有信息」的那段算进「有信息」的那段里，所以必须拆开。
            for source, part in (s.get("by_source") or {}).items():
                print(f"      by_source[{source}] n={part['n']} "
                      f"hit={part['hit_rate']} skill={part['skill']} "
                      f"{part['verdict']}")

        est = pwin.estimate(rows, strategy_id="high_turnover", horizon=HORIZON,
                            basis=settle.BASIS_MEDIAN)
        print("\np_win 自举（全账本）：")
        for key in ("n", "hits", "hit_rate", "days", "oos_days", "p_win", "source",
                    "prior_weight", "prior_weight_effective",
                    "reliable", "verdict", "significance", "oos_skill",
                    "n_eff", "n_eff_reliable", "n_eff_incremental", "hac_lag",
                    # 增量序列的滞后阶数：与 `hac_lag` 相等（都是 horizon-1）。
                    # 字段分开存是因为 §9.18 曾让它取 window-1，§9.20 撤回——
                    # 留一个位置，下次改错时看得见。
                    "hac_lag_incremental",
                    # `truncated_any=False` ⇒ 增量检验没有功效，结论是 NOT_TESTABLE。
                    # `truncated_days` 是「实际比了几天」：True 也可能是 1 天。
                    "truncated_any", "truncated_days", "verdict_incremental",
                    # 第二个检验的可辨识性。真实账本 `n_eff=1` ⇒ UNKNOWN：
                    # 这里的 `INCREMENTAL` 永远不该出现，出现就是判据错了。
                    "significance_incremental",
                    "skill_vs_clim",
                    "reason_incremental", "reason"):
            print(f"  {key:<24} {est.get(key)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print("\n临时目录已清理")


if __name__ == "__main__":
    main()
