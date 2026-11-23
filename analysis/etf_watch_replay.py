"""历史买点回放与信号追踪 —— 用同一段 scan 规则逐日重算，不另写一套判定。

为什么需要它：`etf_watch.py` 只保存最新一份快照；「今天六个候选是提前几天出现的，还是
跌了几天才出现」这个问题，靠最新快照答不了。本脚本把最近 N 个交易日逐日回放，
输出三样东西：

1. `snapshots`：每个交易日的完整行业快照（与 latest.json 同结构，去掉 ledger/data）；
2. `tracks`：最新候选行业的逐日状态、首次触发、连续天数、退出/再进入段；
3. `outcomes`：首次触发日之后 5/10/20 日收益与持有期 MAE/MFE（盘中极值，事后评价，不参与判定）。

三条纪律：
- 判定只调用 `etf_watch.scan`，不复制 F1/F2/F3/F6；`build_context` 只把与日期无关的
  nav/features/regime 预计算一次。
- 停更检查已在 scan 内改为「只看 asof 及以前」，避免回放偷看未来。
- 行业分类/ETF 池无法做到严格 point-in-time，写进 `meta.point_in_time` 并在页面明示；
  不把「当前在市基金 + 当前名称映射」包装成「当时可见」。

用法：
    python analysis/etf_watch_replay.py                 # 最近 120 个交易日
    python analysis/etf_watch_replay.py --lookback 60
    python analysis/etf_watch_replay.py --end 2026-11-20 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402  （判定唯一产地）
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/replay.json"
RULE_VERSION = "F1/F2/F3/F6 @ 2026-11-22"
HORIZONS = (5, 10, 20)
HOLD_DAYS = 20

POINT_IN_TIME = {
    "liquidity": "F6 按每个交易日当日的 amt20 选同行业最大 ETF，不使用最新排名倒推",
    "prices": "close_adj 与盘中 hi_adj/lo_adj 都由 _dejump 摊平后算出，与 _diag_industry_etf_asym 同一口径",
    "regime": "沪深300 状态只用当日及以前数据",
    "stale": "行业停更检查只看 asof 及以前（已修正历史回放的前视）",
    "industry_classification": (
        "行业分类用当前 instruments_etf 名称映射；数据源没有历史名称/行业变更，"
        "分类前视未消除（窗口内影响有限但非零）"
    ),
    "universe": (
        "ETF 池为当前在市基金；已退市/清盘基金不在 etf_daily_full 中，"
        "生存偏差未消除"
    ),
}


# ---------------------------------------------------------------- 轨迹

def _runs(days: list[dict]) -> list[list[dict]]:
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for d in days:
        if d["status"] == "CANDIDATE":
            cur.append(d)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def build_track(industry: str, days: list[dict]) -> dict:
    """把一条行业-日序列压成「首次/连续/退出/再进入」。days 必须按日期升序。"""
    runs = _runs(days)
    triggers = [d for d in days if d["status"] == "CANDIDATE"]
    first = triggers[0] if triggers else None
    last = days[-1] if days else None
    carrier_first = first.get("best_name") if first else None
    carrier_first_symbol = first.get("best_symbol") if first else None
    carrier_now = last.get("best_name") if last else None
    return {
        "industry": industry,
        "name": carrier_now or carrier_first or industry,
        "symbol": (last.get("best_symbol") if last else None) or carrier_first_symbol,
        "first_trigger": first["date"] if first else None,
        "last_trigger": triggers[-1]["date"] if triggers else None,
        "trigger_count": len(triggers),
        "max_consecutive": max((len(r) for r in runs), default=0),
        "left_censored": bool(days and days[0]["status"] == "CANDIDATE"),
        "carrier_at_first": carrier_first,
        "carrier_at_first_symbol": carrier_first_symbol,
        "carrier_changed": bool(carrier_first and carrier_now and carrier_first != carrier_now),
        "segments": [
            {"start": r[0]["date"], "end": r[-1]["date"], "days": len(r)}
            for r in runs
        ],
        "days": days,
    }


def _px(frame: pd.DataFrame, col: str, day: pd.Timestamp) -> float | None:
    if day not in frame.index:
        return None
    v = frame.at[day, col]
    return float(v) if np.isfinite(v) else None


def build_outcome(track: dict, sessions: list[pd.Timestamp],
                  pos: dict[pd.Timestamp, int], bars: dict[str, pd.DataFrame],
                  end: pd.Timestamp) -> dict:
    """首次触发 → 次日收盘进场 → 5/10/20 日；MAE/MFE 用持有期**盘中**极值。

    收益是事后评价：它只读取触发日之后的价格，绝不回写 track 的 status。

    为什么 MAE 用日线 low 而不是收盘：收盘序列看不到「盘中跌破、收盘拉回」，
    会把一次 −10% 的浮亏记成 0。`mae_close` 保留收盘口径只为审计两者差多少，
    不要拿它做仓位判断。
    """
    base = {
        "industry": track["industry"],
        "name": track["name"],
        # symbol 必须是「算 MAE 用的那只」＝首次触发日载体；最新载体另存 symbol_now。
        # 之前这里写的是 track["symbol"]（最新载体），名字是第一载体、价格是第二载体，
        # 拿 symbol 复算的人会得到另一组数。
        "symbol": track.get("carrier_at_first_symbol") or track["symbol"],
        "symbol_now": track["symbol"],
        "first_trigger": track["first_trigger"],
        "entry": None,
        "carrier": track.get("carrier_at_first"),
        "to_end": None,
        "entry_to_end": None,
        "ret5": None,
        "ret10": None,
        "ret20": None,
        "mae": None,
        "mfe": None,
        "mae_close": None,
        "mfe_close": None,
        "note": "",
    }
    first = next((d for d in track["days"] if d["status"] == "CANDIDATE"), None)
    if first is None:
        base["note"] = "窗口内未触发候选"
        return base

    sym = first.get("best_symbol") or track.get("symbol")
    base["symbol"] = sym
    frame = bars.get(sym) if sym else None
    if frame is None or frame.empty:
        base["note"] = "载体价格缺失，无法评价"
        return base

    i = pos.get(pd.Timestamp(first["date"]))
    if i is None or i + 1 >= len(sessions):
        base["note"] = "触发日之后没有下一交易日，无法进场"
        return base

    entry_date = sessions[i + 1]
    trigger_px = _px(frame, "close_adj", pd.Timestamp(first["date"]))
    entry_px = _px(frame, "close_adj", entry_date)
    end_px = _px(frame, "close_adj", end)
    if trigger_px is None or entry_px is None:
        base["note"] = "触发日/进场日价格缺失"
        return base

    base.update({
        "entry": str(entry_date.date()),
        "carrier": first.get("best_name") or track.get("carrier_at_first"),
        "to_end": round(float(end_px / trigger_px - 1), 6) if end_px is not None else None,
        "entry_to_end": round(float(end_px / entry_px - 1), 6) if end_px is not None else None,
    })

    for h in HORIZONS:
        exit_idx = i + 1 + h
        if exit_idx < len(sessions):
            exit_px = _px(frame, "close_adj", sessions[exit_idx])
            if exit_px is not None:
                base[f"ret{h}"] = round(float(exit_px / entry_px - 1), 6)

    hold_end = min(i + 1 + HOLD_DAYS, len(sessions) - 1)
    if hold_end > i + 1:
        # 收盘口径：含进场日收盘（等于进场价，不改变极值），与旧版逐位可比。
        w_close = frame.reindex(sessions[i + 1:hold_end + 1])
        # 盘中口径：从进场日的下一天算起 —— 进场日盘内的低点发生在买入之前，不算回撤。
        w_intra = frame.reindex(sessions[i + 2:hold_end + 1])
        if len(w_close):
            base["mae_close"] = round(float(w_close["close_adj"].min() / entry_px - 1), 6)
            base["mfe_close"] = round(float(w_close["close_adj"].max() / entry_px - 1), 6)
        lo = w_intra["lo_adj"].min() if len(w_intra) else np.nan
        hi = w_intra["hi_adj"].max() if len(w_intra) else np.nan
        if np.isfinite(lo):
            base["mae"] = round(float(lo / entry_px - 1), 6)
        if np.isfinite(hi):
            base["mfe"] = round(float(hi / entry_px - 1), 6)

    notes: list[str] = []
    if track.get("left_censored"):
        notes.append("窗口左端已是候选，真实首次可能更早")
    if hold_end < i + 1 + HOLD_DAYS:
        notes.append("20 日持有窗口未走完，MAE/MFE 为已走部分")
    if track.get("carrier_changed"):
        notes.append(f"最新载体已变为 {track.get('name')}")
    # 公共口径（载体/t+1/无成本/盘中极值）写在页面表头，不在每行重复；这里只留这一行的例外。
    base["note"] = "；".join(notes) if notes else "—"
    return base


def mae_gap(outcomes: list[dict]) -> dict:
    """收盘口径 vs 盘中口径差多少 —— 让「换了口径」这件事可被核查，而不是只写在文案里。"""
    pairs = [(o["industry"], o["mae"], o["mae_close"]) for o in outcomes
             if o.get("mae") is not None and o.get("mae_close") is not None]
    if not pairs:
        return {"n": 0, "median_delta_bp": None, "worst": None}
    worst = min(pairs, key=lambda x: x[1] - x[2])
    return {
        "n": len(pairs),
        "median_delta_bp": round(float(np.median([m - c for _, m, c in pairs])) * 1e4, 1),
        "worst": {
            "industry": worst[0],
            "mae": round(float(worst[1]), 6),
            "mae_close": round(float(worst[2]), 6),
            "delta_bp": round(float(worst[1] - worst[2]) * 1e4, 1),
        },
    }


# ---------------------------------------------------------------- 回放

def build_replay(d: pd.DataFrame, idx: pd.DataFrame, end: pd.Timestamp,
                 lookback: int, industries: list[str] | None = None) -> dict:
    # 回放最早一天之前仍要有 500 个波动观测，所以 context 比展示窗口多留 ~150 日。
    ctx = etf_watch.build_context(d, idx, sessions=etf_watch.TAIL_SESSIONS + lookback + 40)
    all_dates = [t for t in ctx.sessions if t in ctx.nav.index and t in ctx.regime.index]
    dates = [t for t in all_dates if t <= end][-lookback:]
    if not dates or dates[-1] != end:
        raise SystemExit(f"{end.date()} 不在可用交易日里（最新 {all_dates[-1].date() if all_dates else '无'}）")

    snapshots: dict[str, dict] = {}
    for t in dates:
        payload, _ = etf_watch.scan(t, d, idx, [], ctx=ctx)
        meta = dict(payload["meta"])
        meta.update({
            "mode": "replay",
            "replay": True,
            "rule_version": RULE_VERSION,
            "point_in_time": POINT_IN_TIME,
        })
        snapshots[str(t.date())] = {
            "meta": meta,
            "market": payload["market"],
            "counts": payload["counts"],
            "industries": payload["industries"],
        }

    latest = snapshots[str(end.date())]
    tracked = [x for x in latest["industries"] if x["status"] == "CANDIDATE"]
    if industries:
        wanted = set(industries)
        tracked = [x for x in tracked if x["industry"] in wanted]
    if not tracked:
        tracked = []

    tracks: list[dict] = []
    for item in tracked:
        ind = item["industry"]
        days: list[dict] = []
        for t in dates:
            row = next((x for x in snapshots[str(t.date())]["industries"] if x["industry"] == ind), None)
            if row is None:
                continue
            days.append({
                "date": str(t.date()),
                "status": row["status"],
                "dd": row["dd"],
                "vol_pct": row["vol_pct"],
                "amt20": row["amt20"],
                "best_symbol": row["best_symbol"],
                "best_name": row["best_name"],
                "r1": row["r1"], "r2": row["r2"], "r3": row["r3"], "f6": row["f6"],
                "reasons": row["reasons"],
                "market_state": snapshots[str(t.date())]["market"]["state"],
            })
        tracks.append(build_track(ind, days))

    symbols = {t["symbol"] for t in tracks if t["symbol"]} | {t["carrier_at_first_symbol"] for t in tracks if t["carrier_at_first_symbol"]}
    bars: dict[str, pd.DataFrame] = {}
    if symbols:
        sub = d[d["symbol"].isin(symbols)]
        for sym, grp in sub.groupby("symbol", sort=False):
            f = grp.set_index("date")[["close_adj", "hi_adj", "lo_adj"]].sort_index()
            bars[str(sym)] = f[~f.index.duplicated(keep="last")]

    pos = {t: i for i, t in enumerate(dates)}
    outcomes = [build_outcome(t, dates, pos, bars, end) for t in tracks]
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "end": str(end.date()),
        "start": str(dates[0].date()),
        "sessions": len(dates),
        "lookback": lookback,
        "rule_version": RULE_VERSION,
        "mode": "replay",
        "note": "只回放当时可见数据；收益列是事后评价，不参与判定",
        "dd_convention": latest["meta"]["dd_convention"],
        "vol_convention": latest["meta"]["vol_convention"],
        "thresholds": latest["meta"]["thresholds"],
        "mae_convention": (
            "MAE/MFE 用持有期盘中极值：进场日之后的日线 low/high，按 close_adj/close "
            "折成净值单位；不含进场日盘内（那时还没买）。mae_close/mfe_close 是收盘口径，"
            "只用于审计两者差多少，不用于仓位判断。"
        ),
        "mae_gap": mae_gap(outcomes),
        "point_in_time": POINT_IN_TIME,
    }
    return {
        "meta": meta,
        "sessions": [str(t.date()) for t in dates],
        "snapshots": snapshots,
        "tracks": tracks,
        "outcomes": outcomes,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="历史买点回放与信号追踪（同一 scan 规则）")
    ap.add_argument("--end", help="回放末日（默认最新已完成交易日）")
    ap.add_argument("--lookback", type=int, default=120, help="回放交易日数（默认 120）")
    ap.add_argument("--industries", help="只追踪这些行业（逗号分隔，默认最新候选行业）")
    ap.add_argument("--out", default=str(OUT), help="输出 JSON 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印摘要，不写文件")
    args = ap.parse_args()

    if args.lookback < 10:
        raise SystemExit("--lookback 至少 10 个交易日")
    etf, etf_meta = etf_watch.fresh_etf()
    idx, idx_meta = etf_watch.fresh_index()
    end = etf_watch.resolve_asof(etf, idx, args.end, datetime.now())
    d, _ = core.load(etf_raw=etf)
    industries = [x.strip() for x in args.industries.split(",") if x.strip()] if args.industries else None
    replay = build_replay(d, idx, end, args.lookback, industries)

    meta = replay["meta"]
    print(f"=== 历史买点回放 {meta['start']} → {meta['end']}（{meta['sessions']} 个交易日）===")
    print(f"规则 {meta['rule_version']} · 候选行业 {len(replay['tracks'])}")
    for t in replay["tracks"]:
        print(f"  {t['industry']:6s} 首次 {t['first_trigger'] or '—'}  "
              f"连续 {t['max_consecutive']}  触发 {t['trigger_count']}  "
              f"载体 {t['name']}" + ("  [左端已触发]" if t["left_censored"] else ""))
    print("\n首次触发后（载体 ETF，t+1 收盘进场；事后评价，不计成本；MAE/MFE 盘中极值）")
    for o in replay["outcomes"]:
        def f(v):
            return "—" if v is None else f"{v * 100:+.1f}%"
        print(f"  {o['industry']:6s} {o['first_trigger'] or '—'}  至最新 {f(o['to_end'])}  "
              f"5日 {f(o['ret5'])}  10日 {f(o['ret10'])}  20日 {f(o['ret20'])}  "
              f"MAE {f(o['mae'])}(收盘 {f(o['mae_close'])})  MFE {f(o['mfe'])}")
    gap = meta.get("mae_gap") or {}
    if gap.get("n"):
        w = gap["worst"]
        print(f"\n口径差：MAE 盘中 − 收盘 中位 {gap['median_delta_bp']:+.0f}bp，"
              f"最差 {w['industry']} {w['delta_bp']:+.0f}bp（收盘 {w['mae_close'] * 100:+.1f}% → 盘中 {w['mae'] * 100:+.1f}%）")
    print("\npoint-in-time 限制（必须和结论一起读）：")
    for k, v in meta["point_in_time"].items():
        print(f"  - {k}: {v}")

    if args.dry_run:
        print("\n[dry-run] 未写盘")
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"\n写入 {shown}  ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
