"""错一天 = 多少 bp？—— 把 `churn_n_out`（中位 5/20）换成组合收益差。

## 它回答什么，以及为什么不用帧

`_diag_pool_freshness` 只说了「名单换了 5 只」，没说这 5 只值多少钱。要换成 bp
只有一条路：**同一入场日、两套名单，各算一次组合收益**。

    right : 决策日 = T-1  → 名单 picks[T-1]，限价 = close[T-1] × 0.99
    wrong : 决策日 = T-2  → 名单 picks[T-2]，限价 = close[T-2] × 0.99

两者共用同一个 T 日开盘（成交判断）与 T 日收盘（卖出，`i1 = close/open - 1`，
即 `_diag_l1_market_neutral` 里产出 121.49bp 的那个口径）。**不需要帧、不需要日历、
不需要新数据源**——L1 是纯日线产品。

## 口径钉死（上一版错在这里，留作反面）

- 收益是**当日日内** `close[T]/open[T]-1`，不是 `close[T+1]/open[T]`。后者是两日持仓，
  与 121.49bp 不可比（实测 55.27bp，差 2.2 倍 = 错了一个持有期）。
- `churn` 是**名单层** `20 - |∩|`，不是「成交票的差集」。后者分母是 8.7 只，
  必然报出 16 只（实测），与 `_diag_pool_freshness` 的 p50=5 不是同一个量。

## 三个口径必须同时报，缺一个就会读错

1. `diff = wrong − right`：**日历的 alpha 价值**（有正确 T-1 时能多赚多少）。
2. `wrong` 本身：**「什么都不做」的收益**。若它不显著为负，日历的 alpha 价值
   就小于它带来的停产成本。
3. `n_wrong_void`：wrong 因「成交不足 3 只」被排除的天数。这一天实盘是**买不到票**，
   不是**买错票**——两种后果的 bp 完全不同，不能并进 diff 的均值里。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort as L  # noqa: E402

DATA = ROOT / "data"
OUT = Path(__file__).with_name("_diag_churn_pnl_result.json")

GAP = L.L1_GAP_MAX
POOL_MIN = L.L1_POOL_MIN
FILL_MIN = L.L1_FILLED_POOL_MIN
MIN_NAMES = L.L1_MIN_NAMES


def _tstat(x: np.ndarray, rounds: int = 250) -> dict:
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 3:
        return {"n": n, "mean_bp": None, "t": None, "sd_bp": None}
    sd = float(np.std(x, ddof=1))
    mean = float(np.mean(x))
    se = sd / np.sqrt(n)
    t = None if se == 0 else round(mean / se, 2)
    return {
        "n": n,
        "mean_bp": round(mean * 1e4, 2),
        "sd_bp": round(sd * 1e4, 2),
        "t": t,
        "ci95_bp": [round((mean - 1.96 * se) * 1e4, 2),
                    round((mean + 1.96 * se) * 1e4, 2)],
        "ir_ann": None if sd == 0 else round(mean / sd * np.sqrt(rounds), 2),
    }


def _fill(m, tradable, names, s_map, entry_i: int, lim_base_i: int):
    """返回该名单在 `entry_i` 日开盘的成交下标；不满足池/成交门槛则给原因。"""
    if lim_base_i < 0 or entry_i >= m.n_days:
        return None, "no_base"
    c0 = m.close[lim_base_i]
    pool = tradable[entry_i] & np.isfinite(m.c2[entry_i]) & np.isfinite(m.turn[entry_i])
    if int(pool.sum()) < POOL_MIN:
        return None, "pool_small"
    lim = c0 * (1.0 - abs(GAP))
    fill = pool & (m.open[entry_i] <= lim) & np.isfinite(lim) & (lim > 0)
    if int(fill.sum()) < FILL_MIN:
        return None, "fill_small"
    sel = np.array([s_map[s] for s in names if s in s_map], dtype=np.int64)
    if sel.size == 0:
        return None, "no_names"
    good = sel[fill[sel]]
    if good.size < MIN_NAMES:
        return None, "few_names"
    return good, None


def run(require_next: bool) -> dict:
    df, m = L._panel(DATA)
    dates = m.dates
    picks = L._top_picks(df, top_n=L.L1_TOP_N, require_next=require_next)
    s_map = {s: i for i, s in enumerate(m.symbols)}
    tradable = (np.isfinite(m.close) & (m.close > 0)
                & np.isfinite(m.open) & (m.open > 0))
    ret = np.full_like(m.close, np.nan)
    ok = tradable & np.isfinite(m.close)
    ret[ok] = m.close[ok] / m.open[ok] - 1.0

    rows, voids = [], {"right": {}, "wrong": {}}
    for i0 in range(1, len(dates) - 1):
        entry_i = i0 + 1
        right = picks.get(dates[i0]) or []
        wrong = picks.get(dates[i0 - 1]) or []
        if not right or not wrong:
            continue
        gi_r, why_r = _fill(m, tradable, right, s_map, entry_i, i0)
        if gi_r is None:
            voids["right"][why_r] = voids["right"].get(why_r, 0) + 1
            continue
        gi_w, why_w = _fill(m, tradable, wrong, s_map, entry_i, i0 - 1)
        if gi_w is None:
            voids["wrong"][why_w] = voids["wrong"].get(why_w, 0) + 1
            continue
        # 2×2 分解：名单 ∈ {right, wrong} × 限价基准 ∈ {T-1, T-2}
        gi_c, why_c = _fill(m, tradable, right, s_map, entry_i, i0 - 1)
        gi_d, why_d = _fill(m, tradable, wrong, s_map, entry_i, i0)
        # 连缺两天（11-11 真实形态：面板最后一格是 11-06，T-1=11-10 与 T-2=11-09 都缺）
        lag0 = picks.get(dates[i0 - 2]) or [] if i0 >= 2 else []
        gi_e, _why_e = (None, "no_lag2") if not lag0 else _fill(
            m, tradable, lag0, s_map, entry_i, i0)
        if gi_c is None or gi_d is None:
            continue
        rr = ret[entry_i][gi_r]
        rw = ret[entry_i][gi_w]
        rc = ret[entry_i][gi_c]
        rd = ret[entry_i][gi_d]
        re = ret[entry_i][gi_e] if gi_e is not None else np.array([])
        rr, rw, rc, rd = (x[np.isfinite(x)] for x in (rr, rw, rc, rd))
        re = re[np.isfinite(re)] if re.size else re
        if min(rr.size, rw.size, rc.size, rd.size) < MIN_NAMES:
            continue
        rows.append({
            "entry": dates[entry_i],
            "r_right": float(rr.mean()), "r_wrong": float(rw.mean()),
            "diff": float(rw.mean() - rr.mean()),
            #: 纯限价效应：名单对、限价基准落后一天（= 帧也滞后）
            "r_lim_only": float(rc.mean()),
            #: 纯名单效应：限价对（帧给 T-1 收盘）、名单来自 T-2 = **P0 实盘形态**
            "r_name_only": float(rd.mean()),
            #: 连缺两天：名单来自 T-3、限价仍对（帧给 T-1 收盘）
            "r_lag2": float(re.mean()) if re.size >= MIN_NAMES else float("nan"),
            "n_right": int(rr.size), "n_wrong": int(rw.size),
            "n_name_only": int(rd.size),
            #: 名单层换手：20 只里换了几只（与 _diag_pool_freshness 同量）
            "churn": L.L1_TOP_N - len(set(right) & set(wrong)),
            #: 成交层：wrong 实际买到的票里有几只不在 right 名单里
            "wrong_only": int(sum(1 for j in gi_w
                                  if m.symbols[j] not in set(right))),
        })

    if not rows:
        return {"require_next": require_next, "n_days": 0, "voids": voids}

    d = pl.DataFrame(rows)
    r_r = d["r_right"].to_numpy()
    r_w = d["r_wrong"].to_numpy()
    dd = d["diff"].to_numpy()
    r_l = d["r_lim_only"].to_numpy()
    r_n = d["r_name_only"].to_numpy()
    a = _tstat(r_r); b = _tstat(r_w); c = _tstat(dd)
    lim_eff = _tstat(r_l - r_r)      # 纯限价效应
    name_eff = _tstat(r_n - r_r)     # 纯名单效应 = P0 实盘形态
    r2 = d["r_lag2"].cast(pl.Float64).to_numpy()
    lag2_eff = _tstat(r2 - r_r)      # 连缺两天（T-3 名单），限价仍对
    return {
        "require_next": require_next,
        "n_days": len(rows),
        "window": {"from": rows[0]["entry"], "to": rows[-1]["entry"]},
        "churn": {"mean": round(float(d["churn"].mean()), 2),
                  "p50": int(np.median(d["churn"].to_numpy())),
                  "max": int(d["churn"].max())},
        "fill_count": {"right_mean": round(float(d["n_right"].mean()), 2),
                       "wrong_mean": round(float(d["n_wrong"].mean()), 2),
                       "name_only_mean": round(float(d["n_name_only"].mean()), 2)},
        "wrong_only_mean": round(float(d["wrong_only"].mean()), 2),
        "right": a, "wrong": b, "diff_wrong_minus_right": c,
        "decompose": {
            "name_effect": name_eff,        # 名单错、限价对 → P0 实盘
            "lag2_name_effect": lag2_eff,   # 连缺两天、限价对（11-11 真实形态）
            "limit_effect": lim_eff,        # 名单对、限价错 → 帧也滞后
            "interaction": _tstat(r_w - r_l - r_n + r_r),
        },
        "annualized_pct": {
            "right": None if a["mean_bp"] is None else round(a["mean_bp"] * 250 / 1e4, 3),
            "wrong": None if b["mean_bp"] is None else round(b["mean_bp"] * 250 / 1e4, 3),
            "diff": None if c["mean_bp"] is None else round(c["mean_bp"] * 250 / 1e4, 3),
            "name_effect": None if name_eff["mean_bp"] is None
            else round(name_eff["mean_bp"] * 250 / 1e4, 3),
            "lag2_name_effect": None if lag2_eff["mean_bp"] is None
            else round(lag2_eff["mean_bp"] * 250 / 1e4, 3),
            "limit_effect": None if lim_eff["mean_bp"] is None
            else round(lim_eff["mean_bp"] * 250 / 1e4, 3),
        },
        "win_rate_diff": round(float((dd > 0).mean()), 4),
        "by_year": _by_year(rows),
        "voids": voids,
    }


def _by_year(rows: list[dict]) -> dict:
    """按入场年拆 name_effect —— 结论是「不值得修」，最该被这样攻击一次。"""
    buckets: dict[str, list[float]] = {}
    for r in rows:
        y = r["entry"][:4]
        d = r["r_name_only"] - r["r_right"]
        if np.isfinite(d):
            buckets.setdefault(y, []).append(d)
    return {y: _tstat(np.array(v)) for y, v in sorted(buckets.items())}


def main() -> int:
    res = {"generated_for": "错一天（T-2 名单）的组合收益后果",
           "caliber": {
               "ret": "当日日内 raw-equivalent：close[T]/open[T]-1（与 121.49bp 同口径）",
               "right": "决策日 T-1，限价基准 close[T-1]",
               "wrong": "决策日 T-2，限价基准 close[T-2]",
               "annualize": "日序列均值 × 250，非复利",
               "churn": "20 - |名单交集|（名单层），不是成交票差集",
               "voids": "right 未过池/成交门槛的天数；wrong 未过门槛 = 买不到票，不是买错票",
               "note": "不需帧/日历；L1 是纯日线产品。require_next 两套并列。",
           },
           "runs": [run(True), run(False)]}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
