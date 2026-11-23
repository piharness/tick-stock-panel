"""行业 ETF 盘后扫描 —— 「跌破 35% × 高波砸盘」的每日记录器（只记录，不下注）。

为什么要它：`docs/structural-edge/industry-etf-asymmetry.md` §7 把这条线判为「未被证伪」，
唯一能推进的动作是**攒真实事件**，不是再回测。回测的 N 已经用到头了（2 轮周期）。

三条纪律写进代码里：
1. 口径唯一产地 —— 分类、前复权、dd52、行业净值、市场状态全部 import
   `_diag_industry_etf_asym`（跑出 §4/§5 那张表的同一份代码）。本文件不复制任何公式，
   否则「同一个 dd 两个定义」是这套项目里最贵的错。
2. 账本只增不改 —— 事件写 `ledger.jsonl`（type=event），结算另写一行（type=settle），
   读时 join。不做「更新原行」，这样任何一次读数都能追到当时的原样。
3. 不给自己发指令 —— 输出只有读数、判定、理由；没有仓位、没有下单字段。

用法：
    python analysis/etf_watch.py                 # 扫到最新可用交易日，写快照 + 账本
    python analysis/etf_watch.py --asof 2026-11-20
    python analysis/etf_watch.py --dry-run       # 只打印，不落盘
    python analysis/etf_watch.py --verify        # 口径回归：扫描端 nav 必须等于研究端 nav
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _diag_industry_etf_asym as core  # noqa: E402  （口径唯一产地，故意不复制）

ETF_PART = ROOT / "data/kline_etf_daily"
IDX_PART = ROOT / "data/kline_index_enriched"
OUT_DIR = ROOT / "data/etf_watch"
DAILY_DIR = OUT_DIR / "daily"
LEDGER = OUT_DIR / "ledger.jsonl"

# 与 `_diag_industry_etf_asym_bt.py` 同一组阈值（那边是预算，这边是判定，数值必须一致）
LOW, DEEP = -0.35, -0.50     # 低吸区间：跌 35~50%
VOL_HI = 0.66                # 高波砸盘：20 日波动的 500 日分位 > 66%
MIN_TRADE_AMT = 5e7          # F6：日均成交额 5,000 万
COOLDOWN = 60                # 同行业重复事件冷却（交易日）
HOLD = 20                    # V1 的持有期
TAIL_SESSIONS = 620          # 扫描窗口：够 250 日高点 + 500 日波动分位，且比全量快 4 倍
CUTOFF = "15:10"             # 当日这根 K 线在此时间后才算完成（盘后扫描，盘中跑则退回上一日）

TRADEAMT_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]


# ---------------------------------------------------------------- 数据拼装
def _partition_days(root: Path) -> list[tuple[pd.Timestamp, Path]]:
    out: list[tuple[pd.Timestamp, Path]] = []
    if not root.exists():
        return out
    for p in root.iterdir():
        if not p.is_dir() or not p.name.startswith("date="):
            continue
        try:
            out.append((pd.Timestamp(p.name[5:]), p / "part.parquet"))
        except ValueError:
            continue
    return sorted(out)


def fresh_etf() -> tuple[pd.DataFrame, dict]:
    """backfill 全量 + 比它新的日分区。两套来源在同一天上 close/amount/volume 逐只相等（已核）。"""
    base = pd.read_parquet(core.ETF_FILE, columns=TRADEAMT_COLS)
    base["date"] = pd.to_datetime(base["date"])
    last = base["date"].max()
    parts = [(d, p) for d, p in _partition_days(ETF_PART) if d > last and p.exists()]
    frames = [base]
    for _, p in parts:
        x = pd.read_parquet(p)
        frames.append(x[[c for c in TRADEAMT_COLS if c in x.columns]])
    d = pd.concat(frames, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates(["symbol", "date"], keep="last")
    meta = {
        "base": str(core.ETF_FILE.relative_to(ROOT)),
        "base_last": str(last.date()),
        "part_days_added": [str(x.date()) for x, _ in parts],
        "rows": int(len(d)),
        "last": str(d["date"].max().date()),
    }
    return d, meta


def fresh_index() -> tuple[pd.DataFrame, dict]:
    base = pd.read_parquet(core.IDX_FILE, columns=["symbol", "date", "close"])
    base["date"] = pd.to_datetime(base["date"])
    last = base["date"].max()
    parts = [(x, p) for x, p in _partition_days(IDX_PART) if x > last and p.exists()]
    frames = [base]
    for _, p in parts:
        x = pd.read_parquet(p)
        frames.append(x[["symbol", "date", "close"]])
    d = pd.concat(frames, ignore_index=True).drop_duplicates(["symbol", "date"], keep="last")
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["symbol"] == "000300.SH"].copy()
    meta = {
        "base": str(core.IDX_FILE.relative_to(ROOT)),
        "base_last": str(last.date()),
        "part_days_added": [str(x.date()) for x, _ in parts],
        "last": str(d["date"].max().date()),
    }
    return d, meta


def resolve_asof(etf: pd.DataFrame, idx: pd.DataFrame, want: str | None, now: datetime) -> pd.Timestamp:
    """最新**已完成**交易日：当日收盘前跑则退回上一日，避免用半根 K 线做判定。"""
    sessions = pd.DatetimeIndex(sorted(set(idx["date"]) & set(etf["date"])))
    if len(sessions) == 0:
        raise SystemExit("指数与 ETF 没有交集交易日，先补数据")
    if now.strftime("%H:%M") < CUTOFF:
        sessions = sessions[sessions < pd.Timestamp(now.date())]
    if want:
        t = pd.Timestamp(want)
        if t not in sessions:
            raise SystemExit(f"--asof {want} 不是可用交易日（可用最新 {sessions[-1].date()}）")
        return t
    return sessions[-1]


# ---------------------------------------------------------------- 面板与指标
def build_nav(d: pd.DataFrame, sessions: int = TAIL_SESSIONS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (行业净值, 行业-日面板)。

    截尾窗口不改变末日读数（250/500 窗口都在窗口内闭合）；`sessions` 可加宽，
    供历史回放使用：回放最早一天前面仍要有 500 个波动观测，否则 volpct 会退化。
    """
    dates = pd.DatetimeIndex(sorted(d["date"].unique()))
    cut = dates[-min(sessions, len(dates))]
    panel = core.aggregate(d[d["date"] >= cut], require_fwd=False)
    nav = core.industry_nav(panel)
    return nav, panel


def features(nav: pd.DataFrame) -> pd.DataFrame:
    """dd/volpct 与事件研究完全同式（bt.nav_features / event_study）。"""
    hi = nav.rolling(250, min_periods=200).max()
    dd = nav / hi - 1
    volz = nav.pct_change().rolling(20, min_periods=15).std()
    volpct = volz.rolling(500, min_periods=250).rank(pct=True)
    return pd.DataFrame({"dd": dd.stack(), "vol": volz.stack(), "volpct": volpct.stack()}).reset_index()


@dataclass
class ScanContext:
    """一次加载、多日复用的只读面板。

    单日扫描与历史回放的**判定代码必须是同一段**；这里只把与日期无关的
    nav/features/regime/交易日索引预计算一次，避免 60 个日期重复 aggregate 60 次。
    """
    nav: pd.DataFrame
    panel: pd.DataFrame
    feat: pd.DataFrame
    regime: pd.DataFrame
    rets: pd.DataFrame
    sessions: list[pd.Timestamp]
    pos: dict[pd.Timestamp, int]
    ddser: pd.DataFrame


def build_context(d: pd.DataFrame, idx: pd.DataFrame, sessions: int = TAIL_SESSIONS) -> ScanContext:
    nav, panel = build_nav(d, sessions=sessions)
    feat = features(nav).dropna(subset=["dd"])
    regime = core.market_regime(idx_raw=idx).set_index("date")
    rets = panel.pivot(index="date", columns="industry", values="ind_ret").sort_index()
    sess = list(rets.index)
    pos = {t: i for i, t in enumerate(sess)}
    ddser = nav / nav.rolling(250, min_periods=200).max() - 1
    return ScanContext(nav=nav, panel=panel, feat=feat, regime=regime,
                       rets=rets, sessions=sess, pos=pos, ddser=ddser)


def run_starts(flag: pd.DataFrame) -> pd.DataFrame:
    """布尔矩阵的「段首日」：本日为 True 且前一日不是 True。

    必须用 `shift(..., fill_value=False)`，不能用 `fillna(False)`：bool 列 shift 后是 object，
    `~True` 会得到 −2（int），`flag & −2` 于是把「段内每一天」标成 True——
    2026-11-22 就是这样把 86 个段首日算成 486 个的。
    """
    return flag & ~flag.shift(1, fill_value=False)


def industry_etf_snapshot(d: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """asof 当日每行业的可选标的：取 20 日均额最大的一只（F6 用）。"""
    x = d[(d["date"] == asof) & d["industry"].notna() & d["amt20"].notna()].copy()
    if x.empty:
        return pd.DataFrame(columns=["industry", "best_symbol", "best_name", "amt20", "n_etf"])
    x = x.sort_values("amt20", ascending=False)
    best = x.drop_duplicates("industry", keep="first")
    cnt = x.groupby("industry").size().rename("n_etf")
    best = best.merge(cnt, left_on="industry", right_index=True, how="left")
    return best[["industry", "symbol", "name", "amt20", "n_etf"]].rename(
        columns={"symbol": "best_symbol", "name": "best_name"}
    )


# ---------------------------------------------------------------- 账本
def read_ledger() -> tuple[list[dict], list[dict]]:
    """读账本；`type=void` 按出现顺序作废**一条**同 id 的事件，不删原行。

    为什么不是「按 id 永久拉黑」：重跑同一交易日会用同一个 `date|industry` id 写回正确事件，
    永久拉黑会把修正后的那条也一起吞掉（2026-11-22 踩过）。作废是消耗品，一条换一条。
    """
    raw: list[dict] = []
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw.append(json.loads(line))
    pending = Counter(r["id"] for r in raw if r.get("type") == "void" and r.get("id"))
    events: list[dict] = []
    for r in raw:
        if r.get("type") != "event":
            continue
        rid = r.get("id")
        if pending.get(rid, 0) > 0:
            pending[rid] -= 1
            continue
        events.append(r)
    settles = [r for r in raw if r.get("type") == "settle"]
    return events, settles


def append_ledger(records: list[dict]) -> None:
    if not records:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def settle_events(events: list[dict], settles: list[dict], nav: pd.DataFrame) -> list[dict]:
    """到期事件结算：t+1 收盘进场，持有 20 日；MAE/MFE 用持有期内收盘极值。"""
    done = {s["id"] for s in settles}
    base = nav.mean(axis=1)
    idx = nav.index
    out: list[dict] = []
    for e in events:
        if e["id"] in done:
            continue
        t = pd.Timestamp(e["date"])
        if t not in idx or e["industry"] not in nav.columns:
            continue
        i = idx.get_loc(t)
        if i + 1 + HOLD >= len(idx):
            continue
        entry, exit_ = idx[i + 1], idx[i + 1 + HOLD]
        col = nav[e["industry"]]
        if pd.isna(col.loc[entry]) or pd.isna(col.loc[exit_]):
            continue
        win = col.loc[entry:exit_]
        out.append({
            "type": "settle",
            "id": e["id"],
            "settled_at": str(idx[-1].date()),
            "entry": str(entry.date()),
            "exit": str(exit_.date()),
            "ret20": round(float(col.loc[exit_] / col.loc[entry] - 1), 6),
            "rel20": round(float(
                (col.loc[exit_] / col.loc[entry] - 1)
                - (base.loc[exit_] / base.loc[entry] - 1)
            ), 6),
            "mae": round(float(win.min() / col.loc[entry] - 1), 6),
            "mfe": round(float(win.max() / col.loc[entry] - 1), 6),
        })
    return out


def v1_verdict(events: list[dict], settles: list[dict]) -> dict:
    """预注册判据（§7 V1）在真实事件上的读数。

    两个口径必须写在数前面：
    1. **按「事件簇」计数，不按行计数**。同一天 6 个行业一起跌破，是一次行情、一个赌注；
       把它数成 6 次就是把同一轮周期重复计数——研究段 51 个事件只对应 26 个独立月，正是这个坑。
       簇 = 自然月（与 bt 的 t_month 口径一致）。
    2. V1 原文「至少 2 次 p10 > −8%」在 N=3 上无法估分位数，落地为
       **逐笔左尾实现值 MAE > −8%**（持有期内最差收盘浮亏），输出里标注为映射口径。
    """
    smap = {s["id"]: s for s in settles}
    sig = sorted([e for e in events if e.get("signal")], key=lambda x: x["date"])
    joined = [(e, smap[e["id"]]) for e in sig if e["id"] in smap]
    clusters: dict[str, list[dict]] = {}
    for e, s in joined:
        clusters.setdefault(e["date"][:7], []).append(s)
    need = 3
    out = {
        "rule": "3 个事件簇（自然月）内 dd∈[−50,−35] 且 volpct>66%，20 日相对均值 > 0，且 ≥2 簇左尾优于 −8%",
        "counting": "按自然月成簇，不按行业-日计；同月多行业=一次下注",
        "left_tail_mapping": "p10 在 N=3 不可估 ⇒ 用每簇最差 MAE（持有期最差收盘）替代",
        "required": need,
        "signal_events": len(sig),
        "signal_clusters": len({e["date"][:7] for e in sig}),
        "settled": len(joined),
        "settled_clusters": len(clusters),
    }
    if len(clusters) < need:
        out.update({
            "state": "WAITING",
            "progress": f"{len(clusters)}/{need}",
            "mean_rel_bp": round(float(np.mean([s["rel20"] for _, s in joined])) * 1e4, 1) if joined else None,
        })
        return out
    keys = sorted(clusters)[:need]
    rels = [float(np.mean([s["rel20"] for s in clusters[k]])) for k in keys]
    tails = [min(s["mae"] for s in clusters[k]) for k in keys]
    tail_ok = sum(1 for t in tails if t > -0.08)
    fails = sum(1 for r in rels if r < -0.10)
    out.update({
        "progress": f"{len(clusters)}/{need}",
        "clusters_used": keys,
        "cluster_rel_bp": [round(r * 1e4, 1) for r in rels],
        "cluster_mae_pct": [round(t * 100, 2) for t in tails],
        "mean_rel_bp": round(float(np.mean(rels)) * 1e4, 1),
        "left_tail_ok": tail_ok,
    })
    if fails >= 2:
        out["state"] = "RETIRED"
        out["why"] = f"前 3 簇有 {fails} 簇 20 日相对 < −10% ⇒ 无条件退役（V4）"
    elif float(np.mean(rels)) > 0 and tail_ok >= 2:
        out["state"] = "PASS"
    else:
        out["state"] = "FAIL"
        out["why"] = f"均值 {out['mean_rel_bp']}bp / 左尾达标 {tail_ok}/3，未同时满足两条"
    return out


# ---------------------------------------------------------------- 主判定
def scan(asof: pd.Timestamp, d: pd.DataFrame, idx: pd.DataFrame,
         ledger_events: list[dict], ctx: ScanContext | None = None) -> tuple[dict, pd.DataFrame]:
    """`d` 是已由 core.load 算好特征的 ETF 帧（含 industry 列）。

    `ctx` 由 `build_context` 预计算；历史回放逐日复用同一个 context，判定仍走本函数，
    不另写一套简化逻辑。单日调用不传 ctx，行为与旧版一致。
    """
    ctx = ctx or build_context(d, idx)
    nav = ctx.nav
    if asof not in nav.index:
        raise SystemExit(f"{asof.date()} 不在行业净值索引里（窗口内无有效行业日）")

    snap = ctx.feat[ctx.feat["date"] == asof].set_index("industry")
    regime = ctx.regime
    if asof not in regime.index:
        raise SystemExit(f"{asof.date()} 不在指数状态索引里")
    mkt = regime.loc[asof]
    state = str(mkt["state"])

    # 停更检查：某行业最后一根有效收益距 asof 超过 3 个交易日 ⇒ 它的 dd 是旧值，不能当候选。
    # 必须只看 asof 及以前：历史回放若用全序列 last_valid_index，会偷看未来（某行业后来复更，
    # 当时其实已经停更）。这是回放与单日扫描唯一需要主动改的口径点。
    rets, pos = ctx.rets, ctx.pos
    if asof not in pos:
        raise SystemExit(f"{asof.date()} 不在行业收益面板里")
    upto = rets.loc[:asof]
    last_valid = {c: upto[c].last_valid_index() for c in rets.columns}

    cand = industry_etf_snapshot(d, asof)

    rows: list[dict] = []
    for ind in sorted(snap.index):
        r = snap.loc[ind]
        dd, vp = float(r["dd"]), float(r["volpct"]) if not pd.isna(r["volpct"]) else None
        lv = last_valid.get(ind)
        stale = (pos[asof] - pos[lv]) if lv is not None and lv in pos else None
        best = cand[cand["industry"] == ind]
        amt = float(best["amt20"].iloc[0]) if len(best) else 0.0
        hits: list[str] = []
        r1 = (dd <= LOW) and (dd > DEEP)
        r2 = vp is not None and vp > VOL_HI
        r3 = state != "上行·低波"          # F3：牛市里的落后是产业抛弃
        ok_amt = amt >= MIN_TRADE_AMT     # F6
        if dd <= DEEP:
            hits.append("F1 深跌 >50%：不买不加")
        if not r1 and not dd <= DEEP:
            hits.append("不在 −35~−50% 区间")
        if r1 and not r2:
            hits.append(f"F2 低波阴跌（volpct {vp:.0%}）" if vp is not None else "F2 波动分位不可算")
        if not r3:
            hits.append("F3 牛市中买入落后行业")
        if not ok_amt:
            hits.append(f"F6 成交额 {amt/1e4:.0f} 万 < 5,000 万")
        if stale is not None and stale > 3:
            hits.append(f"数据停更 {stale} 日")
        qualified = bool(r1 and r2 and r3 and ok_amt and (stale is None or stale <= 3))
        if qualified:
            status = "CANDIDATE"
        elif dd <= DEEP:
            status = "BLOCKED_F1"
        elif r1:
            status = "BLOCKED"
        elif dd >= -0.10:
            status = "HIGH"
        else:
            status = "NEUTRAL"
        rows.append({
            "industry": ind,
            "dd": round(dd, 4),
            "vol_pct": round(vp, 3) if vp is not None else None,
            "stale_days": stale,
            "n_etf": int(best["n_etf"].iloc[0]) if len(best) else 0,
            "best_symbol": str(best["best_symbol"].iloc[0]) if len(best) else None,
            "best_name": str(best["best_name"].iloc[0]) if len(best) else None,
            "amt20": round(amt, 0),
            "r1": bool(r1), "r2": bool(r2), "r3": bool(r3), "f6": bool(ok_amt),
            "status": status,
            "reasons": hits,
        })

    # 事件：dd 首次跌进区间（区间外 → 区间内），同行业 60 交易日冷却
    ddser = ctx.ddser
    flag = (ddser <= LOW) & (ddser > DEEP)
    enter = run_starts(flag)
    prev_events: dict[str, pd.Timestamp] = {}
    for e in ledger_events:
        t = pd.Timestamp(e["date"])
        if t < asof and (e["industry"] not in prev_events or t > prev_events[e["industry"]]):
            prev_events[e["industry"]] = t

    new_events: list[dict] = []
    seen_ids = {e["id"] for e in ledger_events}
    for ind in flag.columns:
        if asof not in enter.index or not bool(enter.at[asof, ind]):
            continue
        if f"{asof.date()}|{ind}" in seen_ids:
            continue
        last = prev_events.get(ind)
        if last is not None and pos[asof] - pos.get(last, pos[asof]) < COOLDOWN:
            continue
        rec = next(x for x in rows if x["industry"] == ind)
        new_events.append({
            "type": "event",
            "id": f"{asof.date()}|{ind}",
            "date": str(asof.date()),
            "industry": ind,
            "dd": rec["dd"],
            "vol_pct": rec["vol_pct"],
            "state": state,
            "signal": bool(rec["r1"] and rec["r2"]),
            "qualified": bool(rec["status"] == "CANDIDATE"),
            "r3": rec["r3"], "f6": rec["f6"],
            "best_symbol": rec["best_symbol"],
            "best_name": rec["best_name"],
            "amt20": rec["amt20"],
            "reasons": rec["reasons"],
            "source": "live",
        })

    band = [x for x in rows if x["r1"]]
    payload = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asof": str(asof.date()),
            "mode": "record-only",
            "note": "只记录不下注：本文件不含任何仓位或委托字段",
            "dd_convention": "行业净值（同行业 ETF 按成交额加权收益）距 250 日高点",
            "vol_convention": "20 日已实现波动的 500 日分位",
            "thresholds": {"low": LOW, "deep": DEEP, "vol_hi": VOL_HI,
                           "min_amt20": MIN_TRADE_AMT, "cooldown": COOLDOWN, "hold": HOLD},
            "window_sessions": TAIL_SESSIONS,
        },
        "market": {
            "state": state,
            "hs300_close": round(float(mkt["idx"]), 2),
            "ma60": round(float(mkt["ma60"]), 2),
            "vol_pct": round(float(mkt["vol_pct"]), 3) if not pd.isna(mkt["vol_pct"]) else None,
            "trend_up": bool(mkt["trend_up"]),
            "f3_active": state == "上行·低波",
        },
        "counts": {
            "industries": len(rows),
            "in_band": len(band),
            "signal": sum(1 for x in band if x["r2"]),
            "candidate": sum(1 for x in rows if x["status"] == "CANDIDATE"),
            "deep": sum(1 for x in rows if x["dd"] <= DEEP),
            # 同一天触发的多个行业是一笔赌注（同一波行情），V1 按「事件簇」计数，不按行
            "clusters_today": 1 if any(x["r2"] for x in band) else 0,
            "note": "当日多个行业同时触发 = 1 个事件簇；V1 数簇不数行业",
        },
        "industries": sorted(rows, key=lambda x: x["dd"]),
        "new_events": new_events,
    }
    return payload, nav


# ---------------------------------------------------------------- 入口
def load_inst() -> pd.DataFrame:
    return pd.read_parquet(core.INST_FILE)[["symbol", "name"]]


def main() -> None:
    ap = argparse.ArgumentParser(description="行业 ETF 盘后扫描（只记录，不下注）")
    ap.add_argument("--asof", help="指定交易日（默认最新已完成交易日）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--verify", action="store_true", help="口径回归：扫描端 nav 与研究端 nav 对齐")
    args = ap.parse_args()

    etf, etf_meta = fresh_etf()
    idx, idx_meta = fresh_index()
    asof = resolve_asof(etf, idx, args.asof, datetime.now())

    if args.verify:
        run_verify(etf, idx, asof)
        return

    d, _ = core.load(etf_raw=etf)
    events, settles = read_ledger()
    payload, nav = scan(asof, d, idx, events)
    new_settles = settle_events(events + payload["new_events"], settles, nav)

    all_events = events + payload["new_events"]
    all_settles = settles + new_settles
    v1 = v1_verdict(all_events, all_settles)
    payload["ledger"] = {
        "events": sorted(all_events, key=lambda x: (x["date"], x["industry"])),
        "settles": all_settles,
        "v1": v1,
        "low_vol_band_entries": sum(1 for e in all_events if not e["signal"]),
    }
    idx_sessions = pd.DatetimeIndex(sorted(set(idx["date"])))
    payload["data"] = {
        "etf": etf_meta,
        "index": idx_meta,
        "lag_sessions": int((idx_sessions > asof).sum()),
    }
    if payload["data"]["lag_sessions"] > 0:
        payload.setdefault("warnings", []).append(
            f"ETF 数据只到 {etf_meta['last']}，指数已到 {idx_meta['last']}："
            f"缺 {payload['data']['lag_sessions']} 个交易日，先补数据再判定"
        )
    if etf_meta["last"] < idx_meta["last"]:
        payload.setdefault("warnings", []).append(
            f"ETF 帧落后指数 {etf_meta['last']} < {idx_meta['last']}（当日增量未落盘时正常）"
        )

    m = payload["meta"]
    print(f"=== 行业 ETF 扫描 {m['asof']}（{m['mode']}）===")
    print(f"市场状态 {payload['market']['state']}  沪深300 {payload['market']['hs300_close']}  "
          f"vol分位 {payload['market']['vol_pct']}")
    print(f"行业 {payload['counts']['industries']}  区间内 {payload['counts']['in_band']}  "
          f"高波信号 {payload['counts']['signal']}  可下注条件全过 {payload['counts']['candidate']}  "
          f"深跌 {payload['counts']['deep']}")
    cols = ["industry", "dd", "vol_pct", "status", "amt20", "best_name", "stale_days"]
    tab = pd.DataFrame(payload["industries"])[cols].sort_values("dd")
    tab["dd"] = (tab["dd"] * 100).round(1)
    tab["amt20"] = (tab["amt20"] / 1e4).round(0)
    print(tab.to_string(index=False))
    for e in payload["new_events"]:
        print(f"\n[新事件] {e['date']} {e['industry']} dd {e['dd']:.1%} "
              f"volpct {e['vol_pct']} signal={e['signal']} qualified={e['qualified']}")
        for r in e["reasons"]:
            print(f"   - {r}")
    print(f"\n账本：事件 {len(all_events)} / 已结算 {len(all_settles)} / "
          f"V1 {v1['state']} {v1.get('progress', '')}")
    if new_settles:
        print(f"本次结算 {len(new_settles)} 笔")

    if args.dry_run:
        print("\n[dry-run] 未写盘")
        return

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    day_file = DAILY_DIR / f"{m['asof']}.json"
    if day_file.exists():
        print(f"\n[skip] {day_file.name} 已存在（快照不可改写；要重算请先移走它）")
    else:
        day_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    append_ledger(payload["new_events"] + new_settles)
    print(f"\n写入 {day_file.relative_to(ROOT)} + latest.json，账本追加 "
          f"{len(payload['new_events'])} 事件 / {len(new_settles)} 结算")


def run_verify(etf: pd.DataFrame, idx: pd.DataFrame, asof: pd.Timestamp) -> None:
    """口径回归：本文件算出的行业净值必须与研究端 `_etf_asym_tmp/nav.parquet` 逐点相等。

    这是「同一真相只有一个定义点」的机械保证：扫描端换了取数路径（backfill+增量、截尾窗口），
    判定用的 dd/vol 仍然必须落在研究端那张表的同一坐标上。
    """
    ref_file = ROOT / "analysis/_etf_asym_tmp/nav.parquet"
    if not ref_file.exists():
        raise SystemExit(f"缺少参考净值 {ref_file}，先跑 analysis/_diag_industry_etf_asym.py")
    ref = pd.read_parquet(ref_file)
    ref.index = pd.to_datetime(ref.index)

    # 1) 全量、不截尾：应与研究端 nav 完全一致
    d_full, _ = core.load(etf_raw=etf)
    nav_full = core.industry_nav(core.aggregate(d_full, require_fwd=False))
    common_d = nav_full.index.intersection(ref.index)
    common_c = nav_full.columns.intersection(ref.columns)
    diff = (nav_full.loc[common_d, common_c] - ref.loc[common_d, common_c]).abs().max().max()

    # 2) 截尾窗口（扫描实际用法）：只要求 asof 当日读数与全量一致
    nav_tail, _ = build_nav(d_full)
    f_full = features(nav_full).query("date == @asof").set_index("industry")
    f_tail = features(nav_tail).query("date == @asof").set_index("industry")
    j = f_full.join(f_tail, lsuffix="_full", rsuffix="_tail", how="inner")
    d_dd = (j["dd_full"] - j["dd_tail"]).abs().max()
    d_vp = (j["volpct_full"] - j["volpct_tail"]).abs().max()

    print(f"全量 nav 对齐：{len(common_d)} 日 × {len(common_c)} 行业，最大差 {diff:.2e}")
    print(f"{asof.date()} 截尾 vs 全量：dd 最大差 {d_dd:.2e}，volpct 最大差 {d_vp:.2e}")
    ok = diff < 1e-9 and d_dd < 1e-9 and (np.isnan(d_vp) or d_vp < 1e-9)
    print("口径回归:", "PASS" if ok else "FAIL")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
