"""§9.38 的「数必须能 index 到产物」——把它变成一次可失败的检查。

被测对象是 `scripts/check_number_provenance.py` 加 `docs/number-provenance.json`。
本文件钉三件事：

1. **覆盖**：扫描范围里每个断言行要么锚定到产物（值逐项比对），要么在封存
   名单里。改数会同时触发 `STALE`（旧行没了）与 `UNREGISTERED`（新行没出处）。
2. **锚定完整性**：`entries` 指向的产物必须真的由它声明的 `generator` 生产
   （只在产物存在时比对数值，产物不在时 skip——与探针产物类测试同一约定）。
3. **有牙齿**：在临时仓库里造三种坏样本（新数没出处 / 改了封存的数 / 数对
   不上产物），检查必须各自报红。**没有这一组，「检查通过」只说明它没在看**。

第 3 组不碰真实仓库：`check(root, registry)` 的 root 与 registry 都可注入，
就是为了让「它会不会红」本身可测。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "docs" / "number-provenance.json"
ARTIFACTS = (REPO / "data" / ".scratch" / "pwin_variance.json",
             REPO / "data" / ".scratch" / "hac_size.json")


def _load_checker():
    path = REPO / "scripts" / "check_number_provenance.py"
    spec = importlib.util.spec_from_file_location("check_number_provenance", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_number_provenance"] = mod
    spec.loader.exec_module(mod)
    return mod


CHECKER = _load_checker()


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1 · 覆盖：真实仓库上不许有没出处的读数
# --------------------------------------------------------------------------- #

def test_every_reading_is_anchored_or_frozen():
    """断言行必须二选一。红了不代表数错了，代表「这个数没人能复算」。"""
    rep = CHECKER.check(REPO, _registry())
    assert rep.claims >= 90, f"只扫到 {rep.claims} 条断言行，扫描范围被改小了？"
    assert rep.ok, "\n".join(
        f"{f['kind']} {f['file']} {f.get('where', '')}: {f['text'][:80]} ← {f['detail']}"
        for f in rep.failures)
    # 封存是债：锚定的数量不该倒退。
    assert rep.anchored >= 12, f"只锚定了 {rep.anchored} 条，登记表被删了？"


def test_anchored_values_match_their_artifacts():
    """锚定行必须与产物逐项相等。产物不在就 skip，不假装通过。"""
    if not all(p.exists() for p in ARTIFACTS):
        pytest.skip("落盘产物不在（新克隆 / CI），本地重跑探针后才有")
    rep = CHECKER.check(REPO, _registry())
    assert not [f for f in rep.failures if f["kind"] == "VALUE_MISMATCH"]
    assert not [f for f in rep.failures if f["kind"] == "REGISTRY_DRIFT"]
    assert not rep.skipped, f"产物在却有 {len(rep.skipped)} 项跳过：{rep.skipped[:2]}"


def test_registry_artifacts_are_produced_by_their_generator():
    """登记的产物必须真的由它声明的脚本写出——否则「可复算」是空的。

    只查文件名是否出现在 generator 源文本里：够用，且不执行任何脚本。
    """
    for e in _registry()["entries"]:
        name = Path(e["artifact"]).name
        gen = (REPO / e["generator"]).read_text(encoding="utf-8")
        assert name in gen, f"{e['generator']} 里没有 {name}，产物没人生产"


# --------------------------------------------------------------------------- #
# 2 · 有牙齿：造坏样本，检查必须报红
# --------------------------------------------------------------------------- #

def _repo(tmp_path: Path, body: str, name: str = "sample.py") -> Path:
    d = tmp_path / "backend" / "app" / "decision"
    d.mkdir(parents=True)
    (d / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_new_reading_without_provenance_is_rejected(tmp_path):
    """新写一句「实测收窄 X 倍」而登记表里没有 ⇒ 必须红。"""
    root = _repo(tmp_path, '"""实测把区间收窄 0.42 倍。"""\n')
    rep = CHECKER.check(root, {"entries": [], "frozen": []})
    assert not rep.ok
    assert {f["kind"] for f in rep.failures} == {"UNREGISTERED"}


def test_edited_frozen_reading_is_rejected(tmp_path):
    """改了封存的数 ⇒ 旧行 STALE、新行 UNREGISTERED，两条并排就是 diff。"""
    root = _repo(tmp_path, '"""实测把区间收窄 0.42 倍。"""\n')
    reg = {"entries": [], "frozen": [
        {"file": "backend/app/decision/sample.py",
         "text": "实测把区间收窄 0.42 倍。", "sha": ""}]}
    reg["frozen"][0]["sha"] = CHECKER._sha(reg["frozen"][0]["text"])
    assert CHECKER.check(root, reg).ok

    (root / "backend" / "app" / "decision" / "sample.py").write_text(
        '"""实测把区间收窄 0.39 倍。"""\n', encoding="utf-8")
    rep = CHECKER.check(root, reg)
    kinds = {f["kind"] for f in rep.failures}
    assert kinds == {"STALE", "UNREGISTERED"}, kinds


def test_value_mismatch_is_rejected(tmp_path):
    """docstring 与产物给的数不同 ⇒ 必须红（这是「数会随数据腐烂」那一层）。"""
    root = _repo(tmp_path, '"""实测 1.31 倍。"""\n')
    art = root / "data" / ".scratch"
    art.mkdir(parents=True)
    (art / "x.json").write_text(json.dumps({"summary": {"k": 1.23}}), encoding="utf-8")
    reg = {"frozen": [], "entries": [{
        "id": "t", "file": "backend/app/decision/sample.py",
        "text": "实测 1.31 倍。", "artifact": "data/.scratch/x.json",
        "generator": "scripts/probe_x.py",
        "checks": [{"path": "summary.k", "claimed": 1.31, "tol": 0.005}]}]}
    rep = CHECKER.check(root, reg)
    assert [f["kind"] for f in rep.failures] == ["VALUE_MISMATCH"]
    assert "1.23" in rep.failures[0]["detail"]


def test_structural_percent_is_not_a_reading(tmp_path):
    """「单侧 95% 下界」是定义，不是读数——不能被灌进待核名单。

    这条守的是噪声：把定义当读数，名单会长到没人读，检查就死了。
    """
    root = _repo(tmp_path, '"""实测：返回单侧 95% 下界，名义水平 5%。"""\n')
    assert CHECKER.scan(root) == []
