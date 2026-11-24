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

只看 `analysis/` 下**未被跟踪且未被忽略**的 `.py`：

- **已入库的**不动（棘轮之外的历史，不做批量治理）；
- **被 `.gitignore` 忽略的**不管 —— 那是作者明确表示"这是临时文件"；
- 命中的一律提示，并给出两条可执行的路：`git add` 或移进 `data/.scratch/`。

## 默认只提示、不阻断

"该不该交"是判断题，钩子没有资格替人拍板。所以默认 exit 0（打印到 stderr，
不污染 stdout），需要强制时用 `--strict` 让它 exit 1。

## 用法

    python scripts/check_analysis_untracked.py            # 提示（钩子用这个）
    python scripts/check_analysis_untracked.py --strict   # 有残留就失败
    python scripts/check_analysis_untracked.py --quiet    # 安静模式（有残留仍 exit 0）

安装为 pre-commit 钩子：`python scripts/install_git_hooks.py`
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: 关注的后缀。只收脚本：分析产物（`*_result.json`）与文档的归属另有惯例，
#: 混在一起提示会让人直接关掉这条提醒。
WATCHED_SUFFIX = ".py"

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
        if line.strip().endswith(WATCHED_SUFFIX)
    ]
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提示 analysis/ 下未入库的新脚本")
    parser.add_argument("--strict", action="store_true", help="有残留时 exit 1")
    parser.add_argument("--quiet", action="store_true", help="有残留也不打印（仍影响 --strict）")
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
            f"\n提示：analysis/ 下有 {len(pending)} 个未入库的脚本 —— "
            "它们是出处材料还是一次性试算？",
            file=sys.stderr,
        )
        for path in pending:
            print(f"  ?? {path}", file=sys.stderr)
        print(f"\n  是 → {HINT_ADD}", file=sys.stderr)
        print(f"  否 → {HINT_SCRATCH}", file=sys.stderr)
        print(
            "\n（本检查只在提交时提示，不阻断；要强制失败用 --strict）\n",
            file=sys.stderr,
        )

    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
