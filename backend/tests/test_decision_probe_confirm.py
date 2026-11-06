"""v13c（预注册 §12.12）：确认段判定入口 `scripts/probe_confirm.py`。

§12.12 预注册的验收表是「实现轮执行、全部必须绿」的可证伪清单；本文件把
其中能机械固化的部分钉成长期回归（提前拒绝、二次拒绝、封签漂移拒绝、
门钉住、刀锋左沿、样本规则、只读不写、入口不自推门）。两条验收不在
pytest 里：

* 「探索段逐位不变」——三条脚本输出与 v13b 封签下基线 `diff`，属实现轮
  一次性复核（基线在 `.workbuddy/tmp/v13b_baseline/`，随会话留存）；
* 「封签上链」——历史第 11 行 v13c 的落款由 `decision_gate.py seal` 写出，
  由 `test_live_seal_verifies` / `test_prereg_records_the_seal_hash` 兜底。

测试注入方式：`probe_confirm.run()` 的关键字参数（tmp 日志、tmp SERIES）
+ monkeypatch 它自己那一份 `GATE` 实例的 `audit` / `sessions`——
`probe_confirm` 经 `_load` 持有独立的 decision_gate 模块对象，补丁必须
打在 `pc.GATE` 上，打在别处（包括本文件再 `_load` 一份）都不生效。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pc = _load("probe_confirm", "scripts/probe_confirm.py")

CONFIRM_START = pc.GATE.CONFIRM_START
CONFIRM_DAYS = pc.GATE.CONFIRM_DAYS
LOOKBACK = pc.GATE.LOOKBACK
HORIZON = pc.GATE.HORIZON


# ---------- 造数工具 ----------

def _trading_days(anchor: date, n: int, step: int) -> list[str]:
    """从 anchor 起（step=+1）或止于 anchor 前（step=-1）的 n 个工作日。"""
    out, d = [], anchor
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=step)
    return out


def _synthetic_calendar() -> list[str]:
    """20 个决策前工作日 + 2026-10-09 起的 250 个确认决策日 + 20 个 t+20 价日。

    `decision_days(cal) == cal[LOOKBACK:len-HORIZON]` 恰好是那 250 天，
    首日 2026-10-09——与生产日历的唯一差别是没有节假日（造历不影响语义）。
    """
    anchor = date.fromisoformat(CONFIRM_START)
    back = _trading_days(anchor - timedelta(days=1), LOOKBACK, -1)[::-1]
    fwd = _trading_days(anchor, CONFIRM_DAYS + HORIZON, +1)
    return [d.isoformat() for d in back + fwd]


def _win(cal: list[str]) -> list[str]:
    return [d for d in pc.GATE.decision_days(cal) if d >= CONFIRM_START][:CONFIRM_DAYS]


def _synthetic_series(win: list[str], missing=(), exploration=("2025-06-05",
        "2026-10-08")) -> dict:
    """四条格内键 + 若干探索段键（混入键应被剔除）。值为常数 50bp：判 ECON+/PASS。"""
    ser: dict[str, dict[str, float]] = {}
    for key in pc.CONFIRM_KEYS:
        per = {d: 100.0 - i for i, d in enumerate(exploration)}
        per.update({d: 50.0 for d in win if d not in set(missing)})
        ser[key] = per
    ser["momentum_20|all"] = dict(ser[pc.CONFIRM_KEYS[0]])  # 干扰键
    return ser


def _fake_audit(monkeypatch, *, clean: bool = True, computable: int = CONFIRM_DAYS):
    def fake_audit(data_dir=None, paths=None):
        return {
            "clean": clean,
            "violations": [] if clean else [(".poisoned.json", CONFIRM_START)],
            "confirmation_clock": {
                "decision_days_computable": computable,
                "first": CONFIRM_START, "last": "2099-01-01",
                "required": CONFIRM_DAYS,
                "remaining": max(0, CONFIRM_DAYS - computable),
            },
        }
    monkeypatch.setattr(pc.GATE, "audit", fake_audit)


def _inject_calendar(monkeypatch) -> list[str]:
    """补上合成日历，返回 250 个确认决策日（首日 2026-10-09）。"""
    cal = _synthetic_calendar()
    monkeypatch.setattr(pc.GATE, "sessions", lambda data_dir=None: cal)
    return _win(cal)


def _capturing_echo():
    lines: list[str] = []
    return lines.append, lines


# ---------- 验收 1：提前拒绝（真实盘面，不打任何补丁） ----------

def test_real_run_rejects_while_the_clock_is_partial() -> None:
    """当前真实状态直接跑入口：时钟未满即非零退出，消息含剩余天数。

    期望值从 `audit()` 现算，不写死 `250`——时钟每个交易日 +1，写死的常量
    会在 2026-11-05 之后变成一条假红（实测已从 0/250 走到 1/250）。
    """
    if not pc.GATE.sessions():
        pytest.skip("本机没有行情分区")
    if pc.GATE.READ_LOG.exists():
        pytest.skip("真实读取已发生，先验条件解除")
    clk = pc.GATE.audit()["confirmation_clock"]
    if clk["decision_days_computable"] >= clk["required"]:
        pytest.skip("确认时钟已满，本验收不再适用")
    with pytest.raises(SystemExit) as exc:
        pc.run()
    assert (f"确认时钟 {clk['decision_days_computable']} / {clk['required']}"
            in str(exc.value))
    assert f"还差 {clk['remaining']} 个确认决策日" in str(exc.value)
    assert not pc.GATE.READ_LOG.exists(), "拒绝时不许留记账行"


# ---------- 验收 2：二次拒绝 ----------

def test_second_run_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """「只读一次」的机械形式：read-log 里已有 probe_confirm 行即拒绝。"""
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    series = tmp_path / "series.json"
    series.write_text(json.dumps(_synthetic_series(win)), encoding="utf-8")
    log = tmp_path / "read-log.jsonl"

    assert pc.run(series_path=series, read_log_path=log) == 0
    assert log.exists()

    with pytest.raises(SystemExit) as exc:
        pc.run(series_path=series, read_log_path=log)
    assert "只读一次" in str(exc.value) and "probe_confirm" in str(exc.value)


# ---------- 验收 3：封签漂移拒绝 ----------

def test_seal_drift_rejects_a_reswapped_series(tmp_path: Path, monkeypatch) -> None:
    """把 SERIES 换成另一口径（median 重物化的模拟）合成序列：前置检查 e 拒绝。

    做法：把当前封签钉住的每个文件镜像进 tmp 根，只把 SERIES 换成字节不同
    的合成序列，再把 `pc.GATE.ROOT` 指到 tmp——`verify_seal` 逐文件比对时
    唯一失配的就是 SERIES。真实仓库文件全程只读。
    """
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    log = tmp_path / "read-log.jsonl"

    saved = json.loads(pc.GATE.SEAL.read_text(encoding="utf-8"))
    series_rel = next(rel for rel in saved["files"]
                      if rel.endswith(pc.GATE.NEUTRAL_CACHE.name))
    for rel in saved["files"]:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, dst)

    series = tmp_path / series_rel
    series.write_text(json.dumps(_synthetic_series(win)) + "\n", encoding="utf-8")
    assert pc.GATE.sha256_file(series) != saved["files"][series_rel]

    monkeypatch.setattr(pc.GATE, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as exc:
        pc.run(series_path=series, read_log_path=log)
    assert "verify-seal" in str(exc.value)
    assert not log.exists(), "拒绝时不许留记账行"


# ---------- 验收 4：门钉住 ----------

def test_entry_passes_confirm_gate_to_classify(tmp_path: Path, monkeypatch) -> None:
    """入口传给 classify 的门必须恰为 NET.CONFIRM_GATE（=176.0），且不得自推。"""
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    series = tmp_path / "series.json"
    series.write_text(json.dumps(_synthetic_series(win)), encoding="utf-8")
    log = tmp_path / "read-log.jsonl"

    seen: list[float] = []
    real_classify = pc.NET.classify

    def spy(net_lo, net_hi, mde, gate=pc.NET.MDE_GATE):
        seen.append(gate)
        return real_classify(net_lo, net_hi, mde, gate=gate)

    monkeypatch.setattr(pc.NET, "classify", spy)
    assert pc.run(series_path=series, read_log_path=log) == 0
    assert len(seen) == len(pc.CONFIRM_KEYS)
    assert all(g == 176.0 for g in seen)
    assert all(g is pc.NET.CONFIRM_GATE for g in seen), "必须引用单点定义本身"

    src = (REPO / "scripts" / "probe_confirm.py").read_text(encoding="utf-8")
    assert src.count("CONFIRM_CALIB") == 0, "入口内不得重新推导门值"
    assert src.count("gate=NET.CONFIRM_GATE") == 1, "生产调用点全仓唯一"


# ---------- 验收 5：刀锋左沿（经入口路径复用 v13b 断言） ----------

def test_blade_left_edge_with_the_entry_gate(tmp_path: Path, monkeypatch) -> None:
    """入口路径捕获到的门，就是 v13b 钉过的那把刀：176.0 判 FAIL，+ε 判 UNDERPOWERED。"""
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    series = tmp_path / "series.json"
    series.write_text(json.dumps(_synthetic_series(win)), encoding="utf-8")

    seen: list[float] = []
    real_classify = pc.NET.classify

    def spy(net_lo, net_hi, mde, gate=pc.NET.MDE_GATE):
        seen.append(gate)
        return real_classify(net_lo, net_hi, mde, gate=gate)

    monkeypatch.setattr(pc.NET, "classify", spy)
    pc.run(series_path=series, read_log_path=tmp_path / "read-log.jsonl")
    gate = seen[0]
    cross = (-10.0, 10.0)
    assert pc.NET.classify(*cross, mde=176.0, gate=gate) == "FAIL"
    assert pc.NET.classify(*cross, mde=176.0 + 1e-9, gate=gate) == "UNDERPOWERED"
    assert pc.NET.classify(*cross, mde=176.0, gate=pc.NET.MDE_GATE) == "FAIL"


# ---------- 验收 6：样本规则 ----------

def test_sample_missing_twelve_proceeds_and_thirteen_defers(
        tmp_path: Path, monkeypatch) -> None:
    """缺失 ≤12 天直接判定并打印缺失清单与 n_used；>12 天按 §5 顺延拒绝。"""
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    log = tmp_path / "read-log.jsonl"

    missing12 = win[100:112]
    echo, lines = _capturing_echo()
    series = tmp_path / "series12.json"
    series.write_text(json.dumps(_synthetic_series(win, missing=missing12)),
                      encoding="utf-8")
    assert pc.run(series_path=series, read_log_path=log, echo=echo) == 0
    text = "\n".join(lines)
    assert "n_used=238" in text
    assert missing12[0] in text and missing12[-1] in text, "缺失清单要打印"

    missing13 = win[100:113]
    echo2, lines2 = _capturing_echo()
    series2 = tmp_path / "series13.json"
    series2.write_text(json.dumps(_synthetic_series(win, missing=missing13)),
                       encoding="utf-8")
    log2 = tmp_path / "read-log2.jsonl"  # 独立日志：否则先撞「只读一次」检查 c
    with pytest.raises(SystemExit) as exc:
        pc.run(series_path=series2, read_log_path=log2, echo=echo2)
    assert "缺失 13 天" in str(exc.value) and "顺延" in str(exc.value)
    assert not log2.exists(), "顺延拒绝时不许留记账行"


def test_exploration_keys_are_excluded_first_sample_day_is_confirm_start(
        tmp_path: Path, monkeypatch) -> None:
    """混入 2026-10-08 及更早的键被剔除；样本首日 == 2026-10-09。"""
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    echo, lines = _capturing_echo()
    series = tmp_path / "series.json"
    series.write_text(json.dumps(_synthetic_series(win)), encoding="utf-8")
    assert pc.run(series_path=series, read_log_path=tmp_path / "log.jsonl",
                  echo=echo) == 0
    text = "\n".join(lines)
    assert "2025-06-05" not in text, "探索段键不得进入确认样本"
    assert "2026-10-08" not in text
    assert f"确认窗 {CONFIRM_START} …" in text
    assert f"n_used={CONFIRM_DAYS}" in text


# ---------- 验收 7：只读不写 ----------

def test_entry_writes_only_the_read_log(tmp_path: Path, monkeypatch) -> None:
    """入口运行前后：6 产物 + SERIES 哈希不变，唯一变化是 read-log +1 行。"""
    _fake_audit(monkeypatch)
    win = _inject_calendar(monkeypatch)
    if not pc.GATE.NEUTRAL_CACHE.exists():
        pytest.skip("本机没有 SERIES 缓存")
    artifacts = list(pc.GATE.ARTIFACTS) + [pc.GATE.NEUTRAL_CACHE]
    before = {p: pc.GATE.sha256_file(p) for p in artifacts if p.exists()}

    series = tmp_path / "series.json"
    series.write_text(json.dumps(_synthetic_series(win)), encoding="utf-8")
    log = tmp_path / "read-log.jsonl"
    assert pc.run(argv=["probe_confirm"], series_path=series,
                  read_log_path=log) == 0

    after = {p: pc.GATE.sha256_file(p) for p in artifacts if p.exists()}
    assert before == after, "入口不许写任何产物或缓存"

    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["script"] == "probe_confirm"
    assert row["first_decision_day"] == CONFIRM_START
    assert row["last_decision_day"] == win[-1]
    assert row["artifacts"] == {series.name: pc.GATE.sha256_file(series)}
    ok, msg = pc.GATE.verify_log(log)
    assert ok, msg


# ---------- 前置检查 a / d 的独立拒绝路径 ----------

def test_precheck_a_rejects_violations(tmp_path: Path, monkeypatch) -> None:
    _fake_audit(monkeypatch, clean=False)
    with pytest.raises(SystemExit) as exc:
        pc.run(series_path=tmp_path / "nope.json",
               read_log_path=tmp_path / "log.jsonl")
    assert "前置检查 a" in str(exc.value)


def test_precheck_d_rejects_missing_cell_keys(tmp_path: Path, monkeypatch) -> None:
    _fake_audit(monkeypatch)
    _inject_calendar(monkeypatch)
    series = tmp_path / "series.json"
    ser = _synthetic_series(_synthetic_calendar())
    del ser["low_turnover|cell"]
    series.write_text(json.dumps(ser), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        pc.run(series_path=series, read_log_path=tmp_path / "log.jsonl")
    assert "前置检查 d" in str(exc.value) and "low_turnover|cell" in str(exc.value)


# ---------- 结构钉子 ----------

def test_preregistered_keys_and_constants_are_unchanged() -> None:
    """§12.12 修订版钉死的键名与常数：改任何一个都等于改预注册。"""
    assert pc.CONFIRM_KEYS == (
        "momentum_20|cell", "reversal_20|cell",
        "low_turnover|cell", "high_turnover|cell")
    assert pc.DECISION_ROW == "low_turnover|cell"
    assert pc.MAX_MISSING == 12


def test_entry_writes_no_cache_by_construction() -> None:
    """入口源码里不许出现任何写盘调用（json.dump / open("w") / write_text）。"""
    src = inspect.getsource(pc)
    assert "json.dump" not in src
    assert 'open(' not in src.replace('open("rb")', "")
    assert ".write_text(" not in src
    assert ".write(" not in src.replace("f.write", "")
    assert "log_read" in src, "唯一的写盘动作是 log_read 记账行"
