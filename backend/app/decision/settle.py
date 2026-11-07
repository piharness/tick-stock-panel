"""结算与校准 — 让每张决策卡在到期后变成一个可计分的样本。

## 三个问题，一条链

1. **它成交了吗？** 限价单按真实 bar 撮合：开盘 ≤ 限价按开盘，盘中触及按限价，
   否则不成交。不成交不是"没发生"——它单独计数，因为"看得见却买不到"本身就是
   策略的一部分（涨停板追不进、流动性不足）。
2. **它对了吗？** 收益用复权价算（除权日不产生假跌幅），超额对基准同区间。
3. **它比常数预测强吗？** Brier skill = `1 - brier / brier_const`。常数基线取
   **同一样本的命中率**——这是该样本上能做到的最强常数，跑不赢它就没有信息。

样本不足（`n < MIN_N`）时结论固定为 `INSUFFICIENT`，不给"看起来不错"的数字。

## 两种计分口径（`basis`）

| basis | outcome | 常数基线 | 有效样本 |
|---|---|---|---|
| `abs`（默认） | `ret > 0` | 同样本命中率（需估计） | ≈ 决策日数 |
| `median` | `ret > 同日全市场中位数` | **精确 0.5** | ≈ pick 数 |

`abs` 口径下，同一天所有股票同涨同跌，横截面观测高度相关：实测 345 个起点
的日间 sd = 21.4%，p0 的 95%CI 约 ±12%（用 12 个起点）到 ±2.3%（用 223 个）。
把 3 万个观测当独立样本会把精度高估 30 倍以上。

`median` 口径把市场 beta 剥离，零假设是精确的 50%，且实测组内相关 ICC ≈ 0
（同日 20 个 pick 的日胜率 sd 11.1% ≈ 独立假设的 11.2%）。代价是它衡量的是
**选股能力**而非绝对收益：一个策略可以跑赢中位数却仍然亏钱。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.decision import bars
from app.decision.ledger import (
    SETTLEMENT_KIND,
    append_settlements,
    load_cards,
    load_settlements,
    settlement_key,
)

MIN_N = 8              # 少于 8 个已结算样本，任何统计量都不足以支撑结论
Z90 = 1.645            # 单侧 95%
_EPS = 1e-6            # log-loss 边界，避免 log(0)

#: 「不可判定」的有效样本门槛。**它只喂标签，不喂行为**：不隐藏数字、不放行
#: 也不拦截任何请求。这是它和 §9.12 那个 `n_eff < 30 ⇒ 只显示样本不足` 的
#: 唯一区别，也是它能留下来的原因（§9.14）。
#:
#: 30 的来历：`n_eff / 10` 是可辨识参数个数的量级（§9.12），3 个参数起步。
#: 它是经验值，不是推导值——换掉它只会改变标签，不会改变任何数字。
MIN_NEFF = 30

#: 结论三态。词汇与 `gates.py` 对齐：UNKNOWN 既不是 PASS，也不是 FAIL。
SIGNIFICANCE_PASS = "PASS"
SIGNIFICANCE_FAIL = "FAIL"
SIGNIFICANCE_UNKNOWN = "UNKNOWN"


def significance_of(skill_lcb: float | None, n_eff: int | None,
                    n_eff_reliable: bool | None) -> str:
    """把「下界为正」这个**读数**折成三态**结论**。

    为什么不能只有一个布尔：`False` 同时表示「测过了，下界不为正」和
    「根本测不了」。这两件事的后续动作相反——前者该改策略，后者该继续攒样本。
    合成一个字段时，读的人无法区分，而界面上的「否」比「未知」更容易被当成定论。

    为什么 `n_eff` 门槛在这里不构成 §9.12 那个错误：它只决定这个字段的取值，
    不决定任何数字的存亡，也不参与建卡放行。判据的作用域是标签，不是行为。
    """
    if skill_lcb is None or n_eff is None:
        return SIGNIFICANCE_UNKNOWN
    if not n_eff_reliable or n_eff < MIN_NEFF:
        return SIGNIFICANCE_UNKNOWN
    return SIGNIFICANCE_PASS if skill_lcb > 0 else SIGNIFICANCE_FAIL

#: 先验强度：等价于 20 个虚拟样本。`pwin.DEFAULT_KAPPA` 与它同源——
#: 展开基线与 p_win 必须用同一个收缩强度，否则两个数字不可比。
DEFAULT_KAPPA = 20.0

_PRIOR_CACHE: dict[tuple, dict[str, Any]] = {}
_PRIOR_LOCK = threading.Lock()

#: `(data_dir, entry, exit)` → 当日全市场收益中位数。结算逐 pick 调用，
#: 同一 (entry, exit) 会被反复请求，必须缓存，否则每天要重复扫两次全市场分区。
_MEDIAN_CACHE: dict[tuple[str, str, str], float | None] = {}
_MEDIAN_LOCK = threading.Lock()

BASIS_ABS = "abs"
BASIS_MEDIAN = "median"
BASIS_CHOICES = (BASIS_ABS, BASIS_MEDIAN)

#: `median` 口径的常数基线是精确的 0.5（中位数定义使然），不是估计出来的。
P_CONST_MEDIAN = 0.5


# ---------------------------------------------------------------------------
# 撮合
# ---------------------------------------------------------------------------


def _fill(row: dict, limit: float) -> dict[str, Any]:
    """单日限价撮合（与 `strategy/paper.py` 的开盘/限价口径一致）。"""
    open_raw = bars.raw_open(row)
    low_raw = bars.raw_low(row)
    if open_raw is None:
        return {"status": "INVALID", "detail": "缺少可还原的不复权开盘价"}
    if open_raw <= limit + 1e-9:
        return {"status": "FILLED", "basis": "OPEN", "price": open_raw}
    if low_raw is not None and low_raw <= limit + 1e-9:
        return {"status": "FILLED", "basis": "LIMIT", "price": limit}
    return {"status": "NOT_FILLED",
            "detail": f"开盘 {open_raw:.2f}、最低 {low_raw if low_raw is None else round(low_raw, 2)}"
                      f" 均高于限价 {limit:.2f}"}


def _benchmark_ret(data_dir: Path, symbol: str, entry: str, exit_: str) -> float | None:
    rows = bars.bar_map(data_dir, entry, [symbol], table=bars.INDEX_ENRICHED)
    exit_rows = bars.bar_map(data_dir, exit_, [symbol], table=bars.INDEX_ENRICHED)
    if not rows or not exit_rows:
        return None
    try:
        a = float(rows[symbol]["close"])
        b = float(exit_rows[symbol]["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return (b / a - 1.0) if a > 0 else None


def _median_ret(data_dir: Path, entry: str, exit_: str) -> float | None:
    """同区间全市场收益中位数——`median` 口径的基准。

    它是**可复算的**：收盘后拿当天全市场分区就能算出来，没有前视偏差。
    样本不足 100 只时返回 None（指数成分变动、分区缺失），调用方判未知。
    """
    key = (str(Path(data_dir).resolve()), entry, exit_)
    with _MEDIAN_LOCK:
        if key in _MEDIAN_CACHE:
            return _MEDIAN_CACHE[key]
    val: float | None = None
    a = bars.day_rows(Path(data_dir), entry)
    b = bars.day_rows(Path(data_dir), exit_)
    if not a.is_empty() and not b.is_empty():
        j = a.select(["symbol", "close"]).join(
            b.select(["symbol", "close"]), on="symbol", suffix="_end")
        j = j.filter((pl.col("close") > 0) & (pl.col("close_end") > 0))
        if j.height >= 100:
            val = float((j["close_end"] / j["close"] - 1.0).median())
    with _MEDIAN_LOCK:
        _MEDIAN_CACHE[key] = val
    return val


def _settle_pick(data_dir: Path, card: dict[str, Any], pick: dict[str, Any],
                 calendar: list[str]) -> dict[str, Any] | None:
    """返回结算记录；`None` 表示尚未到期（等下一个交易日，不算失败）。"""
    as_of = str(card["as_of"])
    symbol = str(pick["symbol"])
    horizon = int(card["horizon_sessions"])
    limit = float(pick["limit_price"])

    future = [i for i, day in enumerate(calendar) if day > as_of]
    if not future:
        return None
    entry_i = future[0]
    entry = calendar[entry_i]

    row = bars.bar_map(data_dir, entry, [symbol]).get(symbol)
    if row is None:
        return None                      # 入场日无行情：停牌或数据未到，等
    fill = _fill(row, limit)
    meta = (card.get("forecast") or {}).get("pwin")
    meta = meta if isinstance(meta, dict) else {}
    base = {
        "kind": SETTLEMENT_KIND,
        "key": settlement_key(str(card["card_id"]), symbol),
        "card_id": card["card_id"],
        "as_of": as_of,
        "symbol": symbol,
        "strategy_id": card.get("strategy_id"),
        "account_id": card.get("account_id"),
        "horizon_sessions": horizon,
        "p_win": float(pick["p_win"]),
        "limit_price": limit,
        # p_win 的出处必须随结算一起落盘。否则事后无法区分「账本算的」
        # 「先验常数」「人手填的」——而这三者的 Brier 不能混在一个分数里解释。
        "p_win_source": meta.get("source"),
        "p_win_n": meta.get("n"),
        "p_win_verdict": meta.get("verdict"),
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if fill["status"] != "FILLED":
        return {**base, "fill": {**fill, "date": entry}, "exit": None,
                "outcome": None, "brier": None, "logloss": None,
                "excess": None, "benchmark_ret": None,
                "median_ret": None, "excess_med": None, "outcome_med": None}

    exit_i = entry_i + horizon
    if exit_i >= len(calendar):
        return None
    exit_day = calendar[exit_i]
    exit_row = bars.bar_map(data_dir, exit_day, [symbol]).get(symbol)
    if exit_row is None:
        return None                      # 到期日停牌：等它复牌，而不是拿旧价假装结算

    f_entry = bars.adj_factor(row)
    fill_price = float(fill["price"])
    exit_close = exit_row.get("close")
    exit_raw = exit_row.get("raw_close")
    try:
        exit_close_f = float(exit_close)
    except (TypeError, ValueError):
        exit_close_f = None

    if f_entry is not None and exit_close_f is not None:
        entry_adj = fill_price * f_entry
        ret = exit_close_f / entry_adj - 1.0
        price_basis = "ADJUSTED"
    else:
        try:
            ret = float(exit_raw) / fill_price - 1.0
        except (TypeError, ValueError):
            return {**base, "fill": {**fill, "date": entry}, "exit": None,
                    "outcome": None, "brier": None, "logloss": None,
                    "excess": None, "benchmark_ret": None,
                    "median_ret": None, "excess_med": None, "outcome_med": None,
                    "invalid": "到期日缺少可用价格"}
        price_basis = "RAW"

    benchmark_ret = _benchmark_ret(data_dir, str(card.get("benchmark") or "000300.SH"),
                                   entry, exit_day)
    median_ret = _median_ret(data_dir, entry, exit_day)
    p = float(pick["p_win"])
    outcome = 1 if ret > 0 else 0
    brier = (p - outcome) ** 2
    logloss = -(outcome * math.log(min(max(p, _EPS), 1 - _EPS))
                + (1 - outcome) * math.log(min(max(1 - p, _EPS), 1 - _EPS)))
    # `outcome_med` 是 median 口径的因变量：只问"跑赢当天全市场中位数没有"，
    # 不问"涨没涨"。两个口径同时落盘，事后可以并列比较，不需要重跑结算。
    outcome_med = None if median_ret is None else (1 if ret > median_ret else 0)
    return {
        **base,
        "fill": {**fill, "date": entry},
        "exit": {"date": exit_day, "close": round(exit_close_f if exit_close_f is not None
                                                  else float(exit_raw), 4),
                 "ret": round(ret, 6)},
        "price_basis": price_basis,
        "benchmark_ret": None if benchmark_ret is None else round(benchmark_ret, 6),
        "excess": None if benchmark_ret is None else round(ret - benchmark_ret, 6),
        "median_ret": None if median_ret is None else round(median_ret, 6),
        "excess_med": None if median_ret is None else round(ret - median_ret, 6),
        "outcome_med": outcome_med,
        "outcome": outcome,
        "brier": round(brier, 6),
        "logloss": round(logloss, 6),
    }


#: 结算的读-算-写必须整段串行化。`settle()` 的幂等性依赖「先读 done 集合，
#: 再决定写什么」——三步之间若被并发插入，两个调用都会读到空 done 并各自
#: append 同样的 key：账本出现重复结算行，`score()` 把 1 个真实样本计成
#: 多个；Windows 上还会因并发打开同一账本文件抛 PermissionError。
#: `_PRIOR_LOCK` / `_MEDIAN_LOCK` 只护各自的读缓存，护不住这一段。
#:
#: 已知边界：这是**进程内**锁。多进程部署（多个 uvicorn worker）各自持有
#: 独立锁实例，跨进程仍会竞争——那个边界需要文件锁，见 docs/decision-ledger.md
#: 的已知限制清单，当前单进程部署下不构成风险。
_SETTLE_LOCK = threading.Lock()


def settle(data_dir: Path, *, as_of: str | None = None) -> dict[str, Any]:
    """把所有到期且未结算的 (卡, 标的) 结算进 settlements.jsonl。幂等。

    幂等的前提是持锁：`_SETTLE_LOCK` 串行化「读 done → 算记录 → append
    写盘」整段序列（见其注释）。持锁跨过了行情读取，结算批处理因此不
    并行——这是有意的取舍：串行时第二个调用在锁内重读 done 后直接空转
    返回，比两个调用各自完整重算一遍再写重复行便宜得多，也正确得多。
    """
    with _SETTLE_LOCK:
        return _settle_locked(data_dir, as_of=as_of)


def _settle_locked(data_dir: Path, *, as_of: str | None = None) -> dict[str, Any]:
    cards = load_cards(Path(data_dir))
    calendar = bars.sessions(Path(data_dir))
    done = {str(r.get("key")) for r in load_settlements(Path(data_dir))}
    appended: list[dict[str, Any]] = []
    pending = 0
    considered = 0
    for card in cards:
        if as_of is not None and str(card.get("as_of")) > str(as_of):
            continue
        for pick in card.get("picks") or []:
            key = settlement_key(str(card["card_id"]), str(pick["symbol"]))
            if key in done:
                continue
            considered += 1
            record = _settle_pick(Path(data_dir), card, pick, calendar)
            if record is None:
                pending += 1
                continue
            appended.append(record)
            done.add(key)
    written = append_settlements(Path(data_dir), appended)
    return {
        "cards": len(cards),
        "considered": considered,
        "written": written,
        "pending": pending,
        "not_filled": sum(1 for r in appended if r["fill"]["status"] == "NOT_FILLED"),
        # median 口径需要当天全市场分区；缺分区时 outcome_med 为 None，
        # 该样本在 median 计分里被排除。这里如实报告覆盖率，而不是让它静默缩水。
        "median_covered": sum(1 for r in appended if r.get("outcome_med") is not None),
    }


# ---------------------------------------------------------------------------
# 计分
# ---------------------------------------------------------------------------


def _lcb(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return mean - Z90 * math.sqrt(var / len(values))


def _expanding_climatology(settled: list[dict[str, Any]], outcomes: list[int],
                           prior: float = P_CONST_MEDIAN,
                           kappa: float = DEFAULT_KAPPA) -> list[float]:
    """逐样本的「决策时点可知的最优常数」基线。

    第 d 个决策日的基线只用**严格早于 d** 的已结算样本估（同一天的样本共享
    同一个基线，否则同一天内的 pick 会互相看见）。这是气象预报验证里
    climatology 的标准取法：气候学来自训练期，不是验证期。

    为什么不能用样本内命中率：对任意常数 p，`mean((p-y)²) ≥ mean((ȳ-y)²)`，
    等号只在 `p = ȳ` 时成立。所以拿 ȳ 做分母，任何常数预测器的 BSS 恒 ≤ 0——
    这不是「没有信息」，这是恒等式。用它当基线等于把检验做成了同义反复。
    """
    by_day: dict[str, list[int]] = {}
    for i, row in enumerate(settled):
        by_day.setdefault(str(row.get("as_of")), []).append(i)
    base = [prior] * len(settled)
    hits = 0
    n = 0
    for day in sorted(by_day):
        p = (hits + kappa * prior) / (n + kappa)
        idx = by_day[day]
        for i in idx:
            base[i] = p
        for i in idx:
            hits += outcomes[i]
            n += 1
    return base


def outstanding(data_dir: Path) -> int:
    """已登记但尚未结算的 (卡, 标的) 数。未到期 / 停牌等待 / 未成交都算在这里。"""
    total = sum(len(card.get("picks") or []) for card in load_cards(Path(data_dir)))
    done = {str(r.get("key")) for r in load_settlements(Path(data_dir))}
    return max(0, total - len(done))


#: HAC 核。`truncated` 是默认值，`bartlett` 只为复现旧读数保留。
#:
#: 换核的原因不是「矩形核更常见」，是旧读数**有系统性偏差且不随 n 收敛**：
#: 带宽恒等于 `lag+1 = H` 的 Bartlett 核，对重叠 H 日收益这种等权 MA(H−1)
#: 结构（`γ_k = γ_0·(H−k)/H`）的概率极限是
#:
#:     LRV_hat → (2/3 + 1/(3H²))·LRV = 267/400·LRV = 0.6675·LRV    (H = 20)
#:
#: 也就是 `se` 恒为真值的 `sqrt(267/400) ≈ 0.817` 倍（`sqrt(2/3) = 0.8165` 只是
#: H→∞ 的连续极限，差在第四位小数）。H0 下 `lcb > 0` 的概率因此是 12.2%（n=250）、
#: 10.1%（n=600）、8.6%（n=1000），不是 5%——n 越小越重，而面板的 n≈326 就落在
#: 更重的那一端；`n_eff = n·(se_iid/se)²` 被同一份偏差放大 1.5 倍。
#: 这个偏差来自「带宽固定成模型记忆长度」——Wilson 区间那一类错误，加样本
#: 不会好转。实测见 `scripts/probe_hac_size.py`。
KERNEL_TRUNCATED = "truncated"
KERNEL_BARTLETT = "bartlett"


def hac_lcb(values: list[float], lag: int = 0, *,
             kernel: str = KERNEL_TRUNCATED
             ) -> tuple[float, float, float, float] | None:
    """均值、HAC 标准误、单侧 95% 下界、iid 标准误。

    `lag` 必须覆盖序列的自相关长度。持有 `H` 个交易日的卡，相邻决策日的
    收益重叠 `H-1` 天，逐日改善量因此是 MA(H-1) 过程：按 iid 算标准误会把
    区间收窄 2.3–3.4 倍（323 个可评估日 / 20 日持有期上的两条重叠序列：
    verdict 2.57–2.71、增量改善 2.29–3.38；落盘
    `data/.scratch/pwin_variance.json`）。`lag=0` 退化为 iid。

    默认核是截断（矩形）核：`LRV = γ_0 + 2Σ_{k=1..lag} γ̂_k`，`γ̂_k` 的分母
    取 `n-k`。它在 MA(lag) 下无偏——这正是 `lag` 的来历（`lag` 不是「随便
    取够大」，是持有期重叠的长度），所以末阶不能被权重压掉。Bartlett 的
    `1 - k/(lag+1)` 恰好把最后一阶压到 `1/(lag+1)`，于是永远少 1/3。

    **`LRV` 下界取 `γ_0`**：重叠 H 日的序列在 `k < H` 上 `γ_k ≥ 0`，所以
    `LRV ≥ γ_0` 是模型内的硬下界，不是保守选择。它还挡住截断核的一个退化
    情形——样本里负自相关把 `LRV` 压到 0 时 `se = 0`、`lcb = mean`，
    等于宣称「零误差」（iid 压力测试里 n=250 有 2.5% 的重复踩到）。

    ⚠️ `n` 接近 `lag` 时（如 17 个可评估日 / 20 日持有期）NW 本身不可靠，
    下界仍然偏乐观。这种情形下该看的是 `n_eff`（可能只有 1–2），不是下界。
    """
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    dev = [x - mean for x in values]
    g0 = sum(d * d for d in dev) / n
    if kernel == KERNEL_BARTLETT:
        lr = g0
        for k in range(1, min(lag, n - 1) + 1):
            w = 1.0 - k / (lag + 1.0)
            lr += 2.0 * w * sum(dev[i] * dev[i + k] for i in range(n - k)) / n
        lr = max(lr, 0.0)
    elif kernel == KERNEL_TRUNCATED:
        lr = g0
        for k in range(1, min(lag, n - 1) + 1):
            gk = sum(dev[i] * dev[i + k] for i in range(n - k)) / (n - k)
            lr += 2.0 * gk
        lr = max(lr, g0)
    else:
        raise ValueError(f"kernel 必须是 {KERNEL_TRUNCATED}/{KERNEL_BARTLETT}，"
                         f"收到 {kernel!r}")
    se_iid = math.sqrt(g0 / n)
    se = math.sqrt(lr / n)
    return mean, se, mean - Z90 * se, se_iid


#: Newey-West 可用的最低样本/滞后比。低于它时 Bartlett 权重几乎全是 1、
#: 每个滞后的乘积项只剩几个，方差估计的偏差与方差都失控。
HAC_MIN_RATIO = 3


def effective_sample(n: int, se_iid: float, se: float, lag: int) -> tuple[int, bool]:
    """等效独立样本数，以及 Newey-West 在这个样本量下是否可用。

    `n_eff = n·(se_iid/se)²` 回答「这批逐日观测相当于几个独立样本」。它有两个
    必须处理的边界，否则这个数字会被读成它没有的含义：

    1. **截到观测数 `min(n, …)`**：`se < se_iid`（负自相关或估计噪声）会让公式
       给出比总样本数更大的值。「等效独立样本数超过观测数」没有意义。实测 n=17、
       lag=19 的 MA(19) 序列会给出 55。
    2. **`n < 3·(lag+1)` 时不用 NW**：滞后阶数接近样本量时，同一个过程的不同
       抽样会给出 0.56–1.26 倍的方差膨胀（实测 n=17 抽 5 次）。这种情形退回
       **不重叠窗口数** `n // (lag+1)`。它不是「上界」，是 MA(H−1) 下的
       **模型推断值**：长程方差 ≈ H·γ0 ⇒ `n_eff ≈ n/H`。说成上界会让人以为
       真值只会更小；实际上负自相关时真值更大。真实账本 17 天 / 20 日持有期
       因此得到 1，而不是 NW 噪声给出的 11–55。

    返回 `(n_eff, reliable)`。`reliable=False` 时 `n_eff` 是构造值而不是估计值，
    调用方必须如实标注来源，并且不能只凭它给出「显著」结论。
    """
    if n <= 0:
        return 0, False
    if lag <= 0:
        return n, True
    if n < HAC_MIN_RATIO * (lag + 1):
        return max(1, n // (lag + 1)), False
    if se <= 0 or se_iid <= 0:
        return n, True
    return max(1, min(n, int(round(n * (se_iid / se) ** 2)))), True


def _overlap_lag(rows: list[dict[str, Any]]) -> int:
    """逐日序列的自相关长度 = 最长持有期 - 1。

    取最大值而不是众数：混了多个 horizon 的账本里，重叠由最长的那一档决定，
    低估 lag 只会让下界偏乐观。
    """
    hs = [int(r.get("horizon_sessions") or 0) for r in rows]
    return max(0, max(hs) - 1) if hs else 0


def _daily_means(settled: list[dict[str, Any]], field: str) -> list[float]:
    """把逐 pick 的读数聚成**等权日序列**，按日期升序（HAC 对顺序敏感）。

    与 `_daily_clustered_lcb` 同一个约定：一天算一个观测。同一张卡的 k 个 pick
    共享同一天的行情，把它们当 k 个独立样本会把标准误低估 √k 倍。

    缺 `field` 的行跳过；缺 `as_of` 的行与 `_daily_clustered_lcb` 一样归入同一个
    `None` 组，两个下界因此看到同一批决策日。日期升序是这里**显式**做的：
    `_daily_clustered_lcb` 依赖 `settled` 的插入顺序，本函数不依赖调用方。
    """
    by_day: dict[str, list[float]] = {}
    for row in settled:
        v = row.get(field)
        if v is None:
            continue
        by_day.setdefault(str(row.get("as_of")), []).append(float(v))
    return [sum(v) / len(v) for _, v in sorted(by_day.items())]


def _daily_clustered_lcb(settled: list[dict[str, Any]], outcomes: list[int],
                         base: list[float], brier_const: float
                         ) -> tuple[float | None, float | None, int | None, int, bool]:
    """按决策日聚类算 Brier 改善的单侧 95% 下界。

    同一张卡的多个 pick 共享同一天的行情，把它们当独立样本会把标准误低估
    √k 倍。这里先按 `as_of` 聚合到日，再对"每日改善量"求均值和标准误。
    基线**逐样本**传入（`_expanding_climatology`），因为 abs 口径下它每天在变；
    用整体均值会抹掉冷启动期的真实劣势。

    按日聚类只解决了**同日**相关，没解决**跨日**相关：20 日持有期让相邻决策日
    的收益重叠 19 天。所以标准误再用 `hac_lcb` 做一次 Newey-West 修正，
    lag 由 `_overlap_lag()` 从 horizon 推出。

    返回 `(改善下界, skill 下界, n_eff, hac_lag, n_eff_reliable)`。
    少于 2 个决策日时前三项为 `None`。
    """
    by_day: dict[str, list[tuple[float, int, float]]] = {}
    for row, y, b0 in zip(settled, outcomes, base):
        by_day.setdefault(str(row.get("as_of")), []).append((float(row["p_win"]), y, b0))
    lag = _overlap_lag(settled)
    if len(by_day) < 2:
        return None, None, None, lag, False
    daily = []
    for pairs in by_day.values():
        b = sum((p - y) ** 2 for p, y, _ in pairs) / len(pairs)
        bc = sum((b0 - y) ** 2 for _, y, b0 in pairs) / len(pairs)
        daily.append(bc - b)
    est = hac_lcb(daily, lag)
    if est is None:
        return None, None, None, lag, False
    _, se, lcb, se_iid = est
    n_eff, reliable = effective_sample(len(daily), se_iid, se, lag)
    return lcb, (lcb / brier_const if brier_const > 0 else None), n_eff, lag, reliable


def _source_breakdown(settled: list[dict[str, Any]],
                      basis: str) -> dict[str, dict[str, Any]]:
    """按 p_win 的出处分组计分。

    一个总分如果混合了「账本自举」「先验 0.5」「人手填 0.9」，它答不出
    「我的校准到底行不行」——因为分不清是哪个预测器在得分。旧记录没有出处
    字段，统一归入 `unknown`，不假装知道。
    """
    field = "outcome_med" if basis == BASIS_MEDIAN else "outcome"
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in settled:
        groups.setdefault(str(row.get("p_win_source") or "unknown"), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for name, rows in sorted(groups.items()):
        ys = [int(r[field]) for r in rows]
        n = len(ys)
        hit = sum(ys) / n
        brier = sum((float(r["p_win"]) - y) ** 2 for r, y in zip(rows, ys)) / n
        if basis == BASIS_MEDIAN:
            const = P_CONST_MEDIAN * (1 - P_CONST_MEDIAN)
        else:
            # 同 `score()`：abs 口径的基线必须是展开 climatology。用该组自身的
            # 命中率（样本内常数）会让 skill 恒 ≤ 0——分组越细，恒等式越明显。
            clim = _expanding_climatology(rows, ys)
            const = sum((c - y) ** 2 for c, y in zip(clim, ys)) / n
        skill = (1 - brier / const) if const > 0 else None
        if n < MIN_N:
            verdict = "INSUFFICIENT"
        elif skill is not None and skill > 0:
            verdict = "BEATS_CONSTANT"
        else:
            verdict = "NO_INFORMATION"
        out[name] = {
            "n": n,
            "days": len({str(r.get("as_of")) for r in rows}),
            "hit_rate": round(hit, 4),
            "brier": round(brier, 4),
            "skill": None if skill is None else round(skill, 4),
            "verdict": verdict,
        }
    return out


def score(rows: list[dict[str, Any]], *, strategy_id: str | None = None,
          pending: int | None = None, basis: str = BASIS_ABS) -> dict[str, Any]:
    """对一个结算样本集算命中率 / Brier / skill / 超额 LCB。

    `pending` 由调用方用 `outstanding()` 传入：结算流水里没有"未到期"的行
    （它们根本没被写入），所以不能从 rows 里数出来。

    `basis="median"` 时用 `outcome_med`（跑赢同日全市场中位数）作为因变量，
    常数基线固定 0.5。这条口径下同日多个 pick 近似独立，`n` 才是可信的样本量。
    """
    if basis not in BASIS_CHOICES:
        raise ValueError(f"basis 必须是 {BASIS_CHOICES} 之一，收到 {basis!r}")
    subset = [r for r in rows
              if strategy_id is None or r.get("strategy_id") == strategy_id]
    field = "outcome_med" if basis == BASIS_MEDIAN else "outcome"
    settled = [r for r in subset if r.get(field) is not None]
    not_filled = [r for r in subset if (r.get("fill") or {}).get("status") == "NOT_FILLED"]
    pending_n = int(pending) if pending is not None else 0
    empty = {
        "basis": basis,
        "n": 0, "not_filled": len(not_filled), "pending": pending_n,
        "hit_rate": None, "brier": None, "brier_const": None,
        "brier_random": None, "brier_clim": None, "brier_insample": None,
        "skill": None, "skill_vs_clim": None,
        "skill_lcb": None, "skill_lcb_positive": False,
        "significant": False, "significance": SIGNIFICANCE_UNKNOWN, "days": 0,
        "n_eff": None, "hac_lag": None, "n_eff_reliable": False,
        "logloss": None, "ret_median": None, "excess_mean": None, "excess_lcb": None,
        "verdict": "INSUFFICIENT", "reason": f"已结算样本 0 < {MIN_N}",
        "by_source": {},
    }
    if not settled:
        return empty

    n = len(settled)
    outcomes = [int(r[field]) for r in settled]
    hit = sum(outcomes) / n
    # 一律重算：落盘的 `brier` / `logloss` 是 abs 口径的，median 口径必须按自己的
    # 因变量重算，否则会把两套标签混进同一个分数里。
    brier = sum((float(r["p_win"]) - y) ** 2 for r, y in zip(settled, outcomes)) / n
    logloss = sum(
        -(y * math.log(min(max(float(r["p_win"]), _EPS), 1 - _EPS))
          + (1 - y) * math.log(min(max(1 - float(r["p_win"]), _EPS), 1 - _EPS)))
        for r, y in zip(settled, outcomes)
    ) / n

    # 三个基线回答三个不同的问题，不能互相替代：
    #   random    p = 0.5       无信息基准（median 口径的「随机选股」）
    #   clim      展开 climatology  决策时点可知的最优常数（**诚实基线**）
    #   insample  样本内命中率      事后最优常数，恒 ≥ 任何常数预测器，只做诊断
    brier_random = P_CONST_MEDIAN * (1 - P_CONST_MEDIAN)
    brier_insample = sum((hit - y) ** 2 for y in outcomes) / n
    clim = _expanding_climatology(settled, outcomes)
    brier_clim = sum((c - y) ** 2 for c, y in zip(clim, outcomes)) / n

    # abs 口径的分母必须是 clim，不是 insample：后者含未来信息，任何常数
    # 预测器相对它都恒 ≤ 0（§9.3 的恒等式），那不是一个检验，是同义反复。
    brier_const = brier_random if basis == BASIS_MEDIAN else brier_clim

    skill = (1 - brier / brier_const) if brier_const > 0 else None
    skill_vs_clim = (1 - brier / brier_clim) if brier_clim > 0 else None
    # `skill_lcb` 必须和 `skill` 用**同一个分母**：median 口径的分母是常数 0.25，
    # 所以逐日改善也要相对 0.5 算；abs 口径的分母是展开 climatology，逐日改善
    # 就相对它算。混用会让下界和点估计回答两个不同的问题。
    lcb_base = ([P_CONST_MEDIAN] * n if basis == BASIS_MEDIAN else clim)
    lcb, skill_lcb, n_eff, hac_lag, n_eff_reliable = _daily_clustered_lcb(
        settled, outcomes, lcb_base, brier_const)
    days = len({str(r.get("as_of")) for r in settled})
    significance = significance_of(skill_lcb, n_eff, n_eff_reliable)

    rets = sorted(float(r["exit"]["ret"]) for r in settled)
    median = rets[len(rets) // 2] if len(rets) % 2 else (
        rets[len(rets) // 2 - 1] + rets[len(rets) // 2]) / 2
    excess_field = "excess_med" if basis == BASIS_MEDIAN else "excess"
    excess = [float(r[excess_field]) for r in settled if r.get(excess_field) is not None]
    # 逐 pick 的 `_lcb` 是 iid 下界：同一张卡的 k 个 pick 共享当天行情，相邻决策日
    # 的 H 日收益又重叠 H−1 天，两层相关都没进方差。`hac_lcb` 的文档实测这个方向
    # 会窄 2.3–3.4 倍，而**同一个函数**里的 `skill_lcb` 已经走日聚 + HAC——
    # `excess_lcb` 单独走 iid 是口径不一致，不是保守选择。
    #
    # 这里不改 `excess_lcb` 的语义（它不驱动任何判定，只在前端显示），而是并列
    # 给一个同口径的读数。中心是「等权日均值」，每卡 pick 数相同时与 `excess_mean`
    # 逐位相同；不同时两者都是 μ 的无偏估计，差别只是加权。
    exc_daily = _daily_means(settled, excess_field)
    exc_hac = hac_lcb(exc_daily, _overlap_lag(settled)) if len(exc_daily) >= 2 else None

    baseline = "随机 0.5" if basis == BASIS_MEDIAN else "展开 climatology"
    if n < MIN_N:
        verdict, reason = "INSUFFICIENT", f"已结算样本 {n} < {MIN_N}"
    elif skill is not None and skill > 0:
        verdict, reason = "BEATS_CONSTANT", (
            f"Brier skill {skill:+.2%} > 0（基线 {baseline}）")
    else:
        verdict, reason = "NO_INFORMATION", (
            f"Brier skill {skill:+.2%} ≤ 0（基线 {baseline}）" if skill is not None
            else "常数基线退化（样本同质）")

    return {
        "basis": basis,
        "n": n,
        "not_filled": len(not_filled),
        "pending": pending_n,
        "days": days,
        "hit_rate": round(hit, 4),
        "brier": round(brier, 4),
        "brier_const": round(brier_const, 4),
        "brier_random": round(brier_random, 4),
        "brier_clim": round(brier_clim, 4),
        "brier_insample": round(brier_insample, 4),
        "skill": None if skill is None else round(skill, 4),
        "skill_vs_clim": None if skill_vs_clim is None else round(skill_vs_clim, 4),
        "skill_lcb": None if skill_lcb is None else round(skill_lcb, 4),
        # 读数：下界为正吗。它不回答「这个下界可不可信」——那是 `significance`
        # 的事。两者分开，是因为它们的失败模式不一样：读数可能对但无意义，
        # 结论必须同时承担「测得出」和「测得准」。
        "skill_lcb_positive": bool(skill_lcb is not None and skill_lcb > 0),
        # 结论三态。`n_eff` 在这里只是标签的输入，不隐藏数字、不拦截请求。
        # `False` 不再同时表示「测过了，没有」和「测不了」。
        "significance": significance,
        # 向后兼容：`significant` 的语义保持「结论成立」，即 `significance == PASS`。
        # 不把它降格成纯读数——否则 `n_eff=1` 的账本会重新拿到 `significant=true`，
        # 而所有读这个字段的旧调用方都会把它读成「显著」。保守的错误比激进的错误便宜。
        "significant": significance == SIGNIFICANCE_PASS,
        # 等效独立样本数：`days` 是决策日数，`n` 是观测数，都不回答
        # 「有几个独立样本」。20 日持有期让相邻决策日重叠 19 天，
        # 386 条观测 / 40 天可能只值 1–2 个独立样本。
        "n_eff": n_eff,
        "hac_lag": hac_lag,
        # False ⇒ `n_eff` 是构造值（不重叠窗口数，MA(H−1) ⇒ n/H），不是估计值；
        # 此时任何基于下界的结论都只能读作「不可判定」。
        "n_eff_reliable": n_eff_reliable,
        "logloss": round(logloss, 4),
        "ret_median": round(median, 4),
        "excess_mean": round(sum(excess) / len(excess), 4) if excess else None,
        # 旧字段：逐 pick iid 下界，**不得用于推断**（同日相关 + 持有期重叠未计）。
        # 保留是因为前端与历史导出在读它；新读数见下面两个字段。
        "excess_lcb": (round(_lcb(excess), 4) if excess and _lcb(excess) is not None else None),
        # 与 `skill_lcb` 同口径：日聚 + Newey-West（lag = 最长持有期 − 1）。
        "excess_se_hac": None if exc_hac is None else round(exc_hac[1], 6),
        "excess_lcb_hac": None if exc_hac is None else round(exc_hac[2], 6),
        "verdict": verdict,
        "reason": reason,
        "by_source": _source_breakdown(settled, basis),
    }


# ---------------------------------------------------------------------------
# 常数基线（先验）
# ---------------------------------------------------------------------------


def prior(data_dir: Path, *, horizon: int = 20, sample: int = 12,
          step: int = 20) -> dict[str, Any]:
    """全市场"未来 horizon 日上涨"的基础概率，供样本不足时做参照。

    取最近 `sample` 个间隔 `step` 个交易日的起点，统计所有有行情的标的。
    它不是任何策略的成绩，而是"闭眼买一篮子"的胜率——策略必须显著超过它。
    """
    key = (str(Path(data_dir).resolve()), bars.latest_session(Path(data_dir)), horizon, sample, step)
    with _PRIOR_LOCK:
        cached = _PRIOR_CACHE.get(key)
    if cached is not None:
        return cached

    calendar = bars.sessions(Path(data_dir))
    if len(calendar) < horizon + 2:
        return {"p0": None, "n_dates": 0, "n_obs": 0, "horizon": horizon,
                "reason": "交易日不足，无法计算基线"}

    last = len(calendar) - 1 - horizon
    starts = [i for i in range(last, -1, -step)][:sample]
    pos = 0
    total = 0
    dates: list[str] = []
    daily_p: list[float] = []
    for i in starts:
        a_day, b_day = calendar[i], calendar[i + horizon]
        a = bars.day_rows(Path(data_dir), a_day)
        b = bars.day_rows(Path(data_dir), b_day)
        if a.is_empty() or b.is_empty():
            continue
        joined = a.select(["symbol", "close"]).join(
            b.select(["symbol", "close"]), on="symbol", suffix="_end")
        joined = joined.filter((joined["close"] > 0) & (joined["close_end"] > 0))
        if joined.is_empty():
            continue
        rets = joined["close_end"] / joined["close"] - 1.0
        day_pos = int((rets > 0).sum())
        pos += day_pos
        total += joined.height
        daily_p.append(day_pos / joined.height)
        dates.append(a_day)

    # 采样日之间的离散度才是 p0 的真实误差来源。把 3 万个横截面观测当独立样本
    # 会把精度高估一个数量级以上——同一天所有股票同涨同跌。
    p0_se = None
    if len(daily_p) >= 2:
        m = sum(daily_p) / len(daily_p)
        var = sum((x - m) ** 2 for x in daily_p) / (len(daily_p) - 1)
        p0_se = math.sqrt(var / len(daily_p))

    result = {
        "p0": round(pos / total, 4) if total else None,
        "p0_se": None if p0_se is None else round(p0_se, 4),
        "p0_range": ([round(min(daily_p), 4), round(max(daily_p), 4)]
                     if daily_p else None),
        "n_dates": len(dates),
        "n_obs": total,
        "horizon": horizon,
        "sample": sample,
        "step": step,
        "sample_dates": dates,
        "note": ("常数基线：同期全市场上涨占比。注意 p0 随 sample/step 漂移"
                 "（同一数据下 sample=6 ⇒ 35.7%、12 ⇒ 42.0%、24 ⇒ 48.2%），"
                 "它不是一个常数，不能直接当 Brier 基线；"
                 "`median` 口径的基线是精确的 0.5，不需要估计。"),
    }
    with _PRIOR_LOCK:
        _PRIOR_CACHE[key] = result
    return result


def clear_prior_cache() -> None:
    with _PRIOR_LOCK:
        _PRIOR_CACHE.clear()
    with _MEDIAN_LOCK:
        _MEDIAN_CACHE.clear()
