"""EXP-LEG1-LATE-01：延迟进场诊断——「晚几天买同一个 ETF」是不是同一笔交易。

背景：用户真实资金锁在逆回购，11/20 机械信号无法在 11/23（t+1）执行，
直觉上「12/03 买入、反正也是同一个 ETF」与规则进场等价。本脚本用 42 个历史事件
直接量化这个直觉。

预注册（写于看结果之前）：
  A 规则：t0+1 收盘进场，持有 20 个交易日（t0+21 收盘退出）——预注册规则本身
  B 晚 8 日、原退出日：t0+9 收盘进场（12/03 的真实情形），仍在 t0+21 收盘退出 ⇒ 只持有 12 日
  C 晚 8 日、重新计 20 日：t0+9 进场，t0+29 收盘退出
  全部用当日 F6 载体收盘价；相对收益用全行业等权净值做基准；差值按月聚簇做 t。
  附：t0+8 时该行业的 status 分布（F1 深跌阻断 / 仍在区间 / 已修复出区间）——
  信号条件在 8 天后还剩多少。

这是诊断，不改变规则：资金锁定 ⇒ 放弃该笔（account-runbook.md 预注册）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402
import etf_watch_experiments as X  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

LATE = 8          # 11/23→12/03 相差 8 个交易日
HOLD = 20


def main() -> None:
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)
    mats = X.daily_matrices(m)
    status, dd, sym = mats["status"], mats["dd"], mats["symbol"]
    sessions = list(status.index)
    pos = {t: i for i, t in enumerate(sessions)}
    ev = X.build_events(status, dd)
    prices = X.price_frame(d, {str(s) for s in sym.stack().dropna().unique()})
    nav = etf_watch.build_context(d, idx, sessions=10 ** 6).nav
    bench = nav.mean(axis=1)

    rows = []
    stat_at_late: dict[str, int] = {}
    dd_at_late: list[float] = []
    for _, r in ev.iterrows():
        i0 = int(r["i"])
        try:
            sym_code = sym.at[r["date"], r["industry"]]
        except KeyError:
            sym_code = None
        sym_code = str(sym_code) if isinstance(sym_code, str) else None
        px = prices.get(sym_code) if sym_code else None
        if px is None or i0 + 1 + HOLD >= len(sessions):
            continue
        e_idx = i0 + 1                      # A 的进场（t0+1）
        l_idx = i0 + 1 + LATE               # B/C 的进场（t0+9）
        a_exit = e_idx + HOLD               # t0+21
        c_exit = l_idx + HOLD               # t0+29
        if c_exit >= len(sessions):
            continue
        pa = px.reindex(sessions)

        def ret(a: int, b: int) -> float:
            # 每个方案用自己的进场价做分母——之前误用 A 的进场价当统一分母，B 与 A 数值相同
            return float(pa.iloc[b]["close_adj"]) / float(pa.iloc[a]["close_adj"]) - 1

        def base(a: int, b: int) -> float:
            return float(bench.iloc[b]) / float(bench.iloc[a]) - 1

        rows.append({
            "ym": str(r["date"])[:7], "industry": r["industry"],
            "A": ret(e_idx, a_exit), "A_base": base(e_idx, a_exit),
            "B": ret(l_idx, a_exit), "B_base": base(l_idx, a_exit),
            "C": ret(l_idx, c_exit), "C_base": base(l_idx, c_exit),
        })
        # t0+8 时的信号状态
        late_t = sessions[min(l_idx, len(sessions) - 1)]
        st = str(status.at[late_t, r["industry"]])
        stat_at_late[st] = stat_at_late.get(st, 0) + 1
        dv = dd.at[late_t, r["industry"]]
        if np.isfinite(dv):
            dd_at_late.append(float(dv))

    x = pd.DataFrame(rows)

    print(f"样本 {len(x)} 事件 / {x['ym'].nunique()} 独立月（2018-2026 全历史）\n")
    print(f"{'方案':30s}{'月均bp':>8s}{'事件均值bp':>9s}{'胜率':>6s}{'p10':>8s}")
    for lab, col, bcol in (("A 规则：t+1 进场持 20 日", "A", "A_base"),
                           ("B 晚8日·原退出日（12/21）", "B", "B_base"),
                           ("C 晚8日·重新计 20 日（≈12/31）", "C", "C_base")):
        s = x[col].dropna()
        mo = s.groupby(x.loc[s.index, "ym"]).mean()
        rel = (x[col] - x[bcol]).dropna()
        rel_mo = rel.groupby(x.loc[rel.index, "ym"]).mean()
        t_rel = float(rel_mo.mean() / (rel_mo.std(ddof=1) / np.sqrt(len(rel_mo)))) if len(rel_mo) > 6 and rel_mo.std(ddof=1) > 0 else np.nan
        print(f"{lab:30s}{mo.mean()*1e4:+8.0f}{s.mean()*1e4:+9.0f}{(s > 0).mean()*100:5.0f}%{np.percentile(s, 10)*1e4:+8.0f}   相对基准 {rel_mo.mean()*1e4:+6.0f}bp (t={t_rel:+.2f}, {len(rel_mo)}月)")

    print("\n配对差（月聚簇，第一列 − 第二列）:")
    for c1, c2, lab in (("B", "A", "B − A（晚8日·原退出日 − 规则）"),
                        ("C", "A", "C − A（晚8日·重新计20日 − 规则）"),
                        ("C", "B", "C − B（晚8日下多持有 8 日）")):
        g = (x[c1] - x[c2]).dropna()
        mo = g.groupby(x.loc[g.index, "ym"]).mean()
        t = float(mo.mean() / (mo.std(ddof=1) / np.sqrt(len(mo)))) if len(mo) > 6 and mo.std(ddof=1) > 0 else np.nan
        print(f"  {lab:34s}{mo.mean()*1e4:+7.0f}bp  t={t:+.2f}  月胜率 {(mo > 0).mean()*100:.0f}%  ({len(mo)}月)")

    print(f"\nt0+8（≈12/03）时原信号行业的状态分布（{sum(stat_at_late.values())} 个事件）:")
    for k, v in sorted(stat_at_late.items(), key=lambda kv: -kv[1]):
        print(f"  {k:12s} {v:3d} ({v / sum(stat_at_late.values()) * 100:.0f}%)")
    dd_arr = np.array(dd_at_late)
    print(f"  dd 中位 {np.median(dd_arr):+.3f} · 已跌出 −50%（F1 阻断）{(dd_arr <= -0.50).mean()*100:.0f}%"
          f" · 已修复出区间（dd>−35%）{(dd_arr > -0.35).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
