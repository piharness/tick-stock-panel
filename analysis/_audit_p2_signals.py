"""Phase 2 审计 · 步骤 1：从原始日线独立重建信号，并生成真实成交字段。

冻结规则（不得修改）：
  basic_ok  : raw_close 5~200、总市值>=20亿、amount>=1亿、k>=60、非当前ST
  cum5      : 过去 5 日累计超额（相对 basic_ok 等权，0~3%）
  mono5>=0.5, eff5>=0.3, dd5>=-0.6, 1<=snr5<2
  入场 d+1 开盘，出场 d+15 收盘

本脚本只做两件事：
1) 独立重建，和 data/.scratch/footprint20d/gentle_ramp_signals.parquet 对账；
2) 为每个信号补齐真实可成交字段（raw_open、涨跌停、停牌顺延、ADV、波动、行业、市值）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / ".scratch" / "p2audit"
OUT.mkdir(parents=True, exist_ok=True)
PANEL = DATA / ".scratch" / "footprint20d" / "replay_daily.parquet"
SIGOLD = DATA / ".scratch" / "footprint20d" / "gentle_ramp_signals.parquet"
RESEARCH = DATA / ".scratch" / "footprint20d" / "panel.parquet"

MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asof_join(sig: pd.DataFrame, rp: pd.DataFrame) -> pd.DataFrame:
    """把每 5 日的面板锚点映射回信号日：取不晚于信号日的最近一个锚点。"""
    anchor_dates = np.array(sorted(rp["date"].unique()))
    all_dates = np.array(sorted(sig["date"].unique()))
    pos = np.searchsorted(anchor_dates, all_dates, side="right") - 1
    m = pd.DataFrame({"date": all_dates, "date_anchor": anchor_dates[np.clip(pos, 0, None)]})
    out = sig.merge(m, on="date", how="left").merge(
        rp.rename(columns={"date": "date_anchor"}), on=["symbol", "date_anchor"], how="left")
    return out


def zpath(R: np.ndarray, mkt: np.ndarray, d: int):
    ex = R[d - 4:d + 1] - mkt[d - 4:d + 1, None]
    cum = np.cumsum(ex, axis=0)
    cum5 = cum[4]
    day = np.arange(1, 6, dtype=float)
    x = day - day.mean()
    y = cum - cum.mean(axis=0)
    mono = (x @ y) / np.sqrt((x @ x) * (y * y).sum(axis=0) + 1e-12)
    eff = cum5 / (np.abs(ex).sum(axis=0) + 1e-12)
    run = np.maximum.accumulate(cum, axis=0)
    dd = ((cum - run) / (np.abs(cum5) + 1e-9)).min(axis=0)
    snr = cum5 / (ex.std(axis=0) * np.sqrt(5) + 1e-9)
    return cum5, mono, eff, dd, snr


def limit_price(prev: np.ndarray, up: bool) -> np.ndarray:
    """A 股主板 10% 涨跌停价，四舍五入到分。"""
    raw = prev * (1.10 if up else 0.90) * 100.0
    return np.where(up, np.floor(raw + 0.5), np.ceil(raw - 0.5)) / 100.0


def build() -> None:
    df = pd.read_parquet(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df.symbol.str.startswith(MAIN)]
    cols = ["open", "close", "raw_close", "raw_high", "raw_low", "amount", "basic_ok", "ret"]
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index() for c in cols}
    dates = list(w["open"].index)
    syms = list(w["open"].columns)
    op = w["open"].to_numpy(float)
    cl = w["close"].to_numpy(float)
    rc = w["raw_close"].to_numpy(float)
    rh = w["raw_high"].to_numpy(float)
    rl = w["raw_low"].to_numpy(float)
    amt = w["amount"].to_numpy(float)
    B = w["basic_ok"].to_numpy(float) == 1
    R = w["ret"].to_numpy(float)

    # 复权因子一致：high/raw_high == close/raw_close == low/raw_low（已单独验证到 2e-16）
    adj = np.where((cl > 0) & (rc > 0), rc / cl, np.nan)
    ro = op * adj

    prev_rc = pd.DataFrame(rc).ffill().shift(1).to_numpy(float)
    up_lim = limit_price(prev_rc, True)
    dn_lim = limit_price(prev_rc, False)
    oneword = np.abs(rh - rl) < 1e-9
    at_up = ro >= up_lim - 1e-6
    at_dn = rc <= dn_lim + 1e-6

    # ADV / 波动（用于冲击成本）
    adv20 = pd.DataFrame(amt).rolling(20, min_periods=10).mean().to_numpy(float)
    vol20 = pd.DataFrame(R).rolling(20, min_periods=10).std().to_numpy(float)
    turn = np.where(adv20 > 0, 1.0, np.nan)

    mkt = np.array([np.nanmean(R[i, B[i]]) if B[i].sum() > 50 else 0.0 for i in range(len(dates))])

    rows = []
    grid_rows = []  # 供阈值邻域用：逐日全部 basic_ok 的特征（只存索引）
    for d in range(5, len(dates) - 15):
        if B[d].sum() < 300 or not np.isfinite(op[d + 1]).any():
            continue
        cum5, mono, eff, dd, snr = zpath(R, mkt, d)
        base = B[d] & np.isfinite(cum5)
        sig = (base & (cum5 > 0) & (cum5 <= 0.03) & (mono >= 0.5)
               & (eff >= 0.3) & (dd >= -0.6) & (snr >= 1.0) & (snr < 2.0))
        fwd = cl[d + 15] / op[d + 1] - 1
        okb = B[d] & np.isfinite(fwd)
        bench = np.nanmean(fwd[okb]) if okb.sum() > 0 else np.nan

        for a in np.flatnonzero(sig):
            # ---- 入场：d+1 开盘，停牌则顺延（最多 5 日）
            e = d + 1
            while e <= min(d + 5, len(dates) - 1) and not np.isfinite(op[e, a]):
                e += 1
            filled = e <= min(d + 5, len(dates) - 1) and np.isfinite(op[e, a])
            # ---- 出场：d+15 收盘，停牌/跌停顺延（最多到 d+30）
            x = d + 15
            while x <= min(d + 30, len(dates) - 1) and not np.isfinite(cl[x, a]):
                x += 1
            exit_ok = x <= min(d + 30, len(dates) - 1) and np.isfinite(cl[x, a])
            delay = 0
            if exit_ok:
                # 跌停封死 → 顺延一日（只在收盘价等于跌停价且一字板时判定）
                while (x <= min(d + 40, len(dates) - 1) and np.isfinite(cl[x, a])
                       and at_dn[x, a] and oneword[x, a]):
                    x += 1
                    delay += 1
                    while x <= min(d + 40, len(dates) - 1) and not np.isfinite(cl[x, a]):
                        x += 1
                    if x > min(d + 40, len(dates) - 1) or not np.isfinite(cl[x, a]):
                        exit_ok = False
                        break

            ret_orig = cl[d + 15, a] / op[d + 1, a] - 1 if np.isfinite(cl[d + 15, a]) else np.nan
            ret_exec = (cl[x, a] / op[e, a] - 1) if (filled and exit_ok) else np.nan
            rows.append((
                dates[d], d, syms[a], a, cum5[a], mono[a], eff[a], dd[a], snr[a],
                filled, e, d + 1, ro[e, a] if filled else np.nan,
                (ro[e, a] / prev_rc[e, a] - 1) if (filled and np.isfinite(prev_rc[e, a])) else np.nan,
                bool(filled and at_up[e, a]), bool(filled and oneword[e, a] and at_up[e, a]),
                exit_ok, x, d + 15, delay,
                ret_orig, ret_exec, bench,
                ret_orig - bench if np.isfinite(ret_orig) else np.nan,
                ret_exec - bench if np.isfinite(ret_exec) else np.nan,
                amt[d, a], amt[e, a] if filled else np.nan,
                adv20[d, a], adv20[e, a] if filled else np.nan,
                vol20[d, a],
            ))

    cols_out = [
        "date", "di", "symbol", "ai", "cum5", "mono5", "eff5", "dd5", "snr5",
        "entry_filled", "entry_di", "entry_di_planned", "entry_raw_open",
        "entry_gap", "entry_at_limit_up", "entry_oneword_up",
        "exit_ok", "exit_di", "exit_di_planned", "exit_delay_days",
        "ret_orig", "ret_exec", "bench", "ex_orig", "ex_exec",
        "amount_d", "amount_entry", "adv20_d", "adv20_entry", "vol20",
    ]
    s = pd.DataFrame(rows, columns=cols_out)
    s["year"] = s.date.dt.year
    s["main_board"] = s.symbol.str.startswith(MAIN)
    s.to_parquet(OUT / "signals.parquet")

    # ---- 与旧信号对账
    old = pd.read_parquet(SIGOLD)
    old["date"] = pd.to_datetime(old["date"])
    key_new = set(zip(s.date, s.symbol))
    key_old = set(zip(old.date, old.symbol))
    rec = {
        "n_new": len(s), "n_old": len(old),
        "only_new": len(key_new - key_old), "only_old": len(key_old - key_new),
        "same_keys": len(key_new & key_old),
        "old_gross_bp": float(old.ex15.mean() * 1e4),
        "new_gross_bp_orig": float(s.ex_orig.mean() * 1e4),
        "old_days": int(old.date.nunique()), "new_days": int(s.date.nunique()),
        "date_min": str(s.date.min().date()), "date_max": str(s.date.max().date()),
        "filled_rate": float(s.entry_filled.mean()),
        "exit_ok_rate": float(s.exit_ok.mean()),
        "ret_exec_available": float(s.ret_exec.notna().mean()),
    }
    json.dump(rec, open(OUT / "reconcile.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rec, ensure_ascii=False, indent=1))

    # ---- 数据治理统计
    gov = {}
    last = df.groupby("symbol").date.max()
    gov["symbols"] = int(last.size)
    gov["last_date_lt_2025"] = int((last < pd.Timestamp("2025-01-01")).sum())
    gov["last_date_lt_2026"] = int((last < pd.Timestamp("2026-01-01")).sum())
    gov["first_date_gt_2019"] = int((df.groupby("symbol").date.min() > pd.Timestamp("2019-01-02")).sum())
    yr = df.groupby(df.date.dt.year).symbol.nunique()
    gov["symbols_per_year"] = {int(k): int(v) for k, v in yr.items()}
    json.dump(gov, open(OUT / "governance.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(gov, ensure_ascii=False, indent=1))

    # ---- 研究面板补充字段（industry / log_mcap），面板只有每 5 日的锚点，按 di 做 as-of
    rp = pd.read_parquet(RESEARCH, columns=["symbol", "date", "anchor", "industry", "log_mcap"])
    rp = rp[rp.anchor].drop(columns=["anchor"])
    rp["date"] = pd.to_datetime(rp["date"])
    s2 = asof_join(s, rp)
    s2 = s2.sort_values(["date", "symbol"]).reset_index(drop=True)
    s2.to_parquet(OUT / "signals_enriched.parquet")
    print("industry coverage", round(s2.industry.notna().mean(), 4),
          "mcap coverage", round(s2.log_mcap.notna().mean(), 4),
          "n", len(s2))


def hashes() -> None:
    files = [
        ROOT / "analysis" / "_audit_p2_signals.py",
        ROOT / "analysis" / "_diag_gentle_ramp.py",
        ROOT / "analysis" / "_diag_gentle_ablation.py",
        PANEL, SIGOLD,
        DATA / "kline_daily_enriched" / "date=2026-11-20" / "part.parquet",
        DATA / "backfill_20261117" / "fin_shares.parquet",
    ]
    rec = {}
    for p in files:
        if p.exists():
            rec[str(p.relative_to(ROOT)).replace("\\", "/")] = {
                "sha256": sha256(p), "bytes": p.stat().st_size}
    rec["dataset_partitions"] = len(list((DATA / "kline_daily_enriched").glob("date=*")))
    rec["dataset_last_date"] = sorted(
        p.name.split("=")[1] for p in (DATA / "kline_daily_enriched").glob("date=*"))[-1]
    json.dump(rec, open(OUT / "hashes.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rec, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="build", choices=["build", "hash", "enrich"])
    a = ap.parse_args()
    if a.stage == "build":
        build()
    elif a.stage == "enrich":
        rp = pd.read_parquet(RESEARCH, columns=["symbol", "date", "anchor", "industry", "log_mcap"])
        rp = rp[rp.anchor].drop(columns=["anchor"])
        rp["date"] = pd.to_datetime(rp["date"])
        s2 = asof_join(pd.read_parquet(OUT / "signals.parquet"), rp)
        s2 = s2.sort_values(["date", "symbol"]).reset_index(drop=True)
        s2.to_parquet(OUT / "signals_enriched.parquet")
        print("industry coverage", round(s2.industry.notna().mean(), 4),
              "mcap coverage", round(s2.log_mcap.notna().mean(), 4), "n", len(s2))
    else:
        hashes()
