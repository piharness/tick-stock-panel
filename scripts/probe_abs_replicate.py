"""补一格：abs（|mom| 前 N）在**换手率**层是否复现。

族系 p=0.0155 只在成交额层算过；abs 是 18 格里唯一过线的东西，
而它是否只是「成交额层」的产物，取决于同一规则在换手率层是否同号同量级。
这一格原产物没有，属于族系外推的唯一硬检验。

**本脚本是 `data/.scratch/abs_replicate.json` 的唯一产地。** `--json PATH` 决定写到哪里；
不给这个参数时只打印、不写文件。它从 `data/.scratch/diag_abs_replicate.py` 迁进
`scripts/` 并进了封签——这一格是「abs 只在成交额层成立」的唯一硬检验，判据要能被
测试读，不能只活在一份被 `.gitignore` 挡住的日志里。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_decile_net as P  # noqa: E402

H = 60
FRAC = 0.2
BLOCK = 2 * H


def stat(x: np.ndarray) -> tuple[float, float, float]:
    fin = x[np.isfinite(x)]
    mu = 1e4 * float(fin.mean())
    s = P.block_se(fin, block=BLOCK)
    return mu, mu / (1e4 * s), 1e4 * s


def layer_series(key: str) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray], np.ndarray]:
    df = P.load_panel()
    if key == "rate":
        inst = pl.read_parquet(P.ROOT / "data/instruments/instruments.parquet").select(
            ["symbol", "float_shares"])
        df = df.join(inst.rename({"symbol": "thscode"}), on="thscode", how="left")
        df = df.with_columns((pl.col("volume") / pl.col("float_shares")).alias("rate"))
    fwd = f"fwd{H}"
    d = (df.filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                   & (pl.col("turnover") > 0) & pl.col(key).is_not_null() & (pl.col(key) > 0))
         .with_columns((((pl.col(key).rank("ordinal").over("date") - 1)
                        / pl.col(key).count().over("date")) < FRAC).alias("in_b"))
         .filter(pl.col("in_b")).sort("date"))
    out_abs, out_hi = {}, {}
    years, sizes = [], []
    for sub in d.partition_by("date", maintain_order=True):
        y = sub[fwd].to_numpy().astype(np.float64)
        m = sub["mom"].to_numpy().astype(np.float64)
        base = y.mean()
        sizes.append(y.size)
        years.append(str(sub["date"][0])[:4])
        for tag, order in (("abs", np.argsort(-np.abs(m), kind="stable")),
                           ("hi", np.argsort(-m, kind="stable"))):
            cs = np.cumsum(y[order])
            for n in (100, 500):
                k = min(n, y.size)
                (out_abs if tag == "abs" else out_hi).setdefault(n, []).append(cs[k - 1] / k - base)
    return (np.asarray(out_abs[100]), np.asarray(out_abs[500]),
            {n: np.asarray(v) for n, v in out_hi.items()}, np.asarray(years))


def _share(pre: np.ndarray, late: np.ndarray, n_pre: int, n_late: int) -> float | None:
    """2024 起贡献了「样本均值 × 天数」的多少份额；分母为 0 时给 None。"""
    tot = n_pre * pre.mean() + n_late * late.mean()
    return float(n_late * late.mean() / tot) if tot else None


def main(out: Path | None = None) -> None:
    print("层 | N | 超额bp | t | se | 2017-2023 | 2024起")
    rep: dict = {
        "meta": {"script": "scripts/probe_abs_replicate.py", "H": H, "frac": FRAC,
                 "block": BLOCK, "ns": [100, 500],
                 "layers": {"turnover": "成交额（turnover 升序前 20%）",
                            "rate": "换手率 = volume / float_shares（升序前 20%）"}},
        "layers": {},
        "definitions": {
            "bp": "「组合 − 层均值」的逐日超额，对日期取算术均值（bp）",
            "t": "bp 除以 `probe_decile_net.block_se` 的块自助 se（block=120）",
            "pre2024": "2017–2023 年的同一序列；起点 2017 是因为 2016 只有 21 个决策日",
            "post2024": "2024 年起至今；2024 是分界年，不是「样本外」",
            "mean_share_from_2024":
                "post.days*post.bp / (pre.days*pre.bp + post.days*post.bp)。"
                "**只在两个子样本同号时可读**：分母接近 0 时它会炸成 −24 这种数"
                "（例子在 rate|hi/500），那不是份额，是两个符号相消的残渣。",
        },
    }
    for key, name in (("turnover", "成交额"), ("rate", "换手率")):
        a100, a500, hi, yrs = layer_series(key)
        m_pre = (yrs >= "2017") & (yrs <= "2023")
        m_late = yrs >= "2024"
        cells: dict = {}
        for lab, ser in (("abs/100", a100), ("abs/500", a500),
                         ("hi/100", hi[100]), ("hi/500", hi[500])):
            mu, t, se = stat(ser)
            pre = ser[m_pre]
            late = ser[m_late]
            print(f"  {name} | {lab:8s} | {mu:+7.1f} | {t:+5.2f} | {se:4.0f} | "
                  f"{1e4*pre.mean():+7.1f} (t={stat(pre)[1]:+5.2f}) | "
                  f"{1e4*late.mean():+7.1f}")
            cells[lab] = {
                "bp": mu, "t": t, "se_bp": se, "n_days": int(np.isfinite(ser).sum()),
                "pre2024": {"bp": float(1e4 * pre.mean()), "t": stat(pre)[1],
                            "from": "2017", "to": "2023", "days": int(m_pre.sum())},
                "post2024": {"bp": float(1e4 * late.mean()),
                             "from": "2024", "to": sorted(set(yrs.tolist()))[-1],
                             "days": int(m_late.sum())},
                #: 均值的加权份额：`post.days * post.bp / (pre.days*pre.bp + post.days*post.bp)`。
                #: 单独存一个字段，是因为「均值集中在 2024 起」这句话必须能被算，
                #: 而不是每次引用时重新心算一遍。
                "mean_share_from_2024": _share(pre, late, m_pre.sum(), m_late.sum()),
            }
        rep["layers"][key] = {"label": name, "cells": cells}
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"\n写入 {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="abs 在换手率层是否复现；产物唯一产地")
    ap.add_argument("--json", type=Path, default=None, help="产物路径；不给就只打印")
    main(out=ap.parse_args().json)
