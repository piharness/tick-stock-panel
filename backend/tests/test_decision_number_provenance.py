"""§9.38 的「数必须能 index 到产物」——把它变成一次可失败的检查。

被测对象是 `scripts/check_number_provenance.py` 加 `docs/number-provenance.json`。
本文件钉五件事：

1. **覆盖**：扫描范围里每个断言行要么锚定到产物（值逐项比对），要么在封存
   名单里。
2. **身份粒度**：身份是**数字向量**（行里全部数字连同前置符号，剔掉 §章节号与
   v版本号），不是整行文本。改字（错字、标点、措辞）不许触发；改任何一位数、
   或翻转 `+`/`−`/`±`——**包括裸整数**——必须触发。这一对必须同时存在：只有
   「改数会红」的话，把整行哈希当
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
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "docs" / "number-provenance.json"
#: 一个口径一份登记表（§9.50）。合并成一张表要给「不在本口径」发明一个合法状态，
#: 而那个状态同时吃掉「删文件」和「抹空 docstring」两种真失败。
ANALYSIS_REGISTRY = REPO / "docs" / "number-provenance.analysis.json"
ARTIFACTS = (REPO / "data" / ".scratch" / "pwin_variance.json",
             REPO / "data" / ".scratch" / "hac_size.json",
             #: 13r：产品决策真正在引用的四个产物。只放两个旧产物时，
             #: `test_evidence_artifacts_are_shippable` 全绿，而宽面前沿、
             #: 流动性身份、宇宙缺口、生存偏差四份证据在克隆上根本不存在。
             REPO / "data" / ".scratch" / "decile_net.json",
             REPO / "data" / ".scratch" / "width_frontier.json",
             REPO / "data" / ".scratch" / "liquidity_identity.json",
             REPO / "data" / ".scratch" / "universe_gap.json",
             #: 逐只缺口的明细表：JSON 给汇总，CSV 给「313 行可以一行行看」。
             REPO / "data" / ".scratch" / "universe_gap.csv",
             REPO / "data" / ".scratch" / "survivorship_bound.json")


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

#: `(登记表, 它自己的 globs, claims 地板)` —— 一个口径一份表（§9.50）。
#: **新加口径必须在这里登记**，否则 `test_every_registry_on_disk_is_registered_here`
#: 红。地板是「射程不许悄悄缩小」的那一侧：`--rebaseline` 会把不再扫描的文件的债务
#: 整段删掉，而出题人看不见——只有这个数下降能被复算出来。
REGISTRIES = (
    (REGISTRY, CHECKER.SCOPE_GLOBS, 94),
    (ANALYSIS_REGISTRY, ("analysis/*.py",), 106),
)


def _registry(path: Path = REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _scope_orphans(root: Path, reg: dict, globs: tuple[str, ...]) -> list[str]:
    """登记表里「不在自己射程内 / 不在盘上」的 `file`。

    这是 §9.50 那条不变量的**可执行形态**：每个登记表的每个 `file` 必须在它自己的
    globs 命中内、且真实存在。`check()` 只用计数据能看见它（`repo=0 ⇒ STALE`），
    而 `--rebaseline` 一跑，那条债务就连着记录一起消失——写侧永远不该说「这条不算数」。
    """
    in_scope = {p.relative_to(root).as_posix()
                for pat in globs for p in root.glob(pat)}
    out: list[str] = []
    for f in list(reg.get("entries", [])) + list(reg.get("frozen", [])):
        rel = f["file"]
        if not (root / rel).exists():
            out.append(f"{rel}: 文件不在盘上")
        elif rel not in in_scope:
            out.append(f"{rel}: 不在 {globs} 的命中内")
    return out


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


@pytest.mark.parametrize("reg_path, globs, floor", REGISTRIES,
                         ids=[p.name for p, _, _ in REGISTRIES])
def test_each_registry_covers_its_own_scope(reg_path, globs, floor):
    """每个口径在自己的 globs 上必须全绿，且断言行数不低于钉住值。

    这是 §9.50 的配对判据。合并成一张表时，两个口径的正确读数只能靠「跳过不在射程
    的文件」凑出来，而那条跳过同时让「删文件」变绿；分表之后每个口径都是**全量**
    判定——没有「不算数」这个状态。

    地板不是覆盖率：`claims` 下降只有一种成因（源码里带读数的行变少了），而
    `--rebaseline` 会把它悄悄改小。所以它必须被钉住，不能只被打印。
    """
    rep = CHECKER.check(REPO, _registry(reg_path), CHECKER.Config(globs=globs))
    assert rep.claims >= floor, (
        f"{reg_path.name} 只扫到 {rep.claims} 条断言行（地板 {floor}）——射程被改小了，"
        "或那份表的 globs 与源码目录已经错位")
    assert rep.ok, "\n".join(
        f"{f['kind']} {f['file']} {f.get('where', '')}: {f['text'][:80]} ← {f['detail']}"
        for f in rep.failures)
    # 每条断言行都要有归属：绿的时候 anchored + frozen 恰等于 claims。少一条就是
    # 「扫到了但没人记账」，与删除等价。
    assert rep.anchored + rep.frozen == rep.claims


def test_every_registry_on_disk_is_registered_here():
    """`docs/` 下有几份登记表，`REGISTRIES` 里就得有几份、且文件名对得上。

    没有这一条，「新加一个口径」的默认做法就是再写一份 JSON——它会永远绿，因为没人
    跑它。射程是从**盘上发现的**，不是从这个文件记得的。

    它顺带把新登记表钉进版本控制：只要 `docs/number-provenance.analysis.json` 没
    `git add`，新克隆上这一条就红——那是 13r 的形态（产物在盘上、在库里没有）。
    本机是绿的，所以它的牙齿只在「别人拿得到吗」这一侧。
    """
    on_disk = {p.name for p in (REPO / "docs").glob("number-provenance*.json")}
    assert on_disk == {p.name for p, _, _ in REGISTRIES}, (
        f"盘上 {sorted(on_disk)} vs 本文件 {sorted(p.name for p, _, _ in REGISTRIES)}"
        "——新口径必须在 REGISTRIES 里配上它的 globs 与地板")


@pytest.mark.parametrize("reg_path, globs, _floor", REGISTRIES,
                         ids=[p.name for p, _, _ in REGISTRIES])
def test_registry_files_are_in_scope_and_on_disk(reg_path, globs, _floor):
    """§9.50 的不变量：每个 `file` 在自己的射程内、且在盘上。

    它比「跑一次 `check()` 是绿的」更强：`check()` 是用计数据推断「这条还在不在」，
    而 `--rebaseline` 会把推断不出来的那条**删掉**。删掉之后 `check()` 照样绿——
    这正是「绿但无感知」的第四种形态：不是抽样挡在射程外，而是**射程的边界可以
    由被检查的那一方改写**。
    """
    orphans = _scope_orphans(REPO, _registry(reg_path), globs)
    assert not orphans, (
        f"{reg_path.name} 有 {len(orphans)} 条登记指不到东西：\n  " + "\n  ".join(orphans)
        + "\n文件不在了就删它的登记（那是真的删了口径），不要靠 rebaseline 顺手带走。")


def test_scope_orphans_reports_real_orphans(tmp_path):
    """`_scope_orphans` 必须能同时报出两种孤儿——否则上面那条恒绿。

    两个方向都要能红：文件在、但不在射程（glob 改窄）；文件不在盘上（被删/被改名）。
    只造其中一个的话，另一条分支永远是「没扫到 ⇒ 空 ⇒ 绿」。
    """
    _write(tmp_path, "a/b.py", _doc("实测 1.31 倍。"))
    reg = {"entries": [], "frozen": [{"file": "a/b.py", "text": "实测 1.31 倍。"}]}
    assert _scope_orphans(tmp_path, reg, ("a/*.py",)) == []
    assert "不在" in _scope_orphans(tmp_path, reg, ("other/*.py",))[0]
    gone = {"entries": [], "frozen": [{"file": "a/gone.py", "text": "x"}]}
    assert "盘上" in _scope_orphans(tmp_path, gone, ("a/*.py",))[0]


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


def test_only_doc_coordinates_and_list_marks_are_exempt():
    """豁免只有三类：`§9.39` 的章节号、`v13c` 的版本号、行首有序列表编号。

    日期**不**豁免——「2026-11-05 的快照」是对数据新鲜度的声称，改日期该重核。
    全角数字不是另一个数。列表编号是排版：给它换序号不该重核，而 `2.57` 这种
    以「数字+点」开头的读数一个字符都不许吃（`(?!\\d)` 守这一条）。
    """
    assert CHECKER.digits("见 §9.39 与 v13c：收窄 0.42 倍") == ["0.42"]
    assert CHECKER.digits("2026-11-05 的快照，5,579 只") == ["2026", "11", "05",
                                                           "5", "579"]
    assert CHECKER.digits("\uff10\uff0e\uff14\uff12") == ["0.42"]
    # 三类豁免里的第三类
    assert CHECKER.digits("1. **§6.1 的算术**：MDE@250 = MDE@325") == ["250", "325"]
    assert CHECKER.digits("3) 实测 3 倍") == ["3"]
    assert CHECKER.digits("（2）实测 3 倍") == ["3"]
    # 小数点不是列表编号
    assert CHECKER.digits("2.57–2.71 倍") == ["2.57", "2.71"]
    assert CHECKER.digits("0.01bp 的容差") == ["0.01"]


def test_sign_is_part_of_identity():
    """符号翻转必须换身份——它不是错字，是把结论翻了个面。

    同时钉住四种「长得像符号、其实不是」的东西：日期连字符、数字区间、
    markdown 项目符号、以及排版等价的减号变体。
    """
    a = CHECKER.fingerprint("实测验证段 −4.19%，训练段 +4.45%。")
    assert CHECKER.fingerprint("实测验证段 +4.19%，训练段 +4.45%。") != a
    assert CHECKER.fingerprint("实测验证段 −4.19%，训练段 −4.45%。") != a
    # 排版等价：ASCII / Unicode / 全角减号是同一个符号
    assert CHECKER.fingerprint("实测 −4.19%") == CHECKER.fingerprint("实测 -4.19%")
    assert CHECKER.fingerprint("实测 −4.19%") == CHECKER.fingerprint("实测 －4.19%")
    # 日期连字符、数字区间、项目符号都不进符号
    assert CHECKER.digits("2026-11-05 的快照") == ["2026", "11", "05"]
    assert CHECKER.digits("区间 44-49、44–49") == ["44", "49", "44", "49"]
    assert CHECKER.digits("- 250 天窗") == ["250"]
    # 指数符号与公式里的紧邻符号要进身份
    assert CHECKER.digits("1e-05") != CHECKER.digits("1e+05")
    assert CHECKER.digits("MA(horizon-1)")[-1] == "-1"
    assert CHECKER.digits("lag+1")[-1] == "+1"
    # `±` 是独立符号，不是 `+` 的别名
    assert CHECKER.digits("实测 ±12%") == ["±12"]
    assert CHECKER.digits("实测 ±12%") != CHECKER.digits("实测 +12%")


def test_sign_flip_in_a_real_file_turns_red(tmp_path):
    """端到端：封存行把 `−` 翻成 `+`，必须是 `STALE` + `UNREGISTERED`。

    这是 13n 的回归——旧身份按纯数字取值，这个改动全绿，只在 `PROSE_DRIFT`
    里留一条提示。符号不是「只改字」。
    """
    rel = "backend/app/decision/sample.py"
    old = "实测验证段 −4.19%，训练段 +4.45%。"
    new = "实测验证段 +4.19%，训练段 +4.45%。"
    assert CHECKER.fingerprint(old) != CHECKER.fingerprint(new)
    _write(tmp_path, rel, _doc(old))
    reg = {"entries": [], "frozen": [
        {"file": rel, "text": old, "sha": CHECKER.fingerprint(old)}]}
    assert CHECKER.check(tmp_path, reg, CFG).ok

    _write(tmp_path, rel, _doc(new))
    kinds = CHECKER.check(tmp_path, reg, CFG).kinds()
    assert kinds == {"STALE", "UNREGISTERED"}, kinds


def test_renumbering_an_ordered_list_keeps_identity(tmp_path):
    """给 docstring 的列表插一条、后面全部重编号 ⇒ **不该红**。

    13q 之前 `1. ` 里的 `1` 在身份里：在列表中间插一条，后面每一条都换了身份，
    8 行同时 `STALE` + `UNREGISTERED`。人只会去跑 `--rebaseline`，于是真改动
    （同一批里的读数变化）也跟着被一起重签——**假红把真红洗掉了**。
    """
    assert CHECKER.fingerprint("1. 实测 3 倍。") == CHECKER.fingerprint("2. 实测 3 倍。")
    rel = "backend/app/decision/sample.py"
    reg = {"entries": [], "frozen": [
        {"file": rel, "text": "1. 实测 3 倍。"}]}
    _write(tmp_path, rel, _doc("2. 实测 3 倍。"))
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert rep.ok, [f["kind"] for f in rep.failures]
    assert [n["kind"] for n in rep.notes] == ["PROSE_DRIFT"]   # 文字变了，看得见
    # 但列表里的读数本身改了，仍然要红
    _write(tmp_path, rel, _doc("2. 实测 4 倍。"))
    assert CHECKER.check(tmp_path, reg, CFG).kinds() == {"STALE", "UNREGISTERED"}


@pytest.mark.parametrize("globs, floor", ((CHECKER.SCOPE_GLOBS, 90), (("analysis/*.py",), 106)),
                         ids=("default", "analysis"))
def test_every_digit_in_the_repo_is_identity_bearing(globs, floor):
    """真仓库的断言行：每一位数都承重，一个都不能是哑的。

    这条是主断言——「检查器不许悄悄放宽身份」的护栏。豁免必须恰好由 §章节号、
    v版本号与行首列表编号解释掉，多剔一种（例如把日期也剔了）就会红。

    两个口径都跑：analysis 的 313 个承重数字（317 个原始数字 − 4 个文档坐标）
    以前一次都没被这条看过——身份规则是全局的，但**射程是每个口径自己的**。
    """
    lines = CHECKER.scan(REPO, CHECKER.Config(globs=globs))
    assert len(lines) >= floor, f"只扫到 {len(lines)} 条断言行（地板 {floor}）"
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
                         for rx in (CHECKER._SECTION_REF, CHECKER._VERSION,
                                    CHECKER._LIST_MARK)
                         for m in rx.finditer(ln.text))
            if raw - kept != exempt:
                unexplained.append(f"{ln.file}:{ln.lineno} 少了 {raw - kept} 个数字，"
                                   f"§/v/列表编号只解释了 {exempt} 个")
    assert not blind, "这些数字改了不换身份：" + "; ".join(blind[:5])
    assert not unexplained, "\n".join(unexplained[:5])


def test_repo_census_matches_the_quoted_numbers():
    """模块 docstring 里的 94 / 315 / 294 必须是**算出来的**，不是抄的。

    检查器自己不进入扫描范围（否则「94 条」这条断言行会把 94 改成 95，形成自指），
    所以这一层只能由断言兜底：扫描口径一变，这里先红，docstring 才有机会同步。
    13n 的教训是——136 写在 docstring 与账本里，谁也没法验，包括作者。

    `315 − 13 §/v 坐标 − 8 行首列表编号 = 294`：13q 之前那 8 个编号在身份里，
    给 docstring 插一条列表项就会让后面 8 行一起假红。
    """
    lines = CHECKER.scan(REPO, CFG)
    assert len(lines) == 94
    assert sum(len(CHECKER.DIGITS.findall(ln.text)) for ln in lines) == 315
    assert sum(len(CHECKER.digits(ln.text)) for ln in lines) == 294, \
        "315 − 13 个文档坐标 − 8 个行首列表编号 = 294 个承重数字"
    groups = Counter((ln.file, ln.key) for ln in lines)
    assert sum(1 for n in groups.values() if n > 1) == 2, \
        "同指纹撞键组数变了，检查器 docstring 与账本 §9.41 四的 2 组要同步"
    # 分母不钉死（写一句带数字的话就会变），但天花板要有人看：射程暴增是信号。
    assert 94 < CHECKER.check(REPO, _registry(), CFG).numbered < 400, \
        "claim 段落带数字行数翻倍了，扫描口径或 docstring 出了事"


def test_analysis_scope_census_is_the_quoted_debt():
    """账本 §9.50 与两份测试 docstring 引的 **106 / 104 / 2 / 317 / 313** 必须算得出。

    这一层只能由断言兜底，原因与 94 那条相同：检查器自己不进入任何射程，所以它
    docstring 里的数没有任何机制看得见。账本里「新增债务 106」也得与它同源——
    `106 行 → 104 键`（`_diag_l1_kstar_condition.py` 两组撞键各占 2 行）正是
    「82 条封存折成 80 个键」的来源，逐键报 STALE 时不能拿行数当键数。
    """
    lines = CHECKER.scan(REPO, CHECKER.Config(globs=("analysis/*.py",)))
    assert len(lines) == 106
    assert len({ln.file for ln in lines}) == 16, "产出断言行的 analysis 文件数变了"
    groups = Counter((ln.file, ln.key) for ln in lines)
    assert (len(groups), sum(1 for n in groups.values() if n > 1)) == (104, 2)
    # 射程是**单层** glob。`analysis/` 下出现子目录里的 `.py` 会被静默漏掉——
    # 这正是「射程把被测路径挡在外面」的那种绿，所以要有断言，而不是靠记得。
    assert not list((REPO / "analysis").glob("*/*.py")), (
        "analysis/ 出现子目录 .py，`analysis/*.py` 这条单层 glob 会漏掉它")
    reg = _registry(ANALYSIS_REGISTRY)
    assert not reg["entries"] and len(reg["frozen"]) == 106
    rep = CHECKER.check(REPO, reg, CHECKER.Config(globs=("analysis/*.py",)))
    assert (rep.anchored, rep.frozen, rep.failures) == (0, 106, [])
    # 317 / 313 是**数字个数**，不是行数：行数守恒（106）而某一行悄悄多长出一个
    # 数字，前面的断言一条都不动。`test_every_digit_in_the_repo_is_identity_bearing`
    # 的 docstring 正在引这两个数，而那个 docstring 不在任何射程内——所以它必须
    # 在这里被算出来，否则就是「引用一个没人复算的读数」，与 §9.50 修的是同一个病。
    raw = sum(len(CHECKER.DIGITS.findall(ln.text)) for ln in lines)
    kept = sum(len(CHECKER.digits(ln.text)) for ln in lines)
    assert (raw, kept, raw - kept) == (317, 313, 4), (
        f"承重数字口径变了：原始 {raw} / 承重 {kept} / 文档坐标剔除 {raw - kept}")


def test_bare_integer_edit_in_a_real_file_turns_red(tmp_path):
    """真文件 + 真登记表：把样本量改一位，端到端必须红。
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


def test_claimed_value_must_be_a_whole_token(tmp_path):
    """`claimed` 必须是**整枚数字**，不能靠子串命中。

    旧实现 `f"{claimed:g}" in text`：`claimed=1` 命中 `17.5`，`claimed=7` 也命中
    `17.5`。于是「1 条偏宽」改成「2 条偏宽」、登记表仍写 1，R2(b) 照样过——对
    个位数声称值来说那条检查几乎空的。这是活的假绿，不是休眠缺陷。
    """
    assert CHECKER._claimed_in_text("实测 0.7–17.5%、1 条偏宽 10.1%", 1)
    assert not CHECKER._claimed_in_text("实测 0.7–17.5%、2 条偏宽 10.1%", 1)
    assert not CHECKER._claimed_in_text("实测 0.7–17.5%", 7)
    assert not CHECKER._claimed_in_text("实测 0.72 倍", 7)
    assert not CHECKER._claimed_in_text("实测 0.72 倍", 2)
    assert CHECKER._claimed_in_text("区间 44–49", 44)
    assert not CHECKER._claimed_in_text("区间 44–49", 4)

    # 端到端：登记表与正文不同步，必须红
    rel = "backend/app/decision/sample.py"
    _write(tmp_path, rel, _doc("实测 0.7–17.5%、2 条偏宽。"))
    _write(tmp_path, "data/.scratch/x.json", json.dumps({"wider": 1}))
    reg = {"frozen": [], "entries": [{
        "id": "t", "file": rel, "text": "实测 0.7–17.5%、2 条偏宽。",
        "artifact": "data/.scratch/x.json", "generator": "scripts/probe_x.py",
        "checks": [{"path": "wider", "claimed": 1}]}]}
    assert CHECKER.check(tmp_path, reg, CFG).kinds() == {"REGISTRY_DRIFT"}


def test_claimed_sign_must_match(tmp_path):
    """`claimed` 的符号必须与正文一致，且 `−`/`-` 是同一个符号。

    旧实现：`claimed=4.19` 在 `−4.19` 上命中（假绿）；`claimed=-4.19` 拼出 ASCII
    `-4.19`，正文是 U+2212，反而不命中（假红）。`entries` 里负声称值今天是 0 条，
    但这不是「休眠」——只要有人锚定一条负数，它立刻按错的方向工作。
    """
    assert not CHECKER._claimed_in_text("实测验证段 −4.19%", 4.19)
    assert CHECKER._claimed_in_text("实测验证段 −4.19%", -4.19)
    assert CHECKER._claimed_in_text("实测验证段 -4.19%", -4.19)    # ASCII 减号
    assert not CHECKER._claimed_in_text("实测验证段 +4.19%", -4.19)
    assert not CHECKER._claimed_in_text("实测 ±12%", 12)          # ± 不冒充单值

    rel = "backend/app/decision/sample.py"
    line = "实测验证段 −4.19%，训练段 +4.45%。"
    _write(tmp_path, rel, _doc(line))
    _write(tmp_path, "data/.scratch/neg.json", json.dumps({"val": -4.19}))
    _write(tmp_path, "data/.scratch/pos.json", json.dumps({"val": 4.19}))

    def _reg(claimed: float, artifact: str) -> dict:
        return {"frozen": [], "entries": [{
            "id": "t", "file": rel, "text": line,
            "artifact": artifact, "generator": "scripts/probe_x.py",
            "checks": [{"path": "val", "claimed": claimed}]}]}

    # 负值锚定项本身能过（旧实现下这条会假红：`-4.19` 拼的是 ASCII）
    assert CHECKER.check(tmp_path, _reg(-4.19, "data/.scratch/neg.json"), CFG).ok
    # 登记表说 +4.19、产物也是 +4.19，但正文写 −4.19 ⇒ 只有漂移，没有数值错
    assert CHECKER.check(tmp_path, _reg(4.19, "data/.scratch/pos.json"),
                         CFG).kinds() == {"REGISTRY_DRIFT"}


def test_missing_artifact_is_skip_not_failure(tmp_path):
    """`Report` 层：产物不在 ⇒ SKIP，不判失败（`check()` 不区分「谁的机器」）。

    判定层不这么算——`main()` 见到 SKIP 就退出码 2，见
    `test_missing_artifact_blocks_the_gate`。两层分开是为了让 `check()` 能被
    组合进别的检查，而命令行不假装通过。
    """
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, _doc(text))
    reg = {"frozen": [], "entries": [{
        "id": "t", "file": rel, "text": text, "artifact": "data/.scratch/none.json",
        "generator": "scripts/probe_x.py",
        "checks": [{"path": "k", "claimed": 1.31}]}]}
    rep = CHECKER.check(tmp_path, reg, CFG)
    assert rep.ok and len(rep.skipped) == 1
    # `anchored` 是登记条数，`verified` 才是真比过的数——克隆上两者差一个 0。
    assert (rep.anchored, rep.checks, rep.verified) == (1, 1, 0)


def test_missing_artifact_blocks_the_gate(tmp_path):
    """产物不在 ⇒ `main()` 退出码 2（无法核对），不是 0。

    13p 之前：干净树上 36 项核对全部跳过、`已锚定 12` 照旧打印、退出码 0、
    `✓ 每个读数都有出处或已封存`。锚定层唯一的用处正好在别人的机器上不存在，
    而检查器说一切正常。`verify_seal` 早就把「文件不存在」当漂移，这里补齐。
    """
    rel = "backend/app/decision/sample.py"
    text = "实测 1.31 倍。"
    _write(tmp_path, rel, _doc(text))
    reg = {"entries": [{"id": "t", "file": rel, "text": text,
                        "artifact": "data/.scratch/none.json",
                        "generator": "scripts/probe_x.py",
                        "checks": [{"path": "k", "claimed": 1.31}]}],
           "frozen": []}
    reg_dir = tmp_path / "docs"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "number-provenance.json").write_text(
        json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    assert CHECKER.main(["--root", str(tmp_path)]) == 2
    assert CHECKER.main(["--root", str(tmp_path), "--allow-skip"]) == 0


def _registry_of(root: Path) -> dict:
    return json.loads((root / "docs" / "number-provenance.json").read_text(encoding="utf-8"))


def test_empty_scope_is_not_a_pass(tmp_path):
    """扫到 0 行 ⇒ 退出码 `1`，不是 `0`。空射程不是通过。

    glob 写错、目录改名、`--registry` 与 `--globs` 配错、新克隆上压根没有登记表——
    这些都会让 `scan()` 返回空，而判定层原来只看 `failures`：0 行 + 0 失败 ⇒
    「✓ 每个读数都有出处」。它把「我没在看」说成「都对」，比一条假绿更贵。
    （§9.50：这条地板与射程不变量是一对——有 failures 时按 failures 判，诊断更有用。）
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "number-provenance.json").write_text(
        json.dumps({"entries": [], "frozen": []}), encoding="utf-8")
    assert CHECKER.main(["--root", str(tmp_path)]) == 1, "一个文件都没扫到时判了通过"
    # 判据必须在 `Report` 上，而不是在 `main()` 的打印分支里：`--json` 是机器读的
    # 那一面，它若写 `ok: true` 而进程返 1，任何消费方都会信错一半。这条断言钉的是
    # 层位，不是措辞——把它挪回 `main()` 会让这里绿、`--json` 重新说谎。
    empty = CHECKER.check(tmp_path, _registry_of(tmp_path), CFG)
    assert empty.claims == 0 and not empty.ok, (
        "空射程在 `Report.ok` 上仍是绿的——`--json` 会与退出码矛盾")
    assert empty.as_dict()["ok"] is False, "as_dict 的 ok 走了另一条路径"
    # 有文件、在射程内，但一行读数都没有——同样是空射程
    _write(tmp_path, "backend/app/decision/sample.py", '"""设计说明，没有数。"""\n')
    assert CHECKER.main(["--root", str(tmp_path)]) == 1, "扫到文件、没扫到断言行，仍算空"
    # 反过来钉一次：有读数就必须走正常判定（这里没登记 ⇒ 1 是因为 UNREGISTERED，
    # 不是因为地板——否则上面两条可能红在别的原因上）
    _write(tmp_path, "backend/app/decision/sample.py", _doc("实测 1.31 倍。"))
    rep = CHECKER.check(tmp_path, _registry_of(tmp_path), CFG)
    assert rep.claims == 1 and rep.kinds() == {"UNREGISTERED"}
    assert not rep.ok, "有 UNREGISTERED 却判通过"


def _sealed_scratch_artifacts() -> tuple[Path, ...]:
    """封签里 `data/.scratch/` 下的文件——那就是证据层。

    不能写成「`SEALED` 里 `data/` 下的文件」：`decision_gate.SEALED` 末尾
    拼着 `+ ARTIFACTS`，而那 6 份是 `data/.probe_*.json` **运行时缓存**。
    它们被封是为了防换字节，本来就不该入库。所以按目录分，不按「在不在
    `SEALED` 里」分。
    """
    spec = importlib.util.spec_from_file_location(
        "decision_gate_for_ship", REPO / "scripts" / "decision_gate.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    scratch = REPO / "data" / ".scratch"
    return tuple(p for p in mod.SEALED if p.parent == scratch)


def _shippable_paths() -> tuple[Path, ...]:
    """要能克隆的产物 = 手写登记的 `ARTIFACTS` ∪ 封签里 `data/.scratch/` 下的全部。

    只遍历前者时，一份「进 `SEALED`、不进 `ARTIFACTS`」的产物（没有逐日键，
    因而不能进日期扫描表）就没有任何测试守它的白名单——`survivorship_bound.json`
    就这样靠一份手写白名单悬了一轮。这份清单**从封签派生**，于是「加了封签
    却忘了放行」会在 pytest 里红，而不是等下一次克隆才发现。
    """
    sealed = _sealed_scratch_artifacts()
    return tuple(ARTIFACTS) + tuple(p for p in sealed if p not in ARTIFACTS)


def test_evidence_artifacts_are_shippable():
    """证据产物必须不被 `.gitignore` 挡住——它们是锚定层唯一真在核对的东西。

    `data/**` 曾把它们一起挡在版本控制外：克隆后 36 项核对全 SKIP，判定仍 ✓。
    """
    if not (REPO / ".git").exists():
        pytest.skip("没有 git 仓库，无从判断是否可入库")
    for p in _shippable_paths():
        rel = p.relative_to(REPO).as_posix()
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO)
        assert proc.returncode == 1, f"{rel} 仍被 .gitignore 忽略，克隆拿不到"


def _untracked(scope: str) -> list[str]:
    """盘上存在、`git` 不跟踪、且没被 `.gitignore` 挡住的路径。

    `--others --exclude-standard` 就是这三件事的交集。它与上面那条
    `check-ignore` 差一档，而差的正是那一档。
    """
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", scope],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return [p for p in proc.stdout.split("\0") if p]


def test_analysis_dir_is_fully_tracked():
    """`analysis/` 是证据目录：盘上有什么，库里就该有什么。

    已实测的事故（2026-11-11）：53 份 `_result.json` 里有 **5 份从未 `git add`**
    ——打板曲线、k 曲线、k\\* 条件、斜率溯源、premom CI 的全部读数。同一时刻
    `test_evidence_artifacts_are_shippable` 一直是绿的：它问的是「会不会被
    `.gitignore` 挡」，而 `git check-ignore` 对**未跟踪**文件返回 1（没被忽略），
    断言遂通过。于是「克隆拿得到」这句判断对「压根没人提交」完全失明。

    两问不同：`check-ignore` 答「挡没挡」，`ls-files --others` 答「在不在库里」。
    只问前者，等于判据只覆盖了射程的一半——这是「绿但无感知」的第三种形态：
    不是抽样把被测路径挡在射程外，而是**射程本身就是半句**。

    范围只到 `analysis/`，不外扩到全仓：产品代码的 WIP 在 `backend/` 与
    `scripts/`，那里未跟踪是常态，把它们拉进来只会教人删测试。证据目录不同，
    这个目录里每个文件都是某个数的出处，没有「草稿豁免」。

    新克隆上这条恒绿（文件都来自库），它只在 `commit` 之前有牙齿——那正是
    它该在的位置。
    """
    if not (REPO / ".git").exists():
        pytest.skip("没有 git 仓库，无从判断是否入库")
    tracked = len(subprocess.run(
        ["git", "ls-files", "-z", "--", "analysis"], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8").stdout.split("\0")) - 1
    assert tracked >= 150, f"analysis/ 只跟踪到 {tracked} 个文件 ⇒ 这条钉子没在看东西"
    stray = _untracked("analysis")
    assert not stray, (
        f"analysis/ 有 {len(stray)} 个文件在盘上、不在库里：\n  "
        + "\n  ".join(stray)
        + "\n要么 `git add`（它是某个数的出处），要么删掉（它不是）。")


def test_untracked_scope_reports_a_real_file(tmp_path):
    """`_untracked` 必须能报出一个真未跟踪的文件——否则上面那条恒绿。

    这不是重复：`_untracked` 的两个参数（`--others` 与 `--exclude-standard`）
    少任何一个都会静默变成「什么都没扫」。少了前者只剩已跟踪文件、恒空；
    少了后者会把 `__pycache__` 一起算进来、恒非空。两个方向都要能红。
    """
    import os
    if not (REPO / ".git").exists():
        pytest.skip("没有 git 仓库")
    if "PYTEST_XDIST_WORKER" in os.environ:
        pytest.skip("并行 worker 会把探针文件算进上面那条守卫，只能串行跑")
    probe = REPO / "analysis" / "_untracked_probe.tmp"
    ignored = REPO / "analysis" / "__pycache__" / "_untracked_probe.pyc"
    try:
        probe.write_text("x", encoding="utf-8")
        ignored.parent.mkdir(exist_ok=True)
        ignored.write_text("x", encoding="utf-8")
        assert "_untracked_probe.tmp" in "".join(_untracked("analysis")), (
            "未跟踪文件没被报出来 ⇒ 这条守卫对着空气断言")
        assert "_untracked_probe.pyc" not in "".join(_untracked("analysis")), (
            "被 .gitignore 挡住的文件也算进来了 ⇒ 它会天天假红")
    finally:
        for p in (probe, ignored):
            if p.exists():
                os.remove(p)


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
