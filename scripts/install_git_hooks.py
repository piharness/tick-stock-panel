"""安装仓库自带的 git 钩子（pre-commit + post-commit）。

## 为什么是「脚本 + 薄壳」而不是 pre-commit 框架

只有一项检查，为它引入 `pre-commit` 依赖（外加首次运行要 `pre-commit install`）
不划算。这里把**逻辑**放在受版本控制的 `scripts/check_analysis_untracked.py`，
钩子只留一段几乎不会变形的壳去调用它 —— 壳坏了逻辑还在，逻辑改了壳不用动。

代价要说清：`.git/hooks/` **不随仓库分发**。换机器/重新 clone 后要再跑一次本脚本
才生效。这与框架方案的成本相同（框架也需要 `pre-commit install`），区别只是
不用装依赖。

## 用法

    python scripts/install_git_hooks.py            # 安装（幂等）
    python scripts/install_git_hooks.py --force    # 覆盖不相干的既有钩子
"""
from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

#: 归属前缀。用**前缀**而不是全等来判归属：升级钩子内容时（v1→v2）要能自动覆盖
#: 旧版，否则每次改模板都得让人 `--force` 一次。
OWNER_PREFIX = "# tick-stock-panel hook v"

#: 钩子内容。`{marker}` 写在前缀后，标明版本。
HOOK_TEMPLATE = """#!/bin/sh
# {marker}
# 在本仓提交前提示 analysis/ 下未入库的脚本。安装器：scripts/install_git_hooks.py
#
# 任何异常都放行（exit 0）：钩子的职责是提醒，不是拦路。若某个解释器跑不起来
# 就换下一个，全都不行就静默跳过——绝不因为一次环境问题让人提交不了代码。
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -n "$root" ] || exit 0
script="$root/scripts/check_analysis_untracked.py"
[ -f "$script" ] || exit 0
for py in "$root/backend/.venv/Scripts/python.exe" "$root/backend/.venv/bin/python" python3 python; do
    "$py" "$script" 2>&1
    status=$?
    # 0 = 干净；1 = 有残留（拦下，别再试别的解释器）；其它 = 解释器跑不起来，换下一个
    if [ "$status" = "0" ]; then exit 0; fi
    if [ "$status" = "1" ]; then exit 1; fi
done
exit 0
"""

#: post-commit：提交后把当前分支推到 origin。
#:
#: 为什么要它：`scripts/git_autosync.py` 在提交时可能**自己**推不出去（离线、
#: 远端分叉），而"本地有提交、origin 没有"正是账号切换后最容易丢的状态 ——
#: 换台机器/换个账号看不到任何东西。把它挪到 post-commit 上，任何一条提交
#: （不管是谁提交的）都会顺手推一次，不变式变成"本地永远等于 origin"。
#:
#: 静默是硬要求：钩子失败绝不能打断提交。细节落 `.workbuddy/git/autosync.log`。
POST_COMMIT_TEMPLATE = """#!/bin/sh
# {marker}
# 提交后把当前分支推到 origin（永不 force）。摘除：WB_AUTOSYNC_NO_PUSH=1 或删本文件。
[ "x$WB_AUTOSYNC_NO_PUSH" = "x1" ] && exit 0
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -n "$root" ] || exit 0
script="$root/scripts/git_autosync.py"
[ -f "$script" ] || exit 0
for py in "$root/backend/.venv/Scripts/python.exe" "$root/backend/.venv/bin/python" python3 python; do
    if [ -x "$py" ] || command -v "$py" >/dev/null 2>&1; then
        "$py" "$script" --hook --post-commit >/dev/null 2>&1
        exit 0
    fi
done
exit 0
"""

MARKER = f"{OWNER_PREFIX}3"

#: 安装器自己认的"无事可做"文案。`install()` 汇总多条钩子时靠它判断全都没变。
UP_TO_DATE = "已是最新，无需改动"


def hooks_dir(repo_root: Path) -> Path:
    """尊重 `core.hooksPath`：显式配置过就别往默认位置写。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
        configured = out.stdout.strip()
    except Exception:  # noqa: BLE001 — 没配置/没有 git 都走默认
        configured = ""
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (repo_root / path)
    return repo_root / ".git" / "hooks"


def _install_one(
    repo_root: Path, name: str, template: str, *, force: bool = False
) -> tuple[bool, str, bool]:
    """装一条钩子。返回 (成功与否, 说明, 内容是否变化)。"""
    directory = hooks_dir(repo_root)
    target = directory / name
    content = template.format(marker=MARKER)

    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if existing == content:
            return True, UP_TO_DATE, False
        if OWNER_PREFIX not in existing and not force:
            return False, (
                f"{target} 已存在且不是本工具安装的，拒绝覆盖。"
                "确认要替换请加 --force（原文件建议先备份）。"
            ), False

    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    # git 要求钩子可执行；Windows 上该位无意义，失败也不影响
    try:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return True, f"已安装: {target}", True


def install(repo_root: Path, *, force: bool = False) -> tuple[bool, str]:
    """返回 (是否安装成功, 说明)。全部已是最新时返回 (True, "已是最新，无需改动")。

    按 pre-commit → post-commit 顺序装；前一条被拒就整体返回失败，避免"装了一半"
    这种更难解释的状态。
    """
    hooks = (("pre-commit", HOOK_TEMPLATE), ("post-commit", POST_COMMIT_TEMPLATE))
    messages: list[str] = []
    changed = False
    for name, template in hooks:
        ok, message, touched = _install_one(repo_root, name, template, force=force)
        if not ok:
            return False, message
        messages.append(message)
        changed = changed or touched
    return True, "；".join(messages) if changed else UP_TO_DATE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装仓库自带 git 钩子")
    parser.add_argument("--force", action="store_true", help="覆盖非本工具安装的钩子")
    args = parser.parse_args(argv)

    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True,
        )
        root = Path(out.stdout.strip())
    except Exception:  # noqa: BLE001
        print("当前目录不是 git 仓库，无法安装钩子", file=sys.stderr)
        return 1

    ok, message = install(root, force=args.force)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
