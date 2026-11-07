"""§9.38 的「数必须能 index 到产物」——把它变成一次可失败的检查。

被测对象是 `scripts/check_number_provenance.py` 加 `docs/number-provenance.json`。
本文件钉五件事：

1. **覆盖**：扫描范围里每个断言行要么锚定到产物（值逐项比对），要么在封存
   名单里。
2. **身份粒度**：身份是**数字向量**（行里全部数字，剔掉 §章节号与 v版本号），
   不是整行文本。改字（错字、标点、措辞）不许触发；改任何一位数——**包括裸
   整数**——必须触发。这一对必须同时存在：只有「改数会红」的话，把整行哈希当
   身份也能过，而错字会天天假红；只有「改字不红」的话，身份退化成「只认小数」，
   `323 个可评估日` 改成 `324` 就没人管了。
3. **记账**：同一文件里两行写着同一组数时，不能互相顶替（少一条要红，
   多一条也要红）；一句锚定、一句封存是合法的。
4. **有牙齿**：在临时仓库里造坏样本（新数没出处 / 改了封存的数 / 数对不上
   产物 / 登记表与源码不同步），检查必须各自报红。**没有这一组，「检查通过」
   只说明它没在看**。
5. **逃生门自身**：`--rebaseline` 重签后必须仍能区分「哪行是锚定、哪行是封存」，
   否则名单会在两个状态之间来回摆而一直是绿的。

后四组不碰真实仓库：`check(root, registry, cfg)` 的 root、registry 与扫描
口径都可注入，就是为了让「它会不会红」本身可测。
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
CFG = CHECKER.Config()


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _doc(line: str) -> str:
    """把一行文字包成模块 docstring —— 与真实文件同形。"""
    return f'"""{line}"""\n'


# --------------------------------------------------------------------------- #
# 1 · 覆盖：真实仓库上不许有没出处的读数
# --------------------------------------------------------------------------- #

def test_every_reading_is_anchored_or_frozen():
    """断言行必须二选一。红了不代表数错了，代表「这个数没人能复算」。"""
    rep = CHECKER.check(REPO, _registry(), CFG)
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
    rep = CHECKER.check(REPO, _registry(), CFG)
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
# 2 · 身份粒度：错字不换身份，改数必换身份
# --------------------------------------------------------------------------- #

def test_fingerprint_ignores_wording_but_not_numbers():
    """同一组读数、不同措辞 ⇒ 同身份；任何一位数不同 ⇒ 换身份。"""
    a = CHECKER.fingerprint("实测把区间收窄 0.42 倍。")
    assert a == CHECKER.fingerprint("实测把区间收窄了 0.42 倍。")     # 多一个「了」
    assert a == CHECKER.fingerprint("实测：把区间收窄 0.42 倍  ")      # 标点 + 空格
    assert a != CHECKER.fingerprint("实测把区间收窄 0.39 倍。")        # 改数
    # 排版差异不算差异：`3 倍` / `3倍`、`44–49` / `44-49`。
    assert CHECKER.fingerprint("实测 3 倍") == CHECKER.fingerprint("实测 3倍")
    assert CHECKER.fingerprint("实测 44–49") == CHECKER.fingerprint("实测 44-49")


def test_digit_identity_covers_bare_integers():
    """裸整数也承重：样本量、天数、条数改成别的值，必须换身份。

    身份曾经只看「读数形态」（小数 / 带量纲 / 区间），裸整数不在里面，于是整类
    样本量的改动都不红。这是本文件第 2 条真正要守的东西。
    """
    base = CHECKER.fingerprint("样本量 323 个可评估日，区间 44–49。")
    assert CHECKER.fingerprint("样本量 324 个可评估日，区间 44–49。") != base
    assert CHECKER.fingerprint("样本量 323 个可评估日，区间 45–49。") != base
    # 只有措辞变、数字没动 ⇒ 同身份
    assert CHECKER.fingerprint("样本量是 323 个可评估日，区间 44–49。") == base


def test_only_doc_coordinates_are_exempt():
    """豁免只有两类：`§9.39` 的章节号与 `v13c` 的版本号。

    日期**不**豁免——「2026-11-05 的快照」是对数据新鲜度的声称，改日期该重核。
    全角数字不是另一个数。
    """
    assert CHECKER.digits("见 §9.39 与 v13c：收窄 0.42 倍") == ["0.42"]
    assert CHECKER.digits("2026-11-05 的快照，5,579 只") == ["2026", "11", "05",
                                                           "5", "579"]
    assert CHECKER.digits("\uff10\uff0e\uff14\uff12") == ["0.42"]


def test_every_digit_in_the_repo_is_identity_bearing():
    """真仓库的断言行：每一位数都承重，一个都不能是哑的。

    这条是主断言——「检查器不许悄悄放宽身份」的护栏。豁免必须恰好由 §章节号
    与 v版本号解释掉，多剔一种（例如把日期也剔了）就会红。
    """
    lines = CHECKER.scan(REPO, CFG)
    assert len(lines) >= 90, f"只扫到 {len(lines)} 条断言行"
    blind, unexplained = [], []
    for ln in lines:
        body = CHECKER.identity_text(ln.text)
        base = CHECKER.fingerprint(ln.text)
        for mt in CHECKER.DIGITS.finditer(body):
            _s, e = mt.span()
            d = mt.group(0)
            mut = body[:e - 1] + ("1" if d[-1] != "1" else "2") + body[e:]
            if CHECKER.fingerprint(mut) == base:
                blind.append(f"{ln.file}:{ln.lineno} 的 {d}")
        raw, kept = len(CHECKER.DIGITS.findall(ln.text)), len(CHECKER.digits(ln.text))
        if raw != kept:
            exempt = sum(len(CHECKER.DIGITS.findall(m.group(0)))
                         for rx in (CHECKER._SECTION_REF, CHECKER._VERSION)
                         for m in rx.finditer(ln.text))
            if raw - kept != exempt:
                unexplained.append(f"{ln.file}:{ln.lineno} 少了 {raw - kept} 个数字，"
                                   f"§/v 只解释了 {exempt} 个")
    assert not blind, "这些数字改了不换身份：" + "; ".join(blind[:5])
    assert not unexplained, "\n".join(unexplained[:5])


def test_bare_integer_edit_in_a_real_file_turns_red(tmp_path):
    """真文件 + 真登记表：把样本量改一位，端到端必须红。

    这是本轮修法的回归——旧身份只看小数与带量纲的数，这一改动全绿。
    """
    rel = "backend/app/decision/pwin.py"
    src = (REPO / rel).read_text(encoding="utf-8")
    assert src.count("323 个可评估日 ⇒ 44–49") == 1
    reg = _registry()
    reg = {"entries": [e for e in reg["entries"] if e["file"] == rel],
           "frozen": [f for f in reg["frozen"] if f["file"] == rel]}
    _write(tmp_path, rel, src)
    assert CHECKER.check(tmp_path, reg, CFG).ok

    _write(tmp_path, rel, src.replace("323 个可评估日 ⇒ 44–49",
                                      "324 个可评估日 ⇒ 44–49"))
    rep = CHECKER.check(tmp_path, reg, CFG)
    kinds = rep.kinds()
    assert "UNREGISTERED" in kinds and kinds & {"STALE", "STALE_ENTRY"}, kinds


def test_typo_keeps_identity_but_number_edit_breaks_it(tmp_path):
    """端到端：同一个封存项，改字仍然绿，改数立刻 `STALE` + `UNREGISTERED`。"""
    rel = "backend/app/decision/sample.py"
    text = "实测把区间收窄 0.42 倍。"
    reg = {"entries": [], "frozen": [{"file": rel, "text": text}]}
    path = _write(tmp_path, rel, _doc(text))
    assert CHECKER.check(tmp_path, reg, CFG).ok

    _write(tmp_path, rel, _doc("实测把区间收窄了 0.42 倍。"))
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert rep.ok, "错字不该触发硬失败——它只会进 PROSE_DRIFT"
    assert [n["kind"] for n in rep.notes] == ["PROSE_DRIFT"]

    _write(tmp_path, rel, _doc("实测把区间收窄 0.39 倍。"))
    assert {f["kind"] for f in CHECKER.check(tmp_path, reg, CFG).failures} == {
        "STALE", "UNREGISTERED"}


def test_prose_flip_is_visible_but_not_fatal(tmp_path):
    """只改字、不改数的语义翻转不会红，但必须出现在提示里（这是身份只看
    读数付的代价，§9.40 明写）。"""
    rel = "backend/app/decision/sample.py"
    reg = {"entries": [], "frozen": [
        {"file": rel, "text": "实测 8 条序列里 7 条偏窄 0.7–17.5%。"}]}
    _write(tmp_path, rel, _doc("实测 8 条序列里 7 条偏宽 0.7–17.5%。"))
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert rep.ok
    assert len(rep.notes) == 1
    assert rep.notes[0]["kind"] == "PROSE_DRIFT"
    assert rep.notes[0]["similarity"] < 1.0


# --------------------------------------------------------------------------- #
# 3 · 记账：同指纹不能互相顶替
# --------------------------------------------------------------------------- #

def test_duplicate_readings_must_be_counted(tmp_path):
    """两行同一组数只登记一条 ⇒ 多出来的那行没出处；登记两条才对；删掉一行
    ⇒ 登记数超了，必须红。"""
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    one = {"file": rel, "text": text}
    _write(tmp_path, rel, '"""' + text + "\n" + text + '"""\n')

    rep = CHECKER.check(tmp_path, {"entries": [], "frozen": [one]}, CFG)
    assert rep.kinds() == {"UNREGISTERED"}
    assert rep.frozen == 1

    rep = CHECKER.check(tmp_path, {"entries": [], "frozen": [one, one]}, CFG)
    assert rep.ok and rep.frozen == 2

    _write(tmp_path, rel, _doc(text))
    rep = CHECKER.check(tmp_path, {"entries": [], "frozen": [one, one]}, CFG)
    assert rep.kinds() == {"STALE"}


def test_shared_fingerprint_across_anchor_and_freeze_is_legal(tmp_path):
    """真的有两句话说了同一组数：一句锚定、一句封存，各记各的次数。"""
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, '"""' + text + "\n" + text + '"""\n')
    _write(tmp_path, "data/.scratch/x.json", json.dumps({"k": 1.31}))
    reg = {"entries": [{"id": "t", "file": rel, "text": text,
                        "artifact": "data/.scratch/x.json",
                        "generator": "scripts/probe_x.py",
                        "checks": [{"path": "k", "claimed": 1.31}]}],
           "frozen": [{"file": rel, "text": text}]}
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert rep.ok and (rep.anchored, rep.frozen) == (1, 1)


# --------------------------------------------------------------------------- #
# 4 · 有牙齿：造坏样本，检查必须报红
# --------------------------------------------------------------------------- #

def test_new_reading_without_provenance_is_rejected(tmp_path):
    """新写一句带读数的断言而登记表里没有 ⇒ 必须红。"""
    _write(tmp_path, "backend/app/decision/sample.py",
           _doc("实测把区间收窄 0.42 倍。"))
    rep = CHECKER.check(tmp_path, {"entries": [], "frozen": []}, CFG)
    assert not rep.ok
    assert rep.kinds() == {"UNREGISTERED"}


def test_value_mismatch_is_rejected(tmp_path):
    """docstring 与产物给的数不同 ⇒ 必须红（这是「数会随数据腐烂」那一层）。"""
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, _doc(text))
    _write(tmp_path, "data/.scratch/x.json", json.dumps({"summary": {"k": 1.23}}))
    reg = {"frozen": [], "entries": [{
        "id": "t", "file": rel, "text": text,
        "artifact": "data/.scratch/x.json", "generator": "scripts/probe_x.py",
        "checks": [{"path": "summary.k", "claimed": 1.31, "tol": 0.005}]}]}
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert [f["kind"] for f in rep.failures] == ["VALUE_MISMATCH"]
    assert "1.23" in rep.failures[0]["detail"]


def test_registry_drift_is_rejected(tmp_path):
    """只改登记表的 claimed、不动源码 ⇒ 必须红（登记表与 docstring 不同步）。"""
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, _doc(text))
    _write(tmp_path, "data/.scratch/x.json", json.dumps({"k": 1.31}))
    reg = {"frozen": [], "entries": [{
        "id": "t", "file": rel, "text": text,
        "artifact": "data/.scratch/x.json", "generator": "scripts/probe_x.py",
        "checks": [{"path": "k", "claimed": 1.23}]}]}
    assert CHECKER.check(tmp_path, reg, CFG).kinds() == {"REGISTRY_DRIFT",
                                                         "VALUE_MISMATCH"}


def test_missing_artifact_is_skip_not_failure(tmp_path):
    """产物不在 ⇒ SKIP，不假装通过、也不判失败（CI 上没有 .scratch）。"""
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, _doc(text))
    reg = {"frozen": [], "entries": [{
        "id": "t", "file": rel, "text": text, "artifact": "data/.scratch/none.json",
        "generator": "scripts/probe_x.py",
        "checks": [{"path": "k", "claimed": 1.31}]}]}
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert rep.ok and len(rep.skipped) == 1


def test_custom_globs_support_other_layouts(tmp_path):
    """扫描口径是参数：默认口径扫不到 `src/`，换 `globs` 就必须扫到。

    这条守的是「可参数化」本身——否则临时仓库只能长成真实仓库的样子。
    """
    _write(tmp_path, "src/mod.py", _doc("实测 0.42 倍。"))
    assert CHECKER.scan(tmp_path, CFG) == []
    assert len(CHECKER.scan(tmp_path, CHECKER.Config(globs=("src/*.py",)))) == 1


def test_structural_percent_is_not_a_reading(tmp_path):
    """「单侧 95% 下界」是定义，不是读数——不能被灌进待核名单。

    这条守的是噪声：把定义当读数，名单会长到没人读，检查就死了。
    """
    _write(tmp_path, "backend/app/decision/sample.py",
           _doc("实测：返回单侧 95% 下界，名义水平 5%。"))
    assert CHECKER.scan(tmp_path, CFG) == []


def test_frozen_sha_must_match_its_own_text(tmp_path):
    """登记表的 `sha` 必须与它自己的 `text` 对得上。

    `sha` 不参与归组（归组按 text 现算），所以手改 text 不重签时，条目会
    「看起来还在」而静默通过——这条堵的就是那个洞。
    """
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, _doc(text))
    reg = {"entries": [], "frozen": [{"file": rel, "text": text, "sha": "deadbeef"}]}
    assert CHECKER.check(tmp_path, reg, CFG).kinds() == {"REGISTRY_DRIFT"}


def test_rebaseline_keeps_anchor_and_seal_on_their_own_lines(tmp_path):
    """同一指纹下一句锚定、一句封存：重签后仍是一锚一封，文本不互换。

    这条守的是逃生门自己。按扫描顺序扣名额的版本会把锚定行写成封存、把封存行
    预留成锚定；两边 `sha` 都对、检查一直绿，而名单在两种状态之间来回摆。
    """
    rel = "backend/app/decision/sample.py"
    anchored = "实测 1.31 倍，窄 7 条。"
    sealed = "实测：1.31 倍窄在 7 条序列上。"
    assert CHECKER.fingerprint(anchored) == CHECKER.fingerprint(sealed)
    _write(tmp_path, rel, '"""' + anchored + "\n" + sealed + '"""\n')

    reg_path = tmp_path / "docs" / "number-provenance.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg = {"entries": [{"id": "t", "file": rel, "text": anchored,
                        "artifact": "data/.scratch/x.json",
                        "generator": "scripts/probe_x.py",
                        "checks": [{"path": "k", "claimed": 1.31}]}],
           "frozen": [{"file": rel, "text": sealed}]}

    CHECKER._rebaseline(tmp_path, reg_path, reg, CFG)
    once = json.loads(reg_path.read_text(encoding="utf-8"))
    assert [f["text"] for f in once["frozen"]] == [sealed]
    assert CHECKER.check(tmp_path, once, CFG).ok

    CHECKER._rebaseline(tmp_path, reg_path, once, CFG)
    twice = json.loads(reg_path.read_text(encoding="utf-8"))
    assert [f["text"] for f in twice["frozen"]] == [sealed], "重签不幂等：名单在摆"
