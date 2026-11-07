"""`excess_lcb` 的口径回归 — 同一个 `score()` 里两个下界不能有两个方差约定。

背景（2026-11-06 评审 §七）：`skill_lcb` 走 `_daily_clustered_lcb`（按决策日
聚类 + Newey-West），而**同一个函数**里的 `excess_lcb` 走 `_lcb`（逐 pick
iid）。`hac_lcb` 的文档自己写着：20 日持有期让相邻决策日重叠 19 天，按 iid
算标准误会把区间收窄 2.3–3.4 倍（323 个可评估日 / 20 日上的两条重叠序列，
`data/.scratch/pwin_variance.json`）。
再加同一张卡的 k 个 pick 共享当天行情，低估还有 √k 这一层。

`excess_lcb` 不驱动任何判定（verdict 走 Brier skill、significance 走
`skill_lcb`），只在前端显示。所以修法是**增字段不换语义**：
`excess_se_hac` / `excess_lcb_hac` 与 `skill_lcb` 同口径，旧字段保留。

本测试钉三件事：
1. 同日聚簇 + 持有期重叠存在时，HAC 下界必须显著低于 iid 下界；
2. 无聚簇、无重叠（每日 1 笔、H=1）时两者必须一致——改口径不改读数；
3. 缺 `as_of` 的旧账本给 `None` 而不是一个算不出来的数（fail-closed）。
"""
from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.decision import settle

REPO = Path(__file__).resolve().parents[2]


def _ledger(n_days: int, picks: int, horizon: int, *, seed: int = 3,
            common_sd: float = 0.012, noise_sd: float = 1e-4) -> list[dict]:
    """每日 `picks` 笔、同日共享一个 `common` 成分（模拟同一个行情日）。

    `common` 走一条平滑的随机游走，因此逐日序列**自相关**；`horizon` 只影响
    `_overlap_lag`。两者都在 iid 口径里被当成独立噪声。
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    level = 0.004
    for i in range(n_days):
        level += rng.gauss(0, common_sd)
        d = (date(2025, 6, 2) + timedelta(days=i)).isoformat()
        for _ in range(picks):
            exc = level + rng.gauss(0, noise_sd)
            rows.append({
                "as_of": d,
                "horizon_sessions": horizon,
                "p_win": 0.55,
                "outcome": 1,
                "outcome_med": 1,
                "excess": exc,
                "excess_med": exc,
                "fill": {"status": "FILLED"},
                "exit": {"ret": 0.05 + exc},
                "logloss": 0.5,
            })
    return rows


def _se_from_iid_lcb(out: dict) -> float:
    """从 `excess_lcb` 反解 `_lcb` 用的 iid 标准误。"""
    return (out["excess_mean"] - out["excess_lcb"]) / settle.Z90


def test_hac_lowers_the_bound_when_picks_cluster_and_horizons_overlap():
    out = settle.score(_ledger(60, picks=5, horizon=20))
    assert out["excess_lcb_hac"] is not None
    assert out["excess_lcb"] is not None
    # 5 笔/日 ⇒ 只这一层就值 √5 ≈ 2.24 倍的方差收缩；重叠再加一层。
    ratio = out["excess_se_hac"] / _se_from_iid_lcb(out)
    assert ratio > 2.0, f"HAC/iid se 比只有 {ratio:.2f}，聚簇没有被计入"
    assert out["excess_lcb_hac"] < out["excess_lcb"]


def test_excess_block_cannot_move_the_judgment_chain():
    """把超额整列清零，判定链上的每一个数都必须逐位不动。

    这比「断言 verdict 等于某个值」更强：它证明新增的两行只读了 `excess`，
    没有碰到 `p_win` / `outcome` / 样本量，因此不可能改变任何结论。
    """
    rows = _ledger(60, picks=5, horizon=20)
    a = settle.score(rows)
    assert a["excess_mean"] == round(
        sum(float(r["excess"]) for r in rows) / len(rows), 4)
    for r in rows:
        r["excess"] = 0.0
        r["excess_med"] = 0.0
    b = settle.score(rows)
    for key in ("verdict", "skill", "skill_lcb", "significance",
                "n_eff", "hac_lag", "brier", "hit_rate"):
        assert a[key] == b[key], f"{key} 被超额列影响了"
    assert a["excess_lcb_hac"] != b["excess_lcb_hac"]


def test_hac_matches_iid_without_clustering_or_overlap():
    """每日 1 笔、H=1 时两层相关都不存在，两个下界只差 (n−1)/n 的方差约定。"""
    out = settle.score(_ledger(60, picks=1, horizon=1, noise_sd=0.01))
    se_iid = _se_from_iid_lcb(out)
    assert out["excess_se_hac"] is not None
    assert math.isclose(out["excess_se_hac"], se_iid, rel_tol=0.05)


def test_missing_as_of_fails_closed():
    """旧账本没有 `as_of`：只聚得出一天，给 None 而不是一个假的窄区间。"""
    rows = _ledger(30, picks=4, horizon=20)
    for r in rows:
        r.pop("as_of")
    out = settle.score(rows)
    assert out["excess_lcb"] is not None        # 旧字段照旧（iid）
    assert out["excess_lcb_hac"] is None        # 新字段不猜
    assert out["excess_se_hac"] is None


def _load_probe(name: str, rel: str):
    """加载 `scripts/` 下的探针。它们互相 import，得临时把 scripts 放进 path。"""
    scripts = str(REPO / "scripts")
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    added = scripts not in sys.path
    if added:
        sys.path.insert(0, scripts)
    try:
        spec.loader.exec_module(mod)
    finally:
        if added:
            sys.path.remove(scripts)
    return mod


def _ma_series(seed: int, n: int = 400, h: int = 20) -> list[float]:
    rng = random.Random(seed)
    base = [rng.gauss(0.0, 1.0) for _ in range(n)]
    return [sum(base[i:i + h]) / h for i in range(n - h + 1)]


def test_probe_delegates_hac_to_production():
    """探针的 HAC 必须**就是**生产的那一个（13h：它曾经是 Bartlett）。

    守的是「第二份实现会漂移」：本探针自带过一份 Bartlett 估计量（`γ_k` 分母
    `n`、floor `max(lr, 1e-18)`），`se` 与生产不同源——实测 8 条序列里 7 条偏窄
    0.7–17.5%、1 条反而偏宽 10.1%（`γ_20<0` 时截断核压低 LRV），所以连「偏窄」
    都不是恒成立的；而它算出的「按 iid 算会窄多少倍」被 `pwin.py` / `settle.py`
    三处 docstring 引用成实测值。
    逐位相等而不是 `isclose`：两边调的是同一个函数，任何偏差都是又分叉了。
    """
    probe = _load_probe("probe_pwin_variance", "scripts/probe_pwin_variance.py")
    xs = _ma_series(11)
    # 第一条不传 kernel：探针的**默认路径**必须等于生产的默认路径。
    # 只比显式传参的那两条抓不到「探针默认核被改回 bartlett」。
    lcb, infl = probe._hac_stats(xs, 19)
    _, se, prod_lcb, se_iid = settle.hac_lcb(xs, 19)
    assert lcb == prod_lcb
    assert infl == se / se_iid
    # 诊断列同样委托生产，只是换核。
    lcb, infl = probe._hac_stats(xs, 19, settle.KERNEL_BARTLETT)
    _, se, prod_lcb, se_iid = settle.hac_lcb(xs, 19, kernel=settle.KERNEL_BARTLETT)
    assert lcb == prod_lcb
    assert infl == se / se_iid


def test_production_default_kernel_is_truncated():
    """生产默认核必须是截断——换回 Bartlett 会让 `se` 悄悄小 20%。

    这条堵的是上一条的盲区：探针与生产「一起」改成 Bartlett 时，逐位相等的
    断言照样通过。只有在这里钉住**默认核的行为**，才看得见那次改动。
    """
    xs = _ma_series(12)
    se_default = settle.hac_lcb(xs, 19)[1]
    se_bartlett = settle.hac_lcb(xs, 19, kernel=settle.KERNEL_BARTLETT)[1]
    assert se_default > se_bartlett * 1.05, (
        f"截断核只比 Bartlett 宽 {se_default / se_bartlett:.3f} 倍，"
        "默认核可能被换回 bartlett")


def test_probe_artifact_matches_its_own_summary():
    """docstring 里那两个倍数从产物抄——产物必须自洽，否则抄的是手写数。"""
    path = REPO / "data" / ".scratch" / "pwin_variance.json"
    if not path.exists():
        pytest.skip("落盘产物不在（新克隆 / CI），由本地重跑探针生成")
    data = json.loads(path.read_text(encoding="utf-8"))
    infls = [r["d_se_infl"] for r in data["rows"]]
    assert data["summary"]["se_infl_min"] == round(min(infls), 3)
    assert data["summary"]["se_infl_max"] == round(max(infls), 3)
    for r in data["rows"]:
        # infl² = LRV/γ0；= 1 表示截断核的 floor(max(lr, γ0)) 绑定了。
        assert r["d_lr_over_g0"] == pytest.approx(r["d_se_infl"] ** 2)
        assert r["days"] == data["summary"]["days"][0]
