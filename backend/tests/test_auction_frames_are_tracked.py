"""`frames/` 下的每一份归档都必须在版本控制里 —— 证据不能只躺在盘上。

## 为什么需要这条

`data/**` 默认被忽略（批量缓存、可重拉），`frames/` 是唯一的例外：竞价数据
**不可回补**，parquet 是证据本体，`_result.json` 只是它的派生表。于是
`.gitignore` 里逐个文件写了 `!` 放行 —— 这是刻意的：**每多一天都要显式加一行，
让「多了一份证据」必须被看见**（不改成通配，通配会让新证据静默消失）。

刻意的代价是有一天会忘。忘了的形态很安静：09:26 的 burst 写出
`2026-11-13_burst.parquet`，`git status` 干净（被 `data/**` 吃掉），
汇总表照样能算出来，只有换一台机器才发现原始帧不在库里。
所以「记得加一行」必须变成一条断言，而不是一句注释。

## 射程与它的边界

- 目录不存在 ⇒ skip：本机没采过。删掉**已跟踪**的文件是另一个失败形态，
  `git status` 会显示 `D`，由版本控制自己报，不需要这里重复。
- 目录存在 ⇒ 每个 `*.parquet` 都必须 `git ls-files` 命中；未跟踪的（无论是否被忽略）
  都算红。

反例走**同一个谓词**：造一个临时 parquet，`_untracked()` 必须把它点出来；
删掉之后同一谓词必须返回空。全绿的守卫不是守卫。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRAMES = ROOT / "data" / "research" / "auction_indicative" / "frames"


def _untracked(files: list[Path]) -> list[str]:
    """哪些文件没有进版本控制。**判定只有这一个谓词**，反例也走它。"""
    out = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        p = subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                           cwd=ROOT, capture_output=True, text=True)
        if p.returncode != 0:
            out.append(f.name)
    return out


def test_every_archived_frame_file_is_tracked():
    if not FRAMES.is_dir():
        pytest.skip(f"{FRAMES} 不存在：本机没采过竞价帧，没有证据要守")
    files = sorted(FRAMES.glob("*.parquet"))
    assert files, (
        f"{FRAMES} 存在但一个 parquet 都没有 —— 排除「目录存在、证据已删」"
        "这种形态（真要清空就删目录，别留空壳）")
    missing = _untracked(files)
    assert not missing, (
        "这些归档不在版本控制里（被 `data/**` 吃掉、也没 `!` 放行）：\n  "
        + "\n  ".join(missing)
        + "\n竞价不可回补 —— 要么在 .gitignore 里加一行放行并 `git add`，要么删掉它")


def test_the_untracked_predicate_can_go_red():
    """反例：新一天的归档默认未跟踪，谓词必须点出它。不动真归档。"""
    probe = FRAMES / "_red_probe.parquet"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(b"PAR1")
    try:
        assert _untracked([probe]) == [probe.name], (
            "未跟踪的临时 parquet 没被点出来 —— 谓词失去了牙齿")
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", probe.relative_to(ROOT).as_posix()],
            cwd=ROOT, capture_output=True)
        assert ignored.returncode == 0, (
            "临时归档竟然没被 `data/**` 忽略 —— 那么「新证据静默消失」的前提不成立，"
            "这条 docstring 要重写")
    finally:
        probe.unlink()
