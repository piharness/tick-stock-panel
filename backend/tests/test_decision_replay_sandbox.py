"""整条链放到真实 parquet 上跑一遍：produce → settle → 读回 → 第二路径重算。

种子数据只能证明「代码按我理解的口径走」。这个测试跑 `scripts/verify_decision_replay.py`，
它在真实分区上走完整条链，然后**不复用** `app.decision.bars` 的任何一行代码，
从原始文件重算成交价、出场价、收益与全市场中位数，逐项比对。

没有真实分区时跳过，不假装通过。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_decision_replay.py"
DATA = ROOT / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "kline_daily_enriched").is_dir(), reason="需要真实日K分区（盘后管道产物）")


def _run(tmp_path: Path, *extra: str, name: str = "replay.json") -> dict:
    out = tmp_path / name
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(out), *extra],
        cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, (proc.stdout[-3000:] + proc.stderr[-3000:])
    return json.loads(out.read_text(encoding="utf-8"))


def test_chain_runs_on_real_parquet(tmp_path: Path) -> None:
    rep = _run(tmp_path)
    # 1) 冷启动不出卡：没有证据时生产者的行为是 skip，不是编一个 p_win。
    assert rep["cold_start_status"] == "skipped_no_evidence"
    # 2) 显式 allow_prior 才能建卡，出处记 prior。
    assert rep["produce_status"] == "created"
    assert rep["pwin_source"] == "prior"
    # 3) 结算 → 读回，行数闭合。
    assert rep["settle"]["written"] == rep["read_back"] == len(rep["picks"])
    assert rep["settle"]["pending"] == 0
    # 4) median 口径的覆盖率必须等于写入行数:差一条就是永久缺一条。
    assert rep["settle"]["median_covered"] == rep["settle"]["written"]
    # 5) 结算之后账本仍是健康态（没有固化残缺行）。
    assert rep["health_after"] == "ok"


def test_settlement_prices_match_the_second_path(tmp_path: Path) -> None:
    rep = _run(tmp_path)
    assert len(rep["picks"]) >= 3
    assert rep["max_ret_diff_bp"] < rep["tolerance_bp"], rep["picks"]
    assert rep["max_median_diff_bp"] < rep["tolerance_bp"], rep["picks"]
    for pick in rep["picks"]:
        assert pick["diff"]["fill_bp"] == 0
        assert pick["diff"]["outcome_match"] is True
        assert pick["diff"]["outcome_med_match"] is True


def test_adjustment_freshness_is_reported(tmp_path: Path) -> None:
    """窗口内的除权事件有没有并入复权序列，必须与结论一起出现。

    这一项**不**计入 `ok`：它量的是行情侧的数据新鲜度，不是账本口径。但收益
    含不含分红决定 `outcome`，所以它不能被静默省略——回放挑到无事件的标的
    就报绿，等于让一个真实的结算偏差看不见。
    """
    rep = _run(tmp_path)
    adj = rep["adjustment_check"]
    assert adj["checked"] == adj["baked"] + adj["unbaked"]
    assert isinstance(adj["unbaked_symbols"], list)


def test_unbaked_event_names_still_match_the_second_path(tmp_path: Path) -> None:
    """把「参考序列恰好没重算」的标的**主动**塞进比对，而不是等抽样抽到。

    默认抽样刻意避开这些标的（成因分离），代价是 0 bp 只证明「没事件的标的
    算得对」。这条用例反向取样：拿 `adjustment_check` 报出来的未并入标的，
    强制它们进比对。事件因子没进收益时，这里的差就是分红本身——窗口内最大的
    一笔远大于容差，不是小数点后几位。
    """
    base = _run(tmp_path, name="base.json")
    syms = [u["symbol"] for u in base["adjustment_check"].get("unbaked_symbols") or []]
    if not syms:
        pytest.skip("窗口内没有未并入的除权事件，这条路径今天无从验证")
    rep = _run(tmp_path, "--symbols", ",".join(syms), "--include-events",
               name="events.json")
    assert rep["block_events"] is False
    assert rep["picks"], rep
    assert rep["max_ret_diff_bp"] < rep["tolerance_bp"], rep["picks"]
