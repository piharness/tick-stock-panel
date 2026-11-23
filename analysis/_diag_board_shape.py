"""二板 / 三板涨停：可执行形状诊断.

问题不是「连板股第二天涨不涨」，而是「哪一种形状的钱能被拿到」。
日线只有收盘价，没有委托队列，所以把入场按可成交性强弱分成三层：

  E1 板上买（T 日收盘价=涨停价）→ T+1 开盘卖     需要排队 / 或当日开过板
  E2 T+1 开盘买 → T+1 收盘卖                     不需要排队，溢价已付
  E3 T+1 日内大跌 → T+2 开盘买 → T+2 收盘卖      不需要排队，等砸完

形状变量（T 日收盘即可知）：封板形态（一字 / T 字 / 盘中板）、量比、换手、
流通市值（成交额/换手率）、前置 20 日涨幅、开盘涨幅、市场连板宽度。

口径：
  - raw_* 判涨跌停；close/open（前复权）算收益；跨年边界用下一年前 30 个交易日补齐。
  - 剔除当前名称含 ST 的标的、上市 <90 自然日的次新（与 _diag_game_shift 一致）。
  - 超额 = 个股收益 − 同日全市场等权同口径收益。
  - t 值按「交易日聚类」：先按日取事件均值，再对日序列做 t 检验（事件扎堆时事件级 t 会虚高）。
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.price_limits import polars_limit_price, polars_price_limit_pct  # noqa: E402

DAILY = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "board_shape.json"
COLS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "raw_close",
    "raw_high",
    "raw_low",
    "turnover_rate",
    "consecutive_limit_ups",
]
YEARS = list(range(2010, 2027))
PAD = 30
COST = 0.0015  # 双边：佣金 5bp + 印花税 5bp + 滑点 5bp


def load_range(dates: list[str]) -> pl.DataFrame:
    frames = []
    for d in dates:
        p = DAILY / f"date={d}" / "part.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p, columns=COLS))
    return pl.concat(frames).sort(["symbol", "date"])


def prep(df: pl.DataFrame, names: dict, listing: dict) -> pl.DataFrame:
    df = df.with_columns(
        [
            pl.col("symbol").replace_strict(names, default="").alias("name"),
            pl.col("symbol").replace_strict(listing, default=None).alias("ld"),
        ]
    )
    df = df.filter(~pl.col("name").str.to_uppercase().str.contains("ST", literal=True))
    df = df.filter(
        pl.col("ld").is_null()
        | ((pl.col("date") - pl.col("ld").str.to_date()).dt.total_days() >= 90)
    )
    df = df.with_columns(
        [
            (pl.col("open") * pl.col("raw_close") / pl.col("close")).alias("raw_open"),
            pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"),
        ]
    )
    df = df.with_columns(
        polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.lit(False)).alias("lim_pct")
    )
    df = df.with_columns(
        polars_limit_price(pl.col("prev_raw"), pl.col("lim_pct"), up=True).alias("lim")
    )
    df = df.filter(pl.col("prev_raw").is_not_null() & (pl.col("prev_raw") > 0))
    df = df.with_columns(
        [
            (pl.col("raw_close") >= pl.col("lim") - 0.005).alias("lu"),
            (pl.col("raw_high") >= pl.col("lim") - 0.005).alias("touched"),
            (pl.col("raw_open") >= pl.col("lim") - 0.005).alias("open_at_limit"),
            (pl.col("raw_low") >= pl.col("lim") - 0.005).alias("low_at_limit"),
            (pl.col("raw_open") / pl.col("prev_raw") - 1).alias("gap_open"),
            (
                pl.col("amount")
                / pl.col("amount").rolling_mean(5).over("symbol").shift(1).over("symbol")
            ).alias("amt_ratio"),
            (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("prior20"),
            (
                pl.col("amount") / (pl.col("turnover_rate") / 100.0)
            ).alias("float_mcap"),
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret_t"),
        ]
    )
    # 自算连板数，用来校验 enriched 的 consecutive_limit_ups（两套定义必须对得上）
    df = df.with_columns((~pl.col("lu")).cum_sum().over("symbol").alias("grp"))
    df = df.with_columns(
        pl.when(pl.col("lu"))
        .then(pl.col("lu").cum_sum().over(["symbol", "grp"]))
        .otherwise(0)
        .alias("board_mine")
    )
    # 触板当天的「试图高度」：昨日连板数 + 1。炸板日 board_mine=0，必须用它才不会被漏掉
    df = df.with_columns(
        (pl.col("board_mine").shift(1).over("symbol").fill_null(0) + 1).alias("board_try")
    )
    # 收益（前复权）
    df = df.with_columns(
        [
            (pl.col("open").shift(-1).over("symbol") / pl.col("close") - 1).alias("r_gap1"),
            (
                pl.col("close").shift(-1).over("symbol")
                / pl.col("open").shift(-1).over("symbol")
                - 1
            ).alias("r_intra1"),
            (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1).alias("r_c1"),
            (
                pl.col("open").shift(-2).over("symbol")
                / pl.col("close").shift(-1).over("symbol")
                - 1
            ).alias("r_gap2"),
            (
                pl.col("close").shift(-2).over("symbol")
                / pl.col("open").shift(-2).over("symbol")
                - 1
            ).alias("r_intra2"),
            (pl.col("close").shift(-2).over("symbol") / pl.col("close") - 1).alias("r_c2"),
            (pl.col("close").shift(-3).over("symbol") / pl.col("close") - 1).alias("r_c3"),
            (pl.col("close").shift(-5).over("symbol") / pl.col("close") - 1).alias("r_c5"),
            (
                pl.col("close").shift(-2).over("symbol")
                / pl.col("open").shift(-1).over("symbol")
                - 1
            ).alias("r_o1c2"),
            (pl.col("low").shift(-1).over("symbol") / pl.col("open").shift(-1).over("symbol") - 1).alias("r_dd1"),
            (pl.col("low").shift(-2).over("symbol") / pl.col("open").shift(-2).over("symbol") - 1).alias("r_dd2"),
            # 以涨停价成交的口径：T 日买到涨停价 → T+1 开盘卖。
            # lim 是原始价，open(T+1) 是前复权价：必须把 lim 换算到同一复权基准，否则除权日会假跌。
            (
                pl.col("open").shift(-1).over("symbol")
                / (pl.col("lim") * pl.col("close") / pl.col("raw_close"))
                - 1
            ).alias("r_lim_gap1"),
            pl.col("lu").shift(-1).over("symbol").alias("next_lu"),
        ]
    )
    # 日线只能分出三档：一字（开盘=最低=涨停价，买不到）、T 字（开盘涨停后打开）、
    # 盘中板（开盘在涨停价下方）。日内「封死后有没有再开」在日线上不可观测。
    return df.with_columns(
        [
            pl.when(pl.col("open_at_limit") & pl.col("low_at_limit")).then(pl.lit("A 一字"))
            .when(pl.col("open_at_limit")).then(pl.lit("B T字"))
            .otherwise(pl.lit("C 盘中板"))
            .alias("seal"),
        ]
    )


def excess(df: pl.DataFrame) -> pl.DataFrame:
    cols = [
        "r_gap1",
        "r_intra1",
        "r_c1",
        "r_gap2",
        "r_intra2",
        "r_c2",
        "r_c3",
        "r_c5",
        "r_o1c2",
    ]
    mkt = df.group_by("date").agg([pl.col(c).mean().alias(f"m_{c}") for c in cols])
    df = df.join(mkt, on="date", how="left")
    df = df.with_columns([(pl.col(c) - pl.col(f"m_{c}")).alias(f"ex_{c}") for c in cols])
    # 涨停价入场的基准仍是「T 收盘 → T+1 开盘」的市场同口径，故减 m_r_gap1
    return df.with_columns(
        (pl.col("r_lim_gap1") - pl.col("m_r_gap1")).alias("ex_r_lim_gap1")
    )


def day_stats(sub: pl.DataFrame, cols: list[str]) -> dict:
    if sub.height == 0:
        return {"n": 0, "days": 0}
    daily = sub.group_by("date").agg([pl.col(c).mean().alias(c) for c in cols])
    out: dict = {"n": sub.height, "days": daily.height}
    for c in cols:
        v = daily[c].drop_nulls().to_numpy()
        ev = sub[c].drop_nulls().to_numpy()
        if len(v) < 2:
            out[c] = round(float(np.mean(ev)), 6) if len(ev) else None
            out[f"t_{c}"] = None
            continue
        out[c] = round(float(np.mean(ev)), 6)
        out[f"t_{c}"] = round(float(np.mean(v) / (np.std(v, ddof=1) / math.sqrt(len(v)))), 2)
        out[f"win_{c}"] = round(float(np.mean(ev > 0)), 4)
    return out


def qcuts(s: pl.Series, k: int) -> list[float]:
    v = s.drop_nulls().to_numpy()
    cuts = sorted({float(np.quantile(v, i / k)) for i in range(1, k)})
    return cuts


def add_bin(df: pl.DataFrame, col: str, name: str, cuts: list[float], prefix: str = "Q") -> pl.DataFrame:
    expr = pl.when(pl.col(col) <= cuts[0]).then(pl.lit(f"{prefix}1"))
    for i in range(1, len(cuts)):
        expr = expr.when(pl.col(col) <= cuts[i]).then(pl.lit(f"{prefix}{i + 1}"))
    expr = expr.otherwise(pl.lit(f"{prefix}{len(cuts) + 1}"))
    return df.with_columns(expr.alias(name))


def main() -> None:
    dates = sorted(d[5:] for d in os.listdir(DAILY) if d.startswith("date="))
    by_year: dict[str, list[str]] = {}
    for d in dates:
        by_year.setdefault(d[:4], []).append(d)

    inst = pl.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")
    names = dict(inst.select(["symbol", "name"]).iter_rows())
    listing = dict(inst.select(["symbol", "listing_date"]).iter_rows())

    events: list[pl.DataFrame] = []
    touched_events: list[pl.DataFrame] = []
    controls: list[dict] = []
    mismatch = {"rows": 0, "lu_rows": 0, "bad": 0}

    for y in YEARS:
        ds = by_year.get(str(y), [])
        if not ds:
            continue
        nxt = by_year.get(str(y + 1), [])[:PAD]
        df = prep(load_range(ds + nxt), names, listing)
        df = excess(df)
        cur = df.filter(pl.col("date").dt.year() == y)
        mismatch["rows"] += cur.height
        lum = cur.filter(pl.col("lu"))
        mismatch["lu_rows"] += lum.height
        mismatch["bad"] += lum.filter(
            pl.col("consecutive_limit_ups").fill_null(0) != pl.col("board_mine")
        ).height
        keep = [
            "date",
            "symbol",
            "seal",
            "board_mine",
            "consecutive_limit_ups",
            "gap_open",
            "amt_ratio",
            "turnover_rate",
            "float_mcap",
            "prior20",
            "next_lu",
            "ex_r_gap1",
            "ex_r_intra1",
            "ex_r_c1",
            "ex_r_gap2",
            "ex_r_intra2",
            "ex_r_c2",
            "ex_r_c3",
            "ex_r_c5",
            "ex_r_o1c2",
            "r_gap1",
            "r_intra1",
            "r_dd1",
            "r_dd2",
        ]
        events.append(lum.select(keep).with_columns(pl.lit(y).alias("year")))
        tou = cur.filter(pl.col("touched"))
        touched_events.append(
            tou.select(
                [
                    "date",
                    "symbol",
                    "board_mine",
                    "board_try",
                    "seal",
                    "lu",
                    "open_at_limit",
                    "ex_r_lim_gap1",
                    "ex_r_gap1",
                ]
            ).with_columns(pl.lit(y).alias("year"))
        )
        # 同日全市场的「低开 / 日内大跌」对照：验证 2/3 板的回抽是不是连板特有
        for kind, cond, col in (
            ("gap", (pl.col("r_gap1") < -0.03) & ~pl.col("lu"), "ex_r_intra1"),
            ("intra", (pl.col("r_intra1") <= -0.05) & ~pl.col("lu"), "ex_r_intra2"),
        ):
            d = day_stats(cur.filter(cond), [col])
            d.update({"kind": kind, "year": y})
            controls.append(d)
        print(y, "events", lum.height, flush=True)

    ev = pl.concat(events)
    ev = ev.filter(pl.col("board_mine").is_between(1, 6))
    ev.write_parquet(OUT.parent / "board_events.parquet")

    # ---------- 1. 板数 × 收益段 ----------
    rows = []
    eras = {
        "2010-2014": (2010, 2014),
        "2015-2019": (2015, 2019),
        "2020-2026": (2020, 2026),
    }
    for label, (a, b) in eras.items():
        sub = ev.filter(pl.col("year").is_between(a, b))
        for board, tag in ((1, "1板"), (2, "2板"), (3, "3板"), (4, "4板")):
            s = sub.filter(pl.col("board_mine") == board) if board < 4 else sub.filter(
                pl.col("board_mine") >= 4
            )
            d = day_stats(
                s,
                ["ex_r_gap1", "ex_r_intra1", "ex_r_c1", "ex_r_gap2", "ex_r_intra2", "ex_r_c3", "ex_r_c5"],
            )
            d.update({"era": label, "board": tag, "promote": round(float(s["next_lu"].mean()), 4) if s.height else None})
            rows.append(d)

    # ---------- 2. 封板形态 ----------
    def pick(df: pl.DataFrame, b: int) -> pl.DataFrame:
        return df.filter(pl.col("board_mine") == b) if b < 4 else df.filter(pl.col("board_mine") >= 4)

    seal_rows = []
    for board, tag in ((2, "2板"), (3, "3板"), (4, "4板+")):
        for era, (a, b) in eras.items():
            sub = pick(ev, board).filter(pl.col("year").is_between(a, b))
            for seal in ["A 一字", "B T字", "C 盘中板"]:
                s = sub.filter(pl.col("seal") == seal)
                d = day_stats(s, ["ex_r_gap1", "ex_r_intra1", "ex_r_c1", "ex_r_intra2"])
                d.update(
                    {
                        "board": tag,
                        "era": era,
                        "seal": seal,
                        "share": round(s.height / sub.height, 4) if sub.height else None,
                        "promote": round(float(s["next_lu"].mean()), 4) if s.height else None,
                        "raw_gap": round(float(s["r_gap1"].mean()), 6) if s.height else None,
                    }
                )
                seal_rows.append(d)

    # ---------- 2b. 以涨停价成交的真实人口：触板样本，含炸板 ----------
    tou = (
        pl.concat(touched_events)
        .with_columns(pl.col("board_try").alias("board_mine"))
        .filter(pl.col("board_mine").is_between(2, 4))
    )
    tou = tou.with_columns(
        pl.when(pl.col("lu")).then(pl.col("seal")).otherwise(pl.lit("D 炸板")).alias("kind"),
        pl.when(pl.col("seal").is_in(["A 一字", "B T字"]))
        .then(pl.lit("开盘即涨停"))
        .otherwise(pl.lit("盘中触板"))
        .alias("entry"),
    )
    tou.write_parquet(OUT.parent / "board_touches.parquet")
    fill_rows = []
    for board, tag in ((2, "2板"), (3, "3板"), (4, "4板+")):
        sub = pick(tou, board)
        for era, (a, b) in eras.items():
            s2 = sub.filter(pl.col("year").is_between(a, b))
            d = day_stats(s2, ["ex_r_lim_gap1"])
            d.update({"board": tag, "kind": "全部触板", "era": era, "share": 1.0})
            fill_rows.append(d)
        d = day_stats(sub, ["ex_r_lim_gap1"])
        d.update({"board": tag, "kind": "全部触板", "era": "全样本", "share": 1.0})
        fill_rows.append(d)
        for era, (a, b) in eras.items():
            s2 = sub.filter(pl.col("year").is_between(a, b) & (pl.col("seal") != "A 一字"))
            d = day_stats(s2, ["ex_r_lim_gap1"])
            d.update({"board": tag, "kind": "可成交(剔一字)", "era": era, "share": 1.0})
            fill_rows.append(d)
        s2 = sub.filter(pl.col("seal") != "A 一字")
        d = day_stats(s2, ["ex_r_lim_gap1"])
        d.update({"board": tag, "kind": "可成交(剔一字)", "era": "全样本", "share": 1.0})
        fill_rows.append(d)
        for kind in ["A 一字", "B T字", "C 盘中板", "D 炸板"]:
            g = sub.filter(pl.col("kind") == kind)
            d = day_stats(g, ["ex_r_lim_gap1"])
            d.update(
                {
                    "board": tag,
                    "kind": kind,
                    "share": round(g.height / sub.height, 4) if sub.height else None,
                }
            )
            fill_rows.append(d)
        for entry in ["开盘即涨停", "盘中触板"]:
            g = sub.filter(pl.col("entry") == entry)
            d = day_stats(g, ["ex_r_lim_gap1"])
            d.update(
                {
                    "board": tag,
                    "kind": f"按入场:{entry}",
                    "share": round(g.height / sub.height, 4) if sub.height else None,
                }
            )
            fill_rows.append(d)

    # ---------- 3. 盘中板内的形状分箱（只留 C，避免一字板混入） ----------
    bins = []
    tradable = ev.filter(pl.col("seal").is_in(["B T字", "C 盘中板"]))
    for board, tag in ((2, "2板"), (3, "3板")):
        sub = ev.filter((pl.col("seal") == "C 盘中板") & (pl.col("board_mine") == board))
        for var, label in (
            ("turnover_rate", "换手率%"),
            ("amt_ratio", "量比"),
            ("float_mcap", "流通市值"),
            ("prior20", "前20日涨幅"),
            ("gap_open", "当日开盘涨幅"),
        ):
            work = sub.filter(pl.col(var).is_not_null())
            if work.height < 200:
                continue
            cuts = qcuts(work[var], 5)
            work = add_bin(work, var, "bin", cuts)
            for (bname,), g in work.group_by(["bin"], maintain_order=True):
                d = day_stats(g, ["ex_r_gap1", "ex_r_c1"])
                d.update({"board": tag, "var": label, "bin": bname, "cuts": cuts})
                bins.append(d)
    bins.sort(key=lambda r: (r["board"], r["var"], r["bin"]))

    # ---------- 3a. 混淆检查：低量比的高收益是不是「买不到的一字板」 ----------
    confound = []
    for board, tag in ((2, "2板"), (3, "3板")):
        sub = pick(ev, board).filter(pl.col("amt_ratio").is_not_null())
        cuts = qcuts(sub["amt_ratio"], 5)
        sub = add_bin(sub, "amt_ratio", "bin", cuts)
        for (bname,), g in sub.group_by(["bin"], maintain_order=True):
            d = {
                "board": tag,
                "bin": bname,
                "cuts": cuts,
                "n": g.height,
                "yizi_share": round(float((g["seal"] == "A 一字").mean()), 4),
                "gap": round(float(g["ex_r_gap1"].mean()), 6),
            }
            confound.append(d)
    confound.sort(key=lambda r: (r["board"], r["bin"]))

    # ---------- 3b. 年度稳定性：可成交 2/3 板 ----------
    yearly = []
    for board, tag in ((2, "2板"), (3, "3板")):
        s = tradable.filter(pl.col("board_mine") == board)
        for (y,), g in s.group_by(["year"], maintain_order=True):
            d = day_stats(g, ["ex_r_gap1"])
            d.update({"board": tag, "year": int(y)})
            yearly.append(d)
    yearly.sort(key=lambda r: (r["board"], r["year"]))

    # ---------- 3c. 样本外：2010-2019 定分位 → 2020-2026 检验 ----------
    oos = []
    for board, tag in ((2, "2板"), (3, "3板")):
        s = tradable.filter(pl.col("board_mine") == board)
        train = s.filter(pl.col("year") <= 2019)
        test = s.filter(pl.col("year") >= 2020)
        for var, label in (
            ("turnover_rate", "换手率%"),
            ("amt_ratio", "量比"),
            ("float_mcap", "流通市值"),
            ("prior20", "前20日涨幅"),
            ("gap_open", "当日开盘涨幅"),
        ):
            tr = train.filter(pl.col(var).is_not_null())
            te = test.filter(pl.col(var).is_not_null())
            if tr.height < 200 or te.height < 100:
                continue
            cuts = qcuts(tr[var], 5)
            te = add_bin(te, var, "bin", cuts)
            for bname in ("Q1", f"Q{len(cuts) + 1}"):
                g = te.filter(pl.col("bin") == bname)
                d = day_stats(g, ["ex_r_gap1"])
                d.update({"board": tag, "var": label, "bin": bname, "cuts": cuts})
                oos.append(d)

    # ---------- 3d. 两条「不需要排队」的入场形状，逐年检验 ----------
    yearly_e2 = []
    for board, tag in ((2, "2板"), (3, "3板")):
        s = ev.filter((pl.col("board_mine") == board) & (pl.col("r_gap1") < -0.03))
        for (y,), g in s.group_by(["year"], maintain_order=True):
            d = day_stats(g, ["ex_r_intra1"])
            d.update({"board": tag, "year": int(y)})
            yearly_e2.append(d)
    yearly_e2.sort(key=lambda r: (r["board"], r["year"]))

    yearly_e3 = []
    for board, tag in ((1, "1板"), (2, "2板"), (3, "3板")):
        s = ev.filter((pl.col("board_mine") == board) & (pl.col("r_intra1") <= -0.05))
        for (y,), g in s.group_by(["year"], maintain_order=True):
            d = day_stats(g, ["ex_r_intra2"])
            d.update({"board": tag, "year": int(y)})
            yearly_e3.append(d)
    yearly_e3.sort(key=lambda r: (r["board"], r["year"]))

    # ---------- 4. E2：T+1 开盘买 ----------
    e2 = []
    for board, tag in ((2, "2板"), (3, "3板")):
        for era, (a, b) in eras.items():
            s = ev.filter((pl.col("board_mine") == board) & pl.col("year").is_between(a, b))
            d = day_stats(s, ["ex_r_intra1", "ex_r_o1c2"])
            d.update({"board": tag, "era": era})
            e2.append(d)
        # 按 T+1 开盘涨幅分档（T+1 开盘时可知，用未减基准的 raw gap）
        s = ev.filter(pl.col("board_mine") == board)
        for lo, hi, name in ((-1.0, -0.03, "低开<-3%"), (-0.03, 0.0, "-3~0%"), (0.0, 0.03, "0~3%"), (0.03, 9.0, ">3%")):
            g = s.filter((pl.col("r_gap1") >= lo) & (pl.col("r_gap1") < hi))
            d = day_stats(g, ["ex_r_intra1"])
            d.update({"board": tag, "gap_bin": name})
            e2.append(d)

    # ---------- 5. E3：T+1 日内大跌后的 T+2 回抽 ----------
    e3 = []
    for board, tag in ((1, "1板"), (2, "2板"), (3, "3板")):
        s = ev.filter(pl.col("board_mine") == board)
        for thr in (-0.03, -0.05, -0.07):
            g = s.filter(pl.col("r_intra1") <= thr)
            d = day_stats(g, ["ex_r_intra2"])
            d.update({"board": tag, "threshold": thr})
            e3.append(d)

    out = {
        "meta": {
            "years": "2010-2026",
            "cost_roundtrip": COST,
            "events": ev.height,
            "trading_days": ev["date"].n_unique(),
            "board_mismatch": mismatch,
            "note": "ex_* 为同日全市场等权超额；t 值按交易日聚类",
        },
        "board_term": rows,
        "seal": seal_rows,
        "fill": fill_rows,
        "bins": bins,
        "confound": confound,
        "yearly": yearly,
        "yearly_e2": yearly_e2,
        "yearly_e3": yearly_e3,
        "controls": controls,
        "oos": oos,
        "e2": e2,
        "e3": e3,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 打印 ----------
    print("\n== 板数 × 收益段（超额，%） ==")
    print(f"{'era':10} {'board':5} {'n':>6} {'gap1':>8} {'intra1':>8} {'c1':>8} {'intra2':>8} {'c3':>8} {'c5':>8} {'promote':>8}")
    for r in rows:
        print(
            f"{r['era']:10} {r['board']:5} {r['n']:6} "
            f"{100 * (r.get('ex_r_gap1') or 0):8.2f} {100 * (r.get('ex_r_intra1') or 0):8.2f} "
            f"{100 * (r.get('ex_r_c1') or 0):8.2f} {100 * (r.get('ex_r_intra2') or 0):8.2f} "
            f"{100 * (r.get('ex_r_c3') or 0):8.2f} {100 * (r.get('ex_r_c5') or 0):8.2f} "
            f"{100 * (r.get('promote') or 0):8.1f}"
        )
    print("\n== 封板形态（2/3 板） ==")
    for r in seal_rows:
        print(
            f"{r['board']} {r['era']:10} {r['seal']:8} n={r['n']:6} share={100 * (r['share'] or 0):5.1f}% "
            f"gap={100 * (r.get('ex_r_gap1') or 0):6.2f} (t={r.get('t_ex_r_gap1')}) "
            f"c1={100 * (r.get('ex_r_c1') or 0):6.2f} promote={100 * (r.get('promote') or 0):5.1f}%"
        )
    print("\n== 可成交子集分箱（gap1 超额，%） ==")
    for r in bins:
        print(
            f"{r['board']} {r['var']:8} {r['bin']:3} n={r['n']:5} gap={100 * (r.get('ex_r_gap1') or 0):6.2f} "
            f"t={r.get('t_ex_r_gap1')} c1={100 * (r.get('ex_r_c1') or 0):6.2f} cuts={[round(c, 3) for c in r['cuts']]}"
        )
    print("\n== 以涨停价成交（含炸板）gap1 超额（%） ==")
    for r in fill_rows:
        print(
            f"{r['board']} {r['kind']:16} n={r['n']:6} share={100 * (r['share'] or 0):5.1f}% "
            f"ex={100 * (r.get('ex_r_lim_gap1') or 0):7.2f} t={r.get('t_ex_r_lim_gap1')} "
            f"win={r.get('win_ex_r_lim_gap1')}"
        )
    print("\n== 混淆检查：按量比分位的一字占比 ==")
    for r in confound:
        print(
            f"{r['board']} {r['bin']:3} n={r['n']:5} yizi={100 * r['yizi_share']:5.1f}% "
            f"gap={100 * r['gap']:6.2f} cuts={[round(c, 2) for c in r['cuts']]}"
        )
    print("\n== 年度：可成交 2/3 板 gap1 超额（%） ==")
    for r in yearly:
        print(
            f"{r['board']} {r['year']} n={r['n']:4} gap={100 * (r.get('ex_r_gap1') or 0):6.2f} "
            f"t={r.get('t_ex_r_gap1')}"
        )
    print("\n== 样本外（2010-19 定分位，2020-26 检验） gap1 超额（%） ==")
    for r in oos:
        print(
            f"{r['board']} {r['var']:8} {r['bin']:3} n={r['n']:5} gap={100 * (r.get('ex_r_gap1') or 0):6.2f} "
            f"t={r.get('t_ex_r_gap1')}"
        )
    print("\n== E2 逐年：T+1 低开<-3% 买开盘、卖收盘（超额，%） ==")
    for r in yearly_e2:
        print(
            f"{r['board']} {r['year']} n={r['n']:4} intra1={100 * (r.get('ex_r_intra1') or 0):6.2f} "
            f"t={r.get('t_ex_r_intra1')} win={r.get('win_ex_r_intra1')}"
        )
    print("\n== E3 逐年：T+1 日内跌>5% → T+2 开盘买收盘卖（超额，%） ==")
    for r in yearly_e3:
        print(
            f"{r['board']} {r['year']} n={r['n']:4} intra2={100 * (r.get('ex_r_intra2') or 0):6.2f} "
            f"t={r.get('t_ex_r_intra2')} win={r.get('win_ex_r_intra2')}"
        )
    print("\n== 对照：非涨停股同条件 ==")
    for r in controls:
        c = "ex_r_intra1" if r["kind"] == "gap" else "ex_r_intra2"
        print(f"{r['kind']:5} {r['year']} n={r['n']:6} {c}={100 * (r.get(c) or 0):6.2f} t={r.get('t_' + c)}")
    print("\n== E2 T+1 开盘买 ==  ")
    for r in e2:
        print(r)
    print("\n== E3 T+1 大跌 -> T+2 回抽 ==  ")
    for r in e3:
        print(r)
    print("written", OUT)


if __name__ == "__main__":
    main()
