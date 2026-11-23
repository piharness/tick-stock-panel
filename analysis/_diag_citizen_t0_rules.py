"""阶段 2：日内往返规则回测（严格 bar close 成交，无未来函数）。

规则（锚点 = 当日第一根 5min bar 的 close，9:35 时刻已知）：

R1 正T低吸  : 某 bar close <= anchor*(1-d)  -> 买入该 bar close
              其后某 bar close >= anchor     -> 卖出该 bar close
              至 14:55 仍未回锚               -> 以最后 bar close 平仓
R2 负T高抛  : 某 bar close >= anchor*(1+d)  -> 卖出底仓（该 bar close）
              其后某 bar close <= anchor     -> 买回
              至 14:55 仍未回锚               -> 以最后 bar close 买回
R3 开->收   : 第一根 open 买，最后 close 卖（纯多头日内 = beta）
R4 开->收(反): 第一根 open 卖，最后 close 买

全部向量化（S x 48 矩阵）。只用 close/open，不碰 high/low。
只保留 nbar==48 的票日（bar 对齐才可矩阵化），损失量单独报。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_probe_t0"
NBAR = 48
# 一根 bar 的相对振幅低于此值 ⇒ 视为「锁死」（一字板/跌停封死），不可作为成交点。
# 0.2% 对 2 元股 = 0.004 元 < 最小变动价位 0.01 元，等价于 high==low。
LOCK_REL = 0.002


def load_day(d: str):
    p = DATA / "kline_5min" / f"date={d}" / "part.parquet"
    m = pl.read_parquet(p).sort(["symbol", "datetime"]).with_columns(
        pl.int_range(pl.len()).over("symbol").alias("bar"))
    cnt = m.group_by("symbol").len().filter(pl.col("len") == NBAR).select("symbol")
    m = m.join(cnt, on="symbol", how="inner")
    syms = m["symbol"].unique(maintain_order=True).to_list()
    C = np.full((len(syms), NBAR), np.nan)
    O = np.full((len(syms), NBAR), np.nan)
    L = np.full((len(syms), NBAR), np.nan)
    pos = {s: i for i, s in enumerate(syms)}
    idx = np.array([pos[s] for s in m["symbol"].to_list()])
    bar = m["bar"].to_numpy()
    C[idx, bar] = m["close"].to_numpy()
    O[idx, bar] = m["open"].to_numpy()
    L[idx, bar] = ((m["high"] - m["low"]) / m["close"]).to_numpy()
    M = np.isfinite(L) & (L >= LOCK_REL)          # True = 该 bar 可成交
    return syms, O, C, M


def run_day(O: np.ndarray, C: np.ndarray, M: np.ndarray, d: float, cost: float):
    """返回 (r1, r2, r3, r4, filled1, filled2)。成交点必须 M=True。"""
    S = C.shape[0]
    anchor = C[:, 0]
    ar = np.arange(NBAR)
    out = {}
    last_ok = np.where(M.any(1), (M * ar).max(1), NBAR - 1)
    rows = np.arange(S)

    def trip(mask_first, mask_second, buy_first: bool, lag: int = 0):
        """先执行 first，再在其后找 second；未找到则用最后可成交 bar 平仓。

        buy_first=True  买 px1、卖 px2 ⇒ 收益 px2/px1 − 1（正T）
        buy_first=False 卖 px1、买 px2 ⇒ 收益 px1/px2 − 1（负T）
        两者不可互换：负T 的收益是倒数关系，不是取负。

        lag>0：把首个触发点向后推 lag 根 bar（过拟合检验：若收益崩塌，说明
        赚的是「精确踩中那一根」的能力，实盘不可复制）。
        """
        has1 = mask_first.any(1)
        i1_ = mask_first.argmax(1) + lag
        ok_lag = i1_ < NBAR
        i1_ = np.clip(i1_, 0, NBAR - 1)
        after = ar[None, :] > i1_[:, None]
        sec = mask_second & after
        has2 = sec.any(1)
        i2_ = np.where(has2, sec.argmax(1), last_ok)
        px1 = C[rows, i1_]
        px2 = C[rows, i2_]
        r = np.full(S, np.nan)
        ok = has1 & ok_lag & np.isfinite(px1) & np.isfinite(px2) \
            & M[rows, i1_] & M[rows, i2_]
        ratio = px2[ok] / px1[ok] if buy_first else px1[ok] / px2[ok]
        r[ok] = ratio - 1.0 - cost
        return r, has1 & ok_lag & M[rows, i1_]

    # R1 正T低吸：买跌 -> 卖回锚
    buy = (C <= (anchor * (1 - d))[:, None]) & M
    buy[:, 0] = False
    sell = (C >= anchor[:, None]) & M
    out["r1"], out["f1"] = trip(buy, sell, True)
    out["r1_lag1"], _ = trip(buy, sell, True, lag=1)

    # 盘中可知的市场状态：前 6 根 bar（09:30-10:00）内触发的票占全市场比例。
    # 用它当开关才对：收盘后的全天占比是同时性变量，不能交易。
    out["early_share"] = float(buy[:, :6].any(1).mean())

    # R1b：10:00 之后才允许买入（前 6 根 bar 不得成交），与 early_share 配套
    buy_late = buy.copy()
    buy_late[:, :6] = False
    out["r1_late"], out["f1_late"] = trip(buy_late, sell, True)

    # R2 负T高抛：卖涨 -> 买回锚
    sell2 = (C >= (anchor * (1 + d))[:, None]) & M
    sell2[:, 0] = False
    buy2 = (C <= anchor[:, None]) & M
    out["r2"], out["f2"] = trip(sell2, buy2, False)
    out["r2_lag1"], _ = trip(sell2, buy2, False, lag=1)

    # R3 / R4 开->收（首尾 bar 也须可成交）
    o0, c1 = O[:, 0], C[rows, last_ok]
    base = c1 / o0 - 1 - cost
    bad = ~(M[:, 0] & M[rows, last_ok]) | ~np.isfinite(base)
    out["r3"] = np.where(bad, np.nan, base)
    out["r4"] = np.where(bad, np.nan, -base)
    return out


def main() -> None:
    panel = pl.read_parquet(OUT / "panel.parquet")
    days = sorted(panel["date"].unique().to_list())

    DS = (0.005, 0.01, 0.02, 0.03)
    COST = 12.0 / 1e4
    tf = OUT / "trades.parquet"
    if tf.exists():
        df = pl.read_parquet(tf)
        print(f"[trades] cached {df.shape}")
    else:
        frames = []
        t0 = time.time()
        for k, d in enumerate(days):
            sub = panel.filter(pl.col("date") == d).select(
                ["symbol", "level", "ampl", "i1", "ac1"])
            try:
                syms, O, C, M = load_day(d)
            except Exception as e:  # noqa: BLE001
                print(f"  skip {d}: {e}")
                continue
            for dd in DS:
                r = run_day(O, C, M, dd, COST)
                t = pl.DataFrame({
                    "symbol": syms,
                    "date": np.full(len(syms), d, dtype=object),
                    "d": np.full(len(syms), dd, dtype=np.float64),
                    "r1": r["r1"], "r2": r["r2"], "r3": r["r3"], "r4": r["r4"],
                    "r1_lag1": r["r1_lag1"], "r2_lag1": r["r2_lag1"],
                    "r1_late": r["r1_late"],
                    "early_share": np.full(len(syms), r["early_share"]),
                    "f1": r["f1"], "f2": r["f2"],
                }).with_columns(pl.col("date").cast(pl.String)).join(
                    sub, on="symbol", how="inner")
                frames.append(t)
            if k % 60 == 0:
                print(f"  {k}/{len(days)} {d} n={len(frames)} {time.time()-t0:.0f}s", flush=True)
        df = pl.concat(frames)
        df.write_parquet(tf)
        print(f"[trades] {df.shape} {time.time()-t0:.0f}s")

    # ---------------- 汇总 ----------------
    def agg(x: str):
        c = pl.col(x).fill_nan(None)
        return [
            c.len().alias(f"{x}_n"),
            (c.mean() * 1e4).alias(f"{x}_bp"),
            (c.std() * 1e4 / c.len().sqrt()).alias(f"{x}_se"),
            (c > 0).mean().alias(f"{x}_hit"),
        ]

    res = (df.group_by(["level", "d"])
             .agg(agg("r1") + agg("r2") + agg("r3") +
                  [pl.col("f1").mean().alias("fill1"), pl.col("f2").mean().alias("fill2")])
             .with_columns([
                 (pl.col("r1_bp") / pl.col("r1_se")).alias("r1_t"),
                 (pl.col("r2_bp") / pl.col("r2_se")).alias("r2_t"),
                 (pl.col("r3_bp") / pl.col("r3_se")).alias("r3_t"),
             ])
             .sort(["d", "level"]))

    print("\n=== R1 正T低吸（买跌回锚卖）===")
    for row in res.sort(["d", "level"]).iter_rows(named=True):
        print(f"d={row['d']:.3f} {row['level']:<11} n={row['r1_n']:>7} "
              f"net={row['r1_bp']:+7.1f}bp t={row['r1_t']:+5.2f} "
              f"hit={row['r1_hit']:.3f} fill={row['fill1']:.3f}")
    print("\n=== R2 负T高抛（卖涨回锚买）===")
    for row in res.sort(["d", "level"]).iter_rows(named=True):
        print(f"d={row['d']:.3f} {row['level']:<11} n={row['r2_n']:>7} "
              f"net={row['r2_bp']:+7.1f}bp t={row['r2_t']:+5.2f} "
              f"hit={row['r2_hit']:.3f} fill={row['fill2']:.3f}")

    print("\n=== R3 开->收（beta 参照，d 无关）===")
    r3 = df.group_by("level").agg(agg("r3")).with_columns(
        (pl.col("r3_bp") / pl.col("r3_se")).alias("r3_t")).sort("level")
    for row in r3.iter_rows(named=True):
        print(f"{row['level']:<11} n={row['r3_n']:>8} mean={row['r3_bp']:+7.1f}bp "
              f"t={row['r3_t']:+5.2f} hit={row['r3_hit']:.3f}")

    res.write_parquet(OUT / "by_level.parquet")
    print("\nwrote by_level.parquet")


if __name__ == "__main__":
    main()
