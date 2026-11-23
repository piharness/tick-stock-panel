"""投资手册每日刷新 —— 一条命令跑完「扫描 → 账户/现金腿 → 回放 → 体检」。

为什么要有这个入口：
  页面的四块活数据由四个脚本产出。只要漏跑其中一个，页面就会「半新半旧」：
  2026-11-23 19:19 的现场是扫描/账户/现金腿都还停在 11-20，
  而「工具 · 交易日期」的选项来自 replay.json —— 于是控件里根本没有今天。
  四个脚本各自的退出码没人汇总，也没有任何一步会回答「现在该到哪一天」。

  这里把它变成一个幂等动作：
    - 先体检：四个产物都覆盖到最近交易日 ⇒ 直接跳过（可安全地反复调用）；
    - 否则按依赖顺序补跑，任何一步失败就停（不带着半套产物去覆盖快照）；
    - 跑完再体检，退出码即结论（0 最新 / 1 仍落后 / 2 产物缺失）。

用法：
  python analysis/handbook_refresh.py              # 幂等：已最新就什么都不做
  python analysis/handbook_refresh.py --force      # 无条件重跑整条链
  python analysis/handbook_refresh.py --dry-run    # 只报告会跑哪几步

建议调度：交易日 16:10 之后每 30 分钟跑一次，收盘后第一次即生效，之后自动跳过。
（WorkBuddy 的自动化只在桌面端运行时触发；容器 24h 在跑，但 analysis/ 不在镜像里，
  所以这条命令需要在宿主机上调度。）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

from handbook_audit import audit, report  # noqa: E402

STEPS: list[tuple[str, list[str]]] = [
    ("扫描", ["analysis/etf_watch.py"]),
    ("账户 + 现金腿", ["analysis/etf_watch_account.py"]),
    ("单日回放", ["analysis/etf_watch_replay.py", "--lookback", "120"]),
]


def _current() -> tuple[bool, list[str]]:
    rows, facts = audit()
    if not facts:
        return False, ["体检没有拿到扫描快照"]
    bad = [f"{r.name}: {r.detail}" for r in rows if r.status != "OK"]
    return not bad, bad


def main() -> int:
    ap = argparse.ArgumentParser(description="投资手册每日刷新（幂等）")
    ap.add_argument("--force", action="store_true", help="无条件重跑整条链")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不执行")
    args = ap.parse_args()

    ok, bad = _current()
    if ok and not args.force:
        print("已覆盖最近交易日，无需刷新（--force 可强制重跑）。")
        return 0

    if bad:
        print("需要刷新：")
        for line in bad:
            print(f"  - {line}")
    elif args.force:
        print("--force：无条件重跑整条链。")

    for name, argv in STEPS:
        cmd = [sys.executable, *argv]
        print(f"\n=== {name}：{' '.join(argv)} ===", flush=True)
        if args.dry_run:
            continue
        # 不捕获输出：盘后日志要能直接看到扫描判定与写入路径。
        proc = subprocess.run(cmd, cwd=ROOT, check=False)
        if proc.returncode != 0:
            print(f"\n[stop] {name} 失败（exit {proc.returncode}），后续步骤不执行。", flush=True)
            return proc.returncode or 1

    if args.dry_run:
        return 0

    print("\n=== 体检 ===")
    return report(*audit())


if __name__ == "__main__":
    sys.exit(main())
