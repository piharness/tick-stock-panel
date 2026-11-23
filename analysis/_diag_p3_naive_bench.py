"""第 3 阶段 · 归零检验的对照：一条规则能不能替代这个分类器。

不跑回归，直接建对照组合（同为 d+1 开盘买、d+15 收盘卖、basic_ok 池、同日等权）：
  N1 最低 vol20 五分位
  N2 最低 turn20 五分位
  N3 最低 vol20 ∩ 最低 turn20
  S  温和 ramp 分类器
若 N1/N2/N3 的超额接近或超过 S，则分类器没有增量。

复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_naive_bench.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_p3_residualize import nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "p3"
TAB = OUT / "resid_table.parquet"


def main() -> None:
    pdf = (pl.read_parquet(TAB, columns=["date", "sig", "ex15", "vol20", "turn20", "idio_vol60"])
           .filter(pl.col("date") >= pl.date(2018, 9, 1))
           .to_pandas())
    pdf = pdf[np.isfinite(pdf.ex15)]
    g = pdf.groupby("date", sort=True)

    def daily(mask_series: pd.Series) -> pd.Series:
        s = pdf[mask_series].groupby("date").ex15.mean()
        return s

    q_vol = g.vol20.transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
    q_turn = g.turn20.transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
    q_idio = g.idio_vol60.transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))

    specs = {
        "S_gentle": pdf.sig == 1,
        "N1_low_vol20": q_vol == 0,
        "N2_low_turn20": q_turn == 0,
        "N3_low_vol_and_turn": (q_vol == 0) & (q_turn == 0),
        "N4_low_idio_vol": q_idio == 0,
        "N5_low_vol_or_turn": (q_vol == 0) | (q_turn == 0),
        "universe": pd.Series(True, index=pdf.index),
    }
    rows = []
    for name, m in specs.items():
        s = daily(m)
        mu, t = nw_t(s.to_numpy())
        rows.append({"spec": name, "days": int(s.size), "avg_names": float(m.sum() / max(s.size, 1)),
                     "ex_bp": mu * 1e4, "t": t,
                     "net30_bp": (mu - 0.003) * 1e4})
    tab = pd.DataFrame(rows)
    print(tab.round(1).to_string(index=False))

    # 两条曲线的相关（同一天）
    s_sig = daily(specs["S_gentle"]).rename("S")
    out = {"table": tab.to_dict("records"), "corr": {}}
    for name in ("N1_low_vol20", "N2_low_turn20", "N3_low_vol_and_turn", "N4_low_idio_vol"):
        j = pd.concat([s_sig, daily(specs[name]).rename("N")], axis=1).dropna()
        out["corr"][name] = float(j.S.corr(j.N))
    print("\n与 S 的日收益相关：", {k: round(v, 2) for k, v in out["corr"].items()})
    json.dump(out, open(OUT / "naive_bench.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
