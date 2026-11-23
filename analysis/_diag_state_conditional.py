#!/usr/bin/env python3
"""_diag_state_conditional.py — 「空仓等横盘」这个策略，数据上成立吗？

用户命题（待检验）：
  「今年最好的选择是：没入场就空仓/逆回购，等市场进入横盘期再做波段」

它包含三个独立假设，任何一个为假，结论就不成立：
  H1 状态可判且可操作：用均线结构能把「下行/横盘/上行」分开，且不用未来信息
  H2 下行状态里持有是负期望 —— 否则空仓没有收益（只是少波动）
  H3 **横盘状态里做波段是正期望** —— 这是整个策略的落点，也是从未验证过的一环

另外两个必须算清的成本：
  C1 切换滞后：等「横盘」被确认时，市场已经涨了多少（错过多少）
  C2 机会成本：逆回购按年化 1.5% / 2.0% 两档，与错过反弹对照

## 口径（关键，否则结论会被误用）

- 状态用 **全市场等权指数**判定，不用上证指数。2026 年至今上证 −10.0%、全市场等权 **−26.8%**，
  用上证判断会把「散户实际承受的下跌」低估 17pct。
- 状态判定**滞后 1 个交易日**（用 t−1 收盘算出的状态，作用于 t 日），否则就是拿当天收盘判当天。
- 未来收益用等权指数本身（= 持有全市场的收益），不用上证。

输出：analysis/_diag_state_conditional_result.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kline_daily_enriched"
OUT = Path(__file__).with_name("_diag_state_conditional_result.json")
START = "2016-01-01"
COST = 0.0020


def load_panel() -> pl.DataFrame:
    files = []
    for e in os.scandir(DATA):
        if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= START:
            files.append(str(Path(e.path) / "part.parquet"))
    files.sort()
    return pl.read_parquet(
        files, columns=["symbol", "date", "open", "high", "low", "close", "amount", "turnover_rate"]
    ).filter((pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0))


def build_index(df: pl.DataFrame) -> pl.DataFrame:
    """全市场等权指数：每日横截面收益均值，累积。"""
    d = df.sort(["symbol", "date"]).with_columns(
        [
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("r"),
            (pl.col("date") - pl.col("date").shift(1).over("symbol")).dt.total_days().alias("g"),
        ]
    )
    d = d.with_columns(
        pl.when(pl.col("g").is_between(1, 10)).then(pl.col("r")).otherwise(None).alias("r")
    )
    daily = (
        d.group_by("date")
        .agg([pl.col("r").mean().alias("ew_ret"), pl.len().alias("n")])
        .sort("date")
    )
    daily = daily.with_columns((1.0 + pl.col("ew_ret")).cum_prod().alias("ew_idx"))
    return daily


def add_state(daily: pl.DataFrame) -> pl.DataFrame:
    """状态三分。所有均线/收益都用「当日及以前」，落到 t+1 才允许使用。"""
    d = daily.with_columns(
        [
            pl.col("ew_idx").rolling_mean(20).alias("ma20"),
            pl.col("ew_idx").rolling_mean(60).alias("ma60"),
            pl.col("ew_idx").pct_change(60).alias("ret60"),
        ]
    )
    d = d.with_columns(
        pl.when((pl.col("ew_idx") < pl.col("ma60")) & (pl.col("ma20") < pl.col("ma60")) & (pl.col("ret60") < -0.05))
        .then(pl.lit("down"))
        .when((pl.col("ew_idx") > pl.col("ma60")) & (pl.col("ma20") > pl.col("ma60")) & (pl.col("ret60") > 0.05))
        .then(pl.lit("up"))
        .otherwise(pl.lit("flat"))
        .alias("state")
    )
    # 未来收益（指数口径）
    for k in (1, 5, 20, 60):
        d = d.with_columns(pl.col("ew_idx").shift(-k).truediv(pl.col("ew_idx")).sub(1.0).alias(f"f{k}"))
    # 滞后 1 日用：t 日可用的状态 = t-1 的状态
    d = d.with_columns(pl.col("state").shift(1).alias("state_prev"))
    # 状态持续天数
    d = d.with_columns(
        (pl.col("state") != pl.col("state").shift(1)).cum_sum().alias("grp")
    )
    d = d.with_columns(pl.col("grp").len().over("grp").alias("state_len"))
    return d


def state_stats(d: pl.DataFrame) -> dict:
    out = {}
    for st in ("down", "flat", "up"):
        s = d.filter((pl.col("state_prev") == st) & pl.col("f20").is_not_null())
        if s.height < 50:
            continue

        def q(col: str, p: float) -> float:
            v = sorted(x for x in s[col].drop_nulls().to_list())
            if not v:
                return float("nan")
            return v[int(p * (len(v) - 1))] * 100

        out[st] = {
            "n_days": s.height,
            "f1_med": round(q("f1", 0.5), 2),
            "f20_med": round(q("f20", 0.5), 2),
            "f20_pos_pct": round(sum(1 for x in s["f20"].drop_nulls().to_list() if x > 0) / s.height * 100, 1),
            "f20_p10": round(q("f20", 0.10), 2),
            "f20_p90": round(q("f20", 0.90), 2),
            "f60_med": round(q("f60", 0.5), 2),
            "state_len_med": round(float(d.filter(pl.col("state") == st)["state_len"].median() or 0), 0),
        }
    return out


def transition_stats(d: pl.DataFrame) -> dict:
    """从 down 切出去的那一天，之后 20/60 日的收益 —— 即「等到确认再入场」的代价。"""
    d = d.with_columns(pl.col("state").shift(1).alias("state_prev"))
    switch = d.filter(
        (pl.col("state_prev") == "down") & (pl.col("state") != "down")
    )
    if switch.height == 0:
        return {}
    rows = []
    for r in switch.to_dicts():
        rows.append(
            {
                "date": str(r["date"]),
                "to": r["state"],
                "f20": None if r["f20"] is None else round(r["f20"] * 100, 2),
                "f60": None if r["f60"] is None else round(r["f60"] * 100, 2),
            }
        )
    f20 = [r["f20"] for r in rows if r["f20"] is not None]
    f60 = [r["f60"] for r in rows if r["f60"] is not None]
    return {
        "n_switches": len(rows),
        "f20_mean": round(sum(f20) / len(f20), 2) if f20 else None,
        "f20_med": round(sorted(f20)[len(f20) // 2], 2) if f20 else None,
        "f20_pos_pct": round(sum(1 for x in f20 if x > 0) / len(f20) * 100, 1) if f20 else None,
        "f60_mean": round(sum(f60) / len(f60), 2) if f60 else None,
        "f60_med": round(sorted(f60)[len(f60) // 2], 2) if f60 else None,
        "f60_pos_pct": round(sum(1 for x in f60 if x > 0) / len(f60) * 100, 1) if f60 else None,
        "detail": rows,
    }


def band_by_state(df: pl.DataFrame, daily: pl.DataFrame) -> dict:
    """蓝筹逢跌买入，按「事件日前一日的市场状态」分组。检验 H3。"""
    d = df.sort(["symbol", "date"]).with_columns(
        [
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"),
            (pl.col("date") - pl.col("date").shift(1).over("symbol")).dt.total_days().alias("g1"),
        ]
    )
    d = d.with_columns(
        pl.when(pl.col("g1").is_between(1, 10)).then(pl.col("ret1")).otherwise(None).alias("ret1")
    )
    d = d.with_columns([(pl.col("amount") / (pl.col("turnover_rate") / 100)).alias("fmcap")])
    d = d.with_columns(
        [
            (pl.col("amount").rank("ordinal").over("date") - 1).truediv(pl.len().over("date") - 1).alias("pa"),
            (pl.col("fmcap").rank("ordinal").over("date") - 1).truediv(pl.len().over("date") - 1).alias("pm"),
        ]
    )
    d = d.with_columns(((pl.col("pa") >= 0.90) & (pl.col("pm") >= 0.85)).alias("is_blue"))
    d = d.filter(pl.col("is_blue"))
    for k in (1, 5, 20):
        d = d.with_columns(
            [
                pl.col("open").shift(-1).over("symbol").alias("entry"),
                pl.col("close").shift(-k).over("symbol").alias(f"_c{k}"),
                (pl.col("date").shift(-k).over("symbol") - pl.col("date")).dt.total_days().alias(f"_g{k}"),
            ]
        )
        d = d.with_columns(
            pl.when(pl.col(f"_g{k}").is_between(k + 2, k + 22))
            .then(pl.col(f"_c{k}") / pl.col("entry") - 1.0)
            .otherwise(None)
            .alias(f"fwd{k}")
        ).drop([f"_c{k}", f"_g{k}"])
    st = daily.select(["date", pl.col("state_prev").alias("mkt_state")])
    d = d.join(st, on="date", how="left")
    out = {}
    for label, expr in [
        ("down 逢跌买", (pl.col("mkt_state") == "down") & (pl.col("ret1") <= -0.02)),
        ("flat 逢跌买", (pl.col("mkt_state") == "flat") & (pl.col("ret1") <= -0.02)),
        ("up 逢跌买", (pl.col("mkt_state") == "up") & (pl.col("ret1") <= -0.02)),
        ("flat 任意日买", (pl.col("mkt_state") == "flat") & (pl.col("ret1") > -0.02)),
        ("down 任意日买", (pl.col("mkt_state") == "down") & (pl.col("ret1") > -0.02)),
    ]:
        s = d.filter(expr)
        rec = {"n": s.height}
        for k in (1, 5, 20):
            col = s[f"fwd{k}"].drop_nulls()
            if col.len() < 200:
                rec[f"fwd{k}_bp"] = None
                continue
            v = col.to_list()
            m = sum(v) / len(v)
            rec[f"fwd{k}_bp"] = round(m * 1e4, 1)
            rec[f"fwd{k}_net_bp"] = round((m - COST) * 1e4, 1)
            rec[f"fwd{k}_win"] = round(sum(1 for x in v if x > 0) / len(v) * 100, 1)
        out[label] = rec
    return out


def state_by_year(d: pl.DataFrame) -> dict:
    """按年拆：全样本的 +0.40% 会被牛市稀释，2026 必须单独看。"""
    out: dict = {}
    for y in range(2016, 2027):
        sub = d.filter((pl.col("date").dt.year() == y) & pl.col("f20").is_not_null())
        if sub.height < 60:
            continue
        rec = {}
        for st in ("down", "flat", "up"):
            s = sub.filter(pl.col("state_prev") == st)
            v = sorted(x for x in s["f20"].drop_nulls().to_list())
            if len(v) < 20:
                rec[st] = None
                continue
            rec[st] = {
                "n": len(v),
                "med": round(v[len(v) // 2] * 100, 2),
                "pos": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
            }
        out[str(y)] = rec
    return out


def state_age_effect(d: pl.DataFrame) -> dict:
    """下行状态「已持续几天」是否决定后续收益 —— 直接回答「现在（第 14 天）该不该动」。"""
    d = d.with_columns(pl.col("state_len").shift(1).alias("age_prev"))
    s = d.filter((pl.col("state_prev") == "down") & pl.col("f20").is_not_null())
    bands = [(1, 10), (11, 20), (21, 30), (31, 999)]
    out = {}
    for lo, hi in bands:
        b = s.filter((pl.col("age_prev") >= lo) & (pl.col("age_prev") <= hi))
        v = sorted(x for x in b["f20"].drop_nulls().to_list())
        if len(v) < 20:
            continue
        out[f"{lo}-{hi if hi < 999 else '+'}d"] = {
            "n": len(v),
            "med_pct": round(v[len(v) // 2] * 100, 2),
            "pos_pct": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
            "mean_pct": round(sum(v) / len(v) * 100, 2),
        }
    return out


def main() -> None:
    df = load_panel()
    daily = build_index(df)
    daily = add_state(daily)
    last = daily.tail(1).to_dicts()[0]
    print(f"[idx] last={last['date']} ew_idx={last['ew_idx']:.2f} state={last['state']} "
          f"ma20={last['ma20']:.2f} ma60={last['ma60']:.2f} ret60={last['ret60']*100:.1f}%")

    # 2026 年内表现
    y26 = daily.filter(pl.col("date").dt.year() == 2026)
    y26_start = y26["ew_idx"][0]
    print(f"[2026] 等权指数 {y26_start:.2f} -> {y26['ew_idx'][-1]:.2f} = {(y26['ew_idx'][-1]/y26_start-1)*100:+.1f}%")

    st = state_stats(daily)
    print("\n[状态条件收益]（等权指数，状态滞后 1 日）")
    for k, v in st.items():
        print(f"  {k:5s} n={v['n_days']:4d} 未来20日中位 {v['f20_med']:+6.2f}%  正收益 {v['f20_pos_pct']:5.1f}%  "
              f"p10 {v['f20_p10']:+6.2f}%  p90 {v['f20_p90']:+6.2f}%  平均持续 {v['state_len_med']:.0f} 日")

    tr = transition_stats(daily)
    print(f"\n[down 切出] 共 {tr.get('n_switches')} 次：之后 20 日均值 {tr.get('f20_mean')}% 中位 {tr.get('f20_med')}% "
          f"正收益 {tr.get('f20_pos_pct')}%；60 日均值 {tr.get('f60_mean')}%")

    band = band_by_state(df, daily)
    print("\n[蓝筹逢跌买 × 市场状态]（次日 open 入，扣 20bp 前后的毛值）")
    for k, v in band.items():
        print(f"  {k:12s} n={v['n']:7d} fwd1={v.get('fwd1_bp')} fwd5={v.get('fwd5_bp')} "
              f"fwd20={v.get('fwd20_bp')} net20={v.get('fwd20_net_bp')} win20={v.get('fwd20_win')}")

    age = state_age_effect(daily)
    print("\n[下行状态 · 年龄效应] 未来 20 日")
    for k, v in age.items():
        print(f"  已持续 {k:6s} n={v['n']:4d} 中位 {v['med_pct']:+.2f}%  正收益 {v['pos_pct']:5.1f}%  均值 {v['mean_pct']:+.2f}%")

    byyear = state_by_year(daily)
    print("\n[状态 × 年] 未来 20 日中位收益 / 正收益比例")
    for y, rec in byyear.items():
        parts = []
        for st in ("down", "flat", "up"):
            r = rec.get(st)
            parts.append(f"{st}:{r['med']:+.1f}%/{r['pos']:.0f}%(n{r['n']})" if r else f"{st}:-")
        print(f"  {y}  " + "  ".join(parts))

    OUT.write_text(
        json.dumps(
            {
                "last": {"date": str(last["date"]), "state": last["state"],
                         "ew_idx": round(last["ew_idx"], 2), "ret60": round(last["ret60"] * 100, 2)},
                "y2026_ew": round((y26["ew_idx"][-1] / y26_start - 1) * 100, 1),
                "state_stats": st,
                "transition_from_down": {k: v for k, v in tr.items() if k != "detail"},
                "band_by_state": band,
                "state_age_effect": age,
                "ew_daily_tail": daily.tail(120).select(["date", "ew_idx", "state"]).to_dicts(),
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"[out] {OUT}")


if __name__ == "__main__":
    main()
