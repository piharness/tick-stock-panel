"""叙事账本（/narrative）的每日读数 —— 唯一装配点。

这一页不产出信号，只回答三个问题：
  1. 今天的温度是多少（涨停梯队、炸板率、赚钱效应、成交额）；
  2. 钱从哪条腿来（两融 / 龙虎榜 / 大宗 / ETF 占成交额比）；
  3. 市场在用什么词讲这件事（龙虎榜上榜理由的高频词 + 净买额）。

口径纪律（改数之前先读）：
  - 温度**不重算**：直接读 `regime_history`（`regime_builder` 是唯一实现），
    这里只补分位。另建一份涨停统计 = 第二个定义点，`/chip` 页为此付过学费。
  - 主线**不重算**：读 `mainline_history`（`market_mainline` 唯一实现），
    本模块只把「板块涨幅 / 成交额占比」这一层派生出来。
  - 指数波动用「全历史回填 + 近端实盘」拼成一条序列，拼完去重，只用一次定义。
  - 每个块自带 `as_of`：四个数据源时滞不同（两融 T−6、大宗 T−2、主线 T−0），
    页面按块标注，不假装同一天。

数据源时滞是常态，不是错误：块失败时返回 `{"ok": False, "error": ...}`，
前端显示「缺」，不静默用上一日顶替。
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls, timedelta
from pathlib import Path

import polars as pl

from app.services.daily_panel import load_panel

logger = logging.getLogger(__name__)

# 主要宽基：上交所 4 只 + 深交所 2 只。名称在页面侧固定，后端只给数。
INDEX_SPECS = [
    ("000001.SH", "上证指数"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
]
FULL_INDEX = "backfill_20261117/index_daily_full.parquet"

# 上榜理由里没有信息量的词（制度标签，不是题材）
_STOPWORDS = {
    "融资融券", "沪股通", "深股通", "机构专用", "券商重仓", "预盈预增", "预亏预减",
    "MSCI中国", "标准普尔", "富时罗素", "深成500", "上证180", "中证500", "沪深300",
    "创业板综", "转债标的", "股权激励", "股权转让", "参股银行", "参股券商",
}
_SEP = "+、,，;；/|· （）()【】[]《》"
# 引号是排版噪声，出现在词中间（“羊”字辈）时也要去掉，不能只 strip 两端。
_QUOTES = str.maketrans('', '', '“”‘’\"\'')


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _pct_rank(series: pl.Series, value: float) -> float | None:
    """value 在 series 里的分位（0~100）。series 允许含空。"""
    s = series.drop_nulls()
    if s.is_empty() or value is None:
        return None
    return round(float((s <= value).sum()) / s.len() * 100, 1)


# ---------------------------------------------------------------- 温度

def _temp(data_dir: Path) -> dict:
    """情绪温度：读 regime_history 最后一行，补 250 日分位。"""
    p = data_dir / "regime_history" / "part.parquet"
    if not p.exists():
        return {"ok": False, "error": "regime_history 不存在"}
    df = pl.read_parquet(p).sort("date")
    if df.is_empty():
        return {"ok": False, "error": "regime_history 为空"}
    last = df.tail(1).to_dicts()[0]
    tail = df.tail(250)
    keep = ["limit_up", "limit_down", "broken_limit", "max_consecutive", "seal_rate",
            "up_ratio", "total_amount", "ladder_completeness", "score"]
    pct = {k: _pct_rank(tail[k], last.get(k)) for k in keep if k in df.columns}
    return {
        "ok": True,
        "as_of": _iso(last["date"]),
        "phase": last.get("phase"),
        "state": last.get("state"),
        "score": last.get("score"),
        "limit_up": last.get("limit_up"),
        "limit_down": last.get("limit_down"),
        "broken_limit": last.get("broken_limit"),
        "seal_rate": last.get("seal_rate"),
        "max_consecutive": last.get("max_consecutive"),
        "ladder_completeness": last.get("ladder_completeness"),
        "up_ratio": last.get("up_ratio"),
        "up_count": last.get("up_count"),
        "down_count": last.get("down_count"),
        "total_amount_yi": round((last.get("total_amount") or 0) / 1e8, 0),
        "first_board": last.get("first_board"),
        "ge2_count": last.get("ge2_count"),
        "promo_rate": last.get("promo_rate"),
        "pct": pct,
        "sample_days": tail.height,
    }


# ---------------------------------------------------------------- 主线

def _mainline(repo, data_dir: Path, top: int = 10) -> dict:
    """当日主线榜 + 板块 5/20 日涨幅 + 成交额占比。"""
    from app.services.market_mainline import load_mainline_history
    from app.services.rps_rotation import _load_concept_map_df

    hist = load_mainline_history(data_dir, "concept")
    if hist.is_empty():
        return {"ok": False, "error": "mainline_history 为空"}
    last_day = hist["date"].max()
    rows = hist.filter((pl.col("date") == last_day) & (pl.col("rank") <= top))
    if rows.is_empty():
        return {"ok": False, "error": f"{_iso(last_day)} 无主线行"}

    start = last_day - timedelta(days=60)
    panel = load_panel(data_dir, start, last_day)
    if panel.is_empty():
        return {"ok": False, "error": "日线面板为空"}
    panel = panel.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret")
    ).drop_nulls("ret")

    map_df, _ = _load_concept_map_df(repo, "concept")
    if map_df.is_empty():
        return {"ok": False, "error": "概念成分缺失（ext_gn_ths 未抓取）"}
    joined = panel.join(map_df, left_on=pl.col("symbol").str.to_uppercase(), right_on="_sym_up", how="inner")

    agg = joined.group_by(["concept", "date"]).agg(
        pl.col("ret").mean().alias("ret"), pl.col("amount").sum().alias("amt")
    ).sort(["concept", "date"])

    # 每个概念取末日往前 20 个交易日的复利
    def _cum(n: int) -> pl.DataFrame:
        return (
            agg.with_columns((1 + pl.col("ret")).log().alias("lr"))
            .with_columns(pl.col("lr").rolling_sum(n, min_samples=n).over("concept").alias(f"lr{n}"))
            .select(["concept", "date", (pl.col(f"lr{n}").exp() - 1).alias(f"r{n}")])
        )

    c5, c20 = _cum(5), _cum(20)
    last_amt = agg.filter(pl.col("date") == last_day).select(["concept", "amt"])
    total_amt = float(last_amt["amt"].sum()) or 1.0
    last_amt = last_amt.with_columns((pl.col("amt") / total_amt * 100).alias("amt_share"))

    enriched = (
        rows.join(c5.filter(pl.col("date") == last_day).select(["concept", "r5"]),
                  left_on="member", right_on="concept", how="left")
        .join(c20.filter(pl.col("date") == last_day).select(["concept", "r20"]),
              left_on="member", right_on="concept", how="left")
        .join(last_amt.select(["concept", "amt_share"]), left_on="member", right_on="concept", how="left")
    )

    names = _instrument_names(data_dir)
    out = []
    for r in enriched.sort("rank").to_dicts():
        out.append({
            "rank": r["rank"],
            "member": r["member"],
            "limit_up_count": r.get("limit_up_count"),
            "ge2_count": r.get("ge2_count"),
            "max_boards": r.get("max_boards"),
            "rungs_filled": r.get("rungs_filled"),
            "score": round(r.get("score") or 0, 1),
            "leader_symbol": r.get("leader_symbol"),
            "leader_name": names.get(r.get("leader_symbol") or "", ""),
            "r5_pct": round((r.get("r5") or 0) * 100, 2),
            "r20_pct": round((r.get("r20") or 0) * 100, 2),
            "amt_share_pct": round(r.get("amt_share") or 0, 2),
        })
    return {"ok": True, "as_of": _iso(last_day), "rows": out}


def _instrument_names(data_dir: Path) -> dict[str, str]:
    for rel in ("instruments/instruments.parquet", "instruments_etf/instruments_etf.parquet"):
        p = data_dir / rel
        if p.exists():
            try:
                df = pl.read_parquet(p, columns=["symbol", "name"])
                return dict(zip(df["symbol"].to_list(), df["name"].to_list()))
            except Exception:  # noqa: BLE001
                continue
    return {}


# ---------------------------------------------------------------- 资金四路

def _flows(data_dir: Path) -> dict:
    out: dict = {"ok": True}

    # 1) 两融：余额 / 占流通市值 / 5 日净买
    try:
        df = pl.read_parquet(data_dir / "margin" / "daily.parquet").sort("date")
        last = df.tail(1).to_dicts()[0]
        net5 = float(df.tail(5)["fin_net_buy"].sum())
        out["margin"] = {
            "ok": True, "as_of": _iso(last["date"]),
            "balance_yi": round(float(last["fin_balance"]), 0),
            "pct_float": round(float(last["fin_pct_float"]), 2),
            "pct_float_rank": _pct_rank(df.tail(250)["fin_pct_float"], last["fin_pct_float"]),
            "net5_yi": round(net5, 0),
        }
    except Exception as e:  # noqa: BLE001
        out["margin"] = {"ok": False, "error": str(e)[:120]}

    # 2) 龙虎榜：上榜家数 + 净买入合计（读本地缓存，不联网）
    try:
        files = sorted((data_dir / "dragon_tiger").glob("date=*.json"))[-5:]
        net, n_stock, days = 0.0, 0, []
        for f in files:
            payload = json.loads(f.read_text(encoding="utf-8"))
            block = payload.get("all") or {}
            items = block.get("stock_items") or []
            seen = {}
            for it in items:
                key = it.get("thscode") or it.get("ticker")
                if key and key not in seen:
                    seen[key] = float(it.get("net_value") or 0)
            net += sum(seen.values())
            n_stock += len(seen)
            days.append(payload.get("trade_date") or f.stem.replace("date=", ""))
        out["dragon"] = {
            "ok": True, "as_of": days[-1] if days else None, "days": len(files),
            "net_yi": round(net / 1e8, 2), "stocks": n_stock,
        }
    except Exception as e:  # noqa: BLE001
        out["dragon"] = {"ok": False, "error": str(e)[:120]}

    # 3) 大宗：近 5 个交易日成交额 + 折价中位
    try:
        p = data_dir / "block_trade" / "year=2026" / "part.parquet"
        df = pl.read_parquet(p, columns=["date", "amt", "premium_doc"]).drop_nulls("amt")
        # 折价中位在「协议转让按收盘价成交」的那一半里恒为 0，单独看没有信息量。
        # 拆成两件事：折价成交的笔数占比 + 只对折价笔取中位。
        by_day = df.group_by("date").agg(
            pl.col("amt").sum().alias("amt"), pl.len().alias("n")
        ).sort("date").tail(5)
        # 折价占比必须与「近 5 日」对齐：按最后 5 个交易日过滤，不按行数截断
        # （单日 60~200 笔，按 600 行截会混进第 8 天）。
        days5 = by_day["date"].to_list()
        recent = df.filter(pl.col("date").is_in(days5))
        neg = recent.filter(pl.col("premium_doc") < 0)
        last = by_day.tail(1).to_dicts()[0] if by_day.height else {}
        out["block"] = {
            "ok": True,
            "as_of": _iso(last.get("date")) if last else None,
            "amt5_yi": round(float(by_day["amt"].sum()) / 1e8, 1) if by_day.height else None,
            "neg_share": round(neg.height / recent.height * 100, 1) if recent.height else None,
            "neg_med": round(float(neg["premium_doc"].median()) * 100, 2) if neg.height else None,
            "deals": int(by_day["n"].sum()) if by_day.height else 0,
        }
    except Exception as e:  # noqa: BLE001
        out["block"] = {"ok": False, "error": str(e)[:120]}

    # 4) ETF 成交额占全市场比：ETF 是叙事的逐日结算工具。
    #    只读当日两个分区。**不做历史分位**：容器里 /app/data 是 Windows bind mount，
    #    单分区 read 约 240ms（实测 40 天 = 9.6s），为了一个分位把首屏拖到 10s 不值。
    #    要分位就走离线脚本，不要放在请求路径上。
    try:
        etf_file = sorted((data_dir / "kline_etf_daily").glob("date=*/part.parquet"))[-1]
        day = etf_file.parent.name.split("=", 1)[1]
        stk_file = data_dir / "kline_daily_enriched" / f"date={day}" / "part.parquet"
        etf_amt = float(pl.read_parquet(etf_file, columns=["amount"])["amount"].sum())
        stk_amt = float(pl.read_parquet(stk_file, columns=["amount"])["amount"].sum())
        out["etf"] = {
            "ok": True,
            "as_of": day,
            "share_pct": round(etf_amt / (etf_amt + stk_amt) * 100, 2),
            "amt_yi": round(etf_amt / 1e8, 0),
            "market_amt_yi": round((etf_amt + stk_amt) / 1e8, 0),
        }
    except Exception as e:  # noqa: BLE001
        out["etf"] = {"ok": False, "error": str(e)[:120]}

    # 四路时滞不同，父块的 as_of 取最晚的那一路（页面各卡仍标自己的）。
    stamps = [v.get("as_of") for k, v in out.items() if isinstance(v, dict) and v.get("ok")]
    out["as_of"] = max((s for s in stamps if s), default=None)
    return out


# ---------------------------------------------------------------- 指数波动

def _index(data_dir: Path) -> dict:
    live = sorted((data_dir / "kline_index_daily").glob("date=*/part.parquet"))
    if not live:
        return {"ok": False, "error": "kline_index_daily 为空"}
    # 分区列集不一致（部分日分区多一列 quote_ts）⇒ 用 scan + extra_columns=ignore，
    # 只投影需要的四列；直接 read_parquet 会在 concat 时报 schema lengths differ。
    tail = pl.scan_parquet([str(f) for f in live[-40:]], extra_columns="ignore").select(
        ["symbol", "date", "open", "close"]
    ).collect()
    if tail.is_empty():
        return {"ok": False, "error": "近端指数分区为空"}

    full_p = data_dir / FULL_INDEX
    frames = [tail]
    if full_p.exists():
        full = pl.read_parquet(full_p, columns=["symbol", "date", "open", "close"])
        frames.append(full.filter(pl.col("date") < tail["date"].min()))
    px = pl.concat(frames, how="vertical_relaxed").unique(subset=["symbol", "date"], keep="last").sort(["symbol", "date"])

    rows = []
    for sym, name in INDEX_SPECS:
        s = px.filter(pl.col("symbol") == sym)
        if s.height < 80:
            continue
        s = s.with_columns([
            (pl.col("close") / pl.col("close").shift(1) - 1).alias("ret"),
            (pl.col("open") / pl.col("close").shift(1) - 1).alias("on"),
        ]).with_columns(
            (pl.col("close") / pl.col("open") - 1).alias("id")
        ).drop_nulls("ret")
        vol20 = float(s.tail(20)["ret"].std() * (250 ** 0.5) * 100)
        vol_series = s.with_columns(
            (pl.col("ret").rolling_std(20) * (250 ** 0.5) * 100).alias("v20")
        )["v20"].drop_nulls()
        last = s.tail(1).to_dicts()[0]
        on5 = float((1 + s.tail(5)["on"]).product() - 1) * 100
        id5 = float((1 + s.tail(5)["id"]).product() - 1) * 100
        rows.append({
            "symbol": sym, "name": name, "close": round(float(last["close"]), 2),
            "ret1_pct": round(float(last["ret"]) * 100, 2),
            "vol20_pct": round(vol20, 1),
            "vol_rank": _pct_rank(vol_series, vol20),
            "on5_pct": round(on5, 2), "id5_pct": round(id5, 2),
            "window_days": s.height,
        })
    return {"ok": True, "as_of": _iso(tail["date"].max()), "rows": rows, "etf_rows": _broad_etf(px, data_dir)}


# 宽基 ETF → 标的指数。宽基 ETF 是叙事的逐日结算工具：份额申赎在收盘完成，
# 折溢价当天被套利抹平，所以它能读的只有「波动」与「跟踪偏离」，读不出方向。
BROAD_ETFS = [
    ("510300.SH", "沪深300ETF", "000300.SH"),
    ("510500.SH", "中证500ETF", "000905.SH"),
    ("512100.SH", "中证1000ETF", "000852.SH"),
    ("159915.SZ", "创业板ETF", "399006.SZ"),
    ("588000.SH", "科创50ETF", "000688.SH"),
    ("510050.SH", "上证50ETF", "000016.SH"),
]


def _broad_etf(px: pl.DataFrame, data_dir: Path) -> list[dict]:
    """宽基 ETF 的波动与 5 日跟踪偏离（ETF 收益 − 标的指数收益）。

    ETF 的日线本地只保留 260 个分区（滚动一年），这里只扫最近 130 个，
    所以波动分位是「近 130 日分位」，不是全历史——与指数那一列口径不同，页面分开标注。
    """
    all_files = sorted((data_dir / "kline_etf_daily").glob("date=*/part.parquet"))
    files = all_files[-130:]  # 只取近半年：全量 260 个分区在 bind mount 上要多花约 1.5s
    if not files:
        return []
    syms = [e for e, _, _ in BROAD_ETFS]
    etf = (
        pl.scan_parquet([str(f) for f in files], extra_columns="ignore")
        .select(["symbol", "date", "close", "amount"])
        .filter(pl.col("symbol").is_in(syms))
        .collect()
        .sort(["symbol", "date"])
    )
    if etf.is_empty():
        return []
    out = []
    for sym, name, idx in BROAD_ETFS:
        s = etf.filter(pl.col("symbol") == sym).with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1).alias("ret")
        ).drop_nulls("ret")
        if s.height < 60:
            continue
        vol20 = float(s.tail(20)["ret"].std() * (250 ** 0.5) * 100)
        vol_series = s.with_columns(
            (pl.col("ret").rolling_std(20) * (250 ** 0.5) * 100).alias("v20")
        )["v20"].drop_nulls()
        r5_etf = float((1 + s.tail(5)["ret"]).product() - 1) * 100

        b = px.filter(pl.col("symbol") == idx)
        if b.height < 10:
            continue
        b = b.with_columns((pl.col("close") / pl.col("close").shift(1) - 1).alias("ret")).drop_nulls("ret")
        r5_idx = float((1 + b.tail(5)["ret"]).product() - 1) * 100
        out.append({
            "symbol": sym, "name": name, "index_symbol": idx,
            "close": round(float(s.tail(1)["close"][0]), 3),
            "ret1_pct": round(float(s.tail(1)["ret"][0]) * 100, 2),
            "vol20_pct": round(vol20, 1),
            "vol_rank": _pct_rank(vol_series, vol20),
            "r5_pct": round(r5_etf, 2),
            "track5_pp": round(r5_etf - r5_idx, 2),
            "amt_yi": round(float(s.tail(1)["amount"][0]) / 1e8, 1),
            "window_days": s.height,
        })
    return out


# ---------------------------------------------------------------- 词频

def _words(data_dir: Path, days: int = 5, top: int = 12) -> dict:
    """龙虎榜上榜理由的高频词 —— 市场在用哪些词讲今天的事。

    这是最接近「叙事」本身的可观测量：理由文本是券商/交易所对异动原因的
    标准化记录，词频等于当天被反复引用的因果标签。
    """
    files = sorted((data_dir / "dragon_tiger").glob("date=*.json"))[-days:]
    if not files:
        return {"ok": False, "error": "无龙虎榜缓存"}
    counts: dict[str, int] = {}
    money: dict[str, float] = {}
    as_of = None
    for f in files:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        as_of = payload.get("trade_date") or as_of
        for it in (payload.get("all") or {}).get("stock_items") or []:
            reason = it.get("limit_reason") or ""
            net = float(it.get("net_value") or 0)
            for tok in _split_reason(reason):
                if tok in _STOPWORDS or len(tok) < 2 or len(tok) > 14:
                    continue
                counts[tok] = counts.get(tok, 0) + 1
                money[tok] = money.get(tok, 0.0) + net
    rows = [
        {"word": w, "n": c, "net_yi": round(money[w] / 1e8, 2)}
        for w, c in sorted(counts.items(), key=lambda kv: (-kv[1], -abs(money.get(kv[0], 0))))[:top]
    ]
    return {"ok": True, "as_of": as_of, "days": len(files), "rows": rows}


def _split_reason(text: str) -> list[str]:
    out, buf = [], []
    for ch in text:
        if ch in _SEP:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [t.strip("：:").translate(_QUOTES) for t in out if t.translate(_QUOTES).strip()]


# ---------------------------------------------------------------- 事件腿

def _events(data_dir: Path) -> dict:
    """解禁与定增：供给侧日历 + 已结算的量。

    只读事件表（`analysis/_fetch_events.py` 抓的东财两张表），不碰行情——
    这里给的是「什么时候有多少股票变成可卖」，不是收益预测。
    收益读数（解禁前 −276bp）是冻结的研究结论，放在页面 data.ts，不在这里重算。
    """
    lk_p = data_dir / "events" / "lockup.parquet"
    if not lk_p.exists():
        return {"ok": False, "error": "events/lockup.parquet 不存在，先跑 analysis/_fetch_events.py"}
    lk = pl.read_parquet(lk_p, columns=["symbol", "name", "free_date", "lift_mcap_wan", "ratio_float", "kind"])
    lk = lk.with_columns(pl.col("free_date").cast(pl.Utf8).str.slice(0, 10).alias("d"))

    # 数据日：取指数分区最后一天（事件表可能已含未来日期，不能拿它当 as_of）
    idx = sorted((data_dir / "kline_index_daily").glob("date=*/part.parquet"))
    asof = idx[-1].parent.name.split("=", 1)[1] if idx else max(lk["d"].to_list())
    asof_d = date_cls.fromisoformat(asof)

    up = lk.filter((pl.col("d") > asof) & (pl.col("d") <= (asof_d + timedelta(days=45)).isoformat()))
    by_day = (
        up.group_by("d").agg(
            pl.len().alias("n"),
            (pl.col("lift_mcap_wan").sum() / 1e4).round(1).alias("mcap_yi"),
        ).sort("d")
    )
    top = (
        up.sort("lift_mcap_wan", descending=True).head(8)
        .select([
            pl.col("d").alias("date"), "symbol", "name", "kind",
            (pl.col("lift_mcap_wan") / 1e4).round(1).alias("mcap_yi"),
            (pl.col("ratio_float") * 100).round(2).alias("ratio_pct"),
        ])
    )

    lo = (asof_d - timedelta(days=30)).isoformat()
    recent = lk.filter((pl.col("d") <= asof) & (pl.col("d") > lo))

    seo_out = {"n": 0, "raise_yi": 0.0}
    seo_p = data_dir / "events" / "seo.parquet"
    if seo_p.exists():
        seo = pl.read_parquet(seo_p, columns=["listing_date", "raise_yuan"])
        seo = seo.with_columns(pl.col("listing_date").cast(pl.Utf8).str.slice(0, 10).alias("d"))
        s90 = seo.filter((pl.col("d") <= asof) & (pl.col("d") > (asof_d - timedelta(days=90)).isoformat()))
        seo_out = {
            "n": int(s90.height),
            "raise_yi": round(float(s90["raise_yuan"].sum() or 0) / 1e8, 0),
        }

    return {
        "ok": True,
        "as_of": asof,
        "next": {
            "days": int(by_day.height),
            "mcap_yi": round(float(by_day["mcap_yi"].sum()), 1) if by_day.height else 0.0,
            "n": int(by_day["n"].sum()) if by_day.height else 0,
            "by_day": by_day.to_dicts(),
        },
        "top": top.to_dicts(),
        "recent30": {
            "mcap_yi": round(float(recent["lift_mcap_wan"].sum() or 0) / 1e4, 1),
            "n": int(recent.height),
        },
        "seo90": seo_out,
    }


# ---------------------------------------------------------------- 入口

def build(repo) -> dict:
    """五块并行装配。

    五个块互不依赖（温度/主线/资金/指数/词频读的是不同文件），串行 3.9s、并行约 2s
    —— polars 的读取在 Rust 侧释放 GIL，所以线程池确实能重叠 I/O 等待。
    块内异常只降级自己：一个源没更新不该让整页白屏。
    """
    data_dir = Path(repo.store.data_dir)
    tasks = {
        "temp": lambda: _temp(data_dir),
        "mainline": lambda: _mainline(repo, data_dir),
        "flows": lambda: _flows(data_dir),
        "index": lambda: _index(data_dir),
        "words": lambda: _words(data_dir),
        "events": lambda: _events(data_dir),
    }
    blocks: dict[str, dict] = {}

    def _run(item: tuple[str, object]) -> tuple[str, dict]:
        key, fn = item
        try:
            return key, fn()  # type: ignore[operator]
        except Exception as e:  # noqa: BLE001
            logger.warning("narrative_ledger.%s 失败: %s", key, e)
            return key, {"ok": False, "error": str(e)[:160]}

    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="narrative") as pool:
        for key, payload in pool.map(_run, tasks.items()):
            blocks[key] = payload
    as_of = max((b.get("as_of") for b in blocks.values() if b.get("as_of")), default=None)
    return {
        "as_of": as_of,
        "blocks": blocks,
        "disclaimer": (
            "本页是读数，不是信号。概念成分为当前快照回看历史（本地自 2026-07 留存），"
            "早年归属有漂移；四个数据源时滞不同，按块标注 as_of。"
        ),
    }
