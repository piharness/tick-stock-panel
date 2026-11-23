"""守卫：5min 面板只能从 `minute_union` 读，不许直读落地目录。

## 为什么需要一条**可执行**的规则

2026-11-23 修掉的一个静默缺陷：`minute_union.load` 按字面文件名找
`f5_merged/{symbol}.parquet`，换码股（300114 → 302132）用当前码请求时返回空，
2020-2024 的 55,824 行整段消失且不报错。同类问题还有两套物理布局
（`kline_5min/date=*/` 与 `f5_merged/{symbol}.parquet`）——同时 glob 两者会
要么丢一半、要么把重叠日读两遍。

结论：**路径知识只能有一份**，放在 `minute_union.py`。研究脚本直读落地目录
等于复制第二份（还不带 canonicalize 与按票拼接），而它不会报错，只会少一截数据。
所以这里把「不许直读」做成脚本，而不是写在文档里靠自觉。

## 判据

同一条代码行里同时出现「落地路径」与「读操作」才算违规；注释/docstring 里
提到路径不算（本文件自己就是反例，它必须写出这些字面量）。

    python analysis/check_minute_entrypoints.py     # 0 = 干净，1 = 有直读

白名单是**角色**而不是文件清单：生产者（往目录里写）与唯一读取入口本身，
天然需要知道路径；其余一律走 `minute_union.load`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "analysis", ROOT / "scripts"]

#: 角色白名单：这些文件允许出现「路径 + 读」（写生产者 / 唯一读入口 / 只读审计）
ROLE_ALLOW = {
    "minute_union.py",          # 唯一读取入口
    "merge_bs_minute.py",       # 生产者：f5 → f5_merged
    "backfill_bs_minute.py",    # 生产者：baostock → f5
    "_fill_5min_tail.py",       # 生产者：tdx → kline_5min
    "minute_backfill_progress.py",  # 只读看板
    "fetch_5min_sina.py",       # 另一套独立落地（etf/stock_5min_sina），不碰面板表
}

PATH_RE = re.compile(r"f5_merged|kline_5min|backfill_bs_minute[\"']?\s*/\s*[\"']f5")
READ_RE = re.compile(r"read_parquet|scan_parquet|\.glob\(|iterdir\(")


def _iter_files():
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if p.name.startswith("_"):
                continue  # 下划线前缀 = 运维/诊断脚本，允许直读
            yield p


def main() -> int:
    bad: list[tuple[str, int, str]] = []
    for p in _iter_files():
        if p.name in ROLE_ALLOW:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if PATH_RE.search(code) and READ_RE.search(code):
                bad.append((p.name, i, line.strip()[:110]))
    if not bad:
        print(f"OK：{sum(1 for _ in _iter_files())} 个分析/脚本文件，无直读 5min 落地目录")
        return 0
    print("!! 直读 5min 落地目录（应改为 minute_union.load）：")
    for name, i, text in bad:
        print(f"  {name}:{i}  {text}")
    print(f"\n共 {len(bad)} 处。路径知识只能有一份（minute_union.py）；直读不会报错，只会少一截数据。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
