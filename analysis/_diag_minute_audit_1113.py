"""分钟线补齐的验收判据 —— 替代「根数 >= 240」，并给出**能定方向**的两条独立轴。

## 为什么换判据（2026-11-13）

「n == 240」对三种真实故障全部失明：
  * 停牌被补成 240 根 0 量  → 记成「满」；
  * 多一根 + 少一根，净 240  → 记成「满」（一增一减天然抵消）；
  * 全天只有 73 根但每根都在  → 「根数」不比集合，看不出缺时段。

## 两个判据，不许混

**L 标注（时刻集合）** 该 symbol 当日的时刻集合 == 规范网格
  规范网格 = 09:31..11:30 ∪ 13:01..15:00（**按区间末日标注**，240 个）。
  实测 5,570 只/日里 5,530+ 只都落在这个网格上，所以它是数据的既有约定，
  不是本脚本的假设。

**M 金额（跨表对账）** Σ(该 symbol 当日**全部** volume) == 日线 volume
  这是「表里存的东西加起来等不等于交易所的数」。**全量求和，不做网格过滤** ——
  过滤等于把幽灵行从分母里放走，判据就永远绿了，而表里那几行还在。
  附加 amount 同口径对账（volume 相等而 amount 差，说明均价口径错，另一类故障）。

**D 归因（不是判据）** Σ(仅规范网格内的 volume) 与日线的差
  用来回答「M 的差额是不是全部来自网格外那几行」。11-05/11-12 的幽灵行
  就靠这一列定性：D 归零而 M 不归零 ⇒ 差额 = 网格外那几行，且它们的量
  确实被重复计入。

## 外部轴（--external）

本地日线与被检的分钟表可能同源（同一次同步的两个产物），拿它当「真值」是
自证。`--external` 另取 fuyao 日线（`plugins/fuyao`，与本表数据链路独立）。
2026-11-13 实测：55 个可疑 (symbol, day) 组合上本地日线与 fuyao **0 处偏差**，
Σmin 全量偏差 **55/55**，Σmin 规范网格偏差 **1/55** —— 这条轴才是否掉
「是不是本表自己错了」的那一票。默认不联网，只在显式传参时取。

## 分类（决定修法，不是决定红绿）

  phantom_1300  13:00 行且 (open,high,low,close,volume) 与同 symbol 当日 11:30
                行**逐字段相等** ⇒ 11:30 那根被贴了第二个标签。
                amount 与 11:30 差在末两位（round 到 100 元）也一并记录，
                它是「同一根 bar 被两条路径各写一次」的指纹。
  filler_grid   0 量且无日线行（停牌）⇒ 合成网格用了 09:31..11:29 ∪ 13:00..15:00
                （缺 11:30、多 13:00），数量仍是 240 ⇒ 只有 L 看得见。
  volume_gap    网格内 Σ 与日线差 > max(5bp, 100 手) ⇒ 真差异，需人工定性。
                （5bp + 100 手是两条阈值：北交所 920xxx 普遍 Σmin−daily = +1 手，
                 只按比例卡会产出 37 条 ±1 手的假阳性。）

## 另外两条均价轴（2026-11-13 补，都只看**均价与自身价格的相容性**）

**B 收盘 bar** `amount/(volume·100)` vs 该 bar 的 `close`
  14:57–15:00 只有一个价 ⇒ 相等。偏离只能来自不进 OHLC 却进量额的成交。
  判据写**整手残差** `|amount − volume·100·close|/(100·close) > 1 手`，不写百分比：
  volume 整手取整，纯 close 成交的 bar 残差恒 ≤ 1 手（实测 V<1000 的 17,540 根
  max = 0.99），越线的只有 9 根、下一根在 40 手，阈值不敏感。
  写成「dev > 1%」会得到 286 根，其中 277 根手数 < 100（中位 30）——
  那些 1%~29% 全是取整噪声。

**S 日线自身** `amount/(volume·100)` 必须落在**该行** `[low, high]` 内（±2bp）
  6 天 33,430 行里只有 6 行越界、全 BJ；SH/SZ 31,301 行 0 命中。
  它不看外部源就能否掉一行：`920717.BJ 11-06` 均价 6.1666 < low 6.18，
  而 20,580 − 14,152 = 6,428 正好是 fuyao 的值。
  与 fuyao 轴互补：`920640 11-10` / `920964 11-11` / `920375 11-11` 三行
  fuyao 也含那笔量（外部轴恒绿），只有 S 看得见；`920112 11-05` 的成交价
  落在区间内，只有 fuyao 看得见。**单靠任何一轴都不够。**

## 用法

  python analysis/_diag_minute_audit_1113.py                 # 盘上最新 6 个分区
  python analysis/_diag_minute_audit_1113.py --all           # 全部分区
  python analysis/_diag_minute_audit_1113.py --external      # 追加 fuyao 对账
  python analysis/_diag_minute_audit_1113.py --json          # 落产物（过 assert_clean）
  python analysis/_diag_minute_audit_1113.py --repair-phantom            # 只看修法
  python analysis/_diag_minute_audit_1113.py --repair-phantom --apply    # 真改（先备份）

默认窗口取**盘上最新 6 个分区**（`latest_days()`），不是硬编码日期 ——
否则「明天自动生效」实际是「明天再报一次这一周」。

退出码：0 全绿 / 2 仅标注异常 / 3 有金额·量差异 / 4 有非竞价成交（B 或 S 命中）。
3 优先于 4：金额差异更接近「表坏了」，4 更接近「口径未定」。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent

#: 5bp 与 100 手必须同时越线才算差异。
REL_TOL = 0.0005
ABS_TOL = 100.0

#: S 轴（日线均价）的手数下限：`amount/(volume·100)` 的量化噪声是 0.5/volume，
#: 3 手的日线天生带 ±17%，落在 [low,high] 之外也说明不了什么。
#: B 轴（单根 bar）不用这个下限 —— 它用整手残差，自带同一条上界。
MIN_VOL = 100.0

#: 日线均价的容差：价格以分为单位，2bp 足够宽又不会漏 6.16 vs 6.18。
PRICE_EPS = 2e-4

DEFAULT_DAYS = (
    "2026-11-05", "2026-11-06", "2026-11-09",
    "2026-11-10", "2026-11-11", "2026-11-12",
)
WINDOW = 6


def latest_days(n: int = WINDOW) -> list[str]:
    """盘上最新 n 个分钟分区。**默认窗口不能是硬编码日期** ——
    否则「明天自动生效」实际是「明天再报一次这一周」，新数据永远进不来。"""
    parts = sorted(
        p.name[5:] for p in (DATA / "kline_minute").glob("date=*") if p.is_dir()
    )
    return parts[-n:] if parts else list(DEFAULT_DAYS)

_OHLCV = ("open", "high", "low", "close", "volume")


def canonical_grid() -> list[time]:
    """A 股 240 根：上午 09:31..11:30，下午 13:01..15:00（按区间末日标注）。"""
    out: list[time] = []
    for start_h, start_m, n in ((9, 31, 120), (13, 1, 120)):
        t = start_h * 60 + start_m
        out.extend(time((t + i) // 60, (t + i) % 60) for i in range(n))
    return out


GRID = canonical_grid()
GRID_SET = set(GRID)


def _daily_frame(month_cache: dict[Path, pl.DataFrame], day: date) -> pl.DataFrame:
    """某交易日的日线行。月分区与日分区 schema 不同（volume Int64/Double、
    月分区多 quote_ts），因此**按分区族分别读**，不 glob 跨族路径。"""
    base = DATA / "kline_daily"
    for cand in (base / f"date={day:%Y-%m}", base / f"date={day.isoformat()}"):
        if not cand.is_dir():
            continue
        if cand not in month_cache:
            files = sorted(cand.glob("*.parquet"))
            month_cache[cand] = (
                pl.read_parquet([str(f) for f in files])
                .select(
                    pl.col("date").cast(pl.Date),
                    pl.col("symbol").cast(pl.Utf8),
                    pl.col("high").cast(pl.Float64),
                    pl.col("low").cast(pl.Float64),
                    pl.col("volume").cast(pl.Float64).alias("dvol"),
                    pl.col("amount").cast(pl.Float64).alias("damt"),
                )
                if files
                else pl.DataFrame()
            )
        hit = month_cache[cand].filter(pl.col("date") == day).drop("date")
        if hit.height:
            return hit
    return pl.DataFrame(
        schema={"symbol": pl.Utf8, "high": pl.Float64, "low": pl.Float64,
                "dvol": pl.Float64, "damt": pl.Float64}
    )


def _minute_frame(day: date) -> pl.DataFrame | None:
    mdir = DATA / "kline_minute" / f"date={day}"
    files = sorted(mdir.glob("*.parquet"))
    if not files:
        return None
    df = (
        pl.scan_parquet([str(f) for f in files])
        .select("symbol", "datetime", *_OHLCV, "amount")
        .collect()
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("datetime").dt.time().alias("t"),
        )
    )
    # 读取时刻就必须拒绝重复键。多一个文件（备份/流式残留）不会报错，
    # 只会让这一天的行数悄悄翻倍、下游 `(date,symbol)` 重复。判据必须在这里响，
    # 不能等到 order 表 exit 4 再回头找。
    uniq = df.select("symbol", "datetime").n_unique()
    if uniq != df.height:
        raise RuntimeError(
            f"{mdir.name}: (symbol,datetime) 重复 {df.height - uniq} 组；"
            f"分区内 {len(files)} 个文件 [{', '.join(f.name for f in files)}] 有重叠"
        )
    return df


def classify(m: pl.DataFrame) -> pl.DataFrame:
    """逐 symbol 一行：网格差、全量/网格内求和、13:00 幽灵标记。"""
    per = m.group_by("symbol").agg(
        pl.len().alias("bars"),
        pl.col("volume").sum().alias("m_all"),
        pl.col("amount").sum().alias("a_all"),
        pl.col("t").filter(pl.col("t").is_in(list(GRID_SET))).len().alias("hit"),
    )
    on_grid = m.filter(pl.col("t").is_in(list(GRID_SET))).group_by("symbol").agg(
        pl.col("volume").sum().alias("m_can"),
        pl.col("amount").sum().alias("a_can"),
    )
    per = per.join(on_grid, on="symbol", how="left").with_columns(
        pl.col("m_can").fill_null(0.0),
        pl.col("a_can").fill_null(0.0),
    )

    # 13:00 幽灵：与同一 symbol 的 11:30 行逐字段相等（OHLCV）
    b13 = m.filter(pl.col("t") == time(13, 0)).select("symbol", *_OHLCV, "amount")
    b30 = m.filter(pl.col("t") == time(11, 30)).select(
        "symbol", *[pl.col(c).alias(c + "_30") for c in (*_OHLCV, "amount")]
    )
    dup = (
        b13.join(b30, on="symbol", how="left")
        .with_columns(
            pl.all_horizontal(
                [pl.col(c) == pl.col(c + "_30") for c in _OHLCV]
            ).alias("dup_of_1130")
        )
        .select("symbol", "dup_of_1130", pl.col("amount").alias("a_1300"),
                pl.col("amount_30").alias("a_1130"), pl.col("volume").alias("v_1300"))
    )
    return per.join(dup, on="symbol", how="left").with_columns(
        (pl.col("bars") - pl.col("hit")).alias("extra_t"),
        (pl.col("hit") < 240).alias("miss_t"),
        pl.col("dup_of_1130").fill_null(False),
    )


def audit_day(day: date, month_cache: dict[Path, pl.DataFrame],
              keep: dict[str, pl.DataFrame] | None = None) -> dict:
    m = _minute_frame(day)
    if m is None:
        return {"day": day.isoformat(), "minute_partition": False,
                "symbols": 0, "label_bad": 0, "money_bad": 0, "gap_bad": 0,
                "phantom": 0, "filler": 0, "rows": 0,
                "block_1500": 0, "block_1500_detail": [],
                "daily_bad": 0, "daily_bad_detail": []}
    per = classify(m)
    dv = _daily_frame(month_cache, day)
    j = per.join(dv, on="symbol", how="left")
    # 停牌：0 量且无日线行 —— 不参与金额对账，但要单独计数（它们的网格是假的）
    halted = (j["m_all"] == 0) & j["dvol"].is_null()
    j = j.with_columns(halted.alias("halted"))

    # 收盘 bar 均价轴：14:57–15:00 集合竞价只有一个价 ⇒ amount/(volume·100) 必等于 close。
    # 偏离只可能来自**不进 OHLC 却计入量额**的成交（北交所大宗交易/盘后成交）。
    # 这条轴的价值：两侧都把这笔量算进去时，Σ 对账**恒等**、根数与网格全绿，
    # 只有它响。
    #
    # 判据不写百分比，写**整手残差**：volume 是整手取整的，所以一笔"只按 close 成交"的
    # bar 必然满足 |amount − volume·100·close| ≤ 1 手×100×close。
    # 实测 33,401 根里 V<1000 的 17,540 根 max(残差/1手) = 0.99（上界就是取整本身），
    # 越 1 手的只有 9 根、且下一根在 40 手 —— 阈值在 (1,40) 之间取什么都一样。
    # 这比"1% + 手数下限"少两个魔数，也不会把 30 手的 bar（天生 ±1.7%）算成异常。
    b15 = m.filter(pl.col("t") == time(15, 0)).filter(
        (pl.col("volume") > 0) & (pl.col("close") > 0)
    ).with_columns(
        ((pl.col("amount") - pl.col("volume") * 100 * pl.col("close")).abs()
         / (100 * pl.col("close"))).alias("resid_h"),
    )
    block15 = b15.filter(pl.col("resid_h") > 1.0)

    # 日线自身的一致性：amount/(volume·100) 必须落在**本行** [low, high] 内。
    # 这一条不需要任何外部源，抓的是「这一行的量列与额列不同源 / 含非竞价成交」：
    # 33,430 行里 6 行越界、全 BJ，SH/SZ 31,301 行 0 命中（= 极高精度）。
    # 它与 fuyao 轴互补：920717 11-06 本地日线 20,580 = fuyao 6,428 + 14,152，
    # 均价 6.1666 已掉到 low 6.18 之下 ⇒ 不看 fuyao 也知道这行不能用。
    # 反过来 920640 11-10 / 920964 11-11 / 920375 11-11 三行 fuyao 也含那笔量，
    # 外部轴对它们恒绿，只有这条看得见。
    pavg = pl.col("damt") / (pl.col("dvol") * 100)
    dself = dv.filter(
        (pl.col("dvol") >= MIN_VOL) & (pl.col("damt") > 0)
    ).filter(
        (pavg > pl.col("high") * (1 + PRICE_EPS))
        | (pavg < pl.col("low") * (1 - PRICE_EPS))
    ).with_columns(pavg.alias("p_avg"))

    label_bad = j.filter((pl.col("bars") != 240) | pl.col("miss_t")).height
    paid = j.filter(~pl.col("halted") & pl.col("dvol").is_not_null() & (pl.col("dvol") > 0))
    paid = paid.with_columns(
        (pl.col("m_all") - pl.col("dvol")).alias("e_all"),
        (pl.col("m_can") - pl.col("dvol")).alias("e_can"),
        (pl.col("a_all") - pl.col("damt")).alias("ae_all"),
    )
    money_bad = paid.filter(
        ((pl.col("e_all").abs() > REL_TOL * pl.col("dvol")) & (pl.col("e_all").abs() > ABS_TOL))
        | ((pl.col("ae_all").abs() > REL_TOL * pl.col("damt")) & (pl.col("ae_all").abs() > ABS_TOL * 100))
    )
    gap_bad = paid.filter(
        (pl.col("e_can").abs() > REL_TOL * pl.col("dvol")) & (pl.col("e_can").abs() > ABS_TOL)
    )
    if keep is not None:
        keep[day.isoformat()] = paid
    return {
        "day": day.isoformat(),
        "minute_partition": True,
        "rows": m.height,
        "symbols": per.height,
        "label_bad": label_bad,
        "money_bad": money_bad.height,
        "gap_bad": gap_bad.height,
        "phantom": j.filter(pl.col("dup_of_1130")).height,
        "block_1500": block15.height,
        "block_1500_detail": block15.sort("resid_h", descending=True)
        .select("symbol", "close", "volume", "resid_h").to_dicts(),
        "daily_bad": dself.height,
        "daily_bad_detail": dself.sort("p_avg")
        .select("symbol", "low", "high", "p_avg", "dvol", "damt").to_dicts(),
        "filler": int(halted.sum()),
        "money_bad_detail": money_bad.sort("e_all", descending=True)
        .select("symbol", "bars", "m_all", "dvol", "e_all", "m_can", "e_can", "a_all", "damt")
        .to_dicts(),
        "gap_bad_detail": gap_bad.select(
            "symbol", "m_can", "dvol", "e_can", "a_can", "damt").to_dicts(),
    }


def external_check(frames: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame | None, str]:
    """fuyao 日线作为**独立轴**：本地日线与分钟表可能同源，它是第三方。

    一次 10d dump（约 1MB）覆盖全市场，所以不是抽样——6 天全量逐只比。
    返回 (逐 (day,symbol) 的本地/外部并列表, 说明)。
    """
    if not frames:
        return None, "无可比数据"
    local = pl.concat([f.with_columns(pl.lit(d).alias("day")).select(
        pl.lit(d).alias("day"), "symbol", "dvol", "damt", "m_all", "m_can")
        for d, f in frames.items()])
    symbols = sorted(set(local["symbol"].to_list()))
    root = str(ROOT / "backend")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from app.plugins.fuyao import provider as fuyao  # noqa: PLC0415

        ok, why = fuyao.availability()
        if not ok:
            return None, why
        prov = fuyao.FuyaoProvider()
        try:
            lo = datetime.fromisoformat(min(frames))
            hi = datetime.fromisoformat(max(frames))
            from datetime import timedelta  # noqa: PLC0415

            ext = prov.get_daily(symbols, lo - timedelta(days=2), hi + timedelta(days=2))
        finally:
            prov.close()
    except Exception as e:  # noqa: BLE001 — 网络/依赖一律降级，不让判据崩
        return None, f"{type(e).__name__}: {e}"
    if ext is None or ext.is_empty():
        return None, "外部源返回空"
    ext = ext.select(
        pl.col("symbol").cast(pl.Utf8), pl.col("date").cast(pl.Date),
        pl.col("volume").cast(pl.Float64).alias("fvol"),
    ).with_columns(pl.col("date").cast(pl.Utf8).alias("day"))
    return local.join(ext, on=["day", "symbol"], how="left"), f"{len(symbols)} 只"


def repair_plan(month_cache: dict[Path, pl.DataFrame], days: list[str]) -> list[dict]:
    """幽灵行修法，两条都由**数据自身**决定，不需要知道成因。

    1. 有 11:30 且 13:00 是它的副本  → 删 13:00（重复计入，M 虚高）。
    2. 有 13:00 却没有 11:30         → 把 13:00 改回 11:30（合成网格贴错标签，
       数量仍是 240、量仍是 0，只有 L 看得见；改标后 240 根落在规范网格上）。

    改完 L 与 M 都必须归零 —— 这就是验收，不需要相信本函数的理由。
    """
    plan: list[dict] = []
    for d in days:
        day = date.fromisoformat(d)
        m = _minute_frame(day)
        if m is None:
            continue
        per = classify(m)
        for r in per.filter(pl.col("dup_of_1130")).to_dicts():
            plan.append({"day": d, "symbol": r["symbol"],
                         "action": "delete_1300", "volume": float(r["v_1300"] or 0.0)})
        has13 = set(m.filter(pl.col("t") == time(13, 0))["symbol"].to_list())
        has30 = set(m.filter(pl.col("t") == time(11, 30))["symbol"].to_list())
        for sym in sorted(has13 - has30):
            vol = m.filter((pl.col("symbol") == sym) & (pl.col("t") == time(13, 0)))["volume"].sum()
            plan.append({"day": d, "symbol": sym,
                         "action": "relabel_1300_as_1130", "volume": float(vol)})
    return plan


def apply_repair(days: list[str], plan: list[dict]) -> None:
    """按日重写受影响分区。备份原文件；不备份不写。

    备份**必须落在分区目录之外**：`part.bak-<ts>.parquet` 会被 `*.parquet` 命中
    （审计侧与 `decision/bars.py::_day_frame` 都是 glob 后直接 concat），
    下一次读同一天就变成两份数据 —— 修一次、脏两次。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for d in days:
        items = [p for p in plan if p["day"] == d]
        if not items:
            continue
        mdir = DATA / "kline_minute" / f"date={d}"
        files = sorted(mdir.glob("*.parquet"))
        if len(files) != 1:
            print(f"  [skip] {d}: 分区文件 {len(files)} 个，只处理单文件分区")
            continue
        src = files[0]
        bdir = DATA / "_backup" / "minute_audit" / stamp / f"date={d}"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, bdir / src.name)
        df = pl.read_parquet(src)
        df = df.with_columns(pl.col("datetime").dt.time().alias("_t"))
        del_syms = {p["symbol"] for p in items if p["action"] == "delete_1300"}
        relabel = {p["symbol"] for p in items if p["action"] == "relabel_1300_as_1130"}
        drop = (pl.col("_t") == time(13, 0)) & pl.col("symbol").is_in(list(del_syms))
        fix = (pl.col("_t") == time(13, 0)) & pl.col("symbol").is_in(list(relabel))
        out = (
            df.filter(~drop)
            .with_columns(
                pl.when(fix)
                .then(pl.col("datetime") - __import__("datetime").timedelta(minutes=90))
                .otherwise(pl.col("datetime"))
                .alias("datetime")
            )
            .drop("_t")
            .sort("symbol", "datetime")
        )
        # 原地覆盖 1.3M 行的分区不是原子操作：中途失败会留下半个文件。
        tmp = src.parent / (src.name + ".tmp")
        out.write_parquet(tmp)
        os.replace(tmp, src)
        print(f"  [write] {d}: 删 {len(del_syms)} 删行 / 改标 {len(relabel)} "
              f"→ {df.height} → {out.height} 行；备份 {bdir.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("days", nargs="*", help="交易日；省略则用 DEFAULT_DAYS")
    ap.add_argument("--all", action="store_true", help="扫描全部分钟分区")
    ap.add_argument("--external", action="store_true", help="追加 fuyao 外部对账")
    ap.add_argument("--json", action="store_true", help="落 _result.json（过 assert_clean）")
    ap.add_argument("--repair-phantom", action="store_true", help="列出幽灵行修法")
    ap.add_argument("--apply", action="store_true", help="配合 --repair-phantom 真改（先备份）")
    args = ap.parse_args(argv)

    if args.all:
        days = sorted(p.name[5:] for p in (DATA / "kline_minute").glob("date=*"))
    else:
        days = args.days or latest_days()

    cache: dict[Path, pl.DataFrame] = {}
    frames: dict[str, pl.DataFrame] = {}
    report = [audit_day(date.fromisoformat(d), cache, frames) for d in days]

    if args.repair_phantom:
        plan = repair_plan(cache, days)
        print(f"幽灵行 {len(plan)} 条："
              f"删 {sum(p['action'] == 'delete_1300' for p in plan)} / "
              f"改标 {sum(p['action'] == 'relabel_1300_as_1130' for p in plan)}")
        for p in plan[:6]:
            print("   ", p["day"], p["symbol"], p["action"])
        if args.apply and plan:
            apply_repair(days, plan)
        elif plan:
            print("   （未加 --apply，仅计划；真改会先备份到 "
                  "data/_backup/minute_audit/<ts>/date=<day>/part.parquet）")
        return 0

    ext_df, ext_note = external_check(frames) if args.external else (None, "未启用")

    tot = {
        "days": len(report),
        "label_bad": sum(r["label_bad"] for r in report),
        "money_bad": sum(r["money_bad"] for r in report),
        "gap_bad": sum(r["gap_bad"] for r in report),
        "phantom": sum(r["phantom"] for r in report),
        "block_1500": sum(r["block_1500"] for r in report),
        "daily_bad": sum(r["daily_bad"] for r in report),
        "filler": sum(r["filler"] for r in report),
    }
    for r in report:
        print(f"[{r['day']}] " + (
            f"行 {r['rows']} 标的 {r['symbols']} 停牌合成 {r['filler']}"
            f" | L 标注异常 {r['label_bad']}"
            f" | M 金额异常 {r['money_bad']}"
            f" | D 归因残留 {r['gap_bad']}"
            f" | 13:00 幽灵 {r['phantom']}"
            f" | 收盘bar均价异常 {r['block_1500']}"
            f" | 日线均价越界 {r['daily_bad']}"
            if r["minute_partition"] else "无分钟分区"))
        for d in r.get("money_bad_detail", [])[:6]:
            print(f"      M {d['symbol']} bars={d['bars']} Σall={d['m_all']:.0f} "
                  f"daily={d['dvol']:.0f} 差={d['e_all']:+.0f} "
                  f"(网格内差 {d['e_can']:+.0f}) Σamt 差={d['a_all'] - d['damt']:+.0f}")
    print(f"\n=> 汇总 {tot}")
    ext_summary: dict[str, object] = {"enabled": bool(args.external), "note": ext_note}
    if args.external and ext_df is not None:
        got = ext_df.filter(pl.col("fvol").is_not_null())
        for key, col, scale in (("daily", "dvol", 1.0), ("min_all", "m_all", 1.0),
                                ("min_can", "m_can", 1.0)):
            bad = got.filter(
                ((pl.col(col) - pl.col("fvol")).abs() > REL_TOL * pl.col("fvol"))
                & ((pl.col(col) - pl.col("fvol")).abs() > ABS_TOL * scale))
            ext_summary[f"{key}_mismatch"] = bad.height
        ext_summary["rows"] = got.height
        ext_summary["daily_bad_detail"] = got.filter(
            ((pl.col("dvol") - pl.col("fvol")).abs() > REL_TOL * pl.col("fvol"))
            & ((pl.col("dvol") - pl.col("fvol")).abs() > ABS_TOL)
        ).select("day", "symbol", "dvol", "fvol", "m_all", "m_can").sort("day", "symbol").to_dicts()
        ext_summary["missing"] = ext_df.height - got.height
        print(f"   外部轴（{ext_note}）：可比 {got.height} 行，缺 {ext_summary['missing']}；"
              f"本地日线≠fuyao {ext_summary['daily_mismatch']}、"
              f"Σmin全量≠fuyao {ext_summary['min_all_mismatch']}、"
              f"Σmin网格≠fuyao {ext_summary['min_can_mismatch']}")
    elif args.external:
        print(f"   外部轴不可用：{ext_note}")

    rc = 3 if tot["money_bad"] else (
        4 if (tot["block_1500"] or tot["daily_bad"]) else (2 if tot["label_bad"] else 0)
    )
    print("=> 判定:", {0: "全绿", 2: "仅标注异常", 3: "有金额差异",
                       4: "有非竞价成交（量额口径）"}[rc])

    if args.json:
        from _artifact_guard import assert_clean, render_reading  # noqa: PLC0415

        nv = {
            "window": {"days": days},
            "totals": tot,
            "days": report,
            "external": ext_summary,
        }
        nv["reading_template"] = (
            "{days} 个交易日：标注异常 {label_bad} 处、金额异常 {money_bad} 处，"
            "其中 {phantom} 行是 13:00 幽灵（11:30 那根的副本）、"
            "{filler} 只是停牌合成网格；扣掉网格外行后仍对不上账的 {gap_bad} 处。"
            "另有 {block_1500} 根收盘 bar 的均价与收盘价不符（不进 OHLC 却进量额的成交），"
            "{daily_bad} 行日线自身的 amount/(volume·100) 已越出该行 low/high。"
        )
        nv["reading_fields"] = {
            "days": "$totals.days", "label_bad": "$totals.label_bad",
            "money_bad": "$totals.money_bad", "phantom": "$totals.phantom",
            "filler": "$totals.filler", "gap_bad": "$totals.gap_bad",
            "block_1500": "$totals.block_1500", "daily_bad": "$totals.daily_bad",
        }
        nv["reading"] = render_reading(nv)
        assert_clean(nv, "minute_audit")
        out = HERE / "_diag_minute_audit_1113_result.json"
        out.write_text(json.dumps(nv, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"=> 产物 {out.relative_to(ROOT)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
