"""常量到期后的那条分支，用**今天这份已知答案的数据**真跑一遍。

## 这一份回答什么

`analysis/auction_order.py` 的补丁 5 只在 `today > COVERED[1]` 时才取实探。于是那条
分支在 2026 年内不会被执行到 —— 一个一年后才第一次运行的 `except` 分支，和没有它
是同一件事。单测用注入集合覆盖了逻辑，但注入集合本身就是我写的，它证明不了
「真端点 + 真分区 + 真名单」这条路能走通。

办法不是等到 2027，而是把**证据边界**往前挪一天：拿真日历模块、只把 `COVERED`
的右端改成 `today - 1`，让 `2026-11-12` 这个**答案已知**的日子走 2027 的代码路径，
再与常量口径逐字段对比。两个口径必须给出同一份名单、同一个前一格、同一份对照收盘。

## 三个读数，缺一不可

1. **端点形状**：`probe()` 真取一次，报 `(n, left, right)`。这是「无入参、滚动一年、
   右端=今天」这个说法在今天还成不成立 —— 它不成立的话，下面两段都在验证一个
   已经不存在的端点。
2. **两个口径的名单一致**（Level B）：`resolve_pool` 各跑一次，比 `symbols` /
   `as_of` / `entry` / `refs`。差异即「接轴改变了交易决策」，比任何断言都硬。
3. **负控：上游停更时不许放行**。用真的 `2027-01-04` 去问真的端点（它的窗口右端
   是今天 2026-11-12，落后 53 天）⇒ 必须 `exit 4` 且病因是「停更」。
   这既是负控，也是 2027-01-04 那天**如果上游死了**会看到的东西。

只读：不写 `data/`，不改进程内的任何持久状态。产物是本目录的 result.json。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
OUT = HERE / "_diag_calendar_live_rehearsal_result.json"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _truncated(hi: date):
    """真日历模块、只把 `COVERED` 右端改成 `hi`（同一份实现，不是替身）。"""
    cal = _load("trading_calendar_rehearsal", "analysis/trading_calendar.py")
    cal.COVERED = (cal.COVERED[0], hi)
    return cal


def _run_gate(ao, today: date, data_dir: Path | None = None):
    """跑一次 `resolve_pool`，把 `SystemExit` 也当读数（退出码本身就是判据）。"""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        try:
            syms, meta, refs = ao.resolve_pool(today, "today", data_dir)
        except SystemExit as exc:
            return {"exit": int(exc.code or 0), "stderr": buf.getvalue().strip()}
    return {"exit": 0, "stderr": buf.getvalue().strip(), "symbols": syms,
            "as_of": meta["as_of"], "entry": meta["entry"],
            "calendar": meta["calendar"],
            "refs": {k: round(v["prev_close"], 4) for k, v in refs.items()}}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    #: 可选的 data_dir：2026-11-12 实测 `candidates()` 被 11-05 的重复键整体拒绝
    #: （与日历无关，见 `_diag_panel_dup_key.py`）。给一个**修复过的副本**就能把
    #: Level B 跑通，而 `data/` 一个字节不动。
    data_dir = Path(argv[0]) if argv else None
    prob = _load("calendar_probe", "analysis/calendar_probe.py")
    cal = _load("trading_calendar", "analysis/trading_calendar.py")
    ao = _load("auction_order", "analysis/auction_order.py")

    raw, note = prob.probe()
    shape = {"note": note, "n": len(raw) if raw else 0}
    if raw:
        lo, hi = cal.live_span(raw)
        shape.update({"left": lo.isoformat(), "right": hi.isoformat(),
                      "right_gap": (date.today() - hi).days,
                      "health": cal.live_health(raw, date.today()),
                      # 与常量在重叠区逐日对账：这是「真端点说的是真的」的唯一外部证据
                      "reconcile": cal.reconcile(raw)})
    print(f"[形状] {note}")
    if raw:
        print(f"       窗口 {shape['left']} … {shape['right']}（{shape['n']} 天，"
              f"右端落后今天 {shape['right_gap']} 天）")
        rc = shape["reconcile"]
        print(f"       与常量重叠 {rc['overlap']}：分歧 {len(rc['disagree'])} 天")
    if shape["n"] == 0:
        print("端点不可用：后面两段无从演练，直接以 exit 2 结束（这是配置/网络问题，不是判据问题）")
        OUT.write_text(json.dumps({"shape": shape}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        return 2

    # --- Level B：同一份数据，两个证据边界 -------------------------------
    today = date(2026, 11, 12)          # 答案已知：前格 11-11，名单 20 只（11-11 cohort）
    base = _run_gate(ao, today, data_dir)     # 常量口径（正常路径）
    cut = _truncated(today - timedelta(days=1))
    ao.CAL = cut                        # 只换证据边界，算子一个字节没动
    live = _run_gate(ao, today, data_dir)     # 实探口径（2027 的路径）
    ao.CAL = cal                        # 复原，负控段要用真常量

    print(f"\n[Level B] today={today}  常量口径 exit={base['exit']}  "
          f"实探口径 exit={live['exit']}")
    same = {
        "symbols": base.get("symbols") == live.get("symbols"),
        "as_of": base.get("as_of") == live.get("as_of"),
        "entry": base.get("entry") == live.get("entry"),
        "refs": base.get("refs") == live.get("refs"),
    }
    if live["exit"] == 0:
        print(f"          名单 {len(live['symbols'])} 只，前格 {live['as_of']}，"
              f"axis={live['calendar']['axis']}")
    else:
        print(f"          实探口径失败：{live['stderr'][:200]}")
    for k, v in same.items():
        print(f"          {k:8s} 两口径一致={'是' if v else '否'}")

    # --- 负控：真的 2027-01-04 × 真的端点（它没有未来表）------------------
    neg_day = date(2027, 1, 4)
    with contextlib.redirect_stderr(io.StringIO()) as buf2:
        try:
            ao.resolve_pool(neg_day)
            neg = {"exit": 0}
        except SystemExit as exc:
            neg = {"exit": int(exc.code or 0), "stderr": buf2.getvalue().strip()}
    print(f"\n[负控] today={neg_day}（真端点右端 {shape['right']}）⇒ exit {neg['exit']}"
          f"；病因句：{neg.get('stderr', '')[:120]}")
    neg_ok = neg["exit"] == 4 and "停更" in (neg.get("stderr") or "")

    ok = (live["exit"] == 0 and all(same.values()) and neg_ok)
    rep = {"shape": shape, "level_b": {"today": today.isoformat(), "constant": base,
                                       "live": live, "agree": same},
           "negative": {"today": neg_day.isoformat(), "expected": "exit 4 / 停更",
                        "got": neg, "ok": neg_ok},
           "verdict": "pass" if ok else "fail",
           "data_dir": str(data_dir) if data_dir else "data（默认）",
           "how": "把 COVERED 右端提前到 today-1，用已知答案的那一天跑 2027 分支；"
                  "再用真的 2027-01-04 问真端点做负控"}
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n判定 {'✓ pass' if ok else '✗ fail'} —— 产物 {OUT.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
