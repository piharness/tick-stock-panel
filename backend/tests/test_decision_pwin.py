"""p_win 来源与计分口径测试。

三条主张，逐条钉住：
1. p_win 只能来自已结算的卡（冷启动回退常数基线，不用未来信息）；
2. 历史胜率有没有预测力，要用**样本外重放**判断，而不是看样本内 skill；
3. `median` 口径的常数基线是精确 0.5，且同日多个 pick 只算一个自由度。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.decision import pwin, settle

STRAT = "momentum_20"


def _row(as_of: str, outcome: int, *, p_win: float = 0.5,
         strategy: str | None = STRAT, horizon: int = 20,
         ret: float = 0.01) -> dict:
    return {
        "as_of": as_of,
        "strategy_id": strategy,
        "horizon_sessions": horizon,
        "p_win": p_win,
        "outcome": outcome,
        "outcome_med": outcome,
        "excess": ret,
        "excess_med": ret,
        "fill": {"status": "FILLED"},
        "exit": {"ret": ret},
        "logloss": 0.69,
    }


def _day(day: str, wins: int, total: int = 20, **kw) -> list[dict]:
    return [_row(day, 1 if i < wins else 0, **kw) for i in range(total)]


def _series(n_days: int, wins_per_day: int, *, total: int = 20,
            start: int = 1, **kw) -> list[dict]:
    return [r for i in range(n_days)
            for r in _day(f"2026-01-{start + i:02d}", wins_per_day, total, **kw)]


# ---------------------------------------------------------------------------
# 冷启动与收缩
# ---------------------------------------------------------------------------


def test_empty_ledger_returns_insufficient():
    est = pwin.estimate([])
    assert est["p_win"] is None
    assert est["source"] == pwin.SOURCE_PRIOR
    assert est["reliable"] is False
    assert est["verdict"] == pwin.VERDICT_INSUFFICIENT


def test_shrinkage_pulls_toward_prior():
    """8 个样本全胜，收缩后必须远低于 1.0——小样本的 100% 不是 100%。"""
    rows = [_row(f"2026-01-{i + 1:02d}", 1) for i in range(8)]
    est = pwin.estimate(rows, window=10, kappa=20.0, with_walk_forward=False)
    assert est["n"] == 8
    assert est["hit_rate"] == 1.0
    # (8 + 20*0.5) / (8 + 20) = 18/28
    assert est["p_win"] == pytest.approx(18 / 28, abs=1e-6)
    assert est["source"] == pwin.SOURCE_LEDGER


def test_no_p_win_below_min_n():
    """低于 MIN_N 时不给数字，也不假装有先验之外的证据。"""
    rows = [_row("2026-01-01", 1), _row("2026-01-02", 1), _row("2026-01-03", 1)]
    est = pwin.estimate(rows, window=10, with_walk_forward=False)
    assert est["n"] == 3
    assert est["p_win"] is None
    assert est["verdict"] == pwin.VERDICT_INSUFFICIENT


def test_shrinkage_converges_with_many_samples():
    """样本足够大时，收缩几乎不起作用。"""
    rows = _series(60, 14)          # 每天 14/20 = 70%
    est = pwin.estimate(rows, window=100, kappa=20.0, with_walk_forward=False)
    assert est["n"] == 1200
    assert est["hit_rate"] == pytest.approx(0.7, abs=1e-9)
    assert est["p_win"] == pytest.approx(0.7, abs=0.01)


def test_filters_by_strategy_and_horizon():
    rows = (_series(3, 10) + _series(3, 2, start=10, strategy="other")
            + _series(3, 10, start=15, horizon=5))
    est = pwin.estimate(rows, strategy_id=STRAT, horizon=20,
                        window=100, with_walk_forward=False)
    assert est["n"] == 60
    assert est["hits"] == 30


def test_resolve_horizon_picks_dominant_group():
    rows = _series(10, 10) + _series(2, 10, start=15, horizon=5)
    assert pwin.resolve_horizon(rows, strategy_id=STRAT, basis="median") == 20
    assert pwin.resolve_horizon(rows, strategy_id=STRAT, horizon=5,
                                basis="median") == 5


def test_estimate_does_not_mix_horizons():
    """5 日与 20 日的「跑赢中位数」是两件事，混起来会得到第三个数字。"""
    a = _series(10, 20, horizon=20)              # 100% 胜
    b = _series(10, 0, start=15, horizon=5)      # 0% 胜
    est = pwin.estimate(a + b, strategy_id=STRAT, basis="median",
                        with_walk_forward=False)
    assert est["horizon_sessions"] == 20
    assert est["hit_rate"] == 1.0
    assert est["n"] == 200


# ---------------------------------------------------------------------------
# 样本外重放：这个来源靠不靠谱
# ---------------------------------------------------------------------------


def test_walk_forward_reliable_when_signal_is_stable():
    """每天稳定 70% 胜率 ⇒ 历史胜率对未来有预测力。"""
    rows = _series(40, 14)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["verdict"] == pwin.VERDICT_RELIABLE, wf["reason"]
    assert wf["oos_lcb"] > 0
    assert wf["oos_skill"] > 0


def test_walk_forward_unreliable_when_regime_flips():
    """前 20 天全胜、后 20 天全负 ⇒ 历史胜率会把预测带反。

    这是实测中 20 日动量策略的真实处境：训练段 +4.45%，验证段 -4.19%。
    """
    rows = _series(20, 20) + _series(20, 0, start=21)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["verdict"] == pwin.VERDICT_UNRELIABLE, wf["reason"]
    assert wf["oos_lcb"] <= 0


def test_walk_forward_does_not_see_the_regime_it_is_evaluating():
    """制度翻转后，评估用的 p 必须仍停在旧制度上。

    60 天 90% 胜率 → 39 天 10%。honest 重放在翻转后的 20 天里 p 仍在 0.6 以上
    （那 20 天的结果还没结算），所以 Brier 改善必然大幅为负（≈ -0.12）。
    若按决策日回看，p 会在 20 天内跟上新制度，改善被抹平到 ≈ 0 —— 这就是
    「用未来信息把自己的错误解释掉」。
    """
    rows = _series(60, 18) + _series(39, 2, start=61)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["improvement"] < -0.05, wf["reason"]
    assert wf["verdict"] == pwin.VERDICT_UNRELIABLE


def test_walk_forward_cannot_certify_before_first_settlement():
    """20 个决策日 + horizon=20 ⇒ 没有任何一天的结果在次日可见。

    这不是"样本少"，是"结构上还没有历史"：第 20 天的卡要到第 40 天才结算，
    在此之前用它的胜率预测第 21 天，就是用未来信息。必须判 INSUFFICIENT。
    """
    rows = _series(20, 20)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["verdict"] == pwin.VERDICT_INSUFFICIENT
    assert wf["oos_lcb"] is None


def test_walk_forward_honours_exit_date():
    """有 `exit.date` 时按真实出场日判定可见性，而不是按决策日间隔。

    构造：horizon=1，但所有卡的出场日都被推到 2026-02-01。若只看「间隔 > horizon」，
    第 3、4 天会认为前几天已可见；按真实出场日则一天都不可见 ⇒ INSUFFICIENT。
    """
    rows = []
    for day, wins in (("2026-01-01", 20), ("2026-01-02", 20),
                      ("2026-01-03", 0), ("2026-01-04", 0)):
        for i in range(20):
            r = _row(day, 1 if i < wins else 0, horizon=1)
            r["exit"] = {"date": "2026-02-01", "ret": 0.01}
            rows.append(r)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["verdict"] == pwin.VERDICT_INSUFFICIENT


def test_walk_forward_insufficient_with_one_day():
    rows = _day("2026-01-01", 20)
    wf = pwin.walk_forward(rows)
    assert wf["verdict"] == pwin.VERDICT_INSUFFICIENT
    assert wf["oos_lcb"] is None


# ---------------------------------------------------------------------------
# 失败关闭
# ---------------------------------------------------------------------------


def test_suggest_fails_closed_when_no_history():
    with pytest.raises(ValueError, match="没有可用的历史结算样本"):
        pwin.suggest([])


def test_suggest_returns_unreliable_ledger_as_a_label():
    """账本不可靠不再抛错：它是标签，不是 gate（§9.14 六）。

    `reliable` 曾经是 gate，而它的输入由被 gate 的行为（建卡 → 结算）算出，
    于是永久 409。现在照常给 p_win，把 UNRELIABLE / UNKNOWN 写进出处。
    """
    rows = _series(20, 20) + _series(20, 0, start=21)
    est = pwin.suggest(rows)                    # 默认 allow_prior=False
    assert est["p_win"] is not None
    assert est["reliable"] is False
    assert est["verdict"] == pwin.VERDICT_UNRELIABLE
    assert est["significance"] == settle.SIGNIFICANCE_UNKNOWN


def test_suggest_allow_prior_falls_back_on_cold_start():
    """`allow_prior=true` 是唯一能绕过「没有证据」的方式，且卡上记 source=prior。"""
    est = pwin.suggest([], allow_prior=True)
    assert est["p_win"] == 0.5
    assert est["source"] == pwin.SOURCE_PRIOR
    # 纯先验：两个口径的权重都是 1.0，不是 None、也不是 0。
    assert est["prior_weight"] == 1.0
    assert est["prior_weight_effective"] == 1.0


def test_prior_weights_differ_by_denominator():
    """386 条观测 / n_eff=1：按观测数只有 4.9%，按独立样本是 95.2%。

    这个差值就是 `prior_weight_effective` 存在的理由：`source="ledger"` 只说明
    「有账本」，不说明「账本占多少」。
    """
    raw, eff = pwin.prior_weights(386, 1, 20.0, source=pwin.SOURCE_LEDGER)
    assert raw == pytest.approx(20 / 406, abs=1e-6)
    assert eff == pytest.approx(20 / 21, abs=1e-6)
    assert eff > 19 * raw
    # 没跑 walk-forward / 样本不够：有效权重未知，既不是 0 也不是 1。
    assert pwin.prior_weights(386, None, 20.0, source=pwin.SOURCE_LEDGER)[1] is None
    # 纯先验：两个口径都是 1.0。
    assert pwin.prior_weights(0, None, 20.0, source=pwin.SOURCE_PRIOR) == (1.0, 1.0)


def test_estimate_carries_prior_weight_of_the_number_it_returns():
    rows = _series(40, 14)
    est = pwin.estimate(rows, window=250, kappa=20.0)
    assert est["n_eff"] == 1
    assert est["prior_weight"] == pytest.approx(20 / (est["n"] + 20), abs=1e-6)
    assert est["prior_weight_effective"] == pytest.approx(20 / 21, abs=1e-6)


def test_suggest_passes_when_reliable():
    est = pwin.suggest(_series(40, 14))
    assert est["reliable"] is True
    assert 0.6 < est["p_win"] < 0.8


# ---------------------------------------------------------------------------
# 计分口径
# ---------------------------------------------------------------------------


def test_score_rejects_unknown_basis():
    with pytest.raises(ValueError, match="basis"):
        settle.score([], basis="nope")


def test_score_median_basis_uses_median_outcome():
    """`outcome_med` 与 `outcome` 不一致时，median 口径只认前者。"""
    rows = []
    for i in range(10):
        r = _row(f"2026-02-{i + 1:02d}", 0, p_win=0.9)
        r["outcome_med"] = 1          # 亏钱但跑赢中位数
        rows.append(r)
    abs_score = settle.score(rows, basis="abs")
    med_score = settle.score(rows, basis="median")
    assert abs_score["hit_rate"] == 0.0
    assert med_score["hit_rate"] == 1.0


def test_score_median_basis_constant_is_exactly_half():
    """median 口径的常数基线是 0.5，不是样本命中率——不给自己放水。"""
    rows = _series(10, 18)           # 样本命中率 90%
    s = settle.score(rows, basis="median")
    assert s["brier_const"] == pytest.approx(0.25)
    # 若误用样本命中率 (0.9)，brier_const 会是 0.09，skill 会被放大 2.8 倍
    assert s["brier_const"] != pytest.approx(0.09)


def test_score_median_basis_clusters_by_day():
    """同一天 20 个 pick 只提供一个自由度：单日样本算不出置信下界。"""
    rows = _day("2026-01-01", 20, p_win=0.9)
    s = settle.score(rows, basis="median")
    assert s["n"] == 20
    assert s["days"] == 1
    assert s["skill_lcb"] is None
    assert s["significant"] is False


def test_score_median_basis_significant_across_days():
    """多日一致地准 ⇒ 下界才可能为正，且有效样本要撑得住。

    80 天 / 20 日持有期：`80 >= 3·20`，NW 可用，`n_eff` 是估计值而不是上界。
    30 天时下界同样为正，但 `n_eff` 只有 1——那种情形不该给「显著」标签，
    见 `test_score_does_not_claim_significance_without_effective_sample`。
    """
    rows = _series(80, 18, p_win=0.8)
    s = settle.score(rows, basis="median")
    assert s["days"] == 80
    assert s["skill_lcb"] is not None
    assert s["skill_lcb"] > 0
    assert s["n_eff_reliable"] is True
    assert s["significant"] is True


def test_score_does_not_claim_significance_without_effective_sample():
    """下界为正不等于结论成立：30 天 / 20 日持有期只有 1 个不重叠窗口。

    这是把 `n_eff` 暴露出来的直接后果。旧实现会把这份合成账本报成
    `significant=true`，而它的有效样本是 1。`skill_lcb` 保留（它是读数），
    `significance` 降级为 UNKNOWN（它是结论）——数字和结论的失败模式不一样。
    """
    rows = _series(30, 18, p_win=0.8)
    s = settle.score(rows, basis="median")
    assert s["skill_lcb"] is not None and s["skill_lcb"] > 0
    assert s["hac_lag"] == 19
    assert s["n_eff"] == 1
    assert s["n_eff_reliable"] is False
    # 读数与结论是两个字段：读数为正，结论仍不可判定。
    assert s["skill_lcb_positive"] is True
    assert s["significance"] == settle.SIGNIFICANCE_UNKNOWN
    assert s["significant"] is False


def test_significance_of_three_states():
    """三态判定本身：符号只管 PASS/FAIL，分母够不够管 UNKNOWN。

    单独钉住这个函数，是因为「构造一份 n_eff 恰好落在 30 附近、且下界符号
    符合预期的账本」比测这个函数难得多，而判定逻辑才是要守的东西。
    """
    assert settle.significance_of(0.10, 44, True) == settle.SIGNIFICANCE_PASS
    assert settle.significance_of(-0.10, 44, True) == settle.SIGNIFICANCE_FAIL
    # NW 可用但有效样本仍不够：结论只能是「不可判定」，不是「没通过」。
    assert settle.significance_of(0.10, 12, True) == settle.SIGNIFICANCE_UNKNOWN
    # NW 不可用：n_eff 是构造值，下界不能当结论。
    assert settle.significance_of(0.10, 1, False) == settle.SIGNIFICANCE_UNKNOWN
    # 少于两个决策日：没有下界可读。
    assert settle.significance_of(None, None, False) == settle.SIGNIFICANCE_UNKNOWN


def test_score_pass_and_fail_both_require_a_denominator():
    """样本撑得住时，符号决定 PASS / FAIL；两种情形都不是 UNKNOWN。"""
    good = settle.score(_series(80, 18, p_win=0.8), basis="median")
    assert good["n_eff_reliable"] is True
    assert good["significance"] == settle.SIGNIFICANCE_PASS
    assert good["significant"] is True

    # 系统性高估（p=0.9，实际命中率 50%）⇒ 下界为负 ⇒ FAIL。
    bad = settle.score(_series(80, 10, p_win=0.9), basis="median")
    assert bad["skill_lcb"] is not None and bad["skill_lcb"] < 0
    assert bad["significance"] == settle.SIGNIFICANCE_FAIL
    assert bad["significant"] is False
    assert bad["skill_lcb_positive"] is False


def test_score_insufficient_below_min_n():
    rows = [_row("2026-01-01", 1)] * 5
    s = settle.score(rows, basis="median")
    assert s["verdict"] == "INSUFFICIENT"
    assert s["n"] == 5


def test_abs_basis_still_works_without_median_fields():
    """旧记录没有 outcome_med，abs 口径必须照常可算（向后兼容）。"""
    rows = []
    for i in range(10):
        r = _row(f"2026-03-{i + 1:02d}", 1)
        r.pop("outcome_med")
        rows.append(r)
    s = settle.score(rows, basis="abs")
    assert s["n"] == 10
    assert s["hit_rate"] == 1.0
    assert s["brier"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 两个基线：`days` / `oos_days` 的语义，以及「常数预测器恒无增量」
# ---------------------------------------------------------------------------


def _month(prefix: str, n_days: int, wins_per_day: int, *, total: int = 20, **kw):
    return [r for i in range(n_days)
            for r in _day(f"{prefix}-{i + 1:02d}", wins_per_day, total, **kw)]


def test_days_and_oos_days_are_distinct():
    """`days` 恒为总决策日，`oos_days` 恒为可评估日——同一个键不能有两种含义。

    20 天账本 + horizon=20：一天都不可评估。旧实现里 `days` 在失败分支是
    20、在成功分支是 `len(daily)`，而 `estimate()` 又把 `wf["days"]` 透传成
    `oos_days`，于是「账本规模」被当成「样本外天数」显示。
    """
    rows = _series(20, 20)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["verdict"] == pwin.VERDICT_INSUFFICIENT
    assert wf["days"] == 20        # 账本里有 20 个决策日
    assert wf["oos_days"] == 0     # 但没有一天可评估

    est = pwin.estimate(rows, window=250, kappa=20.0)
    assert est["days"] == 20
    assert est["oos_days"] == 0    # 曾经这里会变成 20


def test_success_branch_separates_total_and_oos_days():
    """成功分支同样要分开：60 天账本，horizon=20 吃掉尾部，可评估日必然更少。"""
    rows = _series(60, 14)
    wf = pwin.walk_forward(rows, window=250, kappa=20.0)
    assert wf["days"] == 60
    assert 0 < wf["oos_days"] < 60


def test_constant_predictor_has_no_increment_but_can_be_reliable():
    """稳定 70%：p 比 0.5 好（RELIABLE），但不如全历史常数（NO_INCREMENT）。

    两者同时成立不是矛盾——p 的**水平**对，但**没有区分度**。
    这也是「任何常数预测器相对展开基线的 Brier 改善恒 ≤ 0」的直接体现：
    常数没有分辨率（resolution），分解里 `BSS_clim = (RES - REL)/UNC ≤ 0`。

    窗口必须真的截断（`window=20` < 39 个已可见日），否则近端 ≡ 全历史、
    改善恒为 0，那是 `NOT_TESTABLE` 而不是 `NO_INCREMENT`——见
    `test_increment_is_not_testable_when_window_covers_every_day`。
    这里改善是 −2e−05 而不是精确 0：两个窗口的样本量不同，收缩力度也不同，
    所以「没有分辨率」表现为一个很小的负数，而不是恒等式。
    """
    rows = _series(60, 14)
    wf = pwin.walk_forward(rows, window=20, kappa=20.0)
    assert wf["truncated_any"] is True
    assert wf["verdict"] == pwin.VERDICT_RELIABLE
    assert wf["verdict_incremental"] == pwin.VERDICT_NO_INCREMENT
    assert wf["improvement_vs_clim"] <= 0


def test_increment_detected_when_regime_drifts_and_window_is_short():
    """制度翻转 + 短窗口：近端跟上新制度，全历史还停在旧制度 ⇒ INCREMENTAL。

    这是 `verdict_incremental` 唯一应该有非零答案的场景：p 真的时变。
    """
    rows = (_month("2026-01", 30, 18, horizon=1)
            + _month("2026-03", 30, 2, horizon=1))
    wf = pwin.walk_forward(rows, horizon=1, window=5, kappa=20.0)
    assert wf["verdict_incremental"] == pwin.VERDICT_INCREMENTAL, wf["reason_incremental"]
    assert wf["improvement_vs_clim"] > 0


def test_increment_is_not_testable_when_window_covers_every_day():
    """默认配置在 40 日账本上：`window=250 ≥ 38 个可评估日` ⇒ 检验没有功效。

    `visible[-250:] ≡ visible` ⇒ `p ≡ p_clim` ⇒ 改善恒为 0。旧实现把这个 0
    报成 `NO_INCREMENT` +「近端窗口不比全历史多含信息」——那是拿同一批样本
    跟自己比，结论的强度高于证据。现在状态是 `NOT_TESTABLE`，数字全部撤掉。
    """
    rows = _series(40, 14)
    wf = pwin.walk_forward(rows, window=pwin.DEFAULT_WINDOW)
    assert wf["oos_days"] <= pwin.DEFAULT_WINDOW      # 前置条件：窗口盖住全部可评估日
    assert wf["truncated_any"] is False
    assert wf["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    # 不报数字：恒等式的产物不是估计值。
    assert wf["improvement_vs_clim"] is None
    assert wf["skill_vs_clim"] is None
    assert wf["skill_vs_clim_lcb"] is None
    assert wf["n_eff_incremental"] is None
    # 也不能再说「没有增量」——那句话需要的对照不存在。
    assert "没有功效" in wf["reason_incremental"]
    assert "多含信息" not in wf["reason_incremental"]
    # 第一个检验不受影响：它比的是常数 0.5，不是另一个估计量。
    assert wf["verdict"] == pwin.VERDICT_RELIABLE
    assert wf["oos_skill"] is not None

    est = pwin.estimate(rows, window=pwin.DEFAULT_WINDOW)
    assert est["truncated_any"] is False
    assert est["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert est["oos_improvement_vs_clim"] is None


def test_se_incremental_is_exposed_and_matches_the_verdict():
    """`se_incremental` 与判定同源：判定读的 `lcb` 就是这个 `se` 算出来的。

    这个字段不是「顺便多给一个数」。修完单位（§9.24）之后，面板要回答
    「还差多少日历」只能把 `se` 按 `√(n_eff/B)` 缩放到预算内的块数；而当时
    唯一的入口是 `skill_vs_clim_lcb`——4 位小数的**比值**，真实账本上它落在
    ±0.0004 的格子里，反解出的 `se` 误差可达 25%，正好把「还差两年」和
    「还差二十年」混成一个数。

    断言关系而不是数字：`lcb = mean − 1.645·se`，判定就是这个 `lcb` 的符号。
    """
    wf = pwin.walk_forward(_series(60, 14), window=20, kappa=20.0)
    se = wf["se_incremental"]
    assert isinstance(se, float) and se > 0
    lcb = wf["improvement_vs_clim"] - settle.Z90 * se
    assert ((wf["verdict_incremental"] == pwin.VERDICT_INCREMENTAL)
            == (lcb > 0)), wf["reason_incremental"]


def test_se_incremental_is_none_when_the_test_did_not_run():
    """没做检验就没有标准误：`None` 是「没算」，不是 0。

    `se=0` 会被下游读成「门限 0、已经判定」，把「没测」变成最强的结论——
    和 `n_eff_incremental=None` 同一条规矩，两个字段必须一起撤。
    """
    wf = pwin.walk_forward(_series(40, 14), window=pwin.DEFAULT_WINDOW)
    assert wf["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert wf["se_incremental"] is None
    empty = pwin.walk_forward([])
    assert empty["verdict_incremental"] == pwin.VERDICT_INSUFFICIENT
    assert empty["se_incremental"] is None


def test_increment_is_testable_once_the_window_truncates():
    """同一份账本，window 缩短到真的截断 ⇒ 检验恢复，数字回来。

    与上一条成对：证明 `NOT_TESTABLE` 是「这个配置不可识别」，
    不是「这个检验永远判不了」。
    """
    rows = _series(40, 14)
    wf = pwin.walk_forward(rows, window=10)
    assert wf["truncated_any"] is True
    assert wf["verdict_incremental"] != pwin.VERDICT_NOT_TESTABLE
    assert wf["improvement_vs_clim"] is not None
    assert wf["skill_vs_clim_lcb"] is not None


def test_score_abs_baseline_is_expanding_not_insample():
    """abs 口径的分母不能是样本内命中率——那会让 skill 恒 ≤ 0（§9.3 的恒等式）。

    40 天、每天 30% 命中、p 恒为 0.3：
      旧实现 brier_const = mean((hit-y)²) = 0.21 = brier ⇒ skill 恒为 0；
      新实现的分母是展开 climatology（冷启动段仍是 0.5），
      所以 p=0.3 在冷启动段反而赢过基线，skill > 0。
    """
    rows = _series(40, 6, p_win=0.3)
    s = settle.score(rows, basis="abs")
    assert s["brier_insample"] == pytest.approx(0.21, abs=1e-6)
    # abs 口径下 `skill` 与 `skill_vs_clim` 同源（分母都是展开 climatology）
    assert s["skill"] == pytest.approx(s["skill_vs_clim"], abs=1e-6)
    assert s["skill"] > 0


def test_marginal_verdict_when_increment_is_significant_but_tiny():
    """下界 > 0 但量级低于门槛 ⇒ MARGINAL，不是 INCREMENTAL。

    实测 abs 口径 window=250 就是 `+0.00011 / 下界 +0.00003`。只按符号判定
    会得到「显著但等于零」的结论，那比不判定更误导——它把一个 0.05% 的
    改善说成「近端含有远端没有的信息」。
    """
    v, reason, sig = pwin._incremental_verdict(0.00011, 0.00003, n_eff=100, n_eff_reliable=True)
    assert v == pwin.VERDICT_MARGINAL
    assert sig == settle.SIGNIFICANCE_PASS
    assert "不足以改变决策" in reason

    assert pwin._incremental_verdict(-0.002, -0.004)[0] == pwin.VERDICT_NO_INCREMENT
    big = pwin._incremental_verdict(0.05, 0.03, n_eff=100, n_eff_reliable=True)
    assert big[0] == pwin.VERDICT_INCREMENTAL


def test_not_testable_is_a_state_not_a_zero():
    """同样的 `0 / 0`，一个来自「测过、确实没有」，一个来自「根本没有对照」。

    两者必须分开：前者的后续动作是改策略，后者是改 window。合并成一个
    `NO_INCREMENT` 时，读的人无法区分，而「否」比「未知」更容易被当成定论。
    """
    v, why, sig = pwin._incremental_verdict(0.0, 0.0, testable=False,
                                            window=250, max_visible=39)
    assert v == pwin.VERDICT_NOT_TESTABLE
    assert sig == settle.SIGNIFICANCE_UNKNOWN
    assert "250" in why and "39" in why
    assert "多含信息" not in why

    # 默认 `testable=True`：同样的数字仍是「测过、没有增量」——前提是分母够。
    v2, why2, sig2 = pwin._incremental_verdict(0.0, 0.0, n_eff=100, n_eff_reliable=True,
                                               truncated_days=39, evaluated_days=39)
    assert v2 == pwin.VERDICT_NO_INCREMENT
    assert sig2 == settle.SIGNIFICANCE_FAIL
    assert "没测出增量" in why2
    assert why != why2


def test_one_truncated_day_cannot_speak_for_the_whole_ledger():
    """`truncated_any=True` 只保证**至少一天**被截断；一天撑不起一句数据结论。

    60 天账本、90% → 10% 的制度翻转、horizon=1。窗口取「还能截断的最大值」时，
    只剩最后一天被截断，其余 56 天的逐日改善**恒为 0**（那两天两个估计量相同）。
    均值因此是 1 天的效应除以 57。

    旧句子在这时报「近端窗口不比全历史多含信息」——57 天里只比了 1 天。
    数字留着（它是真的），句子必须和证据等长：覆盖度进 `reason_incremental`，
    `truncated_days` 单独成一个字段。

    这条同时是 P1 的最小复现：1 个对照日，旧实现给 `n_eff=57`（H=1、lag=0，
    于是 NW 的 `n_eff` 就是总天数）⇒ `significance_incremental=FAIL`。
    那个 FAIL 是拿 1 天的证据给出的。分母的上限必须是对照天数。
    """
    rows = (_month("2026-01", 30, 18, horizon=1)
            + _month("2026-03", 30, 2, horizon=1))
    last = None
    for w in range(1, 70):
        wf = pwin.walk_forward(rows, horizon=1, window=w, kappa=20.0)
        if not wf["truncated_any"]:
            break
        last = wf
    assert last is not None
    assert last["truncated_days"] == 1
    assert last["oos_days"] > 50
    # 一天对照至多一个独立样本——不是 57。
    assert last["n_eff_incremental"] <= 1
    assert last["significance_incremental"] == settle.SIGNIFICANCE_UNKNOWN
    assert last["verdict_incremental"] == pwin.VERDICT_NO_INCREMENT
    # 数字是真的：那天近端确实赢了，只是被 56 个恒等零摊薄到 1/57。
    assert last["improvement_vs_clim"] > 0
    assert "1/" in last["reason_incremental"]
    assert "没有分母" in last["reason_incremental"]
    assert "多含信息" not in last["reason_incremental"]

    # 同一份数据、窗口短到大多数天都截断 ⇒ 结论反过来，且句子带覆盖度。
    short = pwin.walk_forward(rows, horizon=1, window=5, kappa=20.0)
    assert short["truncated_days"] > 50
    assert short["verdict_incremental"] == pwin.VERDICT_INCREMENTAL
    assert f"{short['truncated_days']}/{short['oos_days']}" in short["reason_incremental"]


def _long_ledger(n_days: int = 278, wins: int = 14, *, horizon: int = 20) -> list[dict]:
    """278 个决策日 ⇒ 256 个可评估日（H=20，尾部 22 天还没结算）。

    选 278 是为了对上真实账本那一行：默认 `window=250`、256 个可评估日、
    只有 6 天被截断。日期按 28 天一个月生成，保证 `as_of` 合法且升序。
    """
    return [r for i in range(n_days)
            for r in _day(f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                          wins, horizon=horizon)]


def test_identity_zeros_do_not_become_effective_samples():
    """`d = [0]*255 + [0.02]`：1 个真实对照日，NW 却给出 256 个独立样本。

    这 255 个 0 不是抽样噪声，是 `p == p_clim` 的恒等式。它们进序列是对的
    （逐日改善的均值要覆盖全部评估日），但当分母是错的：方差被零稀释
    ⇒ `se ≈ se_iid` ⇒ `n_eff ≈ n`。于是「证据越少，分母越大」。
    """
    daily = [0.0] * 255 + [0.02]
    _, se, _, se_iid = settle.hac_lcb(daily, 19)
    n_eff, reliable = settle.effective_sample(len(daily), se_iid, se, 19)
    assert n_eff == 256            # 旧读数：一天对照 = 256 个独立样本
    assert reliable is True        # 而且它被标成「NW 可用」，不是构造值
    # 2 天 / 6 天同理：MA(19) 下不重叠窗口数都是 1。
    assert pwin._identifiable_sample(256, 1, 19) == 1
    assert pwin._identifiable_sample(132, 2, 19) == 1
    assert pwin._identifiable_sample(48, 6, 19) == 1
    # H=1 没有重叠：上限退化为对照天数本身，不是总天数。
    assert pwin._identifiable_sample(57, 1, 0) == 1
    assert pwin._identifiable_sample(57, 40, 0) == 40


def test_effective_sample_never_exceeds_contrast_days():
    """P1 的不变量：`n_eff_incremental` 不得超过对照天数（并按 MA(H−1) 折算）。

    同一条 256 天账本、同一个常数预测器（每天 14/20，**没有**时变信息）。
    旧实现在 `window=250` 时报 `n_eff=57`、`FAIL`——6 天对照被当成 57 个
    独立样本；`window=245` 报 35、240 报 26、235 报 22，一路掉到 14。
    那个序列连「证据越多、分母越大」都不满足（见下一条）。
    """
    rows = _long_ledger()
    for window in (250, 215, 180, 145):
        wf = pwin.walk_forward(rows, window=window)
        k = wf["truncated_days"]
        step = wf["hac_lag_incremental"] + 1
        assert k > 0
        assert wf["n_eff_incremental"] <= k
        # 更强的一条：对照日按决策日连续排列，持有 H 天时独立样本至多 k/H。
        # 只截到 k 仍会把 36 天当成 17 个样本（真值约 2）。
        assert wf["n_eff_incremental"] <= max(1, k // step)


def test_effective_sample_grows_with_contrast_days():
    """同样本、`truncated_days` 越大，分母必须不减。

    这是「证据越少、分母越大」的直接反面。旧的 NW 读数在 57 → 35 → 26 →
    22 → 19 → 18 → 17 上乱跳，取 `min(n_eff, k)` 之后仍会先升后降；
    按 `k // (lag+1)` 截断才让它随 k 单调。
    """
    rows = _long_ledger()
    pairs = []
    for window in (250, 235, 215, 195, 175, 155):
        wf = pwin.walk_forward(rows, window=window)
        pairs.append((wf["truncated_days"], wf["n_eff_incremental"]))
    ks = [k for k, _ in pairs]
    nes = [n for _, n in pairs]
    assert ks == sorted(ks) and len(set(ks)) == len(ks)
    assert nes == sorted(nes), pairs


def test_default_window_does_not_fail_on_six_contrast_days():
    """验收 (d)：256 个可评估日、默认 window、6 天对照 ⇒ UNKNOWN，不是 FAIL。

    账本是**常数**预测器（每天恰好 14/20 命中）：近端窗口与全历史在期望上
    没有差别，`NO_INCREMENT` 是正确的符号读数。但旧实现给
    `significance_incremental=FAIL`（`n_eff=57`），把一个「没有增量」的结论
    建立在 6 天对照上——而文档说这个判定要 600 天。两句话不能同时为真。
    """
    rows = _long_ledger()
    wf = pwin.walk_forward(rows, window=pwin.DEFAULT_WINDOW)
    assert (wf["oos_days"], wf["truncated_days"]) == (256, 6)
    assert wf["n_eff_incremental"] == 1
    assert wf["significance_incremental"] == settle.SIGNIFICANCE_UNKNOWN
    assert "6/256" in wf["reason_incremental"]
    assert "没有分母" in wf["reason_incremental"]
    # 三态管「有没有分母」，verdict 仍管符号——降级的是结论强度，不是数字。
    assert wf["verdict_incremental"] == pwin.VERDICT_NO_INCREMENT


def test_verdict_cannot_use_a_denominator_bigger_than_the_contrast():
    """判定函数自己也守这条不变量，而不是只靠 `walk_forward` 传对参数。

    传入 1 天对照 + 256 个「等效样本」，旧实现会给出 `FAIL`。这条钉的是
    调用边界：只要两个数一起出现，分母就必须被截。
    """
    v, why, sig = pwin._incremental_verdict(
        0.00004, -0.00001, n_eff=256, n_eff_reliable=True,
        truncated_days=1, evaluated_days=256, hac_lag=19)
    assert sig == settle.SIGNIFICANCE_UNKNOWN
    assert "等效独立样本 1" in why
    assert "1/256" in why
    assert v == pwin.VERDICT_NO_INCREMENT


def test_increment_verdict_can_be_positive_and_still_have_no_denominator():
    """`INCREMENTAL` 读的是符号与量级；有效样本 1 时那个下界没有分母。

    H=20 的账本只有 ~37 个可评估日，`lag=19` ⇒ `n_eff = n // 20 = 1`，
    且 `n_eff_reliable=False`（构造值）。制度翻转仍然让下界为正——改善是真的，
    但「近端窗口含有远端没有的信息」是一句关于可复现性的结论，1 个独立样本
    撑不起它。旧实现把这句话原样印出来，`significance_incremental` 现在必须
    是 UNKNOWN，且句子要换掉（不能只在末尾加脚注：读者读的是句子）。

    同一份数据、`horizon=1` 时 `n_eff=57 ≥ 30`，同一句话才成立——差别不在
    数据，在分母。
    """
    rows20 = (_month("2026-01", 30, 18, horizon=20)
              + _month("2026-03", 30, 2, horizon=20))
    wf = pwin.walk_forward(rows20, horizon=20, window=5, kappa=20.0)
    assert wf["truncated_any"] is True
    assert wf["verdict_incremental"] == pwin.VERDICT_INCREMENTAL
    assert wf["n_eff_incremental"] < settle.MIN_NEFF
    assert wf["n_eff_incremental_reliable"] is False
    assert wf["significance_incremental"] == settle.SIGNIFICANCE_UNKNOWN
    assert "多含信息" not in wf["reason_incremental"]
    assert "没有分母" in wf["reason_incremental"]

    rows1 = (_month("2026-01", 30, 18, horizon=1)
             + _month("2026-03", 30, 2, horizon=1))
    wf1 = pwin.walk_forward(rows1, horizon=1, window=5, kappa=20.0)
    assert wf1["n_eff_incremental"] >= settle.MIN_NEFF
    assert wf1["significance_incremental"] == settle.SIGNIFICANCE_PASS
    assert "含有远端没有的信息" in wf1["reason_incremental"]

    # 检验没做（窗口盖住全部已可见日）不是 FAIL：三态只能是 UNKNOWN。
    nt = pwin.walk_forward(rows1, horizon=1, window=250, kappa=20.0)
    assert nt["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert nt["significance_incremental"] == settle.SIGNIFICANCE_UNKNOWN

    # `estimate` 与 `walk_forward` 必须报同一个状态，否则卡与接口会说两句话。
    est = pwin.estimate(rows20, horizon=20, window=5, kappa=20.0)
    assert est["significance_incremental"] == wf["significance_incremental"]


def test_not_testable_names_the_barrier_that_actually_binds():
    """`NOT_TESTABLE` 的句子必须点名真正卡住的量，否则它开的药方是错的。

    同一份 40 天账本、同一个 `window=250`，两个持有期卡在不同的地方：

    - `H=20`：18 个可评估日、`lag=19` ⇒ 即使把 window 缩到 1，`n_eff` 也只是
      18 // 20 = 1。「缩小 window」能让检验跑起来，却不能让结论成立——实测
      那条路径把状态换成了 `NO_INCREMENT`，一个更像结论、也更假的词。
    - `H=1`：37 个可评估日、`lag=0` ⇒ window 是唯一障碍，缩小它就真能判定。

    所以这句话不能写死成「window 必须小于可评估日数」：它只对第二种成立。
    """
    rows20 = _series(40, 18, horizon=20)
    wf = pwin.walk_forward(rows20, horizon=20, window=250)
    assert wf["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert wf["oos_days"] == 18
    assert "不是改配置能解开的" in wf["reason_incremental"]
    # 30 × (19+1)：这是「按 MA(H−1) 的构造值推算」的样本门槛，不是拍出来的。
    assert "600" in wf["reason_incremental"]

    # 反证：缩 window 之后状态变了，分母没变。
    tight = pwin.walk_forward(rows20, horizon=20, window=1)
    assert tight["verdict_incremental"] != pwin.VERDICT_NOT_TESTABLE
    assert tight["n_eff_incremental"] == 1
    assert tight["significance_incremental"] == settle.SIGNIFICANCE_UNKNOWN

    rows1 = _series(40, 18, horizon=1)
    wf1 = pwin.walk_forward(rows1, horizon=1, window=250)
    assert wf1["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert "window 必须小于可评估日数" in wf1["reason_incremental"]
    assert "不是改配置" not in wf1["reason_incremental"]


def test_incremental_lag_is_the_hold_overlap_not_the_window():
    """增量序列的滞后阶数仍由 `horizon` 决定，`window` 不进这个数。

    §9.18 曾把它改成 `window-1`，依据是「同一份账本上 r1(d)=+0.98」。那个
    0.98 是在**确定性账本**（每天恰好 18/20 命中，无抽样噪声）上量的：此时
    `d_i` 只剩 `δ = p_clim - p` 的缓慢漂移，r1 必然接近 1。换成同一命中率的
    二项抽样账本，r1(d) ≈ -0.02（见 `scripts/probe_incremental_lag.py`）。

    所以这里钉的不是 r1，而是「`n_eff` 不该被 window 打塌」：`horizon=1` 时
    没有结果重叠，`n_eff` 恰好等于**对照天数**——不是总可评估日数。没被截断
    的那些天 `p == p_clim`、`d_i ≡ 0`，它们进不了分母；把 `oos_days` 当分母
    正是「1 天对照 = 256 个独立样本」那个 bug 的形状。
    """
    rows = (_month("2026-01", 30, 18, horizon=1)
            + _month("2026-03", 30, 2, horizon=1))

    for window in (12, 40):
        wf = pwin.walk_forward(rows, horizon=1, window=window, kappa=20.0)
        assert wf["hac_lag"] == 0
        # §9.18 在这里给 11 / 39，并把 n_eff 打到 6 / 1。
        assert wf["hac_lag_incremental"] == 0
        assert wf["n_eff_incremental"] == wf["truncated_days"]
        assert wf["n_eff_incremental"] < wf["oos_days"]

    # 持有 20 天：重叠是结构性的，两个滞后都等于 19。
    rows20 = (_month("2026-01", 30, 18, horizon=20)
              + _month("2026-03", 30, 2, horizon=20))
    wf20 = pwin.walk_forward(rows20, horizon=20, window=12, kappa=20.0)
    assert wf20["hac_lag"] == 19
    assert wf20["hac_lag_incremental"] == 19


def test_median_basis_keeps_clim_as_a_separate_baseline():
    """median 口径的分母必须留在 0.25：0.5 是「随机选股」的定义性基准，不是估计。"""
    rows = _series(30, 18, p_win=0.8)
    s = settle.score(rows, basis="median")
    assert s["brier_const"] == pytest.approx(0.25)
    assert s["brier_random"] == pytest.approx(0.25)
    # 展开基线是另一个数，单独报告，不替换分母
    assert s["brier_clim"] != pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 重叠持有期：逐日观测不是独立样本
# ---------------------------------------------------------------------------


def _wavy(n_days: int, *, total: int = 20, amp: int = 6) -> list[dict]:
    """日胜率缓慢起伏的账本——模拟「制度」而不是「独立抽样」。

    每天 20 个 pick、命中数按正弦漂移，于是逐日改善量强正自相关。
    这正是持有期重叠时真实序列的形状。
    """
    rows = []
    for i in range(n_days):
        wins = total // 2 + round(amp * math.sin(i / 8.0))
        rows.extend(_day(f"2026-01-{i + 1:02d}", wins, total))
    return rows


def test_effective_sample_caps_at_observations():
    """`n_eff` 是「等效独立样本数」，不能比总观测数还大。

    NW 在 n 接近 lag 时会估出 `se < se_iid`（负自相关，或者纯噪声），
    旧公式 `n·(se_iid/se)²` 于是给出 400 这样的值——比 n=100 还多。
    """
    n_eff, reliable = settle.effective_sample(100, se_iid=0.10, se=0.05, lag=10)
    assert reliable is True
    assert n_eff == 100          # 公式给 400，必须截到 n


def test_effective_sample_uses_windows_when_lag_dominates():
    """`n < 3·(lag+1)` 时 NW 不可用，退回不重叠窗口数。

    真实账本 17 天 / 20 日持有期就是这一档：同一个 MA(19) 过程抽 5 次，
    方差膨胀在 0.56–1.26 之间跳，`n_eff` 在 11–55 之间跳。退回
    `n // (lag+1)` 后是 1——与 §9.11 的「真实独立样本可能是 1–2 个」一致，
    但不再是推断，是代码输出。
    """
    assert settle.effective_sample(17, se_iid=0.25, se=0.12, lag=19) == (1, False)
    # 边界：60 == 3·20，刚好够用
    assert settle.effective_sample(60, se_iid=0.10, se=0.20, lag=19) == (15, True)


def test_walk_forward_reports_n_eff_reliability():
    """`n_eff_reliable` 必须随 `n_eff` 一起出现，否则读数没有分母。

    100 天账本 / 20 日持有期 ⇒ 约 79 个可评估日 ≥ 3·20，NW 可用；
    30 天账本 ⇒ 约 9 个可评估日，`n_eff` 退化为 1。
    """
    wf = pwin.walk_forward(_wavy(100), window=30, kappa=20.0)
    assert wf["n_eff_reliable"] is True

    short = pwin.walk_forward(_wavy(30), window=30, kappa=20.0)
    assert short["n_eff"] == 1
    assert short["n_eff_reliable"] is False


def test_walk_forward_keeps_the_two_n_eff_apart():
    """两个检验各有各的残差序列，`n_eff` 不能共用一个键。

    `verdict` 的序列是 `(prior-y)² - (p-y)²`，`verdict_incremental` 的是
    `(p_clim-y)² - (p-y)²`。自协方差不同 ⇒ se 不同 ⇒ n_eff 不同。
    现在两者都落在退化分支（都是 1），看不出差别；账本越过 `3·(lag+1)`
    之后就会分叉，而那时两个都叫 `n_eff` 的数字会指向不同的东西。
    """
    wf = pwin.walk_forward(_wavy(100), window=30, kappa=20.0)
    assert "n_eff_incremental" in wf
    assert "n_eff_incremental_reliable" in wf
    # 退化区间里两者同值，但键已经分开——这是这次改动要守的形状。
    assert wf["n_eff"] is not None
    assert wf["n_eff_incremental"] is not None


def test_estimate_significance_is_unknown_when_sample_is_short():
    """冷启动与短账本：`significance` 是 UNKNOWN，不是 FAIL。

    这条把「没测」和「没通过」钉成两个值。`reliable=False` 仍然保留原语义
    （lcb 不为正），但读的人要能看出它为什么不为正。
    """
    assert pwin.estimate([])["significance"] == settle.SIGNIFICANCE_UNKNOWN

    short = pwin.estimate(_series(30, 18, p_win=0.8), horizon=20, basis="median")
    assert short["n_eff"] == 1
    assert short["n_eff_reliable"] is False
    assert short["significance"] == settle.SIGNIFICANCE_UNKNOWN
    # 旧字段的语义没变：它仍然只回答「lcb > 0 吗」。
    assert short["reliable"] is True


def test_hac_lag_zero_is_plain_iid():
    """lag=0 必须逐位退化为原来的 iid 公式，不能悄悄改变旧口径。"""
    vals = [0.1, -0.2, 0.3, 0.05, -0.1, 0.2, 0.0, 0.15]
    est = settle.hac_lcb(vals, 0)
    assert est is not None
    mean, se, lcb, se_iid = est
    assert se == pytest.approx(se_iid)
    assert lcb == pytest.approx(mean - settle.Z90 * se)


def test_hac_reverses_a_significant_reading_under_autocorrelation():
    """强正自相关的逐日改善：iid 说显著，HAC 说不显著。

    这是 `abs + window=250` 的真实情形（改善 +0.00011、iid 下界 +0.00003）。
    只按 iid 算下界会把「重叠窗口的伪精度」当成证据。
    """
    step = 1e-4
    vals = [5 * step + step * (i - 49.5) for i in range(100)]
    mean, se, lcb, se_iid = settle.hac_lcb(vals, 20)
    assert mean - settle.Z90 * se_iid > 0     # iid 口径：显著为正
    assert lcb < 0                            # HAC 口径：不显著
    assert se > 2 * se_iid


def test_effective_sample_shrinks_when_outcomes_persist():
    """等效独立样本必须小于可评估日：重叠持有期的信息损失要能被看见。

    window 必须短于账本长度，否则近端与全历史逐日相同、改善恒为 0，
    测出来的是「两个估计量完全一样」而不是「重叠导致样本缩水」。
    """
    rows = _wavy(60)
    wf = pwin.walk_forward(rows, window=30, kappa=20.0)
    assert wf["hac_lag"] == 19                    # horizon 20 ⇒ 重叠 19 天
    assert wf["n_eff"] is not None
    assert wf["n_eff"] < wf["oos_days"]

    est = pwin.estimate(rows, window=30, kappa=20.0)
    assert est["n_eff"] == wf["n_eff"]


def test_score_clusters_and_corrects_overlap():
    """`settle.score` 的 skill 下界走同一套 HAC，lag 从账本的 horizon 推出。"""
    rows = [dict(r, p_win=0.7) for r in _wavy(60)]
    s = settle.score(rows, basis="median")
    assert s["days"] == 60
    assert s["skill_lcb"] is not None

    by_day: dict[str, list[tuple[float, int]]] = {}
    for r in rows:
        by_day.setdefault(r["as_of"], []).append((0.7, int(r["outcome_med"])))
    daily = [sum((0.5 - y) ** 2 - (p - y) ** 2 for p, y in v) / len(v)
             for v in by_day.values()]
    iid = settle.hac_lcb(daily, 0)
    hac = settle.hac_lcb(daily, 19)
    assert iid is not None and hac is not None
    # iid 下界更乐观；score 报的必须是 HAC 那一版（除以常数基线 0.25）
    assert iid[2] > hac[2]
    assert s["skill_lcb"] == pytest.approx(hac[2] / 0.25, abs=1e-4)


def test_window_counts_settled_days_not_calendar_days():
    """`window` 必须数**已结算**的决策日，否则 walk-forward 验的不是同一个模型。

    旧实现按日历日取窗口：20 日持有期下最近 21 天全部不可见，于是
    `window=20` 一天都评估不出来（`oos_days=0`），而 `estimate(window=20)`
    照常给数。同一个参数两种含义。
    """
    rows = _wavy(60)
    wf = pwin.walk_forward(rows, window=20, kappa=20.0)
    assert wf["oos_days"] > 0
    assert wf["verdict"] != pwin.VERDICT_INSUFFICIENT

    # 窗口覆盖全部已结算日时，近端与远端是同一个预测器 ⇒ 检验没有功效。
    # 旧实现把恒等式的 0 报成 NO_INCREMENT（「近端不比全历史多含信息」）。
    wide = pwin.walk_forward(rows, window=10_000, kappa=20.0)
    assert wide["truncated_any"] is False
    assert wide["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert wide["improvement_vs_clim"] is None


def test_capped_n_eff_reports_both_numbers():
    """被对照天数截断的 `n_eff` 必须把 NW 的原读数一起印出来。

    `reliable` 回答的是「NW 可不可用」，不是「这个数是不是构造值」。真实账本
    `window=250` 上 `n_eff_incremental=3`（= 72//21），而 NW 读出 32——
    只印 3，屏幕上它就是一个估计值。这是 §9.23 里那个「同一个公式、两处标注」
    的现场：`settle.py` 说它是模型推断值，`pwin.py` 说它是上界。
    """
    verdict, reason, sig = pwin._incremental_verdict(
        0.0001, -0.0002, 32,
        truncated_days=72, evaluated_days=322, n_eff_reliable=True, hac_lag=19)
    assert sig == settle.SIGNIFICANCE_UNKNOWN
    assert "等效独立样本 3" in reason
    assert "NW 读出 32" in reason           # 原读数不能被吞掉
    assert verdict == pwin.VERDICT_NO_INCREMENT

    # 没被截断时不该多印一句：两个数相同，标注就是噪声。
    _, plain, _ = pwin._incremental_verdict(
        0.0001, -0.0002, 3,
        truncated_days=72, evaluated_days=322, n_eff_reliable=True, hac_lag=2)
    assert "NW 读出" not in plain


def test_diluted_mean_shows_the_contrast_day_average():
    """`mean` 是「有对照的 k 天」和「恒零的 n-k 天」的混合均值，两个都要印。

    `d_i ≡ 0` 的那些天两个预测器逐位相同，把 6 天的日均摊成 1/43 之后，
    屏幕上那个 `+0.00009` 已经不代表任何一天的平均改善。`mean × n/k` 精确等于
    有对照那几天的日均（那些 0 是真 0，不是抽样），所以换算不需要估计。
    """
    mean, lcb = 0.00009, -0.00014
    _, reason, _ = pwin._incremental_verdict(
        mean, lcb, 3, truncated_days=72, evaluated_days=322,
        n_eff_reliable=True, hac_lag=19)
    per = mean * 322 / 72
    assert f"{mean:+.5f}" in reason         # 摊薄值还在
    assert f"{per:+.5f}" in reason          # 但旁边就是 72 天的日均
    assert "只看有对照的 72 天" in reason


# ---------------------------------------------------------------------------
# 分辨率：门槛之下还有多宽一段看不见（§9.27）
# ---------------------------------------------------------------------------

def _seq(n: int, wins: int, total: int = 20, horizon: int = 20) -> list[dict]:
    """连续 `n` 个**真实递增**的日期。`_series` 的 `2026-01-{i:02d}` 过 31 就串位，
    而 `_visible` 的回退分支按 `as_of` 排序数间隔——串位的序列会让「160 天」
    悄悄变成一团乱序，分辨率自然不随天数下降。"""
    d0 = date(2026, 1, 1)
    rows = []
    for i in range(n):
        day = (d0 + timedelta(days=i)).isoformat()
        rows.extend(_row(day, 1 if k < wins else 0, horizon=horizon)
                    for k in range(total))
    return rows




def test_mde_is_threshold_plus_2_486_se():
    """`mde_incremental` 必须由 `se_incremental` 唯一决定，不能各算各的。

    `lcb = mean - 1.645·se`，`PASS` 要求 `lcb > 0.001`，所以要 80% 功率就得
    `μ ≥ 0.001 + 2.486·se`。这两个数必须同源：换核、换滞后都会改 `se`，
    分辨率要跟着动，否则读者会拿着一份旧的分辨率去读一个新的检验。
    """
    wf = pwin.walk_forward(_series(60, 14), window=20, kappa=20.0)
    assert wf["truncated_any"] is True
    se = wf["se_incremental"]
    assert se is not None and se > 0
    assert wf["mde_incremental"] == pytest.approx(
        pwin.MIN_INCREMENTAL_EFFECT + 2.486 * se, abs=5e-7)


def test_mde_is_absent_when_the_test_did_not_run():
    """没跑检验就没有分辨率——`None` 不是「分辨率等于门槛」。

    40 日账本 + `window=250`：窗口盖住全部已可见日，两个估计量恒等，
    这时印一个「能看见 0.0042」的数，读者会以为检验做了、只是没测出来。
    """
    wf = pwin.walk_forward(_series(40, 14), window=pwin.DEFAULT_WINDOW)
    assert wf["verdict_incremental"] == pwin.VERDICT_NOT_TESTABLE
    assert wf["mde_incremental"] is None

    # 一个可评估日都没有时同样不能有：问题还没轮到问。
    empty = pwin.walk_forward(_series(20, 20), window=250, kappa=20.0)
    assert empty["verdict_incremental"] == pwin.VERDICT_INSUFFICIENT
    assert empty["mde_incremental"] is None


def test_no_increment_reason_carries_its_own_resolution():
    """`NO_INCREMENT` 必须说明「测不出」的边界，否则会被读成「没有」。

    这是 §9.26 的直接后果：`lcb ≤ 0` 在平稳过程里是结构性的，句子如果只说
    「没测出增量」，读者无法区分「确实没有」和「这个样本看不见」。
    """
    wf = pwin.walk_forward(_series(60, 14), window=20, kappa=20.0)
    assert wf["verdict_incremental"] == pwin.VERDICT_NO_INCREMENT
    mde = wf["mde_incremental"]
    assert mde is not None
    reason = wf["reason_incremental"]
    assert f"{mde:.6f}" in reason                     # 分辨率本身
    assert f"{mde / pwin.MIN_INCREMENTAL_EFFECT:.1f} 倍" in reason
    assert "分辨不出来" in reason

    # 不给 mde 时句子保持原样：这个参数是加法，不是替换。
    # 必须给 n_eff，否则走的是 `UNKNOWN` 分支（那句的措辞是「不可判定」）。
    _, plain, _ = pwin._incremental_verdict(
        -0.002, -0.004, n_eff=100, n_eff_reliable=True)
    assert "没测出增量" in plain
    assert "真实改善需" not in plain


def test_mde_shrinks_when_the_ledger_grows():
    """这是「还差多远」的正确问法：攒样本降低的是分辨率，不是倒计时。

    §9.25 算的「还差 18 个交易日」是到达可判定（`n_eff ≥ 30`）的日期；
    可判定之后能不能过，由 `mde` 决定。它随 `√n` 下降，但下限是门槛本身
    （`MIN_INCREMENTAL_EFFECT`）——所以它永远不会降到 0。
    """
    short = pwin.walk_forward(_seq(60, 14), window=20, kappa=20.0)
    long = pwin.walk_forward(_seq(240, 14), window=20, kappa=20.0)
    assert short["mde_incremental"] is not None and long["mde_incremental"] is not None
    assert long["mde_incremental"] < short["mde_incremental"]
    assert long["mde_incremental"] > pwin.MIN_INCREMENTAL_EFFECT


def test_estimate_forwards_the_resolution():
    """`estimate()` 是面板读的那个口子：分辨率必须跟着 `reason_incremental` 一起到。"""
    est = pwin.estimate(_series(60, 14), window=20, kappa=20.0)
    wf = pwin.walk_forward(_series(60, 14), window=20, kappa=20.0)
    assert est["mde_incremental"] == wf["mde_incremental"]


# ---------------------------------------------------------------------------
# 设计效应的原料
# ---------------------------------------------------------------------------


def test_daily_series_is_opt_in_and_aligned():
    """`daily_hits` / `daily_picks` 与 `evaluated` 同序、同长，且默认不返回。

    设计效应只能用这条序列量：`n_eff` 是 NW 在**残差**上的读数，而 DEFF 问的
    是 `y` 本身。没有它，「600 条 pick 等于几个独立观测」只能靠假设。
    """
    wf = pwin.walk_forward(_series(60, 14), window=20, kappa=20.0)
    assert wf["daily_hits"] is None and wf["daily_picks"] is None

    wf = pwin.walk_forward(_series(60, 14), window=20, kappa=20.0, with_daily=True)
    assert len(wf["daily_hits"]) == wf["oos_days"] == len(wf["daily_picks"])
    # 每天 20 条 pick，命中 14 条——序列必须复现这个比例，而不是另一条口径。
    assert set(wf["daily_picks"]) == {20}
    assert all(h == 14 for h in wf["daily_hits"])


def test_daily_series_absent_when_insufficient():
    """样本不足时是 `None`，不是空列表：空列表会被读成「一天都没评估」。"""
    wf = pwin.walk_forward(_day("2026-01-01", 20), with_daily=True)
    assert wf["verdict"] == pwin.VERDICT_INSUFFICIENT
    assert wf["daily_hits"] is None and wf["daily_picks"] is None
