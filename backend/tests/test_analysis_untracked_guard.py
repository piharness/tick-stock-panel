"""回归测试: `analysis/` 未入库文件的提交前阻断。

背景（2026-11-24）：`analysis/` 是「结论 + 可复现脚本」的目录（已入库 400+ 个
`_diag_*` 产物），但一次性试算也常写在这里。于是每次提交都要重做一遍判断，
而 `git status` 里它们只是几行 `??`，与构建产物混在一起看不出是研究出处。

判据：
  1. 有残留 → 默认 **exit 1 阻断**（本仓提交者是 agent，一条 stderr 会被滚过去，
     "只提示"等于没有）；
  2. `--warn-only` → 才 exit 0；
  3. 已入库的、被 `.gitignore` 忽略的、无关后缀的，都不许出现；
  4. 不在 git 仓库里时静默放行（打包运行环境不能因此报错）；
  5. 安装器幂等、能自动升级旧版、拒绝覆盖外来钩子；
  6. **钩子缺失/过期会被测试抓到** —— `.git/hooks/` 不随仓库分发，这是唯一的
     "换机器后忘了装"检测点。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import check_analysis_untracked as guard  # noqa: E402
import install_git_hooks as installer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "analysis").mkdir()
    (root / "analysis" / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


def test_clean_repo_is_silent(repo: Path, capsys):
    assert guard.main([]) == 0
    assert capsys.readouterr().err == ""


def test_untracked_file_blocks_by_default(repo: Path, capsys):
    (repo / "analysis" / "_diag_new.py").write_text("x = 1\n", encoding="utf-8")

    assert guard.main([]) == 1, "默认阻断：只提示会被工具输出淹没"
    err = capsys.readouterr().err
    assert "analysis/_diag_new.py" in err
    assert "git add" in err and "data/.scratch" in err, "必须给出两条可执行出路"
    assert "--no-verify" in err, "逃生口要写出来，避免用别的方式绕过"


def test_warn_only_can_be_opted_out(repo: Path):
    (repo / "analysis" / "_diag_new.py").write_text("x = 1\n", encoding="utf-8")
    assert guard.main(["--warn-only"]) == 0


@pytest.mark.parametrize("name", ["_diag_x.py", "_diag_x_findings.md", "_diag_x_result.json"])
def test_all_provenance_suffixes_are_reported(repo: Path, capsys, name):
    """三类都是"结论的出处"，留一个在工作区就是同一个待决问题。"""
    (repo / "analysis" / name).write_text("x\n", encoding="utf-8")

    assert guard.main([]) == 1
    assert f"analysis/{name}" in capsys.readouterr().err


def test_ignored_files_are_not_reported(repo: Path, capsys):
    (repo / ".gitignore").write_text("analysis/*_tmp.json\nanalysis/scratch_*\n", encoding="utf-8")
    (repo / "analysis" / "scratch_note.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "analysis" / "run_tmp.json").write_text("{}", encoding="utf-8")

    assert guard.main([]) == 0
    assert capsys.readouterr().err == ""


def test_other_suffixes_are_not_reported(repo: Path, capsys):
    (repo / "analysis" / "notes.txt").write_text("x\n", encoding="utf-8")
    assert guard.main([]) == 0
    assert capsys.readouterr().err == ""


def test_outside_git_repo_is_silent(tmp_path: Path, monkeypatch, capsys):
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert guard.main([]) == 0
    assert capsys.readouterr().err == ""


# ── 安装器 ──────────────────────────────────────────────────────────


def test_installer_writes_hook_and_is_idempotent(repo: Path):
    ok, msg = installer.install(repo)
    assert ok, msg
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    content = hook.read_text(encoding="utf-8")
    assert installer.OWNER_PREFIX in content
    assert "--warn-only" not in content, "钩子必须用默认（阻断）语义"
    assert installer.install(repo)[1] == "已是最新，无需改动"


def test_installer_upgrades_its_own_older_hook(repo: Path):
    """v1→v2 这类模板升级要自动覆盖，否则每次改模板都得手动 --force。"""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    old = hooks / "pre-commit"
    old.write_text(f"#!/bin/sh\n{installer.OWNER_PREFIX}1\nold body\n", encoding="utf-8")

    ok, msg = installer.install(repo)
    assert ok, msg
    assert "old body" not in old.read_text(encoding="utf-8")


def test_installer_refuses_to_clobber_foreign_hook(repo: Path):
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    foreign = hooks / "pre-commit"
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    ok, msg = installer.install(repo)
    assert not ok and "拒绝覆盖" in msg
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"

    ok, _ = installer.install(repo, force=True)
    assert ok
    assert installer.OWNER_PREFIX in foreign.read_text(encoding="utf-8")


def test_hook_never_blocks_on_missing_interpreter(repo: Path):
    """壳里任何解释器都跑不起来时必须 exit 0 —— 一次环境问题不该让人提交不了代码。"""
    ok, _ = installer.install(repo)
    assert ok
    hook = repo / ".git" / "hooks" / "pre-commit"

    completed = subprocess.run(
        ["sh", str(hook)], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/nonexistent"},
    )
    assert completed.returncode == 0


def test_hook_blocks_when_provenance_is_left_behind(repo: Path):
    """端到端：装了钩子之后再留一个文件，钩子必须返回 1。

    要用**带 python 的 PATH** 跑（临时仓库里没有 `backend/.venv`，而钩子找不到任何
    解释器时会按"环境问题不阻断"放行）。这里把当前解释器所在目录加进 PATH，
    模拟一个正常开发 shell —— 否则测的是"环境缺失"，不是"残留被拦"。
    """
    ok, _ = installer.install(repo)
    assert ok
    # 钩子调用的是**仓库自己**的 scripts/check_analysis_untracked.py；临时仓库里没有，
    # 钩子会按 fail-open 提前放行（这是刻意的：脚本不在了也不能拦住提交）。
    # 所以这里必须把真实的检查脚本一起放进去，否则测的是"脚本缺失"而不是"残留被拦"。
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "check_analysis_untracked.py").write_text(
        (REPO_ROOT / "scripts" / "check_analysis_untracked.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    hook = repo / ".git" / "hooks" / "pre-commit"
    (repo / "analysis" / "_diag_leftover.py").write_text("x = 1\n", encoding="utf-8")

    # 钩子的第一个候选是 `$root/backend/.venv/Scripts/python.exe`。临时仓库没有
    # venv，这里放一个指向真实解释器的 shim —— **不要改 PATH**：改了就会把 git
    # 也一起挤掉，`git rev-parse` 拿不到仓库根，钩子按 fail-open 直接放行，
    # 测出来的"通过"是假的（踩过一次）。
    shim = repo / "backend" / ".venv" / "Scripts" / "python.exe"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)

    completed = subprocess.run(["sh", str(hook)], cwd=repo, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "_diag_leftover.py" in (completed.stdout + completed.stderr)


# ── 换机器后忘了装钩子：靠这条测试抓 ────────────────────────────────


def test_this_repos_hook_is_installed_and_current():
    """本仓的钩子必须存在且与模板一致。

    `.git/hooks/` 不随仓库分发，所以"重新 clone 后忘了装"没有任何其他检测点。
    非 git 工作区（打包产物 / CI tarball）跳过而不是失败。
    """
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("不是 git 工作区")

    hook = REPO_ROOT / ".git" / "hooks" / "pre-commit"
    expected = installer.HOOK_TEMPLATE.format(marker=installer.MARKER)
    if not hook.is_file():
        pytest.fail("pre-commit 钩子未安装：运行 `python scripts/install_git_hooks.py`")
    actual = hook.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            "pre-commit 钩子已过期（模板变过）：运行 `python scripts/install_git_hooks.py`"
        )
