"""判决必须是**一个分区**，且必须与 `significance_of()` 同轴。

改动前的判据是 `skill > 0` 这一个谓词。代价在真实账本上是可复现的：
同一批 97 笔结算，abs 口径同时给出

    skill=+0.55%  skill_lcb=−5.09%  significance=UNKNOWN  verdict=BEATS_CONSTANT

四个字段一起返回，读者只会读那个绿的。这不是口径之争，是**同一个函数
自己算出「不可判定」，却仍然输出了一个结论性的措辞**。

这一组钉五件事：

1. `verdict_of()` 是分区——互斥、穷尽、五格都可达；
2. 点估计再高，`n_eff < MIN_NEFF` 也只能是 UNKNOWN（A1）；
3. `KILL` 与 `NOT_ESTABLISHED` 必须分叉（前者已证伪，后者未成立）；
4. 分组判决不许比总分更乐观——组越小 `n_eff` 越低，它最容易假绿；
5. 真实账本重放：97 笔不再判 BEATS_CONSTANT。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.decision import settle

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "data" / "user_data" / "decision" / "settlements.jsonl"

ALL_VERDICTS = {
    settle.VERDICT_INSUFFICIENT,
    settle.VERDICT_UNKNOWN,
    settle.VERDICT_BEATS,
    settle.VERDICT_NO_INFORMATION,
    settle.VERDICT_NOT_ESTABLISHED,
    settle.VERDICT_KILL,
}


def _rows(days: int, hits: int, p: float, *, source: str = "ledger",
          offset: int = 0) -> list[dict]:
    """`days` 个决策日、每日一笔。`hits` 是命中的天数（结果放前 `hits` 天）。

    结果按「先全中后全错」排，是为了让日度改善序列有**非零方差**：
    若把命中打散，日度均值接近常数，`se` 会退化到 0，下界读数失去意义。

    `offset` 让两组样本落在**不重叠**的日期上——否则总分与分组数的是同一批
    决策日，`n_eff` 不会因为分组而变小，第 4 条用例就钉不住任何东西。
    """
    start = date(2026, 1, 1) + timedelta(days=offset)
    out = []
    for i in range(days):
        y = 1 if i < hits else 0
        out.append({
            "as_of": (start + timedelta(days=i)).isoformat(),
            "p_win": p,
            "p_win_source": source,
            "outcome_med": y,
            "outcome": y,
            "exit": {"ret": 0.01 if y else -0.01},
        })
    return out


def test_six_verdicts_are_all_reachable() -> None:
    """六格都可达。一格不可达，说明它写错了或者永远判不出来。"""
    cases = {
        settle.VERDICT_INSUFFICIENT: _rows(3, 3, 0.9),
        settle.VERDICT_UNKNOWN: _rows(10, 10, 0.9),
        settle.VERDICT_BEATS: _rows(40, 40, 0.9),
        # p 恒等于 0.5 ⇒ brier 恰好等于 0.25 ⇒ skill 恰好 0。
        # 这一格的分母与基线完全相同，是唯一能精确落在零点上的构造。
        settle.VERDICT_NO_INFORMATION: _rows(40, 20, 0.5),
        settle.VERDICT_NOT_ESTABLISHED: _rows(40, 25, 0.6),
        settle.VERDICT_KILL: _rows(40, 0, 0.9),
    }
    seen = {}
    for expected, rows in cases.items():
        got = settle.score(rows, basis="median")["verdict"]
        seen[expected] = got
        assert got == expected, f"{expected} 那一格落到了 {got}"
    assert set(seen.values()) == ALL_VERDICTS


def test_high_skill_below_min_neff_cannot_beat_constant() -> None:
    """A1：点估计 +96%，但 `n_eff=10 < 30` ⇒ 只能是 UNKNOWN。

    这是本次改动唯一真正改变行为的一格。旧实现下它判 BEATS_CONSTANT——
    而且是在四个字段并列返回、其中两个说「不知道」的情况下。
    """
    out = settle.score(_rows(10, 10, 0.9), basis="median")
    assert out["skill"] > 0.9, "构造必须让点估计足够漂亮，否则钉不住东西"
    assert out["n_eff"] == 10
    assert out["significance"] == settle.SIGNIFICANCE_UNKNOWN
    assert out["verdict"] == settle.VERDICT_UNKNOWN, (
        "样本不足时给出结论性措辞，等于把「还没测」写成「测出来是好的」")
    assert out["verdict"] != settle.VERDICT_BEATS


def test_kill_and_not_established_are_different_cells() -> None:
    """两者后续动作相反：一个是停用，一个是继续攒。

    合成一格（比如都叫 NO_INFORMATION）会让「点估计为正但下界为负」
    这段最常见的读数无处安放，然后被就近读成「死了」。
    """
    # 点估计 +6%、下界 −4% ⇒ 未成立
    weak = settle.score(_rows(40, 25, 0.6), basis="median")
    assert weak["skill"] > 0
    assert weak["skill_lcb"] < 0
    assert weak["verdict"] == settle.VERDICT_NOT_ESTABLISHED

    # 过度自信（p=0.8）但命中率只有 55% ⇒ 点估计明确为负，下界也为负
    dead = settle.score(_rows(40, 22, 0.8), basis="median")
    assert dead["skill"] < 0
    assert dead["skill_lcb"] < 0
    assert dead["verdict"] == settle.VERDICT_KILL


def test_breakdown_verdict_shares_the_same_predicate() -> None:
    """分组是诊断，不是结论。它自己的 `n_eff` 只会更小。

    若分组另写一份 `skill > 0`，一个各 20 天、skill 都很漂亮的分组会双双判
    BEATS_CONSTANT，而总分因为 `n_eff=40` 才勉强判赢——同一份样本里最小的
    那两格在替整份账本下结论。
    """
    rows = (_rows(20, 20, 0.9, source="ledger")
            + _rows(20, 20, 0.9, source="prior", offset=20))
    out = settle.score(rows, basis="median")
    assert out["verdict"] == settle.VERDICT_BEATS, "总分 40 天应当能判赢"
    assert set(out["by_source"]) == {"ledger", "prior"}
    for name, grp in out["by_source"].items():
        assert grp["n_eff"] < settle.MIN_NEFF, (name, grp["n_eff"])
        assert grp["verdict"] != settle.VERDICT_BEATS, (
            f"分组 {name} 的 n_eff={grp['n_eff']} 不足，却判了 BEATS_CONSTANT")
        assert grp["verdict"] in ALL_VERDICTS


def test_old_predicate_would_have_said_beats(monkeypatch) -> None:
    """把旧判据装回去，这一格必须变绿——否则上面那些断言是恒真的。

    旧实现是 `elif skill is not None and skill > 0: BEATS_CONSTANT`：不看
    `skill_lcb`，也不看 `n_eff`。这条用例存在的意义不是测旧代码，是**证明
    新用例能分开两个实现**。分开不了，就说明 A1 钉住的不是行为，只是一句话。
    """
    def old(*, n, skill, skill_lcb, n_eff, n_eff_reliable, baseline):
        if n < settle.MIN_N:
            return settle.VERDICT_INSUFFICIENT, "old"
        if skill is not None and skill > 0:
            return settle.VERDICT_BEATS, "old"
        return "NO_INFORMATION", "old"

    rows = _rows(10, 10, 0.9)
    monkeypatch.setattr(settle, "verdict_of", old)
    under_old = settle.score(rows, basis="median")
    monkeypatch.undo()
    under_new = settle.score(rows, basis="median")

    assert under_old["n_eff"] == under_new["n_eff"] == 10
    assert under_old["skill"] == under_new["skill"], "两种实现必须读同一批数字"
    assert under_old["verdict"] == settle.VERDICT_BEATS
    assert under_new["verdict"] == settle.VERDICT_UNKNOWN


def test_real_ledger_no_longer_beats_constant() -> None:
    """真实数据重放：97 笔、`n_eff=10`，abs 口径曾判 BEATS_CONSTANT。"""
    if not LEDGER.exists():
        pytest.skip("本机没有决策账本结算流水")
    rows = [json.loads(line) for line in
            LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
    settled = [r for r in rows if r.get("outcome") is not None]
    if len(settled) < settle.MIN_N:
        pytest.skip(f"已结算 {len(settled)} < {settle.MIN_N}，样本不足以钉这条")

    for basis in ("abs", "median"):
        out = settle.score(rows, strategy_id="l1_gap_reversal", basis=basis)
        assert out["n_eff"] < settle.MIN_NEFF, (
            f"{basis}: n_eff={out['n_eff']} 已越过门槛，这条断言的前提不存在了——"
            f"此时应当改判，而不是继续要求 UNKNOWN")
        assert out["verdict"] == settle.VERDICT_UNKNOWN, basis
        assert out["verdict"] != settle.VERDICT_BEATS, basis
