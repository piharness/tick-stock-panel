"""仓位艺术 —— 把「状态择时」写成一条可执行、可结算的仓位规则。

与 `app/backtest/position_policies.py` 的区别（名字接近，对象不同）
--------------------------------------------------------------
那个是**用户回测参数**（收益曲线上的仓位函数，跑完即弃，不产生每日决策）。
这里是**每日一条 stance**：给定 t-1 收盘可算的状态，决定 t 日仓位，并留下
「本应在哪天切、切到几成」的记录。它不参与回测撮合，只回答「今天该不该在场」。

规则（滞回 / Schmitt trigger）
------------------------------
状态：全市场等权指数的趋势三分，全部只用当日及以前的数据。

    down  : ew_idx < ma60 且 ma20 < ma60 且 ret60 < -5%
    up    : ew_idx > ma60 且 ma20 > ma60 且 ret60 > +5%
    flat  : 其余

仓位：**入场看状态，出场看持续期**。

    w(t) = 0  当 state(t-1) == down 且 该段已持续 >= N 日
    w(t) = 1  其余

为什么需要滞回：等权状态段的**中位寿命只有 3 天**（49 段，p75=10，最长 65）。
拿 3 天的信号做每日 0/1 切换，十年要切 98 次、每次 20bp 单边，摩擦吃掉 2.8pct/年，
CAGR 5.31% → 2.50%。滞回（满 N 日才走）把切换压到 19-40 次 —— 但**压不掉亏损**：
N=5/10/15/20 的年化是 2.57 / 2.25 / 3.98 / 4.64%，全部低于持有的 5.31%，
最大回撤还更差。空仓的 278 天日均 +11.1bp，在场日 +2.5bp —— 规则把好日子空掉了。

所以这条规则的结论是**否**：状态标记的是「已经跌过」，不是「还要跌」；等到第 10 天
才离场，正好落在反弹概率最高的位置。本模块保留它是因为它是一个**可结算的对照**，
不是因为它有效。

口径约束（改任一参数都会让证据失效，改前先重跑 `analysis/_diag_state_conditional_audit.py`）
------------------------------------------------------------------------------------
- 收益按**交易日历相邻**（rn 差 1），不用自然日。跨停牌复牌的多日跌幅算成
  单日收益会放大日波动、加深回撤、多判 down。
- 状态滞后 1 日使用：t 日仓位由 t-1 收盘的状态决定，**不看 t 日收盘**。
- 「已持续天数」必须是段内序号 `state_age`（1,2,3...）。用整段总长
  （`len().over(group)`）会变成前视 —— 这一层踩过一次，见 `attach_runs` 的注释。
- 交易成本 20bp 单边，按 |Δw| 计费。
- 不做 |ret| >= 11.5% 的截断：双创 20% 涨跌幅是真实波动，砍掉会把 2020/2021
  的上涨段削平。防复权跳变靠日历相邻，不靠幅度阈值。

已知限制（页面必须显示，不许藏）
--------------------------------
- 独立事件只有 13 段空仓，安慰剂 p≈0.77；8 条候选规则的选择偏差未校正。
  **不是「还不够好」，是这个方向不成立。**
- 等权指数含 100% 生存偏差（本地日线缺 312 只退市股），绝对收益被高估；
  该偏差主要抬高下跌段，故「避开下跌」的账面收益被高估 —— 真实情况更差，不是更好。
"""

from __future__ import annotations

import os
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

# ── 口径常量（与 analysis/_diag_state_conditional_audit.py 一致）──────────────
START = "2016-01-01"
DOWN_CUT = -0.05
UP_CUT = 0.05
MA_FAST = 20
MA_SLOW = 60
RET_WINDOW = 60
COST_ONE_WAY = 0.0020
TAIL_LOOKBACK = 260
TAIL_CUT = -0.20

#: 页面可切换的确认天数。0 = 每日切换（对照组，用来展示摩擦成本）。
CONFIRM_CHOICES: tuple[int, ...] = (0, 3, 5, 10, 15, 20)
CONFIRM_DEFAULT = 10

PLACEBO_ITERS = 200
PLACEBO_SEED = 11

_HALVES = (("2016–2021H1", "2016-01-01", "2021-06-30"), ("2021H2–今", "2021-07-01", "2099-12-31"))


# ── 纯函数：状态 ──────────────────────────────────────────────────────────────
def compute_state_frame(panel: pl.DataFrame) -> pl.DataFrame:
    """`symbol/date/close` → 等权指数与状态序列。

    只保留**交易日历相邻**的一日收益；停牌复牌（rn 差 > 1）整行丢弃。
    """
    if panel.is_empty():
        return pl.DataFrame()
    calendar = pl.DataFrame({"date": sorted(panel["date"].unique().to_list())}).with_row_index("rn")
    d = (
        panel.select(["symbol", "date", "close"])
        .join(calendar, on="date", how="left")
        .sort(["symbol", "date"])
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("_prev"),
            (pl.col("rn") - pl.col("rn").shift(1).over("symbol")).alias("_gap"),
        )
        .with_columns(
            pl.when(pl.col("_gap") == 1)
            .then(pl.col("close") / pl.col("_prev") - 1.0)
            .otherwise(None)
            .alias("ret")
        )
        .drop(["_prev", "_gap"])
    )
    daily = (
        d.group_by("date")
        .agg(pl.col("ret").mean().alias("ew_ret"), pl.col("ret").count().alias("n"))
        .filter(pl.col("ew_ret").is_not_null())
        .sort("date")
        .with_columns((1.0 + pl.col("ew_ret")).cum_prod().alias("ew_idx"))
    )
    return add_state(daily)


def add_state(daily: pl.DataFrame) -> pl.DataFrame:
    """给等权指数打 down/flat/up 标签，并追加段内序号与段长。"""
    if daily.is_empty():
        return daily
    d = daily.with_columns(
        [
            pl.col("ew_idx").rolling_mean(MA_FAST).alias("ma20"),
            pl.col("ew_idx").rolling_mean(MA_SLOW).alias("ma60"),
            pl.col("ew_idx").pct_change(RET_WINDOW).alias("ret60"),
        ]
    ).with_columns(
        pl.when(
            (pl.col("ew_idx") < pl.col("ma60"))
            & (pl.col("ma20") < pl.col("ma60"))
            & (pl.col("ret60") < DOWN_CUT)
        )
        .then(pl.lit("down"))
        .when(
            (pl.col("ew_idx") > pl.col("ma60"))
            & (pl.col("ma20") > pl.col("ma60"))
            & (pl.col("ret60") > UP_CUT)
        )
        .then(pl.lit("up"))
        .otherwise(pl.lit("flat"))
        .alias("state")
    )
    # null 的 ma/ret60（前 60 日热身）落到 flat；否则 is_null 会让判断失效
    return attach_runs(d.with_columns(pl.col("state").fill_null("flat")))


def attach_runs(d: pl.DataFrame) -> pl.DataFrame:
    """给已含 `state` 列的日序追加 `state_age` / `state_prev` / `len_prev`。

    单独拆出来是为了能被单元测试直接喂状态序列 —— 这一层出过一次前视 bug
    （用整段总长当「已持续天数」），必须能不经过价格路径就测到。
    """
    d = d.with_columns(
        (pl.col("state") != pl.col("state").shift(1)).fill_null(True).cum_sum().alias("_grp")
    )
    # 段内**已持续天数**（1,2,3,...），不是整段总长度。
    # 用总长度会变成前视：t 日就知道这段会持续 14 天，于是「满 10 日才走」
    # 退化成「整段都不在场」。页面上「已持续 N 日」也必须用这个值。
    d = d.with_columns(pl.int_range(1, pl.len() + 1).over("_grp").alias("state_age")).with_columns(
        [
            pl.col("state").shift(1).alias("state_prev"),
            pl.col("state_age").shift(1).alias("len_prev"),
        ]
    )
    return d.drop("_grp")


def add_weights(daily: pl.DataFrame, confirm_days: int) -> pl.DataFrame:
    """按滞回规则追加 `w`（t 日仓位，0/1）。

    `confirm_days=0` 表示每日跟随状态切换（无滞回），是对照组。
    """
    if daily.is_empty():
        return daily
    if confirm_days < 0:
        raise ValueError(f"confirm_days 不能为负: {confirm_days}")
    hold = (pl.col("state_prev") != "down") | (pl.col("len_prev").fill_null(0) < confirm_days)
    return daily.with_columns(
        pl.when(hold.fill_null(True)).then(1.0).otherwise(0.0).alias("w")
    )


# ── 纯函数：评价 ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Metrics:
    cagr_pct: float
    mdd_pct: float
    sharpe: float | None
    exposure_pct: float
    switches: int
    final_eq: float


def evaluate(rets: list[float], weights: list[float], cost: float = COST_ONE_WAY) -> Metrics:
    """按日净值评价一条仓位序列。切换成本按 |Δw| × cost 计。"""
    if not rets or len(rets) != len(weights):
        raise ValueError("rets/weights 长度不一致或为空")
    eq, peak, mdd, prev = 1.0, 1.0, 0.0, 0.0
    pnls: list[float] = []
    switches = 0
    for r, w in zip(rets, weights):
        pnl = w * r - abs(w - prev) * cost
        switches += 0 if w == prev else 1
        eq *= 1.0 + pnl
        pnls.append(pnl)
        prev = w
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    n = len(pnls)
    mu = sum(pnls) / n
    var = sum((x - mu) ** 2 for x in pnls) / (n - 1) if n > 1 else 0.0
    sd = var**0.5
    return Metrics(
        cagr_pct=round(((eq ** (244.0 / n)) - 1.0) * 100, 2) if eq > 0 else -100.0,
        mdd_pct=round(mdd * 100, 2),
        sharpe=round(mu / sd * (244**0.5), 2) if sd > 0 else None,
        exposure_pct=round(sum(weights) / n * 100, 1),
        switches=switches,
        final_eq=round(eq, 3),
    )


def flat_blocks(weights: list[float]) -> list[int]:
    """空仓段的长度序列（安慰剂要按同样的段长随机放置）。"""
    out: list[int] = []
    i, n = 0, len(weights)
    while i < n:
        if weights[i] == 0:
            j = i
            while j < n and weights[j] == 0:
                j += 1
            out.append(j - i)
            i = j
        else:
            i += 1
    return out


def placebo_p(
    rets: list[float],
    weights: list[float],
    cost: float = COST_ONE_WAY,
    iters: int = PLACEBO_ITERS,
    seed: int = PLACEBO_SEED,
) -> dict[str, Any]:
    """把空仓段（长度不变）随机挪到别的时间，看实际 CAGR 是否还在尾部。

    检验的是「这 395 天特别差」还是「任何 395 天都差不多」。随机放置也能拿到
    同样的改善，说明规则只是降低暴露，不是择时。
    """
    blocks = flat_blocks(weights)
    total = sum(blocks)
    n = len(rets)
    if total == 0 or n < 200 or not blocks:
        return {"available": False, "reason": "没有空仓段，无需检验"}
    actual = evaluate(rets, weights, cost).cagr_pct
    rnd = random.Random(seed)
    cagrs: list[float] = []
    for _ in range(iters):
        mask = [0.0] * n
        filled, tries, attempts = 0, 0, max(2000, n * 4)
        while filled < total and tries < attempts:
            tries += 1
            ln = rnd.choice(blocks)
            start = rnd.randrange(0, max(1, n - ln))
            if any(mask[start : start + ln]):
                continue
            for k in range(start, start + ln):
                mask[k] = 1.0
            filled += ln
        cagrs.append(evaluate(rets, [1.0 - m for m in mask], cost).cagr_pct)
    cagrs.sort()
    better = sum(1 for c in cagrs if c >= actual)
    return {
        "available": True,
        "blocks": len(blocks),
        "flat_days": total,
        "p_value": round(better / iters, 3),
        "placebo_med_pct": round(cagrs[len(cagrs) // 2], 2),
        "placebo_p90_pct": round(cagrs[int(0.9 * iters)], 2),
    }


def label_stability(daily: pl.DataFrame, panel: pl.DataFrame) -> dict[str, Any]:
    """AC1：换一种指数构造，状态标签翻多少天。

    对照组用**自然日 1..10 天**为间隔 —— 那正是产出这批证据的
    `analysis/_diag_state_conditional.py` 的口径（它把停牌复牌的多日跌幅
    算成单日收益）。本服务改用交易日历相邻，两者都是「合理但不同」的构造，
    差异就是标签的分辨率上限。

    这不是形式主义：3% 的翻转率意味着「今天是什么状态」有 1/30 的概率由
    构造方式决定，而不是由市场决定。
    """
    if panel.is_empty() or daily.is_empty():
        return {"available": False}
    d = (
        panel.select(["symbol", "date", "close"])
        .sort(["symbol", "date"])
        .with_columns(
            [
                pl.col("close").shift(1).over("symbol").alias("_prev"),
                (pl.col("date") - pl.col("date").shift(1).over("symbol"))
                .dt.total_days()
                .alias("_gap"),
            ]
        )
        .with_columns(
            pl.when(pl.col("_gap").is_between(1, 10))
            .then(pl.col("close") / pl.col("_prev") - 1.0)
            .otherwise(None)
            .alias("ret")
        )
        .drop(["_prev", "_gap"])
    )
    alt = (
        d.group_by("date")
        .agg(pl.col("ret").mean().alias("ew_ret"))
        .filter(pl.col("ew_ret").is_not_null())
        .sort("date")
        .with_columns((1.0 + pl.col("ew_ret")).cum_prod().alias("ew_idx"))
    )
    alt = add_state(alt)
    j = daily.select(["date", "state"]).join(
        alt.select(["date", pl.col("state").alias("state_alt")]), on="date", how="inner"
    )
    if j.is_empty():
        return {"available": False}
    diff = int((j["state"] != j["state_alt"]).sum())
    return {
        "available": True,
        "days": j.height,
        "flip_days": diff,
        "flip_pct": round(diff / j.height * 100, 2),
        "threshold_pct": 3.0,
        "pass": diff / j.height * 100 <= 3.0,
    }


def forward_expectation(daily: pl.DataFrame, horizon: int = 20) -> pl.DataFrame:
    """追加 f{horizon}（等权指数自身的前瞻收益），用于 n_eff 与尾部读数。"""
    return daily.with_columns(
        pl.col("ew_idx").shift(-horizon).truediv(pl.col("ew_idx")).sub(1.0).alias(f"f{horizon}")
    )


def state_cells(daily: pl.DataFrame, horizon: int = 20) -> dict[str, Any]:
    """按状态分组的 f20：重叠 n / 非重叠 n / 中位 / 胜率。"""
    d = forward_expectation(daily, horizon).filter(pl.col(f"f{horizon}").is_not_null())
    out: dict[str, Any] = {}
    for st in ("down", "flat", "up"):
        v = d.filter(pl.col("state_prev") == st)[f"f{horizon}"].to_list()
        if len(v) < 10:
            continue
        nov = v[::horizon]
        out[st] = {
            "n_overlap": len(v),
            "n_nonoverlap": len(nov),
            "med_pct": round(_median(v) * 100, 2),
            "pos_pct": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
            "med_nonoverlap_pct": round(_median(nov) * 100, 2),
        }
    return out


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


# ── 装配 ──────────────────────────────────────────────────────────────────────
def build_payload(
    daily: pl.DataFrame,
    confirm_days: int = CONFIRM_DEFAULT,
    cost: float = COST_ONE_WAY,
    *,
    stability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从状态序列装配页面所需的全部读数（纯函数，不碰磁盘）。"""
    d = add_weights(daily, confirm_days)
    d = d.filter(pl.col("ew_ret").is_not_null())
    if d.height < 260:
        return {"available": False, "reason": f"可用交易日不足: {d.height}"}

    rets = d["ew_ret"].to_list()
    ws = d["w"].to_list()
    base_w = [1.0] * len(rets)
    cur = d.tail(1).to_dicts()[0]

    rules = []
    for n in CONFIRM_CHOICES:
        dn = add_weights(daily, n).filter(pl.col("ew_ret").is_not_null())
        m = evaluate(dn["ew_ret"].to_list(), dn["w"].to_list(), cost)
        rules.append(
            {
                "confirm_days": n,
                "cagr_pct": m.cagr_pct,
                "mdd_pct": m.mdd_pct,
                "sharpe": m.sharpe,
                "exposure_pct": m.exposure_pct,
                "switches": m.switches,
                "is_default": n == confirm_days,
            }
        )
    benchmark = evaluate(rets, base_w, cost)
    chosen = evaluate(rets, ws, cost)
    stress = evaluate(rets, ws, cost * 2)

    halves = []
    for label, lo, hi in _HALVES:
        sub = d.filter(
            pl.col("date").is_between(pl.lit(lo).str.to_date(), pl.lit(hi).str.to_date())
        )
        if sub.height < 120:
            continue
        halves.append(
            {
                "label": label,
                "days": sub.height,
                "base_cagr_pct": evaluate(
                    sub["ew_ret"].to_list(), [1.0] * sub.height, cost
                ).cagr_pct,
                "rule_cagr_pct": evaluate(
                    sub["ew_ret"].to_list(), sub["w"].to_list(), cost
                ).cagr_pct,
                "base_mdd_pct": evaluate(
                    sub["ew_ret"].to_list(), [1.0] * sub.height, cost
                ).mdd_pct,
                "rule_mdd_pct": evaluate(sub["ew_ret"].to_list(), sub["w"].to_list(), cost).mdd_pct,
            }
        )

    # 「明天」的仓位由今天的 state/state_age 决定 —— 不能直接复用 w[-1]，
    # 那一位是今天（由 t-1 状态决定）的仓位。两者只差一天，但差的那天正是
    # 这条规则第一次触发的那天，写错会让「今天该空仓」晚一天。
    w_next = [
        0.0 if (str(s) == "down" and int(ln) >= confirm_days) else 1.0
        for s, ln in zip(d["state"].to_list(), d["state_age"].to_list())
    ]
    stance = int(w_next[-1])
    since_idx = len(w_next) - 1
    while since_idx > 0 and int(w_next[since_idx - 1]) == stance:
        since_idx -= 1
    state = str(cur["state"])
    state_age = int(cur["state_age"])
    if state == "down" and state_age < confirm_days:
        trigger = f"下行已持续 {state_age} 日；满 {confirm_days} 日转空仓（还差 {confirm_days - state_age} 日）"
    elif state == "down":
        trigger = "下行段结束（首个非 down 日）即回场"
    elif state == "up":
        trigger = "转 flat 且持续到确认期，或转 down 且满确认期"
    else:
        trigger = "转 down 并持续满确认期则空仓；转 up 则维持在场"

    cells = state_cells(daily)
    year = str(cur["date"])[:4]
    ytd = d.filter(pl.col("date").dt.year() == int(year))
    neff = {
        "year": year,
        "down_overlap": int(ytd.filter(pl.col("state_prev") == "down").height),
        "flat_overlap": int(ytd.filter(pl.col("state_prev") == "flat").height),
    }
    neff["down_nonoverlap"] = neff["down_overlap"] // 20
    neff["flat_nonoverlap"] = neff["flat_overlap"] // 20

    tail = d.tail(500).select(["date", "ew_idx", "state", "w"]).to_dicts()
    series = [
        {
            "date": str(r["date"]),
            "ew_idx": round(float(r["ew_idx"]), 4),
            "state": r["state"],
            "w": float(r["w"]),
        }
        for r in tail
    ]

    # 净值：每 20 个交易日取一点（2643 天 → 133 点），够画形状又不塞爆响应。
    # 规则净值与 evaluate 同一套成本口径（按 |Δw| 计费）。
    eq_base, eq_rule, prev_w = [1.0], [1.0], 0.0
    for r, w in zip(rets, ws):
        eq_base.append(eq_base[-1] * (1.0 + r))
        eq_rule.append(eq_rule[-1] * (1.0 + w * r - abs(w - prev_w) * cost))
        prev_w = w
    dates = [str(x) for x in d["date"].to_list()]
    equity = [
        {"date": dates[i], "base": round(eq_base[i], 4), "rule": round(eq_rule[i], 4)}
        for i in range(0, len(dates), 20)
    ]
    equity.append({"date": dates[-1], "base": round(eq_base[-1], 4), "rule": round(eq_rule[-1], 4)})

    flat_days = [r for r, w in zip(rets, ws) if w == 0]
    in_days = [r for r, w in zip(rets, ws) if w > 0]

    switches = []
    prev_w = int(ws[0])
    for i in range(1, len(ws)):
        w = int(ws[i])
        if w != prev_w:
            switches.append(
                {"date": str(d["date"][i]), "to": w, "state": str(d["state_prev"][i])}
            )
            prev_w = w

    checks = []
    if stability is not None:
        st_ok = bool(stability.get("pass"))
        checks.append(
            {
                "id": "AC1",
                "name": "状态标签稳定",
                "want": "换指数构造后翻转 ≤ 3%",
                "got": (
                    f"{stability.get('flip_days')} / {stability.get('days')} 天"
                    f"（{stability.get('flip_pct')}%）"
                    if stability.get("available")
                    else "未计算"
                ),
                "pass": st_ok,
            }
        )
    checks.append(
        {
            "id": "AC2",
            "name": "独立观测数",
            "want": "进结论的 cell 非重叠 ≥ 30",
            "got": f"{year} 下行 {neff['down_overlap']}→{neff['down_nonoverlap']}、"
            f"横盘 {neff['flat_overlap']}→{neff['flat_nonoverlap']}",
            "pass": min(neff["down_nonoverlap"], neff["flat_nonoverlap"]) >= 30,
        }
    )
    checks.append(
        {
            "id": "AC3",
            "name": "规则胜基准",
            "want": "分半两段 CAGR 均胜、MDD 各改善 ≥5pct",
            "got": "；".join(
                f"{h['label']} {h['base_cagr_pct']:+.1f}→{h['rule_cagr_pct']:+.1f}%"
                f" / MDD {h['base_mdd_pct']:.0f}→{h['rule_mdd_pct']:.0f}%"
                for h in halves
            )
            or "分段不足",
            "pass": bool(halves)
            and all(
                h["rule_cagr_pct"] > h["base_cagr_pct"]
                and h["rule_mdd_pct"] - h["base_mdd_pct"] >= 5
                for h in halves
            ),
        }
    )
    checks.append(
        {
            "id": "AC4",
            "name": "多重检验",
            "want": "≥8 条候选规则做 White RC / 置换检验",
            "got": "未做 —— 页面上的 p 值只校正了「放置位置」，没校正「挑了最好的一条」",
            "pass": False,
        }
    )
    checks.append(
        {
            "id": "AC5",
            "name": "成本压力",
            "want": "40bp 单边仍成立",
            "got": f"CAGR {chosen.cagr_pct:+.2f}% → {stress.cagr_pct:+.2f}%（40bp）",
            "pass": stress.cagr_pct > benchmark.cagr_pct,
        }
    )

    return {
        "available": True,
        "as_of": str(cur["date"]),
        "definition": {
            "index": "全市场等权（横截面日均收益累积）",
            "state": (
                f"down: close<ma{MA_SLOW} 且 ma{MA_FAST}<ma{MA_SLOW} "
                f"且 ret{RET_WINDOW}<{DOWN_CUT:.0%}；"
                f"up: 对称；其余 flat"
            ),
            "rule": f"state(t-1)==down 且该段已持续 ≥{confirm_days} 日 → 空仓，其余持有",
            "confirm_days": confirm_days,
            "cost_one_way_bp": round(cost * 1e4),
            "lag": "状态与持续期均取 t-1，不看 t 日收盘",
        },
        "today": {
            "date": str(cur["date"]),
            "state": state,
            "state_age": state_age,
            "ret60_pct": round(float(cur["ret60"]) * 100, 2) if cur["ret60"] is not None else None,
            "advice": "空仓" if stance == 0 else "在场",
            "stance": stance,
            "since": str(d["date"][since_idx]),
            "days_in_stance": len(w_next) - since_idx,
            "trigger": trigger,
        },
        "chosen": {
            "cagr_pct": chosen.cagr_pct,
            "mdd_pct": chosen.mdd_pct,
            "sharpe": chosen.sharpe,
            "exposure_pct": chosen.exposure_pct,
            "switches": chosen.switches,
        },
        "benchmark": {
            "cagr_pct": benchmark.cagr_pct,
            "mdd_pct": benchmark.mdd_pct,
            "sharpe": benchmark.sharpe,
        },
        "stress": {"cost_bp": 40, "cagr_pct": stress.cagr_pct, "mdd_pct": stress.mdd_pct},
        "rules": rules,
        "halves": halves,
        "placebo": placebo_p(rets, ws, cost),
        "cells": cells,
        "neff": neff,
        "checks": checks,
        "series": series,
        "equity": equity,
        "flat_stats": {
            "flat_days": len(flat_days),
            "flat_mean_bp": round(sum(flat_days) / len(flat_days) * 1e4, 1) if flat_days else None,
            "in_mean_bp": round(sum(in_days) / len(in_days) * 1e4, 1) if in_days else None,
        },
        "recent_switches": switches[-8:],
        "total_switches": len(switches),
        "warning": (
            "验收 1/5 通过。规则的全部价值是否定的：十年年化低于全程持有、回撤更差，"
            "且空仓日的日均收益（+11.1bp）高于在场日（+2.5bp）。"
            "它按日登记只为留下可结算的对照，不作仓位依据。"
        ),
    }


# ── IO + 缓存 ─────────────────────────────────────────────────────────────────
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
#: 装配结果缓存：key 带 confirm_days。build_payload 里安慰剂要跑 200 次
#: 净值循环（约 1.5s）、标签稳定性要全表重算一次指数（约 1s），两个都不该
#: 挂在每次请求上。命中后整个接口 <5ms。
_payload_cache: dict[str, dict[str, Any]] = {}


def _data_dir(data_dir: Path | str) -> Path:
    return Path(data_dir)


def _partition_names(enriched: Path, since: str) -> list[str]:
    try:
        with os.scandir(enriched) as it:
            names = sorted(
                e.name
                for e in it
                if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= since
            )
    except FileNotFoundError as exc:
        raise ValueError(f"日线分区目录不存在: {enriched}") from exc
    return names


def load_panel(data_dir: Path | str, since: str = START) -> pl.DataFrame:
    """读取日线面板（只取 symbol/date/close）。

    只读必要列：全量 2600+ 分区在这里是冷路径，靠 as_of 缓存挡住重复读。
    """
    enriched = _data_dir(data_dir) / "kline_daily_enriched"
    names = _partition_names(enriched, since)
    parts = [enriched / n / "part.parquet" for n in names]
    files = [str(p) for p in parts if p.is_file()]
    if len(files) < 300:
        raise ValueError(f"日线分区不足: {len(files)}")
    return pl.read_parquet(files, columns=["symbol", "date", "close"])


def load_state(data_dir: Path | str, since: str = START) -> tuple[pl.DataFrame, pl.DataFrame]:
    """返回 (状态序列, 原始面板)。同一个 as_of 只读一次盘。"""
    key = f"{_data_dir(data_dir).resolve()}|{since}|{_latest_stamp(_data_dir(data_dir))}"
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    panel = load_panel(data_dir, since)
    daily = compute_state_frame(panel)
    with _cache_lock:
        _cache.clear()  # 只保留最新 as_of 的一份，避免多份面板常驻内存
        _cache[key] = (daily, panel)
    return daily, panel


def _latest_stamp(data_dir: Path) -> str:
    """缓存键里的「数据版本」：最后一个分区的名字。数据更新即失效。"""
    enriched = _data_dir(data_dir) / "kline_daily_enriched"
    try:
        with os.scandir(enriched) as it:
            names = [e.name for e in it if e.name.startswith("date=")]
    except FileNotFoundError:
        return "missing"
    return max(names) if names else "empty"


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _payload_cache.clear()


def policy_payload(data_dir: Path | str, confirm_days: int = CONFIRM_DEFAULT) -> dict[str, Any]:
    """页面主接口：状态 + 今日建议 + 规则对比 + 验收。读不到数据时返回 available=False。"""
    root = _data_dir(data_dir).resolve()
    stamp = _latest_stamp(root)
    key = f"{root}|{stamp}|{confirm_days}"
    with _cache_lock:
        hit = _payload_cache.get(key)
    if hit is not None:
        return hit
    try:
        daily, panel = load_state(data_dir)
        if daily.is_empty():
            raise ValueError("状态序列为空")
        out = build_payload(
            daily, confirm_days, COST_ONE_WAY, stability=label_stability(daily, panel)
        )
    except (OSError, ValueError, ImportError) as exc:  # 数据缺失不该让页面 500
        out = {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    if out.get("available"):
        with _cache_lock:
            _payload_cache.clear()
            _payload_cache[key] = out
    return out


def tail_payload(data_dir: Path | str) -> dict[str, Any]:
    """构成轴：换手 × 市值的 25 格左尾，与状态轴并排比较分辨率。"""
    root = _data_dir(data_dir).resolve()
    key = f"tail|{root}|{_latest_stamp(root)}"
    with _cache_lock:
        hit = _payload_cache.get(key)
    if hit is not None:
        return hit
    out = _tail_payload(data_dir)
    if out.get("available"):
        with _cache_lock:
            _payload_cache[key] = out
    return out


def _tail_payload(data_dir: Path | str) -> dict[str, Any]:
    try:
        daily, _ = load_state(data_dir)
        if daily.is_empty():
            raise ValueError("状态序列为空")
        last_year = int(str(daily["date"][-1])[:4])
        enriched = _data_dir(data_dir) / "kline_daily_enriched"
        names = _partition_names(enriched, START)[-TAIL_LOOKBACK:]
        files = [str(enriched / n / "part.parquet") for n in names]
        files = [f for f in files if Path(f).is_file()]
        if len(files) < 120:
            raise ValueError(f"分区不足: {len(files)}")
        panel = pl.read_parquet(
            files, columns=["symbol", "date", "open", "close", "amount", "turnover_rate"]
        )
        panel = panel.filter(
            (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
        )
        cal = pl.DataFrame({"date": sorted(panel["date"].unique().to_list())}).with_row_index("rn")
        d = (
            panel.join(cal, on="date", how="left")
            .sort(["symbol", "date"])
            .with_columns(
                [
                    (pl.col("amount") / (pl.col("turnover_rate") / 100)).alias("fmcap"),
                    (pl.col("rn").shift(-1).over("symbol") - pl.col("rn")).alias("_de"),
                    (pl.col("rn").shift(-20).over("symbol") - pl.col("rn")).alias("_d20"),
                ]
            )
            .with_columns(
                pl.when((pl.col("_de") == 1) & (pl.col("_d20") == 20))
                .then(
                    pl.col("close").shift(-20).over("symbol")
                    / pl.col("open").shift(-1).over("symbol")
                    - 1.0
                )
                .otherwise(None)
                .alias("f20")
            )
            .with_columns(
                [
                    (
                        (pl.col("turnover_rate").rank("ordinal").over("date") - 1)
                        * 5
                        // pl.len().over("date")
                    ).alias("tq"),
                    (
                        (pl.col("fmcap").rank("ordinal").over("date") - 1)
                        * 5
                        // pl.len().over("date")
                    ).alias("mq"),
                ]
            )
            .drop(["_de", "_d20"])
        )
        st = daily.select(["date", pl.col("state_prev").alias("mkt_state")])
        d = d.join(st, on="date", how="left")
        y = d.filter((pl.col("date").dt.year() == last_year) & pl.col("f20").is_not_null())
        if y.height < 5000:
            raise ValueError(f"{last_year} 年样本不足: {y.height}")

        def grid(keys: list[str]) -> list[dict[str, Any]]:
            g = (
                y.group_by(keys)
                .agg(
                    pl.len().alias("n"),
                    (pl.col("f20") < TAIL_CUT).mean().alias("p_tail"),
                    pl.col("f20").median().alias("med"),
                )
                .sort(keys)
            )
            out: list[dict[str, Any]] = []
            for r in g.to_dicts():
                rec: dict[str, Any] = {}
                for k in keys:
                    rec[k] = int(r[k]) if isinstance(r[k], (int, float)) else str(r[k])
                rec.update(
                    {
                        "n": int(r["n"]),
                        "p_tail_pct": round(r["p_tail"] * 100, 1),
                        "med_pct": round(r["med"] * 100, 2),
                    }
                )
                out.append(rec)
            return out

        cells = grid(["mq", "tq"])
        small = [c for c in cells if c["n"] >= 200]
        best = min(small, key=lambda c: c["p_tail_pct"]) if small else None
        worst = max(small, key=lambda c: c["p_tail_pct"]) if small else None
        by_state = grid(["mkt_state"]) if "mkt_state" in y.columns else []
        return {
            "available": True,
            "year": last_year,
            "cut_pct": round(TAIL_CUT * 100),
            "horizon": 20,
            "cells": cells,
            "by_turnover": grid(["tq"]),
            "by_mcap": grid(["mq"]),
            "by_state": by_state,
            "best": best,
            "worst": worst,
            "ratio": round(worst["p_tail_pct"] / best["p_tail_pct"], 1)
            if best and worst and best["p_tail_pct"] > 0
            else None,
        }
    except (OSError, ValueError, ImportError) as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
