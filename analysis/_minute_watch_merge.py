"""等候八个分片跑完, 自动接合并 —— 避免人工盯 75 分钟, 也避免中途崩溃后无人收尾。

判据: 每个分片日志末尾出现「shard N 完成」。八片齐了才合并;
      超时(默认 3 小时)或发现分片进程已死但日志无完成行, 也执行合并并记录缺失分片。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("minute_watch")

LOGS = Path("/app/data/logs")
SHARDS = 8
DEADLINE_S = 3 * 3600


def _finished(tag: str) -> bool:
    p = LOGS / f"minute_shard{tag}.log"
    if not p.exists():
        return False
    try:
        tail = p.read_text(encoding="utf-8", errors="ignore")[-2000:]
    except OSError:
        return False
    return "完成" in tail


def _alive() -> int:
    n = 0
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().decode("utf8", "ignore").replace("\x00", " ")
        except OSError:
            continue
        if "_minute_backfill.py" in cmd and "--shard" in cmd:
            n += 1
    return n


def main() -> int:
    t0 = time.time()
    while time.time() - t0 < DEADLINE_S:
        done = [i for i in range(SHARDS) if _finished(f"0{i}")]
        alive = _alive()
        log.info("分片完成 %d/%d, 存活进程 %d", len(done), SHARDS, alive)
        if len(done) == SHARDS:
            break
        if alive == 0 and len(done) < SHARDS:
            log.warning("分片进程已全部退出但仅 %d/%d 报完成, 直接合并已得数据", len(done), SHARDS)
            break
        time.sleep(30)
    else:
        log.warning("等待超时, 合并已得数据")

    log.info("开始合并")
    r = subprocess.run(
        ["/app/.venv/bin/python", "/tmp/_minute_backfill.py", "--merge"],
        capture_output=True,
        text=True,
    )
    log.info("合并退出码 %s", r.returncode)
    for line in (r.stdout or "").strip().splitlines()[-25:]:
        log.info("  %s", line)
    if r.returncode != 0:
        log.error("合并失败 stderr: %s", (r.stderr or "")[-1500:])
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
