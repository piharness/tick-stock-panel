#!/usr/bin/env python
"""把「要不要提交」从会话里拿掉：本仓的 commit / push 由这一个脚本独家负责。

## 为什么需要它（2026-11-28）

这台机器上的 WorkBuddy 要频繁切换账号（`~/.workbuddy/settings.json` 的
`claw.users` 里已经躺着 7 个 UID）。切换之后**会话内容与账号相关配置都会重置**，
于是每个新会话都要重新回答一遍：

- 工作区里这 17 个改动是什么？该不该交？交哪些？
- 消息怎么写？推不推？

这个判断的成本不在"做"上，而在**它每次都要重做，而且没人在场时不会被做**。
2026-11-28 11:52 的 FinalStop 事件里，最后一条助手消息的结尾正是「要提交吗？」——
那个问号就是本脚本要消灭的东西。

本仓的提交者只有 WorkBuddy（2026-11-24 已在 `check_analysis_untracked.py` 里
明确写下这个前提）。**唯一使用者 = 唯一维护者**，所以 git 的写权限可以整体
托管，不存在并发协商问题。

## 职责边界

- **只提交，不裁决内容。** 消息优先取 `.workbuddy/git/commit-msg.txt`（会话
  在交付前写下"这次改了什么"）；没有就退化成 `chore(autosync)` + 文件分布。
  **判断是可选的，留存不是。**
- **不改写历史。** 不做 merge，不做 `reset --hard`，**永不 force push**。远端
  分叉时只尝试一次 `pull --rebase --autostash`，失败就停下报告。
- **任何状态都不破坏仓库。** 合并/变基/冲突/游离 HEAD/index.lock 一律拒绝
  **提交**，但仍会写**快照**（`refs/wb/checkpoints`）—— 守卫拦的是 main 的
  整洁，不该连内容本身的留存也拦掉。

## 为什么快照用独立引用，而不是多提交几次 main

main 的历史是给人读的（一条提交 = 一个可解释的工作单元）。自动提交如果直接
落在 main 上，会把"每 20 秒一次"的机械记录混进研究编年史。所以快照走
`refs/wb/checkpoints`：用**临时 index**（`GIT_INDEX_FILE`）读树、`commit-tree`
造提交、`update-ref` 移动引用 —— 全程不碰工作区、不碰 `.git/index`、不碰 HEAD。
代价是它不能替代 main 的提交；收益是**它在任何仓库状态下都能跑**，包括冲突中。

## 状态机

| 检测到的状态 | 提交 | 推送 | 快照 |
|---|---|---|---|
| 干净 | – | 若领先则推 | – |
| 有改动 | ✓ | ✓ | ✓（提交前） |
| 合并/变基/拣选/二分进行中 | ✗ | ✗ | ✓ |
| 冲突（`--diff-filter=U` 非空） | ✗ | ✗ | ✓ |
| 游离 HEAD | ✗ | ✗ | ✓ |
| linked worktree | ✗ | ✗ | ✗（写的是别的检出） |
| `.git/index.lock` 存在 | ✗ | ✗ | ✗（index 被占） |
| 命中密钥规则 | ✗ | ✗ | ✓ |
| 未跟踪文件 > 20 MB | 跳过该文件 | ✓ | ✓ |

被拒绝时退出码 2（`--hook` 模式下恒为 0，钩子不该打断会话）。

## 三道触发（按"账号切换后还活着"排序）

1. `~/.wb-switch/hook.sh` 里追加的一段（Stop / FinalStop）—— **机器级，不随账号
   重置**。这是主力。安装：`--install`。
2. 仓库 `.git/hooks/post-commit`（推送）与 `pre-commit`（出处检查）—— 随 clone
   丢失，由 `AGENTS.md` 的会话开始协议补回。
3. `AGENTS.md` 里的一段协议 —— 账号切换后唯一必然还在的东西是**仓库本身**，
   所以策略必须写在仓库里，而不是写在会被重置的账号配置里。

## 用法

    python scripts/git_autosync.py                 # 提交 + 推送（人用）
    python scripts/git_autosync.py --status        # 只报告，不改任何东西
    python scripts/git_autosync.py --checkpoint    # 只写快照
    python scripts/git_autosync.py --install       # 装仓库钩子 + Stop 钩子（幂等）
    python scripts/git_autosync.py --uninstall     # 只摘掉 Stop 钩子那一段
    python scripts/git_autosync.py --hook          # 钩子调用：静默、恒 0、按签名去重
    python scripts/git_autosync.py --dry-run       # 说清会做什么，不做
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: 快照引用。放在 `refs/wb/` 下而不是 `refs/heads/`：它不是一个分支，不该出现在
#: 任何人的 `git branch` 列表或 PR 里。
CHECKPOINT_REF = "refs/wb/checkpoints"

#: 状态目录（日志/锁/消息暂存）。`.workbuddy/` 已被 gitignore，不会污染提交。
STATE_REL = Path(".workbuddy") / "git"

#: 快照保留条数。200 条覆盖数天的活跃工作；更老的只是占对象库。
MAX_CHECKPOINTS = 200

#: 单个未跟踪文件的上限。超过就跳过并在报告里点名 —— 静默放行会让 `git add -A`
#: 把一次模型下载或数据快照塞进历史，那种历史删不掉。
MAX_FILE_BYTES = 20 * 1024 * 1024

#: 单次 git 调用的超时。钩子里必须有上界：一个卡住的 push 会把会话收尾拖死。
GIT_TIMEOUT = 120
PUSH_TIMEOUT = 60

#: 默认的 git 命令超时。`--hook` 模式下用更短的上界。
SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥块"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "OpenAI 风格 key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b"
            r"\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
        ),
        "明文口令",
    ),
)

#: 密钥扫描的豁免：示例文件与测试夹具本来就要放假 key。
SECRET_EXEMPT = re.compile(r"(?:\.example|\.sample|\.template)$|(?:^|/)tests?/fixtures?/")

#: Stop 钩子那段脚本的边界标记。`--install` 靠它做幂等，`--uninstall` 靠它摘除。
STUB_BEGIN = "# >>> workbuddy git-autosync >>>"
STUB_END = "# <<< workbuddy git-autosync <<<"
STUB_PATH = Path.home() / ".wb-switch" / "hook.sh"


# ── git 基础 ──────────────────────────────────────────────────────────


def _run(
    cwd: Path,
    *args: str,
    timeout: int = GIT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """调 git。**必须显式 utf-8 解码**：Windows 默认 cp936，中文提交信息会乱码。"""
    full_env = dict(os.environ)
    full_env.pop("GIT_INDEX_FILE", None)  # 不继承外部临时 index
    full_env["GIT_PAGER"] = "cat"
    full_env["GIT_TERMINAL_PROMPT"] = "0"  # 绝不停在密码提示上
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=full_env,
    )


def git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT, env: dict[str, str] | None = None) -> str:
    return _run(cwd, *args, timeout=timeout, env=env).stdout.strip()


@dataclass
class Repo:
    root: Path
    git_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_REL


def discover(start: Path | None = None) -> Repo | None:
    """从 cwd 找到仓库根。不在仓库里返回 None（打包后的运行环境会走到这里）。"""
    cwd = start or Path.cwd()
    try:
        root = git(cwd, "rev-parse", "--show-toplevel", timeout=15)
        git_dir = git(cwd, "rev-parse", "--absolute-git-dir", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if not root:
        return None
    return Repo(root=Path(root), git_dir=Path(git_dir))


# ── 状态检测 ──────────────────────────────────────────────────────────


def _git_path(repo: Repo, name: str) -> Path:
    """`--git-path` 会尊重 worktree / 环境变量，比自己拼 `.git/xxx` 稳。

    它可能返回相对路径；因为所有 git 调用都以仓库根为 cwd，这个相对路径就是
    相对仓库根的，显式拼一次，免得调用方的 cwd 变化后判错。
    """
    try:
        path = Path(git(repo.root, "rev-parse", "--git-path", name, timeout=15))
    except (OSError, subprocess.SubprocessError):
        return repo.git_dir / name
    return path if path.is_absolute() else repo.root / path


def is_linked_worktree(repo: Repo) -> bool:
    """`.git/worktrees/<name>` 这类检出：提交会落到别的分支上，别碰。"""
    common = git(repo.root, "rev-parse", "--git-common-dir", timeout=15) or ".git"
    try:
        return Path(common).resolve() != repo.git_dir.resolve()
    except OSError:
        return False


def blockers(repo: Repo) -> list[str]:
    """返回阻断「提交」的原因（空列表 = 可以提交）。快照不受这些限制。"""
    found: list[str] = []
    if is_linked_worktree(repo):
        found.append("linked worktree：提交会落到别的检出，拒绝")
    for name, label in (
        ("MERGE_HEAD", "合并进行中（MERGE_HEAD）"),
        ("rebase-merge", "变基进行中（rebase-merge）"),
        ("rebase-apply", "变基/打补丁进行中（rebase-apply）"),
        ("CHERRY_PICK_HEAD", "拣选进行中（CHERRY_PICK_HEAD）"),
        ("REVERT_HEAD", "回滚进行中（REVERT_HEAD）"),
        ("BISECT_LOG", "二分进行中（BISECT_LOG）"),
        ("index.lock", "index 被占用（index.lock）"),
    ):
        if _git_path(repo, name).exists():
            found.append(label)
    if not git(repo.root, "symbolic-ref", "-q", "HEAD", timeout=15):
        found.append("游离 HEAD：没有分支可落，提交会丢")
    if git(repo.root, "diff", "--name-only", "--diff-filter=U", timeout=60):
        found.append("存在未解决的冲突")
    return found


def changed_entries(repo: Repo) -> list[tuple[str, str]]:
    """(状态码, 路径) 列表。用 porcelain=v1 的短格式，足够做计数与消息。"""
    out = git(repo.root, "status", "--porcelain=v1", "--untracked-files=all", timeout=60)
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        entries.append((line[:2].strip(), line[3:].strip().strip('"')))
    return entries


def status_signature(repo: Repo) -> str:
    """改动签名。Stop 与 FinalStop 会在同一秒各触发一次，靠它去重。"""
    raw = git(repo.root, "status", "--porcelain=v1", "--untracked-files=all", timeout=60)
    head = git(repo.root, "rev-parse", "HEAD", timeout=15)
    return hashlib.sha1(f"{head}\n{raw}".encode("utf-8", "replace")).hexdigest()


# ── 守卫 ──────────────────────────────────────────────────────────────


def oversized_untracked(repo: Repo) -> list[str]:
    out = _run(repo.root, "ls-files", "--others", "--exclude-standard", "-z", timeout=90).stdout
    too_big: list[str] = []
    for rel in filter(None, out.split("\0")):
        try:
            if (repo.root / rel).stat().st_size > MAX_FILE_BYTES:
                too_big.append(rel)
        except OSError:
            continue
    return too_big


def secret_hits(repo: Repo) -> list[tuple[str, int, str]]:
    """扫已暂存的改动。命中即拒绝提交 —— 这是 `git add -A` 自动化唯一真正危险的地方。"""
    changed = _run(
        repo.root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", timeout=60
    )
    names = changed.stdout
    if changed.returncode != 0:
        # 首次提交没有 HEAD 可 diff：退回「全部已暂存」。只在空仓库走到这里，代价可接受。
        names = _run(repo.root, "ls-files", "--cached", "-z", timeout=60).stdout
    hits: list[tuple[str, int, str]] = []
    for rel in filter(None, names.split("\0")):
        if SECRET_EXEMPT.search(rel):
            continue
        path = repo.root / rel
        try:
            if path.stat().st_size > 512 * 1024:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        if b"\0" in blob[:8192]:  # 二进制跳过
            continue
        text = blob.decode("utf-8", "replace")
        for pattern, label in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append((rel, line_no, label))
                break
    return hits


def whitespace_warnings(repo: Repo) -> list[str]:
    """`git diff --check` 的告警。**只记录不阻断**：markdown 表格里的行尾两空格是
    硬换行语法，拿它拦提交会让自动提交在文档改动上永远卡住，得不偿失。"""
    res = _run(repo.root, "diff", "--cached", "--check", timeout=60)
    return [line for line in (res.stdout + res.stderr).splitlines() if line.strip()][:10]


# ── 快照 ──────────────────────────────────────────────────────────────


def checkpoint(repo: Repo, *, reason: str, dry: bool = False) -> str | None:
    """把当前工作区写成一条快照提交，只动 `refs/wb/checkpoints`。

    用临时 index 而不是 `git stash create`：stash 会写 `refs/stash` 并在某些
    状态下拒绝执行；这里读树 + commit-tree 是纯读操作。

    临时 index（`.git/wb-checkpoint.index`）**用完不删**：本机的删除动作会被安全
    策略拦下并把进程杀掉（实测），而每次 `read-tree` 都会整体重置它，留着不串味。
    """
    index = repo.git_dir / "wb-checkpoint.index"
    env = {"GIT_INDEX_FILE": str(index)}
    head = _run(repo.root, "rev-parse", "--verify", "-q", "HEAD", timeout=15)
    has_head = head.returncode == 0
    try:
        _run(repo.root, "read-tree", "HEAD" if has_head else "--empty", env=env, timeout=60)
        added = _run(repo.root, "add", "-A", env=env, timeout=180)
        if added.returncode != 0:
            return None
        tree = git(repo.root, "write-tree", env=env, timeout=60)
        if not tree:
            return None
        prev = _run(repo.root, "rev-parse", "-q", "--verify", f"{CHECKPOINT_REF}^{{tree}}", timeout=15)
        if prev.returncode == 0 and prev.stdout.strip() == tree:
            return None  # 与上一个快照同树，不制造空档
        if dry:
            return f"(dry-run) {tree[:8]}"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        args = ["commit-tree", tree, "-m", f"snapshot {stamp} — {reason}"]
        # 父提交取**上一个快照**，不是 HEAD。取 HEAD 会让快照链把 main 的历史也串进来：
        # `rev-list --count` 数出来是 1237（含主线的 1200+ 条），按条数裁剪就失去意义。
        previous = git(repo.root, "rev-parse", "-q", "--verify", CHECKPOINT_REF, timeout=15)
        if previous:
            args += ["-p", previous]
        sha = git(repo.root, *args, env=env, timeout=60)
        if not sha:
            return None
        git(repo.root, "update-ref", CHECKPOINT_REF, sha, timeout=30)
        _prune(repo)
        return sha
    except (OSError, subprocess.SubprocessError):
        return None


def _prune(repo: Repo, *, keep: int = MAX_CHECKPOINTS) -> None:
    """快照链保留最近 `keep` 条。

    不能靠「把 ref 往回移」来裁剪：ref 只能指向某个提交，往回移丢掉的是**最新的**
    那一批。所以这里重造：取最近 keep 条的树（新→旧），反向重建成一条新的根链，
    再把 ref 指过去。旧的提交随即不可达，交给 `git gc` 回收。

    只在超过 2×keep 时触发一次（迟滞），避免每条快照都重造 200 个提交。
    """
    try:
        count = int(git(repo.root, "rev-list", "--count", CHECKPOINT_REF, timeout=60) or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return
    if count <= keep * 2:
        return
    try:
        trees = git(
            repo.root, "log", f"--max-count={keep}", "--format=%T", CHECKPOINT_REF, timeout=120
        ).split()
        rebuilt = ""
        for tree in reversed(trees):  # 旧 → 新，逐条挂到上一条后面
            args = ["commit-tree", tree, "-m", f"snapshot (pruned) {tree[:8]}"]
            if rebuilt:
                args += ["-p", rebuilt]
            rebuilt = git(repo.root, *args, timeout=60)
            if not rebuilt:
                return
        if rebuilt:
            git(repo.root, "update-ref", CHECKPOINT_REF, rebuilt, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return


# ── 消息 ──────────────────────────────────────────────────────────────


def _consume(path: Path) -> None:
    """消费掉一份一次性文件（提交消息）。

    不删：本机安全策略会拦截删除并把进程杀掉。改名同样达到"下次读不到"的效果，
    而且内容还在（可以追查某条提交的消息是从哪来的），失败再退化成清空。
    """
    try:
        path.replace(path.with_name(path.name + ".used"))
    except OSError:
        try:
            path.write_text("", encoding="utf-8")
        except OSError:
            pass


def intent_message(repo: Repo) -> str | None:
    """会话写下的"这次改了什么"。有就用，没有不勉强。"""
    path = repo.state_dir / "commit-msg.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


def auto_message(entries: list[tuple[str, str]], checkpoint_sha: str | None) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    dirs = Counter((path.split("/")[0] if "/" in path else "(根目录)") for _, path in entries)
    scope = " | ".join(f"{name} {count}" for name, count in dirs.most_common(4))
    subject = f"chore(autosync): {stamp} 会话留存（{len(entries)} 文件）"
    body = [
        f"改动分布：{scope}",
        "",
        "无人工在场的兜底提交：消息由 scripts/git_autosync.py 生成。",
        "要更好的历史就把结论写进 .workbuddy/git/commit-msg.txt —— 判断是可选的，留存不是。",
    ]
    if checkpoint_sha:
        body.append(f"写入快照：{CHECKPOINT_REF} @ {checkpoint_sha[:8]}")
    return subject + "\n\n" + "\n".join(body)


# ── 提交与推送 ────────────────────────────────────────────────────────


def commit_all(repo: Repo, message: str, *, force: bool = False) -> tuple[str | None, list[str], list[str]]:
    """返回 (提交 sha | None, 跳过的大文件, 说明)。"""
    skipped = oversized_untracked(repo)
    added = _run(repo.root, "add", "-A", timeout=180)
    if added.returncode != 0:
        return None, skipped, [f"git add 失败：{added.stderr.strip()[:200]}"]
    for rel in skipped:
        _run(repo.root, "reset", "-q", "--", rel, timeout=60)

    hits = secret_hits(repo)
    if hits and not force:
        return None, skipped, [
            f"密钥扫描命中：{path}:{line}（{label}）" for path, line, label in hits
        ]

    msg_file = repo.state_dir / "last-message.txt"
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    msg_file.write_text(message.rstrip() + "\n", encoding="utf-8")

    res = _run(repo.root, "commit", "-s", "-F", str(msg_file), timeout=180)
    if res.returncode != 0:
        detail = (res.stdout + res.stderr).strip()
        if "nothing to commit" in detail or "无文件要提交" in detail:
            return None, skipped, []
        return None, skipped, [f"git commit 失败：{detail[:400]}"]
    return git(repo.root, "rev-parse", "HEAD", timeout=15), skipped, []


def push(repo: Repo) -> tuple[bool | None, str]:
    """(是否推送成功 | None=无需/无远端, 说明)。永不 force。"""
    if not git(repo.root, "remote", timeout=15):
        return None, "无远端，仅本地提交"
    branch = git(repo.root, "symbolic-ref", "--short", "-q", "HEAD", timeout=15)
    if not branch:
        return None, "游离 HEAD，跳过推送"
    upstream = git(repo.root, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}", timeout=15)
    ahead = 0
    if upstream:
        try:
            ahead = int(git(repo.root, "rev-list", "--count", f"{upstream}..{branch}", timeout=60) or 0)
        except ValueError:
            ahead = 1
        if ahead == 0:
            return None, "已与远端一致"

    res = _run(repo.root, "push", timeout=PUSH_TIMEOUT)
    if res.returncode != 0 and upstream:
        # 分叉：只尝试一次 rebase，失败立刻 abort —— 绝不把仓库留在变基中间态。
        rebase = _run(repo.root, "pull", "--rebase", "--autostash", timeout=PUSH_TIMEOUT)
        if rebase.returncode != 0:
            if _git_path(repo, "rebase-merge").exists() or _git_path(repo, "rebase-apply").exists():
                _run(repo.root, "rebase", "--abort", timeout=60)
            return False, f"远端分叉且 rebase 失败，已保持本地提交：{rebase.stderr.strip()[:200]}"
        res = _run(repo.root, "push", timeout=PUSH_TIMEOUT)
    if res.returncode != 0 and not upstream:
        res = _run(repo.root, "push", "-u", "origin", branch, timeout=PUSH_TIMEOUT)
    if res.returncode != 0:
        return False, f"push 失败：{res.stderr.strip()[:200]}"
    return True, f"已推送 {branch}"


# ── 主流程 ────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    state: str = "clean"
    commit: str | None = None
    checkpoint: str | None = None
    pushed: bool | None = None
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.state.startswith("blocked")

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "commit": self.commit,
            "checkpoint": self.checkpoint,
            "pushed": self.pushed,
            "notes": self.notes,
            "skipped_big_files": self.skipped,
        }


def _state(repo: Repo) -> dict:
    try:
        return json.loads((repo.state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(repo: Repo, data: dict) -> None:
    try:
        repo.state_dir.mkdir(parents=True, exist_ok=True)
        (repo.state_dir / "state.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass


def _log(repo: Repo, payload: dict) -> None:
    try:
        repo.state_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"), **payload}, ensure_ascii=False
        )
        with (repo.state_dir / "autosync.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


@contextmanager
def _lock(repo: Repo):
    """进程间互斥（Stop 与 FinalStop 会背靠背触发，两个进程同时 `git add` 会抢 index）。

    刻意**不用**「建文件=持锁 / 删文件=释放」那套：本机的安全策略会拦截删除动作并
    把进程无声杀掉（实测 `Path.unlink` 让进程直接退出 1、连异常栈都不留），而崩溃
    残留的锁文件会让后面每一次同步空转十分钟。改用操作系统级建议锁：句柄一关就
    释放，进程被 kill 也不留残骸。
    """
    try:
        repo.state_dir.mkdir(parents=True, exist_ok=True)
        handle = (repo.state_dir / "autosync.lock").open("a+b")
    except OSError:
        yield False
        return
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()  # 关句柄即释放；不显式解锁，少一条会抛异常的路径


def sync(
    repo: Repo,
    *,
    push_enabled: bool = True,
    force: bool = False,
    dry: bool = False,
    dedupe: bool = False,
) -> Outcome:
    outcome = Outcome()
    if is_linked_worktree(repo):
        outcome.state = "blocked:linked-worktree"
        outcome.notes.append("这是 linked worktree，交给主检出处理")
        return outcome

    reasons = blockers(repo)
    entries = changed_entries(repo)

    if dedupe:
        signature = status_signature(repo)
        previous = _state(repo)
        if previous.get("sig") == signature and time.time() - float(previous.get("ts", 0)) < 600:
            outcome.state = "deduped"
            # 也记一笔：否则"钩子到底跑没跑"在日志里查不到，成了一次静默的绿灯。
            _log(repo, {"mode": "sync", "state": "deduped"})
            return outcome

    if reasons:
        outcome.state = "blocked:" + reasons[0].split("（")[0]
        outcome.notes.extend(reasons)
        if not dry:
            outcome.checkpoint = checkpoint(repo, reason=reasons[0])
        _log(repo, {"mode": "sync", "state": outcome.state, **outcome.as_dict()})
        return outcome

    if not entries:
        if push_enabled:
            if dry:
                outcome.notes.append("dry-run：会检查是否需要推送")
            else:
                outcome.pushed, note = push(repo)
                if note:
                    outcome.notes.append(note)
        outcome.state = "clean"
        _save_state(repo, {"sig": status_signature(repo), "ts": time.time()})
        return outcome

    outcome.checkpoint = checkpoint(repo, reason=f"{len(entries)} 项改动待提交", dry=dry)
    if dry:
        outcome.state = "dirty"
        outcome.notes.append(f"会提交 {len(entries)} 项改动")
        return outcome

    message = intent_message(repo) or auto_message(entries, outcome.checkpoint)
    sha, skipped, problems = commit_all(repo, message, force=force)
    outcome.skipped = skipped
    if problems:
        outcome.state = "blocked:commit"
        outcome.notes.extend(problems)
        _log(repo, {"mode": "sync", "state": outcome.state, **outcome.as_dict()})
        return outcome
    outcome.commit = sha
    if sha:
        _consume(repo.state_dir / "commit-msg.txt")
        if skipped:
            outcome.notes.append(f"跳过 {len(skipped)} 个超过 {MAX_FILE_BYTES // 1048576} MB 的文件")
        if push_enabled:
            outcome.pushed, note = push(repo)
            if note:
                outcome.notes.append(note)
    outcome.state = "committed" if sha else "clean"
    if skipped:
        outcome.state = "committed:partial"
    _save_state(repo, {"sig": status_signature(repo), "ts": time.time()})
    _log(repo, {"mode": "sync", **outcome.as_dict()})
    return outcome


# ── 安装 ──────────────────────────────────────────────────────────────


def _stub_block(repo: Repo) -> str:
    """生成追加到 `~/.wb-switch/hook.sh` 的那段。

    路径写死是**故意的**：安装器本来就是机器相关的产物，而 `$payload` 里的
    JSON 用反斜杠转义，靠 sed 抠 `cwd` 迟早出错。用仓库目录名做一次廉价过滤，
    避免在别的项目里也触发。
    """
    root = repo.root.as_posix()
    python = repo.root / "backend" / ".venv" / "Scripts" / "python.exe"
    return f"""{STUB_BEGIN}
# 由 `python scripts/git_autosync.py --install` 写入（2026-11-28）。
# 每次 Stop / FinalStop 把 tick-stock-panel 的改动作业提交并推送：这台机器频繁
# 切换账号，会话会被重置，git 是唯一还活着的记忆。摘除：--uninstall。
case "$payload" in
*{repo.root.name}*)
    if [ -x '{python.as_posix()}' ]; then
        ( cd '{root}' && '{python.as_posix()}' scripts/git_autosync.py --hook ) >/dev/null 2>&1
    fi
    ;;
esac
{STUB_END}
"""


def install_stub(repo: Repo) -> tuple[bool, str]:
    if not STUB_PATH.is_file():
        return False, f"未找到 {STUB_PATH}（本机没有账号切换器，跳过；仓库钩子仍有效）"
    text = STUB_PATH.read_text(encoding="utf-8", errors="replace")
    block = _stub_block(repo)
    if STUB_BEGIN in text:
        start = text.index(STUB_BEGIN)
        end = text.find(STUB_END, start)
        end = len(text) if end < 0 else end + len(STUB_END)
        current = text[start:end].rstrip()
        if current == block.rstrip():
            return True, "Stop 钩子已是最新"
        STUB_PATH.write_text(text[:start] + block + text[end:], encoding="utf-8", newline="\n")
        return True, "Stop 钩子已更新"
    STUB_PATH.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8", newline="\n")
    return True, "Stop 钩子已写入"


def uninstall_stub() -> tuple[bool, str]:
    if not STUB_PATH.is_file():
        return False, f"未找到 {STUB_PATH}"
    text = STUB_PATH.read_text(encoding="utf-8", errors="replace")
    if STUB_BEGIN not in text:
        return True, "Stop 钩子里没有本工具的段落，无需处理"
    start = text.index(STUB_BEGIN)
    end = text.find(STUB_END, start)
    end = len(text) if end < 0 else end + len(STUB_END)
    STUB_PATH.write_text((text[:start] + text[end:]).rstrip() + "\n", encoding="utf-8", newline="\n")
    return True, "Stop 钩子段落已摘除"


def install(repo: Repo) -> list[str]:
    lines: list[str] = []
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import install_git_hooks

        ok, message = install_git_hooks.install(repo.root)
        lines.append(("仓库钩子：" if ok else "仓库钩子失败：") + message)
    except Exception as exc:  # noqa: BLE001 — 安装器坏了不该让整个安装失败
        lines.append(f"仓库钩子安装异常：{exc}")
    ok, message = install_stub(repo)
    lines.append(("Stop 钩子：" if ok else "Stop 钩子跳过：") + message)
    return lines


# ── 报告 ──────────────────────────────────────────────────────────────


def report(repo: Repo) -> str:
    entries = changed_entries(repo)
    reasons = blockers(repo)
    branch = git(repo.root, "symbolic-ref", "--short", "-q", "HEAD", timeout=15) or "(游离)"
    upstream = git(repo.root, "rev-parse", "--abbrev-ref", "@{upstream}", timeout=15)
    ahead = behind = 0
    if upstream:
        try:
            counts = git(repo.root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD", timeout=60)
            behind, ahead = (int(x) for x in counts.split())
        except (ValueError, OSError):
            pass
    checkpoint_sha = git(repo.root, "rev-parse", "-q", "--verify", CHECKPOINT_REF, timeout=15)
    checkpoint_count = ""
    if checkpoint_sha:
        try:
            checkpoint_count = git(repo.root, "rev-list", "--count", CHECKPOINT_REF, timeout=60)
        except (OSError, subprocess.SubprocessError):
            checkpoint_count = "?"
    stub_ok = STUB_PATH.is_file() and STUB_BEGIN in STUB_PATH.read_text(
        encoding="utf-8", errors="replace"
    )
    post_hook = repo.git_dir / "hooks" / "post-commit"

    dirs = Counter((path.split("/")[0] if "/" in path else "(根目录)") for _, path in entries)
    lines = [
        f"仓库    {repo.root}  ({branch}"
        + (f"，领先 {ahead} / 落后 {behind})" if upstream else "，无上游)")
        ,
        f"状态    {'干净' if not entries else f'{len(entries)} 项改动'}"
        + (f"：{' | '.join(f'{k} {v}' for k, v in dirs.most_common(5))}" if entries else ""),
        f"阻断    {'、'.join(reasons) if reasons else '无'}",
        f"钩子    post-commit {'✓' if post_hook.is_file() else '✗'}"
        f"   Stop 段 {'✓' if stub_ok else '✗'}（{STUB_PATH}）",
        f"快照    {CHECKPOINT_REF} {('@ ' + checkpoint_sha[:8] + f'  共 {checkpoint_count} 条') if checkpoint_sha else '(空)'}",
    ]
    return "\n".join(lines)


# ── 入口 ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本仓 git 的唯一维护入口")
    parser.add_argument("--status", action="store_true", help="只报告，不改任何东西")
    parser.add_argument("--checkpoint", action="store_true", help="只写快照，不提交不推送")
    parser.add_argument("--install", action="store_true", help="安装仓库钩子与 Stop 钩子（幂等）")
    parser.add_argument("--uninstall", action="store_true", help="摘除 Stop 钩子段落")
    parser.add_argument("--post-commit", action="store_true", help="post-commit 钩子用：只推送")
    parser.add_argument("--hook", action="store_true", help="钩子模式：静默、恒 0、按签名去重")
    parser.add_argument("--no-push", action="store_true", help="只提交不推送")
    parser.add_argument("--force", action="store_true", help="命中密钥也提交（需自行确认）")
    parser.add_argument("--dry-run", action="store_true", help="只说明会做什么")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    repo = discover()
    if repo is None:
        if not args.hook:
            print("当前目录不是 git 仓库", file=sys.stderr)
        return 0 if args.hook else 2

    if args.install:
        for line in install(repo):
            print(line)
        return 0
    if args.uninstall:
        ok, message = uninstall_stub()
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2
    if args.status:
        print(report(repo))
        return 0

    if args.post_commit:
        # 刻意不加锁：post-commit 是父进程 `git commit` 里长出来的子进程，而父进程
        # 正握着锁。在这里抢锁等于永远推不出去 —— 推送本身是并发安全的。
        if args.no_push:
            return 0
        pushed, note = push(repo)
        _log(repo, {"mode": "post-commit", "pushed": pushed, "note": note})
        return 0

    with _lock(repo) as acquired:
        if not acquired:
            if not args.hook:
                print("另一个 autosync 正在跑，跳过", file=sys.stderr)
            return 0
        if args.checkpoint:
            sha = checkpoint(repo, reason="手动快照", dry=args.dry_run)
            if not args.hook:
                print(f"快照：{sha or '无变化'}")
            return 0
        outcome = sync(
            repo,
            push_enabled=not args.no_push,
            force=args.force,
            dry=args.dry_run,
            dedupe=args.hook,
        )

    if args.hook:
        return 0
    if args.json:
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=1))
    else:
        head = {
            "clean": "工作区干净",
            "committed": "已提交并推送" if outcome.pushed else "已提交",
            "committed:partial": "已提交（有大文件被跳过）",
            "dirty": "有改动待提交（dry-run）",
            "deduped": "刚刚跑过，跳过",
        }.get(outcome.state, f"未提交（{outcome.state}）")
        print(head)
        if outcome.commit:
            print(f"  提交  {outcome.commit[:8]}")
        if outcome.checkpoint:
            print(f"  快照  {outcome.checkpoint[:8]}")
        for note in outcome.notes:
            print(f"  · {note}")
        for rel in outcome.skipped:
            print(f"  · 跳过（过大）{rel}")
    return 2 if outcome.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
