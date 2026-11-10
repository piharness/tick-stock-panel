"""信号 → 产品：把一个 bp 级的统计超额，换算成组合年化能拿到多少。

回答的问题不是「有没有超额」，而是「即使有，值不值得建」。
三步：
  1) 触发率：全市场每股票日有多大概率命中该形态（分母 = 5581 只 × 366 个交易日）
  2) 单次效应：T 日收盘可知 → T+1 开盘买 → T+H 收盘卖，超额对「同日全市场等权」和
     「同日同涨幅箱」两种基准各算一遍（后者扣掉「异动 vs 不动」）
  3) 年化贡献 = 242 × 触发率 × 单次效应 − 242 × 触发率 × 往返成本
     并与组合年化波动（等权 ~25%）对比，得到信息比量级

所有 t 值均由「按交易日聚合后的序列」算出，不是按笔。
输出：analysis/_diag_signal_to_product_result.json
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import duckdb
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).with_name("_diag_signal_to_product_result.json")

TRADING_DAYS_PER_YEAR = 242
PORT_VOL = 0.25  # 等权全市场年化波动量级，用于把贡献换成信息比
COST_BP = {"低(仅佣金+印花税)": 12.0, "中(含开盘滑点10bp×2)": 32.0, "高(小票冲击)": 60.0}


def load_daily() -> pl.DataFrame:
    con = duckdb.connect()
    con.execute(
        "create view e as select * from read_parquet("
        "'data/kline_daily_enriched/date=*/*.parquet', hive_partitioning=1)"
    )
    df = con.sql(
        """
        select symbol, date, open, high, low, close, raw_high, raw_close,
               turnover_rate, consecutive_limit_ups
        from e where open is not null and close is not null and raw_close is not null
        order by symbol, date
        """
    ).pl()
    con.close()
    return df


def limit_pct_of(sym: str) -> float:
    code = sym.split(".")[0]
    if code[:3] in ("300", "301", "302", "688", "689"):
        return 0.20
    if code[:3] == "920":
        return 0.30
    return 0.10


def build(df: pl.DataFrame) -> pl.DataFrame:
    df = df.with_columns(
        pl.col("raw_close").shift(1).over("symbol").alias("prev_raw_close"),
        pl.col("date").shift(-1).over("symbol").alias("next_date"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
        pl.col("close").shift(-1).over("symbol").alias("close_1"),
        pl.col("close").shift(-2).over("symbol").alias("close_2"),
        pl.col("close").shift(-5).over("symbol").alias("close_5"),
    )
    cal = df["date"].unique().sort().to_list()
    nxt = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
    df = df.with_columns(
        pl.col("date").replace_strict(nxt, default=None).alias("cal_next")
    )
    # 只保留「下一行就是下一交易日」的观测，避免停牌把 shift 变成跨多日
    df = df.filter(pl.col("next_date") == pl.col("cal_next"))
    df = df.with_columns(
        pl.col("symbol").map_elements(limit_pct_of, return_dtype=pl.Float64).alias("lp")
    )
    df = df.with_columns(
        (pl.col("raw_close") / pl.col("prev_raw_close") - 1).alias("pct"),
        (pl.col("raw_high") / pl.col("prev_raw_close") - 1).alias("high_pct"),
        (pl.col("next_open") / pl.col("prev_raw_close") - 1).alias("next_open_pct"),
    )
    df = df.with_columns(
        (pl.col("next_open_pct") < pl.col("lp") - 0.005).alias("buyable")
    )
    for h, col in ((1, "close_1"), (2, "close_2"), (5, "close_5")):
        df = df.with_columns((pl.col(col) / pl.col("next_open") - 1).alias(f"r{h}"))
    return df


def add_excess(df: pl.DataFrame) -> pl.DataFrame:
    out = df
    for h in (1, 2, 5):
        mkt = df.group_by("date").agg(pl.col(f"r{h}").mean().alias(f"m{h}"))
        out = out.join(mkt, on="date", how="left")
        out = out.with_columns((pl.col(f"r{h}") - pl.col(f"m{h}")).alias(f"x{h}"))
        # 同涨幅箱基准：涨幅按 0.5% 分箱
        out = out.with_columns(
            (pl.col("pct") / 0.005).round().cast(pl.Int32).alias("bucket")
        )
        bm = out.group_by(["date", "bucket"]).agg(
            pl.col(f"r{h}").mean().alias(f"bm{h}"), pl.len().alias(f"bn{h}")
        )
        out = out.join(bm, on=["date", "bucket"], how="left")
        out = out.with_columns(
            pl.when(pl.col(f"bn{h}") >= 5)
            .then(pl.col(f"r{h}") - pl.col(f"bm{h}"))
            .otherwise(None)
            .alias(f"c{h}")
        )
    return out


def tstat_by_day(df: pl.DataFrame, cond: str, col: str, h: int) -> dict:
    sub = df.filter(pl.col(cond)).select(["date", col]).drop_nulls()
    if sub.height == 0:
        return {"n": 0}
    daily = sub.group_by("date").agg(pl.col(col).mean().alias("v"), pl.len().alias("k"))
    s = daily["v"]
    n = len(s)
    mean = s.mean()
    sd = s.std()
    neff = max(n / h, 1.0)
    se = sd / math.sqrt(neff)
    return {
        "n": int(sub.height),
        "days": n,
        "mean_bp": round(mean * 1e4, 1),
        "t": round(mean / se, 2) if se > 0 else None,
        "sd_daily_bp": round(sd * 1e4, 1),
        "n_eff": round(neff, 1),
        "mde_bp": round(2 * sd / math.sqrt(neff) * 1e4, 1),
    }


def main() -> None:
    raw = load_daily()
    df = add_excess(build(raw))
    total_days = df["date"].n_unique()
    total_obs = df.height

    # 触发条件（全部只用 T 日收盘可知信息）
    df = df.with_columns(
        [
            (pl.col("consecutive_limit_ups") >= 1).alias("t_limit"),
            (pl.col("consecutive_limit_ups") == 1).alias("t_first"),
            (
                (pl.col("high_pct") >= pl.col("lp") - 0.003)
                & (pl.col("consecutive_limit_ups") == 0)
            ).alias("t_zhaban"),
            (pl.col("turnover_rate") >= 25).alias("t_tr25"),
            (pl.col("turnover_rate") >= 27.7).alias("t_tr_top"),
        ]
    )

    triggers = {
        "涨停(含连板)": "t_limit",
        "首板": "t_first",
        "炸板(曾触板未封)": "t_zhaban",
        "换手≥25%": "t_tr25",
        "换手≥27.7%(top0.1%)": "t_tr_top",
    }

    res: dict = {
        "meta": {
            "days": total_days,
            "stock_days": total_obs,
            "symbols": df["symbol"].n_unique(),
            "first_date": str(df["date"].min()),
            "last_date": str(df["date"].max()),
        },
        "triggers": {},
        "buyable_share": round(n_buy / total_obs, 4) if (n_buy := 0) is not None else None,
    }

    df_buy = df.filter(pl.col("buyable"))
    n_buy = df_buy.height
    for name, cond in triggers.items():
        rate = df.filter(pl.col(cond)).height / total_obs
        row = {"rate": round(rate, 5), "per_day": round(rate * total_obs / total_days, 1)}
        for h in (1, 2, 5):
            row[f"mkt_H{h}"] = tstat_by_day(df_buy, cond, f"x{h}", h)
            row[f"bucket_H{h}"] = tstat_by_day(df_buy, cond, f"c{h}", h)
        res["triggers"][name] = row

    # 年化换算：用「同涨幅箱」口径（更保守），H2 与 H5 各算一遍
    conv = {}
    for name, row in res["triggers"].items():
        rate = row["rate"]
        hits_per_year = TRADING_DAYS_PER_YEAR * rate
        item = {"hits_per_stock_year": round(hits_per_year, 2)}
        for h in (2, 5):
            for basis in ("mkt", "bucket"):
                cell = row[f"{basis}_H{h}"]
                if not cell.get("n"):
                    continue
                eff_bp = abs(cell["mean_bp"])
                gross = hits_per_year * eff_bp / 1e4
                costs = {
                    k: hits_per_year * v / 1e4 for k, v in COST_BP.items()
                }
                item[f"{basis}_H{h}"] = {
                    "effect_bp": cell["mean_bp"],
                    "gross_annual": round(gross, 4),
                    "net_annual": {k: round(gross - c, 4) for k, c in costs.items()},
                    "ir_net": {k: round((gross - c) / PORT_VOL, 3) for k, c in costs.items()},
                }
        conv[name] = item
    res["annualised"] = conv

    # 全样本基线：什么条件都不加时，每天有多少比例的股票「值得卖」
    res["unconditional"] = {
        "daily_share_limit_up": round(
            df.filter(pl.col("t_limit")).height / total_obs, 4
        ),
        "mean_holdings_hit_per_year_if_50_names": round(
            50 * TRADING_DAYS_PER_YEAR * df.filter(pl.col("t_limit")).height / total_obs, 1
        ),
    }

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"样本 {total_days} 日 / {total_obs} 股票日 / {res['meta']['symbols']} 只\n")
    hdr = f"{'形态':<22}{'触发率':>8}{'日均只数':>9}{'H2箱超额':>10}{'t':>7}{'H5箱超额':>10}{'t':>7}{'MDE_H2':>8}"
    print(hdr)
    for name, row in res["triggers"].items():
        b2, b5 = row["bucket_H2"], row["bucket_H5"]
        print(
            f"{name:<22}{row['rate']*100:>7.2f}%{row['per_day']:>9.1f}"
            f"{b2.get('mean_bp','-'):>10}{b2.get('t','-'):>7}"
            f"{b5.get('mean_bp','-'):>10}{b5.get('t','-'):>7}{b2.get('mde_bp','-'):>8}"
        )
    print("\n年化换算（同涨幅箱口径，往返成本 12/32/60bp）:")
    for name, item in conv.items():
        for h in (2, 5):
            k = f"bucket_H{h}"
            if k not in item:
                continue
            c = item[k]
            print(
                f"{name:<22}H{h} 每年命中{item['hits_per_stock_year']:>5.2f}次 "
                f"单次{c['effect_bp']:>8.1f}bp -> 毛{100*c['gross_annual']:>6.2f}%/年 "
                f"净{ {kk: round(v*100,2) for kk,v in c['net_annual'].items()} }"
            )
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
