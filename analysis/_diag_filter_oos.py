"""评分/出场改造的样本外检验：2019-2021 选参，2022-2026 只跑一次。"""
from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"
PANEL = OUT / "replay_daily.parquet"

spec = importlib.util.spec_from_file_location("fr", ROOT / "analysis" / "_diag_filter_replay.py")
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)

DESIGN = (pd.Timestamp("2019-01-01"), pd.Timestamp("2021-12-31"))
OOS = (pd.Timestamp("2022-01-01"), pd.Timestamp("2026-11-20"))

FILTERS = ["baseline", "no_limit", "no_both"]
SCORES = ["high", "no_q4", "low", "q0"]
EXITS = [
    ("orig", -0.08, True),
    ("stop15", -0.15, True),
    ("stop20", -0.20, True),
    ("fixed20", None, False),
    ("stop15_fixed", -0.15, False),
]

PRESPEC = [
    ("baseline", "high", "orig", "baseline"),
    ("no_q4", "no_q4", "orig", "no_both"),
    ("low", "low", "orig", "no_both"),
    ("no_q4_fixed", "no_q4", "fixed20", "no_both"),
    ("low_fixed", "low", "fixed20", "no_both"),
    ("q0_fixed", "q0", "fixed20", "no_both"),
    ("no_q4_stop15", "no_q4", "stop15", "no_both"),
    ("low_stop15", "low", "stop15", "no_both"),
    ("high_fixed", "high", "fixed20", "no_both"),
]


def run(panel, mode, score_mode, exit_name, stop, sig, window):
    r = fr.replay(panel, mode, score_mode=score_mode, stop_pct=stop,
                  use_signal_exit=sig, start=window[0], end=window[1])
    r.pop("nav", None)
    r.pop("trades_df", None)
    r["score_mode"] = score_mode
    r["exit"] = exit_name
    r["filter"] = mode
    return r


def main() -> None:
    df = pd.read_parquet(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    design, oos = [], []
    for mode, score_mode, (ex_name, stop, sig) in itertools.product(FILTERS, SCORES, EXITS):
        r = run(df, mode, score_mode, ex_name, stop, sig, DESIGN)
        design.append(r)
        print("D", mode, score_mode, ex_name,
              round(r["ann_excess"], 3), round(r["sharpe_excess"], 2), flush=True)
    d = pd.DataFrame(design)
    d.to_csv(OUT / "oos_design.csv", index=False)

    # 选参：设计窗 Sharpe 最高，要求交易数 >=100、平均暴露 >=0.7
    ok = d[(d["trades"] >= 100) & (d["avg_exposure"] >= 0.7)].copy()
    ok = ok.sort_values("sharpe_excess", ascending=False)
    top = ok.head(5)
    print("\nTOP5 design:\n", top[["filter", "score_mode", "exit", "cagr", "ann_excess",
                                  "sharpe_excess", "max_dd", "ann_turnover", "trades"]].to_string(index=False))

    for _, row in top.iterrows():
        r = run(df, row["filter"], row["score_mode"], row["exit"],
                dict((e[0], e[1]) for e in EXITS)[row["exit"]],
                dict((e[0], e[2]) for e in EXITS)[row["exit"]], OOS)
        oos.append(r)
    o = pd.DataFrame(oos)
    o.to_csv(OUT / "oos_test.csv", index=False)
    print("\nOOS top5:\n", o[["filter", "score_mode", "exit", "cagr", "ann_excess",
                             "sharpe_excess", "max_dd", "ann_turnover", "trades", "win_rate"]].to_string(index=False))

    pre = []
    for name, score_mode, ex_name, mode in PRESPEC:
        r = run(df, mode, score_mode, ex_name,
                dict((e[0], e[1]) for e in EXITS)[ex_name],
                dict((e[0], e[2]) for e in EXITS)[ex_name], OOS)
        r["name"] = name
        pre.append(r)
    p = pd.DataFrame(pre)
    p.to_csv(OUT / "oos_prespec.csv", index=False)
    print("\nPRESPEC OOS:\n", p[["name", "cagr", "ann_excess", "sharpe_excess", "max_dd",
                                "ann_turnover", "trades", "win_rate"]].to_string(index=False))

    json.dump({"design": d.to_dict("records"), "oos_top": o.to_dict("records"),
               "oos_prespec": p.to_dict("records")},
              open(OUT / "oos_result.json", "w"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    main()
