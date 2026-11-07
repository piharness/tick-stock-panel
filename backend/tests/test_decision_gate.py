"""确认段闸门的契约：截止日、截断、记账、封签。

这组测试钉的不是某个读数，而是**预注册 §5 能不能被机械执行**：

1. 截断真的截在 2026-10-08，而且保留 t+20 需要的未来价——不是靠调用者自觉；
2. 含确认段决策日时拒绝运行；`allow=True` 时留下一行不可撤销的记录；
3. 日志被改写或删行能被查出来（哈希链 + 序号）；
4. 封签只覆盖代码与缓存、不覆盖文档（否则哈希循环），且不随调用时间变；
5. 预注册里确实写着封签哈希——否则封签无人认领。

另有一条 6 是**先验条件**：首次读取之前，产物里不许有确认段日期。它会在
第一次正式读取后自动 skip，因为那时这个条件已经被解除，而不是被违反了。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load("decision_gate", "scripts/decision_gate.py")


def _daily(start: str, n: int) -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _trading_days_ending(end: str, n: int) -> list[str]:
    """以 `end` 结尾的 n 个工作日（只用于造历，不模拟节假日）。"""
    d, out = date.fromisoformat(end), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_boundary_constants_match_the_prereg() -> None:
    """改这三个数等于改样本区间，必须同时改预注册 §5。"""
    assert gate.EXPLORATION_END == "2026-10-08"
    assert gate.CONFIRM_START == "2026-10-09"
    assert gate.CONFIRM_DAYS == 250
    assert gate.EXPLORATION_END < gate.CONFIRM_START


def test_cap_calendar_stops_decision_days_but_keeps_the_forward_window() -> None:
    """截的是决策日，不是价格日历。

    末决策日 2026-10-08 的 20 日收益要读到 2026-10-28；把价格也截掉会把
    第 326 天整个丢掉，样本从 326 变 325。
    """
    cal = _daily("2026-09-01", 90)
    # 不截：决策日已经进入确认段——这就是 2026-11-06 之后会发生的事
    assert max(gate.decision_days(cal)) >= gate.CONFIRM_START

    cap = gate.cap_calendar(cal)
    dd = gate.decision_days(cap)
    assert dd[-1] == "2026-10-08"
    assert all(d < gate.CONFIRM_START for d in dd)
    assert len(cap) == cal.index(gate.EXPLORATION_END) + 1 + gate.HORIZON
    assert cap[-1] == (date.fromisoformat(gate.EXPLORATION_END)
                       + timedelta(days=gate.HORIZON)).isoformat()


def test_audit_keeps_the_analysis_window_fixed_while_the_clock_advances(
        tmp_path: Path) -> None:
    """第 367 个分区落盘时，分析窗不许跟着走，时钟必须走。

    回归的是 v6 的 `audit()`：它用**全历**算分析窗，366 天时恰好也是 326，
    看起来对；第 367 天一到 `decision_days_last` 变成 2026-10-09，而断言写成
    `<= 2026-10-08`。测试会红，但产物是干净的——报错原因与真实原因相反。
    """
    base = tmp_path / gate.TABLE
    base.mkdir()
    #: 与生产一致：366 个分区、末日 2026-11-05，第 346 个分区是 2026-10-08。
    for d in _trading_days_ending("2026-11-05", 366):
        (base / f"date={d}").mkdir()

    a = gate.audit(data_dir=tmp_path, paths=[])
    assert a["analysis_window"] == {"decision_days": 326,
                                    "last_decision_day": gate.EXPLORATION_END}
    assert a["confirmation_clock"]["decision_days_computable"] == 0
    assert a["confirmation_clock"]["remaining"] == 250

    #: 2026-11-06 收盘后落盘的正是这一天。
    for i, d in enumerate(("2026-11-06", "2026-11-09"), 1):
        (base / f"date={d}").mkdir()
        a = gate.audit(data_dir=tmp_path, paths=[])
        assert a["analysis_window"]["decision_days"] == 326
        assert a["analysis_window"]["last_decision_day"] == gate.EXPLORATION_END
        assert a["confirmation_clock"]["decision_days_computable"] == i
        assert a["confirmation_clock"]["first"] == "2026-10-09"
        assert a["confirmation_clock"]["remaining"] == 250 - i
        assert a["clean"], "产物是干净的：错的只是被合在一起的那个字段"


def test_artifact_scan_finds_confirmation_dates(tmp_path: Path) -> None:
    p = tmp_path / "exc.json"
    p.write_text(json.dumps({"low_turnover": {
        "2026-10-08": [0.01] * 20, "2026-10-09": [0.01] * 20}}), encoding="utf-8")
    rep = gate.scan_artifacts([p])
    assert rep["exc.json"]["last"] == "2026-10-09"
    assert rep["exc.json"]["confirmation_days"] == ["2026-10-09"]


def test_require_exploration_refuses_then_logs_exactly_one_row(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    days = ["2026-10-08", "2026-10-09"]
    with pytest.raises(SystemExit) as exc:
        gate.require_exploration(days, "probe_net_diff", ["--nmax=400"], path=log)
    assert "--include-confirmation" in str(exc.value)
    assert not log.exists(), "拒绝时不许留下半条记录"

    gate.require_exploration(days, "probe_net_diff",
                             ["--nmax=400", "--include-confirmation"],
                             allow=True, path=log)
    rows = _rows(log)
    assert len(rows) == 1
    assert rows[0]["first_decision_day"] == "2026-10-09"
    assert rows[0]["last_decision_day"] == "2026-10-09"
    assert rows[0]["n"] == 1
    ok, msg = gate.verify_log(log)
    assert ok, msg


def test_log_chain_detects_edits_and_deletions(tmp_path: Path) -> None:
    """日志是「只追加」的证据：改一个字、删一行都会断链。"""
    log = tmp_path / "log.jsonl"
    gate.log_read("probe_net_diff", ["a"], "2026-10-09", "2026-10-10", path=log)
    gate.log_read("probe_net_diff", ["b"], "2026-10-11", "2026-10-12", path=log)
    assert gate.verify_log(log)[0]

    lines = log.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["last_decision_day"] = "2026-09-30"
    lines[0] = json.dumps(row, ensure_ascii=False, sort_keys=True)
    edited = tmp_path / "edited.jsonl"
    edited.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, msg = gate.verify_log(edited)
    assert not ok and "哈希" in msg

    cut = tmp_path / "cut.jsonl"
    cut.write_text(lines[1] + "\n", encoding="utf-8")
    ok, msg = gate.verify_log(cut)
    assert not ok and "序号" in msg


def test_seal_covers_code_and_caches_but_not_docs() -> None:
    """文档不能进封签：哈希要写进预注册，把预注册哈希进去就永远对不上。"""
    payload = gate.build_seal()
    assert "scripts/probe_window.py" in payload["files"]
    assert "scripts/probe_net_diff.py" in payload["files"]
    assert "scripts/decision_gate.py" in payload["files"]
    assert not any(k.startswith("docs/") for k in payload["files"])
    assert len(payload["seal_sha256"]) == 64
    assert payload["audit"]["clean"]


def test_seal_hash_is_stable_across_calls() -> None:
    """`signed_at` 不进哈希，否则每次封签都是新哈希，无法引用。"""
    assert gate.build_seal()["seal_sha256"] == gate.build_seal()["seal_sha256"]


def test_written_seal_verifies(tmp_path: Path) -> None:
    """写盘再校验必须通过——`signed_at` 若混进哈希，封签一落盘就自证失效。"""
    p = tmp_path / "seal.json"
    payload = gate.seal(p)
    ok, drift = gate.verify_seal(p)
    assert ok, drift
    assert json.loads(p.read_text(encoding="utf-8"))["seal_sha256"] == payload["seal_sha256"]


def test_seal_is_closed_over_loaded_scripts() -> None:
    """封签必须对**内部依赖**闭合，两条通路都算：`_load()` 与 `import probe_*`。

    `probe_net_diff.py` 的配对 p 值与 se_HAC 来自 `probe_paired_diff.py`。
    只哈希调用方，等于把「配对表显著」这个论据的产地留在门外——改一行
    `p_two_sided` 就能换掉整张表，而 `verify-seal` 不会响。

    同一条病还有第二条通路：`import probe_decile_net as P`。13s 把生存偏差的
    **产物**封了，生成器却留在门外，于是「改探针、产物不动」四道门可以一起绿。
    只查 `_load()` 的旧版看不见这种写法——正则覆盖谁，就等于豁免谁。
    """
    import re
    load_pat = re.compile(r'_load\(\s*"[^"]+",\s*"(scripts/[^"]+\.py)"')
    imp_pat = re.compile(r"^\s*(?:import|from)\s+(probe_\w+)", re.M)
    sealed_rel = {str(p.relative_to(REPO)).replace("\\", "/")
                  for p in gate.SEALED if str(p).endswith(".py")}
    missing = set()
    for rel in sealed_rel:
        text = (REPO / rel).read_text(encoding="utf-8")
        for target in load_pat.findall(text):
            if target not in sealed_rel:
                missing.add(f"{rel} → {target}")
        for mod in imp_pat.findall(text):
            target = f"scripts/{mod}.py"
            if target not in sealed_rel:
                missing.add(f"{rel} → {target}（import）")
    assert not missing, "被冻结脚本依赖了未冻结的脚本：" + "；".join(sorted(missing))


def test_resigning_keeps_the_previous_hash(tmp_path: Path, monkeypatch) -> None:
    """重签不能抹掉上一版：否则「改到对得上为止」就是敞开的。"""
    p = tmp_path / "seal.json"
    first = gate.seal(p)

    #: 注入点必须在**算哈希之前**：改 `build_seal` 的返回值会绕过哈希，
    #: 落盘即自相矛盾。`audit()` 在 build_seal 内部、哈希之前被调用。
    orig = gate.audit
    monkeypatch.setattr(gate, "audit", lambda *a, **k: {**orig(*a, **k), "note": "v6"})
    second = gate.seal(p)

    hist = gate.seal_history_path(p)
    rows = _rows(hist)
    assert [r["n"] for r in rows] == [1, 2]
    assert rows[0]["seal_sha256"] == first["seal_sha256"]
    assert rows[1]["seal_sha256"] == second["seal_sha256"]
    assert rows[1]["prev"] == rows[0]["seal_sha256"]
    assert gate.verify_history(p)[0] == []
    assert gate.verify_seal(p)[0]


def test_seal_history_records_why_it_was_resigned(tmp_path: Path) -> None:
    """链上只有哈希和时间，两次签字相隔 14 分钟就分不清改了什么。

    `reason` 只进历史行、不进 `seal_sha256`：内容相同的两次签字应当得到
    同一个哈希，而「为什么又签一次」由链回答。
    """
    p = tmp_path / "seal.json"
    first = gate.seal(p, reason="v6：§4 重写为四格分区")
    second = gate.seal(p, reason="v7：audit 拆分析窗与确认时钟")
    rows = _rows(gate.seal_history_path(p))
    assert [r["reason"] for r in rows] == ["v6：§4 重写为四格分区",
                                          "v7：audit 拆分析窗与确认时钟"]
    assert rows[1]["prev"] == rows[0]["seal_sha256"]
    assert first["seal_sha256"] == second["seal_sha256"], "reason 不许进封签哈希"
    assert gate.verify_history(p)[0] == []


def test_swapping_back_an_older_seal_is_detected(tmp_path: Path,
                                                monkeypatch) -> None:
    """把旧封签原样放回来，文件哈希全对——只有历史链能看出来。"""
    p = tmp_path / "seal.json"
    gate.seal(p)
    old_text = p.read_text(encoding="utf-8")

    orig = gate.audit
    monkeypatch.setattr(gate, "audit", lambda *a, **k: {**orig(*a, **k), "note": "v6"})
    gate.seal(p)

    p.write_text(old_text, encoding="utf-8")
    ok, drift = gate.verify_seal(p)
    assert not ok and any("历史" in d for d in drift), drift


def test_seal_history_chain_detects_a_missing_row(tmp_path: Path,
                                                  monkeypatch) -> None:
    p = tmp_path / "seal.json"
    gate.seal(p)
    orig = gate.audit
    monkeypatch.setattr(gate, "audit", lambda *a, **k: {**orig(*a, **k), "note": "v6"})
    gate.seal(p)

    hist = gate.seal_history_path(p)
    rows = _rows(hist)
    hist.write_text(json.dumps(rows[1], ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    problems, _ = gate.verify_history(p)
    assert problems and any("序号" in x for x in problems), problems


def test_live_seal_verifies() -> None:
    """仓库里那份封签本身必须在 CI 里被校验。

    没有这条，改一个冻结脚本只会让 `verify-seal` 这条**手动命令**变红，
    `pytest` 全绿——而 `test_prereg_records_the_seal_hash` 只比对
    「文档里写没写这个哈希」，它不重算文件哈希，抓不到代码漂移。
    """
    if not gate.SEAL.exists():
        pytest.skip("尚未封签")
    ok, drift = gate.verify_seal()
    assert ok, drift


def test_prereg_records_the_seal_hash() -> None:
    if not gate.SEAL.exists():
        pytest.skip("尚未封签")
    saved = json.loads(gate.SEAL.read_text(encoding="utf-8"))
    text = gate.PREREG.read_text(encoding="utf-8")
    assert saved["seal_sha256"] in text, "封签哈希没写进预注册 §12——封签无人认领"


def test_artifacts_are_clean_before_the_first_logged_read() -> None:
    """先验条件：没有读取记录时，产物里不许有确认段决策日。"""
    if gate.READ_LOG.exists():
        pytest.skip("已发生首次读取，先验条件解除")
    if not gate.sessions():
        pytest.skip("本机没有行情分区")
    a = gate.audit()
    assert a["clean"], a["violations"]
    #: 断言的是**分析窗**，不是确认时钟：时钟必须每天增长，拿它当先验条件会
    #: 在分区落盘的那天把一条正确的前进读成越界。
    assert a["analysis_window"]["last_decision_day"] <= gate.EXPLORATION_END


CS_IC_CACHE = gate.DATA / ".probe_cs_ic.json"


def test_cs_ic_cache_is_audited_and_sealed() -> None:
    """v13a：写盘清单必须对 `probe_cs_ic.py` 的缓存闭合。

    该脚本不 import 闸门、不在 §5.2 拦截表里——v12 的清单对它不成立，
    `audit` 恒报「无此产物」。366 个分区时它恰好只算到 2026-10-08，所以
    看起来干净；这不是闸门在工作，是巧合在工作。毒化验收见下一条。
    """
    assert CS_IC_CACHE in gate.ARTIFACTS
    assert CS_IC_CACHE in gate.SEALED
    if not CS_IC_CACHE.exists():
        pytest.skip("本机没有 cs_ic 缓存")
    rep = gate.scan_artifacts([CS_IC_CACHE])[CS_IC_CACHE.name]
    assert rep["n_days"] == 326
    assert rep["last"] == gate.EXPLORATION_END
    assert rep["confirmation_days"] == []


def test_poisoned_cs_ic_cache_is_caught_by_audit(tmp_path: Path) -> None:
    """毒化副本（塞进 2026-10-09）必须让 `audit` 报 violation。

    这条是 v13a 的可证伪验收：改动前，cs_ic 缓存不在 `ARTIFACTS` 里，
    同样的毒化对 `audit` 完全不可见——「清单闭合」不是一句声明，是这条
    测试能红的那个事实。
    """
    if not CS_IC_CACHE.exists():
        pytest.skip("本机没有 cs_ic 缓存")
    raw = json.loads(CS_IC_CACHE.read_text(encoding="utf-8"))
    name = next(iter(raw))
    assert isinstance(raw[name], dict), "结构变了：{策略:{日期:...}} 的假设失效"
    raw[name]["2026-10-09"] = raw[name][sorted(raw[name])[-1]]
    poisoned = tmp_path / CS_IC_CACHE.name
    poisoned.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    a = gate.audit(paths=(poisoned,))
    assert not a["clean"]
    assert (poisoned.name, "2026-10-09") in a["violations"]
    #: 真缓存本身必须仍然干净——毒化的只是副本。
    assert gate.scan_artifacts([CS_IC_CACHE])[CS_IC_CACHE.name]["confirmation_days"] == []
