"""补丁 5（实探接班）的变异复核：每个变异必须被**对应**的测试杀死。

## 规矩（前两轮的代价换来的，不是格式要求）

* **每轮先从母本还原再改**，否则上一轮的变异会污染下一轮（M4–M6 曾三行同输）；
* 改完先断言「目标片段恰好出现一次且不在三引号内」—— 插到 docstring 里等于什么都
  没改，而它照样会红，因为红的因是错的（M5）；
* **字节读写**。Windows 文本模式会把 LF 改成 CRLF，于是「还原失败」与「变异生效」
  在断言上同形 —— 那正是上一轮 docstring 假证据的同一族。
* 一个变异只要有**任意**测试变红就算被杀死，但必须打印出是哪一个：只报「KILLED」
  不报凶手，下一轮就不知道该保住哪条测试。

## 这一批覆盖什么

补丁 5 新增的全部分支：常量覆盖外的早退、`usable` 的合取、四种拒绝病因、
产物自证 `axis`、以及 `_carries` 不许自己再算一遍那个合取。
M8–M13 是上一轮那批里目标文本未被本轮改动波及的部分，一并复跑做回归。

用法（只在人工复核时跑，会临时改写下列源文件，结束后按字节还原并校验哈希）：

    backend/.venv/Scripts/python.exe analysis/_diag_mutate_p0_live.py
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALARM = ROOT / "scripts" / "panel_stall_alarm.py"
CAL = ROOT / "analysis" / "trading_calendar.py"
AO = ROOT / "analysis" / "auction_order.py"
PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
TESTS = ("tests/test_auction_order_plan.py", "tests/test_panel_stall_alarm.py")

MUTATIONS = [
    # ---- 补丁 5 新增的分支 ------------------------------------------------
    ("M14 _live_calendar 去掉「常量覆盖内不取实探」的早退", AO,
     "    if today <= hi:\n        return None, {\"used\": False,",
     "    if False:\n        return None, {\"used\": False,"),
    ("M15 闸门不看 usable，直接把原始实探当可用", AO,
     "    ok, judged = cal.usable(raw, today)",
     "    ok, judged = raw, {\"health\": cal.live_health(raw, today),"
     " \"reconcile\": cal.reconcile(raw)}"),
    # 注：这条**必须改分支条件**，不能只改消息的第一行 —— 我第一次写成了替换首句，
    # 续行里还留着「补常量在这里没用」，语义没变 ⇒ 测试当然不响。假变异比没变异更贵。
    ("M16 实探不可用（n=0）掉进体检分支，给「补常量」这个错建议", AO,
     '    if not live_info.get("n"):',
     '    if False:'),
    ("M17 gap_days 少传实探集合（回到只看常量）", AO,
     "    cal_gap = CAL.gap_days(prev, today, live)",
     "    cal_gap = CAL.gap_days(prev, today)"),
    ("M18 产物的 expected_prev 少传实探集合", AO,
     "                  \"calendar\": {\"expected_prev\": CAL.prev_market_day(today, live).isoformat(),",
     "                  \"calendar\": {\"expected_prev\": CAL.prev_market_day(today).isoformat(),"),
    ("M19 产物不写 axis（读者分不出谁算的）", AO,
     "                               \"axis\": \"live\" if live else \"constant\"},",
     "                               \"axis\": \"constant\"},"),
    ("M20 _carries 只看「有没有数据」，不读 usable", ALARM,
     "    lv = rep.get(\"live\") or {}\n    return bool(lv.get(\"usable\"))",
     "    lv = rep.get(\"live\") or {}\n    return bool(lv.get(\"n\"))"),
    ("M21 usable 只看体检，不查重叠区分歧", CAL,
     "    ok = bool(live) and h[\"ok\"] and not rc[\"disagree\"]",
     "    ok = bool(live) and h[\"ok\"]"),
    # ---- 上一轮那批里目标文本未变的部分（回归）-----------------------------
    ("M8  停摆不再优先（STALLED 返回 0）", ALARM,
     "    if rep[\"status\"] == STALLED:\n        return 1",
     "    if rep[\"status\"] == STALLED:\n        return 0"),
    ("M9  is_market_day 改成实探优先", CAL,
     "    lo, hi = COVERED\n    if lo <= d <= hi:\n        return d.weekday() < 5 and d not in HOLIDAYS\n    if live and d in live:\n        return True",
     "    lo, hi = COVERED\n    if live and d in live:\n        return True\n    if lo <= d <= hi:\n        return d.weekday() < 5 and d not in HOLIDAYS"),
    ("M10 gap_days 忽略实探右端（回到只看常量）", CAL,
     "    s = live_span(live)\n    if s is not None:\n        lo, hi = min(lo, s[0]), max(hi, s[1])",
     "    s = live_span(live)\n    if False:\n        lo, hi = min(lo, s[0]), max(hi, s[1])"),
    ("M11 右端新鲜度 10 天放宽到 30 天", CAL,
     "LIVE_MAX_RIGHT_GAP_DAYS = 10",
     "LIVE_MAX_RIGHT_GAP_DAYS = 30"),
    ("M12 分歧只查一个方向（const - liv）", CAL,
     "    dis = sorted(const ^ liv)",
     "    dis = sorted(const - liv)"),
    ("M13 去掉「回放未来日期不查实探」的守卫", ALARM,
     "        if today > real_today:",
     "        if False:"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def in_docstring(text: str, idx: int) -> bool:
    """粗略但够用：数 idx 之前的三引号个数，奇数即在字符串内。"""
    return text.count('"""', 0, idx) % 2 == 1


def main() -> int:
    pristine = {p: p.read_bytes() for p in (ALARM, CAL, AO)}
    baseline = {p: sha(p) for p in pristine}
    killed: list[tuple[str, list[str]]] = []

    for name, path, old, new in MUTATIONS:
        for p, raw in pristine.items():
            p.write_bytes(raw)
        for p, h in baseline.items():
            assert sha(p) == h, f"{p} 未还原到母本"

        raw = pristine[path]
        crlf = b"\r\n" in raw
        body = raw.decode("utf-8").replace("\r\n", "\n")
        n = body.count(old)
        assert n == 1, f"{name}: 目标片段出现 {n} 次（必须恰好 1 次）"
        assert not in_docstring(body, body.index(old)), f"{name}: 变异落在 docstring 内 ⇒ 假证据，中止"
        mutated = body.replace(old, new, 1)
        assert mutated != body, f"{name}: 文本未改变"
        path.write_bytes((mutated.replace("\n", "\r\n") if crlf else mutated).encode("utf-8"))

        r = subprocess.run([str(PY), "-m", "pytest", *TESTS, "-q", "--no-header", "-p", "no:warnings"],
                           cwd=str(ROOT / "backend"), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        failed = sorted(set(re.findall(r"^FAILED\s+\S+::(\w+)", r.stdout, re.M)))
        print(f"{'KILLED' if failed else 'SURVIVED':8s} {name}")
        for f in failed:
            print(f"           └─ {f}")
        killed.append((name, failed))

        for p, raw in pristine.items():
            p.write_bytes(raw)
        for p, h in baseline.items():
            assert sha(p) == h, f"{name}: 还原失败"

    survived = [n for n, f in killed if not f]
    print(f"\n共 {len(MUTATIONS)} 个变异，存活 {len(survived)} 个")
    for s in survived:
        print("  存活:", s)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
