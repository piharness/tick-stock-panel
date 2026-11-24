"""回归测试: `analysis/` 未入库脚本的提交前提示。

背景（2026-11-24）：`analysis/` 是「结论与可复现脚本一起入库」的目录（已入库
400+ 个 `_diag_*` 产物），但一次性试算也常写在这里。于是每次提交都要重新判断
"这批算不算该交"——16 个脚本躺在工作区，`git status` 里只是几行 `??`。

判据：
  1. 有残留 → 打印出来（含两条可执行出路），默认 **exit 0 不阻断**；
  2. `--strict` → exit 1（给"要强制"的场景）；
  3. 已入库的、被 `.gitignore` 忽略的、非 `.py` 的，都不许出现在提示里；
  4. 不在 git 仓库里时静默放行（打包运行环境不能因此报错）；
  5. 安装器幂等，且**拒绝覆盖**不是自己装的钩子。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import check_analysis_untracked as guard  # noqa: E402
import install_git_hooks as installer  # noqa: E402


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
    assert guard.main(["--strict"]) == 0
    assert capsys.readouterr().err == ""


def test_untracked_script_is_reported_but_not_blocking(repo: Path, capsys):
    (repo / "analysis" / "_diag_new.py").write_text("x = 1\n", encoding="utf-8")

    assert guard.main([]) == 0, "默认只提示，不阻断提交"
    err = capsys.readouterr().err
    assert "analysis/_diag_new.py" in err
    assert "git add" in err and "data/.scratch" in err, "必须给出两条可执行出路"


def test_strict_mode_fails(repo: Path):
    (repo / "analysis" / "_diag_new.py").write_text("x = 1\n", encoding="utf-8")
    assert guard.main(["--strict"]) == 1


def test_ignored_files_are_not_reported(repo: Path, capsys):
    (repo / ".gitignore").write_text("analysis/*_tmp.json\nanalysis/scratch_*\n", encoding="utf-8")
    (repo / "analysis" / "scratch_note.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "analysis" / "run_tmp.json").write_text("{}", encoding="utf-8")

    assert guard.main([]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("name", ["_diag_x.py", "_diag_x_findings.md", "_diag_x_result.json"])
def test_all_provenance_suffixes_are_reported(repo: Path, capsys, name):
    """三类都是"结论的出处"，留一个在工作区就是同一个待决问题。"""
    (repo / "analysis" / name).write_text("x\n", encoding="utf-8")

    assert guard.main([]) == 0
    assert f"analysis/{name}" in capsys.readouterr().err


def test_other_suffixes_are_not_reported(repo: Path, capsys):
    (repo / "analysis" / "notes.txt").write_text("x\n", encoding="utf-8")
    assert guard.main(["--strict"]) == 0
    assert capsys.readouterr().err == ""


def test_tracked_script_is_not_reported(repo: Path, capsys):
    guard.main([])
    capsys.readouterr()
    assert guard.main(["--strict"]) == 0


def test_outside_git_repo_is_silent(tmp_path: Path, monkeypatch, capsys):
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert guard.main(["--strict"]) == 0
    assert capsys.readouterr().err == ""


# ── 安装器 ──────────────────────────────────────────────────────────


def test_installer_writes_hook_and_is_idempotent(repo: Path):
    ok, msg = installer.install(repo)
    assert ok, msg
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    assert installer.MARKER in hook.read_text(encoding="utf-8")
    assert installer.install(repo)[1] == "已是最新，无需改动"


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
    assert installer.MARKER in foreign.read_text(encoding="utf-8")


def test_hook_never_blocks_on_missing_interpreter(repo: Path):
    """壳里任何解释器都跑不起来时必须 exit 0 —— 否则一次环境问题就提交不了代码。"""
    hook = repo / ".git" / "hooks" / "pre-commit"
    ok, _ = installer.install(repo)
    assert ok

    # 用一个空 PATH 跑钩子：找不到 python，应当仍然 0
    completed = subprocess.run(
        ["sh", str(hook)], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/nonexistent"},
    )
    assert completed.returncode == 0
