"""`analysis/_artifact_guard.py` 的守门测试：让「产物被守」进入 pytest 射程。

为什么挂在这里而不是只留 analysis 下的脚本：`_test_artifact_guard.py` 的文件名以 `_` 开头，
pytest 不收集，于是「守门器绿」会退化成「我手动跑过」。这个文件把它拉进套件，
并额外断言盘上所有 `analysis/*_result.json` 都干净。

依赖方向：backend/tests → analysis/（与 test_eval_protocol.py 相同的 sys.path 约定）。
两者必须同批提交，否则克隆后 import 失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
if str(ANALYSIS) not in sys.path:
    sys.path.insert(0, str(ANALYSIS))

from _artifact_guard import violations  # noqa: E402
import _test_artifact_guard as cases  # noqa: E402


@pytest.mark.parametrize(
    "name,desc,obj,want", cases.CASES, ids=[c[0] for c in cases.CASES])
def test_guard_case(name: str, desc: str, obj: dict, want: bool) -> None:
    """每条规则配一份故意错的输入：该红的必须红，该绿的不许红。"""
    got = violations(obj)
    assert bool(got) == want, f"{name}（{desc}）期望 red={want}，实得 {got}"


def test_leg_cover_red_on_wrong_leg() -> None:
    """D9：旧不变量 2 在错腿上仍然通过；换成能失败的版本后必须红。"""
    _, want, got = cases.case_leg_cover_wrong_leg()
    assert got == want


def test_leg_cover_green_on_counts() -> None:
    _, want, got = cases.case_leg_cover_good()
    assert got == want


def test_old_invariant_was_a_tautology() -> None:
    """`|A\\C|−|C\\A| ≡ |A|−|C|`：2,000 组无关随机集合 0 次报错 ⇒ 它不可能是护栏。"""
    _, want, got = cases.case_old_invariant_is_tautology()
    assert got == want


def test_disk_artifacts_clean() -> None:
    _, want, got = cases.case_disk_clean()
    assert got == want, "盘上存在散文/镜像键/符号不一致的产物"


def test_guard_scans_at_least_30_artifacts() -> None:
    """防止「全绿」其实是「一个都没扫」。"""
    n = len(list(ANALYSIS.glob("*_result.json")))
    assert n >= 30, f"只找到 {n} 个产物，守门器没覆盖到东西"


def test_d8_shape_is_rejected_directly() -> None:
    """D8 的原始字节形状：同一 dict 两个镜像键都是 +0.1519。"""
    art = {
        "minute_null": {"implied_centroid": {
            "median_C_minus_C_eff": 0.1519,
            "median_C_eff_minus_C": 0.1519,
            "share_C_eff_gt_C": 0.645,
        }}}
    v = violations(art)
    assert len(v) == 2, v           # 镜像键 + share 符号，两条独立规则都要报
    assert any("镜像键不反号" in x for x in v)
    assert any("符号与 share" in x for x in v)


def _dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _dicts(v)


# ---------------------------------------------------------------- D13
ENTRY = "_diag_l1_vwap_entry_result.json"
GAPSET = "_diag_l1_gapset_audit_result.json"
CURV = "_diag_l1_vwap_curvature_result.json"

_DECLARED = {"cell", "cells", "day", "days", "in_band", "median", "p10", "p90",
             "gap", "winsor", "sd", "slope", "intercept"}


def _load(name: str) -> dict:
    return json.loads((ANALYSIS / name).read_text(encoding="utf-8"))


def undeclared_weighting(obj) -> list[str]:
    """含计数键的 dict 里出现**不带定义域/权重 token** 的 `*_bp` 均值键 ⇒ 违例。

    D13 的形状：同一产物里 `gap_pool_unselected.open_entry_bp=45.7`（逐日）与
    `gap_pool_by_window.full.open_entry_bp=43.8`（cell）是同一批 cell 的两个权重。
    """
    bad: list[str] = []
    for d in _dicts(obj):
        if "n_cells" not in d:
            continue
        for k, v in d.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            if k.endswith("_bp") and not (_DECLARED & set(k.split("_"))):
                bad.append(k)
    return sorted(set(bad))


def test_undeclared_weighting_is_rejected() -> None:
    """这条不变量必须在 `6442a67` 的形状上红，否则它只是注释。"""
    old_shape = {"gap_pool_unselected": {"n_cells": 243584, "open_entry_bp": 45.7},
                 "gap_pool_by_window": {"full": {"n_cells": 243584, "open_entry_bp": 43.8}}}
    assert undeclared_weighting(old_shape) == ["open_entry_bp"]


def test_entry_declares_weighting() -> None:
    assert undeclared_weighting(_load(ENTRY)) == []


def test_entry_pool_matches_gapset_caliber() -> None:
    """entry 的池统计必须与 gapset 的集合口径**逐位**相同：两个脚本、同一个对象。"""
    e = _load(ENTRY)["gap_pool_unselected"]
    g = _load(GAPSET)["sets"]
    assert e["caliber"] == "C"
    c = g["C_cal_adj"]
    assert e["n_cells"] == c["cells"], "C 池 cell 数"
    assert e["open_entry_day_bp"] == c["i1_daily_equal_bp"]
    assert e["open_entry_cell_bp"] == c["i1_cell_equal_bp"]
    a, la = g["A_shift_raw"], e["a_legacy"]
    assert la["n_cells"] == a["cells"], "A 对照块 cell 数"
    assert la["open_entry_day_bp"] == a["i1_daily_equal_bp"]
    assert la["open_entry_cell_bp"] == a["i1_cell_equal_bp"]


def test_entry_vwap_stats_match_gapset_screen_block() -> None:
    """VWAP 侧的 8 个数与 gapset 的 `pool_mean_caliber.C_screen` 逐位相同。

    这是 D13 里最容易再错的一格：均值、中位、逐日等权**各自有两个定义域**，
    gapset 把筛前/筛后都存在同一个 dict 里，所以对拍必须逐键指名。
    """
    e = _load(ENTRY)["gap_pool_unselected"]
    c = _load(GAPSET)["pool_mean_caliber"]["C_screen"]
    assert e["n_cells"] == c["cells"]
    assert e["n_cells_in_band"] == c["cells_after_screen"]
    assert e["n_cells_out_of_band"] == c["out_of_band"]
    assert e["vwap_entry_cell_bp"] == c["iv_mean_after_screen_bp"]
    assert e["vwap_entry_day_bp"] == c["iv_daily_equal_after_screen_bp"]
    assert e["vwap_entry_cell_median_in_band_bp"] == c["iv_median_after_screen_bp"]
    assert e["vwap_entry_cell_median_all_cells_bp"] == c["iv_median_bp"]
    assert e["vwap_unfiltered"]["cell_bp"] == c["iv_mean_bp"]
    assert e["vwap_unfiltered"]["day_bp"] == c["iv_daily_equal_bp"]
    # 两个中位只差 0.005bp：两位小数把它们显示成 −13.74 / −13.75，
    # 读成「筛选中位动了」就是第 7 个假分歧。
    assert 0 < e["vwap_entry_cell_median_abs_gap_bp"] < 0.01


def test_entry_full_window_equals_pool_block() -> None:
    """分窗的 full 与全池汇总必须是同一批数（防展平时漏键/换集合）。"""
    e = _load(ENTRY)
    w = e["robustness"]["gap_pool_by_window"]["full"]
    p = e["gap_pool_unselected"]
    for k in ("n_cells", "n_cells_in_band", "n_days", "open_entry_cell_bp",
              "open_entry_day_bp", "vwap_entry_cell_bp", "vwap_entry_day_bp"):
        assert w[k] == p[k], k


def test_entry_ratios_match_curvature_all_cells() -> None:
    """`pen_over_i1_all_cells` 与 curvature 的 `ratio_of_means` 是同一估计量。"""
    e = _load(ENTRY)["robustness"]["gap_pool_by_window"]
    c = _load(CURV)["pen_slope"]
    assert e["full"]["pen_over_i1_all_cells"] == c["gap_pool"]["ratio_of_means_pen_over_i1"]
    for y in ("2025", "2026"):
        assert (e[y]["pen_over_i1_all_cells"]
                == c["gap_pool_by_year"][y]["ratio_of_means_pen_over_i1"]), y
    # in_band 版是另一个定义域，差必须小到不改结论、又大到不能被当成同一个数
    assert e["full"]["pen_over_i1_in_band"] != e["full"]["pen_over_i1_all_cells"]


def test_entry_depth_table_is_reference_not_producer() -> None:
    """深度四档只允许一个产地（curvature）；entry 只留引用块。"""
    p = _load(ENTRY)["gap_pool_unselected"]
    assert "by_gap_depth" not in p, "A 口径的深度表又回来了"
    ref = p["gap_pool_by_depth"]
    assert ref["role"] == "reference" and ref["caliber"] == "C"
    f, _, key = ref["source"].partition("#")
    rows = _load(f)[key]
    assert len(rows) == 4
    assert sum(r["n"] for r in rows) == ref["n_cells_in_band"] == p["n_cells_in_band"]


def test_cohort_day_state_reconstructs_headline() -> None:
    """按日状态三分位切完，加权必须回到 121.5 —— 否则分档漏了天或换了集合。"""
    e = _load(ENTRY)
    ter = e["cohort_by_pool_size_tercile"]
    days = sum(v["days"] for v in ter.values())
    assert days == e["cohort"]["n_days"] == 316
    recon = sum(v["cohort_i1_day_bp"] * v["days"] for v in ter.values()) / days
    assert abs(recon - e["daily_bp"]["open_entry"]) <= 0.05, (recon, e["daily_bp"]["open_entry"])


def test_day_state_block_is_net_of_cost_consistent() -> None:
    e = _load(ENTRY)
    for k, v in e["cohort_by_pool_size_tercile"].items():
        assert abs(v["cohort_i1_day_bp"] - v["cohort_net_day_after_cost_bp"] - 20.0) < 0.01, k


def test_weighting_probe_matches_entry_pool() -> None:
    """机制探针（第三产地）必须与 entry 的池统计同一批数。"""
    probe = _load("_diag_d13_weighting_gap_result.json")
    e = _load(ENTRY)["gap_pool_unselected"]
    assert probe["panel"]["days"] == e["n_days"]
    assert probe["panel"]["cells"] == e["n_cells"]
    assert probe["panel"]["i1_day_equal_bp"] == e["open_entry_day_bp"]
    assert probe["panel"]["i1_cell_equal_bp"] == e["open_entry_cell_bp"]


def test_weighting_probe_excess_is_difference_not_third_producer() -> None:
    """超额只能是「池 − 非池」的差：三个数写在三个地方，改一个必须红。

    反例（旧字节）：`excess_day_bp` 单独抄成一个常数，其余两处怎么改都不报。
    """
    probe = _load("_diag_d13_weighting_gap_result.json")
    b = probe["benchmark"]
    for lab, v in probe["terciles_by_names"].items():
        assert abs((v["i1_day_bp"] - v["nonpool_day_bp"]) - v["excess_day_bp"]) <= 0.02, lab
    assert abs((b["pool_day_bp"] - b["nonpool_day_bp"]) - b["excess_day_bp"]) <= 0.02
    recon_ex = sum(v["excess_day_bp"] * v["days"]
                   for v in probe["terciles_by_names"].values()) / probe["panel"]["days"]
    assert abs(recon_ex - b["excess_day_bp"]) <= 0.02, recon_ex


def test_weighting_probe_recuts_boundaries_in_bootstrap() -> None:
    """块自举必须真的在每个重采样上重定三分位，而不是沿用样本内边界。

    判据是两版区间的**宽度差**：重定边界后 raw 的区间至少要宽 10bp。
    若有人把 `_fixed_cuts` 那版当成唯一口径（或直接复制过去），差为 0 ⇒ 红。
    """
    c = _load("_diag_d13_weighting_gap_result.json")["benchmark"]["contrast_mid_vs_ends"]
    lo, hi = c["raw_ci95_block20"]
    flo, fhi = c["raw_ci95_block20_fixed_cuts"]
    assert (hi - lo) - (fhi - flo) >= 10.0, (hi - lo, fhi - flo)
    # 结论就挂在这条差上：固定边界不含 0，重定边界含 0 ⇒ 「U 形」不可判。
    assert flo <= c["raw_bp"] <= fhi
    assert lo <= 0 <= hi, "重定边界后仍然显著 —— 结论要重写，不是改测试"
    # 超额在两个基准上都必须跨 0（非池 / 全市场），否则「日状态无关」要重写
    for key in ("excess_ci95_block20", "excess_vs_allmarket_ci95_block20"):
        a, z = c[key]
        assert a < 0 < z, key


def test_cohort_carries_both_lenses() -> None:
    """cohort 必须同时给「原始 EV」与「减非池股」两个镜头，且各自能重建回总体。

    只给原始 EV 时，中段的塌陷会被读成策略性质；只给超额时，成本比较没有对象。
    """
    e = _load(ENTRY)
    ter = e["cohort_by_pool_size_tercile"]
    days = sum(v["days"] for v in ter.values())
    for lab, v in ter.items():
        # 三个数写在三个地方：改一个必须红（与池侧同一条纪律）
        assert abs((v["cohort_i1_day_bp"] - v["nonpool_day_bp"])
                   - v["cohort_excess_day_bp"]) <= 0.02, lab
    recon_ex = sum(v["cohort_excess_day_bp"] * v["days"] for v in ter.values()) / days
    assert 60 < recon_ex < 140, recon_ex
    # 原始三档是 U 形（中段最低）；超额三档必须**不再**是中段最低 —— 这正是两次「池≠策略」的教训
    raw = [ter[k]["cohort_i1_day_bp"] for k in ("pool_small", "pool_mid", "pool_large")]
    ex = [ter[k]["cohort_excess_day_bp"] for k in ("pool_small", "pool_mid", "pool_large")]
    assert raw[1] == min(raw)
    assert ex[1] != min(ex)


@pytest.mark.parametrize("name", [
    "_diag_l1_gapset_audit_result.json",
    "_diag_l1_vwap_curvature_result.json",
])
def test_prose_declares_provenance(name: str) -> None:
    """本轮改过的两个产物：每段散文都必须留下「模板 + 字段路径」。

    其余 44 个产物只受 R2/R3 覆盖（它们没有 reading_template）——这是已知缺口，
    不是通过；补法是逐个把散文改成模板，见 `_artifact_guard.KNOWN_HOLES`。
    """
    obj = json.loads((ANALYSIS / name).read_text(encoding="utf-8"))
    templated = [d for d in _dicts(obj) if any(k.endswith("_template") for k in d)]
    assert templated, f"{name} 里没找到可核对的散文"
    for d in templated:
        for k in d:
            if not k.endswith("_template"):
                continue
            stem = k[:-len("_template")]
            assert isinstance(d.get(stem), str), (name, stem)
            assert d.get(f"{stem}_fields") or d.get("reading_fields"), (name, stem)
