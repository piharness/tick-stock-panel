"""回归测试：`scripts/git_autosync.py` 的提交/推送/快照判据。

背景（2026-11-28）：这台机器的 WorkBuddy 要频繁切换账号，切换后会话与账号配置
重置，于是"要不要提交"这个判断每次都要重做，而且**没人在场时不会被做**
（2026-11-28 11:52 的 FinalStop 记录里，最后一条助手消息的结尾就是「要提交吗？」）。
本仓提交者只有 WorkBuddy，所以 git 托管给它。

判据：
  1. 有改动 → 提交，消息优先取 `.workbuddy/git/commit-msg.txt`，取完即消费；
  2. 无改动 → 不制造空提交（Stop 与 FinalStop 背靠背触发，靠签名去重）；
  3. 合并/变基/冲突/游离 HEAD → **不提交**，但**要写快照**（守卫拦的是 main 的
     整洁，不该连内容留存也拦掉）；
  4. 命中密钥 → 不提交、快照仍在、文件原样留在盘上；
  5. 超过 20 MB 的未跟踪文件 → 跳过并点名，不拖累其余改动；
  6. 无远端 → 仍提交，只是不推送；
  7. 安装器幂等，`--uninstall` 只摘自己那段，不动别人的内容；
  8. **本仓的 post-commit 必须已安装** —— `.git/hooks/` 不随仓库分发，这是唯一
     的"换机器后忘了装"检测点。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import git_autosync as autosync  # noqa: E402
import install_git_hooks as installer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 运行时拼出来，避免测试文件自己命中密钥规则（它会被 autosync 扫描并拒绝提交）。
AWS_LIKE = "AKIA" + "ABCDEFGHIJKLMNOP"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    # 真实仓库里 `.workbuddy/` 已被忽略；临时仓库必须同样处理，否则状态文件自己会被提交
    (root / ".gitignore").write_text(".workbuddy/\n", encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


def _sync(repo: Path, **kwargs) -> autosync.Outcome:
    kwargs.setdefault("push_enabled", False)
    found = autosync.discover()
    assert found is not None
    return autosync.sync(found, **kwargs)


def test_clean_repo_makes_no_commit(repo: Path):
    before = _git(repo, "rev-parse", "HEAD")

    outcome = _sync(repo)

    assert outcome.state == "clean"
    assert _git(repo, "rev-parse", "HEAD") == before, "干净工作区不该制造空提交"


def test_dirty_tree_is_committed(repo: Path):
    (repo / "notes.md").write_text("结论\n", encoding="utf-8")

    outcome = _sync(repo)

    assert outcome.state == "committed"
    assert outcome.commit and len(outcome.commit) == 40
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "log", "-1", "--pretty=%s").startswith("chore(autosync):")
    assert "Signed-off-by" in _git(repo, "log", "-1", "--pretty=%B")


def test_intent_message_is_used_and_consumed(repo: Path):
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    intent = repo / ".workbuddy" / "git" / "commit-msg.txt"
    intent.parent.mkdir(parents=True)
    intent.write_text("feat(probe): 换成同列口径\n\n正文\n", encoding="utf-8")

    outcome = _sync(repo)

    assert outcome.state == "committed"
    assert _git(repo, "log", "-1", "--pretty=%s") == "feat(probe): 换成同列口径"
    assert not intent.exists(), "消息用过就该消费掉，否则下次会重复挂在别的提交上"


def test_second_sync_is_noop(repo: Path):
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    _sync(repo)
    head = _git(repo, "rev-parse", "HEAD")

    outcome = _sync(repo)

    assert outcome.state == "clean"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_dedupe_skips_back_to_back_hook_runs(repo: Path):
    """Stop 与 FinalStop 会在同一秒各触发一次，第二次必须是空转。"""
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    first = _sync(repo, dedupe=True)
    assert first.state == "committed"

    second = _sync(repo, dedupe=True)

    assert second.state == "deduped"
    assert second.commit is None


def test_secret_blocks_commit_but_still_snapshots(repo: Path):
    (repo / "leak.py").write_text(f'KEY = "{AWS_LIKE}"\n', encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD")

    outcome = _sync(repo)

    assert outcome.blocked and outcome.state == "blocked:commit"
    assert any("密钥" in note for note in outcome.notes)
    assert _git(repo, "rev-parse", "HEAD") == head, "命中密钥绝不能进历史"
    assert (repo / "leak.py").exists(), "也不许为了干净而删文件"
    assert autosync.git(repo, "rev-parse", "-q", "--verify", autosync.CHECKPOINT_REF), (
        "被守卫拦下时，内容仍必须落到快照里"
    )


def test_oversized_untracked_is_skipped_not_fatal(repo: Path):
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    big = repo / "blob.bin"
    with big.open("wb") as fh:
        fh.truncate(autosync.MAX_FILE_BYTES + 1)

    outcome = _sync(repo)

    assert outcome.state == "committed:partial"
    assert "blob.bin" in outcome.skipped
    tracked = _git(repo, "ls-files")
    assert "blob.bin" not in tracked
    assert "notes.md" in tracked, "大文件只该拖累自己"


def test_merge_in_progress_blocks_but_snapshots(repo: Path):
    (repo / ".git" / "MERGE_HEAD").write_text(_git(repo, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD")

    outcome = _sync(repo)

    assert outcome.blocked and "合并" in outcome.state
    assert _git(repo, "rev-parse", "HEAD") == head
    assert outcome.checkpoint, "合并中不提交，但快照必须写"


def test_checkpoint_does_not_touch_index_or_head(repo: Path):
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    before_status = _git(repo, "status", "--porcelain")
    before_head = _git(repo, "rev-parse", "HEAD")

    sha = autosync.checkpoint(autosync.discover(), reason="test")

    assert sha
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "status", "--porcelain") == before_status, (
        "快照用临时 index，绝不能把工作区的东西顺手暂存了"
    )


def test_push_is_skipped_without_remote(repo: Path):
    (repo / "notes.md").write_text("x\n", encoding="utf-8")

    outcome = _sync(repo, push_enabled=True)

    assert outcome.state == "committed"
    assert outcome.pushed is None
    assert any("无远端" in note for note in outcome.notes)


def test_linked_worktree_is_refused(repo: Path, tmp_path: Path, monkeypatch):
    other = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(other), "-b", "side")
    monkeypatch.chdir(other)

    outcome = autosync.sync(autosync.discover(), push_enabled=False)

    assert outcome.state == "blocked:linked-worktree"
    assert not outcome.checkpoint, "别在别人的检出里写快照"


def test_install_writes_both_hooks_and_is_idempotent(repo: Path):
    ok, message = installer.install(repo)
    assert ok, message
    assert (repo / ".git" / "hooks" / "pre-commit").is_file()
    assert (repo / ".git" / "hooks" / "post-commit").is_file()
    assert installer.install(repo)[1] == installer.UP_TO_DATE


def test_post_commit_hook_pushes_quietly_when_nothing_to_do(repo: Path):
    """没有远端时 post-commit 必须静默成功 —— 钩子失败绝不能打断提交。"""
    installer.install(repo)
    hook = repo / ".git" / "hooks" / "post-commit"

    completed = subprocess.run(
        ["sh", str(hook)], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/nonexistent"},
    )

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_stub_install_is_idempotent_and_reversible(repo: Path, monkeypatch, tmp_path: Path):
    stub = tmp_path / "hook.sh"
    original = "#!/bin/sh\npayload=$(cat)\nprintf '{}'\n"
    stub.write_text(original, encoding="utf-8")
    monkeypatch.setattr(autosync, "STUB_PATH", stub)

    ok, message = autosync.install_stub(autosync.discover())
    assert ok, message
    first = stub.read_text(encoding="utf-8")
    assert autosync.STUB_BEGIN in first
    assert "printf '{}'" in first, "别人的那段必须原样保留"

    ok, message = autosync.install_stub(autosync.discover())
    assert ok and "已是最新" in message
    assert stub.read_text(encoding="utf-8") == first

    ok, message = autosync.uninstall_stub()
    assert ok, message
    assert stub.read_text(encoding="utf-8").strip() == original.strip()


# ── 换机器后忘了装钩子：靠这条测试抓 ────────────────────────────────


def test_this_repos_hooks_are_installed_and_current():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("不是 git 工作区")
    for name, template in (("pre-commit", installer.HOOK_TEMPLATE),
                           ("post-commit", installer.POST_COMMIT_TEMPLATE)):
        hook = REPO_ROOT / ".git" / "hooks" / name
        if not hook.is_file():
            pytest.fail(f"{name} 钩子未安装：运行 `python scripts/git_autosync.py --install`")
        if hook.read_text(encoding="utf-8") != template.format(marker=installer.MARKER):
            pytest.fail(f"{name} 钩子已过期：运行 `python scripts/git_autosync.py --install`")
