"""提交前提示：`analysis/` 下有没有「没入库、也没被忽略」的新脚本。

## 为什么需要它

这个仓库的研究惯例是**结论与可复现脚本一起入库**（`analysis/` 里已有 400+ 个
`_diag_*` 产物）。但"写脚本"和"决定这批算不算该交"是两件事，后者每次提交都要
重做一遍判断：2026-11-24 就有 16 个脚本躺在工作区，一次 `git add` 时的取舍需要
人回忆"这条线写完了吗、文档引用了哪些"。

`git status` 里它们只是若干 `??` 行，与构建产物、临时文件混在一起，看不出
"这些是研究出处"。等到某天有人 `git add -A`，要么把半成品提进去，要么把已写结论
的出处漏掉。

## 判据

只看 `analysis/` 下**未被跟踪且未被忽略**的 `.py` / `.md` / `.json`：

- **已入库的**不动（棘轮之外的历史，不做批量治理）；
- **被 `.gitignore` 忽略的**不管 —— 那是作者明确表示"这是临时文件"；
- 命中的一律提示，并给出两条可执行的路：`git add` 或移进 `data/.scratch/`。

## 为什么默认阻断而不是"提示一下"

本仓的提交者只有 AI agent（2026-11-24 明确）。对 agent 来说，"只提示"等于没有：
一条 stderr 会淹没在几十行工具输出里，而我下一次看到 `git status` 干净就会以为
没事了 —— 这正是"绿但无感知比红更贵"。

阻断之后，我必须在提交前把话说清楚：是出处就 `git add`，是试算就移进
`data/.scratch/`。没有第三条路，也没有"下次再说"。

绕过的方式当然存在（`git commit --no-verify`），但它需要一个**明确的、要写出来的
决定**，而不是一次顺手忽略 —— 这就是这里想要的区别。

## 用法

    python scripts/check_analysis_untracked.py            # 有残留即 exit 1（钩子用这个）
    python scripts/check_analysis_untracked.py --warn-only # 只提示、exit 0
    python scripts/check_analysis_untracked.py --quiet     # 只影响退出码，不打字

安装为 pre-commit 钩子：`python scripts/install_git_hooks.py`
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: 关注的后缀。`.py`（脚本）、`.md`（结论文档）、`.json`（样例/结果）都算 ——
#: 这三类在 `analysis/` 里都是"结论的出处"，留一个在工作区就是同一个待决问题。
#:
#: 不担心噪声：真正属于临时产物的文件本该被 `.gitignore` 挡掉，而
#: `--exclude-standard` 正好不报告它们。2026-11-24 起，`data/` 是产物默认去处，
#: `analysis/` 是出处，二者的边界由这条提示维持。
WATCHED_SUFFIXES = (".py", ".md", ".json")

#: 提示里给出的两条出路。写死在文案里，避免"提示了但没说怎么办"。
HINT_ADD = "git add <文件>          # 它是要入库的出处材料（结论已写进 docs/ 或记忆）"
HINT_SCRATCH = "mv <文件> data/.scratch/  # 一次性试算：该目录已被 gitignore，git 不会再问"


def repo_root() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(out.stdout.strip()) or None


def untracked_scripts(root: Path) -> list[str]:
    """`analysis/` 下未跟踪、未忽略的 `.py`（相对仓库根的路径，已排序）。

    用 `--others --exclude-standard` 而不是自己解析 `git status`：后者带颜色、
    引号转义与状态列，解析迟早出错。
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "analysis"],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    found = [
        line.strip()
        for line in out.stdout.splitlines()
        if line.strip().endswith(WATCHED_SUFFIXES)
    ]
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 analysis/ 下未入库的新文件")
    parser.add_argument(
        "--warn-only", action="store_true",
        help="有残留只提示、仍 exit 0（默认是 exit 1）",
    )
    parser.add_argument("--quiet", action="store_true", help="有残留也不打印，只影响退出码")
    args = parser.parse_args(argv)

    root = repo_root()
    if root is None:
        return 0  # 不在 git 仓库里（如打包后的运行环境）：静默放行

    pending = untracked_scripts(root)
    if not pending:
        return 0

    if not args.quiet:
        # 走 stderr：pre-commit 的输出在 stdout 会被某些工具当作结果吞掉
        print(
            f"\n提示：analysis/ 下有 {len(pending)} 个未入库的文件 —— "
            "它们是出处材料还是一次性试算？",
            file=sys.stderr,
        )
        for path in pending:
            print(f"  ?? {path}", file=sys.stderr)
        print(f"\n  是 → {HINT_ADD}", file=sys.stderr)
        print(f"  否 → {HINT_SCRATCH}", file=sys.stderr)
        print(
            "\n提交被拦下：先决定它们是出处还是试算。"
            "确属误报可 `git commit --no-verify`，但请顺手修掉根因。\n",
            file=sys.stderr,
        )

    if args.warn_only:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
