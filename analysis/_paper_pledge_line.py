"""质押贴线多头：冻结规格的纸面回放（prereg-pledge-line-paper-2026-11-27）。

规则见 docs/structural-edge/prereg-pledge-line-paper.md。本文只做两件事：
1. 按冻结规格跑 design / holdout 的每笔与组合；
2. 输出可复核的 JSON + Markdown，不改页面、不接账户。

单一实现点：信号复用 _diag_chip_audit_fix.load() 的 drift_c / ratio_c，
不在这里重写 ratio 公式。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_audit_fix import load, nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
KLINE = ROOT / "data" / "kline_daily_enriched"
REPORTS = ROOT / "reports"


def month_stats(d: pd.DataFrame, col: str) -> dict:
    g = d.groupby("ym")[col].mean().dropna()
    if g.empty:
        return {"mean": None, "t": None, "months": 0, "positive": None}
    return {
        "mean_bp": round(float(g.mean() * 1e4), 1),
        "t": round(nw_t(g.to_numpy()), 2),
        "months": int(g.size),
        "positive": round(float((g > 0).mean()), 3),
    }


def load_prices(symbols: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "open", "close", "amount"],
        filter=(ds.field("date") >= pd.Timestamp("2019-06-01").date()) & ds.field("symbol").isin(symbols),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    O = px.pivot_table(index="date", columns="symbol", values="open").sort_index()
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    A = px.pivot_table(index="date", columns="symbol", values="amount").sort_index()
    return O, C, A


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--max-pos", type=int, default=60)
    ap.add_argument("--cost", type=float, default=0.005, help="双边总成本，默认 0.5%")
    ap.add_argument("--tag", default="full", help="输出文件后缀：full / holdout / design")
    args = ap.parse_args()

    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    ev = load()
    ev = ev[(ev["date"] >= start) & (ev["date"] <= end)].copy()

    ins = pd.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")[
        ["symbol", "name", "listing_date", "exchange"]
    ]
    ev = ev.merge(ins, on="symbol", how="left")
    ev["listing_date"] = pd.to_datetime(ev["listing_date"], errors="coerce")
    ev["is_st"] = ev["name"].fillna("").str.contains("ST", case=False)
    ev["is_bj"] = ev["symbol"].str.endswith(".BJ") | (ev["exchange"] == "BJ")
    ev["listed_days"] = (ev["date"] - ev["listing_date"]).dt.days

    symbols = ev["symbol"].unique().tolist()
    O, C, A = load_prices(symbols)
    A20 = A.rolling(20).mean()
    di = C.index
    pos = {s: i for i, s in enumerate(C.columns)}
    ev["row"] = di.searchsorted(ev["date"].values)
    ev["amount20"] = [
        A20.iat[r, pos[s]] if (s in pos and 0 <= r < len(di)) else np.nan
        for r, s in zip(ev["row"], ev["symbol"])
    ]

    # 全事件池（用于同日基准）：过滤前，保留 f60 有效
    pool = ev.dropna(subset=["f60", "ym"]).copy()
    by_date = pool.groupby("date")["f60"].agg(["sum", "count"])
    by_month = pool.groupby("ym")["f60"].mean()

    # 冻结过滤
    near = ev[
        (ev["ratio_c"] > 0.8)
        & (ev["drift_c"].between(-0.9, 3.0))
        & (~ev["is_st"])
        & (~ev["is_bj"])
        & (ev["listed_days"] >= 365)
        & (ev["amount20"] >= 15_000_000)
        & ev["f60"].notna()
    ].copy()

    # 同日同事件池基准（剔除自身；同日只有 1 个事件时用当月均值）
    def bench(row: pd.Series) -> float:
        s = by_date.loc[row["date"]]
        if int(s["count"]) > 1:
            return float((s["sum"] - row["f60"]) / (s["count"] - 1))
        return float(by_month.get(row["ym"], np.nan))

    near["bench_event"] = near.apply(bench, axis=1)
    near["net"] = near["f60"] - args.cost
    near["excess"] = near["net"] - near["bench_event"]
    near["hit"] = near["f60"] > 0
    near["hit_excess"] = near["excess"] > 0
    near["window"] = np.where(near["date"] < pd.Timestamp("2025-01-01"), "design", "holdout")
    near["year"] = pd.to_datetime(near["notice"]).dt.year

    out: dict = {
        "spec": "prereg-pledge-line-paper-2026-11-27",
        "asOf": str(pd.Timestamp.today().date()),
        "window": [str(start.date()), str(end.date())],
        "rules": {
            "signal": "ratio_c > 0.8; drift_c in [-0.9, 3.0]",
            "entry": "next trading day open",
            "exit": "entry + 60 trading days close",
            "universe": "non-ST (current name), non-BJ, listed>=365d, amount20>=15m",
            "portfolio": f"slot=capital/{args.max_pos}*0.95; max_pos={args.max_pos}; cash=0",
            "cost": args.cost,
        },
        "trades": int(len(near)),
        "by_window": {},
        "by_year": [],
        "portfolio": {},
        "capacity": {},
    }

    for w in ["design", "holdout"]:
        d = near[near["window"] == w]
        if d.empty:
            continue
        ms = month_stats(d, "excess")
        out["by_window"][w] = {
            "n": int(len(d)),
            "mean_gross_bp": round(float(d["f60"].mean() * 1e4), 1),
            "mean_net_bp": round(float(d["net"].mean() * 1e4), 1),
            "median_net_bp": round(float(d["net"].median() * 1e4), 1),
            "mean_excess_bp": ms["mean_bp"],
            "t_excess": ms["t"],
            "months": ms["months"],
            "positive_months": ms["positive"],
            "hit": round(float(d["hit"].mean()), 3),
            "hit_excess": round(float(d["hit_excess"].mean()), 3),
            "bench_mean_bp": round(float(d["bench_event"].mean() * 1e4), 1),
        }

    for y, g in near.groupby("year"):
        if len(g) < 20:
            continue
        out["by_year"].append({
            "year": int(y),
            "n": int(len(g)),
            "gross_bp": round(float(g["f60"].mean() * 1e4), 1),
            "net_bp": round(float(g["net"].mean() * 1e4), 1),
            "excess_bp": round(float(g["excess"].mean() * 1e4), 1),
            "hit": round(float(g["hit"].mean()), 3),
        })

    # ── 组合：固定 slot，满仓跳过，现金 0 ─────────────────────────────
    slot = args.capital / args.max_pos * 0.95
    trades = []
    for _, r in near.sort_values(["date", "symbol"]).iterrows():
        row = int(r["row"])
        sym = r["symbol"]
        j = pos.get(sym)
        if j is None or row >= len(di) or row + 60 >= len(di):
            continue
        entry = O.iat[row, j]
        exit_px = C.iat[row + 60, j]
        if not np.isfinite(entry) or entry <= 0 or not np.isfinite(exit_px):
            continue
        trades.append({
            "entry_row": row, "exit_row": row + 60, "symbol": sym,
            "entry": float(entry), "exit": float(exit_px),
            "shares": slot / float(entry),
            "amount20": float(r["amount20"]) if np.isfinite(r["amount20"]) else np.nan,
        })

    C_ff = C.ffill()
    cash = args.capital
    active: list[dict] = []
    eq_dates: list[pd.Timestamp] = []
    eq_vals: list[float] = []
    active_counts: list[int] = []
    skipped = 0
    max_active = 0
    invested_sum = 0.0
    entries: dict[int, list[dict]] = {}
    exits: dict[int, list[dict]] = {}
    for t in trades:
        entries.setdefault(t["entry_row"], []).append(t)
        exits.setdefault(t["exit_row"], []).append(t)

    all_rows = sorted(set(entries) | set(exits))
    if all_rows:
        for row in range(min(all_rows), max(all_rows) + 1):
            # 先买（开盘），再卖（收盘）：同日卖出的钱不能用于同日开盘买入。
            for t in entries.get(row, []):
                if len(active) >= args.max_pos or cash < slot * (1 + args.cost / 2):
                    skipped += 1
                    continue
                cash -= slot * (1 + args.cost / 2)
                active.append(t)
            for t in exits.get(row, []):
                if t not in active:
                    continue
                px = C_ff.iat[row, pos[t["symbol"]]]
                if not np.isfinite(px):
                    px = t["exit"]
                cash += t["shares"] * float(px) * (1 - args.cost / 2)
                active.remove(t)
            invested = sum(t["shares"] * float(C_ff.iat[row, pos[t["symbol"]]]) for t in active)
            eq = cash + invested
            eq_dates.append(di[row])
            eq_vals.append(eq)
            active_counts.append(len(active))
            max_active = max(max_active, len(active))
            invested_sum += invested / eq if eq > 0 else 0.0

    if eq_vals:
        eq = pd.Series(eq_vals, index=pd.DatetimeIndex(eq_dates))
        peak = eq.cummax()
        dd = (eq / peak - 1).min()
        years = max(len(eq) / 252, 1e-9)
        cagr = (eq.iloc[-1] / args.capital) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
        out["portfolio"] = {
            "capital": args.capital,
            "slot": round(slot, 2),
            "trades_taken": int(len(trades) - skipped),
            "skipped": int(skipped),
            "max_active": int(max_active),
            "avg_active": round(float(np.mean(active_counts)), 1),
            "total_return": round(float(eq.iloc[-1] / args.capital - 1), 4),
            "cagr": round(float(cagr), 4),
            "max_drawdown": round(float(dd), 4),
            "avg_exposure": round(float(invested_sum / max(len(eq_vals), 1)), 3),
            "equity_start": str(eq.index[0].date()),
            "equity_end": str(eq.index[-1].date()),
        }

    if "amount20" in near:
        a = near["amount20"].dropna()
        if len(a):
            out["capacity"] = {
                "amount20_median": round(float(a.median()), 0),
                "amount20_p10": round(float(a.quantile(0.1)), 0),
                "amount20_min": round(float(a.min()), 0),
                "slot_over_adv_median": round(float((slot / a).median()), 6),
                "capacity_1pct_median": round(float(a.median() * 0.01 * args.max_pos), 0),
                "capacity_1pct_p10": round(float(a.quantile(0.1) * 0.01 * args.max_pos), 0),
            }

    (OUT / f"paper_pledge_line_{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))

    # ── Markdown ─────────────────────────────────────────────────────
    lines = [
        "# 质押贴线多头纸面（冻结规格）",
        "",
        f"> 规格：`prereg-pledge-line-paper-2026-11-27` · 回放窗口 {start.date()} ~ {end.date()} · 数据截止 2026-08-24",
        "> 信号：`ratio_c > 0.8`；公告次日开盘买；持有 60 个交易日；扣双边 0.5%；最多 60 仓、满仓跳过。",
        "> **准 OOS**：规则在 2020-2026 全样本上选过；本表只作先验，裁决看 2027-06-30 前的真前瞻。",
        "",
        "## 每笔",
        "",
        "| 段 | n | 毛收益 | 净收益 | 中位净 | 同事件池超额 | t | 月数 | 正月份 | 胜率 | 超额胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for w in ["design", "holdout"]:
        d = out["by_window"].get(w)
        if not d:
            continue
        lines.append(
            f"| {w} | {d['n']} | {d['mean_gross_bp']:+.1f}bp | {d['mean_net_bp']:+.1f}bp | "
            f"{d['median_net_bp']:+.1f}bp | {d['mean_excess_bp']:+.1f}bp | {d['t_excess']:.2f} | "
            f"{d['months']} | {d['positive_months']*100:.0f}% | {d['hit']*100:.0f}% | {d['hit_excess']*100:.0f}% |"
        )
    lines += [
        "",
        "## 组合（固定 slot，现金 0）",
        "",
        "| 段 | 总收益 | 年化 | 最大回撤 | 平均暴露 | 平均/最大持仓 | 跳过 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    p = out.get("portfolio", {})
    if p:
        lines.append(
            f"| 全段 | {p['total_return']*100:+.1f}% | {p['cagr']*100:+.1f}% | {p['max_drawdown']*100:+.1f}% | "
            f"{p['avg_exposure']*100:.0f}% | {p['avg_active']:.1f}/{p['max_active']} | {p['skipped']} |"
        )
    lines += ["", "## 分年", "", "| 年 | n | 毛 | 净 | 超额 | 胜率 |", "|---|---:|---:|---:|---:|---:|"]
    for r in out["by_year"]:
        lines.append(f"| {r['year']} | {r['n']} | {r['gross_bp']:+.0f}bp | {r['net_bp']:+.0f}bp | {r['excess_bp']:+.0f}bp | {r['hit']*100:.0f}% |")
    if out.get("capacity"):
        c = out["capacity"]
        lines += [
            "",
            "## 容量",
            "",
            f"- 20 日均额：中位 {c['amount20_median']/1e4:.0f} 万 / p10 {c['amount20_p10']/1e4:.0f} 万 / 最小 {c['amount20_min']/1e4:.0f} 万",
            f"- 单 slot 占 ADV：中位 {c['slot_over_adv_median']*100:.4f}%",
            f"- 1% ADV 可承载资金：中位 {c['capacity_1pct_median']/1e4:.0f} 万 / p10 {c['capacity_1pct_p10']/1e4:.0f} 万",
        ]
    lines += [
        "",
        "## 判据对照（冻结）",
        "",
        "| 判据 | 阈值 | holdout 实测 | 结论 |",
        "|---|---|---|---|",
    ]
    h = out["by_window"].get("holdout", {})
    if h:
        main_ok = h["mean_excess_bp"] is not None and h["mean_excess_bp"] > 0 and h["t_excess"] >= 1.65
        pos_ok = h["positive_months"] is not None and h["positive_months"] >= 0.6
        lines.append(f"| 主判据 | 净超额>0 且 t≥1.65 | {h['mean_excess_bp']:+.1f}bp / t {h['t_excess']:.2f} | {'过' if main_ok else '不过'} |")
        lines.append(f"| 稳健 | 正月份≥60% | {h['positive_months']*100:.0f}% | {'过' if pos_ok else '不过'} |")
    lines += [
        "",
        "## 结论",
        "",
        "本表是**准 OOS**，不是部署依据。holdout 只提供先验；真前瞻从 2026-11-30 起，",
        "需先补 2026-09 之后的质押公告。评估日 2027-06-30，判据不变。",
    ]
    (REPORTS / f"pledge-line-paper-2026-11-27_{args.tag}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
