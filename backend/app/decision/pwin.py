"""p_win 的真实来源 — 账本自举，而不是外部命中率。

## 为什么不能用「回测命中率」

回测的命中率是样本内的：参数在同一段历史上挑过，命中率被选择偏差抬高。
而且回测的入场由策略自己决定，决策卡的入场是限价单——口径不同，不能互相背书。

## 为什么不能用「人工填值」

手填的 p_win 无法审计。Brier 会奖励乐观的填值（只要运气好），
而账本的全部目的是把「运气」和「能力」分开。

## 本模块的做法：账本自举

第 N 张卡的 p_win，只能来自前 N-1 张**已封存并结算**的卡。
这是天然的 walk-forward：没有任何未来信息能进入 p_win。

    冷启动（n = 0）  -> 回退到常数基线，source="prior"，reliable=False
    n < MIN_N        -> 同上，verdict="INSUFFICIENT"
    n >= MIN_N       -> 收缩估计，source="ledger"

收缩：`p = (hits + kappa * prior) / (n + kappa)`。
`kappa` 是「等价于多少个虚拟样本」的先验强度，固定为 20，**不参与调优**
（调它就是在用验证集选参，会把 skill 抬高）。

## 第二层：这个来源本身可靠吗

有 p_win 还不够，还要回答「用历史胜率预测未来，在样本外有效吗」。
本模块把历史按决策日重放一遍：第 i 天的 p 只用第 i 天之前的数据估，
然后算 Brier 改善及其按日聚类、按持有期做 Newey-West 修正的单侧下界。
重叠持有期让逐日观测强自相关（lag1 0.66–0.87），不修正会把区间收窄
2.57–2.71 倍；`n_eff` 就是修正后的等效独立样本数（323 个可评估日 ⇒ 44–49）。
落盘 `data/.scratch/pwin_variance.json` 的 `verdict_rows`——**别和
`summary.se_infl_*` 混用**，那是增量改善序列（2.29–3.38，`n_eff` 28–62），
两条序列的自协方差不同。

    lcb > 0  -> RELIABLE    历史胜率对未来有预测力
    lcb <= 0 -> UNRELIABLE  历史胜率对未来没有预测力（可能是非平稳）

`UNRELIABLE` 不是「算错了」，而是一个结论：**这个策略的历史命中率不该被当作
信息使用**。实测中 20 日动量策略就是这种情况——训练段 skill +4.45%，
验证段 -4.19%，符号翻转。

## 「之前」是**结算之后**，不是「决策之后」

这是本模块最容易写错的一处，也是它曾经写错的地方。一张 `as_of = d` 的卡，
结果要到 `d + horizon` 才出来。所以决策日 `D` 能用的样本必须满足：

    exit.date < D              （优先：结算记录里的真实出场日）
    或  D - d > horizon + 1    （回退：没有 exit.date 时按决策日间隔保守判定）

按 `days[i-window:i]` 直接回看是错的：它把最近 `horizon` 天、尚未结算的结果
当成了已知信息。这种重放会给出偏乐观的 skill，而且偏多少取决于窗口长度与
制度长度之比——正是那种「看起来通过了、其实没通过」的检验。

`scripts/probe_pwin_source.py` 的预登记口径本来就是对的
（`cutoff = pos[t] - HORIZON`）；本模块现在与它一致。
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from typing import Any

from app.decision.settle import (
    BASIS_CHOICES,
    BASIS_MEDIAN,
    DEFAULT_KAPPA,
    MIN_NEFF,
    MIN_N,
    P_CONST_MEDIAN,
    SIGNIFICANCE_UNKNOWN,
    effective_sample,
    hac_lcb,
    significance_of,
)

#: 至少这么多个决策日，历史胜率才有「时间维度」可言；少于 2 天无法算聚类标准误。
MIN_DAYS = 2
#: 默认滚动窗口（决策日数）。实测 40 / 80 / 160 / 320 / 全历史 的 skill 差异
#: 小于 1 个百分点，所以这个值不敏感——真正重要的是样本外验证，不是窗口长度。
#: 但它对 `verdict_incremental` 是**定义性**的：窗口一旦覆盖全部已可见决策日，
#: 近端与全历史就是同一批样本，两个估计量恒等 ⇒ 检验没有功效，结论只能是
#: `NOT_TESTABLE`。要让这个检验可判定，window 必须小于可评估日数——但这只是
#: 必要条件：把 window 缩到 1 只能让检验**跑起来**，`n_eff` 仍由
#: `可评估日数 / H` 决定，达到 `MIN_NEFF` 需要约 `MIN_NEFF × H` 个可评估日。
#: 两个障碍的补救方式不同，所以 `_not_testable_remedy` 会先算再开药方。
DEFAULT_WINDOW = 250

SOURCE_PRIOR = "prior"
SOURCE_LEDGER = "ledger"

VERDICT_INSUFFICIENT = "INSUFFICIENT"
VERDICT_RELIABLE = "RELIABLE"
VERDICT_UNRELIABLE = "UNRELIABLE"

#: 相对**展开基线**的判定。与 RELIABLE/UNRELIABLE 是两个正交的问题：
#: 前者问「比无信息常数好吗」，后者问「比同样诚实的历史常数好吗」。
#: 一个常数 p 可以同时是 RELIABLE 和 NO_INCREMENT——那不是矛盾，那是
#: 「水平对、但没有区分度」。
VERDICT_INCREMENTAL = "INCREMENTAL"
VERDICT_NO_INCREMENT = "NO_INCREMENT"
#: 统计显著但量级不足。`lcb > 0` 只说明「可分辨」，不说明「有用」：
#: 实测 abs 口径在 window=250 下改善 +0.00011、下界 +0.00003，若只按符号判定
#: 会得到一个「显著但等于零」的 INCREMENTAL。这一档专门装这种情况。
VERDICT_MARGINAL = "MARGINAL"
#: 检验没有功效——**不是**「没有增量」。
#:
#: `window` 覆盖了全部已可见决策日时，`visible[-window:] ≡ visible` ⇒
#: `p ≡ p_clim` ⇒ 改善恒为 0、下界恒为 0。这个 0 是恒等式的产物，不是估计值：
#: 两个估计量吃的是同一批样本，比较它们等于拿尺子量自己。把它报成
#: `NO_INCREMENT` 会输出一句强度高于证据的话（「近端不比全历史多含信息」），
#: 而这句话需要的对照根本不存在。
#:
#: 与 `INSUFFICIENT` 的区别：那个是**样本不够**；这个是**配置上不可识别**——
#: 先得把 window 缩小到能截断，才谈得上判定。缩了之后仍可能卡在样本上
#: （真实账本就是这样），所以补救方式要算，不能由状态名推（`_not_testable_remedy`）。
VERDICT_NOT_TESTABLE = "NOT_TESTABLE"

#: 判定「有增量」所需的最小 Brier 改善。0.001 ≈ 预测误差降低 0.1 个百分点，
#: 低于这个量级即使下界为正，也不足以改变任何决策。
MIN_INCREMENTAL_EFFECT = 0.001

#: 单侧 95% 下界 + 80% 功效：`z_{0.95} + z_{0.80}`。
#:
#: `lcb > MIN_INCREMENTAL_EFFECT` 要成立，真值必须比门槛高出这么多个标准误
#: （`P(PASS) = Φ((μ-τ)/se - 1.645)`，令其为 0.8 即得）。所以
#:
#:     可检出下限 = MIN_INCREMENTAL_EFFECT + 2.486 · se_incremental
#:
#: 这个数把「门槛」和「分辨率」分开，而 §9.21–§9.25 一直把两者当成一件：
#: 门槛 0.001 对应约 3.2pp 的真实抬升（改善 ≈ a²），而 900 日账本上
#: `se_incremental ≈ 9e−4` ⇒ 可检出下限 ≈ 0.0032，约 20pp。**门槛比自己的
#: 检出下限低 3 倍（Brier 单位）/ 6 倍（胜率 pp）**，于是判定只在 UNKNOWN
#: 与 FAIL 之间摆动，`PASS` 成了名义上存在的状态（§9.26 §9.27）。
#:
#: 报出这个数，是为了让每个 `NO_INCREMENT` 自带分辨率：读者能看出「测不出」
#: 指的是「低于 0.0032 的改善测不出」，而不是「没有改善」。
_MDE_Z = 1.645 + 0.842


def _field(basis: str) -> str:
    return "outcome_med" if basis == BASIS_MEDIAN else "outcome"


def _prior_of(basis: str) -> float:
    """常数基线。median 口径是精确的 0.5；abs 口径没有精确值，只能给 0.5 当无信息先验。"""
    return P_CONST_MEDIAN if basis == BASIS_MEDIAN else 0.5


def _select(rows: list[dict[str, Any]], *, strategy_id: str | None,
            horizon: int | None, basis: str) -> list[dict[str, Any]]:
    field = _field(basis)
    out = []
    for r in rows:
        if r.get(field) is None:
            continue
        if strategy_id is not None and r.get("strategy_id") != strategy_id:
            continue
        if horizon is not None and int(r.get("horizon_sessions") or 0) != int(horizon):
            continue
        out.append(r)
    out.sort(key=lambda r: (str(r.get("as_of")),
                            str((r.get("exit") or {}).get("date") or "")))
    return out


def resolve_horizon(rows: list[dict[str, Any]], *, strategy_id: str | None = None,
                    horizon: int | None = None, basis: str = BASIS_MEDIAN) -> int | None:
    """`horizon=None` 时取样本数最多的那个 horizon。

    不同 horizon 的胜率不可比（5 日和 20 日的"跑赢中位数"是两件事），
    把它们混在一起取平均会得到一个既不是 5 日也不是 20 日的数字。
    """
    if horizon is not None:
        return int(horizon)
    counts: dict[int, int] = {}
    for r in _select(rows, strategy_id=strategy_id, horizon=None, basis=basis):
        h = int(r.get("horizon_sessions") or 0)
        counts[h] = counts.get(h, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: (counts[k], k))


def _by_day(rows: list[dict[str, Any]], field: str) -> list[tuple[str, list[int], str | None]]:
    """按决策日聚合，并记下该日**最后**一个出场日。

    取最大值而不是最小值：一天里只要还有一笔没结算完，这一天的结果就还不完整，
    不能整体当作已知。保守取 max，宁可少用一个样本。
    """
    groups: dict[str, list[int]] = {}
    last_exit: dict[str, str] = {}
    for r in rows:
        day = str(r.get("as_of"))
        groups.setdefault(day, []).append(int(r[field]))
        ex = (r.get("exit") or {}).get("date")
        if ex:
            ex = str(ex)
            if ex > last_exit.get(day, ""):
                last_exit[day] = ex
    return [(d, groups[d], last_exit.get(d)) for d in sorted(groups)]


def _visible(exit_date: str | None, today: str, gap_days: int, horizon: int) -> bool:
    """决策日 `today` 时，`gap_days` 天前的那个决策日的结果是否已经可见。

    有真实出场日就用它（精确）。没有就退回按决策日间隔判定：结算的出场日是
    `as_of + 1 + horizon`（次日入场 + 持有 horizon 天），决策日又是交易日的子集，
    所以需要 `gap > horizon + 1` 才能保证 `today > exit`。这个回退只会更保守。
    """
    if exit_date:
        return exit_date < today
    return gap_days > horizon + 1


def _shrink(hits: int, n: int, prior: float, kappa: float) -> float:
    return (hits + kappa * prior) / (n + kappa)


def prior_weights(n: int, n_eff: int | None, kappa: float,
                  *, source: str) -> tuple[float, float | None]:
    """这个 p_win 里先验占多大比重。两个分母各算一次，差值就是这个字段的意义。

    - `prior_weight = κ/(n+κ)`：按**观测数**算。真实账本 386 条时只有 4.9%，
      读起来像「几乎全是证据」。
    - `prior_weight_effective = κ/(n_eff+κ)`：按**等效独立样本数**算。同一份
      账本 `n_eff=1` 时是 95.2%——这才是那个 0.214 的真实构成。

    两个都要给。只给前者会把「386 条高度重叠的观测」当成 386 个独立样本；
    只给后者又看不出差在哪里。`n_eff=None`（没跑 walk-forward，或样本不够）
    时后者为 `None`：「没测」既不是「全先验」也不是「全证据」。

    这是**标签**，不是 gate。`prior_weight_effective > 0.5 ⇒ 拒绝建卡` 会重新
    落入 §9.14 的闭环：建卡 → 结算 → `n_eff` 涨。所以它只进出处、进哈希。
    """
    if source == SOURCE_PRIOR:
        return 1.0, 1.0
    raw = round(kappa / (n + kappa), 6) if n > 0 else 1.0
    eff = None if n_eff is None else round(kappa / (n_eff + kappa), 6)
    return raw, eff


def _mean_se_lcb(values: list[float], lag: int = 0
                 ) -> tuple[float, float, float, int, bool]:
    """均值、标准误、单侧 95% 下界、有效独立样本数、NW 是否可用。

    `lag > 0` 时标准误用 HAC（截断核，见 `settle.hac_lcb`）。持有 `horizon`
    个交易日的卡，相邻决策日的结果重叠 `horizon-1` 天，逐日改善量因此是
    MA(horizon-1) 过程；按 iid 算会把区间收窄 2.3–3.4 倍（323 个可评估日 /
    20 日持有期上的两条序列：verdict 2.57–2.71、增量改善 2.29–3.38，落盘
    `data/.scratch/pwin_variance.json`）。`n_eff = n · (se_iid/se)²` 是
    「这批逐日观测相当于几个独立样本」——§9.2 第 3 条说的 18 个不重叠窗口，
    就是它的量级。

    `se` 的口径在 §9.23 换过一次核（Bartlett → 截断）。旧口径下 `se` 恒为
    真值的 0.817 倍，`n_eff` 因此被系统性放大 1.5 倍：同一个门槛 30 在旧
    读数下约等于 20 个真独立样本。换核没有改 `MIN_NEFF` 的值，改的是它
    终于数对了单位。

    最后一位是 `reliable`：`n < 3·(lag+1)` 时 NW 不可用，`n_eff` 退化为
    不重叠窗口数（构造值，MA(H−1) ⇒ n/H）。真实账本 17 天 / 20 日持有期
    在这里得到 1，而不是 NW 噪声给出的 11–55。
    """
    est = hac_lcb(values, lag)
    if est is None:  # 调用方已保证 len >= 2，这里只是类型收窄
        raise ValueError("至少需要 2 个观测")
    mean, se, lcb, se_iid = est
    n_eff, reliable = effective_sample(len(values), se_iid, se, lag)
    return mean, se, lcb, n_eff, reliable


def _trading_span(sessions: list[str] | None, first: str | None,
                  last: str | None, count: int) -> int | None:
    """`first`…`last` 之间有几个**交易日**（含两端）。日历不可用时返回 `None`。

    这个函数存在的唯一理由是单位：分子数的是决策日，分母数的是交易日。
    每 3 个交易日发一张卡时，两天卡之间隔 3 个交易日，`k // (lag+1)` 因此
    把上限低估 3 倍（`scripts/probe_neff_units.py`：16 vs 48）。

    日历不可用（没传、长度不够、日期落在范围外）时返回 `None`，调用方退回
    按决策日计数的旧口径——**不是**静默采用一个更小的上限，而是让调用方
    在结果里标注 `n_eff_cap_unit="decision_days"`。
    """
    if not sessions or not first or not last or count <= 0:
        return None
    lo = bisect_left(sessions, first)
    if lo >= len(sessions):
        return None
    # `last` 落在日历末尾之后时钳到最后一格：账本可能比行情多一天（当天刚发卡、
    # 行情还没落盘），此时宁可少算一格，也不要整条退化回决策日口径。
    hi = min(bisect_left(sessions, last), len(sessions) - 1)
    span = hi - lo + 1
    # 每个决策日都是一个不同的交易日 ⇒ 跨度不可能小于决策日数。小于就是日历
    # 与账本对不上（补了历史卡、或 as_of 不是交易日），此时退回旧口径。
    return span if span >= count else None


def _identifiable_sample(n_eff: int | None, truncated_days: int | None,
                         lag: int | None, *,
                         trading_days: int | None = None) -> int | None:
    """可辨识性的分母：等效独立样本不能超过真正比过的天数。

    **这是本模块最容易被填充序列骗过的地方。** 没有被截断的那一天
    `p == p_clim` ⇒ `d_i == 0`，而且这个 0 是结构性的：窗口盖住全部已可见日
    时，它对任何一份账本都恒等于 0。把这些 0 当成观测喂给 NW，方差被稀释
    ⇒ `se ≈ se_iid` ⇒ `n_eff ≈ n`。实测 256 个可评估日、6 个对照日得到
    `n_eff=57`（另一份账本 81），1 个对照日得到 `n_eff=256`：
    **一天对照等于 256 个独立样本**，于是 `significance_incremental` 在
    只有 1 天对照时也能给出 `FAIL`——一个比「测不了」强得多的话。

    上限不能只用 `truncated_days`。对照日按决策日连续排列（`visible` 单调
    增长，一旦超过 window 就一直超过），持有 `H` 天时相邻两天重叠 `H−1` 天，
    独立样本数至多是 `truncated_days // (lag+1)`。这与 `effective_sample`
    的退化值、`_not_testable_remedy` 的推算是同一条 MA(H−1) 模型；`H=1` 时
    它恰好退化为 `truncated_days`。

    为什么不用更弱的 `min(n_eff, truncated_days)`：同一份 256 天账本上，
    它在 `truncated_days=36`、`n_eff_nw=17` 时给出 17——36 天按 MA(19) 只有
    约 1.8 个独立样本，报 17 仍然高估一个数量级。而且 `n_eff_nw` 随 window
    非单调下降（57 → 35 → 26 → 22 → 19 → 18 → 17 …），取 min 之后序列是
    6 → 11 → 16 → 21 → 19 → 18 → 17，先升后降，
    「证据越多、分母越大」这个方向都保不住。

    截断后的值**不是**「上界」：它是 MA(H−1) 下的模型推断值，与
    `effective_sample` 里那个 `n // (lag+1)` 是同一条公式、同一个含义
    （说成上界会让人以为真值只会更小；负自相关时真值更大，见
    `settle.effective_sample` 第 2 条）。这种措辞曾经在本模块和 `settle`
    里各写一半、互相矛盾（§9.23）。
    `n_eff_reliable` 保持原义（NW 是否可用），不因为这里截断而翻转——
    `effective_sample` 里的 `min(n, …)` 也是这么做的。
    它只影响 `significance` 的三态：分母没到 `MIN_NEFF` 就只能是 `UNKNOWN`。

    **`trading_days` 是同一个上限的另一个分子。** `truncated_days` 数的是
    决策日，`lag+1` 数的是交易日，两者只在「每个交易日都发卡」时相等。发卡变
    稀疏时间隔被放大：每 3 个交易日一张卡，`k` 个对照日跨 `3k` 个交易日，
    上限是 `3k/(lag+1)` 而不是 `k/(lag+1)`——低估 3 倍。传了日历就用真实跨度，
    没传就退回决策日计数并让调用方标注单位（`n_eff_cap_unit`），不假装两者一样。
    """
    if n_eff is None or truncated_days is None or truncated_days <= 0:
        return n_eff
    step = (lag or 0) + 1
    # 跨度小于决策日数说明日历与账本对不上（`_trading_span` 已挡，这里再挡一次：
    # 直接调用这个函数的判定路径没有日历可校验）。
    span = (trading_days if trading_days and trading_days >= truncated_days
            else truncated_days)
    return min(n_eff, max(1, span // step))


def _coverage(truncated: int | None, evaluated: int | None) -> str:
    """这个检验实际比了多少天（不带括号，由调用方拼进同一个括号）。

    逐日改善里，**没有被截断的那一天恒等于 0**：`p` 与 `p_clim` 是同一个数。
    这些 0 是真的（当天两个预测器确实相同），但它们不构成「近端 vs 远端」的
    证据。`mean` 是全部评估日的平均，所以 `k/n` 越小，均值被摊得越薄——
    把 `k/n` 写进结论，读者才知道那个 `+0.000097` 是在几天上量出来的。
    """
    if truncated is None or evaluated is None or evaluated <= 0:
        return ""
    if truncated <= 0:
        return f"{evaluated} 天里没有一天被截断"
    if truncated >= evaluated:
        return f"{evaluated} 天全部被截断"
    return (f"只有 {truncated}/{evaluated} 天被截断，"
            f"其余 {evaluated - truncated} 天两个估计量恒等、改善恒为 0")


def _tail(n_eff: int | None, n_eff_reliable: bool | None,
          truncated: int | None, evaluated: int | None,
          raw_n_eff: int | None = None) -> str:
    """把「几个独立样本」和「几天真的比过」放进**同一个**括号。

    两个括号背靠背（`（等效独立样本 57）（只有 1/57 天被截断…）`）读起来像
    两个脚注，而它们修饰的是同一句话。

    `n_eff_reliable=False` 时 `n_eff` 是构造值（不重叠窗口数），不是 NW 估计值。
    这一点必须写进句子：`1` 和「估出来是 1」是两件事，前者是 `n // (lag+1)`。

    `raw_n_eff > n_eff` 是第二种构造值：NW 真的估出了一个数，但被对照天数
    截断了（`_identifiable_sample`）。它也必须是两个数一起出现——真实账本
    `window=250` 报 3，NW 读数 32，只印 3 就是在说「估出来是 3」。这正是
    §9.23 里那句「同一个公式、两处标注」的现场：`reliable` 回答的是
    「NW 可不可用」，不是「这个数是不是构造值」。
    """
    parts = []
    if n_eff is not None:
        if n_eff_reliable is False:
            parts.append(f"等效独立样本 {n_eff}，NW 不可用，取不重叠窗口数")
        elif raw_n_eff is not None and raw_n_eff > n_eff:
            parts.append(f"等效独立样本 {n_eff}"
                         f"（按对照日的交易日跨度折算；NW 读出 {raw_n_eff}）")
        else:
            parts.append(f"等效独立样本 {n_eff}")
    cover = _coverage(truncated, evaluated)
    if cover:
        parts.append(cover)
    return f"（{'；'.join(parts)}）" if parts else ""


def _no_denominator(n_eff: int | None, n_eff_reliable: bool | None,
                    raw_n_eff: int | None = None) -> str:
    """「这个下界没有分母」的完整说法：几、是不是构造值、门槛是几。

    只说「等效独立样本 2」不够：2 可能是 NW 估出来的，也可能是
    `n // (lag+1)` 这个构造值。两种情况都不够 `MIN_NEFF`，但读者要能分辨
    自己看到的是哪一种——§9.11 起这个项目就要求「构造值必须标注」。
    """
    if n_eff is None:
        return "等效独立样本未知：下界没有分母，只能读作不可判定"
    if n_eff_reliable is False:
        return (f"等效独立样本 {n_eff}（NW 不可用，取不重叠窗口数）< {MIN_NEFF}："
                f"下界没有分母，只能读作不可判定")
    note = (f"（按对照日的交易日跨度折算；NW 读出 {raw_n_eff}）"
            if raw_n_eff is not None and raw_n_eff > n_eff else "")
    return (f"等效独立样本 {n_eff}{note} < {MIN_NEFF}："
            f"下界没有分母，只能读作不可判定")


def _not_testable_remedy(days: int | None, hac_lag: int | None, *,
                         trading_days: int | None = None) -> str:
    """`NOT_TESTABLE` 要靠什么解除——改配置，还是攒样本。

    这个状态有两个可能的障碍，它们经常同时成立：

    1. `window` 盖住了全部已可见日 ⇒ 两个估计量恒等（**配置**）；
    2. 已可见日本身不够多 ⇒ 就算把 window 缩到 1，`n_eff` 也到不了 `MIN_NEFF`
       （**样本**）。

    只说第 1 条会开错药方。实测（e2e 账本，39 个决策日 / 17 个可评估日 /
    H=20）：把 window 从 250 缩到 12，检验确实跑起来了，但 `n_eff=1`、
    `significance_incremental=UNKNOWN`，而 `verdict_incremental` 从
    `NOT_TESTABLE` 变成了 `NO_INCREMENT`——**一个看起来像结论、实际来自噪声的
    词**，比原先的「测不了」更容易被引用。所以配置能不能解除这个状态，必须先算。

    判据用构造值 `span // (lag+1)`：MA(H−1) 下长程方差 ≈ H·γ0 ⇒ `n_eff ≈ n/H`。
    它在 `n < 3(lag+1)` 时本来就是 `effective_sample` 的返回值，在更大的样本上
    也只是同阶近似——句子里写「约」。

    **门槛的单位是交易日，不是可评估日。** 30 个独立样本 × H 个交易日 = 630
    个交易日，这个数不随发卡节奏变；变的是它折成多少张卡。传了日历（
    `trading_days`）就把两者都写出来：每 3 个交易日一张卡时，630 个交易日
    只要 210 个可评估日——旧句子对 daily 和 sparse 印同一个 630，把「要攒多久」
    多报了 3 倍，而门禁想要的方向恰恰是把发卡变稀疏。
    """
    if hac_lag is None or days is None:
        return "要让这个检验可判定，window 必须小于可评估日数，且样本要足够到能估出分母"
    step = hac_lag + 1
    span = trading_days if trading_days and trading_days >= days else days
    best = max(1, span // step)
    if best < MIN_NEFF:
        # 每个决策日占几个交易日。日历缺失时它是 1（等同于旧口径），
        # 所以这个比值同时也是「这句话用的是哪种单位」的读数。
        cadence = span / days
        need_td = MIN_NEFF * step
        if cadence > 1.05:
            need_days = int(round(need_td / cadence))
            plan = (f"需要约 {need_td} 个交易日（按当前每 {cadence:.1f} 个交易日"
                    f"一张卡，约 {need_days} 个可评估日）")
        else:
            plan = (f"需要约 {need_td} 个交易日（每个交易日都发卡 ⇒ "
                    f"也是 {need_td} 个可评估日）")
        return (f"把 window 缩到 1 也只能让检验跑起来：{days} 个可评估日"
                f"（{span} 个交易日）、MA({hac_lag}) ⇒ 等效独立样本约 "
                f"{best} < {MIN_NEFF}，结论仍然不可判定。要让它可判定，"
                f"按 MA(H−1) 的构造值推算{plan}，不是改配置能解开的")
    return "要让这个检验可判定，window 必须小于可评估日数"


def _incremental_verdict(mean: float, lcb: float,
                         n_eff: int | None = None, *,
                         testable: bool = True,
                         window: int | None = None,
                         max_visible: int | None = None,
                         truncated_days: int | None = None,
                         evaluated_days: int | None = None,
                         n_eff_reliable: bool | None = None,
                         hac_lag: int | None = None,
                         raw_n_eff: int | None = None,
                         truncated_trading_days: int | None = None,
                         evaluated_trading_days: int | None = None,
                         mde: float | None = None
                         ) -> tuple[str, str, str]:
    """相对展开基线的判定。`lcb > 0` 是必要条件，不是充分条件。

    单独抽出来是为了能被直接测：构造一个「改善落在 (0, MIN_INCREMENTAL_EFFECT)」
    的真实序列很难，但这个判定本身必须被钉住——否则「显著但等于零」会被
    报成 INCREMENTAL。

    返回 `(verdict, reason, significance)`。第三个值是与 `verdict` 那条序列
    **同一个三态**（PASS / FAIL / UNKNOWN），回答「这个下界有没有分母」：

    - `verdict` 读符号与量级，`significance` 读可辨识性。二者正交：
      `INCREMENTAL × UNKNOWN` 表示「改善为正、且过门槛，但有效样本不足 30，
      不能当结论」——这是 §9.17 留下的那个不对称，§9.21 补齐。
    - 为什么不是给 `verdict_incremental` 加第五态：`NOT_TESTABLE` 时改善、下界、
      `n_eff` 全是恒等式的产物，必须撤掉；样本不够时数字是真的，只是没有分母。
      一个状态既要撤数字又要留数字，就是两个状态。**补救方式不能当判据**——
      真实账本的 `NOT_TESTABLE` 同样只能靠攒样本解除（§9.21 §六），两种障碍
      经常同时成立。所以状态留在 `verdict`，可辨识性进 `significance`，
      与第一个检验的 `verdict × significance` 同构。

    `testable=False` 时**不读** `mean` / `lcb`，直接返回 `NOT_TESTABLE` 与
    `UNKNOWN`。那两个数在这个分支里恒为 0（两个估计量用的是同一批样本），
    报出去会被读成「近端没有增量」——而这句话需要的对照不存在。状态和数字
    必须二选一：给了状态就不能再给数字，否则读者只会看数字。

    `testable=False` 的**补救方式**由 `_not_testable_remedy` 决定，不由这个状态名
    决定：窗口盖住全历史只说明「现在没跑」，不说明「跑了就能读」。真实账本
    17 个可评估日 / MA(19)，把 window 缩到 1 只会得到 `n_eff=1` 和一个更像
    结论的 `NO_INCREMENT`。所以句子必须点名真正卡住的那个量。

    `truncated_days` / `evaluated_days` 是**覆盖度**，不是阈值。`testable=True`
    只保证「至少有一天被截断」，它不保证这一天足够多：57 个评估日里只有 1 天
    被截断时，均值被另外 56 个恒等零摊成 1/57，符号仍然是 `NO_INCREMENT`。
    那个数字是真的（当天两个预测器确实相同），所以不能撤。

    但覆盖度**是分母的上界**：没有对照的那些天 `d_i ≡ 0`，不能算成独立样本。
    所以 `n_eff` 先按 `truncated_days // (lag+1)` 截断，再交给
    `significance_of`——1 天对照最多 1 个独立样本，`FAIL` 退回 `UNKNOWN`。
    这不是新增阈值：`MIN_NEFF=30` 仍是唯一门槛，只是它现在数的是对照天数。

    `significance=UNKNOWN` 时句子必须换掉，不能只在末尾加脚注：`lcb=+0.09`
    配「近端窗口含有远端没有的信息」是一句比证据强的话，而读者读的是句子。

    `raw_n_eff` 是截断**之前**的 NW 读数。`walk_forward` 为了写字段已经截过
    一次，若这里再按字段值算「有没有截断」就永远算不出差别——真实账本上
    那句话会继续把 3 印成估计值（NW 原读数 32）。

    `truncated_trading_days` / `evaluated_trading_days` 是同一件事的日历单位。
    不传时按「每个交易日都发卡」处理（`span == days`），也就是本节之前的口径；
    传了就按真实跨度折算。它们只改变**上限**和**补救句里的天数**，不改变任何
    已观测的数字——所以缺日历不会让读数变乐观，只会让它继续低估发卡稀疏的账本。

    `mde` 是这个样本**能分辨的最小真实改善**（`MIN_INCREMENTAL_EFFECT +
    2.486·se`）。它不参与判定，只进 `NO_INCREMENT` 的句子：不给它，那句
    「没测出增量」就会被读成「没有增量」，而这两个结论之间差着整个分辨率。
    门槛低于分辨率时，`NO_INCREMENT` 是**结构性的**——它会随样本一起变负，
    不会因为攒够天数而翻面（§9.26 的代数：`E[d] = σ_c²-σ_w² → -σ_w² < 0`）。
    """
    if not testable:
        span = ("window 覆盖了全部已可见决策日" if window is None or max_visible is None
                else f"window={window} ≥ 最多 {max_visible} 个已可见决策日")
        days_now = evaluated_days if evaluated_days is not None else max_visible
        return VERDICT_NOT_TESTABLE, (
            f"{span}：没有一天被截断 ⇒ 近端窗口 ≡ 全历史，"
            f"p 与展开基线是同一个估计量，改善恒为 0。"
            f"这是检验没有功效，不是估计值为零——两个估计量吃的是同一批样本；"
            f"{_not_testable_remedy(days_now, hac_lag, trading_days=evaluated_trading_days)}"
        ), SIGNIFICANCE_UNKNOWN
    # 分母不得超过对照天数（见 `_identifiable_sample`）。放在这里而不是只放在
    # `walk_forward`，是因为这个函数是判定本身：调用方把 `n_eff` 和
    # `truncated_days` 一起交进来时，就不该再允许「1 天对照 = 256 个独立样本」。
    raw_n_eff = n_eff if raw_n_eff is None else raw_n_eff
    n_eff = _identifiable_sample(raw_n_eff, truncated_days, hac_lag,
                                 trading_days=truncated_trading_days)
    sig = significance_of(lcb, n_eff, n_eff_reliable)
    tail = _tail(n_eff, n_eff_reliable, truncated_days, evaluated_days, raw_n_eff)
    # 无分母的分支已经完整说过 `n_eff`，这里只补覆盖度——同一句话印两遍，
    # 读者会以为是两个不同的读数。
    cover_only = _tail(None, None, truncated_days, evaluated_days)
    # 未被截断的那些天 `d_i ≡ 0`（`p` 与 `p_clim` 逐位相同），所以 `mean` 是
    # 「有对照的 k 天」和「恒零的 n-k 天」的混合均值。`mean·n/k` **精确**等于
    # 有对照那几天的日均——不是换算估计，是同一个数换了个分母。只印前者，
    # 读者看到的 `+0.00009` 其实是 6 天日均被 43 天摊薄的结果（§9.23）。
    head = f"相对展开基线改善 {mean:+.5f}"
    if truncated_days and evaluated_days and 0 < truncated_days < evaluated_days:
        known = mean * evaluated_days / truncated_days
        head += (f"（{evaluated_days} 天摊薄；只看有对照的 {truncated_days} 天"
                 f"是 {known:+.5f}）")
    head += f"，下界 {lcb:+.5f}"
    # 分辨率必须进句子。`lcb ≤ 0` 只说明「没到门槛」，不说明「没有」：
    # 门槛 0.001 是材料性标准，而 `mde` 是这个样本能看见的最小量级。
    # 两者差 3 倍时，「没测出增量」的真话是「低于 0.0032 的改善测不出」。
    # 它在两个 `NO_INCREMENT` 分支里都要印：样本不够（`UNKNOWN`）时更需要——
    # 那句话既没分母、也没分辨率，读者手上就只剩一个负号。
    need = ("" if mde is None or mde <= 0 else
            f"；要让下界过 {MIN_INCREMENTAL_EFFECT:.3f}，真实改善需 ≥ "
            f"{round(mde, 6):.6f}（门槛的 {mde / MIN_INCREMENTAL_EFFECT:.1f} 倍），"
            f"低于它的增量这个样本分辨不出来")
    if sig == SIGNIFICANCE_UNKNOWN:
        # 三个分支的句子在这里是同构的：只报符号，不报结论。
        # 「近端含有远端没有的信息」和「没测出增量」都需要分母，一个都没有。
        verdict = (VERDICT_NO_INCREMENT if lcb <= 0
                   else VERDICT_MARGINAL if lcb < MIN_INCREMENTAL_EFFECT
                   else VERDICT_INCREMENTAL)
        sign = "≤ 0" if lcb <= 0 else "> 0"
        detail = need if lcb <= 0 else ""
        return verdict, (f"{head} {sign}，但"
                         f"{_no_denominator(n_eff, n_eff_reliable, raw_n_eff)}"
                         f"{cover_only}{detail}"), sig
    if lcb <= 0:
        return VERDICT_NO_INCREMENT, (
            f"{head} ≤ 0：这个 window 没测出增量{tail}{need}"), sig
    if lcb < MIN_INCREMENTAL_EFFECT:
        return VERDICT_MARGINAL, (
            f"{head} > 0 但低于 {MIN_INCREMENTAL_EFFECT:.3f}："
            f"可分辨，不足以改变决策{tail}"), sig
    return VERDICT_INCREMENTAL, (
        f"{head} > 0：近端窗口含有远端没有的信息{tail}"), sig


def _insufficient(total_days: int, oos_days: int, horizon: int | None,
                  reason: str) -> dict[str, Any]:
    """样本不足时的返回值，字段与成功分支**完全一致**，只是数值为 None。

    字段齐全是为了让调用方可以无条件 `update()`，不用分支判断。
    `days` 恒为总决策日、`oos_days` 恒为可评估日——这个区分曾经丢过：
    成功分支返回 `days=len(daily)`、失败分支返回 `days=len(days)`，
    同一个键两种含义，而 `estimate()` 又把 `wf["days"]` 透传成 `oos_days`。
    §9.8 的结论正靠这个字段讲，所以必须拆开。
    """
    return {
        "days": total_days,
        "oos_days": oos_days,
        "n_eff": None,
        "n_eff_reliable": None,
        "n_eff_incremental": None,
        "n_eff_incremental_reliable": None,
        "hac_lag": None,
        "hac_lag_incremental": None,
        "horizon_sessions": horizon,
        "brier": None,
        "brier_const": None,
        "brier_clim": None,
        "oos_skill": None,
        "oos_skill_lcb": None,
        "oos_lcb": None,
        "improvement": None,
        "skill_vs_clim": None,
        "skill_vs_clim_lcb": None,
        "improvement_vs_clim": None,
        "se_incremental": None,
        #: 分辨率：这个样本能分辨的最小真实改善。`None` ⇒ 检验没跑。
        "mde_incremental": None,
        "verdict": VERDICT_INSUFFICIENT,
        "verdict_incremental": VERDICT_INSUFFICIENT,
        #: 与 `verdict_incremental` 一样是「样本不够」，所以同样不是 FAIL。
        "significance_incremental": SIGNIFICANCE_UNKNOWN,
        #: 没有任何可评估日，所以「有没有一天被截断」无从谈起——`None` 不是 `False`。
        "truncated_any": None,
        "truncated_days": None,
        #: 对照日跨了多少个交易日。`None` ⇒ 没有日历，上限按决策日计数。
        "truncated_trading_days": None,
        #: `n_eff_cap_unit` 的取值。没有可评估日时同样无从谈起。
        "n_eff_cap_unit": None,
        "significance": SIGNIFICANCE_UNKNOWN,
        #: 与成功分支同键：样本不足时没有逐日序列可报，`None` 不是空列表。
        "daily_hits": None,
        "daily_picks": None,
        "reason": reason,
        "reason_incremental": None,
    }


def walk_forward(rows: list[dict[str, Any]], *, strategy_id: str | None = None,
                 horizon: int | None = None, basis: str = BASIS_MEDIAN,
                 window: int = DEFAULT_WINDOW, kappa: float = DEFAULT_KAPPA,
                 sessions: Sequence[str] | None = None,
                 with_daily: bool = False) -> dict[str, Any]:
    """把历史按决策日重放，同时回答两个**不同**的问题。

    **问题一（`verdict`，现行语义）**：这个 p 比「无信息常数 0.5」好吗？
    分母是 `(prior - y)²`。

    **问题二（`verdict_incremental`，新增）**：这个 p 比「同样诚实的历史常数」
    好吗？分母是**展开基线**——第 i 天用第 i 天之前全部已可见样本的收缩胜率。
    两者共用同一套可见性、同一个 `kappa`，唯一差别是「看不看远端」。

    为什么必须分开：若 p 接近常数，问题一有闭式解 `skill = 1 - 4p(1-p)`——
    命中率 15% ⇒ +49%，28% ⇒ +19%。这个数字只说明「胜率离 50% 有多远」，
    不说明「近端比远端多知道什么」。而任何**常数**预测器相对展开基线的
    Brier 改善都 ≤ 0（常数的分辨率恒为 0），所以问题二只在 p 真的时变时
    才有非零答案——它检验的正是「时变估计有没有用」。

    **问题二有前置条件**：`window` 必须真的截断过某一天。`window` 覆盖全部
    已可见决策日时，`visible[-window:] ≡ visible`，两个估计量是同一个数，
    改善恒为 0——这不是「没有增量」，是**没有对照**。此时返回
    `verdict_incremental="NOT_TESTABLE"`，且不报 `improvement_vs_clim` /
    `skill_vs_clim` / `skill_vs_clim_lcb`（全为 `None`）。`truncated_any`
    就是这个前置条件的读数。

    第 i 天的 p 只用**在第 i 天已经结算完**的样本估；第 i 天自身的 outcome
    只用于计分。因此两个 skill 都是样本外的。

    **问题二的可辨识性是第三个字段**（`significance_incremental`）。`lcb > 0`
    只说明「这个样本里近端赢了」，不说明「这个赢法可复现」：`n_eff` 只有 2 时
    下界没有分母。三态与第一个检验的 `significance` 同构，见 `_incremental_verdict`。

    这个分母必须在 `truncated_days` 处截断。没有被截断的那一天 `p == p_clim`
    ⇒ `d_i == 0`，而这不是抽样噪声、是窗口配置的恒等式：把 256 个这样的 0
    和 1 个真实对照日一起喂给 NW，方差被稀释到 `se ≈ se_iid`，`n_eff` 就是 256。
    于是「600 天才可判定」和「256 天就 FAIL」同时成立，而先到的是后者。
    截断规则见 `_identifiable_sample`。

    **两个检验共用同一个滞后阶数**（`horizon-1`），但字段仍分开存：两条残差
    序列不同，`n_eff` 会分叉；滞后不会。`window` 制造的是「两个估计量共用样本」，
    它改变 `δ = p_clim - p`，不改变 `y` 的跨日相关，所以不进入 `d_i` 的
    自协方差（推导见下方 `lag_inc` 处）。`horizon=1` 时 `hac_lag=0` 是**正确
    读数**，不是「假定自相关为零」——平稳账本上 r1(d) ≈ 0，实测见
    `scripts/probe_incremental_lag.py`。

    **`sessions` 是交易日历，用来把决策日折成交易日。** 上面那个上限
    `k // (lag+1)` 里，`k` 数决策日、`lag+1` 数交易日，只有每天发卡时两者
    才相等。门禁想要的恰是**稀疏发卡**：每 3 个交易日一张卡时，`k` 个对照日
    跨 `3k` 个交易日，独立样本上限是 `3k/H` 而不是 `k/H`（`scripts/probe_neff_units.py`：
    16 vs 48）。    不传日历时退回旧口径并在 `n_eff_cap_unit` 里写明——**不假装
    两种单位一样**，也不因为缺日历就把读数改小：缺日历只会继续低估稀疏账本。

    **`with_daily=True` 额外返回逐日命中数与 pick 数**（`daily_hits` /
    `daily_picks`，与 `evaluated` 同序）。这两条序列是设计效应唯一的原料：
    `hac_lcb` 估的是**残差**的自相关，而设计效应问的是 `y` 本身的自相关，
    两者不等，且 `E[d_i]` 的主导项是 `δ_i·(p*−y_i)`，所以 `y` 的 DEFF 是
    `d` 的 DEFF 的近似上界。默认关闭：一张卡存 300–900 个整数会撑大契约，
    而 `estimate()` 不透传（也没有任何下游读它）。离线诊断的入口是
    `scripts/probe_real_deff.py`。
    """
    if basis not in BASIS_CHOICES:
        raise ValueError(f"basis 必须是 {BASIS_CHOICES} 之一，收到 {basis!r}")
    field = _field(basis)
    prior = _prior_of(basis)
    horizon = resolve_horizon(rows, strategy_id=strategy_id, horizon=horizon, basis=basis)
    selected = _select(rows, strategy_id=strategy_id, horizon=horizon, basis=basis)
    days = _by_day(selected, field)
    if len(days) < MIN_DAYS + 1:
        return _insufficient(
            len(days), 0, horizon,
            f"决策日 {len(days)} 不足（至少需要 {MIN_DAYS + 1} 天才能留出样本外）")
    # 日历只用来换算跨度，不参与任何可见性判定：`_visible` 仍然用账本里
    # 真实的 `exit.date`。排序去重一次，后面每个跨度查询都是二分。
    calendar = sorted(set(sessions)) if sessions else None

    daily: list[float] = []
    daily_clim: list[float] = []
    brier_all: list[float] = []
    brier_const_all: list[float] = []
    brier_clim_all: list[float] = []
    usable = int(horizon or 0)
    #: 是否至少有一个被评估日的可见样本被窗口截断。它决定第二个检验
    #: **能不能做**：一天都没截断时 `kept == visible`，两个估计量恒等。
    truncated_any = False
    #: 有多少个被评估日真的被截断。`truncated_any` 只说「能不能做」，
    #: 这个数说「做了几天」——它是唯一能衡量结论分量的量，因为没被截断的
    #: 那些天逐日改善恒为 0，会把均值摊薄到 1/n。
    truncated_days = 0
    #: 单个被评估日能看到的已结算决策日上限——`NOT_TESTABLE` 的解释要用它。
    max_visible = 0
    #: 被截断的那段对照日在日历上跨了几个交易日。`None` ⇒ 没有日历可用，
    #: 上限只能按决策日计数（旧口径），由 `n_eff_cap_unit` 标注。
    truncated_trading_days: int | None = None
    first_trunc: str | None = None
    last_trunc: str | None = None
    #: 被评估的决策日（升序）。补救句要用它算「每几个交易日一张卡」。
    evaluated: list[str] = []
    #: 逐日命中数与 pick 数（与 `evaluated` 一一对应）。只在 `with_daily=True`
    #: 时收集——它是设计效应唯一的原料，但默认不进返回值：一张卡片存 300–900
    #: 个整数会把契约撑大，而绝大多数调用方只读结论。
    day_hits: list[int] = []
    day_picks: list[int] = []
    for i in range(MIN_DAYS, len(days)):
        today = days[i][0]
        visible: list[int] = []     # 已结算、可用的决策日（按时间升序）
        past_all: list[int] = []    # 远端：全部已可见
        for j in range(i):
            _, outs_j, exit_j = days[j]
            if not _visible(exit_j, today, i - j, usable):
                continue
            visible.append(j)
            past_all.extend(outs_j)
        if not visible:
            continue
        # 先记录「这一天到底截断没有」，再取窗口。`window <= 0` 表示不限窗口，
        # 等价于永远不截断 ⇒ 永远不可判定，所以它不置位。
        max_visible = max(max_visible, len(visible))
        if window > 0 and len(visible) > window:
            truncated_any = True
            truncated_days += 1
            if first_trunc is None:
                first_trunc = today
            last_trunc = today
        # `window` 数的是**已结算的决策日**，不是日历上的决策日。
        # 按 `j >= i - window` 数会把最近 horizon+1 个尚未结算的日子算进窗口，
        # 于是 window=60 实际只剩约 39 天的样本，而 `estimate()` 的 window=60
        # 拿到的是 60 天。同一个参数两种含义 ⇒ walk_forward 验证的不是
        # estimate 输出的那个预测器；window ≤ horizon 时甚至一天都评估不出来。
        kept = visible[-window:] if window > 0 else visible
        past = [y for j in kept for y in days[j][1]]
        if not past:
            continue
        p = _shrink(sum(past), len(past), prior, kappa)
        p_clim = _shrink(sum(past_all), len(past_all), prior, kappa)
        outs = days[i][1]
        b = sum((p - y) ** 2 for y in outs) / len(outs)
        bc = sum((prior - y) ** 2 for y in outs) / len(outs)
        bclim = sum((p_clim - y) ** 2 for y in outs) / len(outs)
        brier_all.append(b)
        brier_const_all.append(bc)
        brier_clim_all.append(bclim)
        daily.append(bc - b)
        daily_clim.append(bclim - b)
        evaluated.append(today)
        if with_daily:
            day_hits.append(sum(outs))
            day_picks.append(len(outs))

    if len(daily) < MIN_DAYS:
        return _insufficient(
            len(days), len(daily), horizon,
            f"可用于样本外评估的决策日 {len(daily)} 不足（已结算的样本还不够）")

    # 决策日 → 交易日的换算。两个跨度都必须由同一份日历算出来，否则
    # 「上限」和「补救句」会用两种单位回答同一个问题（§9.24）。
    if calendar:
        if first_trunc is not None and last_trunc is not None:
            truncated_trading_days = _trading_span(
                calendar, first_trunc, last_trunc, truncated_days)
        evaluated_trading_days = _trading_span(
            calendar, evaluated[0], evaluated[-1], len(evaluated))
    else:
        evaluated_trading_days = None

    # 逐日改善量是 MA(horizon-1)：持有期让相邻决策日的结果重叠。
    #
    # 两条序列各算各的 `n_eff`，不能共用一个：`daily` 的残差是
    # `(prior-y)² - (p-y)²`，`daily_clim` 是 `(p_clim-y)² - (p-y)²`。
    # 自协方差不同 ⇒ `se` 不同 ⇒ `n_eff` 不同。同一个键装两个数，
    # 就是 §9.8 那个「一个键两种含义」的翻版，只是换了个字段名。
    lag = max(0, usable - 1)
    # 增量序列的滞后阶数**不**由 `window` 决定。这一点被写错过一次（§9.18），
    # 因此把理由留在这里。
    #
    # 直觉上，第 i 天的 p 用 `[i-w, i-1]`、第 i+1 天用 `[i-w+1, i]`，共用 w-1 天，
    # 似乎该取 `lag = w-1`。但逐日改善是
    #
    #     d_i = (p_clim,i - y_i)² - (p_i - y_i)² = δ_i · (p_clim,i + p_i - 2y_i)
    #     δ_i ≡ p_clim,i - p_i
    #
    # `δ_i` 确实随窗口缓慢漂移，可它乘的那一项以 `y_i` 为主，而 `y_i` 与
    # `y_{i+1}` 只通过**持有期**相关（`horizon-1`）。估计量共用样本只改变 `δ`，
    # 不改变 `y` 的跨日相关，所以 `Cov(d_i, d_{i+k})` 在 `k > horizon-1` 处消失：
    #
    #     Cov(d_i, d_{i+k}) ≈ E[δ_i δ_{i+k}] · E[z_i z_{i+k}],  z_i = p_clim+p_i-2y_i
    #                         └── 缓慢但有限 ──┘  └── k>H-1 时为 0 ──┘
    #
    # 实测（`scripts/probe_incremental_lag.py`）：平稳 iid 账本、`window=40`、
    # n=57 时 r1(d) = -0.02；同一 window 的确定性账本（每天恰好 18/20 命中，
    # 没有抽样噪声）r1(d) = +0.98——那个 0.98 来自「日命中数是常数、只剩 δ 在
    # 漂移」，不是零假设下的性质。§9.18 把这份确定性账本的自相关当成了零假设的
    # 自相关，于是把 `lag` 拉到 `w-1`：n_eff 从 57 掉到 1，检验被关掉，30 个
    # window「翻转」成 NO_INCREMENT。那不是数据发现，是功效消失。
    #
    # 正确性检验不是看 r1，而是看 `se` 能不能对上 mean(d) 在重复实验里的真实
    # 波动。同一探针的蒙特卡洛：H=1/w=40/n=57 时真实 sd 0.00051，`lag=0` 给
    # 0.00048、`lag=w-1` 给 0.00033（低估 35%）；H=20 时 `lag=19` 给 0.0237、
    # `lag=39` 给 0.0225，真实 0.0265——`w-1` 从未更接近。所以增量序列与
    # 水平序列用同一个结构性滞后：`horizon-1`。
    lag_inc = lag
    mean, _, lcb, n_eff, n_eff_reliable = _mean_se_lcb(daily, lag)
    mean_clim, se_clim, lcb_clim, n_eff_inc, n_eff_inc_reliable = _mean_se_lcb(
        daily_clim, lag_inc)
    # 分辨率：这个样本能分辨的最小真实改善（见 `_MDE_Z`）。`se` 只在成功分支
    # 有值，所以它跟着 `se_clim` 一起被撤。
    mde_inc = (None if se_clim is None
               else MIN_INCREMENTAL_EFFECT + _MDE_Z * se_clim)
    # `daily_clim` 里没被截断的那些天恒为 0，NW 会把它们当独立观测 ⇒
    # `n_eff` 随「没比过的天数」一起长。截到对照天数（按 MA(H−1) 折算），
    # 否则 1 个对照日也能拿到 256 个等效样本和一个 FAIL 标签。
    #: 截断前的 NW 读数。字段存截断值（下游按「至多这么多」读），句子两个都要：
    #: 真实账本 `window=250` 上它是 32，字段是 3，只印 3 就是在说「估出来是 3」。
    n_eff_inc_raw = n_eff_inc
    n_eff_inc = _identifiable_sample(n_eff_inc, truncated_days, lag_inc,
                                     trading_days=truncated_trading_days)
    brier = sum(brier_all) / len(brier_all)
    brier_const = sum(brier_const_all) / len(brier_const_all)
    brier_clim = sum(brier_clim_all) / len(brier_clim_all)
    skill = (1 - brier / brier_const) if brier_const > 0 else None
    skill_lcb = (lcb / brier_const) if brier_const > 0 else None
    skill_clim = (1 - brier / brier_clim) if brier_clim > 0 else None
    skill_clim_lcb = (lcb_clim / brier_clim) if brier_clim > 0 else None

    # `n_eff_reliable=False` ⇒ `n_eff` 是 `n // (lag+1)`。把构造值写成估计值，
    # 是本节修掉的同一个毛病，只是换到第一个检验上——两处用同一种标注。
    neff_note = "" if n_eff_reliable else "，NW 不可用，取不重叠窗口数"
    if lcb > 0:
        verdict = VERDICT_RELIABLE
        reason = (f"样本外 Brier 改善 {mean:+.5f}，单侧 95% 下界 {lcb:+.5f} > 0"
                  f"（{len(daily)} 个可评估决策日，等效独立样本 {n_eff}{neff_note}）")
    else:
        verdict = VERDICT_UNRELIABLE
        reason = (f"样本外 Brier 改善 {mean:+.5f}，单侧 95% 下界 {lcb:+.5f} ≤ 0"
                  f"（{len(daily)} 个可评估决策日，等效独立样本 {n_eff}{neff_note}）")

    verdict_inc, reason_inc, sig_inc = _incremental_verdict(
        mean_clim, lcb_clim, n_eff_inc,
        testable=truncated_any, window=window, max_visible=max_visible,
        truncated_days=truncated_days, evaluated_days=len(daily),
        n_eff_reliable=n_eff_inc_reliable,
        # 只在 `NOT_TESTABLE` 的补救句里用：要判断「缩 window 能不能解开这个
        # 状态」，必须知道 MA 的长度。它和判定本身无关，所以放在最后。
        hac_lag=lag_inc,
        # 截断前的读数：句子要说清「3」是折算值而不是估计值。
        raw_n_eff=n_eff_inc_raw,
        # 同一个上限的另一种单位。没有日历就保持 `None`，由 `_identifiable_sample`
        # 退回决策日计数——它不会悄悄用更小的上限，只会在结果里标注口径。
        truncated_trading_days=truncated_trading_days,
        evaluated_trading_days=evaluated_trading_days,
        # 只进 `NO_INCREMENT` 的句子：让「没测出」自带分辨率。
        mde=mde_inc)
    if not truncated_any:
        # 两个估计量吃的是同一批样本 ⇒ 下面这些都是恒等式的产物，不是估计：
        # 改善恒 0、skill 恒 0、下界恒 0、`n_eff` 是零序列上的伪读数。
        # 报出去就会被读成「近端没有增量」。状态已经写在 `verdict_incremental`
        # 里，这里必须把数字撤掉，否则读者只会看数字。
        mean_clim = se_clim = lcb_clim = skill_clim = skill_clim_lcb = None
        n_eff_inc = n_eff_inc_reliable = None
        # 分辨率是 `se` 的函数，`se` 撤了它就跟着撤——否则卡上会留一个
        # 「这个检验能看见 0.0042」的数，而那个检验并没有跑。
        mde_inc = None
        # 数字撤掉了，可辨识性也必须是「没测」：这一行是冗余的（`testable=False`
        # 已经返回 UNKNOWN），写出来是为了让「撤数字」与「撤状态」在同一个
        # 分支里看得见——分开写的地方，迟早只改一半。
        sig_inc = SIGNIFICANCE_UNKNOWN

    return {
        #: 总决策日。恒为「账本里有几个决策日」，与 `oos_days` 严格区分。
        "days": len(days),
        #: 可评估决策日（结果已结算、能进入样本外计分的天数）。
        "oos_days": len(daily),
        #: 等效独立样本数，**对应 `verdict` 那条序列**（相对常数先验）。
        #: `oos_days / n_eff` 就是持有期重叠带来的信息损失，实测 323 个可评估
        #: 日 / 20 日持有期约为 44–49——这才是判断「够不够」的分母。
        "n_eff": n_eff,
        #: `False` ⇒ `n_eff` 是构造值（不重叠窗口数），不是 NW 估计值。
        "n_eff_reliable": n_eff_reliable,
        #: 增量检验那条序列的 `n_eff`。与上面的 `n_eff` **不是同一个数**：
        #: 两条残差序列的自协方差不同。账本越过 `3·(lag+1)` 之后就会分叉，
        #: 所以现在就分开存。`None` ⇒ 检验没做（`NOT_TESTABLE`），不是「等于 1」。
        #: **已按对照天数截断**：没被截断的日子 `d_i ≡ 0`，它们会把 NW 的
        #: `n_eff` 抬到总天数（1 天对照 ⇒ 256）。上限是
        #: `truncated_days // (lag+1)`，所以这个数读作「至多这么多独立样本」。
        "n_eff_incremental": n_eff_inc,
        "n_eff_incremental_reliable": n_eff_inc_reliable,
        #: Newey-West 的滞后阶数 = horizon - 1。
        "hac_lag": lag,
        #: 增量检验那条序列的滞后阶数。**结构上与 `hac_lag` 相等**，字段仍然
        #: 分开，是因为下游按「每条序列各有自己的滞后」读过它：把两者合并会让
        #: 一次错误的滞后修正无处可标。§9.18 曾让它取 `window-1`，§9.20 撤回。
        "hac_lag_incremental": lag_inc,
        "brier": round(brier, 6),
        "brier_const": round(brier_const, 6),
        "brier_clim": round(brier_clim, 6),
        "oos_skill": None if skill is None else round(skill, 4),
        "oos_skill_lcb": None if skill_lcb is None else round(skill_lcb, 4),
        "oos_lcb": round(lcb, 6),
        "improvement": round(mean, 6),
        "skill_vs_clim": None if skill_clim is None else round(skill_clim, 4),
        "skill_vs_clim_lcb": None if skill_clim_lcb is None else round(skill_clim_lcb, 4),
        "improvement_vs_clim": None if mean_clim is None else round(mean_clim, 6),
        #: `improvement_vs_clim` 的 HAC 标准误（lag = horizon-1）。**没有这个数，
        #: 「离判定还差多远」就只能从 `skill_vs_clim_lcb` 反解**——那是 4 位小数
        #: 的比值，真实账本上它落在 ±0.0004 格子里，反解出的 se 误差可达 25%。
        #: `verdict_incremental` 说「符号」、`significance_incremental` 说
        #: 「分母够不够」，这个数说「再攒多少日历才够」：检测门限
        #: `= z·se/√(B)`，B 是预算内的独立块数。
        "se_incremental": None if se_clim is None else round(se_clim, 8),
        #: 这个样本**能分辨的最小真实改善**：`MIN_INCREMENTAL_EFFECT + 2.486·se`。
        #: 它不是判据，是分辨率——`NO_INCREMENT` 的句子会印它，读者据此区分
        #: 「低于 0.0032 的改善测不出」与「没有改善」。`None` ⇒ 检验没跑
        #: （`NOT_TESTABLE` / `INSUFFICIENT`），不是「分辨率等于门槛」。
        #:
        #: 读法上有一个陷阱：它用的是**当前观测到的** `se`，而 `d_i` 的方差随
        #: 效应本身增长（`d_i = δ_i(δ_i + 2(p*-y))`，`δ_i` 越大噪声越大）。所以
        #: 真有大增量时 `se` 会涨，`mde` 可能反而大于 `improvement_vs_clim`——
        #: 两个数不在同一个假设下（一个在观测点，一个在零假设邻域），不能相减
        #: 当「离判定还差多少」。
        "mde_incremental": None if mde_inc is None else round(mde_inc, 6),
        "verdict": verdict,
        "verdict_incremental": verdict_inc,
        #: 第二个检验的**可辨识性**，与 `significance` 同构：PASS / FAIL / UNKNOWN。
        #: `verdict_incremental` 读符号与量级，这个字段读「那个下界有没有分母」。
        #: 二者正交：`INCREMENTAL × UNKNOWN` = 改善过门槛、但有效样本不足 30，
        #: 只能读作不可判定（§9.21）。`NOT_TESTABLE` 时恒为 UNKNOWN——检验没做，
        #: 不是「没通过」。
        "significance_incremental": sig_inc,
        #: 第二个检验有没有做过。`False` ⇒ 窗口覆盖了全部已可见日，两个估计量
        #: 恒等，`verdict_incremental` 只能是 `NOT_TESTABLE`，上面的数字全为 `None`。
        #: `None` ⇒ 一个可评估日都没有（`INSUFFICIENT`），问题还没轮到问。
        "truncated_any": truncated_any,
        #: 真的被截断的被评估日数。`truncated_any=True` 只保证 ≥ 1 天；这个数
        #: 才是结论的分量——`truncated_days / oos_days` 越小，`improvement_vs_clim`
        #: 被恒等零摊得越薄。它不 gate，只进结论句。
        "truncated_days": truncated_days,
        #: 这段对照日在日历上跨了几个交易日。与 `truncated_days` 一起读才知道
        #: 发卡有多稀疏：`truncated_trading_days / truncated_days` 就是每张卡
        #: 占几个交易日。`None` ⇒ 没传日历。
        "truncated_trading_days": truncated_trading_days,
        #: `n_eff_incremental` 的上限用哪种单位算的。`"trading_days"` ⇒ 传了
        #: 日历、按真实跨度折算；`"decision_days"` ⇒ 退回旧口径，**只在每张卡
        #: 占一个交易日时才对**。读到 `decision_days` 就意味着这个分母可能低估。
        #: 一天都没截断时没有上限可言，所以是 `None`——「没算」不是「按决策日算」。
        "n_eff_cap_unit": (None if truncated_days == 0 else
                           "trading_days" if truncated_trading_days is not None
                           else "decision_days"),
        #: 三态结论。`verdict` 只回答「lcb 符号」，这个字段还回答「这个 lcb
        #: 有没有分母」。两者都保留：前者是既有契约，后者是唯一能当结论读的。
        "significance": significance_of(skill_lcb, n_eff, n_eff_reliable),
        #: 逐日命中数与 pick 数（与 `evaluated` 同日序）。`with_daily=False` 时
        #: 为 `None`——不是空列表：空列表会被读成「一天都没评估」。
        #:
        #: 这两个序列是设计效应的唯一原料。`hac_lcb` 只在**残差**上估自相关，
        #: 而设计效应要的是 `y` 本身的自相关；两者不相等，且 `d_i` 的主导项
        #: 是 `δ_i·(p*−y_i)`，所以 `y` 的 DEFF 是 `d` 的 DEFF 的近似上界。
        #: 有了它就能把「600 条 pick 等于几个独立观测」从假设变成读数
        #: （`scripts/probe_real_deff.py` 在离线缓存上做的就是这件事）。
        "daily_hits": day_hits if with_daily else None,
        "daily_picks": day_picks if with_daily else None,
        "reason": reason,
        "reason_incremental": reason_inc,
    }


def estimate(rows: list[dict[str, Any]], *, strategy_id: str | None = None,
             horizon: int | None = None, basis: str = BASIS_MEDIAN,
             window: int = DEFAULT_WINDOW, kappa: float = DEFAULT_KAPPA,
             sessions: Sequence[str] | None = None,
             with_walk_forward: bool = True) -> dict[str, Any]:
    """估一个可用于建卡的 p_win，并把它的出处一并返回。

    返回值里的 `reliable` 是**元校准**结论：不是「这个数字算得准不准」，
    而是「用这类历史胜率预测未来，在样本外有没有预测力」。
    """
    if basis not in BASIS_CHOICES:
        raise ValueError(f"basis 必须是 {BASIS_CHOICES} 之一，收到 {basis!r}")
    field = _field(basis)
    prior = _prior_of(basis)
    horizon = resolve_horizon(rows, strategy_id=strategy_id, horizon=horizon, basis=basis)
    selected = _select(rows, strategy_id=strategy_id, horizon=horizon, basis=basis)

    # 只取最近 window 个决策日，与 walk_forward 的口径一致
    days = _by_day(selected, field)
    kept = days[-window:] if window > 0 else days
    kept_dates = {d for d, _, _ in kept}
    recent = [r for r in selected if str(r.get("as_of")) in kept_dates]

    n = len(recent)
    hits = sum(int(r[field]) for r in recent)
    base = {
        "basis": basis,
        "strategy_id": strategy_id,
        "horizon_sessions": horizon,
        "n": n,
        "hits": hits,
        "hit_rate": round(hits / n, 6) if n else None,
        "days": len(kept),
        "window": window,
        "kappa": kappa,
        "prior": prior,
        #: p_win 里先验的比重。两个分母各算一次：`n` 是观测数，`n_eff` 是
        #: 等效独立样本数。账本 386 条 / `n_eff=1` 时是 4.9% 对 95.2%——
        #: 差距就是「386 条观测不等于 386 个样本」的全部内容。
        "prior_weight": None,
        "prior_weight_effective": None,
        # walk-forward 字段：`with_walk_forward=False` 时保持 None，跑时被覆盖。
        # 预置在这里是为了让返回结构恒定，调用方不必区分两种形状。
        "oos_days": None,
        "n_eff": None,
        "n_eff_reliable": None,
        "n_eff_incremental": None,
        "n_eff_incremental_reliable": None,
        "hac_lag": None,
        "hac_lag_incremental": None,
        "oos_skill": None,
        "oos_skill_lcb": None,
        "oos_improvement": None,
        "brier": None,
        "brier_const": None,
        "brier_clim": None,
        "skill_vs_clim": None,
        "skill_vs_clim_lcb": None,
        "oos_improvement_vs_clim": None,
        "verdict_incremental": None,
        "reason_incremental": None,
        #: 分辨率。冷启动 / 未跑 walk-forward 时为 `None`——「没测」不是
        #: 「分辨率等于门槛」。缺这个键会让 `estimate()` 的两次返回形状不同，
        #: 而调用方是按成功分支的形状写的。
        "mde_incremental": None,
        #: 第二个检验的可辨识性。冷启动 / 未跑 walk-forward 时是 UNKNOWN——
        #: 「没测」既不是 PASS，也不是 FAIL。
        "significance_incremental": SIGNIFICANCE_UNKNOWN,
        #: 第二个检验有没有做过（`False` ⇒ 窗口覆盖全历史，结论是 NOT_TESTABLE）。
        #: 冷启动 / 未跑 walk-forward 时为 None：不是「没截断」，是「没测」。
        "truncated_any": None,
        #: 真的被截断的被评估日数。与 `oos_days` 一起读：`truncated_days/oos_days`
        #: 就是 `improvement_vs_clim` 里有多少天在承载证据。
        "truncated_days": None,
        #: 对照日跨了几个交易日，以及上限用的是哪种单位。没有日历时为
        #: `None` / `None`——「没算」不是「等于 0」。
        "truncated_trading_days": None,
        "n_eff_cap_unit": None,
        #: 三态结论。冷启动 / 未跑 walk-forward 时恒为 UNKNOWN——「没测」不是「没通过」。
        "significance": SIGNIFICANCE_UNKNOWN,
    }

    if n < MIN_N:
        return {
            **base,
            "p_win": None,
            "source": SOURCE_PRIOR,
            "reliable": False,
            "verdict": VERDICT_INSUFFICIENT,
            # 没有 p_win 就不报「先验占多少」：权重的对象是那个数字，不是账本。
            # `suggest(allow_prior=True)` 真的把先验填进去时再置 1.0。
            "reason": f"已结算样本 {n} < {MIN_N}：没有历史，p_win 只能用常数基线 {prior:.2f}",
        }

    p_win = _shrink(hits, n, prior, kappa)
    w, _ = prior_weights(n, None, kappa, source=SOURCE_LEDGER)
    result = {
        **base,
        "p_win": round(p_win, 6),
        "source": SOURCE_LEDGER,
        "reliable": False,
        "verdict": VERDICT_INSUFFICIENT,
        "prior_weight": w,
        "reason": f"样本 {n}（{len(kept)} 个决策日）",
    }
    if not with_walk_forward:
        return result

    wf = walk_forward(rows, strategy_id=strategy_id, horizon=horizon,
                      basis=basis, window=window, kappa=kappa, sessions=sessions)
    # 有效权重只能用 walk_forward 的 `n_eff` 当分母代理：那是**计分序列**的等效
    # 独立样本数，不是收缩本身的自由度。两者在 MA(H−1) 下同阶（都约 n/H），
    # 所以拿它当代理是保守的；但它随 `window` 变，不是账本的固有属性。
    _, w_eff = prior_weights(n, wf.get("n_eff"), kappa, source=SOURCE_LEDGER)
    result.update({
        # 可评估日 ≠ 账本规模。这里曾经透传 `wf["days"]`，而它在成功分支
        # 是「可评估日」、在 INSUFFICIENT 分支是「总决策日」——同一个键两种含义。
        "oos_days": wf.get("oos_days"),
        "n_eff": wf.get("n_eff"),
        "n_eff_reliable": wf.get("n_eff_reliable"),
        "n_eff_incremental": wf.get("n_eff_incremental"),
        "n_eff_incremental_reliable": wf.get("n_eff_incremental_reliable"),
        "hac_lag": wf.get("hac_lag"),
        "hac_lag_incremental": wf.get("hac_lag_incremental"),
        "oos_skill": wf.get("oos_skill"),
        "oos_skill_lcb": wf.get("oos_skill_lcb"),
        "oos_improvement": wf.get("improvement"),
        "brier": wf.get("brier"),
        "brier_const": wf.get("brier_const"),
        "brier_clim": wf.get("brier_clim"),
        "skill_vs_clim": wf.get("skill_vs_clim"),
        "skill_vs_clim_lcb": wf.get("skill_vs_clim_lcb"),
        "oos_improvement_vs_clim": wf.get("improvement_vs_clim"),
        # 分辨率：这个样本能分辨的最小真实改善。与 `verdict_incremental` 一起读——
        # 那个结论说的是「没到门槛」，这个数说「门槛之下还有多宽一段看不见」。
        "mde_incremental": wf.get("mde_incremental"),
        "verdict_incremental": wf.get("verdict_incremental"),
        "reason_incremental": wf.get("reason_incremental"),
        # 第二个检验可辨识性的三态。与 `verdict_incremental` 一起读：
        # 状态说结论，这个字段说「那个下界有没有分母」（§9.21）。
        "significance_incremental": wf.get("significance_incremental"),
        # 第二个检验可不可做。与 `verdict_incremental` 一起读：状态说结论，
        # 这个标志说原因（窗口覆盖全历史 ⇒ 两个估计量恒等）。
        "truncated_any": wf.get("truncated_any"),
        # 做了几天。`truncated_any=True` 也可能是 1 天——那个 `improvement_vs_clim`
        # 就是 1 天的效应除以 n，不读这个数会把它当成 n 天的结论。
        "truncated_days": wf.get("truncated_days"),
        # 天数与交易日跨度一起进结论句：`truncated_days` 是分母，交易日跨度
        # 决定上限。没有日历（`n_eff_cap_unit="decision_days"`）时上限可能低估。
        "truncated_trading_days": wf.get("truncated_trading_days"),
        "n_eff_cap_unit": wf.get("n_eff_cap_unit"),
        # 有效权重：`n_eff=None`（样本不够）时保持 None，不假装知道。
        "prior_weight_effective": w_eff,
        "reliable": wf["verdict"] == VERDICT_RELIABLE,
        "verdict": wf["verdict"],
        # 结论三态。`reliable` 保留原语义（lcb > 0），它是二态契约；
        # 要区分「测过了没有」和「测不了」，读这个字段。
        "significance": wf.get("significance", SIGNIFICANCE_UNKNOWN),
        "reason": f"样本 {n}（{len(kept)} 个决策日）；{wf['reason']}",
    })
    return result


def suggest(rows: list[dict[str, Any]], *, strategy_id: str | None = None,
            horizon: int | None = None, basis: str = BASIS_MEDIAN,
            window: int = DEFAULT_WINDOW, kappa: float = DEFAULT_KAPPA,
            sessions: Sequence[str] | None = None,
            allow_prior: bool = False) -> dict[str, Any]:
    """建卡前的 p_win 决策：能不能用、用多少。

    gate 的对象是**有没有证据**，不是**信不信这个证据**：

    - `allow_prior=False`（默认）：账本里没有可结算样本（`n < MIN_N`）就抛
      ValueError。此时 p_win 只能等于常数先验，放行等于用 prior 冒充证据。
    - `allow_prior=True`：冷启动也建卡，`source="prior"`，出处随卡封存；
      结算时 `by_source` 会把它和账本证据分开计分。

    `reliable=False` **不再抛错**。它曾经是 gate，而 gate 的输入（`reliable`）
    由被 gate 的行为（建卡 → 结算）算出：不出卡就没有结算，没有结算就永远
    unreliable——永久 409。现在它只是标签：`verdict="UNRELIABLE"`、
    `significance="UNKNOWN"` 写进出处并进哈希。见 `docs/decision-ledger.md` §9.15。
    """
    est = estimate(rows, strategy_id=strategy_id, horizon=horizon, basis=basis,
                   window=window, kappa=kappa, sessions=sessions)
    if est["p_win"] is None:
        if not allow_prior:
            raise ValueError(
                f"策略 {strategy_id or '(手工卡)'} 没有可用的历史结算样本"
                f"（n={est['n']} < {MIN_N}）：只有先验，没有证据。"
                f"先补登历史卡并结算，或显式传 allow_prior=true "
                f"用常数基线 {est['prior']:.2f} 建卡（卡上记 source=prior）。")
        est["p_win"] = est["prior"]
        est["source"] = SOURCE_PRIOR
        # 填进去的就是先验本身：两个口径的权重都是 1.0，不是 None、也不是 0。
        est["prior_weight"], est["prior_weight_effective"] = prior_weights(
            0, None, kappa, source=SOURCE_PRIOR)
        return est
    return est
