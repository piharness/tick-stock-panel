"""探针 4：5% 指纹判据的 precision / recall 验收（金标准 = 2026-06 的当前 ST 名单）。

判据（每只票、每个交易日，回看 W=120 个交易日）：
  n_big  = 期间 |r| > 5.5% 的天数
  n_hit5 = 期间 4.5% <= |r| <= 5.5% 的天数
  is_st  = (n_big == 0) and (n_hit5 >= 2)

金标准：
  正样本 = instruments 里名字含 ST、且在 2026-06 有行情的票
  负样本 = 主板、名字不含 ST、2026-06 有行情的票

用途：如果 precision/recall 不够，就不能用指纹重建历史；如果够，才能做面板。
"""
from __future__ import annotations

import glob
from datetime import date

import polars as pl

QFQ = "data/archive/kline_daily_qfq"
W = 120


def main_board(code: str) -> bool:
    return code.startswith(("60", "000", "001", "002", "003"))


def main() -> None:
    # 拉 2025-11 ~ 2026-06 的分区（足够覆盖 2026-06 末回看 120 日）
    months = []
    y, m = 2025, 11
    while (y, m) <= (2026, 6):
        months.append(f"{QFQ}/month={y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    parts = []
    for d in months:
        for p in glob.glob(d + "/*.parquet"):
            parts.append(pl.read_parquet(p))
    df = pl.concat(parts, how="diagonal_relaxed").sort(["symbol", "date"])

    inst = pl.read_parquet("data/instruments/instruments.parquet")
    board = {r["symbol"]: main_board(r["code"]) for r in inst.iter_rows(named=True)}
    st_now = set(inst.filter(pl.col("name").str.contains("ST"))["symbol"].to_list())

    df = df.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("r"))
    df = df.with_columns(pl.col("r").abs().alias("ar"))
    df = df.with_columns([
        (pl.col("ar") > 0.055).cast(pl.Int32).alias("big"),
        ((pl.col("ar") >= 0.045) & (pl.col("ar") <= 0.055)).cast(pl.Int32).alias("hit5"),
    ])
    df = df.with_columns([
        pl.col("big").rolling_sum(W, min_samples=60).over("symbol").alias("n_big"),
        pl.col("hit5").rolling_sum(W, min_samples=60).over("symbol").alias("n_hit5"),
    ])
    last = df.filter(pl.col("date") == pl.col("date").max().over("symbol"))
    last = last.with_columns(
        ((pl.col("n_big") == 0) & (pl.col("n_hit5") >= 2)).alias("pred_st")
    ).filter(pl.col("n_hit5").is_not_null())

    rows = last.to_dicts()
    tp = fp = fn = tn = 0
    false_pos = []
    false_neg = []
    for row in rows:
        sym = row["symbol"]
        if not board.get(sym, False):
            continue
        truth = sym in st_now
        pred = bool(row["pred_st"])
        if truth and pred:
            tp += 1
        elif truth and not pred:
            fn += 1
            false_neg.append((sym, inst.filter(pl.col("symbol") == sym)["name"][0], row["n_big"], row["n_hit5"]))
        elif not truth and pred:
            fp += 1
            false_pos.append((sym, round(row["n_hit5"] or 0), round(row["n_big"] or 0)))
        else:
            tn += 1
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    print(f"主板样本 tp={tp} fp={fp} fn={fn} tn={tn}  precision={prec:.3f} recall={rec:.3f}")
    print(f"误判为ST（fp）示例 {len(false_pos)}:", false_pos[:8])
    print(f"漏判（fn）示例 {len(false_neg)}:", false_neg[:8])


if __name__ == "__main__":
    main()
