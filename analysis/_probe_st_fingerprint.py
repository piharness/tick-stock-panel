"""否证记录：用「5% 涨跌幅指纹」重建历史 ST 面板 —— 判据不成立。

结论（2026-11-17 实测，不要重做）：
  主板 ST 股涨跌幅在 2001-2026.07 之间是 5%，看起来可以从日线里读出
  「哪一段是 ST」。在 2026-06 的横截面上用金标准（当前 ST 名单）评估：

      precision = 0.448    recall = 0.423      (主板 3162 个样本)

  也就是「判成 ST 的有一半不是、真的是 ST 的有一半没判出来」，不可用。

两条各自独立的失败原因：
  1. 2026-07-06 起沪深主板风险警示股涨跌幅由 5% 上调至 10%（与普通股一致），
     创业板/科创板 ST 一直是 20%、北交所 *ST 是 30% —— 指纹从这天起彻底消失；
  2. 即便在 5% 时代，本地前复权日线也不能逐日复现涨跌停幅度：600340 在
     2026-01-14 戴帽后仍出现 -10.09% / -10.20% / -10.23% 的连续一字跌停
     （volume>0、amount/volume 与 close 自洽，不是脏数据），说明这份日线的
     价格序列没有保留「当日涨跌幅上限」这一约束。

因此「ST 生命周期 / 摘帽率 / 退市率」无法用本地日线重建。产品侧改为
`services/delisting_guard.py`：只算当前这一天的规则距离（面值/市值/
净资产/净利润与营收），不给概率。

顺带否证的另一条路（同一份记录）：
  `data/research/symbol_lifespan.parquet` 只含 5579 只**在市**标的，
  instruments 里 296 个 ST 名字中有 100 只在库里没有任何行情（如 600823
  *ST世茂、000587 *ST金洲），名字含「退」的 155 只里只有 1 只有数据。
  ⇒ 死亡样本缺失，任何「从现存样本统计生存率」的做法都会系统性高估生存率。

本脚本保留可重跑的最小版本，用于将来换数据源后复评。
"""
from __future__ import annotations

import glob

import polars as pl

QFQ = "data/archive/kline_daily_qfq"
W = 120  # 回看窗口（交易日）


def _main_board(code: str) -> bool:
    return code.startswith(("60", "000", "001", "002", "003"))


def evaluate(as_of_month: str = "2026-06") -> dict:
    """在指定月份末，用当前 ST 名单做金标准评估指纹判据。"""
    y, m = map(int, as_of_month.split("-"))
    months = []
    yy, mm = y, m
    for _ in range(8):  # 回看 8 个月，足够覆盖 W=120 个交易日
        months.append(f"{QFQ}/month={yy:04d}-{mm:02d}")
        mm -= 1
        if mm == 0:
            yy, mm = yy - 1, 12
    parts = []
    for d in months:
        for p in glob.glob(d + "/*.parquet"):
            parts.append(pl.read_parquet(p))
    if not parts:
        return {"error": "no data"}
    df = pl.concat(parts, how="diagonal_relaxed").sort(["symbol", "date"])

    inst = pl.read_parquet("data/instruments/instruments.parquet")
    board = {r["symbol"]: _main_board(r["code"]) for r in inst.iter_rows(named=True)}
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
    last = last.with_columns(((pl.col("n_big") == 0) & (pl.col("n_hit5") >= 2)).alias("pred_st"))
    last = last.filter(pl.col("n_hit5").is_not_null())

    tp = fp = fn = tn = 0
    for row in last.to_dicts():
        if not board.get(row["symbol"], False):
            continue
        truth = row["symbol"] in st_now
        pred = bool(row["pred_st"])
        if truth and pred:
            tp += 1
        elif truth:
            fn += 1
        elif pred:
            fp += 1
        else:
            tn += 1
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(tp / (tp + fp), 3) if tp + fp else 0.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 0.0,
    }


if __name__ == "__main__":
    print(evaluate("2026-06"))
