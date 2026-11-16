"""横截面 alpha 统计链 — 把「策略信号」换算成 eval_protocol.Claim 所需的统计量。

这是策略自动捕获 S4 段的核心。解决的问题: 回测引擎产出组合绩效(净值/回撤/夏普),
但 eval_protocol 五道门要的是横截面超额统计 (effect_bp / t_stat / per_day_t /
mde_bp / matched_effect_bp)。本模块从「策略在历史每个交易日选出的信号股票集合」出发,
套用 docs/youzi-strategy-audit.md 与 analysis/_diag_signal_to_product.py 确立的口径,
算出可喂给五道门的统计量。

口径(与 _diag_signal_to_product.py 逐位对齐, 这是判据的一部分):
  - 收益 r{h}: T 日收盘可知信号 → T+1 开盘买 → T+h 收盘卖 (复权价 close/open)
  - 超额: 扣「同日全市场等权」基准 (x{h}); 同涨幅箱基准 (c{h}) 单独算, 不混
  - t 检验: 按交易日聚合 (同一天多只股票先等权成一条组合收益), 再对日度序列做 t;
            se = sd / sqrt(n_eff), n_eff = max(n / h, 1)  (HAC 重叠窗口校正)
  - mde = 2 * sd / sqrt(n_eff)

设计边界:
  - 本模块只做「信号 → 统计量」, 不持有策略引擎/数据目录单例, 依赖由调用方注入。
  - 信号来源是一个 DataFrame: 每行 (date, symbol), 表示「该日该股被策略选中」。
    这个 DataFrame 由调用方(采集管道)通过逐日跑策略得到。
  - 本模块不判断「信号怎么来的」, 只负责「信号 → 超额统计」这一段的正确性。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import polars as pl


def _board_of(symbol: str) -> str:
    """从 symbol 代码推断板块(与 abnormal_moves.board_of 同口径: 主板/创业板/科创板/北交所)。"""
    code = symbol.split(".")[0]
    if symbol.endswith(".BJ") or code[:2] in {"43", "83", "87", "92"}:
        return "北交所"
    if code.startswith("68"):
        return "科创板"
    if code.startswith(("30", "301")):
        return "创业板"
    return "主板"


def _limit_pct_of(symbol: str) -> float:
    """涨停幅度(小数): 主板 10%、创业板/科创板 20%、北交所 30%。

    与 analysis/_diag_signal_to_product.py::limit_pct_of 同口径。
    """
    code = symbol.split(".")[0]
    if code[:3] in ("300", "301", "302", "688", "689"):
        return 0.20
    if code[:3] == "920":
        return 0.30
    return 0.10


def _tstat_by_day(sub: pl.DataFrame, col: str, h: int) -> dict:
    """对已含超额列的信号子集做按交易日聚合的 t 检验。

    Args:
        sub: 含 date 与 col 的 DataFrame (col 是超额收益, 小数制)
        h: 持有天数 (用于 HAC 重叠窗口校正 n_eff = n / h)
    Returns:
        {n, days, mean_bp, t, sd_daily_bp, n_eff, mde_bp}
    """
    sel = sub.select(["date", col]).drop_nulls()
    if sel.height == 0:
        return {"n": 0}
    daily = sel.group_by("date").agg(pl.col(col).mean().alias("v"))
    s = daily["v"]
    n = len(s)
    mean = s.mean()
    sd = s.std()
    # n == 1 时标准差无定义 (polars std of single value = None): 无法算 t / mde
    if n <= 1 or sd is None:
        return {
            "n": int(sel.height),
            "days": n,
            "mean_bp": round(mean * 1e4, 1),
            "t": None,
            "sd_daily_bp": None,
            "n_eff": None,
            "mde_bp": None,
        }
    neff = max(n / h, 1.0)
    se = sd / math.sqrt(neff)
    return {
        "n": int(sel.height),
        "days": n,
        "mean_bp": round(mean * 1e4, 1),
        "t": round(mean / se, 2) if se > 0 else None,
        "sd_daily_bp": round(sd * 1e4, 1),
        "n_eff": round(neff, 1),
        "mde_bp": round(2 * sd / math.sqrt(neff) * 1e4, 1),
    }


@dataclass
class AlphaEstimate:
    """一个信号在某个持有期、某个基准口径下的横截面 alpha 估计。"""
    h: int
    benchmark: str  # "market" (同日全市场等权) | "bucket" (同涨幅箱)
    n_signals: int
    n_days: int
    mean_bp: float | None
    t_stat: float | None
    mde_bp: float | None
    sd_daily_bp: float | None
    n_eff: float | None


@dataclass
class AlphaResult:
    """对一个策略信号的全套横截面 alpha 统计。"""
    strategy_id: str
    signals: list[dict] = field(default_factory=list)  # 原始 (date, symbol) 命中
    estimates: list[AlphaEstimate] = field(default_factory=list)
    #: 入场不可成交占比 (0~1): 次日开盘仍触及涨停(一字/接近涨停)买不进的比例。
    #: 由 buyable 逻辑算出, 不是硬传 0.0。
    unfillable_share: float | None = None
    #: 信号总数(含可买与不可买), 供触发率等口径使用
    n_signals_total: int = 0
    #: 可买入的信号数(次日开盘 < 涨停价 - 0.5%)
    n_signals_buyable: int = 0

    def estimate(self, h: int, benchmark: str) -> AlphaEstimate | None:
        for e in self.estimates:
            if e.h == h and e.benchmark == benchmark:
                return e
        return None


def compute_signal_alpha(
    signals: pl.DataFrame,
    daily: pl.DataFrame,
    *,
    holding_days: tuple[int, ...] = (1, 2, 5),
) -> AlphaResult:
    """核心: 从信号集合 + 全市场日线, 算横截面超额统计。

    Args:
        signals: 每行 (date, symbol), 表示该日该股被策略选中。
                 只应含「T 日收盘可知」的信号 (调用方保证)。
        daily: 全市场日线 enriched, 需含列:
            symbol, date, open, close, raw_close (原始收盘, 用于算涨跌幅与同涨幅箱)
            以及「下一交易日」信息。本模块内部计算 next_open / next close。
            数据须按 symbol 排序且每 symbol 内 date 连续(交易日序)。
    Returns:
        AlphaResult, 含 h ∈ holding_days × benchmark ∈ {market, bucket} 的估计。
    """
    # 1. 构造未来收益: T 日 → T+1 开盘买 → T+h 收盘卖 (复权价)
    # 统一 date 为 Utf8: 真实 enriched 的 date 是 date 类型, trades 提取的信号是 str,
    # 直接 join 会 SchemaError。这里统一成字符串, 与调用方无关。
    df = daily.sort(["symbol", "date"]).with_columns(
        pl.col("date").cast(pl.Utf8).alias("date"),
    ).with_columns(
        pl.col("raw_close").shift(1).over("symbol").alias("prev_raw_close"),
        pl.col("date").shift(-1).over("symbol").alias("next_date"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
    )
    for h in holding_days:
        df = df.with_columns(
            pl.col("close").shift(-h).over("symbol").alias(f"close_{h}")
        )

    # 交易日历: 只保留「下一行就是下一交易日」的观测(停牌 shift 会跨多日)
    cal = df["date"].unique().sort().to_list()
    nxt = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
    df = df.with_columns(
        pl.col("date").replace_strict(nxt, default=None).alias("cal_next")
    ).filter(pl.col("next_date") == pl.col("cal_next"))

    # 涨跌幅(用于同涨幅箱基准)与未来收益
    df = df.with_columns(
        (pl.col("raw_close") / pl.col("prev_raw_close") - 1).alias("pct"),
    )
    # 入场可成交性: 次日开盘价相对昨收的涨幅, 与涨停幅度比较。
    # buyable = 次日开盘 < 涨停价 - 0.5% (即开盘不触及涨停, 开盘价可成交)。
    # 这是 open_t1 口径下 entry_unfillable_share 的正确来源:
    # 开盘买入的「买不进」只有一种情况——次日开盘仍(接近)一字涨停。
    df = df.with_columns(
        pl.col("symbol").map_elements(_limit_pct_of, return_dtype=pl.Float64).alias("lp"),
    ).with_columns(
        (pl.col("next_open") / pl.col("prev_raw_close") - 1).alias("next_open_pct"),
    ).with_columns(
        (pl.col("next_open_pct") < pl.col("lp") - 0.005).alias("buyable"),
    )
    # 板块维度: 从 symbol 代码推断(与 abnormal_moves.board_of 同口径), 供 board 匹配
    df = df.with_columns(
        pl.col("symbol").map_elements(
            lambda s: _board_of(s), return_dtype=pl.Utf8,
        ).alias("board"),
    )
    for h in holding_days:
        df = df.with_columns(
            (pl.col(f"close_{h}") / pl.col("next_open") - 1).alias(f"r{h}")
        )

    # 2. 超额: 扣同日全市场等权 (x{h}); 同涨幅箱 (c{h}, 0.5% 分箱);
    #    同板块+同涨幅箱 (b{h}, board × 0.5% 分箱) — 这是 youzi 审计补记确立的正确口径
    df = df.with_columns((pl.col("pct") / 0.005).round().cast(pl.Int32).alias("bucket"))
    for h in holding_days:
        mkt = df.group_by("date").agg(pl.col(f"r{h}").mean().alias(f"m{h}"))
        df = df.join(mkt, on="date", how="left").with_columns(
            (pl.col(f"r{h}") - pl.col(f"m{h}")).alias(f"x{h}")
        )
        # 同涨幅箱(仅 pct)
        bm = df.group_by(["date", "bucket"]).agg(
            pl.col(f"r{h}").mean().alias(f"bm{h}"), pl.len().alias(f"bn{h}")
        )
        df = df.join(bm, on=["date", "bucket"], how="left").with_columns(
            pl.when(pl.col(f"bn{h}") >= 5)
            .then(pl.col(f"r{h}") - pl.col(f"bm{h}"))
            .otherwise(None)
            .alias(f"c{h}")
        )
        # 同板块 + 同涨幅箱
        bb = df.group_by(["date", "board", "bucket"]).agg(
            pl.col(f"r{h}").mean().alias(f"bbm{h}"), pl.len().alias(f"bbn{h}")
        )
        df = df.join(bb, on=["date", "board", "bucket"], how="left").with_columns(
            pl.when(pl.col(f"bbn{h}") >= 5)
            .then(pl.col(f"r{h}") - pl.col(f"bbm{h}"))
            .otherwise(None)
            .alias(f"b{h}")
        )

    # 3. 信号命中 → join 到 df, 取超额
    # 空信号: 返回每个 (h, benchmark) 均为 n_signals=0 的空估计, 让调用方能区分
    # 「无信号」与「有信号但算不出」(后者才是真错误)。
    if signals is None or signals.height == 0:
        result = AlphaResult(strategy_id="", signals=[])
        for h in holding_days:
            for bench in ("market", "bucket", "board_bucket"):
                result.estimates.append(AlphaEstimate(
                    h=h, benchmark=bench, n_signals=0, n_days=0,
                    mean_bp=None, t_stat=None, mde_bp=None,
                    sd_daily_bp=None, n_eff=None,
                ))
        return result

    # 信号 date 列统一 Utf8 (与上面 daily 的 date cast 对齐)
    sig = (
        signals.select(
            pl.col("date").cast(pl.Utf8),
            pl.col("symbol").cast(pl.Utf8),
        ).unique()
    )
    joined = sig.join(df, on=["date", "symbol"], how="inner")

    result = AlphaResult(strategy_id="", signals=joined.select(["date", "symbol"]).to_dicts())

    # 入场不可成交占比: 信号中「次日开盘仍触及涨停」的比例。
    # open_t1 口径下, 买不进 = 次日开盘仍(接近)一字涨停, 由 buyable 列判断。
    result.n_signals_total = joined.height
    if joined.height > 0 and "buyable" in joined.columns:
        result.n_signals_buyable = int(joined["buyable"].fill_null(True).sum())
        result.unfillable_share = 1.0 - result.n_signals_buyable / result.n_signals_total
    else:
        result.unfillable_share = None

    for h in holding_days:
        for bench, col in (("market", f"x{h}"), ("bucket", f"c{h}"), ("board_bucket", f"b{h}")):
            stat = _tstat_by_day(joined, col, h)
            result.estimates.append(AlphaEstimate(
                h=h,
                benchmark=bench,
                n_signals=stat.get("n", 0),
                n_days=stat.get("days", 0),
                mean_bp=stat.get("mean_bp"),
                t_stat=stat.get("t"),
                mde_bp=stat.get("mde_bp"),
                sd_daily_bp=stat.get("sd_daily_bp"),
                n_eff=stat.get("n_eff"),
            ))
    return result


def compute_capacity_curve(
    data_dir,
    strategy_id: str,
    *,
    max_positions: tuple[int, ...] = (5, 20, 100),
    start=None,
    end=None,
    asset_type: str = "stock",
) -> dict[int, float] | None:
    """跑容量曲线: {max_positions → 年化收益(%)}, 供 eval_protocol G2 判「选择偏差」。

    复用 backtest worker(spawn 子进程), 对每个 max_positions 各跑一次回测,
    取 stats["annual_return"](比例, 如 0.484=48.4%)×100 转成百分比数值。

    口径: capacity_annual_pct 的语义是「换一种选股容量, 年化掉多少」, 与
    youzi-strategy-audit.md 的 weak_board_absorption +48.4%(10) vs +1.9%(100) 同一件事。

    Returns:
        {max_positions: annual_pct}, 只含成功跑出来的档位; 全失败返回 None
        (调用方据此标 failed, 不强造数字)。
    """
    from datetime import date

    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.market_time import cn_today
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    end_date = end if isinstance(end, date) else date.fromisoformat(end) if end else cn_today()
    start_date = (
        start if isinstance(start, date)
        else date.fromisoformat(start) if start
        else end_date - __import__("datetime").timedelta(days=180)
    )

    curve: dict[int, float] = {}
    for mp in max_positions:
        cfg = StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=None,
            start=start_date,
            end=end_date,
            max_positions=mp,
            asset_type=asset_type,
        )
        try:
            with shared_heavy_job_limiter.slot("normal"):
                task = make_worker_task("backtest", data_dir, cfg)
                result = run_worker_task(task)
        except Exception as exc:  # noqa: BLE001 — 单个容量档失败只跳过, 不中断整条曲线
            logger = __import__("logging").getLogger(__name__)
            logger.warning("capacity curve mp=%s failed: %s", mp, exc)
            continue
        stats = result.get("stats") or {}
        if "annual_return" in stats:
            curve[mp] = round(float(stats["annual_return"]) * 100, 2)
    return curve or None


def build_claim(
    strategy_id: str,
    result: AlphaResult,
    *,
    universe: str = "all_a",
    entry: str = "open_t1",
    exit: str = "close_t2",
    h: int = 2,
    benchmark: str = "bucket",
    event_type: str = "volume_shock",
    n_tests: int = 200,
    cost_bp_roundtrip: float = 20.0,
    n_stock_days: int | None = None,
    capacity_annual_pct: dict[int, float] | None = None,
    entry_unfillable_share: float | None = None,
) -> dict:
    """把 AlphaResult 折算成 eval_protocol.Claim 所需字段(返回 dict, 不直接 import 协议)。

    关键诚实标注: 本函数产出的 effect_bp / t_stat / mde_bp / n_tests / annual_net_pct
    是**自动推导**的, matched_effect_bp 用同涨幅箱基准近似(不是真正的一对一匹配),
    这些都在 provenance 里标 auto_derived=True, 由采集管道落盘供人工复核。

    Args:
        h: 收益持有期 h (r{h} = T+1 开盘买 → T+h 收盘卖); 与 (entry, exit) 推导天数对应。
           注意: entry=open_t1 / exit=close_t2 ⇒ 推导持有 1 个交易日, 对应 h=2 的 r2
           (T+1 开盘 → T+2 收盘), 二者是同一个持有期的两种表达, 不能都当「h」填。
        benchmark: 用哪个超额口径做 effect_bp (market=全市场等权, bucket=同涨幅箱)
        event_type: 必须命中 eval_protocol.EVENT_REGISTRY; 且 benchmark_dims 必须覆盖
            该事件的 required = KNOWN_CONFOUNDS - defines, 否则 G3 恒红(这是正确行为)。
            默认 volume_shock(defines={volume_shock}) ⇒ required={board,pct,turnover},
            配 bucket 口径只控 {pct} 不够 —— 调用方须传 n_stock_days 与完整 dims。
        n_stock_days: 全市场股票日总数(触发率分母)。缺省时 annual_net_pct 置 None
            (→ G5 economy_unassessed 红, 诚实拒绝, 不强算)。
    """
    est = result.estimate(h, benchmark)
    if est is None:
        raise ValueError(f"缺少 h={h} benchmark={benchmark} 的估计")
    effect_bp = est.mean_bp
    t_stat = est.t_stat
    mde_bp = est.mde_bp
    n_days = est.n_days
    n_signals = est.n_signals

    # 入场不可成交占比: 优先用 AlphaResult 算出的 buyable 口径,
    # 缺省时回退调用方显式传入的值 (None 时协议判 unfillable_share_missing, 诚实拒绝)。
    unfillable = result.unfillable_share if result.unfillable_share is not None else entry_unfillable_share

    # benchmark → (benchmark 字段名, 控制的混杂维度)
    # market: 全市场等权, 不控任何维度(会触发 G3 unmatched_benchmark, 诚实)
    # bucket: 控 pct; board_bucket: 控 board + pct
    if benchmark == "board_bucket":
        benchmark_field = "matched"
        dims = frozenset({"board", "pct"})
    elif benchmark == "bucket":
        benchmark_field = "matched"
        dims = frozenset({"pct"})
    else:
        benchmark_field = "market_equal"
        dims = frozenset()

    # 触发率 = 信号命中数 / 全市场股票日数; 只有给了分母才算年化, 否则诚实留空
    trigger_rate = (n_signals / n_stock_days) if n_stock_days else None
    hits_per_year = None
    annual_gross_pct = None
    annual_net_pct = None
    if trigger_rate is not None and effect_bp is not None:
        hits_per_year = 242 * trigger_rate
        # 年化毛贡献 = 242 × 触发率 × 单次效应(bp→比例)
        annual_gross_pct = hits_per_year * effect_bp / 1e4 * 100
        # 年化净 = 毛 − 242 × 触发率 × 往返成本
        annual_net_pct = annual_gross_pct - hits_per_year * cost_bp_roundtrip / 1e4 * 100

    # 持有天数: 从 (entry, exit) 推导, 保证与成交价口径自洽(不会 mismatch)。
    # open_t1→close_t2 = 1 个交易日。协议要求显式给, 且必须与推导值一致。
    try:
        from app.strategy.eval_protocol import holding_days_between as _hdb
        holding_days = _hdb(entry, exit)
    except Exception:  # noqa: BLE001 — 推导失败则留 None, 协议会判 holding_days_missing
        holding_days = None

    claim = {
        "name": strategy_id,
        "universe": universe,
        "entry": entry,
        "exit": exit,
        "holding_days": holding_days,
        "agg_unit": "per_day",
        "per_day_t": t_stat,
        "effect_bp": effect_bp,
        "t_stat": t_stat,
        "n_obs": n_signals,
        "n_days": n_days,
        "benchmark": benchmark_field,
        "benchmark_dims": dims,
        "event_type": event_type,
        "matched_effect_bp": effect_bp,
        "matched_t": t_stat,
        "mde_bp": mde_bp,
        "mde_universe": universe,
        "n_tests": n_tests,
        "cost_bp_roundtrip": cost_bp_roundtrip,
        "trigger_rate_per_stock_day": trigger_rate,
        "hits_per_stock_year": hits_per_year,
        "annual_gross_pct": annual_gross_pct,
        "annual_net_pct": annual_net_pct,
        "capacity_annual_pct": capacity_annual_pct,
        "entry_unfillable_share": unfillable,
        "provenance": {
            "auto_derived": "true",
            "source": "cross_section_alpha",
            "benchmark": benchmark,
            "h": str(h),
            "unfillable_source": "buyable(次日开盘<涨停价-0.5%)" if result.unfillable_share is not None else "caller",
            "note": "effect_bp/t/mde/annual_net 由信号横截面超额自动推导, matched 用同涨幅箱近似, 未经人工复核",
        },
    }
    return claim
