"""买点体检 —— 判据、记录、事后结算。

## 判据从哪来

`data/entry_check/stats.json`，由 `analysis/_diag_entry_criteria.py --write` 产出
（2016-11~2026-09，118 期，46.5 万票日）。本模块**不硬编码任何收益数字**：
文件缺失就返回 `calibrated=False`，页面显示"未校准"，绝不编一个。

实测三档（20 日、等权、绝对收益）：

    低风险 17.6%  胜率 51.0%  中位 +0.3%  暴跌率 (<-10%) 9.7%
    中性   65.4%  胜率 48.1%  中位 -0.4%  暴跌率       16.6%
    高危   17.0%  胜率 40.8%  中位 -3.2%  暴跌率       29.6%
    基准  100%    胜率 47.35% 中位 -0.6%  暴跌率       17.57%

**它不预测涨。** 低风险档胜率 51.0% 只比抛硬币好 3.7pp，中位 +0.3% 约等于零。
它能做的是把暴跌概率从 17.6% 压到 9.7%（高危档推到 29.6%），且分年稳定
（低风险档胜率仅 1/10 年跑输基准，暴跌率 0/10 年变差）。所以页面上的措辞必须是
「风险档位」，不是「买入信号」。

## 为什么要记 journal

历史统计是**样本内**的：门槛、档位定义都在同一段历史上挑过。唯一能证伪它的
办法是**从今天开始记录，20 个交易日后结算**，看实际胜率/暴跌率是否落在历史
区间内。本模块做这件事，且只做这一件事——它不参与打分、不回写判据。

记录幂等：同一 `(symbol, as_of, criteria)` 只写一条。判据版本变了就是另一批
样本，不能与旧记录混在一起统计（`entry-v1` 与 `entry-v2` 的本底概率不同）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services import market_percentiles as mp

logger = logging.getLogger(__name__)

CRITERIA = "entry-v1"
STATS_FILE = "stats.json"
JOURNAL_FILE = "journal.jsonl"
FWD_DAYS = 20
#: 结算节流：页面请求频繁，但结算按天变化，10 分钟一次足够
SETTLE_INTERVAL_S = 600

_lock = threading.Lock()
_settle_at: dict[str, float] = {}


# ================================================================
# 判据（预注册，改阈值即换版本号）
# ================================================================

THRESH = {
    "c_turnover_low": 0.33,
    "c_vol_low": 0.33,
    "c_rev_mid": (0.33, 0.67),
    "c_holder_low": 0.40,
    "r_high": 0.80,
    "low_min_hits": 3,
    "high_min_hits": 2,
}


def judge(state: dict) -> dict:
    """把四个分位落进三档。缺失的项按「未成立」处理，并单独报告缺了哪几项。

    缺失不能当成"通过"：户数只在季报后更新，新股可能没有历史户数；把缺失
    当成立会把一批数据不全的票推入低风险档，而这批票恰好是信息最少的那批。
    """
    to = state.get("turnover_pct")
    vol = state.get("vol_pct")
    rev = state.get("rev_pct")
    hold = state.get("holder_pct")

    checks = [
        {"id": "C1", "label": "低换手", "value": to,
         "hit": to is not None and to <= THRESH["c_turnover_low"],
         "detail": f"换手率分位 {fmt_pct(to)}"},
        {"id": "C2", "label": "低波动", "value": vol,
         "hit": vol is not None and vol <= THRESH["c_vol_low"],
         "detail": f"波动率分位 {fmt_pct(vol)}"},
        {"id": "C3", "label": "涨幅居中", "value": rev,
         "hit": rev is not None and THRESH["c_rev_mid"][0] < rev <= THRESH["c_rev_mid"][1],
         "detail": f"20日涨幅分位 {fmt_pct(rev)}"},
        {"id": "C4", "label": "户数不膨胀", "value": hold,
         "hit": hold is not None and hold <= THRESH["c_holder_low"],
         "detail": f"户数同比分位 {fmt_pct(hold)}"},
    ]
    risks = [
        {"id": "R1", "label": "高换手", "hit": to is not None and to >= THRESH["r_high"],
         "detail": f"换手率分位 {fmt_pct(to)}"},
        {"id": "R2", "label": "高波动", "hit": vol is not None and vol >= THRESH["r_high"],
         "detail": f"波动率分位 {fmt_pct(vol)}"},
        {"id": "R3", "label": "涨幅过大", "hit": rev is not None and rev >= THRESH["r_high"],
         "detail": f"20日涨幅分位 {fmt_pct(rev)}"},
    ]
    bull = sum(1 for c in checks if c["hit"])
    bear = sum(1 for c in risks if c["hit"])
    missing = [c["id"] for c in checks if c["value"] is None]

    if bear >= THRESH["high_min_hits"]:
        tier = "high"
    elif bull >= THRESH["low_min_hits"]:
        tier = "low"
    else:
        tier = "mid"

    return {
        "tier": tier,
        "bull_hits": bull,
        "bear_hits": bear,
        "checks": checks,
        "risks": risks,
        "missing": missing,
        "criteria": CRITERIA,
    }


def fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


# ================================================================
# 统计常数（唯一产地：回测产出的 stats.json）
# ================================================================

def load_stats(data_dir: Path) -> dict | None:
    p = data_dir / "entry_check" / STATS_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("买点判据统计读取失败 %s: %s", p, e)
        return None


# ================================================================
# Journal：记录 + 惰性结算
# ================================================================

def _journal_path(data_dir: Path) -> Path:
    return data_dir / "entry_check" / JOURNAL_FILE


def load_journal(data_dir: Path) -> list[dict]:
    p = _journal_path(data_dir)
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 半行（进程被杀）跳过，不让整份账本作废
    except OSError as e:
        logger.warning("journal 读取失败: %s", e)
    return out


def _append(data_dir: Path, rec: dict) -> None:
    p = _journal_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record(data_dir: Path, symbol: str, as_of: str, verdict: dict, state: dict) -> bool:
    """幂等记录一次判定。返回是否真的写入。"""
    key = (symbol, as_of, CRITERIA)
    with _lock:
        existing = {
            (r.get("symbol"), r.get("as_of"), r.get("criteria"))
            for r in load_journal(data_dir)
        }
        if key in existing:
            return False
    _append(data_dir, {
        "symbol": symbol,
        "as_of": as_of,
        "criteria": CRITERIA,
        "tier": verdict["tier"],
        "bull_hits": verdict["bull_hits"],
        "bear_hits": verdict["bear_hits"],
        "state": {k: state.get(k) for k in
                  ("turnover_pct", "vol_pct", "rev_pct", "holder_pct")},
        "fwd20": None,
        "settled_on": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return True


def _settle_one(repo, rec: dict) -> dict | None:
    """用日 K 结算一条记录。数据不够（未到期 / 长期停牌）返回 None。"""
    symbol = rec["symbol"]
    as_of = date.fromisoformat(rec["as_of"])
    try:
        df = repo.get_daily(symbol, as_of, as_of + timedelta(days=90))
    except Exception as e:  # noqa: BLE001
        logger.debug("结算取数失败 %s: %s", symbol, e)
        return None
    if df.is_empty() or "close" not in df.columns:
        return None
    df = df.filter(pl.col("date") >= as_of).sort("date")
    if df.height < FWD_DAYS + 1:
        return None  # 还没到期
    base_close = float(df["close"][0])
    fwd_close = float(df["close"][FWD_DAYS])
    if base_close <= 0:
        return None
    return {
        "fwd20": round(fwd_close / base_close - 1, 6),
        "settled_on": str(df["date"][FWD_DAYS])[:10],
        "_last_date": str(df["date"][df.height - 1])[:10],
    }


def settle_due(repo, data_dir: Path, force: bool = False) -> int:
    """结算所有到期记录。返回本次结算条数。

    整体重写 journal（原子替换）：逐行改写比"追加一行结算结果"简单，也不会
    出现同一条记录被结算两次。文件很小（每天最多几十条），重写成本可忽略。
    """
    key = str(data_dir)
    now = time.time()
    if not force and now - _settle_at.get(key, 0.0) < SETTLE_INTERVAL_S:
        return 0

    recs = load_journal(data_dir)
    pending = [r for r in recs if r.get("fwd20") is None]
    if not pending:
        _settle_at[key] = now
        return 0

    settled = 0
    for r in pending:
        out = _settle_one(repo, r)
        if out:
            r["fwd20"] = out["fwd20"]
            r["settled_on"] = out["settled_on"]
            settled += 1

    if settled:
        p = _journal_path(data_dir)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in recs) + "\n",
            encoding="utf-8",
        )
        tmp.replace(p)
    _settle_at[key] = now
    return settled


def journal_summary(recs: list[dict], symbol: str | None = None) -> dict:
    """按判据版本分组统计：实际胜率 / 暴跌率 vs 历史期望。"""
    # total 要数**全部**记录（含未结算）：只数已结算的话，刚上线时页面会一直
    # 显示「已记录 0 次」，看起来像没在工作。
    same_all = [r for r in recs if r.get("criteria") == CRITERIA]
    done = [r for r in same_all if r.get("fwd20") is not None]
    tiers: dict[str, dict] = {}
    for t in ("low", "mid", "high"):
        sub = [r for r in done if r.get("tier") == t]
        if not sub:
            tiers[t] = {"n": 0}
            continue
        xs = [r["fwd20"] for r in sub]
        tiers[t] = {
            "n": len(sub),
            "win": round(sum(1 for x in xs if x > 0) / len(xs), 4),
            "crash": round(sum(1 for x in xs if x < -0.10) / len(xs), 4),
            "mean": round(sum(xs) / len(xs), 5),
        }
    mine = [r for r in recs if symbol and r.get("symbol") == symbol]
    mine_done = [r for r in mine if r.get("fwd20") is not None]
    return {
        "criteria": CRITERIA,
        "total": len(same_all),
        "settled": len(done),
        "pending": len(same_all) - len(done),
        "tiers": tiers,
        "symbol_history": [
            {k: r.get(k) for k in ("as_of", "tier", "fwd20", "settled_on")}
            for r in sorted(mine, key=lambda x: x.get("as_of") or "", reverse=True)[:8]
        ],
        "symbol_settled": len(mine_done),
    }


# ================================================================
# 组装
# ================================================================

TIER_LABEL = {"low": "低风险", "mid": "中性", "high": "高危"}


def build(repo, symbol: str, holder_pct: float | None) -> dict:
    """个股当前买点体检。

    `holder_pct` 由调用方（shareholder 服务）给：户数分位属于股东模块的口径，
    这里不重复算，避免同一真相两个定义点。
    """
    data_dir = Path(repo.store.data_dir)
    pct = mp.market_percentiles(repo)
    cur = pct.get(symbol)
    if not cur:
        return {
            "symbol": symbol, "available": False,
            "reason": "本地没有这只票最近的日 K，无法定位它在市场中的分位",
            "calibrated": False,
        }

    state = {
        "turnover_pct": cur.get("turnover_pct"),
        "vol_pct": cur.get("vol_pct"),
        "rev_pct": cur.get("rev_pct"),
        "holder_pct": holder_pct,
        "turnover_abs": cur.get("to_avg"),
        "ann_vol_abs": cur.get("ann_vol"),
        "ret20_abs": cur.get("ret"),
    }
    verdict = judge(state)

    # as_of 用最近一个日K分区日期：周末/节假日打开页面时，判定的"今天"是最后交易日
    as_of = _latest_trade_date(data_dir) or cn_today().isoformat()

    settle_due(repo, data_dir)
    wrote = record(data_dir, symbol, as_of, verdict, state)
    recs = load_journal(data_dir)

    stats = load_stats(data_dir)
    tier_stat = (stats or {}).get("tiers", {}).get(verdict["tier"])

    return {
        "symbol": symbol,
        "available": True,
        "as_of": as_of,
        "calibrated": stats is not None,
        "state": state,
        "verdict": verdict,
        "tier_label": TIER_LABEL.get(verdict["tier"], verdict["tier"]),
        "stats": {
            "base": (stats or {}).get("base"),
            "tier": tier_stat,
            "all_tiers": (stats or {}).get("tiers"),
            "stability": (stats or {}).get("stability"),
            "meta": (stats or {}).get("meta"),
        },
        "journal": {**journal_summary(recs, symbol), "recorded_now": wrote},
    }


def _latest_trade_date(data_dir: Path) -> str | None:
    base = data_dir / "kline_daily_enriched"
    if not base.exists():
        return None
    try:
        dirs = sorted(d.name for d in base.iterdir() if d.is_dir() and d.name.startswith("date="))
    except OSError:
        return None
    return dirs[-1][5:] if dirs else None
