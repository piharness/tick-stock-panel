"""短线雷达排序体检：前向登记 + 同日随机零分布评估。

为什么存在（docs/structural-edge/stock-dip-repair.md §7/§14，prereg-heat-sort.md）：

  线上排序键 `heat`（振幅/换手/市值/当日成交额四项分位等权）在 2013-2026 回测中，
  每天取前 15 只比**同日池内随机取 15 只**差 79bp/20日（z=-15.1，500 次抽样第 0 百分位）。
  也就是说排序不是「不够好」，是在**反向工作**。

  2013-2026 的回测留出已被审计看过，任何再用历史数据选参数的做法都是样本内。
  所以这里只做两件事，都不产生新参数：

  1. `record_day`：每天收盘后把「池子成员 + 现行 15 只 + 候选规则 15 只」追加进台账。
     这是前向登记 —— 唯一干净的样本外从登记日开始。
  2. `rebuild`：对已登记且已有 21 个交易日后续数据的日期，算
        lift  = mean(exc20[picks]) - mean(exc20[pool])
        z     = lift / (pool_sd / sqrt(n) * sqrt((N-n)/(N-1)))     # 同日解析零分布
     按月聚簇汇总，写 `data/shortline/sort_audit.json`。

零分布为什么用解析式而不是抽样：随机 15 只的均值分布由池子自身的均值与
横截面 SD 完全决定（有限总体修正后），不需要 Monte Carlo；与审计脚本的
500 次抽样在 N>60 时一致到小数点后一位。这不是第二套定义，是同一个分布的闭式解。

成本口径：`exc` 一律为「T+1 收盘买 → T+21 收盘卖 − 同日全市场等权同窗」，未扣成本；
`net` = exc − 12bp（双边）。与 `stock-dip-repair.md` §2 完全同口径。

产出物只有一个定义点：本文件。页面/API 只读 JSON，不重算。
"""

from __future__ import annotations

import json
import math
from datetime import date as date_cls
from pathlib import Path

import polars as pl

LEDGER_ROWS = "shortline/sort_ledger.jsonl"
ARTIFACT = "shortline/sort_audit.json"

# ---- 预注册常量（冻结，改动必须先改 prereg 文档再改这里） -----------------

PREREG_ID = "heat-sort-2026-11-24"
PREREG_START = "2026-11-24"          # 前向登记从这一天起算；之前的行只能是 backfill
RULE_CURRENT = "heat-v1"             # 线上现状：四项等权
RULE_SHADOW = "heat-v2-noactivity"   # 候选：(p_amp + (1-p_scale))/2，阈值仍 0.75
COST_RT = 0.0012                     # 双边 12bp
TOP_N = 15
GATE_MIN_MONTHS = 6                  # 评估前至少要有这么多独立月
GATE_LIFT_DELTA_BP = 50.0            # 候选须比现行好至少这么多（月均，按月配对）
GATE_T = 1.65                        # 配对差的月度 t 门槛
GATE_NET_BP = 0.0                    # 红线：候选的「选中−全市场」净超额月均 ≥ 0

PREREG_NOTE = (
    "候选规则已冻结为 heat-v2-noactivity：(p_amp + (1-p_scale))/2，阈值仍 0.75。"
    "依据是机制（去掉「今天最活跃」的两项代理）而不是样本内排名 —— 纯小市值在 2013-18 "
    "同样最好，但因纯规模暴露且前段为零被排除。判据、否决条件与改判规则见 "
    "docs/structural-edge/prereg-heat-sort.md。"
)


# ------------------------------------------------------------------ 路径与 IO

def ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / LEDGER_ROWS


def artifact_path(data_dir: Path) -> Path:
    return Path(data_dir) / ARTIFACT


def read_ledger(data_dir: Path) -> list[dict]:
    f = ledger_path(data_dir)
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_artifact(data_dir: Path) -> dict | None:
    f = artifact_path(data_dir)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ------------------------------------------------------------------ 候选规则

def shadow_score(p_amp: float | None, p_scale: float | None) -> float:
    """冻结的候选排序键：(p_amp + (1-p_scale))/2。缺项按 0 兜底（与 heat 同策略）。"""
    return ((p_amp or 0.0) + (1.0 - (p_scale if p_scale is not None else 1.0))) / 2.0


def _top(pool: list[dict], key, n: int = TOP_N) -> list[str]:
    ranked = sorted(
        (r for r in pool if key(r) is not None),
        key=key, reverse=True,
    )
    return [r["symbol"] for r in ranked[:n]]


def shadow_picks(pool: list[dict]) -> list[str]:
    return _top(pool, lambda r: shadow_score(r.get("p_amp"), r.get("p_scale")))


# ------------------------------------------------------------------ 登记

def record_day(data_dir: Path, as_of: date_cls | None = None,
               source: str = "forward", force: bool = False) -> dict | None:
    """把某一天的池子/现行 15 只/候选 15 只追加进台账。幂等：同日期已存在则跳过。

    预注册守卫：起始日（PREREG_START）之前的数据一律标 backfill —— 即使有人把
    `source="forward"` 传进来。裁决只认 forward，这条守卫让「起始日之前」不可能
    以 forward 身份混进评估口径。
    """
    from app.services import citizenship  # 局部导入，避免模块级环

    data_dir = Path(data_dir)
    payload = citizenship.get_citizenship(data_dir, as_of)
    if payload.get("as_of") is None:
        return None
    day = payload["as_of"]
    if source == "forward" and day < PREREG_START:
        source = "backfill"
    rows = read_ledger(data_dir)
    if not force and any(r.get("date") == day for r in rows):
        return None

    row = {
        "date": day,
        "source": source,
        "rule_current": RULE_CURRENT,
        "rule_shadow": RULE_SHADOW,
        "budget": payload["focus"]["budget"],
        "picks": [p["symbol"] for p in payload["focus"]["picks"]],
        "shadow": shadow_picks(payload["focus"].get("pool") or []),
        "pool": payload["focus"].get("pool") or [],
        "prereg_id": PREREG_ID,
        "generated_at": date_cls.today().isoformat(),
    }
    f = ledger_path(data_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# ------------------------------------------------------------------ 评估

def _fwd20_table(data_dir: Path, days: list[date_cls]) -> dict[str, dict[str, float]]:
    """对每个已登记日，返回 {symbol: exc20}（未扣成本）。

    exc20 = close[t+21]/close[t+1] - 1 - 全市场同日同窗等权均值。
    只在 t+21 存在时才给读数 —— 不到 21 个交易日后续数据的日期不进指标。
    """
    idx = {d: i for i, d in enumerate(days)}
    out: dict[str, dict[str, float]] = {}
    for row in read_ledger(data_dir):
        t = date_cls.fromisoformat(row["date"])
        i = idx.get(t)
        if i is None or i + 21 >= len(days):
            continue
        d1, d21 = days[i + 1], days[i + 21]
        c0 = _closes(data_dir, t)
        c1 = _closes(data_dir, d1)
        c21 = _closes(data_dir, d21)
        exc: dict[str, float] = {}
        vals: list[float] = []
        for sym, base in c1.items():
            end = c21.get(sym)
            if not end or not base or sym not in c0:
                continue
            r = end / base - 1.0
            exc[sym] = r
            vals.append(r)
        if len(vals) < 50:
            continue
        mkt = sum(vals) / len(vals)
        out[row["date"]] = {k: v - mkt for k, v in exc.items()}
    return out


def _closes(data_dir: Path, day: date_cls) -> dict[str, float]:
    f = Path(data_dir) / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    if not f.exists():
        return {}
    df = pl.read_parquet(f, columns=["symbol", "close"]).filter(pl.col("close") > 0)
    return dict(df.iter_rows())


def evaluate(data_dir: Path) -> dict:
    """对台账做零分布评估。缺后续数据的日期自动跳过，并在返回里说明。"""
    from app.services import citizenship

    data_dir = Path(data_dir)
    rows = read_ledger(data_dir)
    days = citizenship._enriched_days(data_dir)
    exc_by_day = _fwd20_table(data_dir, days)

    daily: list[dict] = []
    skipped = 0
    for row in rows:
        exc = exc_by_day.get(row["date"])
        if exc is None:
            skipped += 1
            continue
        pool = [p for p in row.get("pool", []) if p["symbol"] in exc]
        n_pool = len(pool)
        picks = [s for s in row.get("picks", []) if s in exc]
        shadow = [s for s in row.get("shadow", []) if s in exc]
        if n_pool < 30 or len(picks) < 5:
            skipped += 1
            continue
        vals = [exc[p["symbol"]] for p in pool]
        mean_pool = sum(vals) / n_pool
        sd_pool = math.sqrt(sum((v - mean_pool) ** 2 for v in vals) / (n_pool - 1)) if n_pool > 1 else 0.0
        cur = sum(exc[s] for s in picks) / len(picks)
        shd = (sum(exc[s] for s in shadow) / len(shadow)) if shadow else None
        fpc = math.sqrt((n_pool - len(picks)) / (n_pool - 1)) if n_pool > len(picks) else 1.0
        se = sd_pool / math.sqrt(len(picks)) * fpc if sd_pool > 0 else None
        daily.append({
            "date": row["date"],
            "n_pool": n_pool,
            "pool_bp": round(mean_pool * 1e4, 1),
            "cur_bp": round(cur * 1e4, 1),
            "shadow_bp": round(shd * 1e4, 1) if shd is not None else None,
            "cur_lift_bp": round((cur - mean_pool) * 1e4, 1),
            "shadow_lift_bp": round((shd - mean_pool) * 1e4, 1) if shd is not None else None,
            "cur_z": round((cur - mean_pool) / se, 2) if se else None,
            "shadow_z": round((shd - mean_pool) / se, 2) if (se and shd is not None) else None,
            "source": row.get("source", "forward"),
        })

    fwd_daily = [d for d in daily if d.get("source") == "forward"]
    return {
        # 全部（回填对照 + 前向）。回填是样本内，只作对照，不参与裁决。
        "all": _aggregate(daily),
        # 裁决唯一看这里：预注册起始日之后登记的交易日。没有前向行时为 None。
        "forward": _aggregate(fwd_daily) if fwd_daily else None,
        "days_registered": len(rows),
        "fwd_days": len(fwd_daily),
        "backfill_days": len(rows) - len(fwd_daily),
        "days_evaluated": len(daily),
        "days_pending": skipped,
        "prereg_start": PREREG_START,
        "gate_min_months": GATE_MIN_MONTHS,
    }


def _aggregate(daily: list[dict]) -> dict:
    """按月聚簇。fwd20 逐日重叠 20 倍，逐日 t 会虚高 —— 与研究同口径，按月只算 1 个观测。"""
    by_month: dict[str, list[dict]] = {}
    for d in daily:
        by_month.setdefault(d["date"][:7], []).append(d)
    series = []
    cur_l, shd_l = [], []
    for ym in sorted(by_month):
        grp = by_month[ym]
        cur_lift = sum(g["cur_lift_bp"] for g in grp) / len(grp)
        shd_vals = [g["shadow_lift_bp"] for g in grp if g["shadow_lift_bp"] is not None]
        shd_lift = sum(shd_vals) / len(shd_vals) if shd_vals else None
        series.append({
            "month": ym,
            "days": len(grp),
            "pool_bp": round(sum(g["pool_bp"] for g in grp) / len(grp), 1),
            "cur_bp": round(sum(g["cur_bp"] for g in grp) / len(grp), 1),
            "shadow_bp": round(sum(g["shadow_bp"] for g in grp if g["shadow_bp"] is not None)
                               / max(1, len(shd_vals)), 1),
            "cur_lift_bp": round(cur_lift, 1),
            "shadow_lift_bp": round(shd_lift, 1) if shd_lift is not None else None,
            "cur_z": round(sum(g["cur_z"] for g in grp if g["cur_z"] is not None)
                           / max(1, sum(1 for g in grp if g["cur_z"] is not None)), 2),
            "shadow_z": round(sum(g["shadow_z"] for g in grp if g["shadow_z"] is not None)
                              / max(1, sum(1 for g in grp if g["shadow_z"] is not None)), 2)
            if any(g["shadow_z"] is not None for g in grp) else None,
        })
        cur_l.append(cur_lift)
        if shd_lift is not None:
            shd_l.append(shd_lift)

    def _t(vals: list[float]) -> float | None:
        if len(vals) < 3:
            return None
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
        return round(m / (sd / math.sqrt(len(vals))), 2) if sd > 0 else None

    cur_mean = round(sum(cur_l) / len(cur_l), 1) if cur_l else None
    shd_mean = round(sum(shd_l) / len(shd_l), 1) if shd_l else None
    paired = [b - a for a, b in zip(cur_l, shd_l)] if len(shd_l) == len(cur_l) else []
    # 红线口径：候选的「选中−全市场」毛超额月均，扣双边 12bp。
    shd_abs = [s["shadow_bp"] for s in series if s["shadow_bp"] is not None]
    net_shd = round(sum(shd_abs) / len(shd_abs) - COST_RT * 1e4, 1) if shd_abs else None
    return {
        "months": len(series),
        "series": series,
        "cur_lift_bp": cur_mean,
        "cur_t": _t(cur_l),
        "shadow_lift_bp": shd_mean,
        "shadow_t": _t(shd_l),
        "paired_delta_bp": round(shd_mean - cur_mean, 1) if (shd_mean is not None and cur_mean is not None) else None,
        "paired_t": _t(paired),
        "shadow_net_bp": net_shd,
    }


def rebuild(data_dir: Path) -> dict:
    """重算指标并写产出物。幂等，可在登记后随时跑。"""
    data_dir = Path(data_dir)
    ev = evaluate(data_dir)
    art = {
        "generated_at": date_cls.today().isoformat(),
        "prereg_id": PREREG_ID,
        "rule_current": RULE_CURRENT,
        "rule_shadow": RULE_SHADOW,
        "note": PREREG_NOTE,
        "gate": {
            "min_months": GATE_MIN_MONTHS,
            "lift_delta_bp": GATE_LIFT_DELTA_BP,
            "t": GATE_T,
            "net_bp": GATE_NET_BP,
        },
        **ev,
    }
    f = artifact_path(data_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(art, ensure_ascii=False, indent=1), encoding="utf-8")
    return art
