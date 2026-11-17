"""市场公民 —— 全市场个股的「多维流动性身份」分级与注意力预算。

为什么不是「一个成交额排名」
----------------------------
「值不值得看」不能用单一数字排序。`scripts/probe_liquidity_identity.py` 已经用
2024–25 的数据证明：成交额(≈规模×换手率)与换手率(关注度)选的不是同一批票，
两者在最近两年分道扬镳。把「流动性」压成一个标量，会得到一个「看起来权威、
实则错误」的等级。

因此这里不给单标量，而是保留三根独立轴，各算各的当日分位：
    scale   规模      = 收盘价 × 流通股本（float_shares）
    turnover 关注度   = enriched 的 turnover_rate（历史股本口径，百分数值）
    amount   成交额   = 当日成交额（元）
等级由「三轴分位的组合」映射而来，映射规则写死在 `LEVEL_RULES`，可解释、可证伪。

「注意力」的模型：等级不是静态标签，而是**时变**的 —— 用最近 N 个交易日的
滚动窗口，让「最近是否活跃」进入分位计算。一只票今天活跃、下月沉默，等级会
随之漂移。这是「注意力预算」而非「一次贴标签」的关键差异。

诚实边界：本模块只描述流动性身份，不声称等级能预测收益。样本外回测
（2005–2026，5289 日）显示等级预测的是「注意力粘性」而非收益：越沉默的票
历史超额越高（silent +606bp vs core −11bp），但事件研究 + 匹配归因
（analysis/_verify_silent_event_study.py）拆掉小盘/低流动性后，这份超额
消失甚至转负（事件后差分 CAR ≈ −134bp）——它是小盘+低流动性的投影，
不是「沉默」本身有 alpha。等级回答「它值不值得花注意力」，不是「它涨不涨」。

只读 `kline_daily_enriched` + `instruments`，不写任何数据。
"""

from __future__ import annotations

import threading
from datetime import date as date_cls
from pathlib import Path

import polars as pl

# ===== 参数 =====

# 分位窗口：恒为 1（当日横截面）。分位是「当日全市场分位」，时变性由
# 「每天重新算」天然保证，不需要读历史窗口。
WINDOW_DAYS = 1
# 分位档数（十分位）。
N_QUANTILES = 10
# 结果缓存上限：面板是百 MB 级中间态，不能随标签页数量线性吃内存。
_CACHE_MAX = 4

# ===== 等级映射 =====
#
# 「公民等级」是一个可解释的命名，落在三轴分位（0~1，1=全市场最高）的组合上。
# 规则用分位阈值表达，而不是黑盒评分：
#   - 核心：成交额前 10%（流动性绝对中心，大盘蓝筹换手低但成交额极高）
#   - 活跃：成交额前 50%，或换手率前 10%（「小而活跃」的异动票）
#   - 沉默：成交额后 10% 且换手率后 10%（注意力黑洞）
#   - 背景：其余长尾
#
# 阈值的含义写清楚，便于以后用样本外数据检验「等级是否真的预测了该不该看」。

LEVEL_RULES = {
    # 核心公民：成交额分位 ≥ 0.9（全市场成交额前 10%）。
    #   成交额是「值不值得看」的第一性判据 —— 大盘蓝筹（宁德时代/平安银行/格力）
    #   换手率天然偏低（流通盘大、周转慢），但成交额是全市场最硬的流动性信号。
    #   若要求换手率也高，会把 A 股核心蓝筹整体误判成「非核心」（实测踩过）。
    "core": {"min_amount": 0.90},
    # 活跃公民：成交额分位 ≥ 0.5，或换手率分位 ≥ 0.9（「小而活跃」的异动票）。
    #   换手率是识别「小而活跃」的补充信号：小票盘子小、换手一高就是异动。
    "active": {"min_amount": 0.50, "min_turnover": 0.90},
    # 沉默公民：成交额分位 < 0.1 且换手率分位 < 0.1。
    #   既没成交、也没人看 —— 彻底的注意力黑洞。
    "silent": {"max_amount": 0.10, "max_turnover": 0.10},
    # 兜底：不满足以上任一规则的，都是长尾「背景板」。
}

# 展示顺序：越靠前越「值得看」。silent 恒在最后。
LEVEL_ORDER = ["core", "active", "background", "silent"]

# 等级中文名与一句「为什么」。
LEVEL_LABELS = {
    "core": "核心公民",
    "active": "活跃公民",
    "background": "背景公民",
    "silent": "沉默公民",
}

LEVEL_HINTS = {
    "core": "成交额全市场前 10%，是流动性的绝对中心",
    "active": "成交活跃，或小而活跃的异动票，值得纳入观察",
    "background": "长尾背景板，没有明显活跃信号",
    "silent": "既没成交、也没人看，注意力黑洞",
}

_CACHE: dict[tuple, dict] = {}
_CACHE_LOCK = threading.Lock()


def _quantile_rank(col: str, n: int = N_QUANTILES) -> pl.Expr:
    """当日全市场的分位（0~1，1=最高）。用 ordinal rank 归一，稳定且无 NaN。"""
    return (
        (pl.col(col).rank("ordinal").over("date") - 1)
        / (pl.col(col).count().over("date") - 1).clip(lower_bound=1)
    )


def _assign_level() -> pl.Expr:
    """把三轴分位映射到公民等级。规则见 LEVEL_RULES，顺序即优先级。"""
    t, a = pl.col("p_turnover"), pl.col("p_amount")
    core = a >= LEVEL_RULES["core"]["min_amount"]
    active = (a >= LEVEL_RULES["active"]["min_amount"]) | \
             (t >= LEVEL_RULES["active"]["min_turnover"])
    silent = (a < LEVEL_RULES["silent"]["max_amount"]) & \
             (t < LEVEL_RULES["silent"]["max_turnover"])
    return (
        pl.when(core).then(pl.lit("core"))
        .when(active).then(pl.lit("active"))
        .when(silent).then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
    )


def _latest_trading_day(data_dir: Path) -> date_cls | None:
    """enriched 表的最新交易日。

    直接从 `kline_daily_enriched` 的 `date=YYYY-MM-DD` 目录名取最大日期。
    enriched 是**日分区**（非月分区），目录名即日期，不需要读 parquet。

    为什么不用 `kline_trading_days`（读 `kline_daily`）：那是月分区表，
    `list_partition_dates` 要对每个月分区 `scan_parquet` 读 date 列去重，
    容器内 9p 挂载上实测 30s。而 citizenship 只读 enriched，且只需要
    「最新一天」，单次 scandir 目录名排序即可（~ms 级）。
    """
    base = Path(data_dir) / "kline_daily_enriched"
    if not base.is_dir():
        return None
    import os

    latest: date_cls | None = None
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith("date=") or not entry.is_dir():
                    continue
                value = name[5:]
                if len(value) != 10:
                    continue
                try:
                    d = date_cls.fromisoformat(value)
                except ValueError:
                    continue
                if latest is None or d > latest:
                    latest = d
    except OSError:
        return None
    return latest


def _prev_trading_day(data_dir: Path, target: date_cls) -> date_cls | None:
    """找 target 之前最近的一个有 enriched 数据的交易日。

    一次 scandir 收集所有 `date=` 目录名并排序，取比 target 小的最大日期。
    复杂度与 `_latest_trading_day` 相同（~ms 级，只读目录名不读 parquet）。
    """
    base = Path(data_dir) / "kline_daily_enriched"
    if not base.is_dir():
        return None
    import os

    prev: date_cls | None = None
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith("date=") or not entry.is_dir():
                    continue
                value = name[5:]
                if len(value) != 10:
                    continue
                try:
                    d = date_cls.fromisoformat(value)
                except ValueError:
                    continue
                if d < target and (prev is None or d > prev):
                    prev = d
    except OSError:
        return None
    return prev


def _resolve_as_of(data_dir: Path, as_of: date_cls | None) -> date_cls | None:
    """把用户指定的日期解析为「有 enriched 数据的最近交易日」。

    用户可能输入非交易日（周末/节假日）：该日没有分区，直接返回空会让页面
    显示「暂无数据」，令人困惑。这里向前回溯（最多 10 个自然日）到最近一个有
    数据的交易日。`as_of=None` 时用最新交易日。
    """
    if as_of is None:
        return _latest_trading_day(data_dir)
    base = Path(data_dir) / "kline_daily_enriched"
    for back in range(10):
        candidate = date_cls.fromordinal(as_of.toordinal() - back)
        if (base / f"date={candidate.isoformat()}").is_dir():
            return candidate
    return None


def compute(
    data_dir: Path,
    as_of: date_cls | None = None,
) -> dict:
    """计算全市场公民等级 + 注意力预算汇总。

    只读**一个交易日**的横截面（分位是「当日全市场分位」），不读历史窗口：
      - 分位只需要当日横截面即可客观计算（ordinal rank），与历史无关；
      - 读历史窗口的唯一理由曾是「过滤样本太薄的票」，但那是伪需求——今天
        有成交、有换手率的票，其当日分位就是客观的，不需要窗口 bar 数背书；
      - 读 120 天窗口在 9p 挂载上要遍历 5310 个分区目录（实测容器内 ~27s），
        单日直读只需 1 个文件。

    「时变性」由「每天重新算当日分位」天然保证：今天活跃的票，明天的分位会变。

    返回结构：
      as_of           实际使用的交易日
      summary         注意力预算（各等级数量 / 占比）
      rows            每只票一行：symbol、name、三轴分位、等级
      window_days     分位窗口（恒为 1，表示「当日横截面」）
      disclaimer      诚实边界声明
    """
    latest = _resolve_as_of(data_dir, as_of)
    if latest is None:
        return _empty()

    inst = _instruments(data_dir)

    # 只读一个交易日的横截面（单个分区文件，不遍历目录）。_resolve_as_of
    # 已保证该分区存在，故这里直接读，不再需要 load_panel 降级。
    day_file = Path(data_dir) / "kline_daily_enriched" / f"date={latest.isoformat()}" / "part.parquet"
    day = pl.read_parquet(
        day_file,
        columns=["symbol", "date", "close", "amount", "turnover_rate", "volume"],
    )

    df = day.join(inst, on="symbol", how="left").filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0)
        & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
    )

    # 流通市值（元）= 收盘价 × 流通股本。先算 mcap，再据此做三轴分位。
    df = df.with_columns(
        (pl.col("close") * pl.col("float_shares")).alias("mcap")
    )

    # 三轴分位（当日全市场，1=最高）。
    df = df.with_columns([
        _quantile_rank("mcap").alias("p_scale"),
        _quantile_rank("turnover_rate").alias("p_turnover"),
        _quantile_rank("amount").alias("p_amount"),
    ]).with_columns(_assign_level().alias("level"))

    # 等级迁移：读前一交易日，用**前一交易日自己的横截面**算 prev_level。
    # 迁移是「被重新关注(升格)/被抛弃(降格)」的前瞻信号（见 docs/citizenship-transition-signal.md），
    # 降格尤其稳健——是给持仓者的减仓/回避提示。
    prev = _prev_trading_day(data_dir, latest)
    transition_expr = pl.lit("stay").cast(pl.Utf8).alias("transition")
    prev_level_expr = pl.lit(None, dtype=pl.Utf8).alias("prev_level")
    if prev is not None:
        prev_file = Path(data_dir) / "kline_daily_enriched" / f"date={prev.isoformat()}" / "part.parquet"
        prev_day = pl.read_parquet(
            prev_file,
            columns=["symbol", "date", "close", "amount", "turnover_rate"],
        )
        prev_df = prev_day.join(inst, on="symbol", how="left").filter(
            (pl.col("close") > 0) & (pl.col("amount") > 0)
            & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
        ).with_columns(
            (pl.col("close") * pl.col("float_shares")).alias("mcap")
        )
        prev_df = prev_df.with_columns([
            _quantile_rank("mcap").alias("p_scale"),
            _quantile_rank("turnover_rate").alias("p_turnover"),
            _quantile_rank("amount").alias("p_amount"),
        ]).with_columns(_assign_level().alias("prev_level"))
        prev_level_map = prev_df.select(["symbol", "prev_level"])

        df = df.join(prev_level_map, on="symbol", how="left")

        # 等级序号：core=0 < active=1 < background=2 < silent=3，序号越小越「高」。
        # 升格 = 序号变小，降格 = 序号变大。用向量化 replace 而非逐行 map_elements。
        order = {lv: i for i, lv in enumerate(LEVEL_ORDER)}
        df = df.with_columns([
            pl.col("level").replace_strict(order, default=99).alias("_lvl_rank"),
            pl.col("prev_level").replace_strict(order, default=99).alias("_prev_rank"),
        ])
        transition_expr = (
            pl.when(pl.col("prev_level").is_null())
            .then(pl.lit("new"))
            .when(pl.col("_prev_rank") > pl.col("_lvl_rank"))
            .then(pl.lit("up"))
            .when(pl.col("_prev_rank") < pl.col("_lvl_rank"))
            .then(pl.lit("down"))
            .otherwise(pl.lit("stay"))
            .alias("transition")
        )
        prev_level_expr = pl.col("prev_level")
    else:
        # 无前一交易日（数据只有一天），全部标记为 new
        transition_expr = pl.lit("new").cast(pl.Utf8).alias("transition")

    df = df.with_columns(prev_level_expr, transition_expr)

    rows = (
        df.select([
            "symbol", "name", "level",
            "mcap", "turnover_rate", "amount",
            "p_scale", "p_turnover", "p_amount",
            "prev_level", "transition",
        ])
        .sort(["level", "amount"], descending=[False, True])
    )

    summary = _summary(rows)

    return {
        "as_of": latest.isoformat(),
        "prev_as_of": prev.isoformat() if prev else None,
        "window_days": WINDOW_DAYS,
        "summary": summary,
        "rows": rows.to_dicts(),
        "disclaimer": (
            "等级描述流动性身份（规模 / 关注度 / 成交额），不预测收益。"
            "「沉默」在 A 股历史上确实跑出过更高超额，但事件研究（匹配归因）"
            "拆掉小盘/低流动性后，这份超额消失甚至转负——它是小盘+低流动性"
            "的投影，不是「沉默」本身有 alpha。等级回答「值不值得花注意力」，"
            "不是「该不该买」。"
        ),
    }


def _instruments(data_dir: Path) -> pl.DataFrame:
    """读标的维表，取 symbol/name/float_shares。缺列时降级为空，不阻断。"""
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return pl.DataFrame(schema={"symbol": pl.Utf8, "name": pl.Utf8, "float_shares": pl.Float64})
    cols = pl.read_parquet_schema(path)
    keep = [c for c in ("symbol", "name", "float_shares") if c in cols]
    if "symbol" not in keep:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "name": pl.Utf8, "float_shares": pl.Float64})
    df = pl.read_parquet(path, columns=keep)
    if "name" not in keep:
        df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("name"))
    if "float_shares" not in keep:
        df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("float_shares"))
    return df


def _summary(rows: pl.DataFrame) -> dict:
    """注意力预算：各等级数量与占比，「值得看」的边界，以及迁移计数。"""
    if rows.is_empty():
        return {"levels": [], "total": 0, "attention_top": 0,
                "transitions": {"up": 0, "down": 0, "stay": 0, "new": 0}}
    counts = rows.group_by("level").len().rename({"len": "count"})
    total = rows.height
    levels = []
    order = {lvl: i for i, lvl in enumerate(LEVEL_ORDER)}
    for row in counts.iter_rows(named=True):
        lvl = row["level"]
        levels.append({
            "level": lvl,
            "label": LEVEL_LABELS.get(lvl, lvl),
            "hint": LEVEL_HINTS.get(lvl, ""),
            "count": row["count"],
            "share": round(row["count"] / total, 4),
        })
    # 按 LEVEL_ORDER 稳定排序（Polars 内不依赖版本敏感 API）。
    levels.sort(key=lambda x: order.get(x["level"], 99))
    # 「值得投入注意力」= core + active。
    attention_top = sum(
        l["count"] for l in levels if l["level"] in ("core", "active")
    )
    # 迁移计数：降格(被抛弃)/升格(被关注)/停留/新进。降格是风控提示（见
    # docs/citizenship-transition-signal.md，跨周期 t≈-13~-20）。
    transitions = {"up": 0, "down": 0, "stay": 0, "new": 0}
    if "transition" in rows.columns:
        for key, n in rows.group_by("transition").len().iter_rows():
            if key in transitions:
                transitions[str(key)] = int(n)
    return {"levels": levels, "total": total, "attention_top": attention_top,
            "transitions": transitions}


def _empty(as_of: date_cls | None = None) -> dict:
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "prev_as_of": None,
        "window_days": WINDOW_DAYS,
        "summary": {"levels": [], "total": 0, "attention_top": 0,
                    "transitions": {"up": 0, "down": 0, "stay": 0, "new": 0}},
        "rows": [],
        "disclaimer": "",
    }


def get_citizenship(data_dir: Path, as_of: date_cls | None = None) -> dict:
    """带缓存的入口。缓存键 = (data_dir, as_of)。"""
    key = (str(data_dir), as_of.isoformat() if as_of else None)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    result = compute(data_dir, as_of)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = result
    return result


def history_summary(data_dir: Path, days: int = 30, end: date_cls | None = None) -> dict:
    """近 N 个交易日的注意力预算时序（等级分布 + 迁移计数），供前端画趋势。

    只取每天的 summary（等级数量/占比 + transitions），不返回逐票 rows，
    因此每「日」仍要一次 compute（读当日+前一日两个分区），N 天 = 2N 次单文件读，
    9p 挂载上每文件 ~ms 级，30 天可接受。这是「历史回看」的轻量形态：
    看的是「注意力结构如何演变」，不需要逐票明细。
    """
    from datetime import timedelta

    days = max(1, min(days, 120))
    latest = _resolve_as_of(data_dir, end)
    if latest is None:
        return {"series": [], "days": days}

    series: list[dict] = []
    cursor = latest
    for _ in range(days):
        # 每天算 summary，只保留轻量字段
        result = compute(data_dir, cursor)
        summ = result.get("summary", {})
        series.append({
            "as_of": result.get("as_of"),
            "total": summ.get("total", 0),
            "attention_top": summ.get("attention_top", 0),
            "levels": summ.get("levels", []),
            "transitions": summ.get("transitions", {}),
        })
        prev = _prev_trading_day(data_dir, cursor)
        if prev is None:
            break
        cursor = prev

    series.reverse()  # 升序（最早在前）
    return {"series": series, "days": days}
