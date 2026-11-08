"""计分能不能回答「这些行是按什么价格口径算出来的」。

结算在事件表缺位时会如实降级（`ADJUSTED` / `RAW`），并把实际口径写进每一行
的 `price_basis`。但 `score()` 只按 `outcome` 非空筛样本，不看这一列：降级行
与含分红的行混在同一个 Brier / skill 里，分数上再也看不出掺了几条。

三个用例钉三件事：分组计数、旧行没有这一列时归 `unknown`（不假装知道）、
以及**空样本与非空样本的键集必须一致**——否则读的人只能靠 `.get()` 猜。
"""
from __future__ import annotations

from app.decision import settle


def _row(as_of: str, *, outcome: int, ret: float,
         price_basis: str | None) -> dict:
    row = {
        "as_of": as_of,
        "p_win": 0.6,
        "p_win_source": "ledger",
        "outcome": outcome,
        "exit": {"ret": ret},
    }
    if price_basis is not None:
        row["price_basis"] = price_basis
    return row


def test_score_separates_event_rows_from_degraded_rows():
    rows = [
        _row("2026-01-05", outcome=1, ret=0.05, price_basis="EVENT"),
        _row("2026-01-06", outcome=0, ret=-0.02, price_basis="EVENT"),
        _row("2026-01-07", outcome=1, ret=0.03, price_basis="ADJUSTED"),
    ]
    out = settle.score(rows, basis="abs")

    assert out["n"] == 3
    assert {k: v["n"] for k, v in out["by_price_basis"].items()} == {
        "EVENT": 2, "ADJUSTED": 1}
    assert out["n_non_event"] == 1
    # 分组自己也带分数：含分红的那组与掺了降级行的那组分得开。
    assert out["by_price_basis"]["ADJUSTED"]["hit_rate"] == 1.0
    assert out["by_price_basis"]["EVENT"]["hit_rate"] == 0.5
    # 两个 "basis" 是不同的轴：`basis` 选因变量，`by_price_basis` 分收益口径。
    assert out["basis"] == "abs"


def test_rows_without_the_column_are_unknown_not_event():
    rows = [
        _row("2026-01-05", outcome=1, ret=0.05, price_basis=None),
        _row("2026-01-06", outcome=0, ret=-0.02, price_basis="EVENT"),
        # 第二行 EVENT 不是凑数：只有 1 行 EVENT 时 `!=` 与 `==` 都得 1，
        # 变异体存活（构造恰好对称）。两行 EVENT 让两个算子分叉（1 vs 2）。
        _row("2026-01-07", outcome=1, ret=0.01, price_basis="EVENT"),
    ]
    out = settle.score(rows, basis="abs")
    assert out["by_price_basis"]["unknown"]["n"] == 1
    assert out["n_non_event"] == 1
    # 恒等式：这两个数走两条独立路径（逐行 `!=` 计数 vs 分组 `==` 计数）。
    # 将来改分组逻辑（把 None 并进 EVENT、加白名单、改 missing 标签）会分叉。
    assert out["n_non_event"] == out["n"] - out["by_price_basis"]["EVENT"]["n"]


def test_empty_and_non_empty_scores_have_the_same_keys():
    """键集随分支漂移，读的人只能靠 `.get()` 猜——账本里已经为这件事写过一次注。"""
    empty = settle.score([], basis="abs")
    one = settle.score([_row("2026-01-05", outcome=1, ret=0.05,
                            price_basis="EVENT")], basis="abs")
    assert set(empty) == set(one)
    assert empty["by_price_basis"] == {}
    assert empty["n_non_event"] == 0
    # 单行 EVENT ⇒ 非 EVENT 必须是 0。上面那条用例里 3 行中 1 行非 EVENT，
    # 把 `!=` 翻成 `==` 也只得 1；这一行让同一个变异在"全 EVENT"处现形。
    assert one["n_non_event"] == 0
    assert one["n_non_event"] == one["n"] - one["by_price_basis"]["EVENT"]["n"]
