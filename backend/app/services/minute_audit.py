"""分钟 K 验收判据 —— **唯一实现**。

## 为什么在后端而不在 `analysis/`

`analysis/` 不在镜像里（`Dockerfile` 只 COPY `backend/app`）。判据留在那里，
「明天自动生效」就只是开发机上的手动动作：容器里既看不见异常，也没有修法。
所以台账逻辑必须落在 `backend/app/services/`，`analysis/_diag_minute_audit_1113.py`
退化成薄壳（保留长文档与 JSON 产物），`daily_pipeline` 调同一份。

这与 `market_time.minute_grid()` 是同一条教训：同一个映射写两遍，只覆盖一份的分支。

## 四条轴，各管一段

**L 标注（时刻集合）** 该 symbol 当日的时刻集合 == 规范网格（240 格）。
  「n == 240」对三种真实故障全部失明：停牌被补成 240 根 0 量、多一根 + 少一根
  净 240、全天只有 73 根但每根都在。只有比集合才看得见。

**M 金额（跨表对账）** Σ(该 symbol 当日**全部** volume) == 日线 volume。
  全量求和，**不做网格过滤** —— 过滤等于把幽灵行从分母里放走，判据就永远绿了。

**D 归因（不是判据）** Σ(仅网格内) 与日线的差，回答「M 的差是不是全来自网格外」。

**B 收盘 bar 均价** `|amount − volume·100·close| / (100·close) > 1 手`。
  14:57–15:00 只有一个价，偏离只可能来自不进 OHLC 却进量额的成交。判据写整手
  残差而不是百分比：`volume` 整手取整，纯 close 成交的 bar 残差恒 ≤ 1 手。

**S 日线自身均价** `amount/(volume·100)` 必须落在**该行** `[low, high]` 内（±2bp）。
  6 天 33,430 行里只有 6 行越界、全 BJ；SH/SZ 31,301 行 0 命中。它不看外部源
  就能否掉一行，与 fuyao 轴互补（各有各看不见的那一半）。

## 大宗交易：B/S/M 是同一件事的三个投影，不是三个缺陷

2026-11-13 复核把 16 条明细逐条追到同一个机制上：**北交所的大宗交易被并入分钟侧
15:00 那根**（量、额都进，OHLC 不进），而**日线一行是否含它取决于写这一行的是谁**。
`data/kline_daily/date=2026-11` 里 `quote_ts` 非空的是现网同步写的（含大宗），
为空的是迁移带过来的历史行（不含，与 fuyao 逐只相同）。于是同一天里：

* 日线含大宗 → Σ 对账恒等（M 绿），但日线均价被折价单拉出 `[low, high]`（S 红）；
* 日线不含大宗 → Σ 多出那笔（M 红），S 反而绿；
* 分钟侧那根 15:00 永远含它 → B 红。

三条轴各自看见的都不是「数据坏了」，是**口径**。把它们放进退出码的后果是
「每天恒红」——而恒红的判据等于静音（`quarantined` 那条已经踩过一次）。
所以这里把它们分成两层：

* `*_basis`：符号是 `*.BJ` 且隐含价落在 2× 包络内（`IMPLIED_LO/HI`）。进产物、
  进逐日行，**不进退出码**；
* `*_unexplained`：其余全部。非 BJ、隐含价荒谬、或有量差却没有对应的大宗价
  ⇒ 退出码照旧红。

包络不是法定价幅，只是「算术上讲不讲得通」：一笔大宗可以折价 10%，不会折到
十分之一。用法定价幅会引入一个每年都可能变的常量，而这里要排除的是「十倍误差」
这一类，2× 足够。

最后一层是**两条轴必须对同一笔大宗给出同一个量**：`M` 的隐含手数 × (1 − 隐含价/
close) 应当等于 `B` 的残差手数（920375.BJ 11-09 实测 403.8 vs 402.8）。这一条
不成立，说明 M 的差额并不全在 15:00 那根（例如别处还有重复行）——那才是要红的。

## 与落盘闸门的分工

`kline_sync._off_grid_duplicate_rows` 只认**会双计**的那一支（越界时刻 +
与网格内某行 OHLCV 全等 + volume>0），处置是**逐行隔离**：那一行不落盘、原样进
`data/_quarantine/<表>/date=<d>/`，同帧其余行照写。L/B/S/M 四条轴它一条都不管：
拦下来会把「口径未定」当「数据坏了」，而且写路径没有足够的上下文做定性。
判据留在这里，写路径只做「拒绝 → 双计」这一件不可逆的事。

隔离行数进 `quarantined` 但不进退出码：数据已经是对的，剩的是「源还在产脏」，
那件事由 WARNING 与按天的产物负责（每天恒红的判据等于静音）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from app.market_time import cn_now, minute_grid
from app.tickflow.partitions import locate_partition_dir

#: 5bp 与 100 手必须同时越线才算差异。
REL_TOL = 0.0005
ABS_TOL = 100.0

#: S 轴（日线均价）的手数下限：`amount/(volume·100)` 的量化噪声是 0.5/volume，
#: 3 手的日线天生带 ±17%，落在 [low,high] 之外也说明不了什么。
#: B 轴（单根 bar）不用这个下限 —— 它用整手残差，自带同一条上界。
MIN_VOL = 100.0

#: 日线均价的容差：价格以分为单位，2bp 足够宽又不会漏 6.16 vs 6.18。
PRICE_EPS = 2e-4

#: 北交所：唯一观察到「大宗交易并入分钟 15:00 根」的总体（6 天 16/16 条明细）。
#: 这是**观察到的总体**而不是「北交所豁免」：非 BJ 一旦出现同样形态，那是新事实，
#: 必须红（见模块 docstring）。
BJ_SUFFIX = ".BJ"
#: 隐含大宗价的 2× 包络（× 当日 close / [low, high]）。不是法定价幅 —— 只排除
#: 「十倍误差」这一类，不假装知道北交所大宗交易的申报区间。
IMPLIED_LO, IMPLIED_HI = 0.5, 2.0
#: M 与 B 对同一笔大宗必须给出同一个量（残差手数），容差 10%。
BRIDGE_TOL = 0.10

#: 默认窗口：盘上最新 N 个分区。**不能硬编码日期** —— 否则「明天自动生效」
#: 实际是「明天再报一次这一周」，新数据永远进不来。
WINDOW = 6

#: 收盘（15:00 最后一根）+ 落盘/合并余量：这个钟点之后今天的才算一天。
CLOSE_SETTLE = time(15, 5)

_OHLCV = ("open", "high", "low", "close", "volume")

GRID = minute_grid()
GRID_SET = frozenset(GRID)
#: `is_in` 用有序 list（一次构造、复用）：set 的迭代顺序不稳定，任何落进错误信息的
#: 时刻顺序都应该可复现；而且每轮重建 240 元素 list 在 1.3M 行上是纯浪费。
_GRID_LIST = list(GRID)

#: 退出码：0 全绿 / 2 仅标注异常 / 3 有金额·量差异 / 4 有非竞价成交。
#: 3 优先于 4：金额差异更接近「表坏了」，4 更接近「口径未定」。
RC_GREEN, RC_LABEL, RC_MONEY, RC_BLOCK = 0, 2, 3, 4


class MinutePartitionError(RuntimeError):
    """分区内 `(symbol, datetime)` 重复 —— 多一个文件（备份/流式残留）。

    必须在**读取时刻**响：`scan_parquet` 不报错，只会让这一天悄悄多出几百只，
    下游要等到 `(date,symbol)` 重复才发觉（实测 11-05 曾走到 09:20 下单表 exit 4）。
    """


def latest_days(data_dir: Path, n: int = WINDOW, *, now: datetime | None = None) -> list[str]:
    """盘上最新 n 个**已完成**的分钟分区（按目录名排序，缺日不填补）。

    **未收盘的今天不算一天。** 09:30 到收盘之间，今天的分区天然只有几根，把它算进
    窗口会让 CLI 当场报出 1400+ 条「标注异常」—— 那不是数据坏了，是判据拿一个还在
    长的分区当成品（2026-11-13 09:37 实测：窗口从 11-05..11-12 滑到 11-06..11-13，
    `label_bad 0 → 1375`，而 11-06..11-12 那五天一字未变）。

    规则只看钟点，不看数据形状：收盘前不可能有完整交易日，所以它**不会掩盖真异常**
    —— 15:05 之后今天照样进窗口、照样可以被判红。反过来，若按「这天不满 240 根」
    去筛，真正缺失的那一天会被自己筛掉，L 轴就永远绿了。

    **日期与钟点必须来自同一个时钟。** 上一版写的是 `(now or cn_now()).time()` 比
    `cn_today().date()`：测试里两个都被 patch 到同一天所以看不出来，生产里也一样
    （两者都取北京当下）。但任何「回放某一天」的调用 —— 审计产物里的 `--days`、
    盘后补跑、变异脚本 —— 只要传入 `now`，日期就必须跟着 `now` 走，否则
    「今天是哪一天」和「现在几点」来自两个时刻，判据会在跨零点时放行一个未收盘的分区。
    """
    base = Path(data_dir) / "kline_minute"
    parts = sorted(p.name[5:] for p in base.glob("date=*") if p.is_dir())
    ref = now or cn_now()
    if parts and parts[-1] == ref.date().isoformat() and ref.time() < CLOSE_SETTLE:
        parts = parts[:-1]
    return parts[-n:]


def _minute_frame(data_dir: Path, day: date) -> pl.DataFrame | None:
    """某日分钟 K，附加 `t`（时刻）列。无分区返回 None。"""
    mdir = Path(data_dir) / "kline_minute" / f"date={day.isoformat()}"
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
    uniq = df.select("symbol", "datetime").n_unique()
    if uniq != df.height:
        raise MinutePartitionError(
            f"{mdir.name}: (symbol,datetime) 重复 {df.height - uniq} 组；"
            f"分区内 {len(files)} 个文件 [{', '.join(f.name for f in files)}] 有重叠"
        )
    return df


def _daily_frame(
    data_dir: Path, cache: dict[Path, pl.DataFrame], day: date
) -> pl.DataFrame:
    """某交易日的日线行。月分区与日分区 schema 不同（volume Int64/Double、
    月分区多 quote_ts），因此**按分区族分别读**，不 glob 跨族路径。

    分区定位走 `partitions.locate_partition_dir` —— 布局知识只允许有一份。
    """
    base = Path(data_dir) / "kline_daily"
    part = locate_partition_dir(base, "kline_daily", day)
    if part.is_dir():
        if part not in cache:
            files = sorted(part.glob("*.parquet"))
            cache[part] = (
                pl.read_parquet([str(f) for f in files])
                .select(
                    pl.col("date").cast(pl.Date),
                    pl.col("symbol").cast(pl.Utf8),
                    pl.col("high").cast(pl.Float64),
                    pl.col("low").cast(pl.Float64),
                    pl.col("close").cast(pl.Float64),
                    pl.col("volume").cast(pl.Float64).alias("dvol"),
                    pl.col("amount").cast(pl.Float64).alias("damt"),
                )
                if files
                else pl.DataFrame()
            )
        # 空目录（迁移窗口/手工清理残留）不是「这天没有日线行」，但也读不出行：
        # 直接回落空 schema 帧，别让 `filter` 在无列的帧上报 ColumnNotFound。
        hit = (cache[part].filter(pl.col("date") == day).drop("date")
               if not cache[part].is_empty() else cache[part])
        if hit.height:
            return hit
    return pl.DataFrame(
        schema={"symbol": pl.Utf8, "high": pl.Float64, "low": pl.Float64,
                "close": pl.Float64, "dvol": pl.Float64, "damt": pl.Float64}
    )


def classify(m: pl.DataFrame) -> pl.DataFrame:
    """逐 symbol 一行：网格差、全量/网格内求和、13:00 幽灵标记。"""
    per = m.group_by("symbol").agg(
        pl.len().alias("bars"),
        pl.col("volume").sum().alias("m_all"),
        pl.col("amount").sum().alias("a_all"),
        pl.col("t").filter(pl.col("t").is_in(_GRID_LIST)).len().alias("hit"),
    )
    on_grid = m.filter(pl.col("t").is_in(_GRID_LIST)).group_by("symbol").agg(
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


def _quarantined_rows(data_dir: Path, day: date) -> int:
    """当天被落盘路径隔离的越界副本行数（`data/_quarantine/kline_minute/date=*/`）。

    只数 `kline_minute` —— 与 `audit_day` 审计的表同一张。ETF 分钟
    （`kline_etf_minute`）的隔离记录在自己的子目录里，目前没有对应的审计轴，
    混进来就是跨口径相加。

    为什么不进 `verdict`: 隔离之后**盘上的数据是对的**（副本没落盘、量额不双计），
    这一项说的是「源还在产脏」。让它驱动退出码 = 每天恒红 = 报警静音（同 M 轴那条
    刻意不进 `stage_errors` 的理由）。它进产物与摘要行，做成可按天回看的时间序列。
    """
    root = Path(data_dir) / "_quarantine" / "kline_minute" / f"date={day.isoformat()}"
    total = 0
    for f in sorted(root.glob("*.json")):
        try:
            total += int(json.loads(f.read_text(encoding="utf-8")).get("rows") or 0)
        except Exception:  # noqa: BLE001 — 半截/非本函数写的文件不该让审计变故障源
            continue
    return total


def _empty_day(day: date) -> dict:
    return {"day": day.isoformat(), "minute_partition": False,
            "symbols": 0, "label_bad": 0, "money_bad": 0, "gap_bad": 0,
            "phantom": 0, "filler": 0, "rows": 0, "quarantined": 0,
            "block_1500": 0, "block_1500_basis": 0, "block_1500_detail": [],
            "daily_bad": 0, "daily_bad_basis": 0, "daily_bad_detail": [],
            "money_bad_basis": 0, "bridge_bad": 0, "bridge": []}


def _is_bj() -> pl.Expr:
    return pl.col("symbol").str.ends_with(BJ_SUFFIX)


def audit_day(
    data_dir: Path,
    day: date,
    month_cache: dict[Path, pl.DataFrame] | None = None,
    keep: dict[str, pl.DataFrame] | None = None,
) -> dict:
    """单日四条轴 + 大宗口径分层 + 明细。`keep` 收下参与 M 对账的逐行帧（供外部轴复用）。"""
    cache = {} if month_cache is None else month_cache
    m = _minute_frame(data_dir, day)
    if m is None:
        # 分区不在但隔离区有记录 = 那一天的行全被判成副本 —— 这两项必须一起看,
        # 否则「无分区」会被当成「源没给数据」。
        return {**_empty_day(day), "quarantined": _quarantined_rows(data_dir, day)}
    per = classify(m)
    dv = _daily_frame(data_dir, cache, day)
    j = per.join(dv, on="symbol", how="left")
    # 停牌：0 量且无日线行 —— 不参与金额对账，但要单独计数（它们的网格是假的）
    halted = (j["m_all"] == 0) & j["dvol"].is_null()
    j = j.with_columns(halted.alias("halted"))

    # 收盘 bar 均价轴：14:57–15:00 只有一个价 ⇒ amount/(volume·100) 必等于 close。
    # 偏离只可能来自**不进 OHLC 却计入量额**的成交（北交所大宗交易/盘后成交）。
    # 两侧都把这笔量算进去时 Σ 对账恒等、根数与网格全绿，只有它响。
    b15 = m.filter(pl.col("t") == time(15, 0)).filter(
        (pl.col("volume") > 0) & (pl.col("close") > 0)
    ).with_columns(
        ((pl.col("amount") - pl.col("volume") * 100 * pl.col("close")).abs()
         / (100 * pl.col("close"))).alias("resid_h"),
        (pl.col("amount") / (pl.col("volume") * 100)).alias("avg_px"),
    ).with_columns(
        (_is_bj()
         & (pl.col("avg_px") >= pl.col("close") * IMPLIED_LO)
         & (pl.col("avg_px") <= pl.col("close") * IMPLIED_HI)).alias("block_basis"),
    )
    block15 = b15.filter(pl.col("resid_h") > 1.0)
    block15_basis = block15.filter(pl.col("block_basis"))

    # 日线自身的一致性：amount/(volume·100) 必须落在**本行** [low, high] 内。
    # 33,430 行里 6 行越界、全 BJ，SH/SZ 31,301 行 0 命中（= 极高精度）。
    # 与 fuyao 轴互补：920717 11-06 本地 20,580 = fuyao 6,428 + 14,152。
    pavg = pl.col("damt") / (pl.col("dvol") * 100)
    dself = dv.filter(
        (pl.col("dvol") >= MIN_VOL) & (pl.col("damt") > 0)
    ).filter(
        (pavg > pl.col("high") * (1 + PRICE_EPS))
        | (pavg < pl.col("low") * (1 - PRICE_EPS))
    ).with_columns(pavg.alias("p_avg")).with_columns(
        (_is_bj()
         & (pl.col("p_avg") >= pl.col("low") * IMPLIED_LO)
         & (pl.col("p_avg") <= pl.col("high") * IMPLIED_HI)).alias("block_basis"),
    )
    dself_unexplained = dself.filter(~pl.col("block_basis"))

    label_bad = j.filter((pl.col("bars") != 240) | pl.col("miss_t")).height
    paid = j.filter(~pl.col("halted") & pl.col("dvol").is_not_null() & (pl.col("dvol") > 0))
    paid = paid.with_columns(
        (pl.col("m_all") - pl.col("dvol")).alias("e_all"),
        (pl.col("m_can") - pl.col("dvol")).alias("e_can"),
        (pl.col("a_all") - pl.col("damt")).alias("ae_all"),
    )
    # 隐含大宗价 = 多出来的那部分量额对应的成交价。量差为 0 时无解（不管额差），
    # 那种行一定落进 unexplained —— 「量一样、额不一样」不可能是一笔大宗。
    paid = paid.with_columns(
        pl.when(pl.col("e_all") != 0)
        .then((pl.col("a_all") - pl.col("damt")) / ((pl.col("m_all") - pl.col("dvol")) * 100))
        .otherwise(None)
        .alias("implied_px")
    ).with_columns(
        (_is_bj()
         & (pl.col("e_all") != 0)
         & (pl.col("implied_px") >= pl.col("low") * IMPLIED_LO)
         & (pl.col("implied_px") <= pl.col("high") * IMPLIED_HI)).alias("block_basis"),
    )
    money_bad = paid.filter(
        ((pl.col("e_all").abs() > REL_TOL * pl.col("dvol"))
         & (pl.col("e_all").abs() > ABS_TOL))
        | ((pl.col("ae_all").abs() > REL_TOL * pl.col("damt"))
           & (pl.col("ae_all").abs() > ABS_TOL * 100))
    )
    money_bad_basis = money_bad.filter(pl.col("block_basis"))

    # 两条轴对同一笔大宗必须给出同一个量：M 的隐含手数 × (1 − 隐含价/close)
    # 应当等于 B 的残差手数。920375.BJ 11-09: 4001 × (1 − 5.179/5.76) = 403.8,
    # 实测 402.8。不成立 ⇒ M 的差额并不全在 15:00 那根，那才是要红的。
    bridge = (
        money_bad_basis.select("symbol", "e_all", "implied_px", pl.col("close").alias("d_close"))
        .join(block15_basis.select("symbol", "resid_h"), on="symbol", how="inner")
        .with_columns(
            (pl.col("e_all") * (1 - pl.col("implied_px") / pl.col("d_close"))).alias("pred_resid"))
        .with_columns((pl.col("pred_resid") - pl.col("resid_h")).abs().alias("bridge_err"))
    )
    bridge_bad = bridge.filter(
        pl.col("bridge_err") > BRIDGE_TOL * pl.max_horizontal(pl.col("resid_h"), pl.lit(1.0))
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
        "money_bad_basis": money_bad_basis.height,
        "gap_bad": gap_bad.height,
        "phantom": j.filter(pl.col("dup_of_1130")).height,
        "quarantined": _quarantined_rows(data_dir, day),
        "block_1500": block15.height,
        "block_1500_basis": block15_basis.height,
        "block_1500_detail": block15.sort("resid_h", descending=True)
        .select("symbol", "close", "volume", "resid_h", "avg_px", "block_basis").to_dicts(),
        "daily_bad": dself.height,
        "daily_bad_basis": dself.height - dself_unexplained.height,
        "daily_bad_detail": dself.sort("p_avg")
        .select("symbol", "low", "high", "p_avg", "dvol", "damt", "block_basis").to_dicts(),
        "bridge_bad": bridge_bad.height,
        "bridge": bridge.sort("bridge_err", descending=True)
        .select("symbol", "e_all", "implied_px", "d_close", "pred_resid", "resid_h",
                "bridge_err").to_dicts(),
        "filler": int(halted.sum()),
        "money_bad_detail": money_bad.sort("e_all", descending=True)
        .select("symbol", "bars", "m_all", "dvol", "e_all", "m_can", "e_can", "a_all", "damt",
                "implied_px", "block_basis").to_dicts(),
        "gap_bad_detail": gap_bad.select(
            "symbol", "m_can", "dvol", "e_can", "a_can", "damt").to_dicts(),
    }


def audit_days(
    data_dir: Path,
    days: list[str],
    keep: dict[str, pl.DataFrame] | None = None,
) -> list[dict]:
    """逐日审计。**单日失败不中断整批**：那一天记 `error`，其余日照常出数。

    盘后管道用它，所以这条纪律比 CLI 更重要 —— 一个坏分区不该让整段验收消失。
    """
    cache: dict[Path, pl.DataFrame] = {}
    report: list[dict] = []
    for d in days:
        try:
            report.append(audit_day(data_dir, date.fromisoformat(d), cache, keep))
        except Exception as e:  # noqa: BLE001 — 审计是观测, 不许把观测变成故障源
            report.append({**_empty_day(date.fromisoformat(d)),
                           "error": f"{type(e).__name__}: {e}"})
    return report


def summarize(report: list[dict]) -> dict:
    """汇总（键顺序稳定；管道 result 直接吃这一份）。

    `*_basis` 与 `*_unexplained` 是**同一批明细的两个分组**，不是两批数据：
    `unexplained = 总数 − basis`。退出码只吃 `unexplained`（见模块 docstring）。
    """
    keys = ("label_bad", "money_bad", "gap_bad", "phantom", "quarantined",
            "block_1500", "daily_bad", "filler", "bridge_bad",
            "money_bad_basis", "block_1500_basis", "daily_bad_basis")
    tot: dict[str, int] = {"days": len(report)}
    for k in keys:
        tot[k] = sum(int(r.get(k) or 0) for r in report)
    # 「条」不是「只」：同一只票可以在 B 与 S 上各出现一次。
    tot["basis_rows"] = (tot["money_bad_basis"] + tot["block_1500_basis"]
                         + tot["daily_bad_basis"])
    for k in ("money_bad", "block_1500", "daily_bad"):
        tot[f"{k}_unexplained"] = max(0, tot[k] - tot[f"{k}_basis"])
    tot["days_missing_partition"] = sum(1 for r in report if not r.get("minute_partition"))
    tot["days_error"] = sum(1 for r in report if r.get("error"))
    return tot


def verdict(totals: dict) -> int:
    """退出码。3 优先于 4，4 优先于 2（见模块 docstring）。

    **只认 unexplained**：北交所大宗交易那 16 条明细进去过一次，结果是每天恒红，
    而恒红的判据等于静音。`bridge_bad` 进 3 —— 它说的是「M 的差额并不全在
    15:00 那根」，那是量额对不上，不是口径未定。
    """
    if totals.get("money_bad_unexplained") or totals.get("bridge_bad"):
        return RC_MONEY
    if totals.get("block_1500_unexplained") or totals.get("daily_bad_unexplained"):
        return RC_BLOCK
    if totals.get("label_bad"):
        return RC_LABEL
    return RC_GREEN


def repair_plan(data_dir: Path, days: list[str]) -> list[dict]:
    """幽灵行修法，两条都由**数据自身**决定，不需要知道成因。

    1. 有 11:30 且 13:00 是它的副本 → 删 13:00（重复计入，M 虚高）。
    2. 有 13:00 却没有 11:30         → 把 13:00 改回 11:30（合成网格贴错标签，
       数量仍是 240、量仍是 0，只有 L 看得见；改标后才落在规范网格上）。

    改完 L 与 M 都必须归零 —— 这就是验收，不需要相信本函数的理由。
    """
    plan: list[dict] = []
    for d in days:
        day = date.fromisoformat(d)
        m = _minute_frame(data_dir, day)
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


def apply_repair(data_dir: Path, days: list[str], plan: list[dict]) -> list[str]:
    """按日重写受影响分区。备份原文件；不备份不写。返回写过的日期。

    备份**必须落在分区目录之外**：`part.bak-<ts>.parquet` 会被 `*.parquet` 命中
    （审计侧与 `decision/bars.py::_day_frame` 都是 glob 后直接 concat），
    下一次读同一天就变成两份数据 —— 修一次、脏两次。
    """
    data_dir = Path(data_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    written: list[str] = []
    for d in days:
        items = [p for p in plan if p["day"] == d]
        if not items:
            continue
        mdir = data_dir / "kline_minute" / f"date={d}"
        files = sorted(mdir.glob("*.parquet"))
        if len(files) != 1:
            print(f"  [skip] {d}: 分区文件 {len(files)} 个，只处理单文件分区")
            continue
        src = files[0]
        bdir = data_dir / "_backup" / "minute_audit" / stamp / f"date={d}"
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
                .then(pl.col("datetime") - timedelta(minutes=90))
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
        written.append(d)
        print(f"  [write] {d}: 删 {len(del_syms)} 删行 / 改标 {len(relabel)} "
              f"→ {df.height} → {out.height} 行；备份 {bdir.relative_to(data_dir.parent)}")
    return written


def format_day_line(r: dict) -> str:
    if not r.get("minute_partition"):
        return f"[{r['day']}] 无分钟分区"
    return (
        f"[{r['day']}] 行 {r['rows']} 标的 {r['symbols']} 停牌合成 {r['filler']}"
        f" | L 标注异常 {r['label_bad']}"
        f" | M 金额异常 {r['money_bad']}(大宗口径 {r.get('money_bad_basis', 0)})"
        f" | D 归因残留 {r['gap_bad']}"
        f" | 13:00 幽灵 {r['phantom']}"
        f" | 已隔离副本 {r.get('quarantined', 0)}"
        f" | 收盘bar均价异常 {r['block_1500']}(大宗口径 {r.get('block_1500_basis', 0)})"
        f" | 日线均价越界 {r['daily_bad']}(大宗口径 {r.get('daily_bad_basis', 0)})"
        f" | 桥不一致 {r.get('bridge_bad', 0)}"
    )


def format_findings(report: list[dict], *, limit: int = 3) -> list[str]:
    """逐日一行的紧凑结论（盘后管道日志用，与 CLI 的宽表分开）。

    日志里必须带上**哪个 symbol**：只报计数时，第二天读到「M 金额异常 1」既不能
    复现也不能修；而把 6 条明细全打进日志又会淹没阶段行。

    明细只列 **unexplained**：北交所大宗口径那几条每天都有，列出来只是噪声，
    而它们的数量已经在 `(基N)` 里。
    """
    out: list[str] = []
    for r in report:
        if not r.get("minute_partition"):
            out.append(f"{r['day']} 无分钟分区")
            continue
        bits = (f"{r['day']} 行{r['rows']} L{r['label_bad']}"
                f" M{r['money_bad']}(基{r.get('money_bad_basis', 0)})"
                f" D{r['gap_bad']} 幽灵{r['phantom']}"
                f" B{r['block_1500']}(基{r.get('block_1500_basis', 0)})"
                f" S{r['daily_bad']}(基{r.get('daily_bad_basis', 0)})"
                f" 桥{r.get('bridge_bad', 0)} 停牌合成{r['filler']}")
        if r.get("error"):
            bits += f" 读取失败({r['error']})"
        names = [f"M {d['symbol']}({d['e_all']:+.0f})"
                 for d in r.get("money_bad_detail", [])[:limit]
                 if not d.get("block_basis")]
        names += [f"B {d['symbol']}({d['resid_h']:.0f}手)"
                  for d in r.get("block_1500_detail", [])[:limit]
                  if not d.get("block_basis")]
        names += [f"S {d['symbol']}(均价{d['p_avg']:.3f} 越界)"
                  for d in r.get("daily_bad_detail", [])[:limit]
                  if not d.get("block_basis")]
        names += [f"桥 {d['symbol']}(预测{d['pred_resid']:.0f}/实测{d['resid_h']:.0f})"
                  for d in r.get("bridge", [])[:limit]
                  if d.get("bridge_err", 0) > BRIDGE_TOL * max(d.get("resid_h", 0), 1.0)]
        if names:
            bits += " | " + ", ".join(names)
        out.append(bits)
    return out


def main(
    argv: list[str] | None = None,
    *,
    data_dir: Path | None = None,
    write_artifact: Callable[[dict], None] | None = None,
) -> int:
    """CLI。容器内可直接 `python -m app.services.minute_audit ...`。

    `write_artifact` 由 `analysis/` 的薄壳注入（它要过 `_artifact_guard`）；
    不注入时 `--json` 写默认路径。
    """
    ap = argparse.ArgumentParser(description="分钟 K 验收判据")
    ap.add_argument("days", nargs="*", help="交易日；省略则取盘上最新 %d 个分区" % WINDOW)
    ap.add_argument("--data-dir", default=None, help="数据根目录（默认 $DATA_DIR 或 ./data）")
    ap.add_argument("--all", action="store_true", help="扫描全部分钟分区")
    ap.add_argument("--external", action="store_true", help="追加 fuyao 外部对账")
    ap.add_argument("--json", action="store_true", help="落 _result.json")
    ap.add_argument("--repair-phantom", action="store_true", help="列出幽灵行修法")
    ap.add_argument("--apply", action="store_true", help="配合 --repair-phantom 真改（先备份）")
    args = ap.parse_args(argv)

    root = Path(args.data_dir) if args.data_dir else Path(
        data_dir or os.environ.get("DATA_DIR") or Path.cwd() / "data"
    )

    if args.all:
        days = sorted(p.name[5:] for p in (root / "kline_minute").glob("date=*"))
    else:
        days = args.days or latest_days(root)
    if not days:
        print("=> 没有分钟分区可审计")
        return RC_GREEN

    if args.repair_phantom:
        plan = repair_plan(root, days)
        print(f"幽灵行 {len(plan)} 条："
              f"删 {sum(p['action'] == 'delete_1300' for p in plan)} / "
              f"改标 {sum(p['action'] == 'relabel_1300_as_1130' for p in plan)}")
        for p in plan[:6]:
            print("   ", p["day"], p["symbol"], p["action"])
        if args.apply and plan:
            apply_repair(root, days, plan)
        elif plan:
            print("   （未加 --apply，仅计划；真改会先备份到 "
                  "data/_backup/minute_audit/<ts>/date=<day>/part.parquet）")
        return RC_GREEN

    frames: dict[str, pl.DataFrame] = {}
    report = audit_days(root, days, frames)
    totals = summarize(report)
    for r in report:
        print(format_day_line(r))
        if r.get("error"):
            print(f"      读取失败: {r['error']}")
        for d in r.get("money_bad_detail", [])[:6]:
            print(f"      M {d['symbol']} bars={d['bars']} Σall={d['m_all']:.0f} "
                  f"daily={d['dvol']:.0f} 差={d['e_all']:+.0f} "
                  f"(网格内差 {d['e_can']:+.0f}) Σamt 差={d['a_all'] - d['damt']:+.0f}")
    print(f"\n=> 汇总 {totals}")

    ext_summary: dict[str, object] = {"enabled": False, "note": "未启用"}
    if args.external:
        from app.services.minute_audit_external import external_check  # noqa: PLC0415

        ext_df, ext_note = external_check(root, frames)
        ext_summary = {"enabled": True, "note": ext_note}
        if ext_df is not None:
            got = ext_df.filter(pl.col("fvol").is_not_null())
            for key, col, scale in (("daily", "dvol", 1.0), ("min_all", "m_all", 1.0),
                                    ("min_can", "m_can", 1.0)):
                bad = got.filter(
                    ((pl.col(col) - pl.col("fvol")).abs() > REL_TOL * pl.col("fvol"))
                    & ((pl.col(col) - pl.col("fvol")).abs() > ABS_TOL * scale))
                ext_summary[f"{key}_mismatch"] = bad.height
            ext_summary["rows"] = got.height
            ext_summary["missing"] = ext_df.height - got.height
            # 明细是「哪几行本地日线 ≠ fuyao」的唯一证据；只报计数等于把可修的东西
            # 变成一句「有 4 处不一致」。
            ext_summary["daily_bad_detail"] = got.filter(
                ((pl.col("dvol") - pl.col("fvol")).abs() > REL_TOL * pl.col("fvol"))
                & ((pl.col("dvol") - pl.col("fvol")).abs() > ABS_TOL)
            ).select("day", "symbol", "dvol", "fvol", "m_all", "m_can").sort(
                "day", "symbol").to_dicts()
            print(f"   外部轴（{ext_note}）：可比 {got.height} 行，缺 {ext_summary['missing']}；"
                  f"本地日线≠fuyao {ext_summary['daily_mismatch']}、"
                  f"Σmin全量≠fuyao {ext_summary['min_all_mismatch']}、"
                  f"Σmin网格≠fuyao {ext_summary['min_can_mismatch']}")
        else:
            print(f"   外部轴不可用：{ext_note}")

    rc = verdict(totals)
    print("=> 判定:", {RC_GREEN: "全绿", RC_LABEL: "仅标注异常", RC_MONEY: "有金额差异",
                       RC_BLOCK: "有非竞价成交（量额口径）"}[rc])

    if args.json:
        nv = {"window": {"days": days}, "totals": totals, "days": report,
              "external": ext_summary}
        if write_artifact is not None:
            write_artifact(nv)
        else:
            out = root / "_audit" / "minute_audit_result.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(nv, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
            print(f"=> 产物 {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
