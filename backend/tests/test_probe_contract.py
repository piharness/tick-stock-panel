"""九条判据的跨语言契约：后端 RULE_IDS ↔ 前端 probes.ts 的 PROBES。

为什么需要这层断言：同一份「九条」清单在 Python 与 TypeScript 里各写了一遍。
这正是本项目最贵的一类缺陷——**同一真相的第二个定义点**：两侧漂移时没有任何地方会报错，
表现是「页面上少一格」「跳到了一条不相干的常识」，而所有人都以为那是设计如此。

这里把它们绑成一条断言：任一侧增删或改序，测试先红。
（不绑定展示文案 label：那只是文字，漂移不影响判定；绑定的是 id 与顺序。）
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.table_game import RULE_IDS

PROBES_TS = Path(__file__).resolve().parents[2] / "frontend/src/pages/common-sense/probes.ts"


def _frontend_ids() -> list[str]:
    """只读 PROBES 数组字面量里的 id —— 不能把 ProbeId 的联合类型也一起读进来。"""
    src = PROBES_TS.read_text(encoding="utf-8")
    start = src.index("export const PROBES")
    end = src.index("export const PROBE_ORDER")
    return re.findall(r"\bid:\s*'([a-z_]+)'", src[start:end])


def test_frontend_probes_file_exists():
    """缺了这份文件，市场常识页的「个股判据」整块消失——必须在这里先红。"""
    assert PROBES_TS.is_file(), f"找不到前端判据定义：{PROBES_TS}"


def test_probe_ids_and_order_match_backend():
    front = _frontend_ids()
    assert front == list(RULE_IDS), (
        "前后端九条判据不一致：\n"
        f"  后端 RULE_IDS = {list(RULE_IDS)}\n"
        f"  前端 PROBES   = {front}"
    )
