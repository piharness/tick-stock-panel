"""七个「改一位」口径的盲数字必须与账本 §9.41 八 的表逐位相同。

脚本自己会比对并不是保障——没人跑就等于散文。这里把它接进测试矩阵。
没有 git 历史时（例如从 sdist 安装）跳过，不假装通过。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_identity_mutation.py"

#: 账本 §9.41 八 的那张表。改这里等于改账本，两处必须同一次评审。
LEDGER = {
    "correct_target": 132,
    "prefix_dup": 135,
    "replace_all": 110,
    "replace_first": 125,
    "first_digit": 132,
    "no_mod": 132,
    "value_plus_1": 132,
}


@pytest.mark.skipif(not (ROOT / ".git").exists(),
                    reason="需要 b4322c8 在 git 历史里")
def test_seven_mutation_modes_match_the_ledger(tmp_path: Path) -> None:
    out = tmp_path / "identity_mutation.json"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json", str(out)],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]

    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["matches_ledger"] is True
    assert {m["mode"]: m["blind"] for m in rep["modes"]} == LEDGER
    # 分母与旧身份分类也要钉住：132 = 116 裸整数 + 12 章节号 + 3 日期 + 1 版本。
    assert rep["numbers"] == 315 and rep["lines"] == 94
    assert next(m for m in rep["modes"] if m["mode"] == "correct_target"
                )["by_kind"] == {"digit": 116, "section": 12, "date": 3, "version": 1}
