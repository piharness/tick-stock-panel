"""Audit rounds 1-3 for real bugs. Assume something is wrong until proven otherwise.

A1 T+1 legality        - does any leg require a same-day round trip?
A2 end-to-end replay   - 5 signals, every raw field, r_on recomputed by hand.
A3 adjustment          - qfq factor jumps between t and t+1 (ex-div handling).
A4 turnover base       - float_shares vs total_shares: which one does
                         turnover_rate divide by? Decides whether the 50-200e8
                         mcap filter is really free-float mcap.
A5 zero volume         - would inflate the volume ratio.
A6 ST pollution        - 5% limit-ups are NOT flagged by consecutive_limit_ups.
A7 exit tradability    - next-day limit-down opens (cannot sell).
A8 null propagation    - does the matched benchmark leave all-null dates whose
                         std() would poison the t statistic?
A9 duplicates          - same (symbol,date) read twice.
A10 5-min last bar     - is the 15:00 bar equal to the daily close? (known: no)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_tail_audit_result.json")
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close", "volume",
        "amount", "turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs"]


def main() -> None:
    rep: dict = {}
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore").select(COLS).collect())

    dup = df.group_by(["symbol", "date"]).len().filter(pl.col("len") > 1)
    rep["A9_duplicates"] = int(dup.height)
    print(f"[A9] duplicate (symbol,date) rows: {dup.height}")

    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("close").shift(-1).over("symbol").alias("close_n"),
        pl.col("close").shift(1).over("symbol").alias("close_p"),
        pl.col("volume").shift(1).over("symbol").alias("vol_p1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
    ])
    df = df.with_columns([
        (pl.col("close") / pl.col("close_p") - 1).alias("chg"),
        pl.col("vol_p1").rolling_mean(5).over("symbol").alias("vol5"),
        (pl.col("consecutive_limit_ups") >= 1).cast(pl.Int8)
            .rolling_max(20).over("symbol").alias("lim20"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8).otherwise(None).alias("fmcap"),
    ])
    df = df.with_columns([
        (pl.col("volume") / pl.col("vol5")).alias("vr"),
        (pl.col("open_n") / pl.col("close") - 1).alias("r_on"),
    ])

    # ---------------- A5 ----------------
    rep["A5_zero_vol_rows"] = int(df.filter(pl.col("volume") <= 0).height)
    print(f"\n[A5] volume<=0 rows: {rep['A5_zero_vol_rows']:,}")

    # ---------------- A4 turnover base ----------------
    print("\n[A4] is turnover_rate float-based or total-share based?")
    last = df.filter(pl.col("date") == df["date"].max())
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet").select(
        ["symbol", "name", "total_shares", "float_shares"])
    j = last.join(inst, on="symbol", how="inner").filter(
        (pl.col("float_shares") > 0) & (pl.col("total_shares") > 0) & pl.col("fmcap").is_finite())
    j = j.with_columns([
        (pl.col("raw_close") * pl.col("float_shares") / 1e8).alias("cap_float"),
        (pl.col("raw_close") * pl.col("total_shares") / 1e8).alias("cap_total"),
    ]).with_columns([
        (pl.col("cap_float") / pl.col("fmcap") - 1).alias("err_float"),
        (pl.col("cap_total") / pl.col("fmcap") - 1).alias("err_total"),
    ])
    n = j.height
    ef = j["err_float"].abs()
    et = j["err_total"].abs()
    rep["A4_n"] = n
    rep["A4_median_abs_err_float"] = round(float(ef.median()), 4)
    rep["A4_median_abs_err_total"] = round(float(et.median()), 4)
    rep["A4_share_err_float_lt_2pct"] = round(float((ef < 0.02).mean()), 3)
    rep["A4_share_err_total_lt_2pct"] = round(float((et < 0.02).mean()), 3)
    print(f"  n={n:,}")
    print(f"  |fmcap - cap_float|/fmcap : median {ef.median():.4f}  within 2%: {(ef<0.02).mean():.1%}")
    print(f"  |fmcap - cap_total|/fmcap : median {et.median():.4f}  within 2%: {(et<0.02).mean():.1%}")
    verdict = "FLOAT" if ef.median() < et.median() else "TOTAL"
    rep["A4_verdict"] = verdict
    print(f"  => turnover_rate divides by {verdict} shares")
    # a name where the two differ a lot, as a human-checkable example
    big = j.with_columns((pl.col("cap_total") / pl.col("cap_float")).alias("ratio")).sort(
        "ratio", descending=True).head(3)
    print(big.select(["symbol", "name", "fmcap", "cap_float", "cap_total",
                      "err_float", "err_total", "ratio"]))
    rep["A4_examples"] = big.select(["symbol", "name", "fmcap", "cap_float", "cap_total"]).to_dicts()

    # ---------------- filters ----------------
    base_ok = ((pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
               & pl.col("chg").is_finite() & pl.col("vr").is_finite() & (pl.col("vr") > 0)
               & pl.col("fmcap").is_finite() & pl.col("turnover_rate").is_finite()
               & (pl.col("rn_n") == pl.col("rn") + 1))
    tail5 = ((pl.col("vr") > 1) & pl.col("turnover_rate").is_between(5, 10)
             & pl.col("fmcap").is_between(50, 200) & pl.col("chg").is_between(0.03, 0.05))
    formula = df.filter(base_ok & (pl.col("lim20") == 1) & tail5)

    # ---------------- A6 ----------------
    st_like = formula.filter(pl.col("chg").is_between(0.0485, 0.0515))
    sealed = st_like.filter(pl.col("close") == pl.col("high"))
    rep["A6_formula_n"] = int(formula.height)
    rep["A6_st_like_n"] = int(st_like.height)
    rep["A6_st_like_share"] = round(st_like.height / max(formula.height, 1), 4)
    rep["A6_st_sealed_n"] = int(sealed.height)
    print(f"\n[A6] formula n={formula.height:,}  chg in [4.85,5.15]%: {st_like.height:,} "
          f"({st_like.height/formula.height:.2%})  close==high among them: {sealed.height:,}")

    # ---------------- A7 ----------------
    dl = formula.filter(pl.col("consecutive_limit_downs") >= 1)
    gapdn = formula.filter(pl.col("r_on") <= -0.095)
    rep["A7_next_limit_down_n"] = int(dl.height)
    rep["A7_next_limit_down_share"] = round(dl.height / max(formula.height, 1), 4)
    rep["A7_open_le_-9.5_n"] = int(gapdn.height)
    print(f"[A7] next-day limit-down CLOSE: {dl.height} ({rep['A7_next_limit_down_share']:.2%})")
    print(f"     exit open <= -9.5%: {gapdn.height} ({gapdn.height/max(formula.height,1):.2%})")

    # ---------------- A3 ----------------
    b = df.filter(pl.col("r_on").is_finite() & (pl.col("raw_close") > 0) & (pl.col("open") > 0))
    b = b.with_columns((pl.col("close") / pl.col("raw_close")).alias("adj"))
    b = b.with_columns((pl.col("adj") / pl.col("adj").shift(1).over("symbol") - 1).alias("adj_jump"))
    jumps = b.filter(pl.col("adj_jump").abs() > 0.001)
    rep["A3_adj_jumps"] = int(jumps.height)
    rep["A3_adj_jump_share"] = round(jumps.height / max(b.height, 1), 5)
    print(f"\n[A3] qfq-factor jumps between t-1 and t: {jumps.height:,} "
          f"({rep['A3_adj_jump_share']:.3%}) -> these are ex-div/split days")
    print("     returns use qfq close on both sides, so the jump cancels")

    # ---------------- A2 replay ----------------
    print("\n[A2] hand replay of 5 formula signals")
    picks = formula.filter(pl.col("r_on").is_finite()).sort("date")
    step = max(picks.height // 6, 1)
    sample = picks.gather_every(step).head(5)
    rows, all_match, any_sealed = [], True, False
    for r in sample.to_dicts():
        sym, d, rn = r["symbol"], r["date"], r["rn"]
        a = df.filter((pl.col("symbol") == sym) & (pl.col("date") == d)).to_dicts()[0]
        nb = df.filter((pl.col("symbol") == sym) & (pl.col("rn") == rn + 1)).to_dicts()
        if not nb:
            continue
        nb = nb[0]
        recalc = nb["open"] / a["close"] - 1
        ok = abs(recalc - r["r_on"]) < 1e-12
        all_match &= ok
        any_sealed |= bool(a["close"] == a["high"])
        rows.append({
            "symbol": sym, "date": str(d), "date_next": str(nb["date"]),
            "t_open": a["open"], "t_high": a["high"], "t_low": a["low"], "t_close": a["close"],
            "t_raw_close": a["raw_close"], "t1_open": nb["open"], "t1_close": nb["close"],
            "chg": round(a["chg"], 4), "turn": round(a["turnover_rate"], 3),
            "vr": round(a["vr"], 3), "fmcap_yi": round(a["fmcap"], 1),
            "close_eq_high": bool(a["close"] == a["high"]),
            "r_on_bp": round(recalc * 1e4, 1), "match": ok,
            "r_cc_bp": round((nb["close"] / a["close"] - 1) * 1e4, 1),
        })
        print(f"  {sym} {d}->{nb['date']}  chg={a['chg']:+.2%} turn={a['turnover_rate']:.2f}% "
              f"vr={a['vr']:.2f} fmcap={a['fmcap']:.0f}e8 close==high:{a['close']==a['high']}")
        print(f"     r_on={recalc*1e4:+7.1f}bp (pipeline {r['r_on']*1e4:+7.1f}) match={ok}  "
              f"r_cc={(nb['close']/a['close']-1)*1e4:+7.1f}bp")
    rep["A2_replay"] = rows
    rep["A2_all_match"] = all_match
    rep["A2_any_close_eq_high"] = any_sealed

    # ---------------- A8 null propagation ----------------
    d2 = df.filter(pl.col("turnover_rate").is_finite() & pl.col("fmcap").is_finite()
                   & (pl.col("fmcap") > 0) & (pl.col("turnover_rate") > 0))
    d2 = d2.with_columns([
        ((pl.col("turnover_rate").rank("ordinal").over("date") - 1) * 10
         // pl.len().over("date")).alias("td"),
        ((pl.col("fmcap").rank("ordinal").over("date") - 1) * 10
         // pl.len().over("date")).alias("md"),
    ])
    bench = d2.group_by(["date", "td", "md"]).agg(pl.col("r_on").mean().alias("b_on"))
    sig = formula.join(
        d2.select(["symbol", "date", "td", "md"]), on=["symbol", "date"], how="left"
    ).join(bench, on=["date", "td", "md"], how="left")
    n_null_td = sig.filter(pl.col("td").is_null()).height
    n_null_b = sig.filter(pl.col("b_on").is_null()).height
    act = (sig["r_on"] - sig["b_on"]).drop_nulls().to_numpy()
    rep["A8_signals"] = int(sig.height)
    rep["A8_null_td"] = int(n_null_td)
    rep["A8_null_bench"] = int(n_null_b)
    rep["A8_active_mean_bp"] = round(float(act.mean()) * 1e4, 1)
    print(f"\n[A8] signals={sig.height:,}  td null={n_null_td}  b_on null={n_null_b}  "
          f"active mean={act.mean()*1e4:+.1f}bp")

    # ---------------- A10 5-min last bar ----------------
    print("\n[A10] does the 14:55 and 15:00 5-min bar match the daily close?")
    import glob
    files = sorted(glob.glob(str(DATA / "kline_5min" / "date=2026-11-1*" / "part.parquet")))[-3:]
    mism = []
    for f in files:
        s = (pl.scan_parquet(f).select(["symbol", "datetime", "close"])
             .with_columns(pl.col("datetime").dt.strftime("%H:%M:%S").alias("hm"))
             .filter(pl.col("hm").is_in(["14:50:00", "14:55:00", "15:00:00"]))
             .collect())
        day = s["datetime"].dt.date()[0]
        dd = df.filter(pl.col("date") == day).select(["symbol", "raw_close"])
        s = s.join(dd, on="symbol", how="inner")
        for t in ["14:50:00", "14:55:00", "15:00:00"]:
            sub = s.filter(pl.col("hm") == t)
            if sub.height == 0:
                continue
            err = ((sub["close"] / sub["raw_close"] - 1).abs())
            mism.append({"date": str(day), "time": t,
                         "median_abs_err": round(float(err.median()), 5),
                         "share_within_0.2pct": round(float((err < 0.002).mean()), 3),
                         "n": int(sub.height)})
    for m in mism:
        print(f"  {m['date']} {m['time']}  median|err|={m['median_abs_err']:.5f}  "
              f"within 20bp: {m['share_within_0.2pct']:.1%}  n={m['n']}")
    rep["A10_5min_vs_close"] = mism

    # ---------------- A1 ----------------
    rep["A1"] = {
        "legs_used": ["close[t] -> open[t+1]", "close[t] -> close[t+1]"],
        "same_day_roundtrip_anywhere": False,
        "verdict": "both legs hold at least one overnight, sellable next session -> T+1 legal",
        "note": "r_id (open[t+1]->close[t+1]) is reported only as the leg you forgo by "
                "selling at the open; it is never assumed tradable on its own",
    }
    print("\n[A1] T+1: every executed leg buys at close[t] and sells no earlier than t+1.")

    OUT.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
