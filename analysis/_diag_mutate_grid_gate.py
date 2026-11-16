"""「落盘闸门 / 修标 / 审计口径」这一批的变异复核 —— 每条变异必须被**具名**测试杀死。

## 为什么有这个脚本

上一轮（2026-11-13 09:20）报告了「12 条变异全被杀」，但**没有脚本、没有产物**：
除作者之外没人能重跑，那 12 条和「我记得我改过」没有区别。同一天晚些时候的复核
又发现三处口径问题，其中一处（B/S 恒红）方向相反 —— 没有可重跑的变异，就没有
任何东西能证明改完之后原先那些判据还站着。

## 规矩（沿自 `_diag_mutate_p0_live.py`，都是被代价逼出来的）

* 每轮先从母本还原再改，否则上一轮的变异污染下一轮；
* 改完先断言「目标片段恰好出现一次且不在三引号内」—— 插进 docstring 等于没改；
* **字节读写**：Windows 文本模式会把 LF 改成 CRLF，「还原失败」与「变异生效」同形；
* 一个变异只要有**任意**测试变红就算被杀死，但必须打印出是哪一个：只报 KILLED
  不报凶手，下一轮就不知道该保住哪条测试。

## 产物

`analysis/_diag_mutate_grid_gate_result.json`：逐条 `id / name / file / killed_by`，
外加每个被改文件的 sha256（改前 == 改后，证明还原）。

用法（只在人工复核时跑；会临时改写下列源文件，结束后按字节还原并校验哈希）：

    backend/.venv/Scripts/python.exe analysis/_diag_mutate_grid_gate.py
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"
PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
AUDIT = APP / "services" / "minute_audit.py"
LABELS = APP / "minute_labels.py"
SYNC = APP / "services" / "kline_sync.py"
TDX = APP / "plugins" / "tdx" / "provider.py"
TDXQ = APP / "plugins" / "tdxquant" / "provider.py"

TESTS = (
    "tests/test_minute_audit_service.py",
    "tests/test_minute_labels.py",
    "tests/test_minute_grid_gate.py",
    "tests/test_tdxquant_provider.py",
    "tests/test_tdx_source.py",
)

MUTATIONS = [
    # ---- 窗口：日期与钟点必须同一个时钟 --------------------------------
    ("M01 今天永远不算一天（盘中把未收盘分区当成品）", AUDIT,
     "    if parts and parts[-1] == ref.date().isoformat() and ref.time() < CLOSE_SETTLE:",
     "    if False:"),
    ("M02 只看日期不看钟点（收盘后也不让今天进窗口）", AUDIT,
     "    if parts and parts[-1] == ref.date().isoformat() and ref.time() < CLOSE_SETTLE:",
     "    if parts and parts[-1] == ref.date().isoformat():"),
    ("M03 日期回到真实当下（双时钟）", AUDIT,
     "    if parts and parts[-1] == ref.date().isoformat() and ref.time() < CLOSE_SETTLE:",
     "    if parts and parts[-1] == cn_now().date().isoformat() and ref.time() < CLOSE_SETTLE:"),
    # ---- 退出码：只认 unexplained --------------------------------------
    ("M04 大宗口径也进 rc（BJ 恒红）", AUDIT,
     '    if totals.get("money_bad_unexplained") or totals.get("bridge_bad"):',
     '    if totals.get("money_bad") or totals.get("bridge_bad"):'),
    ("M05 B/S 不分层（回归「每天恒 4」）", AUDIT,
     '    if totals.get("block_1500_unexplained") or totals.get("daily_bad_unexplained"):',
     '    if totals.get("block_1500") or totals.get("daily_bad"):'),
    # ---- 大宗口径的三道门 ---------------------------------------------
    ("M06 B 轴不看交易所（SH/SZ 也当口径）", AUDIT,
     '        (_is_bj()\n         & (pl.col("avg_px") >= pl.col("close") * IMPLIED_LO)',
     '        ((pl.col("avg_px") >= pl.col("close") * IMPLIED_LO)'),
    ("M07 S 轴不看交易所", AUDIT,
     '        (_is_bj()\n         & (pl.col("p_avg") >= pl.col("low") * IMPLIED_LO)',
     '        ((pl.col("p_avg") >= pl.col("low") * IMPLIED_LO)'),
    ("M08 M 轴不看交易所", AUDIT,
     '        (_is_bj()\n         & (pl.col("e_all") != 0)',
     '        ((pl.col("e_all") != 0)'),
    ("M09 隐含价没有下限（十倍误差也能当大宗）", AUDIT,
     '         & (pl.col("implied_px") >= pl.col("low") * IMPLIED_LO)',
     '         & (pl.col("implied_px") >= -1e18)'),
    # ---- 桥：两条轴必须说同一件事 --------------------------------------
    ("M10 桥不判（M 与 B 各红各的）", AUDIT,
     '        pl.col("bridge_err") > BRIDGE_TOL * pl.max_horizontal(pl.col("resid_h"), pl.lit(1.0))',
     "        pl.lit(False)"),
    ("M11 桥的预测式反号（1 + px/close）", AUDIT,
     '            (pl.col("e_all") * (1 - pl.col("implied_px") / pl.col("d_close"))).alias("pred_resid"))',
     '            (pl.col("e_all") * (1 + pl.col("implied_px") / pl.col("d_close"))).alias("pred_resid"))'),
    # ---- 修标：四支判据 ------------------------------------------------
    ("M12 窄窗口也改标（没看见上午就断言 11:30 缺）", LABELS,
     "            elif not morning and saw_morning:",
     "            elif not morning:"),
    ("M13 只丢不改标（停牌合成格留洞）", LABELS,
     '                out.append({**r, "datetime": r["datetime"].replace(hour=11, minute=30)})',
     "                pass"),
    ("M14 有 11:30 就丢（不比 OHLCV）", LABELS,
     '            elif morning and all(\n                r.get(c) == morning[0].get(c) for c in _OHLCV\n            ):',
     "            elif morning:"),
    # ---- 两个生产者必须绑同一份 ----------------------------------------
    ("M15 tdx 不修标（第二个生产者漏掉）", TDX,
     "    return fix_morning_close_label(symbol, out)",
     "    return out"),
    ("M16 tdxquant 不修标", TDXQ,
     "    return fix_morning_close_label(symbol, out)",
     "    return out"),
    # ---- 落盘闸门 ------------------------------------------------------
    ("M17 判据恒空（幽灵行直接落盘）", SYNC,
     "    if off.is_empty() or on_grid.is_empty():\n        return day_df.head(0)",
     "    if True:\n        return day_df.head(0)"),
    ("M18 取消归属（盘上已有的也当本次输入删掉）", SYNC,
     '            from_batch = bad.join(keys, on=["symbol", "datetime"], how="semi")',
     "            from_batch = bad"),
    ("M19 收窄异常边界（磁盘满不再拒写）", SYNC,
     "                except OSError as e:",
     "                except FileNotFoundError as e:"),
    ("M20 隔离记录不写 provider（分不出是哪个源）", SYNC,
     '        "provider": provider,',
     '        "provider": "unknown",'),
    ("M21 隔离记录不写 origin（分不出是哪个入口）", SYNC,
     '        "origin": origin,',
     '        "origin": "unknown",'),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def in_docstring(text: str, idx: int) -> bool:
    """粗略但够用：数 idx 之前的三引号个数，奇数即在字符串内。"""
    return text.count('"""', 0, idx) % 2 == 1


def main() -> int:
    paths = sorted({p for _, p, _, _ in MUTATIONS})
    pristine = {p: p.read_bytes() for p in paths}
    baseline = {p: sha(p) for p in paths}
    rows: list[dict] = []

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

        r = subprocess.run([str(PY), "-m", "pytest", *TESTS, "-q", "--no-header",
                            "-p", "no:warnings"],
                           cwd=str(ROOT / "backend"), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        failed = sorted(set(re.findall(r"^FAILED\s+\S+::(\w+)", r.stdout, re.M)))
        print(f"{'KILLED' if failed else 'SURVIVED':8s} {name}")
        for f in failed:
            print(f"           └─ {f}")
        rows.append({"id": name.split()[0], "name": name, "file": str(path.relative_to(ROOT)),
                     "killed_by": failed, "status": "killed" if failed else "survived"})

        for p, raw in pristine.items():
            p.write_bytes(raw)
        for p, h in baseline.items():
            assert sha(p) == h, f"{name}: 还原失败"

    survived = [r for r in rows if r["status"] == "survived"]
    print(f"\n共 {len(MUTATIONS)} 个变异，存活 {len(survived)} 个")
    for s in survived:
        print("  存活:", s["name"])

    out = ROOT / "analysis" / "_diag_mutate_grid_gate_result.json"
    payload: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "targets": {str(p.relative_to(ROOT)): h for p, h in baseline.items()},
        "tests": list(TESTS),
        "mutations": rows,
        "summary": {
            "total": len(MUTATIONS),
            "killed": len(MUTATIONS) - len(survived),
            "survived": len(survived),
            "survived_text": "、".join(s["name"] for s in survived) or "无",
            "target_count": len(baseline),
        },
    }
    payload["reading_template"] = (
        "{killed}/{total} 个变异被杀死，存活 {survived}。"
        "每个变异都从母本还原后重放，改前改后哈希一致（{target_count} 个文件）。"
    )
    payload["reading_fields"] = {
        "killed": "summary.killed", "total": "summary.total",
        "survived": "summary.survived_text", "target_count": "summary.target_count",
    }
    try:
        sys.path.insert(0, str(ROOT / "analysis"))
        from _artifact_guard import assert_clean, render_reading  # noqa: PLC0415

        payload["reading"] = render_reading(payload)
        assert_clean(payload)
    except ImportError:
        payload["reading"] = "（未安装 _artifact_guard，散文未渲染）"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=> 产物 {out.relative_to(ROOT)}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
