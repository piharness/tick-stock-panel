"""竞价产物「读点精确到秒」的契约：`HH:MM` 标签不许静默变成该分钟的末帧。

背景（2026-11-12）：首版 `frame_at()` 用 `HH:MM` 字符串比大小，`<= "09:24"` 实际选中的
是 09:24:59 那一帧 —— 标签写 09:24、值却是 09:24:59 的。三处后果都能量出来：

1. 09:24 的倍数被系统性低估（最后一分钟恰是流入最猛的一分钟）；
2. 「退回 `V_obs` 原值损失多少头寸」的分母跟着错；
3. 验收判据的分位阈值会随读点翻转 ⇒ 判据本身不成立。

下面钉住四件事：

1. `probes` 里的 `used_ts` 与标签的偏移 ≤ 容差，且 1 秒级的差必须真的取到 1 秒级；
2. 帧太稀时偏移会被记进 `probe_guard.misaligned`（不是静默通过）；
3. `V_final` 逐票取：同一帧里 stamp 有 092500/092501/092502 的票都不许被丢掉；
4. `late_share` 与 `mult` 是同一个数的两种写法，不许各算一套。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auc = _load("diag_auction_indicative", "analysis/_diag_auction_indicative.py")


def _quote(*, name: str, last: float, b1v: float, b1p: float, amt: float,
           stamp: str, shared: bool) -> dict:
    """造一条快照。`shared=True` 走科创板口径（[6] 是股），否则走主板（[6] 是手）。"""
    vol = amt * 1e4 / last / (1.0 if shared else 100.0)
    return {"name": name, "last": last, "prev": last, "open": last,
            "vol_hand": vol, "amount_wan": amt,
            "b1p": b1p, "b1v": b1v, "a1p": b1p, "a1v": b1v, "stamp": stamp}


def _pre(sym: str, amt: float = 200000.0) -> dict:
    """竞价前的一帧：stamp 是前一交易日，amount_wan 是昨全天成交额。"""
    return {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=0.0, amt=amt,
                        stamp="20261111161415", shared=False)}


def _frame(ts: str, snap: dict) -> dict:
    return {"ts": f"2026-11-12T{ts}", "label": "test", "snap": snap}


def test_probe_label_is_second_precise_not_minute_end():
    """09:24:00 必须取 09:23:59 那帧，不许跳到最后 09:24:59 的帧（首版正是这样偏了 59 秒）。"""
    sym = "000001.SZ"
    frames = [
        _frame("09:10:00", _pre(sym)),
        _frame("09:23:59", {sym: _quote(name="测试", last=10.0, b1v=100.0, b1p=10.0,
                                       amt=0.0, stamp="20261112092357", shared=False)}),
        _frame("09:24:59", {sym: _quote(name="测试", last=10.0, b1v=400.0, b1p=10.0,
                                       amt=0.0, stamp="20261112092457", shared=False)}),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.99,
                                       amt=1000.0, stamp="20261112092500", shared=False)}),
    ]
    rows, meta = auc.analyze_day(frames)
    assert meta["probes"]["09:24:00"]["used_ts"].endswith("09:23:59")
    assert meta["probes"]["09:24:00"]["buffer_s"] == 61
    assert meta["probes"]["09:24:59"]["used_ts"].endswith("09:24:59")

    r = rows[0]
    # 09:24:00 的虚拟匹配额 = 100 手 × 100 × 10 / 1e4 = 10 万元 ⇒ 倍数 100
    assert r["ind_amt_wan_09:24:00"] == pytest.approx(10.0)
    assert r["mult_09:24:00"] == pytest.approx(100.0)
    # 09:24:59 = 40 万元 ⇒ 倍数 25。两个读点差 4 倍 —— 所以标签错 59 秒不是小事。
    assert r["mult_09:24:59"] == pytest.approx(25.0)


def test_sparse_frames_are_flagged_not_silently_passed():
    """帧太稀时（09:20 前后只有 09:19:00 那一帧）偏移是分钟级，必须能被记成 misaligned。"""
    sym = "000001.SZ"
    frames = [
        _frame("09:19:00", {sym: _quote(name="测试", last=10.0, b1v=50.0, b1p=10.0,
                                       amt=0.0, stamp="20261112091858", shared=False)}),
        _frame("09:24:59", {sym: _quote(name="测试", last=10.0, b1v=400.0, b1p=10.0,
                                       amt=0.0, stamp="20261112092457", shared=False)}),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.99,
                                       amt=1000.0, stamp="20261112092500", shared=False)}),
    ]
    _, meta = auc.analyze_day(frames)
    off = meta["probes"]["09:20:00"]["offset_s"]
    assert off is not None and off > auc.PROBE_TOL_S
    # 09:24:59 的 1 秒偏移在容差内，但它不是可行动读点（余量 < 30 秒）
    assert meta["probes"]["09:24:59"]["actionable"] is False
    # 注意 buffer_s 单独看会骗人：09:24:30 回落到了 09:19:00，余量 360 秒看着最宽裕，
    # 其实是 5 分半前的老帧。所以「可行动」必须同时看 offset_s —— 只看余量会把陈旧帧
    # 当成最新帧，这正是本测试第一版自己犯的错。
    assert meta["probes"]["09:24:30"]["offset_s"] > auc.PROBE_TOL_S
    assert meta["probes"]["09:24:30"]["buffer_s"] > auc.ACTION_MIN_BUFFER_S
    assert meta["probes"]["09:24:30"]["actionable"] is False


def test_minute_label_without_seconds_is_rejected():
    """传 `HH:MM` 必须抛，不许靠字符串比较静默取末帧。"""
    with pytest.raises(ValueError):
        # 通过 analyze_day 的公开路径触发：PROBES 一旦被改成分钟级就会在这里炸。
        original = auc.PROBES
        try:
            auc.PROBES = ("09:24",)
            auc.analyze_day([
                _frame("09:24:59", {"000001.SZ": _quote(name="测试", last=10.0, b1v=1.0,
                                                       b1p=10.0, amt=0.0,
                                                       stamp="20261112092457", shared=False)}),
                _frame("09:25:01", {"000001.SZ": _quote(name="测试", last=10.0, b1v=0.0,
                                                       b1p=9.99, amt=1000.0,
                                                       stamp="20261112092500", shared=False)}),
            ])
        finally:
            auc.PROBES = original


def test_final_is_per_stock_so_push_skew_does_not_drop_names():
    """092500 / 092501 / 092502 三种 stamp 都要拿到 V_final：逐帧要求「全部到位」会丢票。"""
    a, b, c = "000001.SZ", "000002.SZ", "600000.SH"
    frames = [
        _frame("09:10:00", {**_pre(a), **_pre(b), **_pre(c)}),
        _frame("09:25:02", {
            a: _quote(name="A", last=10.0, b1v=0.0, b1p=9.9, amt=900.0,
                      stamp="20261112092500", shared=False),
            b: _quote(name="B", last=10.0, b1v=0.0, b1p=9.9, amt=800.0,
                      stamp="20261112092501", shared=False),
            c: _quote(name="C", last=10.0, b1v=0.0, b1p=9.9, amt=700.0,
                      stamp="20261112092502", shared=False),
        }),
    ]
    rows, meta = auc.analyze_day(frames)
    assert meta["n_final"] == 3
    assert {r["symbol"] for r in rows} == {a, b, c}
    assert {r["final_stamp"] for r in rows} == {"092500", "092501", "092502"}


def test_continuous_session_amount_is_not_taken_as_auction_amount():
    """09:30 之后同一字段被连续竞价改写；stamp ≥ 093000 的帧不许当 V_final。"""
    sym = "000001.SZ"
    frames = [
        _frame("09:10:00", _pre(sym)),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.99,
                                       amt=1000.0, stamp="20261112092500", shared=False)}),
        _frame("09:30:05", {sym: _quote(name="测试", last=10.0, b1v=5.0, b1p=9.99,
                                       amt=99999.0, stamp="20261112093001", shared=False)}),
    ]
    rows, _ = auc.analyze_day(frames)
    assert rows[0]["auc_amt_wan"] == 1000.0


def test_late_share_and_mult_are_the_same_number():
    """`late_share = 1 − 1/mult`。两个产地各算一套的话，容量口径会悄悄错。"""
    sym = "000001.SZ"
    frames = [
        _frame("09:10:00", _pre(sym)),
        _frame("09:23:59", {sym: _quote(name="测试", last=10.0, b1v=100.0, b1p=10.0,
                                       amt=0.0, stamp="20261112092357", shared=False)}),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.9,
                                       amt=1000.0, stamp="20261112092500", shared=False)}),
    ]
    rows, _ = auc.analyze_day(frames)
    r = rows[0]
    assert r["late_share_09:24:00"] == pytest.approx(1 - 1 / r["mult_09:24:00"])


@pytest.mark.parametrize("sym,shared,expect", [
    ("600000.SH", False, 100.0),   # 主板 [6] 是手
    ("688386.SH", True, 1.0),      # 科创板 [6] 是股
])
def test_unit_factor_splits_board_conventions(sym, shared, expect):
    """单位反解：主板 100、科创板 1。混用会让 688 的倍数凭空大 100 倍。"""
    f = {"ts": "2026-11-12T09:25:01", "snap": {sym: _quote(
        name="测试", last=10.0, b1v=0.0, b1p=9.99, amt=1000.0,
        stamp="20261112092500", shared=shared)}}
    assert auc.unit_factor(f, sym) == expect


def test_stat_reports_both_tails():
    """判据取**下**分位（倍数小了才破 10% 线），所以 p05 必须在场；p95 同时留着做对照。"""
    s = auc._stat([1.0, 2.0, 3.0, 4.0, 10.0])
    assert s["p05"] < s["median"] < s["p95"]
    assert set(s) >= {"n", "median", "p05", "p10", "p90", "p95", "min", "max"}
