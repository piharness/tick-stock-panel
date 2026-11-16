"""对手盘模型 — 把「今天谁在和我做对手」做成可证伪的对象模型，而不是讲行业故事。

为什么要有这个模块
------------------
「博弈链路」页原来按时间顺序摆六段数据（环境→结构→主线→资金→竞价→结算），
它回答的是「今天发生了什么」。但读者真正要的是「对面是谁、他的约束有多硬、
我能不能看到他的动作」。这两个问题不能靠散文回答——散文无法被证伪，因此
永远正确、也永远没用。

本模块只做一件事：**把每个参与者对象落到一个能被本地数据观测（或明确观测
不到）的痕迹上**，并对「跟买能不能赚钱」这一条给出带样本量、带 t、带对照组的
结论。每一条结论都可以用 `python -m app.services.table_game --verify` 重算。

口径纪律（三条，违反任何一条结论就作废）
----------------------------------------
1. **匹配对照**：龙虎榜上榜股天然是高换手高波动票，直接用「相对全市场等权」
   会把波动率溢价算成席位技能。这里按 **T 日涨跌幅十分位 × 换手率五分位**
   分桶，用同日同桶的非上榜股做对照。分桶变量全部在 T 日收盘时已知，
   没有前视。自检判据：非上榜股的匹配超额必须 ≈ 0（实测 -0.24bp）。
2. **可成交**：收益口径是「T+1 开盘买 → T+1 收盘卖」，且单独剔除 T+1 开盘
   即涨停（一字板，买不进）的样本。不做这一步的正超额是 paper alpha。
3. **观测 / 推算 / 无数据**三态必须显式：本地没有十大股东、大宗交易、减持公告、
   质押、ETF 份额五张表，因此「国家队 / 社保 / 汇金 / 保险 / 公募 / 私募 /
   上市股东」四个对象的持仓动向在本仓库里**不可观测**。页面必须这么写，
   而不是用「机构专用」席位冒充它们——那是一个混合桶。

数据来源（只读，不新增采集）
--------------------------
- kline_daily_enriched：日线（复权 open/close + 原始 volume/amount + turnover_rate）
- dragon_tiger/date=*.json：242 个交易日的龙虎榜（1 日榜 / 3 日榜 / 游资席位）
- instruments.parquet：名称、流通股本
- financials/metrics：ROE（仅用于壳价值代理）
"""

from __future__ import annotations

import json
import math
import threading
from datetime import date as date_cls
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.trading_days import kline_trading_days

# 回看窗口：一年。少于 200 日会让「分位」和「匹配桶」都不稳。
LOOKBACK_DAYS = 250
# 匹配桶：涨跌幅十分位 × 换手率五分位 = 50 桶/日
CHG_BUCKETS = 10
TURN_BUCKETS = 5
# 一字板判定：T+1 开盘涨幅 ≥ 9.8% 视为买不进（主板 10% 涨停，留 20bp 容差）
LIMIT_GAP = 0.098
# 席位排行最小样本：低于这个数不进榜（n=30 的均值没有意义）
SEAT_MIN_N = 80

_CACHE: dict[tuple[str, str, int], dict] = {}
_CACHE_LOCK = threading.Lock()
# 面板与龙虎榜是全页最贵的一步（250 个分区 + 242 个 JSON，9p 上实测 6.5s）。
# 名册、扫描层与个股实例共用同一份结果，否则每换一只票就重读一遍。
_PANEL_CACHE: dict[tuple[str, str], pl.DataFrame] = {}
_EVENTS_CACHE: dict[tuple[str, str], tuple[pl.DataFrame, pl.DataFrame]] = {}
# 匹配帧 + 账本：名册与扫描层都要，且都只读，算一次即可。
_CORE_CACHE: dict[tuple[str, str], tuple[pl.DataFrame, pl.DataFrame, dict]] = {}


def _panel_cached(data_dir: Path, as_of: date_cls) -> pl.DataFrame:
    key = (str(data_dir), as_of.isoformat())
    with _CACHE_LOCK:
        hit = _PANEL_CACHE.get(key)
    if hit is not None:
        return hit
    frame = _load_panel(Path(data_dir), as_of)
    with _CACHE_LOCK:
        _PANEL_CACHE[key] = frame
    return frame


def _events_cached(data_dir: Path, as_of: date_cls) -> tuple[pl.DataFrame, pl.DataFrame]:
    key = (str(data_dir), as_of.isoformat())
    with _CACHE_LOCK:
        hit = _EVENTS_CACHE.get(key)
    if hit is not None:
        return hit
    pair = _load_events(Path(data_dir), as_of)
    with _CACHE_LOCK:
        _EVENTS_CACHE[key] = pair
    return pair


def _core_cached(data_dir: Path, as_of: date_cls) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """面板 / 匹配帧 / 账本 —— 名册与扫描层共用，只算一次。

    没有这层缓存时，扫描层会让「打开页面」多花一次 _attach_matched（1.4M 行
    的分桶 join）。两个接口读同一份数字，就不该各算一遍。
    """
    key = (str(data_dir), as_of.isoformat())
    with _CACHE_LOCK:
        hit = _CORE_CACHE.get(key)
    if hit is not None:
        return hit
    panel = _panel_cached(data_dir, as_of)
    events, seats = _events_cached(data_dir, as_of)
    matched = _attach_matched(panel, events)
    ledger = _ledger(matched, seats)
    with _CACHE_LOCK:
        _CORE_CACHE[key] = (panel, matched, ledger)
    return panel, matched, ledger


def warm(data_dir: Path, as_of: date_cls | None = None) -> None:
    """启动后台预热：把最贵的一步提前做掉，页面首次打开就不用等 6.5 秒。

    失败只吞掉并记日志——预热是优化，不是前置条件。
    """
    try:
        target = as_of or cn_today()
        get_overview(Path(data_dir), target)
        get_scan(Path(data_dir), target)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("table_game 预热失败: %s", exc)


# ===== 基础装载 =====


def _enriched_files(data_dir: Path, start: date_cls, end: date_cls) -> list[Path]:
    """按区间取日线分区文件。月分区（date=2026-11）按目录名区间比较即可，
    因为分区名是 ISO 前缀，字符串序 == 时间序。"""
    base = data_dir / "kline_daily_enriched"
    if not base.is_dir():
        return []
    out: list[Path] = []
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("date="):
            continue
        tag = d.name[5:]
        # 月分区右端展开成月末，避免 '2026-11' < '2026-11-13' 的字符串比较陷阱
        if len(tag) == 7:
            lo, hi = f"{tag}-01", f"{tag}-31"
        else:
            lo = hi = tag
        if hi < start.isoformat() or lo > end.isoformat():
            continue
        for f in d.glob("*.parquet"):
            out.append(f)
    return sorted(out)


def _load_panel(data_dir: Path, as_of: date_cls) -> pl.DataFrame:
    """一年日线面板 + 三个派生列。缺失列一律当无数据，不猜。"""
    days = [d for d in kline_trading_days(data_dir) if d <= as_of]
    if not days:
        return pl.DataFrame()
    days = days[-LOOKBACK_DAYS:]
    files = _enriched_files(data_dir, days[0], days[-1])
    if not files:
        return pl.DataFrame()
    lf = pl.scan_parquet([str(f) for f in files], extra_columns="ignore")
    df = (
        lf.select([
            "symbol", "date", "open", "high", "low", "close",
            "volume", "amount", "turnover_rate",
            "consecutive_limit_ups", "consecutive_limit_downs",
        ])
        .collect()
        .sort(["symbol", "date"])
    )
    if df.is_empty():
        return df
    prev_close = pl.col("close").shift(1).over("symbol")
    return df.with_columns([
        # 当日涨跌幅（复权口径，用于分桶与形态判定）
        (pl.col("close") / prev_close - 1).alias("ret_t"),
        # 次日竞价高开幅度：T 收盘 → T+1 开盘（复权口径，跨除权日也成立）
        (pl.col("open").shift(-1).over("symbol") / pl.col("close") - 1).alias("d1_gap"),
        # 次日「开盘买→收盘卖」：散户唯一能成交的那一半
        (
            pl.col("close").shift(-1).over("symbol")
            / pl.col("open").shift(-1).over("symbol") - 1
        ).alias("d1_oc"),
        # 次日开盘 → T+2 收盘（持两日），用来对照「拿住」是不是更好
        (
            pl.col("close").shift(-2).over("symbol")
            / pl.col("open").shift(-1).over("symbol") - 1
        ).alias("d12_oc"),
        # 振幅（占昨收），对敲判定要用
        ((pl.col("high") - pl.col("low")) / prev_close).alias("amp"),
    ])


def _bucket_expr(col: str, n: int) -> pl.Expr:
    """按当日横截面排名切等数量桶（0..n-1）。不用 qcut：polars 各版本签名不一致，
    且 qcut 遇到大量重复值（涨停日 change 全等）会把桶数压缩到不可控。"""
    return (
        ((pl.col(col).rank("ordinal").over("date") - 1) / pl.len().over("date") * n)
        .floor().clip(0, n - 1).cast(pl.Int32)
    )


def _load_events(data_dir: Path, as_of: date_cls) -> tuple[pl.DataFrame, pl.DataFrame]:
    """龙虎榜事件表。只取 1 日榜：3 日/10 日榜的净额是累计值，
    和「次日收益」不是同一根时间轴，混用会把 3 天的钱记到 1 天上。"""
    base = data_dir / "dragon_tiger"
    if not base.is_dir():
        return pl.DataFrame(), pl.DataFrame()
    stock_rows: list[tuple] = []
    seat_rows: list[tuple] = []
    for p in sorted(base.glob("date=*.json")):
        tag = p.stem[5:]
        try:
            if date_cls.fromisoformat(tag) > as_of:
                continue
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        td = payload.get("trade_date") or tag
        for it in (payload.get("all") or {}).get("stock_items") or []:
            if it.get("range_days") != 1:
                continue
            stock_rows.append((
                td, it.get("thscode"), it.get("name"),
                it.get("net_value"), it.get("org_net_value"),
                it.get("hot_money_net_value"), it.get("change"),
            ))
        for seat in (payload.get("hot_money") or {}).get("hot_money_items") or []:
            for r in seat.get("rows") or []:
                if r.get("range_days") != 1:
                    continue
                seat_rows.append((
                    td, seat.get("name"), r.get("thscode"), r.get("hot_money_item_net_value"),
                ))
    # 目录存在但没有任何 ≤ as_of 的榜单文件时，直接返回空帧。
    # 不这么写就会在下面 pl.col("date").str.to_date() 上炸：空列表构造出的
    # date 列是 Null 而不是 String，整个端点 500——「这一天之前还没榜单」
    # 本来是个正常的空射程，不该变成异常。
    if not stock_rows:
        return pl.DataFrame(), pl.DataFrame()
    st = pl.DataFrame(
        stock_rows,
        schema=["date", "symbol", "name", "net", "org_net", "hot_net", "chg"],
        orient="row",
    ).with_columns(pl.col("date").str.to_date())
    se = pl.DataFrame(
        seat_rows,
        schema=["date", "seat", "symbol", "seat_net"],
        orient="row",
    ).with_columns(pl.col("date").str.to_date())
    # 同一只票同日可能因榜单口径重复出现，按 (date,symbol) 去重；
    # 席位按 (date,seat,symbol) 去重。不去重会让单只票被加权两次。
    if not st.is_empty():
        st = st.unique(subset=["date", "symbol"], keep="first")
    if not se.is_empty():
        se = se.unique(subset=["date", "seat", "symbol"], keep="first")
    return st, se


def _attach_matched(panel: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """给面板每一行算「同日同桶对照超额」，对照组**剔除事件本身**。

    为什么必须剔除：匹配桶只有几十只票，上榜股占比虽小，但如果把它们算进对照组，
    对照均值会被处理组自己抬高，超额被系统性衰减（夹具实测：真值 60bp 衰减到 28bp）。
    真实数据里事件只占 0.9% 的格子，影响很小，但估计量应该是无偏的，
    不能靠"数据量大所以偏误小"来兜底。

    返回的帧包含 is_lhb 标记；非上榜行的 exm 均值就是对照组自检（应 ≈ 0）。
    """
    if panel.is_empty():
        return panel
    p = panel.filter(
        pl.col("d1_oc").is_not_null()
        & pl.col("ret_t").is_not_null()
        & pl.col("turnover_rate").is_not_null()
    )
    if p.is_empty():
        return p
    if events.is_empty():
        # 无龙虎榜：仍然给出面板与对照超额，但 is_lhb 全空，账本会自然返回空。
        # 三个净额列必须是 null 而不是缺失，否则下游 filter 直接抛列不存在。
        p = p.with_columns([
            pl.lit(None, dtype=pl.Int8).alias("is_lhb"),
            pl.lit(None, dtype=pl.Float64).alias("org_net"),
            pl.lit(None, dtype=pl.Float64).alias("hot_net"),
            pl.lit(None, dtype=pl.Float64).alias("net"),
        ])
    else:
        p = p.join(
            events.select(["date", "symbol", "org_net", "hot_net", "net"]).with_columns(
                pl.lit(1, dtype=pl.Int8).alias("is_lhb")
            ),
            on=["date", "symbol"],
            how="left",
        )
    p = p.with_columns([
        _bucket_expr("ret_t", CHG_BUCKETS).alias("cb"),
        _bucket_expr("turnover_rate", TURN_BUCKETS).alias("tb"),
        pl.col("is_lhb").fill_null(0).alias("_t"),
        pl.col("d1_oc").is_not_null().cast(pl.Int32).alias("_n1"),
        pl.col("d12_oc").is_not_null().cast(pl.Int32).alias("_n2"),
        pl.col("d1_oc").fill_null(0.0).alias("_s1"),
        pl.col("d12_oc").fill_null(0.0).alias("_s2"),
    ])
    cell = p.group_by(["date", "cb", "tb"]).agg([
        pl.col("_s1").sum().alias("s1"),
        pl.col("_s2").sum().alias("s2"),
        pl.col("_n1").sum().alias("n1"),
        pl.col("_n2").sum().alias("n2"),
        (pl.col("_s1") * pl.col("_t")).sum().alias("ts1"),
        (pl.col("_s2") * pl.col("_t")).sum().alias("ts2"),
        (pl.col("_n1") * pl.col("_t")).sum().alias("tn1"),
        (pl.col("_n2") * pl.col("_t")).sum().alias("tn2"),
    ])
    m = p.join(cell, on=["date", "cb", "tb"], how="left")
    # 剔除事件后的对照均值；格子被事件占满时退回全格均值（此时对照无意义）
    ctrl = pl.when(pl.col("n1") - pl.col("tn1") > 0).then(
        (pl.col("s1") - pl.col("ts1")) / (pl.col("n1") - pl.col("tn1"))
    ).otherwise(pl.col("s1") / pl.col("n1"))
    ctrl2 = pl.when(pl.col("n2") - pl.col("tn2") > 0).then(
        (pl.col("s2") - pl.col("ts2")) / (pl.col("n2") - pl.col("tn2"))
    ).otherwise(pl.col("s2") / pl.col("n2"))
    return m.with_columns([
        ctrl.alias("ctrl"),
        ctrl2.alias("ctrl2"),
        (pl.col("n1") - pl.col("tn1")).alias("ctrl_n"),
        (pl.col("d1_oc") - ctrl).alias("exm"),
        (pl.col("d12_oc") - ctrl2).alias("exm2"),
    ]).drop(["_t", "_n1", "_n2", "_s1", "_s2"])


def _mean_t(values: pl.Series) -> tuple[float | None, float | None, int]:
    """均值与 t。用样本标准差；n<3 时 t 无意义返回 None。"""
    v = [x for x in values.to_list() if x is not None and not (isinstance(x, float) and math.isnan(x))]
    n = len(v)
    if n == 0:
        return None, None, 0
    mean = sum(v) / n
    if n < 3:
        return mean, None, n
    var = sum((x - mean) ** 2 for x in v) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else None
    return mean, t, n


def _bp(x: float | None) -> float | None:
    return None if x is None else round(x * 1e4, 1)


# 分档结论的最小样本与显著性门槛。写在一个地方，前端只上色不重判。
GAP_MIN_N = 30
GAP_MIN_T = 2.0
# 量化席位识别：龙虎榜昵称里带这几个词的，归到「量化 / 程序化」这个对象。
# 它是一个**按昵称的**近似，不是机构分类——页面必须这么说。
QUANT_SEAT_PATTERN = "量化|程序化|算法"


def _gap_verdict(label: str, mean: float | None, t: float | None, n: int) -> str:
    """一档竞价缺口能不能接。四态：take / weak / avoid / unbuyable（另加 thin）。

    判据只有两条：档内超额的方向，和它够不够显著。方向为正但不显著时写 weak，
    因为「正的但 t=1.2」和「负的且 t=-6」在决策上是两件事，不能都叫"能接"。
    """
    if "买不进" in label:
        return "unbuyable"
    if n < GAP_MIN_N or mean is None:
        return "thin"
    if mean <= 0:
        return "avoid"
    if t is None or abs(t) < GAP_MIN_T:
        return "weak"
    return "take"


def _bucket_stable(mean: float | None, halves: list[dict]) -> bool | None:
    """一档结论在前后半段是否都成立。三态：True / False / None（切不动两段）。

    为什么不一刀切：低开档前后半段是 +67.8bp(t=7.2) 与 +76.5bp(t=8.15)，
    而高开 3~9.8% 档后半段 t 只有 -1.77——方向没变，显著性掉了。
    这两件事在决策上不同：前者可以当规则用，后者只能当倾向。
    """
    if not halves:
        return None
    same_sign = all(
        (h["bp"] is not None) and (mean is not None) and ((h["bp"] >= 0) == (mean >= 0))
        for h in halves
    )
    both_sig = all(h["t"] is not None and abs(h["t"]) >= GAP_MIN_T for h in halves)
    return bool(same_sign and both_sig)


def _quant_seat_stats(matched: pl.DataFrame, seats: pl.DataFrame | None) -> dict | None:
    """把「量化 / 程序化」席位拼回匹配超额。

    名册里这一项原来标着 observed、正文写着「本地可单独统计其次日结果」，
    但没有给数字——承诺和数据对不上。这里把那句话兑现：按昵称匹配量化席位，
    给出 n / bp / t。昵称是近似，所以口径写在 label 里，不假装是机构分类。
    """
    if seats is None or seats.is_empty():
        return None
    base = matched.filter(pl.col("is_lhb") == 1).select(["date", "symbol", "exm"])
    if base.is_empty():
        return None
    j = seats.join(base, on=["date", "symbol"], how="inner").filter(
        pl.col("seat").str.contains(QUANT_SEAT_PATTERN)
    )
    if j.is_empty():
        return None
    mean, t, n = _mean_t(j["exm"])
    if mean is None:
        return None
    return {
        "label": "量化席位净买后次日",
        "bp": _bp(mean),
        "t": None if t is None else round(t, 2),
        "n": n,
        "seats": sorted(j["seat"].unique().to_list()),
    }


# ===== 名册：12 个对象 =====

# 每个对象的字段含义：
#   capital     钱的来源（决定它能不能等）
#   horizon     决策尺度（同一根 K 线，不同对象读出的东西不同）
#   hard        硬约束：什么情况下它**必须**交易（被迫单才是可预测的）
#   evidence    observed=本地可观测 | proxied=只有代理变量 | no_data=本地无此表
#   trace       它在数据里留下的痕迹（没有就明说没有）
#   lie         它会怎么把货交给你
_OBJECTS: list[dict] = [
    {
        "id": "national",
        "name": "国家队",
        "group": "公共资金",
        "capital": "财政/央行流动性，无赎回压力，成本不是考核项",
        "horizon": "3-10 年",
        "hard": "只在指数失稳时买；买完不能轻易卖（卖出本身就是信号）",
        "evidence": "no_data",
        "trace": "本仓库没有 ETF 份额、十大股东、汇金持仓三张表。唯一能间接看到的只有"
                 "宽基 ETF 尾盘异常放量——而 ETF 份额数据不在本地。",
        "lie": "你会把龙虎榜的「机构专用」当成它。那个席位里同时坐着公募、保险、社保、"
               "私募和量化，是一个混合桶；把它读成国家队，是所有误读里最贵的一条。",
    },
    {
        "id": "social_security",
        "name": "社保基金",
        "group": "公共资金",
        "capital": "委托理财，考核 3 年滚动，年度必须正收益",
        "horizon": "1-3 年",
        "hard": "季末/年末考核前减仓兑现；单一标的持仓有比例上限，超限必须卖",
        "evidence": "no_data",
        "trace": "无十大流通股东表，无法观测。可见的只有「季末最后一周大市值股异常波动」，"
                 "而这条痕迹与公募排名行为重叠，无法归因。",
        "lie": "「社保重仓」是研报话术，不是实时信号。你看到的是 3 个月前的季报快照。",
    },
    {
        "id": "huijin",
        "name": "汇金 / 证金",
        "group": "公共资金",
        "capital": "准财政，2015 年救市留下的存量仓位至今仍在",
        "horizon": "5 年以上",
        "hard": "几乎不主动卖；减持需报批，节奏以年为单位",
        "evidence": "no_data",
        "trace": "无持仓表。历史痕迹是 2015-2016 年银行/券商权重股的巨量对倒，"
                 "本地日线只能看到价格与成交，看不到对手方。",
        "lie": "「汇金增持」公告出来时，价格通常已经涨过一轮——公告是结果，不是原因。",
    },
    {
        "id": "insurance",
        "name": "保险资金",
        "group": "长钱",
        "capital": "保费负债，久期 10 年以上，但每年要付预定利率",
        "horizon": "3-5 年",
        "hard": "IFRS9 后公允价值波动直接进利润表 → 高波动股拿不住；"
                "偿付能力充足率触线时必须减仓",
        "evidence": "proxied",
        "trace": "代理变量：高股息 + 低波动 + 大市值三条件同时满足的标的，其换手率长期低于"
                 "全市场中位数——这是长钱锁仓的形状，但无法区分保险与社保。",
        "lie": "「险资举牌」是极端事件，一年没几次；把每次大盘股异动都归因于险资，是无信息量的解释。",
    },
    {
        "id": "broker",
        "name": "券商自营 / 资管",
        "group": "机构",
        "capital": "自有资金 + 资管产品，年度考核",
        "horizon": "1 季度 - 1 年",
        "hard": "自营有年度利润目标，亏损到线必须止损；做市业务有持仓上限",
        "evidence": "proxied",
        "trace": "龙虎榜「机构专用」席位的一部分。券商席位常出现在新股/次新与 ETF 套利上，"
                 "但席位不披露具体机构，只能按行为特征猜。",
        "lie": "卖方研报是券商另一个部门写的，与自营持仓无关——但两者常被读者混为一谈。",
    },
    {
        "id": "mutual_fund",
        "name": "公募基金",
        "group": "机构",
        "capital": "基民申赎，随时可赎回；管理费按规模收",
        "horizon": "1 季度（排名），实际持仓周期 6-12 个月",
        "hard": "排名考核是相对的：跌的时候别人也跌就没事 → 抱团；"
                "季末最后三个交易日拉重仓股做净值；赎回潮时被动卖出（不管基本面）",
        "evidence": "proxied",
        "trace": "可观测的形状：同一概念板块内多只票在季末最后 3 日同向异动，且这些票的"
                 "基金重仓比例高。本地无基金持仓表，只能用「板块同向 + 季末时点」做代理。",
        "lie": "「机构调研」「基金加仓」都是滞后 1-3 个月的信息，你看到时排名战已经打完了。",
    },
    {
        "id": "private_fund",
        "name": "私募基金",
        "group": "机构",
        "capital": "高净值 + 通道，多数有 0.8/0.85 净值预警线与 0.7 止损线",
        "horizon": "1 个月 - 1 季度",
        "hard": "净值触预警线 → 强制降仓，与看多看空无关；"
                " lock-up 到期 → 赎回压力；单一标的不得超过净值 10%（部分合同）",
        "evidence": "proxied",
        "trace": "被迫单的形状：无差别卖出（好票坏票一起卖）、且集中在同一周。"
                 "本地无净值数据，只能从「板块内相关性突然抬升 + 放量下跌」反推。",
        "lie": "私募的对外净值通常是费后、且可能只披露周净值；你看到的曲线比真实回撤平滑。",
    },
    {
        "id": "controlling_shareholder",
        "name": "上市股东 / 董监高",
        "group": "产业资本",
        "capital": "持股成本常低于 1 元/股（发起人股），减持即暴利",
        "horizon": "以解禁日为节点，3-10 年",
        "hard": "解禁日 / 减持新规（集中竞价 90 日内 ≤1%、大宗 ≤2%）/ 质押平仓线 /"
                " 定增破发 → 被迫补仓或违约",
        "evidence": "no_data",
        "trace": "本仓库没有十大股东、限售解禁、股权质押、大宗交易、减持公告五张表。"
                 "这是最该补的一张——「谁在卖公司」在日线上几乎看不出来，只在公告里。",
        "lie": "「基于对公司未来发展的信心」是减持公告的标准开头。"
               "间接减持走大宗 + 过桥方，二级市场只看到一笔折价成交。",
    },
    {
        "id": "fund_manager",
        "name": "基金经理（个人）",
        "group": "机构",
        "capital": "不是账户，是人：考核、排名、年终奖、跳槽",
        "horizon": "年度排名 → 实际行为是「最后一个月赌一把」",
        "hard": "排名后 1/2 的基金经理次年规模腰斩 → 11-12 月有做高净值的强动机；"
                "重仓股集中度受「双十」限制",
        "evidence": "proxied",
        "trace": "11-12 月的「拉净值」是可观测的季节性：重仓股在年末最后 5 日跑赢，"
                 "次年 1 月回吐。本地可用「12 月最后一周 vs 1 月第一周」的板块收益差验证。",
        "lie": "路演说的和持仓做的经常不是一回事；季报只披露前十大，看不到全部仓位。",
    },
    {
        "id": "hot_money",
        "name": "游资",
        "group": "短线资金",
        "capital": "自有 + 配资，成本极高（年化 8-15%），必须快进快出",
        "horizon": "1-3 个交易日",
        "hard": "T+1 无法当日卖出；涨停板排队买不进；一旦炸板当天就被闷",
        "evidence": "observed",
        "trace": "本地唯一**直接可观测**的对手方：龙虎榜按昵称披露的席位（68 个，"
                 "近一年 11,647 条 1 日榜记录）。每个席位的次日结果都在下面这张账本里。",
        "lie": "游资会在涨停板对倒制造人气（对敲），也会在出货日继续挂大买单；"
               "你看到的「净买入」可能是他为出货做的流动性。",
    },
    {
        "id": "quant",
        "name": "量化 / 程序化",
        "group": "短线资金",
        "capital": "产品化资金，靠换手与统计套利，单笔利润极薄",
        "horizon": "分钟到 1 日",
        "hard": "策略容量上限；回撤触线降频；T+1 使其只能做「隔夜 + 日内反转」",
        "evidence": "observed",
        "trace": "龙虎榜里真实存在「量化抢筹」「量化打板」类席位昵称，本地可单独统计其"
                 "次日结果。它们是把「涨停 → 次日高开」这条规律吃干的人。",
        "lie": "「量化砸盘」是最省事的归因。真实情况是量能在提供流动性，"
               "同时在你不注意的时候收走价差。",
    },
    {
        "id": "retail",
        "name": "散户",
        "group": "你我",
        "capital": "工资/存款，无成本但有心理账户；最大的成本是机会成本与时间",
        "horizon": "1 天到 1 周（声称 1 年，实际操作 1 天）",
        "hard": "T+1；无法融券；信息到达时价格已走完；拿不住也跑不掉（跌停无法卖出）",
        "evidence": "proxied",
        "trace": "代理变量：全市场「现价 vs 近 20 日成交加权均价」的浮亏比例。"
                 "它衡量的是「最近 20 天买入的人现在是否被套」，是最接近散户体感的量。",
        "lie": "最大的谎来自自己：把「我看好」当成「它会涨」，"
               "把「昨天涨停」当成「今天还会涨」。",
    },
]


def _retail_proxy(panel: pl.DataFrame) -> dict | None:
    """全市场浮亏比例：现价低于近 20 日收盘均价的只数占比。

    口径说明：不用 VWAP（amount/volume = 原始价），因为 close 是复权价，
    两者直接比会在除权日算出假的浮亏。改用同一口径的「近 20 日收盘均值」
    作为成本代理——牺牲一点精度，换口径一致。
    """
    if panel.is_empty():
        return None
    cost = panel.group_by("symbol").agg([
        pl.col("close").tail(20).mean().alias("cost20"),
        pl.col("close").last().alias("last"),
        pl.len().alias("n"),
    ]).filter(pl.col("n") >= 15)
    if cost.is_empty():
        return None
    under = cost.filter(pl.col("last") < pl.col("cost20")).height
    return {
        "n": cost.height,
        "under_pct": round(under / cost.height * 100, 1),
        "median_gap_pct": round(float((cost["last"] / cost["cost20"] - 1).median()) * 100, 2),
    }


# ===== 账本：跟买到底赚不赚 =====


def _ledger(matched: pl.DataFrame, seats: pl.DataFrame | None = None) -> dict:
    """龙虎榜上榜股次日可成交超额 + 席位排行。

    自检：非上榜股的匹配超额必须 ≈ 0。若这一项显著不为 0，说明分桶没起作用，
    后面的席位排行全部作废——这条自检比任何结论都重要。
    """
    empty = {
        "n_event": 0, "excess_bp": None, "t": None, "control_bp": None,
        "seats": [], "gap_buckets": [], "splits": [], "window_days": 0,
    }
    if matched.is_empty() or "is_lhb" not in matched.columns:
        return empty

    ev = matched.filter(pl.col("is_lhb") == 1)
    ctrl = matched.filter(pl.col("is_lhb").is_null())
    if ev.is_empty():
        return empty

    mean, t, n = _mean_t(ev["exm"])
    cmean, _, cn = _mean_t(ctrl["exm"])
    m2, t2, n2 = _mean_t(ev["exm2"])

    # 可成交子样本：剔除 T+1 一字板
    tradeable = ev.filter(pl.col("d1_gap") < LIMIT_GAP)
    tm, tt, tn = _mean_t(tradeable["exm"])

    splits = []
    for label, cond in [
        ("机构净买", pl.col("org_net") > 0),
        ("机构净卖", pl.col("org_net") < 0),
        ("游资净买", pl.col("hot_net") > 0),
        ("游资净卖", pl.col("hot_net") < 0),
    ]:
        s = ev.filter(cond)
        sm, st, sn = _mean_t(s["exm"])
        splits.append({"label": label, "n": sn, "bp": _bp(sm), "t": None if st is None else round(st, 2)})

    # 次日竞价高开分档：这是「能不能买」的物理约束
    gap_defs = [
        ("次日低开 < 0%", pl.col("d1_gap") < 0),
        ("次日平开 0~3%", (pl.col("d1_gap") >= 0) & (pl.col("d1_gap") < 0.03)),
        ("次日高开 3~9.8%", (pl.col("d1_gap") >= 0.03) & (pl.col("d1_gap") < LIMIT_GAP)),
        ("次日一字板 ≥ 9.8%（买不进）", pl.col("d1_gap") >= LIMIT_GAP),
    ]
    # 样本外切分点：分档结论必须前后两段都成立，否则它只是某段行情的巧合。
    # 这一步以前只做在「整体超额」上，最可执行的那张分档表反而没有检验。
    dates = sorted(ev["date"].unique().to_list())
    mid = dates[len(dates) // 2] if len(dates) >= 20 else None
    gap_buckets = []
    for label, cond in gap_defs:
        s = ev.filter(cond)
        gm, gt, gn = _mean_t(s["exm"])
        bucket_halves = []
        if mid is not None:
            for hl, hcond in [("前半段", pl.col("date") < mid), ("后半段", pl.col("date") >= mid)]:
                hm, ht, hn = _mean_t(s.filter(hcond)["exm"])
                bucket_halves.append({
                    "label": hl, "n": hn, "bp": _bp(hm),
                    "t": None if ht is None else round(ht, 2),
                })
        gap_buckets.append({
            "label": label, "n": gn, "bp": _bp(gm),
            "t": None if gt is None else round(gt, 2),
            "share_pct": round(gn / ev.height * 100, 1) if ev.height else None,
            # 这一档能不能接，由服务端一次判完：前端只负责上色，不重新定义阈值。
            "verdict": _gap_verdict(label, gm, gt, gn),
            "halves": bucket_halves,
            "stable": _bucket_stable(gm, bucket_halves),
        })

    seat_rows = _seat_ledger(matched, seats)

    # 样本外稳定性：把窗口对半切，看同一结论在前后两段是否都成立。
    # 只有一个数字的结论无法分辨「规律」与「某段行情的巧合」。
    halves = []
    if mid is not None:
        for label, cond in [
            ("前半段", pl.col("date") < mid),
            ("后半段", pl.col("date") >= mid),
        ]:
            s = ev.filter(cond)
            hm, ht, hn = _mean_t(s["exm"])
            halves.append({
                "label": label, "n": hn, "bp": _bp(hm),
                "t": None if ht is None else round(ht, 2),
                "from": str(s["date"].min()), "to": str(s["date"].max()),
            })

    return {
        "n_event": n,
        "excess_bp": _bp(mean),
        "t": None if t is None else round(t, 2),
        "control_bp": _bp(cmean),
        "control_n": cn,
        "hold2_bp": _bp(m2),
        "hold2_t": None if t2 is None else round(t2, 2),
        "hold2_n": n2,
        "tradeable_bp": _bp(tm),
        "tradeable_t": None if tt is None else round(tt, 2),
        "tradeable_n": tn,
        "median_gap_pct": round(float(ev["d1_gap"].median()) * 100, 2),
        "splits": splits,
        "gap_buckets": gap_buckets,
        "seats": seat_rows,
        "halves": halves,
        "window_days": int(matched["date"].n_unique()),
    }


def _seat_ledger(matched: pl.DataFrame, seats: pl.DataFrame | None) -> list[dict]:
    """席位账本：把游资昵称拼回匹配超额。样本不足的席位不入榜，
    避免「某席位 3 次全中」这种故事变成结论。"""
    if seats is None or seats.is_empty():
        return []
    base = matched.filter(pl.col("is_lhb") == 1).select(["date", "symbol", "exm"])
    if base.is_empty():
        return []
    j = seats.join(base, on=["date", "symbol"], how="inner")
    if j.is_empty():
        return []
    g = j.group_by("seat").agg([
        pl.len().alias("n"),
        pl.col("exm").mean().alias("mean"),
        pl.col("seat_net").sum().alias("net_sum"),
        pl.col("seat_net").median().alias("net_med"),
        pl.col("symbol").n_unique().alias("n_symbol"),
        pl.col("exm").std().alias("sd"),
    ]).filter(pl.col("n") >= SEAT_MIN_N)
    rows: list[dict] = []
    for r in g.iter_rows(named=True):
        n = int(r["n"])
        sd = r["sd"] or 0.0
        t = (r["mean"] / (sd / math.sqrt(n))) if (sd > 0 and n > 2) else None
        rows.append({
            "seat": r["seat"],
            "n": n,
            "symbols": int(r["n_symbol"]),
            "bp": _bp(r["mean"]),
            "t": None if t is None else round(t, 2),
            "net_med_yi": round(float(r["net_med"] or 0) / 1e8, 2),
        })
    rows.sort(key=lambda x: (x["bp"] is None, -(x["bp"] or 0)))
    return rows


# ===== 对外：名册 + 账本 =====


def _cache_key(data_dir: Path, as_of: date_cls, extra: str) -> tuple[str, str, int]:
    return (str(data_dir), as_of.isoformat(), hash(extra) & 0xFFFF)


def get_overview(data_dir: Path, as_of: date_cls | None = None) -> dict:
    """名册 + 账本 + 信息链。一个请求答完「对面有谁、他们各自的约束、跟买赚不赚」。"""
    data_dir = Path(data_dir)
    as_of = as_of or cn_today()
    key = _cache_key(data_dir, as_of, "overview")
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    panel, matched, ledger = _core_cached(data_dir, as_of)
    _events, seats = _events_cached(data_dir, as_of)
    retail = _retail_proxy(panel) if not panel.is_empty() else None
    quant = _quant_seat_stats(matched, seats)

    # 名册里每个对象的统计数字：只有 observed / proxied 的才有
    stats_by_id: dict[str, list[dict]] = {
        "hot_money": [
            {"label": "净买后次日", "bp": next((s["bp"] for s in ledger["splits"] if s["label"] == "游资净买"), None),
             "n": next((s["n"] for s in ledger["splits"] if s["label"] == "游资净买"), 0),
             "t": next((s["t"] for s in ledger["splits"] if s["label"] == "游资净买"), None)},
            {"label": "净卖后次日", "bp": next((s["bp"] for s in ledger["splits"] if s["label"] == "游资净卖"), None),
             "n": next((s["n"] for s in ledger["splits"] if s["label"] == "游资净卖"), 0),
             "t": next((s["t"] for s in ledger["splits"] if s["label"] == "游资净卖"), None)},
        ],
        "retail": [] if retail is None else [
            {"label": f"全市场浮亏占比（{retail['n']} 只）", "pct": retail["under_pct"], "n": retail["n"]},
            {"label": "现价距 20 日均价中位", "pct_raw": retail["median_gap_pct"], "n": retail["n"]},
        ],
        # 这一项以前是空的：标 observed 却不给数字。现在真算出来，样本不足就返回空。
        "quant": [] if quant is None else [{
            "label": quant["label"], "bp": quant["bp"], "n": quant["n"], "t": quant["t"],
            "note": "按席位昵称匹配（" + " / ".join(quant["seats"]) + "），不是机构分类",
        }],
    }

    objects = []
    for o in _OBJECTS:
        row = dict(o)
        row["stats"] = stats_by_id.get(o["id"], [])
        # 有数字的对象不可能是 no_data：一旦能给数就说明观测到了代理
        objects.append(row)

    out = {
        "as_of": as_of.isoformat(),
        "window": {
            "days": int(panel["date"].n_unique()) if not panel.is_empty() else 0,
            "start": str(panel["date"].min()) if not panel.is_empty() else None,
            "end": str(panel["date"].max()) if not panel.is_empty() else None,
        },
        "objects": objects,
        "ledger": ledger,
        "retail": retail,
        "info_chain": _info_chain(),
        "missing_tables": [
            {"name": "十大流通股东", "unlocks": "国家队 / 社保 / 保险 / 公募的真实持仓与被动机器"},
            {"name": "限售解禁与减持公告", "unlocks": "产业资本的被迫卖压（本模型最大的空白）"},
            {"name": "大宗交易", "unlocks": "间接减持的折价率，是「卖公司」最直接的痕迹"},
            {"name": "股权质押", "unlocks": "平仓线，把「看错」变成「不得不卖」的那个点"},
            {"name": "ETF 份额申赎", "unlocks": "国家队唯一可实时观测的入口"},
        ],
    }
    with _CACHE_LOCK:
        _CACHE[key] = out
    return out


# ===== 对外：扫描层（今天该看哪几只） =====

SCAN_LIMIT = 12
SCAN_SEATS = 3
# 单票自己的历史样本低于这个数，就不能拿它自己的历史下判断，只能用全市场条件基准。
OWN_MIN_N = 20
# 榜单滞后 ≥2 个交易日不可能是发布节奏（T 日榜 T 日晚发布，滞后 ≤1）。
LHB_MAX_LAG = 1


def get_scan(data_dir: Path, as_of: date_cls | None = None, limit: int = SCAN_LIMIT) -> dict:
    """今日可扫描层：今日榜单 × 席位历史 × 该票自身历史 × 明早的分档规则。

    为什么需要这一层：名册回答「对手是谁」，个股实例回答「这只票上发生了什么」，
    但两者都要求你先指定一只票。真实流程是相反方向——先扫一遍今天谁上榜、
    谁在买、买的人历史上赚不赚，再决定要不要深入。没有扫描层，这一页只是一个
    更精致的详情页，入口永远是「净买额第一」那一个。

    三个字段必须一起读：
      seats   买方席位 + 该席位近一年的历史超额（n 小就写样本不足，不给数字）
      own     这只票自己上榜后的历史——多数票 n<20，页面必须说明「只能看全市场基准」
      gate    明早 09:25 的分档规则：低开/平开/高开/一字板各自的历史超额与样本外切分

    这里不预测明天：给的是「条件 → 历史结果」的对照表，条件（竞价缺口）明早才出现。
    """
    data_dir = Path(data_dir)
    as_of = as_of or cn_today()
    key = _cache_key(data_dir, as_of, f"scan:{limit}")
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    panel, matched, ledger = _core_cached(data_dir, as_of)
    events, seats = _events_cached(data_dir, as_of)

    empty = {
        "as_of": as_of.isoformat(),
        "trade_date": None,
        "state": "no_data",
        "lag_days": None,
        "counts": {"total": 0, "positive": 0, "shown": 0},
        "names": [],
        "gate": {"buckets": [], "median_gap_pct": None, "n_event": 0, "tradeable_bp": None},
    }
    if panel.is_empty() or events.is_empty():
        with _CACHE_LOCK:
            _CACHE[key] = empty
        return empty

    today = events["date"].max()
    panel_last = panel["date"].max()
    # 榜单滞后：面板已经走到 panel_last，榜单却停更——这是取数链路的问题，
    # 不是「T 日盘后才公布」的发布节奏。≥2 个交易日就一定是故障。
    lag = len([d for d in kline_trading_days(data_dir) if today < d <= panel_last])
    today_ev = events.filter(pl.col("date") == today)
    today_seats = seats.filter(pl.col("date") == today) if not seats.is_empty() else seats

    # 席位历史：只认账本里过门槛的席位；其余席位写「样本不足」而不是给个 n=3 的均值。
    seat_hist = {s["seat"]: s for s in ledger["seats"]}

    # 单票自身历史：多数票一年只上榜几次，这里算出来正是为了说明它不够用。
    own_rows = (
        matched.filter(pl.col("is_lhb") == 1)
        .group_by("symbol")
        .agg([
            pl.col("exm").mean().alias("m"),
            pl.col("exm").std().alias("sd"),
            pl.len().alias("n"),
        ])
    )
    own: dict[str, dict] = {}
    for r in own_rows.iter_rows(named=True):
        n = int(r["n"])
        sd = r["sd"] or 0.0
        t = (r["m"] / (sd / math.sqrt(n))) if (sd > 0 and n > 2) else None
        own[r["symbol"]] = {
            "n": n, "bp": _bp(r["m"]),
            "t": None if t is None else round(t, 2),
            "enough": n >= OWN_MIN_N,
        }

    rows = []
    for r in today_ev.sort("net", descending=True).iter_rows(named=True):
        sym = r["symbol"]
        srows = today_seats.filter(pl.col("symbol") == sym) if not today_seats.is_empty() else None
        buys: list[dict] = []
        sells: list[dict] = []
        if srows is not None and not srows.is_empty():
            for s in srows.sort("seat_net", descending=True).iter_rows(named=True):
                hist = seat_hist.get(s["seat"])
                item = {
                    "seat": s["seat"],
                    "net_yi": round((s["seat_net"] or 0) / 1e8, 2),
                    "bp": None if hist is None else hist["bp"],
                    "t": None if hist is None else hist["t"],
                    "n": None if hist is None else hist["n"],
                }
                (buys if (s["seat_net"] or 0) > 0 else sells).append(item)
        rows.append({
            "symbol": sym,
            "name": r["name"],
            "chg_pct": None if r["chg"] is None else round(r["chg"] * 100, 2),
            "net_yi": None if r["net"] is None else round(r["net"] / 1e8, 2),
            "org_net_yi": None if r["org_net"] is None else round(r["org_net"] / 1e8, 2),
            "hot_net_yi": None if r["hot_net"] is None else round(r["hot_net"] / 1e8, 2),
            "buy_seats": buys[:SCAN_SEATS],
            "sell_seats": sells[:2],
            "own": own.get(sym, {"n": 0, "bp": None, "t": None, "enough": False}),
        })

    out = {
        "as_of": as_of.isoformat(),
        "trade_date": str(today),
        "state": "ok" if lag <= LHB_MAX_LAG else "stale",
        "lag_days": lag,
        "counts": {
            "total": today_ev.height,
            "positive": today_ev.filter(pl.col("net") > 0).height,
            "shown": min(limit, len(rows)),
        },
        "names": rows[:limit],
        "gate": {
            "buckets": ledger.get("gap_buckets") or [],
            "median_gap_pct": ledger.get("median_gap_pct"),
            "n_event": ledger.get("n_event") or 0,
            "tradeable_bp": ledger.get("tradeable_bp"),
            "window_days": ledger.get("window_days") or 0,
        },
    }
    with _CACHE_LOCK:
        _CACHE[key] = out
    return out


def _info_chain() -> list[dict]:
    """信息到达顺序。时滞是制度决定的（可查），不是估计值。"""
    return [
        {"at": "T 日 09:15", "layer": "盘口", "who": "游资 / 量化", "lag": "实时",
         "note": "你能看到，但看到的匹配量 09:20 前可撤。"},
        {"at": "T 日 15:00", "layer": "收盘价", "who": "所有人", "lag": "实时",
         "note": "收盘价是唯一的定版数据，盘中一切都会变。"},
        {"at": "T 日 盘后", "layer": "龙虎榜", "who": "游资 / 机构专用", "lag": "+30 分钟 ~ 2 小时",
         "note": "这是本地唯一能拿到「具体是谁」的数据；但它只覆盖异动股。"},
        {"at": "T+1 09:25", "layer": "竞价", "who": "所有人", "lag": "+18 小时",
         "note": "你按这份名单下单时，价格已经在竞价里定好了。"},
        {"at": "T+1 ~ T+3", "layer": "公告 / 互动易", "who": "上市公司", "lag": "+1 ~ 3 日",
         "note": "公告是结果确认，不是原因；异动公告常写在股价涨完之后。"},
        {"at": "T+3 ~ T+10", "layer": "卖方研报", "who": "券商研究所", "lag": "+3 ~ 10 日",
         "note": "研报的发布时点与自营/资管仓位无关，但与「已经涨了」高度相关。"},
        {"at": "T+5 ~ T+30", "layer": "财经媒体 / 短视频", "who": "媒体", "lag": "+1 周",
         "note": "到这里信息已经变成故事；故事越完整，价格越接近尾声。"},
        {"at": "季末 +15 日", "layer": "基金季报", "who": "公募", "lag": "+15 日 ~ 3 个月",
         "note": "你看到的「基金重仓」是 3 个月前的快照，且只有前十大。"},
    ]


# ===== 对外：个股实例 =====


def _pct_rank(values: list[float], v: float) -> float | None:
    vals = [x for x in values if x is not None]
    if not vals:
        return None
    return round(sum(1 for x in vals if x <= v) / len(vals) * 100, 0)


def _ols_slope(ys: list[float]) -> tuple[float | None, float | None]:
    """最小二乘斜率（对 log 价格）与 t 值。用于判断「重心是否在上移」。"""
    n = len(ys)
    if n < 8:
        return None, None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    resid = [y - (my + b * (x - mx)) for x, y in zip(xs, ys)]
    sse = sum(r * r for r in resid)
    if n <= 2:
        return b, None
    se = math.sqrt(sse / (n - 2) / sxx) if sse > 0 else 0.0
    t = b / se if se > 0 else None
    return b, t


def _rule(state: str, **kw) -> dict:
    base = {"state": state}
    base.update(kw)
    return base


def _stock_rules(series: pl.DataFrame, events: pl.DataFrame, meta: dict) -> list[dict]:
    """九条行业常识在**这一只票**上的可观测判定。

    state 四态：
      triggered 条件成立 | clear 条件不成立 | derived 算术推算（不是观测）
      no_data   本地无此表，给出需要补哪张表
    """
    out: list[dict] = []
    if series.is_empty():
        return out
    last = series.row(-1, named=True)
    closes = series["close"].to_list()
    turns = [t for t in series["turnover_rate"].to_list() if t is not None]
    amps = [a for a in series["amp"].to_list() if a is not None]
    rets = series["ret_t"].to_list()
    vols = series["volume"].to_list()
    n = len(closes)

    # ① 吸筹：价格重心上移 + 换手不放大 → 有人在不动声色地收
    win = closes[-20:] if n >= 20 else closes
    slope, st = _ols_slope([math.log(c) for c in win if c and c > 0])
    t5 = sum(turns[-5:]) / max(1, len(turns[-5:])) if turns else None
    t20 = sum(turns[-20:]) / max(1, len(turns[-20:])) if turns else None
    turn_ratio = (t5 / t20) if (t5 and t20) else None
    trip = (slope is not None and 0 < slope * 1e4 < 200 and (st or 0) > 2
            and turn_ratio is not None and turn_ratio < 1.0)
    out.append(_rule(
        "triggered" if trip else ("clear" if slope is not None else "no_data"),
        id="accum", label="吸筹",
        claim="吸筹在日线上的形状是「价升 + 量不增」：重心在抬高，但没有人抢。"
              "换手放大到 1.5 倍以上时，那不是吸筹，是抢筹；斜率超过 2%/日时"
              "也不是吸筹，是拉升——吸筹之所以能悄悄完成，就是因为它慢。",
        value=None if slope is None else round(slope * 1e4, 1),
        unit="bp/日（20 日 log 价斜率）",
        detail=f"斜率 t={None if st is None else round(st, 1)}；"
               f"换手 5 日均 / 20 日均 = {None if turn_ratio is None else round(turn_ratio, 2)}",
    ))

    # ② 对敲：量堆着、价不动
    if len(turns) >= 20 and len(amps) >= 20:
        t_now = sum(turns[-5:]) / 5
        a_now = sum(amps[-5:]) / 5
        tr = _pct_rank(turns, t_now)
        ar = _pct_rank(amps, a_now)
        hit = tr is not None and ar is not None and tr >= 80 and ar <= 25
        out.append(_rule(
            "triggered" if hit else "clear", id="wash", label="对敲 / 换手堆积",
            claim="对敲的痕迹不是「大单」，而是「换手很高但振幅很小」——"
                  "筹码在账户之间搬家，价格却没走。它制造的是成交量，不是价格。",
            value=None if tr is None else int(tr),
            unit="换手率分位",
            detail=f"近 5 日换手第 {None if tr is None else int(tr)} 百分位 / "
                   f"振幅第 {None if ar is None else int(ar)} 百分位；"
                   f"触发条件：换手 ≥80 且振幅 ≤25",
        ))
    else:
        out.append(_rule("no_data", id="wash", label="对敲 / 换手堆积",
                         claim="对敲需要「高换手 + 低振幅」两条同时成立。", detail="样本不足 20 日"))

    # ③ 震仓：放量下跌后 5 日内收回
    shakes = 0
    shake_days: list[str] = []
    for i in range(max(1, n - 40), n - 1):
        if rets[i] is None or rets[i] > -0.04:
            continue
        base_vols = [v for v in vols[max(0, i - 20):i] if v]
        if len(base_vols) < 10:
            continue
        if vols[i] is None or vols[i] < 1.8 * (sum(base_vols) / len(base_vols)):
            continue
        fwd = closes[min(n - 1, i + 5)]
        if closes[i] and fwd > closes[i]:
            shakes += 1
            shake_days.append(str(series["date"][i]))
    out.append(_rule(
        "triggered" if shakes else "clear", id="shakeout", label="震仓",
        claim="震仓和真跌的区别只有一个：5 个交易日内有没有收回那根阴线。"
              "收回了叫洗盘，没收回叫趋势——事后才分得清，所以它是用来解释的，不是用来预判的。",
        value=shakes, unit="次（近 40 日）",
        detail="；".join(shake_days[-3:]) if shake_days else "近 40 日无「放量下跌后 5 日收复」形态",
    ))

    # ④ 飞刀：连续跌停打开后的真实接盘结果
    knife_rets: list[float] = []
    knife_days: list[str] = []
    for i in range(max(1, n - 120), n - 5):
        cd = series["consecutive_limit_downs"][i]
        if cd is None or cd < 2:
            continue
        if closes[i] and closes[i + 5] is not None:
            knife_rets.append(closes[i + 5] / closes[i] - 1)
            knife_days.append(str(series["date"][i]))
    if knife_rets:
        m = sum(knife_rets) / len(knife_rets)
        out.append(_rule(
            "triggered", id="knife", label="飞刀（连续跌停后接盘）",
            claim="「跌多了就会反弹」是本页要打掉的第一个直觉。这里给的是这只票历史上"
                  "连续跌停 ≥2 天后，从打开日收盘买入并持有 5 日的真实结果。",
            value=round(m * 100, 2), unit="%（5 日均值）",
            detail=f"{len(knife_rets)} 次样本；{', '.join(knife_days[-3:])}",
        ))
    else:
        out.append(_rule("no_data", id="knife", label="飞刀（连续跌停后接盘）",
                         claim="近 120 日没有连续跌停 ≥2 天的样本，无法给出真实接盘结果。",
                         detail="空射程不落盘：没有样本时不显示 0%"))

    # ⑤ 派发：高位 + 放量 + 上影
    hi = _pct_rank(closes, last.get("close") or 0)
    dist = 0
    for i in range(max(1, n - 10), n):
        base_vols = [v for v in vols[max(0, i - 20):i] if v]
        if not base_vols or vols[i] is None:
            continue
        if vols[i] < 1.5 * (sum(base_vols) / len(base_vols)):
            continue
        o, c, h = series["open"][i], closes[i], series["high"][i]
        upper = (h - max(o, c)) if (h is not None and o and c) else 0
        body = abs(c - o) if (c and o) else 0
        if (c is not None and o is not None and c < o) or (body > 0 and upper > 2 * body):
            dist += 1
    out.append(_rule(
        "triggered" if (dist >= 2 and (hi or 0) >= 80) else "clear", id="distribution",
        label="派发（高位放量长上影）",
        claim="出货不需要「跌」，只需要有人接。高位放量 + 上影线 = 有人在把货交给追高的人，"
              "而收盘价还是红的——这就是它难被识破的原因。",
        value=dist, unit="次（近 10 日）",
        detail=f"当前价位于近 {n} 日第 {None if hi is None else int(hi)} 百分位；"
               f"触发条件：高位（≥80）+ 近 10 日 ≥2 次放量长上影/放量收阴",
    ))

    # ⑥ 控盘成本：算术，不是观测
    float_cap = meta.get("float_cap")
    amt20 = series["amount"].tail(20).mean()
    if float_cap and amt20:
        need = float_cap * 0.2
        days = need / amt20 if amt20 else None
        out.append(_rule(
            "derived", id="control", label="控盘 20% 流通盘需要的钱",
            claim="「庄家控盘 20%-30%」是流传最广的一句常识。它是算术，不是观测："
                  "拿走 20% 流通盘需要的资金 = 流通市值 × 20%，"
                  "再除以日均成交额就是「不吃高价的吸筹天数」。天数越长，越不可能悄悄完成。",
            value=None if days is None else round(days, 1), unit="个交易日的全部成交额",
            detail=f"流通市值 {float_cap / 1e8:.1f} 亿；近 20 日日均成交 {amt20 / 1e8:.2f} 亿；"
                   f"需 {need / 1e8:.1f} 亿",
        ))

    # ⑦ 壳价值：小市值 + 亏损（代理）
    roe = meta.get("roe")
    cap_rank = meta.get("cap_rank")
    shell = (cap_rank is not None and cap_rank <= 20 and roe is not None and roe < 0)
    out.append(_rule(
        "triggered" if shell else ("clear" if roe is not None and cap_rank is not None else "no_data"),
        id="shell", label="壳价值",
        claim="壳价值 = 上市资格的价格，它随 IPO 节奏和退市新规变化，不随公司经营变化。"
              "判据是「小市值 + 不赚钱 + 主业无增长」，三者同时成立才有壳属性。",
        value=None if cap_rank is None else int(cap_rank), unit="流通市值分位（低=小）",
        detail=f"ROE={None if roe is None else round(roe, 1)}%（财务表原始单位就是百分数）；"
               f"触发条件：市值分位 ≤20 且 ROE < 0",
    ))

    # ⑧ 减持压力：本地无表 → 永远 no_data，并明说缺哪四张表。
    # 不用「机构专用净额」冒充减持：那是混合桶，方向也不等于减持。
    out.append(_rule(
        "no_data", id="reduce", label="减持压力（产业资本）",
        claim="「大股东在卖公司」在日线上几乎看不出来：集中竞价每 90 日只能卖 1%，"
              "真正的量走大宗 + 过桥，折价 5-10%，二级市场只留下一根不起眼的阴线。"
              "这是本页最大的空白，也是散户最容易忽略的一条。",
        detail="本地缺「十大股东 / 限售解禁 / 大宗交易 / 减持公告」四张表，此处不给数字。",
    ))

    # ⑨ 机构 vs 游资分歧：唯一能观测到的「两类钱方向相反」的时点
    if not events.is_empty():
        latest = events.sort("date").row(-1, named=True)
        org = latest.get("org_net")
        hot = latest.get("hot_net")
        if org is not None and hot is not None and org * hot < 0:
            side = "机构卖、游资买" if org < 0 else "机构买、游资卖"
            out.append(_rule(
                "triggered", id="divergence", label="机构 / 游资分歧",
                claim="分歧比共识有信息：它说明两类钱对同一根 K 线的读法不同。"
                      "机构卖游资买，通常是游资在接机构倒出来的货；"
                      "机构买游资卖，常常是游资把筹码交给了长钱。前者次日更容易反复。",
                value=round((org - hot) / 1e8, 2), unit="亿元（机构净额 − 游资净额）",
                detail=f"{latest['date']} {side}：机构 {org / 1e8:+.2f} 亿 / 游资 {hot / 1e8:+.2f} 亿",
            ))
        else:
            out.append(_rule(
                "clear", id="divergence", label="机构 / 游资分歧",
                claim="分歧比共识有信息：两类钱方向相反时，才说明有人在接别人的货。",
                detail=f"{latest['date']} 机构与游资同向或数据缺失，无分歧可读",
            ))
    return out


def get_board(
    data_dir: Path,
    symbol: str,
    as_of: date_cls | None = None,
    days: int = 60,
) -> dict:
    """个股实例：事件时间轴 + 九条规则判定。"""
    data_dir = Path(data_dir)
    as_of = as_of or cn_today()
    key = _cache_key(data_dir, as_of, f"board:{symbol}:{days}")
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    panel = _panel_cached(data_dir, as_of)
    if panel.is_empty():
        return {"symbol": symbol, "state": "no_data", "events": [], "rules": [], "meta": {}}
    sym = panel.filter(pl.col("symbol") == symbol)
    if sym.is_empty():
        return {"symbol": symbol, "state": "not_in_universe", "events": [], "rules": [], "meta": {}}

    meta = _stock_meta(data_dir, panel, sym, symbol)
    lhb = _stock_lhb(data_dir, as_of, symbol)
    events = _stock_events(sym, lhb, days)
    rules = _stock_rules(sym, lhb, meta)

    out = {
        "symbol": symbol,
        "state": "ok",
        "as_of": str(panel["date"].max()),
        "meta": meta,
        "events": events,
        "rules": rules,
    }
    with _CACHE_LOCK:
        _CACHE[key] = out
    return out


def _stock_meta(data_dir: Path, panel: pl.DataFrame, sym: pl.DataFrame, symbol: str) -> dict:
    """名称 / 流通市值 / 市值分位 / ROE。缺失一律 None，不用 0 冒充。"""
    name = None
    float_cap = None
    inst = data_dir / "instruments" / "instruments.parquet"
    shares = None
    if inst.exists():
        try:
            row = (
                pl.read_parquet(inst, columns=["symbol", "name", "float_shares"])
                .filter(pl.col("symbol") == symbol)
            )
            if not row.is_empty():
                name = row["name"][0]
                shares = row["float_shares"][0]
        except (OSError, pl.exceptions.PolarsError):
            pass

    last_row = sym.row(-1, named=True)
    last_close = last_row.get("close")
    if shares and last_close:
        float_cap = float(shares) * float(last_close)

    cap_rank = None
    if float_cap:
        caps = _all_float_caps(data_dir, sym["date"][-1], last_close)
        if caps:
            below = sum(1 for c in caps if c <= float_cap)
            cap_rank = round(below / len(caps) * 100, 0)

    roe = None
    fin = data_dir / "financials" / "metrics" / "part.parquet"
    if fin.exists():
        try:
            f = pl.read_parquet(fin, columns=["symbol", "period_end", "roe"]).filter(
                pl.col("symbol") == symbol
            )
            if not f.is_empty():
                f = f.sort("period_end").drop_nulls("roe")
                if not f.is_empty():
                    roe = float(f["roe"][-1])
        except (OSError, pl.exceptions.PolarsError):
            pass

    return {
        "symbol": symbol,
        "name": name,
        "close": last_close,
        "float_cap": float_cap,
        "cap_rank": cap_rank,
        "roe": roe,
        "turnover_rate": last_row.get("turnover_rate"),
        "amount": last_row.get("amount"),
    }


def _all_float_caps(data_dir: Path, last_date, _close: float | None) -> list[float]:
    """全市场流通市值列表（用于市值分位）。取不到就返回空表，不做近似。"""
    inst = data_dir / "instruments" / "instruments.parquet"
    if not inst.exists():
        return []
    try:
        i = pl.read_parquet(inst, columns=["symbol", "float_shares"])
    except (OSError, pl.exceptions.PolarsError):
        return []
    # 用最新一天的收盘价（同一天、同一口径），标签只用于分位排序，不对外展示
    as_of = last_date
    day = pl.read_parquet(
        data_dir / "kline_daily_enriched" / f"date={as_of}" / "part.parquet",
        columns=["symbol", "close"],
    ) if (data_dir / "kline_daily_enriched" / f"date={as_of}").is_dir() else None
    if day is None:
        return []
    j = day.join(i, on="symbol", how="inner").filter(
        pl.col("float_shares").is_not_null() & pl.col("close").is_not_null()
    )
    return (j["float_shares"] * j["close"]).to_list()


def _stock_lhb(data_dir: Path, as_of: date_cls, symbol: str) -> pl.DataFrame:
    events, _ = _events_cached(data_dir, as_of)
    if events.is_empty():
        return events
    return events.filter(pl.col("symbol") == symbol)


def _stock_events(sym: pl.DataFrame, lhb: pl.DataFrame, days: int) -> list[dict]:
    """事件时间轴：价格形态 + 龙虎榜席位，统一到「谁 / 什么动作 / 你什么时候能看到」。

    seen_at 是本页的核心字段：同一件事，游资 T 日盘中就知道，你 T+1 09:25 才知道。
    """
    ev: list[dict] = []
    full = sym
    if full.is_empty():
        return ev
    rows = full.to_dicts()
    n = len(rows)
    cutoff = str(full["date"][-min(days, n)])
    closes_all = [r.get("close") for r in rows]
    last_high = -99
    last_low = -99
    last_turn = -99
    for i, r in enumerate(rows):
        d = str(r["date"])
        if d < cutoff:
            continue
        ret = r.get("ret_t")
        turn = r.get("turnover_rate")
        gap = r.get("d1_gap")
        # 250 日新高 / 新低：比涨停更常见的「位置」事件，安静票也有时间轴
        window = [c for c in closes_all[max(0, i - 249):i + 1] if c is not None]
        if len(window) >= 120 and r.get("close") is not None:
            if r["close"] >= max(window) - 1e-9 and i - last_high >= 5:
                last_high = i
                ev.append({
                    "date": d, "kind": "high", "actor": "private_fund",
                    "actor_label": "高位筹码持有者",
                    "title": "阶段新高",
                    "detail": f"收 {r['close']:.2f}，为近 {len(window)} 日最高；"
                              "新高意味着所有持仓者都在浮盈——最容易出货，也最容易追高",
                    "value": ret, "seen_at": "T 日 15:00",
                    "evidence": "observed",
                })
            elif r["close"] <= min(window) + 1e-9 and i - last_low >= 5:
                last_low = i
                ev.append({
                    "date": d, "kind": "low", "actor": "retail",
                    "actor_label": "套牢盘",
                    "title": "阶段新低",
                    "detail": f"为近 {len(window)} 日最低；新低意味着所有持仓者都在浮亏——"
                              "止损盘与补仓盘同时存在",
                    "value": ret, "seen_at": "T 日 15:00",
                    "evidence": "observed",
                })
        if r.get("consecutive_limit_ups"):
            ev.append({
                "date": d, "kind": "limit_up", "actor": "hot_money",
                "actor_label": "游资 / 打板资金",
                "title": f"涨停（第 {r['consecutive_limit_ups']} 板）",
                "detail": f"换手 {None if turn is None else round(turn, 1)}%",
                "value": ret, "seen_at": "T 日 15:00（封板即可见）",
                "evidence": "observed",
            })
        if r.get("consecutive_limit_downs"):
            ev.append({
                "date": d, "kind": "limit_down", "actor": "retail",
                "actor_label": "跌停板上的卖方",
                "title": f"跌停（连续第 {r['consecutive_limit_downs']} 天）",
                "detail": "跌停时卖不掉——流动性单向消失",
                "value": ret, "seen_at": "T 日 15:00",
                "evidence": "observed",
            })
        # 放量异动：量比 ≥2 且当天有明显方向
        if i >= 20 and ret is not None and r.get("volume"):
            base = [rows[k]["volume"] for k in range(i - 20, i) if rows[k].get("volume")]
            if base:
                ratio = r["volume"] / (sum(base) / len(base))
                if ratio >= 2.0 and abs(ret) >= 0.03:
                    ev.append({
                        "date": d, "kind": "volume_spike",
                        "actor": "private_fund" if ret < 0 else "mutual_fund",
                        "actor_label": "被迫卖出（私募/赎回）" if ret < 0 else "主动买入（机构/游资）",
                        "title": f"放量{'下跌' if ret < 0 else '上涨'} {round(ret * 100, 1)}%",
                        "detail": f"量比 {ratio:.1f}×；换手 {None if turn is None else round(turn, 1)}%",
                        "value": ret, "seen_at": "T 日盘中（事后才能确认是不是被迫单）",
                        "evidence": "proxied",
                    })
        # 换手极值：安静票唯一会「说话」的一天
        if turn is not None:
            hist_turn = [x.get("turnover_rate") for x in rows[max(0, i - 249):i + 1]
                         if x.get("turnover_rate") is not None]
            if len(hist_turn) >= 120:
                rank = sum(1 for x in hist_turn if x <= turn) / len(hist_turn) * 100
                if rank >= 95 and (i - last_turn) >= 5:
                    last_turn = i
                    ev.append({
                        "date": d, "kind": "turnover_spike", "actor": "quant",
                        "actor_label": "换手异动（量化 / 游资 / 被动调仓）",
                        "title": f"换手 {turn:.1f}%，自身第 {rank:.0f} 百分位",
                        "detail": "换手突然放大而价格没走远，通常不是新资金进场，"
                                  "而是同一批筹码在换手（对敲/调仓/ETF 申赎）",
                        "value": ret, "seen_at": "T 日盘中",
                        "evidence": "proxied",
                    })
        if gap is not None and abs(gap) >= 0.03:
            ev.append({
                "date": d, "kind": "gap",
                "actor": "retail",
                "actor_label": "次日竞价的接盘方",
                "title": f"次日{'高开' if gap > 0 else '低开'} {round(gap * 100, 1)}%",
                "detail": "竞价决定你能不能买到，也决定你买到的是不是别人的出货",
                "value": gap, "seen_at": "T+1 09:25",
                "evidence": "observed",
            })

    # 龙虎榜席位事件：唯一「具体是谁」的观测
    if not lhb.is_empty():
        for r in lhb.sort("date", descending=True).head(20).iter_rows(named=True):
            net = r.get("net")
            org = r.get("org_net")
            hot = r.get("hot_net")
            who = []
            if org:
                who.append(f"机构专用 {org / 1e8:+.2f} 亿")
            if hot:
                who.append(f"游资 {hot / 1e8:+.2f} 亿")
            ev.append({
                "date": str(r["date"]), "kind": "lhb",
                "actor": "hot_money" if (hot or 0) >= (org or 0) else "mutual_fund",
                "actor_label": "龙虎榜",
                "title": f"上榜：净额 {None if net is None else round(net / 1e8, 2)} 亿",
                "detail": " · ".join(who) if who else "无席位明细",
                "value": net, "seen_at": "T 日盘后（约 +2 小时）",
                "evidence": "observed",
            })

    ev.sort(key=lambda x: x["date"], reverse=True)
    return ev[:40]


# ===== 自检 CLI =====


def verify(data_dir: Path) -> dict:
    """重算页面上的每一个数字。任何一条口径改动都必须让这里跟着变。"""
    panel, matched, led = _core_cached(Path(data_dir), cn_today())
    _events, seats = _events_cached(Path(data_dir), cn_today())
    scan = get_scan(Path(data_dir), cn_today())
    return {
        "window_days": int(panel["date"].n_unique()) if not panel.is_empty() else 0,
        "rows": matched.height,
        "events": led["n_event"],
        "excess_bp": led["excess_bp"],
        "t": led["t"],
        "control_bp": led["control_bp"],
        "hold2_bp": led["hold2_bp"],
        "tradeable_bp": led["tradeable_bp"],
        "median_gap_pct": led["median_gap_pct"],
        "gap_buckets": led["gap_buckets"],
        "splits": led["splits"],
        "halves": led["halves"],
        "seats_top": led["seats"][:6],
        "seats_bottom": led["seats"][-4:],
        "quant": _quant_seat_stats(matched, seats),
        "scan": scan,
        "retail": _retail_proxy(panel),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="对手盘模型自检：重算页面上的每个数字")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[2] / "data"))
    ap.add_argument("--symbol", default=None, help="同时打印某只票的规则判定")
    ap.add_argument("--scan", action="store_true", help="打印扫描层（今日榜单）")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    out = verify(data_dir)
    if not args.scan:
        out.pop("scan", None)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    if out["control_bp"] is not None and abs(out["control_bp"]) > 5:
        print(
            f"\n[FAIL] 对照组自检失败：非上榜股匹配超额 {out['control_bp']}bp，应≈0。"
            "分桶没起作用，席位排行全部作废。",
            file=sys.stderr,
        )
        return 1
    scan = out.get("scan") or {}
    if scan and scan.get("state") == "stale":
        # 榜单停更与「T 日盘后才公布」是两件事，退出码必须能分开。
        print(
            f"\n[FAIL] 榜单滞后 {scan.get('lag_days')} 个交易日：面板已到 {scan.get('as_of')}，"
            f"榜单停在 {scan.get('trade_date')}。这不是发布节奏，是取数链路。",
            file=sys.stderr,
        )
        return 2
    if args.symbol:
        board = get_board(data_dir, args.symbol)
        print(json.dumps(board, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
