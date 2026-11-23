"""个股分析 API — 关键价位 + AI 四维分析 + 报告持久化。

路由前缀: /api/stock-analysis

端点:
  GET  /levels?symbol=         11 类关键价位(图表 markLine 数据源)
  GET  /name-history?symbol=   简称轨迹(龙虎榜快照里的历史简称观测)
  POST /analyze                AI 流式四维分析(NDJSON)
  GET  /reports                历史报告列表
  POST /reports                保存一条报告
  DELETE /reports/{report_id}  删除一条报告
"""
from __future__ import annotations

import logging
import math
from datetime import date as _date
from datetime import timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.indicators.levels import compute_chip_distribution, compute_levels, summarize_levels
from app.market_time import cn_today
from app.services import entry_check
from app.services import shareholder as shareholder_svc
from app.services import stock_history_fill, stock_name_history, stock_reports
from app.services.delisting_guard import build_market_countdown, build_risk_guard
from app.services.ndjson_heartbeat import with_heartbeat
from app.services.stock_analyzer import analyze_stock_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-analysis", tags=["stock-analysis"])


def _as_date(v) -> _date:
    """polars/str/datetime 三态 → date。图表接口只认一种形状，转换点收在这里。

    注意顺序：`datetime` 也是 `date` 的实例，先判 `.date()` 才不会把
    00:00:00 带进 ISO 字符串（前端按 slice(0,10) 取日期）。
    """
    if hasattr(v, "hour") and callable(getattr(v, "date", None)):
        return v.date()
    if isinstance(v, _date):
        return v
    return _date.fromisoformat(str(v)[:10])


def _to_float_list(series: pl.Series) -> list:
    """polars Series → JSON 安全的 float 列表(null/NaN → None)。"""
    out: list = []
    for v in series.to_list():
        if v is None:
            out.append(None)
            continue
        try:
            f = float(v)
            out.append(round(f, 2) if math.isfinite(f) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _build_series(df: pl.DataFrame) -> dict:
    """提取带状指标(布林带 / Keltner通道 / ATR止损)的每日时间序列。

    这些指标的本质是"每日一条线",随 MA/ATR/σ 漂移,画成曲线才能体现通道形态。
    其余固定价位(枢轴/前高前低等)不在此,仍用水平 markLine。

    返回结构(每个 value 都是按日期对齐的数组):
      {
        "boll":      {"upper": [...], "lower": [...]},
        "keltner_s": {"upper": [...], "lower": [...]},   # 短期 MA20±2ATR
        "keltner_m": {"upper": [...], "lower": [...]},   # 中期 MA60±2.5ATR
        "keltner_l": {"upper": [...], "lower": [...]},   # 长期 MA120±3ATR
        "atr":       {"stop_loss": [...], "take_profit": [...]},  # close∓2ATR
      }
    """
    if df.is_empty() or "close" not in df.columns:
        return {}

    out: dict[str, dict] = {}
    close = df["close"]
    has_atr = "atr_14" in df.columns

    # 布林带(上/下/中轨;中轨 = MA20,数据层已预计算)
    if "boll_upper" in df.columns and "boll_lower" in df.columns:
        out["boll"] = {
            "upper": _to_float_list(df["boll_upper"]),
            "lower": _to_float_list(df["boll_lower"]),
            "mid": _to_float_list(df["ma20"]) if "ma20" in df.columns else None,
        }

    # Keltner 通道三档(需要 ATR)
    if has_atr:
        atr = df["atr_14"]
        # MA120 现场算(不在预计算列中)
        ma120 = df.select(pl.col("close").rolling_mean(120))["close"] if df.height >= 120 else None

        def _channel(ma: pl.Series, n: float) -> dict:
            return {
                "upper": _to_float_list(ma + n * atr),
                "lower": _to_float_list(ma - n * atr),
            }

        if "ma20" in df.columns:
            out["keltner_s"] = _channel(df["ma20"], 2.0)
        if "ma60" in df.columns:
            out["keltner_m"] = _channel(df["ma60"], 2.5)
        if ma120 is not None:
            out["keltner_l"] = _channel(ma120, 3.0)

        # ATR 止损/止盈: close ± 2×ATR(跟随行情漂移的动态止损线)
        out["atr"] = {
            "stop_loss": _to_float_list(close - 2 * atr),
            "take_profit": _to_float_list(close + 2 * atr),
        }

    return out


@router.get("/levels")
def get_levels(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    days: int = Query(120, ge=30, le=500, description="计算样本天数"),
):
    """计算 11 类关键价位(成交密集区压力支撑 / 枢轴点 / 前高前低 /
    布林带 / Keltner短中长 / ATR止损 / 缺口 / 斐波那契 / 整数关口)。

    返回 {levels: {sr, pivot, extreme, boll, keltner_s, keltner_m, keltner_l,
    atr_stop, gap, fib, round}, close, summary, dates, series}。
    前端按 levels 的 key 渲染开关按钮,逐组显隐 markLine / 曲线。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    end = cn_today()
    start = end - timedelta(days=days * 2)
    # 按资产类型分流: ETF/指数走独立 enriched 存储, 股票保持原路径
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return {"levels": {"sr": [], "pivot": [], "extreme": [],
                           "boll": [], "keltner_s": [], "keltner_m": [], "keltner_l": [],
                           "atr_stop": [], "gap": [], "fib": [], "round": []},
                "close": None, "summary": "无数据", "symbol": symbol,
                "dates": [], "series": {}}

    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    # 日期 + 带状曲线序列(供前端画 Keltner/ATR/布林带曲线)
    dates = df["date"].to_list()
    series = _build_series(df)
    # 筹码分布明细：与 levels["sr"] 共用同一个 `_chip_weights` 内核，
    # 所以分布图的最高柱与图上那条 POC 线必然指向同一个价位。
    chip = compute_chip_distribution(df)
    return {
        "levels": levels,
        "chip": chip,
        "close": close,
        "summary": summarize_levels(levels, close),
        "symbol": symbol,
        "dates": [str(d) for d in dates],
        "series": series,
    }


@router.get("/entry-point")
def get_entry_point(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
):
    """买点体检 —— 当前状态下「该不该下注」的风险档位，以及它的历史兑现。

    三条口径必须随数字返回，缺一条都会让这个面板变成误导：

      1. 这是**风险档位**，不是买入信号。低风险档历史胜率 51.0%、中位 +0.3%，
         只比抛硬币好一点；它能做的是把暴跌概率从 17.6% 压到 9.7%。
      2. 数字来自 `data/entry_check/stats.json`（回测产出，样本内）。文件缺失
         时返回 `calibrated=false`，页面显示"未校准"，不编数字。
      3. 每次调用都会**幂等记录**一条判定（symbol + as_of + criteria），
         到期（20 个交易日）后惰性结算，把实际胜率/暴跌率写回 journal。
         这是唯一能证伪上面那些历史数字的机制。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")
    repo = request.app.state.repo
    try:
        asset_type = repo.resolve_asset_type(symbol)
    except Exception:  # noqa: BLE001
        asset_type = "stock"
    if asset_type != "stock":
        return {
            "symbol": symbol, "available": False, "calibrated": False,
            "reason": f"{asset_type.upper()} 不做买点体检 —— 判据在个股横截面上标定",
        }
    try:
        # 户数分位属于股东模块的口径，这里复用而不是重算（同一真相一个定义点）
        prof = shareholder_svc.build_profile(repo, symbol)
        holder_pct = (prof.get("pressure") or {}).get("holder_pct")
        return entry_check.build(repo, symbol, holder_pct)
    except Exception as e:  # noqa: BLE001 - 附加信息失败不该让整页 500
        logger.warning("entry-point 失败 %s: %s", symbol, e)
        return {"symbol": symbol, "available": False, "calibrated": False,
                "reason": f"计算失败: {e}"}


@router.get("/shareholder")
def get_shareholder(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
):
    """股东结构 —— 户数集中度 / 十大流通股东 / 限售解禁 / 抛压象限。

    这个端点回答的是「谁在持有、筹码在集中还是扩散」，与同页其余模块
    （关键价位、退市红线、AI 四维）互不依赖。三条口径必须随数字返回：

      - 户数分位 = **同报告期全市场横截面**分位，不是该票自身的时间序列分位；
      - 户数只有**变化率**经回测有 alpha（spread -87bp/20日，t=-4.5，
        11/11 年同向），**水平**（户均市值、绝对户数）实测无效（t=-0.13），
        所以本端点不把"散户多"当成信号；
      - 象限收益常数来自 data/shareholder/quadrant_stats.json（回测产出），
        文件缺失时返回 null 而不是编一个数字。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")
    repo = request.app.state.repo
    try:
        return shareholder_svc.build_profile(repo, symbol)
    except Exception as e:  # noqa: BLE001 - 附加信息失败不该让整页 500
        logger.warning("shareholder profile 失败 %s: %s", symbol, e)
        return {"symbol": symbol, "available": False, "reason": f"读取失败: {e}"}


@router.get("/name-history")
def get_name_history(request: Request, symbol: str = Query(..., description="标的代码,如 600340.SH")):
    """简称轨迹 —— 这只票在本地快照里被看到过的每一个名字。

    个股分析页可以画两年以上的日 K，而维表只存**当前**简称。改名这件事
    （华夏幸福 → *ST华幸）在价格图形上是不可见的，只能靠历史快照还原：
    本地唯一带历史简称的表是龙虎榜 JSON 快照。

    返回的是观测，不是公告：`changes[].after/before` 是「前一个简称最后
    见到 / 后一个简称首次见到」，改名发生在这段区间内，具体哪一天本地无证据。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    current = None
    try:
        inst = repo.get_instruments_asset(repo.resolve_asset_type(symbol))
        if not inst.is_empty() and "name" in inst.columns:
            hit = inst.filter(pl.col("symbol") == symbol)
            if not hit.is_empty():
                current = hit["name"][0]
    except Exception as e:  # noqa: BLE001 - 维表读失败不该让简称轨迹整块不可用
        logger.debug("name-history: 维表当前简称读取失败 %s: %s", symbol, e)

    return stock_name_history.name_history(repo.store.data_dir, symbol, current_name=current)


@router.get("/risk-guard")
def get_risk_guard(request: Request, symbol: str = Query(..., description="标的代码,如 600340.SH")):
    """退市红线诊断 —— 面值 / 市值 / 净资产 / 净利润与营收 四条硬约束。

    个股分析页其余模块都在回答「它涨得像什么」，这个端点回答「它还活不活
    得下去」。两条交易类红线（面值 / 市值）按**不复权原始价**逐日数连续
    天数；财务类只认年报口径。算不出来的项返回 status="unknown" 并附
    caveats，不拿前复权价或季报冒充 —— 一只 1 元股被复权因子放大 2 倍后
    就会被判成「安全」，这种错误比不给结论更贵。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")
    repo = request.app.state.repo
    return build_risk_guard(repo, symbol).to_dict()


@router.get("/delisting-countdown")
def get_delisting_countdown(request: Request):
    """全市场退市「连续 20 日」倒计时 —— 只返回已进入倒数的票。

    与 /market-snapshot 的横截面「今天破没破线」不同，这个端点回答的是
    「连续破线几天了」：规则要的是连续 20 个交易日，不是「某天跌破」。
    一只票破面值 1 天和 19 天，对散户是两种完全不同的危险等级，但横截面
    快照里长得一模一样 —— 这就是市场常识页「退市生死全景」缺的那块确定性。

    口径与单股 /risk-guard 完全一致（原始价 raw_close 判面值、市值按板块
    阈值、连续 20 日），同一真相一个定义点。只含 par_streak>0 或
    mcap_streak>0 的票，未破线的票不占 payload。
    """
    repo = request.app.state.repo
    try:
        from app.services.screener import ScreenerService

        svc = ScreenerService(repo)
        as_of = svc.latest_date()
    except Exception:  # noqa: BLE001
        as_of = None
    if not as_of:
        from app.market_time import cn_today

        as_of = cn_today()
    return build_market_countdown(repo, as_of)


@router.get("/crowding-verdict")
def get_crowding_verdict(request: Request):
    """全市场换手率（拥挤度代理）→ 后续 20 日超额收益的判决。

    市场常识页「踩踏链」那条常识，原本用硬编码 danger 值画「趋同→拥挤→踩踏」。
    本端点用全市场日线算「高换手 vs 低换手」的后续超额，判决「拥挤到底危险不
    危险」。结论：大盘股放量滞涨真危险（高换手跑输约 3%），小盘股高换手不危险。
    """
    from app.services.market_percentiles import build_crowding_verdict

    return build_crowding_verdict(request.app.state.repo)


@router.get("/history")
def get_history(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 600340.SH"),
    start_date: str = Query(..., description="起始日期 YYYY-MM-DD"),
    end_date: str | None = Query(None, description="截止日期 YYYY-MM-DD, 默认北京今天"),
    fill: bool = Query(True, description="本地不足的头部是否用上游前复权补齐"),
):
    """个股分析图表的日 K —— 本地优先，缺的头部用上游**前复权**补齐（只读）。

    为什么不是 `/api/kline/daily`：那条路只在本地为空时整段回退到上游，而且
    回退分支用的是 `days`（默认 120）而不是请求区间 —— 一个两年的请求会静默
    拿到 150 根。这里改成分段拼接，并把"哪一段来自哪里"显式写进 segments。

    返回:
      rows:     {date, open, high, low, close, volume, amount} 升序
      segments: [{start, end, source: "local"|"upstream", n}]
      coverage: {local_first, local_last}
      upstream: 补齐那一段的取数元信息（attempted / fetched_rows / error / elapsed_ms）
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")
    try:
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date) if end_date else cn_today()
    except ValueError as e:
        raise HTTPException(400, f"日期格式错误: {e}") from e
    if start > end:
        raise HTTPException(400, "start_date 不能晚于 end_date")

    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    cols = ["date", "open", "high", "low", "close", "volume", "amount"]
    local = repo.get_daily_asset(asset_type, symbol, start, end, columns=cols)

    local_first = local["date"].min() if not local.is_empty() else None
    local_last = local["date"].max() if not local.is_empty() else None

    head = pl.DataFrame()
    upstream_meta: dict = {"attempted": False}
    if fill and (local_first is None or local_first > start):
        # 只有「本地覆盖之内确实存在交易日」的缺口才值得打一次上游。
        # 反例（实测）：请求 2024-09-01 是周日，首根 2024-09-02 —— 一天都不缺，
        # 却因为 local_first > start 去打了一次上游。判据用交易日列表，不用天数差。
        has_gap = local_first is None
        if local_first is not None:
            try:
                from app.services.trading_days import kline_trading_days

                days = kline_trading_days(repo.store.data_dir)
                # 交易日列表拿不到时退回"首根晚于起点"这条弱判据（宁可多补一次，
                # 不可因为一个辅助函数失败就少画一段）。
                has_gap = any(start <= d < local_first for d in days) if days else (local_first > start)
            except Exception:  # noqa: BLE001
                has_gap = local_first > start
        if has_gap:
            head, upstream_meta = stock_history_fill.fill_head(symbol, start, end, local_first)
            if upstream_meta.get("error"):
                logger.info("history fill 失败(按本地返回) %s: %s", symbol, upstream_meta["error"])

    parts: list[pl.DataFrame] = []
    if not head.is_empty():
        parts.append(head.select([c for c in cols if c in head.columns]))
    if not local.is_empty():
        parts.append(local.select([c for c in cols if c in local.columns]))

    if parts:
        df = pl.concat(parts, how="diagonal_relaxed").unique(subset=["date"], keep="last").sort("date")
    else:
        df = pl.DataFrame(schema={c: pl.Utf8 if c == "date" else pl.Float64 for c in cols})

    rows = df.to_dicts()
    for r in rows:  # 日期统一成字符串: 前端 slice(0,10) 的契约
        r["date"] = _as_date(r.get("date")).isoformat()

    segments: list[dict] = []
    if rows:
        head_dates = set(head["date"].to_list()) if not head.is_empty() else set()
        for r in rows:
            d = r["date"]
            src = "upstream" if _as_date(d) in head_dates else "local"
            if segments and segments[-1]["source"] == src:
                segments[-1]["end"] = d
                segments[-1]["n"] += 1
            else:
                segments.append({"start": d, "end": d, "source": src, "n": 1})

    name = _instrument_name(repo, symbol)
    return {
        "symbol": symbol,
        "name": name,
        # 维表里有没有这只票：用来区分"这只票不存在"与"这段区间没有K线"。
        # 前者换个区间也没用，后者换个区间就好 —— 两种情况不该共用一句话。
        "known": name is not None,
        "rows": rows,
        "segments": segments,
        "coverage": {
            "local_first": str(local_first) if local_first else None,
            "local_last": str(local_last) if local_last else None,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
        },
        "upstream": upstream_meta,
    }


def _instrument_name(repo, symbol: str) -> str | None:
    try:
        inst = repo.get_instruments_asset(repo.resolve_asset_type(symbol))
        if not inst.is_empty() and "name" in inst.columns:
            hit = inst.filter(pl.col("symbol") == symbol)
            if not hit.is_empty():
                return hit["name"][0]
    except Exception as e:  # noqa: BLE001
        logger.debug("history: 名称读取失败 %s: %s", symbol, e)
    return None


class AnalyzeRequest(BaseModel):
    """AI 个股分析请求。"""
    symbol: str
    focus: str = ""  # 可选:用户追加的分析关注点


@router.post("/analyze")
async def analyze_stock(request: Request, req: AnalyzeRequest):
    """AI 个股四维分析 — NDJSON 流式返回。

    组合 K 线(技术指标)+ 财务表 + 关键价位 → 客观技术分析提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。
    """
    if not req.symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    async def stream_gen():
        async for chunk in with_heartbeat(analyze_stock_stream(repo, data_dir, req.symbol, req.focus)):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# 报告 CRUD(历史报告持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 个股分析报告。"""
    symbol: str
    name: str = ""
    focus: str = ""
    content: str
    summary: str = ""
    close: float | None = None
    levels: dict | None = None


@router.get("/reports")
def list_reports(request: Request):
    """获取全部历史报告(按时间降序,后端已裁剪到上限)。"""
    return {"reports": stock_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条报告。"""
    report = stock_reports.save_report({
        "symbol": req.symbol,
        "name": req.name,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "close": req.close,
        "levels": req.levels,
    })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条报告。"""
    ok = stock_reports.delete_report(report_id)
    return {"ok": ok}
