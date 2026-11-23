"""短线雷达排序体检（shortline_audit）测试 — 锁「会让前向登记变假」的五件事。

1. 池子必须从漏斗里暴露出来（零分布的分母），且成员 == cold_ok 本身，不另造第二套定义。
2. 登记幂等：同日期不重复写（定时任务 17:10 与 20:10 各跑一次）。
3. 零分布是解析式：lift / z 必须能用独立的闭式计算复核（不是「自己验自己」）。
4. 按月聚簇：fwd20 重叠 20 倍，逐日 t 会虚高 —— 聚合必须每月只算 1 个观测。
5. 回填与前向必须分开聚合：裁决只看 forward，混在一起就废了预注册。

夹具全部合成，不读用户数据、不联网。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services import shortline_audit as svc


# ------------------------------------------------------------------ 池子暴露

def test_focus_exposes_pool_for_null_distribution():
    """_focus 必须把 cold_ok 的成员（含 heat 四项分位）原样暴露出来。

    这是零分布的分母。若哪天有人在这里二次过滤（比如又剔一遍 ST），
    台账里的池子就和页面预算里的 cold 对不上，z 会静默变假。
    """
    from app.services import citizenship

    rows = pl_rows(40)
    f = citizenship._focus(rows)
    syms = [p["symbol"] for p in f["pool"]]
    assert len(syms) == f["budget"]["cold"]
    assert set(syms) == {r["symbol"] for r in rows.to_dicts()}
    # 供候选键计算，四项分位必须都在
    assert all({"p_amp", "p_turn_med", "p_scale", "p_amount", "heat"} <= set(p) for p in f["pool"])
    # 空态也必须有这个键（前端读 focus.sort_audit 不抛）
    assert citizenship._focus(rows.slice(0, 0))["pool"] == []


def pl_rows(n: int):
    from tests.test_citizenship import FOCUS_COLS, _rows  # 复用同一份合成口径

    return _rows(*[{"symbol": f"3000{i:03d}.SZ", "heat": 0.9} for i in range(n)]).select(FOCUS_COLS)


# ------------------------------------------------------------------ 候选键与登记

def test_shadow_score_is_frozen_formula():
    """候选键 = (p_amp + (1-p_scale))/2。冻结公式一旦被改，预注册就作废 —— 必须红。"""
    assert svc.shadow_score(0.8, 0.2) == pytest.approx((0.8 + 0.8) / 2)
    assert svc.shadow_score(None, None) == pytest.approx(0.0)  # 双缺兜底 = 0.5*0 + 0.5*(1-1)
    assert svc.shadow_score(0.0, 1.0) == pytest.approx(0.0)
    assert svc.shadow_score(1.0, 0.0) == pytest.approx(1.0)


def test_record_day_is_idempotent(tmp_path: Path, monkeypatch):
    """同日期登记两次只落一行 —— 定时任务主跑 + 补跑共用这个保证。"""
    payload = _payload()
    monkeypatch.setattr("app.services.citizenship.get_citizenship", lambda dd, as_of=None: payload)

    r1 = svc.record_day(tmp_path, source="forward")
    assert r1 is not None and r1["date"] == "2026-11-24"
    r2 = svc.record_day(tmp_path, source="forward")
    assert r2 is None, "同日期必须跳过，否则定时任务每天写两行"

    rows = svc.read_ledger(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "forward"
    assert row["rule_current"] == svc.RULE_CURRENT
    assert row["rule_shadow"] == svc.RULE_SHADOW
    assert row["picks"] == ["300001.SZ", "300002.SZ"]
    # 候选键排序：p_amp 高者在前
    assert row["shadow"][0] == "300001.SZ"


def test_forward_before_prereg_start_is_downgraded_to_backfill(tmp_path: Path, monkeypatch):
    """预注册守卫：起始日之前的数据不得以 forward 身份入账。

    2026-11-23 部署当天 17:10 定时任务就会跑一次 —— 若不拦，11-23 会混进
    forward 口径，预注册的「唯一干净样本外从 11-24 开始」就失效了。
    """
    payload = _payload()
    payload["as_of"] = "2026-11-23"  # 早于 PREREG_START(2026-11-24) 一天
    monkeypatch.setattr("app.services.citizenship.get_citizenship", lambda dd, as_of=None: payload)

    row = svc.record_day(tmp_path, source="forward")
    assert row is not None
    assert row["source"] == "backfill", "起始日之前请求 forward 必须被降级"


def _payload() -> dict:
    return {
        "as_of": "2026-11-24",
        "focus": {
            "budget": {"market": 5000, "struct": 400, "cold": 60, "picks": 2},
            "picks": [{"symbol": "300001.SZ"}, {"symbol": "300002.SZ"}],
            "pool": [
                {"symbol": "300001.SZ", "heat": 0.95, "p_amp": 0.9, "p_turn_med": 0.9,
                 "p_scale": 0.2, "p_amount": 0.9, "amount": 1e8},
                {"symbol": "300002.SZ", "heat": 0.90, "p_amp": 0.7, "p_turn_med": 0.8,
                 "p_scale": 0.3, "p_amount": 0.8, "amount": 8e7},
            ],
        },
    }


# ------------------------------------------------------------------ 零分布评估

def test_evaluate_matches_closed_form(tmp_path: Path, monkeypatch):
    """lift 与 z 必须能被独立闭式计算复核。

    合成宇宙：60 只票，日收益三档（选中 1%、影子 0.5%、其余 0%）。
    close 取几何级数 ⇒ 任一 20 日窗收益 = (1+v)^20 − 1，只依赖 v 不依赖起点，
    这样闭式期望才写得出来。期望值在测试里独立算出 —— 这是「第二份定义
    被测试绑住」的落点，不是自己验自己。
    """
    n = 60
    r = {f"3000{i:03d}.SZ": (0.01 if i < 15 else 0.005 if 20 <= i < 35 else 0.0) for i in range(n)}
    cal = [date(2026, 1, 5) + timedelta(days=i) for i in range(100)]  # 100 个自然日当交易日历

    # 三个信号日，分布在三个不同月份 ⇒ 聚合后 months=3
    signal_idx = [0, 35, 70]
    ledger = []
    for k in signal_idx:
        pool = [{"symbol": s, "heat": 0.9, "p_amp": 0.9, "p_turn_med": 0.9,
                 "p_scale": 0.2, "p_amount": 0.9, "amount": 1e8} for s in r]
        ledger.append({
            "date": cal[k].isoformat(), "source": "backfill",
            "rule_current": svc.RULE_CURRENT, "rule_shadow": svc.RULE_SHADOW,
            "budget": {"cold": n}, "picks": [f"3000{i:03d}.SZ" for i in range(15)],
            "shadow": [f"3000{i:03d}.SZ" for i in range(20, 35)], "pool": pool,
        })
    f = tmp_path / "shortline" / "sort_ledger.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join(json.dumps(x) for x in ledger), encoding="utf-8")

    def fake_closes(dd, day):
        j = cal.index(day)
        return {s: 100.0 * (1 + v) ** j for s, v in r.items()}

    monkeypatch.setattr("app.services.citizenship._enriched_days", lambda dd: cal)
    monkeypatch.setattr(svc, "_closes", fake_closes)

    out = svc.evaluate(tmp_path)
    assert out["days_registered"] == 3
    assert out["days_evaluated"] == 3
    assert out["forward"] is None, "回填行不得进 forward"
    allw = out["all"]
    assert allw["months"] == 3

    # 独立闭式：单窗收益 f(v)=(1+v)^20−1；exc = f(v) − mean(f)；pool=全宇宙 ⇒ mean(exc[pool])=0
    fr = {s: (1 + v) ** 20 - 1 for s, v in r.items()}
    mkt = sum(fr.values()) / n
    exc = {s: v - mkt for s, v in fr.items()}
    exp_cur_lift = sum(exc[f"3000{i:03d}.SZ"] for i in range(15)) / 15 * 1e4
    exp_shd_lift = sum(exc[f"3000{i:03d}.SZ"] for i in range(20, 35)) / 15 * 1e4
    assert allw["cur_lift_bp"] == pytest.approx(exp_cur_lift, abs=0.5)
    assert allw["shadow_lift_bp"] == pytest.approx(exp_shd_lift, abs=0.5)

    # z 也用闭式核对（不只是看符号）：sd(ddof=1) / sqrt(n) * 有限总体修正
    vals = list(exc.values())
    mean_exc = sum(vals) / n
    sd = (sum((v - mean_exc) ** 2 for v in vals) / (n - 1)) ** 0.5
    se = sd / (15 ** 0.5) * ((n - 15) / (n - 1)) ** 0.5
    exp_cur_z = (exp_cur_lift / 1e4) / se
    exp_shd_z = (exp_shd_lift / 1e4) / se
    assert allw["series"][0]["cur_z"] == pytest.approx(exp_cur_z, abs=0.05)
    assert allw["series"][0]["shadow_z"] == pytest.approx(exp_shd_z, abs=0.05)
    # 方向感：现行明显优于随机，影子介于「随机」与「现行」之间
    assert allw["series"][0]["cur_z"] > 5
    assert 0 < allw["series"][0]["shadow_z"] < allw["series"][0]["cur_z"]


def test_forward_and_backfill_are_aggregated_separately(tmp_path: Path, monkeypatch):
    """裁决口径：forward 聚合只含预注册起始日之后的行。混了就废了预注册。"""
    n = 60
    r = {f"3000{i:03d}.SZ": 0.0 for i in range(n)}
    cal = [date(2026, 1, 5) + timedelta(days=i) for i in range(90)]
    ledger = []
    for k, src in [(0, "backfill"), (40, "forward"), (41, "forward")]:
        ledger.append({
            "date": cal[k].isoformat(), "source": src,
            "rule_current": svc.RULE_CURRENT, "rule_shadow": svc.RULE_SHADOW,
            "budget": {"cold": n},
            "picks": [f"3000{i:03d}.SZ" for i in range(15)],
            "shadow": [f"3000{i:03d}.SZ" for i in range(20, 35)],
            "pool": [{"symbol": s, "heat": 0.9, "p_amp": 0.5, "p_turn_med": 0.5,
                      "p_scale": 0.5, "p_amount": 0.5, "amount": 1e8} for s in r],
        })
    f = tmp_path / "shortline" / "sort_ledger.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join(json.dumps(x) for x in ledger), encoding="utf-8")

    monkeypatch.setattr("app.services.citizenship._enriched_days", lambda dd: cal)
    monkeypatch.setattr(svc, "_closes", lambda dd, day: {s: 100.0 for s in r})

    out = svc.evaluate(tmp_path)
    assert out["days_registered"] == 3
    assert out["fwd_days"] == 2 and out["backfill_days"] == 1
    # 全零收益 ⇒ lift=0；但池子 <30 只会被跳过……这里 40 只，全部可评估
    assert out["all"]["months"] >= 1
    assert out["forward"] is not None and out["forward"]["months"] >= 1
    assert out["all"]["cur_lift_bp"] == pytest.approx(0.0, abs=0.05)


def test_prereg_constants_are_frozen_and_consistent():
    """预注册常量必须自洽：起始日可解析、门槛为正、评估窗口与文档一致。"""
    assert date.fromisoformat(svc.PREREG_START) >= date(2026, 11, 24)
    assert svc.GATE_MIN_MONTHS >= 6
    assert svc.GATE_LIFT_DELTA_BP > 0 and svc.GATE_T > 0
    assert svc.COST_RT > 0 and svc.TOP_N == 15
    assert svc.RULE_SHADOW != svc.RULE_CURRENT


def test_artifact_roundtrip_and_missing(tmp_path: Path):
    assert svc.load_artifact(tmp_path) is None
    art = {"prereg_id": svc.PREREG_ID, "gate": {"min_months": svc.GATE_MIN_MONTHS}}
    f = svc.artifact_path(tmp_path)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(art), encoding="utf-8")
    assert svc.load_artifact(tmp_path)["prereg_id"] == svc.PREREG_ID
