"""退市红线诊断 —— 把「会不会退市」拆成可计算的规则距离。

为什么需要它：个股分析页能画 K 线、给关键价位、跑 AI 四维分析，但对一只
*ST 股最要紧的三个数字（面值、市值、净资产）一个都不显示。用户问「这只票
会不会像当年那只一样」，页面只能给技术形态 —— 答非所问。

本模块只做一件事：把交易所的强制退市红线翻译成「当前值 / 阈值 / 已连续
天数」，并明确写出哪一条算不出来。规则口径（2024 年退市新规，2024-10-30
起施行，2026 年沿用）：

  交易类 · 面值：连续 20 个交易日每日收盘价均低于 1 元 → 终止上市
  交易类 · 市值：连续 20 个交易日每日市值均低于 5 亿元（主板）/
                 3 亿元（创业板、科创板、北交所）→ 终止上市
  财务类：最近一个会计年度经审计净利润（扣非前后孰低）为负且营业收入
         低于 3 亿元（主板）/ 1 亿元（创业板、科创板），或期末净资产为负
         → 实施退市风险警示；连续两个会计年度触发 → 终止上市

口径纪律：
  - 面值与市值必须用**不复权原始价** `raw_close`（CONTRIBUTING 3.2）。
    前复权价在低价股上被缩放因子放大，用它判 1 元线会得出错误结论：
    实测 600340 的 close 与 raw_close 目前相同（近年无除权），但 000001
    这类长期分红股的 close 只有真实价的 47%，混用会把「高于面值」判成
    「低于面值」。
  - 财务类只用**年报口径**（period_end 落在 12-31）判「是否触发」。
    季报只用于展示趋势，不参与判定 —— 规则说的是「经审计的会计年度」，
    用三季报净资产去判退市，会提前一个季度给出错误结论。
  - 算不出来就返回 `unknown`，不拿最新价冒充历史、不拿季报冒充年报。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from app.market_time import cn_today
from app.services.financial_sync import get_financial_df

logger = logging.getLogger(__name__)

# 连续低于阈值的交易日上限（交易所规则：连续 20 个交易日）
_STREAK_LIMIT = 20
# 面值红线（元）
_PAR_VALUE = 1.0
# 市值红线（元）：主板 5 亿，其余板块 3 亿
_MCAP_MAIN = 5e8
_MCAP_OTHER = 3e8
# 营业收入红线（元）：主板 3 亿，创业板/科创板 1 亿
_REV_MAIN = 3e8
_REV_OTHER = 1e8
# 距阈值余量小于该比例时提示「接近」
_NEAR_RATIO = 0.20
# 取多少自然日的历史用于数连续天数（20 个交易日 ≈ 30 自然日，留一倍余量）
_LOOKBACK_DAYS = 90


@dataclass
class Gate:
    """一条退市红线的当前状态。"""

    id: str
    label: str
    rule: str
    status: str  # triggered / counting / near / safe / unknown
    value: float | None = None
    threshold: float | None = None
    distance_pct: float | None = None
    streak: int | None = None
    streak_limit: int | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "rule": self.rule,
            "status": self.status,
            "value": self.value,
            "threshold": self.threshold,
            "distance_pct": self.distance_pct,
            "streak": self.streak,
            "streak_limit": self.streak_limit,
            "detail": self.detail,
        }


@dataclass
class RiskGuard:
    symbol: str
    name: str | None
    board: str
    is_st: bool
    st_kind: str  # *ST / ST / ""
    as_of: str | None
    gates: list[Gate] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "board": self.board,
            "is_st": self.is_st,
            "st_kind": self.st_kind,
            "as_of": self.as_of,
            "gates": [g.to_dict() for g in self.gates],
            "caveats": self.caveats,
            "counts": self.counts,
        }


def classify_board(code: str) -> str:
    """代码 → 板块。阈值随板块不同，猜错会把红线算成 3 亿或 5 亿。"""
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("4", "8", "920")):
        return "北交所"
    return "主板"


def _st_kind(name: str | None) -> str:
    if not name:
        return ""
    if name.startswith("*ST") or name.startswith("*st"):
        return "*ST"
    if name.startswith("ST") or name.startswith("st"):
        return "ST"
    return ""


def _load_recent(repo, asset_type: str, symbol: str) -> pl.DataFrame:
    """取最近一段日线，只保留判红线要用的列。

    `raw_close` 可能缺失（老数据 / 非 enriched 存储）：缺失时保留 close 但
    由调用方标记为估算，不能静默当成原始价。
    """
    end = cn_today()
    start = end - timedelta(days=_LOOKBACK_DAYS)
    cols = ["date", "close", "raw_close", "volume"]
    df = repo.get_daily_asset(asset_type, symbol, start, end, columns=cols)
    if df.is_empty():
        return df
    return df.sort("date")


def _price_col(df: pl.DataFrame) -> tuple[str, bool]:
    """返回 (用于判定的价格列名, 是否为原始价)。"""
    if "raw_close" in df.columns and df["raw_close"].null_count() < df.height:
        return "raw_close", True
    return "close", False


def _streak_below(series: pl.Series, threshold: float) -> int:
    """从最新一根往前数连续低于阈值的交易日数。遇到第一个未越线即停。"""
    n = 0
    for v in reversed(series.to_list()):
        if v is None:
            break
        if float(v) < threshold:
            n += 1
        else:
            break
    return n


def _par_gate(prices: pl.DataFrame, price_col: str, raw_is_true: bool) -> Gate:
    label = "面值退市"
    rule = "连续 20 个交易日收盘价均低于 1.00 元"
    series = prices[price_col]
    last = float(series[-1]) if series.len() else None
    if last is None:
        return Gate(id="par", label=label, rule=rule, status="unknown", detail="缺少收盘价")
    streak = _streak_below(series, _PAR_VALUE)
    distance = (last - _PAR_VALUE) / _PAR_VALUE * 100
    if streak >= _STREAK_LIMIT:
        status = "triggered"
        detail = f"已连续 {streak} 个交易日低于面值，达到终止上市条件"
    elif last < _PAR_VALUE:
        status = "counting"
        detail = f"已连续 {streak}/{_STREAK_LIMIT} 个交易日低于面值"
    elif distance <= _NEAR_RATIO * 100:
        status = "near"
        detail = f"距面值红线 {distance:+.2f}%"
    else:
        status = "safe"
        detail = f"距面值红线 {distance:+.2f}%"
    if not raw_is_true:
        detail += "（口径：前复权价，非原始价，仅作参考）"
    return Gate(
        id="par",
        label=label,
        rule=rule,
        status=status,
        value=round(last, 4),
        threshold=_PAR_VALUE,
        distance_pct=round(distance, 4),
        streak=streak,
        streak_limit=_STREAK_LIMIT,
        detail=detail,
    )


def _mcap_gate(prices: pl.DataFrame, price_col: str, total_shares: float | None, board: str) -> Gate:
    label = "市值退市"
    limit = _MCAP_MAIN if board == "主板" else _MCAP_OTHER
    rule = f"连续 20 个交易日市值均低于 {limit / 1e8:.0f} 亿元"
    if not total_shares or total_shares <= 0:
        return Gate(id="mcap", label=label, rule=rule, status="unknown", detail="维表缺总股本")
    last = float(prices[price_col][-1]) if prices.height else None
    if last is None:
        return Gate(id="mcap", label=label, rule=rule, status="unknown", detail="缺少收盘价")
    # 用最新股本算历史市值是近似：规则要的是当日市值。历史股本缺失时
    # 这个近似只会把「曾经更小」的市值算大（股本通常只增不减），对「是否
    # 低于红线」是保守方向，不产生假安全。
    mcaps = prices.select((pl.col(price_col) * total_shares).alias("mcap"))["mcap"]
    current = float(mcaps[-1])
    streak = _streak_below(mcaps, limit)
    distance = (current - limit) / limit * 100
    if streak >= _STREAK_LIMIT:
        status = "triggered"
        detail = f"已连续 {streak} 个交易日低于 {limit / 1e8:.0f} 亿元"
    elif current < limit:
        status = "counting"
        detail = f"已连续 {streak}/{_STREAK_LIMIT} 个交易日低于市值红线"
    elif distance <= _NEAR_RATIO * 100:
        status = "near"
        detail = f"距市值红线 {distance:+.1f}%"
    else:
        status = "safe"
        detail = f"距市值红线 {distance:+.1f}%"
    return Gate(
        id="mcap",
        label=label,
        rule=rule,
        status=status,
        value=round(current, 2),
        threshold=limit,
        distance_pct=round(distance, 4),
        streak=streak,
        streak_limit=_STREAK_LIMIT,
        detail=detail,
    )


def _annual_rows(df: pl.DataFrame, symbol: str) -> pl.DataFrame:
    """只保留年报口径（period_end 落在 12-31）。"""
    if df.is_empty() or "symbol" not in df.columns or "period_end" not in df.columns:
        return pl.DataFrame()
    sub = df.filter(pl.col("symbol") == symbol)
    if sub.is_empty():
        return pl.DataFrame()
    return sub.filter(pl.col("period_end").cast(pl.Utf8).str.ends_with("-12-31")).sort("period_end")


def _financial_gates(data_dir, symbol: str, board: str) -> list[Gate]:
    """财务类两条：净资产为负 / 净利润为负且营收低于门槛。

    只用年报判「是否触发」，年报不足时 status=unknown；季报另给趋势说明。
    """
    gates: list[Gate] = []
    rev_limit = _REV_MAIN if board == "主板" else _REV_OTHER
    try:
        bs = get_financial_df(data_dir, "balance_sheet")
        inc = get_financial_df(data_dir, "income")
    except Exception as e:  # noqa: BLE001 - 财务表读失败不该让整个面板挂掉
        logger.debug("退市红线：财务表读取失败 %s: %s", symbol, e)
        bs = inc = pl.DataFrame()

    # --- 净资产 ---
    eq_rule = "最近一个会计年度经审计期末净资产为负"
    annual_bs = _annual_rows(bs, symbol) if not bs.is_empty() else pl.DataFrame()
    if annual_bs.is_empty() or "total_equity" not in annual_bs.columns:
        gates.append(Gate(id="equity", label="净资产为负", rule=eq_rule, status="unknown",
                          detail="本地无年报口径资产负债表"))
    else:
        latest = annual_bs.tail(1)
        equity = latest["total_equity"][0]
        period = latest["period_end"][0]
        if equity is None:
            gates.append(Gate(id="equity", label="净资产为负", rule=eq_rule, status="unknown",
                              detail=f"{period} 净资产缺失"))
        else:
            equity = float(equity)
            consecutive = 0
            for v in reversed(annual_bs["total_equity"].to_list()):
                if v is not None and float(v) < 0:
                    consecutive += 1
                else:
                    break
            if equity < 0:
                status = "triggered"
                detail = (
                    f"{period} 净资产 {equity / 1e8:+.2f} 亿元，已触发退市风险警示"
                    + (f"；连续 {consecutive} 个年度为负，再负一年即终止上市" if consecutive >= 1 else "")
                )
            else:
                status = "safe"
                detail = f"{period} 净资产 {equity / 1e8:+.2f} 亿元"
            gates.append(Gate(
                id="equity", label="净资产为负", rule=eq_rule, status=status,
                value=round(equity, 2), threshold=0.0, distance_pct=None,
                streak=consecutive, detail=detail,
            ))

    # --- 净利润 + 营业收入 ---
    ni_rule = f"最近一个会计年度净利润为负且营业收入低于 {rev_limit / 1e8:.0f} 亿元"
    annual_inc = _annual_rows(inc, symbol) if not inc.is_empty() else pl.DataFrame()
    if annual_inc.is_empty():
        gates.append(Gate(id="revenue", label="净利润与营收", rule=ni_rule, status="unknown",
                          detail="本地无年报口径利润表"))
    else:
        latest = annual_inc.tail(1)
        period = latest["period_end"][0]
        ni = latest["net_income_attributable"][0] if "net_income_attributable" in latest.columns else None
        rev = latest["revenue"][0] if "revenue" in latest.columns else None
        if ni is None or rev is None:
            gates.append(Gate(id="revenue", label="净利润与营收", rule=ni_rule, status="unknown",
                              detail=f"{period} 净利润或营收缺失"))
        else:
            ni, rev = float(ni), float(rev)
            triggered = ni < 0 and rev < rev_limit
            if triggered:
                status = "triggered"
                detail = f"{period} 净利 {ni / 1e8:+.2f} 亿、营收 {rev / 1e8:.2f} 亿，双条件同时成立"
            elif ni < 0:
                status = "near"
                detail = f"{period} 净利 {ni / 1e8:+.2f} 亿为负，但营收 {rev / 1e8:.2f} 亿高于 {rev_limit / 1e8:.0f} 亿门槛"
            else:
                status = "safe"
                detail = f"{period} 净利 {ni / 1e8:+.2f} 亿、营收 {rev / 1e8:.2f} 亿"
            gates.append(Gate(
                id="revenue", label="净利润与营收", rule=ni_rule, status=status,
                value=round(ni, 2), threshold=rev_limit, distance_pct=None,
                detail=detail,
            ))
    return gates


def _latest_quarter_trend(data_dir, symbol: str) -> str | None:
    """最新一期（含季报）净资产的变化速度，用于说明「经营性修复是否可达」。"""
    try:
        bs = get_financial_df(data_dir, "balance_sheet")
    except Exception:  # noqa: BLE001
        return None
    if bs.is_empty() or "symbol" not in bs.columns or "total_equity" not in bs.columns:
        return None
    sub = bs.filter(pl.col("symbol") == symbol).sort("period_end")
    if sub.is_empty() or sub.height < 2:
        return None
    tail = sub.tail(2)
    prev, cur = tail["total_equity"][0], tail["total_equity"][1]
    if prev is None or cur is None:
        return None
    delta = float(cur) - float(prev)
    return (
        f"{tail['period_end'][1]} 净资产 {float(cur) / 1e8:+.2f} 亿元，"
        f"较上一期 {tail['period_end'][0]} 变动 {delta / 1e8:+.2f} 亿元"
    )


def build_risk_guard(repo, symbol: str, prices: pl.DataFrame | None = None) -> RiskGuard:
    """组装一只票的退市红线面板。

    prices: 调用方已经读过的日线（AI 分析路径里 K 线已在内存，再读一次等于
    在 9p 挂载上多付一次元数据往返）。传入时只取判定需要的列，且强制按日期
    升序 —— 「从最新往前数连续天数」依赖顺序，倒序会数出 0。

    失败一律降级为 unknown + caveat，不抛异常：这个面板是「补充信息」，
    它坏了不应该让整页 K 线打不开。
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol 不能为空")

    asset_type = repo.resolve_asset_type(sym)
    name = None
    total_shares = None
    code = sym.split(".")[0]
    try:
        inst = repo.get_instruments_asset(asset_type)
        if not inst.is_empty() and "symbol" in inst.columns:
            hit = inst.filter(pl.col("symbol") == sym)
            if not hit.is_empty():
                name = hit["name"][0] if "name" in hit.columns else None
                if "total_shares" in hit.columns:
                    total_shares = hit["total_shares"][0]
    except Exception as e:  # noqa: BLE001
        logger.debug("退市红线：维表读取失败 %s: %s", sym, e)

    board = classify_board(code)
    st_kind = _st_kind(name)
    guard = RiskGuard(
        symbol=sym, name=name, board=board,
        is_st=bool(st_kind), st_kind=st_kind, as_of=None,
    )

    # 退市红线只对股票成立：ETF 是清盘、指数无退市概念，别把股票的规则套上去
    if asset_type != "stock":
        guard.caveats.append(f"{asset_type} 不适用股票退市红线，本面板仅对股票计算。")
        guard.gates = [
            Gate(id="par", label="面值退市", rule="仅适用股票", status="unknown",
                 detail="该标的不是股票"),
        ]
        return guard

    try:
        if prices is None:
            prices = _load_recent(repo, asset_type, sym)
        else:
            keep = [c for c in ("date", "close", "raw_close", "volume") if c in prices.columns]
            prices = prices.select(keep).sort("date") if "date" in keep else pl.DataFrame()
    except Exception as e:  # noqa: BLE001
        logger.debug("退市红线：日线读取失败 %s: %s", sym, e)
        prices = pl.DataFrame()

    if prices.is_empty():
        guard.caveats.append("本地无该标的近期日线，交易类红线无法计算（可能是已退市或长期停牌）。")
        guard.gates = [
            Gate(id="par", label="面值退市", rule="连续 20 个交易日收盘价均低于 1.00 元",
                 status="unknown", detail="无日线数据"),
            Gate(id="mcap", label="市值退市", rule="连续 20 个交易日市值低于红线",
                 status="unknown", detail="无日线数据"),
        ]
    else:
        price_col, raw_is_true = _price_col(prices)
        guard.as_of = str(prices["date"][-1])[:10]
        if not raw_is_true:
            guard.caveats.append("缺 raw_close（原始价），面值/市值用前复权价估算，低价股可能失真。")
        guard.gates = [
            _par_gate(prices, price_col, raw_is_true),
            _mcap_gate(prices, price_col, total_shares, board),
        ]

    guard.gates.extend(_financial_gates(repo.store.data_dir, sym, board))

    trend = _latest_quarter_trend(repo.store.data_dir, sym)
    if trend:
        guard.counts["equity_trend"] = trend

    # 结论性提示：只在能算的地方给，且只陈述规则后果，不给概率。
    # 三条都算不出来时不能说「未触及」—— 那是把 unknown 说成 safe，
    # 已退市的 000587 就会得到一句「当前未触及硬红线」的假安全结论。
    triggered = [g for g in guard.gates if g.status == "triggered"]
    counting = [g for g in guard.gates if g.status == "counting"]
    judged = [g for g in guard.gates if g.status != "unknown"]
    if triggered:
        guard.counts["verdict"] = "已触发" + "、".join(g.label for g in triggered) + "条件"
    elif counting:
        guard.counts["verdict"] = "正在累计" + "、".join(g.label for g in counting) + "天数"
    elif not judged:
        guard.counts["verdict"] = "本地数据不足，无法判定"
    elif guard.is_st:
        guard.counts["verdict"] = "当前未触及交易类/财务类硬红线"

    guard.caveats.append(
        "本面板只算规则距离，不给摘帽/退市概率：本地日线库仅含在市标的（5579 只），"
        "已退市个股不在库内，用现存样本统计概率会系统性高估生存率。"
    )
    return guard


# ──────────────────────────────────────────────────────────────────
# 全市场「连续 20 日」倒计时 —— 把退市生死全景从「横截面快照」升级成
# 「规则距离计数」。横截面只能回答「今天破没破线」，规则要的是「连续多少天
# 破线」。二者对散户的含义完全不同：破线 1 天是「还有机会」，破线 19 天是
# 「明天就是死线」。
# ──────────────────────────────────────────────────────────────────

def build_market_countdown(
    repo,
    as_of: date,
    *,
    lookback_trading_days: int = 25,
) -> dict:
    """全市场批量计算「当前跌破面值/市值线的连续交易日数」。

    只返回**已经进入倒数**（连续 ≥1 日破线）的票，未破线的票不占 payload。
    与单股 `build_risk_guard` 共用同一套阈值与口径（`_PAR_VALUE` /
    `_MCAP_MAIN` / `_MCAP_OTHER` / `_STREAK_LIMIT` / `classify_board`），
    避免「同一真相两个定义点」。

    口径纪律（与单股一致）：
      - 面值用 `raw_close`（原始价），市值 = raw_close × total_shares。
        前复权价在长期分红股上会缩小（000001 只有真实价 47%），用它判 1 元线
        会把「高于面值」误判成「低于面值」。
      - 市值用「当日总股本」——本地维表只有当前股本，历史股本缺失时用当前值
        近似。股本通常只增不减，这个近似把「曾经更小」的市值算大，方向保守，
        不产生假安全。

    返回:
      {
        "as_of": "2026-11-17",
        "countdown": [
          {"symbol", "name", "board", "close", "mcap",
           "par_streak", "mcap_streak", "streak_limit"},
          ...
        ],
        "caveats": [...],
      }
    只含 par_streak>0 或 mcap_streak>0 的票。
    """
    from app.parquet import scan_enriched_parquet
    from app.services.daily_panel import enriched_files, enriched_partition_dates

    data_dir = repo.store.data_dir
    # 枚举最近 lookback_trading_days 个交易日（分区目录名 = 交易日，与 trading_days
    # 同口径，不按自然日回看以免春节长假整段落空）。日历取自 enriched 自己：
    # `enriched_partition_dates` 一次 scandir 读目录名（0.01s），而旧写法
    # `root.glob("date=*")` + `Path.is_dir()` 要对 5313 个目录各 stat 一次（5.0s）。
    all_days = [d for d in enriched_partition_dates(data_dir) if d <= as_of]
    days = all_days[-lookback_trading_days:]
    if not days:
        return {"as_of": str(as_of), "countdown": [], "caveats": ["本地无日线分区"]}

    start = min(days)

    # 只扫这 25 个分区，不 glob 全树：`**/*.parquet` 会让 polars 在
    # `collect_schema()` 阶段逐个打开 5313 个文件读 footer（实测 16.8s），
    # 而这 25 天之外的行一行都用不上。
    files = enriched_files(data_dir, start, as_of)
    if not files:
        return {"as_of": str(as_of), "countdown": [], "caveats": ["本地无日线分区"]}
    lf = scan_enriched_parquet([str(f) for f in files])
    available = [c for c in ("symbol", "date", "raw_close", "close", "name", "total_shares")
                 if c in lf.collect_schema().names()]
    # total_shares 在维表不在日线，日线里只有行情列；维表单独 join。
    need = [c for c in ("symbol", "date", "raw_close", "close") if c in available]
    if "symbol" not in need or "date" not in need:
        return {"as_of": str(as_of), "countdown": [], "caveats": ["日线缺 symbol/date 列"]}

    try:
        df = (
            lf.filter((pl.col("date") >= start) & (pl.col("date") <= as_of))
            .select(need)
            .collect()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("market_countdown scan failed: %s", e)
        return {"as_of": str(as_of), "countdown": [], "caveats": [f"日线扫描失败: {e}"]}

    if df.is_empty():
        return {"as_of": str(as_of), "countdown": [], "caveats": ["日线为空"]}

    # 价格列：优先 raw_close，缺失时退回 close（并如实标注口径）。
    price_col = "raw_close" if ("raw_close" in df.columns and df["raw_close"].null_count() < df.height) else "close"
    raw_is_true = price_col == "raw_close"

    # 维表 `total_shares` 单位是**股**（平安银行 1.9406e10 = 194 亿股，
    # 萃华退 2.56156e8 = 2.56 亿股），直接用。
    #
    # 这里曾乘 1e4 —— 把另一张表（`RPT_F10_EH_EQUITY` 时点股本）的「万股」
    # 口径套了过来。后果不是报错：市值被放大 1 万倍 ⇒ `mcap < 5e8` 永不成立
    # ⇒ 市值退市这一腿静默全灭，而列表照常出票（全部由面值那条腿贡献），
    # 页面看起来完全正常。单股 `_mcap_gate` 一直是 `price * total_shares`，
    # 两条路径的答案因此长期不一致 —— 同一真相的第二个定义点。
    inst = repo.get_instruments_asset("stock")
    shares_map: dict[str, float] = {}
    if inst is not None and not inst.is_empty():
        for c in ("symbol", "total_shares"):
            if c not in inst.columns:
                break
        else:
            shares_map = {
                r["symbol"]: float(r["total_shares"])
                for r in inst.select(["symbol", "total_shares"]).to_dicts()
                if r.get("total_shares") is not None
            }

    # 按 symbol 分组，数「从最新一根往回连续跌破」的天数。
    #
    # 这里曾用 `for symbol, grp in df.group_by(...)` 逐票 Python 循环：5579 只票
    # 各建一次子 DataFrame 再 sort，实测 **4.45s**，占整个接口的 99%（同一份数据
    # `collect()` 只要 0.03s）。现在全部走表达式，0.03s。
    #
    # 语义与逐票版**逐票等价**，停止条件的定义只有一条：
    #   从末尾往前数，遇到「未破线」或「空值」就停；停止位置距末尾的天数即 streak；
    #   整个窗口都破线时 streak = 组内行数。
    # 空值必须停而**不计入** streak —— 尾部停牌（价格缺失）在原实现里 streak=0，
    # 若把它当成「破线」继续往前数，会把停牌前一天的破线也算进来。
    # `tests/test_delisting_guard.py` 里留了一份朴素逐股参考实现做对拍。
    df = df.filter(pl.col("symbol").is_not_null() & (pl.col("symbol") != "None"))
    df = df.sort(["symbol", "date"])

    boards = {s: classify_board(str(s).split(".")[0]) for s in df["symbol"].unique().to_list()}
    limits = {s: (_MCAP_MAIN if b == "主板" else _MCAP_OTHER) for s, b in boards.items()}

    scored = (
        df.with_columns([
            pl.col(price_col).cast(pl.Float64).alias("_p"),
            pl.int_range(pl.len()).over("symbol").alias("_i"),
            pl.len().over("symbol").alias("_n"),
        ])
        .with_columns([
            # 距末尾的天数：最后一行 _rev=0
            (pl.col("_n") - 1 - pl.col("_i")).alias("_rev"),
            pl.col("symbol").replace_strict(limits, default=None).alias("_lim"),
            pl.col("symbol").replace_strict(boards, default=None).alias("_board"),
            pl.col("symbol").replace_strict(shares_map, default=None).alias("_sh"),
        ])
        .with_columns(
            # 股本缺失或为 0（退市股维表常见 0）时市值「无法判定」，必须是 null
            # 而不是 0 —— 否则 `0 < 5 亿` 会让这些票凭空变成「市值破线」。
            pl.when(pl.col("_sh").is_not_null() & (pl.col("_sh") > 0))
            .then(pl.col("_p") * pl.col("_sh"))
            .otherwise(None)
            .alias("_mcap")
        )
        .with_columns([
            (pl.col("_p").is_null() | (pl.col("_p") >= _PAR_VALUE)).alias("_par_stop"),
            (
                pl.col("_p").is_null()
                | pl.col("_mcap").is_null()
                | (pl.col("_mcap") >= pl.col("_lim"))
            ).alias("_mcap_stop"),
        ])
        .group_by("symbol")
        .agg([
            pl.when(pl.col("_par_stop")).then(pl.col("_rev")).otherwise(None).min().alias("_ps"),
            pl.when(pl.col("_mcap_stop")).then(pl.col("_rev")).otherwise(None).min().alias("_ms"),
            pl.col("_n").first().alias("_n_rows"),
            pl.col("_p").last().alias("_last_p"),
            pl.col("_mcap").last().alias("_last_mcap"),
            pl.col("_board").first().alias("_board"),
        ])
        .with_columns([
            pl.coalesce([pl.col("_ps"), pl.col("_n_rows")]).alias("par_streak"),
            pl.coalesce([pl.col("_ms"), pl.col("_n_rows")]).alias("mcap_streak"),
        ])
        .filter((pl.col("par_streak") > 0) | (pl.col("mcap_streak") > 0))
        # 危险度排序：先触发面值、再看面值 streak 降序、再市值 streak。
        .sort(["par_streak", "mcap_streak"], descending=[True, True])
    )

    rows = [
        {
            "symbol": r["symbol"],
            "close": round(float(r["_last_p"]), 4) if r["_last_p"] is not None else None,
            "mcap": round(float(r["_last_mcap"]), 2) if r["_last_mcap"] is not None else None,
            "board": r["_board"],
            "par_streak": int(r["par_streak"]),
            "mcap_streak": int(r["mcap_streak"]),
            "streak_limit": _STREAK_LIMIT,
        }
        for r in scored.to_dicts()
    ]

    caveats: list[str] = []
    if not raw_is_true:
        caveats.append("本地缺 raw_close，面值/市值用前复权价估算，低价股可能失真。")

    return {"as_of": str(as_of), "countdown": rows, "caveats": caveats}
