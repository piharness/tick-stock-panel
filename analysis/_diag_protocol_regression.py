"""五道门回归测试：把已经发生过的假阳性当夹具，把已经发生过的旁路当反例。

验收标准（可证伪）：

端到端（已经发生过的事故必须被判红，且理由对得上）：
    1. weak_board_absorption  +48.4% / Sharpe 0.97   → REJECT，含 selection_dependent
    2. 炸板低吸                +117.5bp / t=7.19      → REJECT，含 per_trade_aggregation
    3. 上榜平盘                -291.1bp / t=-5.08     → REJECT，含 uncontrolled_confound
    4. 换手≥25%（主板）        -160.0bp / t=-2.55     → REJECT，含 multiplicity_fail 与 below_hurdle
    5. 正对照（合成，口径齐全）                        → PASS

单门隔离（每道门必须能被单独打红）：七条，见 `unit`。

旁路反例（每一行都是一个"单行改动就能变绿"的输入，必须被判红）：见 `mutation`。
    1. 事件类型未登记（原先：把三个混杂全声明成事件定义变量）
    2. open_t1 → close_t1 且自报持有 1 天（当日买卖）
    3. n_tests 从 200 改成 1（族系校验失效）
    4. universe 未登记 / MDE 跨宇宙借用
    5. exit 未登记（原先只校验 entry）
    6. 口径齐全但一个数都不报（原先 G4/G5 双双 SKIP 后 PASS）
    7. 自报族系阈值比 Bonferroni 更松
    8. n_tests 缺省 / 买不进占比缺省 / 报年化却不报容量曲线

运行：backend/.venv/Scripts/python.exe analysis/_diag_protocol_regression.py
产物：analysis/_diag_protocol_regression_result.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import eval_protocol as protocol  # noqa: E402
from eval_protocol import (  # noqa: E402
    EVENT_REGISTRY,
    KNOWN_CONFOUNDS,
    Claim,
    bonferroni_threshold,
    evaluate,
    holding_days_between,
    render,
)

RESULT_PATH = HERE / "_diag_protocol_regression_result.json"
SIGNAL_PRODUCT = HERE / "_diag_signal_to_product_result.json"


# --------------------------------------------------------------------------- #
# 夹具：数字出处写在 provenance 里，引用前已逐条回查
# --------------------------------------------------------------------------- #

WEAK_BOARD = Claim(
    name="weak_board_absorption(+48.4%,Sharpe 0.97)",
    universe="all_a",
    entry="open_t1",
    exit="close_t1",
    holding_days=1,
    entry_unfillable_share=0.0,
    agg_unit="per_day",
    effect_bp=None,          # 组合绝对收益，不是事件超额
    t_stat=None,
    n_days=366,
    benchmark="",            # 回测只报绝对收益，未做匹配基准
    benchmark_dims=frozenset(),
    event_type="weak_board",
    capacity_annual_pct={10: 48.4, 100: 1.9},
    annual_gross_pct=48.4,
    annual_net_pct=None,
    cost_bp_roundtrip=32.0,
    n_tests=1,
    provenance={
        "capacity": "docs/youzi-strategy-audit.md:96-100（max_positions 10→100，+48.4%(0.97)→+1.9%(0.22)）",
        "mechanism": "score 恒为 0 时引擎按 symbol 顺序取前 N；+48% 主要是'恰好买了 000 开头'",
        "window": "open_t1 → close_t1 是当日买卖（推导 0 个交易日）；它当初被判无效的正是这一点",
    },
    notes="这条不是统计问题，是组合构造问题：超额来自买了哪几只。",
)

ZHA_BAN = Claim(
    name="炸板低吸(+117.5bp,t=7.19)",
    universe="all_a",
    entry="open_t1",
    exit="close_t3",
    holding_days=2,
    entry_unfillable_share=0.0,
    agg_unit="per_trade",
    effect_bp=117.5,
    t_stat=7.19,
    per_trade_t=7.19,
    per_day_t=-0.30,
    n_obs=2950,
    n_days=274,
    benchmark="market_equal",
    benchmark_dims=frozenset(),
    event_type="zha_ban",
    mde_bp=None,
    n_tests=40,
    bonferroni_threshold=3.23,   # 40 次的双侧 Bonferroni = 3.227，只允许更严
    cost_bp_roundtrip=32.0,
    provenance={
        "per_trade": "docs/youzi-strategy-audit.md:90（按笔 +117.5bp / t=7.19）",
        "per_day": "docs/youzi-strategy-audit.md:91（按交易日 -9.3bp / t=-0.30）",
    },
    notes="同一形态，换聚合单位后符号翻转。这是本仓库最贵的一次假阳性。",
)

SHANG_BANG = Claim(
    name="龙虎榜平盘上榜(-291.1bp,t=-5.08)",
    universe="all_a",
    entry="open_t1",
    exit="close_t6",
    holding_days=5,
    entry_unfillable_share=0.0,
    agg_unit="per_day",
    effect_bp=-291.1,
    t_stat=-5.08,
    n_obs=1009,
    n_days=239,
    benchmark="同板块同涨幅箱",
    benchmark_dims=frozenset({"board", "pct"}),
    event_type="dragon_tiger",
    matched_effect_bp=-85.6,
    matched_t=-0.97,
    mde_bp=None,
    n_tests=68,
    cost_bp_roundtrip=32.0,
    provenance={
        "raw": "docs/dragon-tiger-assessment.md:131（10cm 平盘 H5 -291.1 / t=-5.08）",
        "matched": "docs/dragon-tiger-assessment.md:141（换手率最邻近匹配 H5 -85.6 / t=-0.97）",
        "threshold": "docs/dragon-tiger-assessment.md:92 原用单侧 3.1（29 席 ×1 持有期）；"
        "本夹具按 68 席双记，双侧 Bonferroni 为 3.38，故不再传 override",
    },
    notes="它不是龙虎榜的：换手≥25% 单独复现八到九成负超额。",
)

TURNOVER = Claim(
    name="主板换手≥25%(H5 -160.0bp,t=-2.55)",
    universe="mainboard",
    entry="open_t1",
    exit="close_t6",
    holding_days=5,
    entry_unfillable_share=0.0,
    agg_unit="per_day",
    effect_bp=-160.0,
    t_stat=-2.55,
    n_obs=4783,
    n_days=365,
    benchmark="主板同涨幅箱",
    benchmark_dims=frozenset({"board", "pct"}),
    event_type="turnover_shock",
    mde_bp=100.0,
    # 这个下限来自全市场口径（analysis/_diag_signal_to_product_result.json
    # triggers['换手≥25%'].bucket_H5.mde_bp=100.0），主板样本更少 ⇒ 真实下限只会更大。
    # 借用方向是保守的，但"借用"本身要被门看见，而不是靠注释自觉。
    mde_universe="all_a",
    annual_gross_pct=1.63,
    annual_net_pct=1.22,
    cost_bp_roundtrip=32.0,
    n_tests=200,
    provenance={
        "effect": "docs/youzi-strategy-audit.md:159（主板同板块同涨幅箱 H5 -160.0 / t=-2.55）",
        "annual": "docs/youzi-strategy-audit.md:215-217（毛 1.63% / 净 1.22% / IR 0.05）",
        "mde": "analysis/_diag_signal_to_product_result.json（全市场口径，借用；主板自己的 MDE 尚无产物）",
        "trigger": "rate=0.42%/股票日、1.02 次/年/股（主板）",
    },
    notes="唯一活着的候选。它同时是：单次检验过、族系不过、经济性不过、MDE 跨口径。",
)

CLEAN = Claim(
    name="正对照(合成，口径齐全)",
    universe="mainboard",
    entry="open_t1",
    exit="close_t3",
    holding_days=2,
    entry_unfillable_share=0.0,
    agg_unit="per_day",
    effect_bp=420.0,
    t_stat=4.60,
    n_obs=6000,
    n_days=365,
    benchmark="同板块同涨幅同换手箱",
    benchmark_dims=frozenset({"board", "pct", "turnover"}),
    event_type="volume_shock",
    matched_effect_bp=380.0,
    matched_t=4.10,
    mde_bp=120.0,
    mde_universe="mainboard",
    annual_gross_pct=10.2,
    annual_net_pct=8.0,
    capacity_annual_pct={10: 12.0, 100: 8.0},
    cost_bp_roundtrip=32.0,
    n_tests=25,
    provenance={"note": "合成数据，仅用于证明协议不是恒 REJECT"},
    notes="如果这一条不能 PASS，说明协议把所有东西都判红了，前三条验收毫无意义。",
)


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #

def run() -> dict:
    cases = [WEAK_BOARD, ZHA_BAN, SHANG_BANG, TURNOVER, CLEAN]
    verdicts = [evaluate(c) for c in cases]
    by_name = {v.name: v for v in verdicts}

    expectations = {
        WEAK_BOARD.name: ("REJECT", {"selection_dependent", "non_executable_window"}),
        ZHA_BAN.name: ("REJECT", {"per_trade_aggregation"}),
        SHANG_BANG.name: ("REJECT", {"uncontrolled_confound"}),
        TURNOVER.name: ("REJECT", {"multiplicity_fail", "below_hurdle", "universe_mismatch"}),
        CLEAN.name: ("PASS", set()),
    }

    end_to_end = []
    for name, (want_decision, want_reasons) in expectations.items():
        v = by_name[name]
        ok = v.decision == want_decision and want_reasons <= set(v.reasons)
        end_to_end.append(
            {
                "case": name,
                "want": want_decision,
                "got": v.decision,
                "want_reasons": sorted(want_reasons),
                "got_reasons": sorted(v.reasons),
                "pass": ok,
            }
        )

    # --- 单门隔离：每个门单独喂一个会让它红的输入 ---
    unit = []

    def probe(gate: str, label: str, claim: Claim, want: str, *, decision: str | None = None):
        v = evaluate(claim)
        ok = want in v.reasons and (decision is None or v.decision == decision)
        unit.append(
            {
                "gate": gate,
                "probe": label,
                "want": want,
                "got_decision": v.decision,
                "got_reasons": sorted(v.reasons),
                "pass": ok,
            }
        )

    probe("G1", "open_t1 → close_t1（当日买卖）", replace(CLEAN, exit="close_t1", holding_days=1),
          "non_executable_window")
    probe("G2", "agg_unit=per_trade", replace(CLEAN, agg_unit="per_trade", per_day_t=0.4),
          "per_trade_aggregation")
    probe("G2", "capacity 10→100 衰减 93%", replace(CLEAN, capacity_annual_pct={10: 30.0, 100: 2.0}),
          "selection_dependent")
    probe("G3", "事件=上榜，基准只控 board/pct（缺 turnover）",
          replace(CLEAN, benchmark_dims=frozenset({"board", "pct"}), event_type="dragon_tiger"),
          "uncontrolled_confound")
    probe("G4", "MDE 800bp > 效应 420bp", replace(CLEAN, mde_bp=800.0), "below_detection_limit")
    probe("G5", "年化净 1.0% < 门槛 3%", replace(CLEAN, annual_net_pct=1.0), "below_hurdle",
          decision="REJECT_ECON")

    # 反向：同样只控 board/pct，但事件由换手定义 ⇒ 不该红。
    # 这一条是 G3 的判别力所在：同样两个维度，红绿由登记表决定，不是硬编码。
    g3_rev = evaluate(
        replace(CLEAN, benchmark_dims=frozenset({"board", "pct"}), event_type="turnover_shock")
    )
    unit.append(
        {
            "gate": "G3",
            "probe": "事件=换手，基准只控 board/pct（不该红）",
            "want": "(no uncontrolled_confound)",
            "got_decision": g3_rev.decision,
            "got_reasons": sorted(g3_rev.reasons),
            "pass": "uncontrolled_confound" not in g3_rev.reasons and g3_rev.decision == "PASS",
        }
    )

    # --- 旁路反例：每一条都必须被判红 ---
    mutation = []

    def escape(label: str, claim: Claim, guard: str):
        v = evaluate(claim)
        mutation.append(
            {
                "bypass": label,
                "guard": guard,
                "want_decision": "REJECT",
                "got_decision": v.decision,
                "got_reasons": sorted(v.reasons),
                "pass": v.decision != "PASS" and guard in v.reasons,
            }
        )

    # 1. 事件类型未登记（原先：benchmark_dims={board} + 三个混杂全声明成事件定义变量）
    escape(
        "事件类型未登记（原：全部混杂当事件定义）",
        replace(CLEAN, benchmark_dims=frozenset({"board"}), event_type="all_confounds"),
        "unknown_event",
    )
    # 2. 当日买卖 + 自报 1 天
    escape(
        "open_t1→close_t1 自报 1 天",
        replace(CLEAN, exit="close_t1", holding_days=1),
        "non_executable_window",
    )
    # 3. n_tests 显式给 1：这一条**没有被第 0 步关闭**，见 known_holes。
    #    第 0 步只关掉"缺省即放行"：缺省必须红。
    # 4a. universe 未登记
    escape(
        "universe 随便填",
        replace(CLEAN, universe="mainboard_magic"),
        "unknown_universe",
    )
    # 4b. MDE 跨宇宙借用
    escape(
        "MDE 借别的宇宙",
        replace(CLEAN, mde_universe="all_a"),
        "universe_mismatch",
    )
    # 5. exit 未登记（原先只校验 entry）
    escape("exit 未登记", replace(CLEAN, exit="close_t_minus9"), "unknown_exit")
    # 6. 口径齐全但一个数都不报（原先 G4/G5 双 SKIP 后 PASS）
    escape(
        "只填口径不报数",
        Claim(
            name="只填口径不报数",
            universe="mainboard",
            entry="open_t1",
            exit="close_t3",
            holding_days=2,
            entry_unfillable_share=0.0,
            agg_unit="per_day",
            benchmark="箱",
            benchmark_dims=frozenset({"board", "pct", "turnover"}),
            event_type="volume_shock",
        ),
        "effect_missing",
    )
    # 7. 自报族系阈值比 Bonferroni 更松
    escape(
        "族系阈值 1.96 < Bonferroni 3.66",
        replace(TURNOVER, n_tests=200, bonferroni_threshold=1.96, annual_net_pct=3.5),
        "threshold_too_loose",
    )
    # 3. n_tests 显式给 1：这一条**没有被第 0 步关闭**，见 known_holes。
    # 3'. 但 n_tests 缺省必须红（缺省 ≠ 只测过一次）。
    #     这一条必须**真的不传**：replace(..., n_tests=None) 显式传了 None，
    #     与"取默认值"是两种调用方，而缺陷只在默认值那一半。
    escape(
        "完全不传 n_tests 与买不进占比",
        Claim(
            name="完全不传 n_tests 与买不进占比",
            universe="mainboard",
            entry="open_t1",
            exit="close_t3",
            holding_days=2,
            agg_unit="per_day",
            effect_bp=420.0,
            t_stat=4.60,
            n_obs=6000,
            n_days=365,
            benchmark="同箱",
            benchmark_dims=frozenset({"board", "pct", "turnover"}),
            event_type="volume_shock",
            matched_effect_bp=380.0,
            matched_t=4.10,
            mde_bp=120.0,
            mde_universe="mainboard",
            annual_gross_pct=10.2,
            annual_net_pct=8.0,
            capacity_annual_pct={10: 12.0, 100: 8.0},
            cost_bp_roundtrip=32.0,
        ),
        "n_tests_missing",
    )
    # 8b. 买不进占比缺省
    escape(
        "entry_unfillable_share 缺省",
        replace(CLEAN, entry_unfillable_share=None),
        "unfillable_share_missing",
    )
    # 8c. 报年化却不报容量曲线
    escape(
        "报年化但不报容量",
        replace(CLEAN, capacity_annual_pct=None),
        "selection_unassessed",
    )
    # 8d. 自报持有天数与配对不一致
    escape("自报 3 天但配对 2 天", replace(CLEAN, holding_days=3), "declared_holding_mismatch")

    # --- 结构断言：旁路不再"可表达"，而不是"表达了才红" ---
    structural = []

    structural.append(
        {
            "check": "Claim 不再接受 event_defining_vars（单调逃逸的载体已移除）",
            "pass": not hasattr(Claim(name="x"), "event_defining_vars"),
        }
    )
    _bare = Claim(name="bare")
    structural.append(
        {
            "check": "n_tests / entry_unfillable_share 的默认值必须是 None（缺省即红）",
            "got": {"n_tests": _bare.n_tests, "entry_unfillable_share": _bare.entry_unfillable_share},
            "pass": _bare.n_tests is None and _bare.entry_unfillable_share is None,
        }
    )
    try:
        protocol._validate_registry(
            {**EVENT_REGISTRY, "__bogus__": {"defines": KNOWN_CONFOUNDS, "mechanism": "由全部混杂定义"}}
        )
        raised = False
    except RuntimeError:
        raised = True
    structural.append(
        {
            "check": "登记表拒绝 defines=全部混杂 的条目",
            "pass": raised,
        }
    )
    pairs = {
        "open_t1→close_t1": holding_days_between("open_t1", "close_t1"),
        "open_t1→close_t3": holding_days_between("open_t1", "close_t3"),
        "open_t1→close_t6": holding_days_between("open_t1", "close_t6"),
        "close_t→open_t1": holding_days_between("close_t", "open_t1"),
        "close_t→close_t1": holding_days_between("close_t", "close_t1"),
        "open_t1→close_t": holding_days_between("open_t1", "close_t"),
        "open_t1→bogus": holding_days_between("open_t1", "bogus"),
    }
    want_pairs = {
        "open_t1→close_t1": 0,
        "open_t1→close_t3": 2,
        "open_t1→close_t6": 5,
        "close_t→open_t1": 1,
        "close_t→close_t1": 1,
        "open_t1→close_t": -1,
        "open_t1→bogus": None,
    }
    structural.append(
        {
            "check": "配对推导的交易日数",
            "got": pairs,
            "pass": pairs == want_pairs,
        }
    )
    structural.append(
        {
            "check": "Bonferroni 阈值随 n_tests 单调上升（1/25/40/68/200）",
            "got": {n: round(bonferroni_threshold(n), 4) for n in (1, 25, 40, 68, 200)},
            "pass": all(
                bonferroni_threshold(a) < bonferroni_threshold(b)
                for a, b in ((1, 25), (25, 40), (40, 68), (68, 200))
            ),
        }
    )

    # --- 未关闭的洞：不参与 all_pass，但必须出现在产物里 ---
    # 第 0 步能关掉"缺省即放行"，关不掉"显式自报一个更有利的数"。
    # 把它们列成 closed=false，是为了让下一次读产物的人不会把 all_pass 读成"协议无洞"。
    clean_low_t = replace(CLEAN, t_stat=2.2)
    hole_flip = {
        "n_tests=1": evaluate(replace(clean_low_t, n_tests=1)).decision,
        "n_tests=200": evaluate(replace(clean_low_t, n_tests=200)).decision,
        "threshold_1.96": bonferroni_threshold(1),
        "threshold_200": round(bonferroni_threshold(200), 4),
        "t_stat": clean_low_t.t_stat,
    }
    holes = [
        {
            "id": "n_tests 显式给 1",
            "closed": hole_flip["n_tests=1"] == hole_flip["n_tests=200"],
            "demonstration": hole_flip,
            "closes_with": "第 3 步：只增不改的实验登记表，n_tests 从登记表读数而不是从参数读数",
        },
        {
            "id": "entry_unfillable_share 自报 0.0",
            "closed": False,
            "demonstration": "G1 只核对这个数是否给出，无法知道真实的买不进占比",
            "closes_with": "第 2 步：用 GP14 的开板次数 / GET_ZDT 的 OpenTimesZT 导出，替换自报浮点",
        },
        {
            "id": "matched 结果缺失只记 WARN",
            "closed": False,
            "demonstration": "不报 matched_effect_bp 时，G3 无法判断效应是否由混杂解释",
            "closes_with": "要求每条声明都附 matched 结果（会同时收窄正对照的口径）",
        },
        {
            "id": "登记表可被直接编辑（少报 defines）",
            "closed": False,
            "demonstration": "EVENT_REGISTRY 只保证'不可能由全部混杂定义'，不保证'定义写全了'",
            "closes_with": "登记表进版本控制 + diff 审查；协议层无法自证",
        },
    ]

    # --- 跨口径一致：主板的 -160 与产物的 -120 是两个宇宙，不能互抄 ---
    cross = {"checked": False}
    if SIGNAL_PRODUCT.exists():
        data = json.loads(SIGNAL_PRODUCT.read_text(encoding="utf-8"))
        all_a = data["triggers"]["换手≥25%"]["bucket_H5"]
        cross = {
            "checked": True,
            "mainboard_effect_bp": TURNOVER.effect_bp,
            "all_a_effect_bp": all_a["mean_bp"],
            "all_a_t": all_a["t"],
            "all_a_mde_bp": all_a["mde_bp"],
            "same_sign": (TURNOVER.effect_bp or 0) * all_a["mean_bp"] > 0,
            "note": "两个宇宙：主板 -160.0 vs 全市场 -120.0，符号一致、量级不同，禁止互换。",
        }

    ok = (
        all(r["pass"] for r in end_to_end)
        and all(r["pass"] for r in unit)
        and all(r["pass"] for r in mutation)
        and all(r["pass"] for r in structural)
    )

    return {
        "generated_by": "analysis/_diag_protocol_regression.py",
        "protocol": "analysis/eval_protocol.py",
        "all_pass": ok,
        "end_to_end": end_to_end,
        "unit": unit,
        "mutation": mutation,
        "structural": structural,
        "known_holes": holes,
        "cross_universe_check": cross,
        "verdicts": [v.to_dict() for v in verdicts],
    }


def main() -> int:
    result = run()
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("=" * 96)
    print("五道门回归：端到端")
    print("=" * 96)
    for r in result["end_to_end"]:
        flag = "OK  " if r["pass"] else "FAIL"
        print(f"[{flag}] {r['case']:<46} want={r['want']:<11} got={r['got']}")
        if not r["pass"]:
            print(f"        want_reasons={r['want_reasons']} got_reasons={r['got_reasons']}")

    print()
    print("=" * 96)
    print("单门隔离（每道门必须能被单独打红）")
    print("=" * 96)
    for r in result["unit"]:
        flag = "OK  " if r["pass"] else "FAIL"
        print(f"[{flag}] {r['gate']:<3} probe={r['probe']:<44} want={r['want']}")

    print()
    print("=" * 96)
    print("旁路反例（每条都必须变红）")
    print("=" * 96)
    for r in result["mutation"]:
        flag = "OK  " if r["pass"] else "FAIL"
        print(f"[{flag}] {r['bypass']:<38} want={r['want_decision']:<7} got={r['got_decision']:<11} guard={r['guard']}")

    print()
    print("=" * 96)
    print("结构断言")
    print("=" * 96)
    for r in result["structural"]:
        flag = "OK  " if r["pass"] else "FAIL"
        print(f"[{flag}] {r['check']}")

    print()
    print("=" * 96)
    print("未关闭的洞（不参与 all_pass，但必须读）")
    print("=" * 96)
    for h in result["known_holes"]:
        flag = "SHUT" if h["closed"] else "OPEN"
        print(f"[{flag}] {h['id']:<34} closes_with={h['closes_with']}")
    open_holes = sum(1 for h in result["known_holes"] if not h["closed"])

    print()
    for v in result["verdicts"]:
        print(render(evaluate(CLAIM_BY_NAME[v["name"]])))
        print()

    if result["cross_universe_check"].get("checked"):
        c = result["cross_universe_check"]
        print(
            f"跨口径校验：主板 {c['mainboard_effect_bp']}bp vs 全市场 {c['all_a_effect_bp']}bp"
            f"（t={c['all_a_t']}, mde={c['all_a_mde_bp']}bp），同号={c['same_sign']}"
        )

    print()
    tail = "全部通过" if result["all_pass"] else "存在失败用例"
    print(f"{tail}（未关闭的洞 {open_holes} 项）→ {RESULT_PATH.name}")
    return 0 if result["all_pass"] else 1


CLAIM_BY_NAME = {c.name: c for c in (WEAK_BOARD, ZHA_BAN, SHANG_BANG, TURNOVER, CLEAN)}


if __name__ == "__main__":
    raise SystemExit(main())
