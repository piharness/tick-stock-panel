"""判定器能不能红——用真实回执做基底，逐格注入坏数据，看 fail_class 是否按 §4 变。

为什么要写这个：`--evaluate` 是个"只输出 PASS/C1"的黑箱时，它和永远打印 PASS 的
函数长得一模一样。这个测试给每一类 FAIL 都造一个人工样本，要求判定器必须跳到那一类。
真实回执作为 PASS 侧基线，坏数据只改一格、不动其余，保证失败一定是那一格造成的。

跑法：backend/.venv/Scripts/python.exe analysis/_test_probe_verdict.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _diag_probe_gp as probe  # noqa: E402

BASE = json.loads((HERE / "_diag_probe_gp_result.json").read_text(encoding="utf-8"))
POS = {r["code"] + probe._suffix(r["code"]) for r in BASE["controls"]["positive"]}
NEG = {r["code"] + probe._suffix(r["code"]) for r in BASE["controls"]["negative"]}


def _run(result: dict, name: str) -> tuple[str, dict]:
    """把 result 落成临时回执，跑 evaluate()，返回 (fail_class, verdict)。"""
    with tempfile.TemporaryDirectory() as tmp:
        rp = Path(tmp) / "r.json"
        vp = Path(tmp) / "v.json"
        rp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        old = (probe.RESULT_PATH, probe.VERDICT_PATH)
        probe.RESULT_PATH, probe.VERDICT_PATH = rp, vp
        try:
            probe.evaluate()
            return json.loads(vp.read_text(encoding="utf-8"))["fail_class"], {}
        finally:
            probe.RESULT_PATH, probe.VERDICT_PATH = old


def _empty(rec: dict, error: str = "") -> dict:
    rec = copy.deepcopy(rec)
    rec.update(ok=False, empty=True, value_json=None, error_text=error or None, error=error or None)
    return rec


def _set_gp(rec: dict, field: str, values: list[str]) -> dict:
    rec = copy.deepcopy(rec)
    val = json.loads(rec["value_json"])
    val[field] = [{"Date": "20251113", "Value": values}]
    rec["value_json"] = json.dumps(val, ensure_ascii=False)
    rec["ok"], rec["empty"] = True, False
    return rec


def main() -> int:
    cases: list[tuple[str, dict, str]] = []

    # 基线：真实回执 —— 实质全过、字面 P1 不过（负控无行）
    cases.append(("真实回执", copy.deepcopy(BASE), "C1"))

    # F1 权限：四只全空且错误原文含权限字样
    c = copy.deepcopy(BASE)
    c["records"] = [_empty(r, "权限不足或未开通该接口") for r in c["records"]]
    cases.append(("F1 全空+权限文本", c, "F1"))

    # F0 调用层：错误原文是 has no attribute（旧版会误判成 F1 并关线）
    c = copy.deepcopy(BASE)
    c["records"] = [_empty(r, "get_gpjy_value: object has no attribute 'get_gpjy_value'") for r in c["records"]]
    cases.append(("F0 方法不存在", c, "F0"))

    # F4 不可归因：全空但一句错误都没有
    c = copy.deepcopy(BASE)
    c["records"] = [_empty(r, "") for r in c["records"]]
    cases.append(("F4 空且无错误", c, "F4"))

    # F2 负控也拿到封单 → 字段不是封单
    c = copy.deepcopy(BASE)
    c["records"] = [_set_gp(r, "GP15", ["2.00", "888.00"]) if r["symbol"] in NEG else r for r in c["records"]]
    cases.append(("F2 负控有封单", c, "F2"))

    # F3 语义恒等式不成立：封成比放大 3 倍（相对误差 ~200%）
    c = copy.deepcopy(BASE)
    out = []
    for r in c["records"]:
        if r["symbol"] in POS:
            val = json.loads(r["value_json"])
            ratio = float(val["GP22"][0]["Value"][0]) * 3
            r = _set_gp(r, "GP22", [f"{ratio:.2f}", val["GP22"][0]["Value"][1]])
        out.append(r)
    c["records"] = out
    cases.append(("F3 恒等式不成立", c, "F3"))

    bad = 0
    for name, result, want in cases:
        got, _ = _run(result, name)
        flag = "ok " if got == want else "BAD"
        if got != want:
            bad += 1
        print(f"[{flag}] {name:<18} 期望 {want:<4} 实得 {got}")
    print(f"\n{len(cases) - bad}/{len(cases)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
