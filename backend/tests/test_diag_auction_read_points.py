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


# ---------------------------------------------------------------- 守卫必须能红
# 全绿的守卫如果永远不可能红，就不是守卫，是装饰。下面每条都造一个**故意错的输入**。

def test_guard_prev_amt_stale_when_burst_starts_after_open():
    """burst 从 09:10 之后起 ⇒ 首帧 stamp 已是当日 ⇒ `A` 从「昨成交额」变成
    「今日成交额」，κ 与 mult 全错。这条今天就踩得到，不是假想缺口。"""
    sym = "000001.SZ"
    frames = [
        _frame("09:15:00", {sym: _quote(name="测试", last=10.0, b1v=100.0, b1p=10.0,
                                        amt=5000.0, stamp="20261112091500", shared=False)}),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.99,
                                        amt=1000.0, stamp="20261112092500", shared=False)}),
    ]
    _, meta = auc.analyze_day(frames)
    assert meta["veto"]["prev_amt_stale"], "首帧 stamp 已是当日，A 已错，却没红"
    g = auc.summarize_guards([meta], frames)
    assert any("prev_amt_stale" in f for f in g["failures"])


def test_guard_stale_obs_when_read_point_falls_back_to_prior_day():
    """昨日 16:14 的收盘快照混在帧里：`b1p==a1p` 且 `b1v==a1v>0`，帧级撮合率 100%
    —— 长得完全像竞价帧。现行射程下 `frame_at` 按 `ts` 选帧，`offset_s` 会到分钟级
    （09:20:00 读点 − 09:06:43 帧 = 797 秒），所以**对齐守卫本来就是第二道防线**。
    这道 `stamp[:8]==交易日` 是纵深：它挡的是「offset 小但内容时间是昨天」，
    也就是采集时钟与内容时钟不一致，而对齐守卫只比两者之差、不比绝对日期。
    """
    sym = "000001.SZ"
    frames = [
        _frame("09:06:43", {sym: _quote(name="测试", last=10.0, b1v=500.0, b1p=10.0,
                                        amt=200000.0, stamp="20261111161415", shared=False)}),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.99,
                                        amt=1000.0, stamp="20261112092500", shared=False)}),
    ]
    _, meta = auc.analyze_day(frames)
    pm = meta["probes"]["09:20:00"]
    # 对齐守卫先挡住：off=797s 远超容差 ⇒ 不是可行动读点（这与「陈旧帧 offset=0」的
    # 说法不同，后者在现行 PROBES 下取不到）
    assert pm["offset_s"] > auc.PROBE_TOL_S and pm["actionable"] is False
    assert meta["veto"]["stale_obs"], "陈旧帧进了读点却没红"
    assert meta["veto"]["stale_obs"][0]["stamp"].startswith("20261111")
    g = auc.summarize_guards([meta], frames)
    assert any("stale_obs" in f for f in g["failures"])


def test_guard_zero_obs_after_0920():
    """09:20 之后 `b1v=0` 说明该票不在撮合里。静默丢票会让日聚合偏向活跃票。"""
    sym = "000001.SZ"
    frames = [
        _frame("09:10:00", _pre(sym)),
        _frame("09:22:00", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=0.0,
                                        amt=0.0, stamp="20261112092158", shared=False)}),
        _frame("09:25:01", {sym: _quote(name="测试", last=10.0, b1v=0.0, b1p=9.99,
                                        amt=1000.0, stamp="20261112092500", shared=False)}),
    ]
    _, meta = auc.analyze_day(frames)
    assert any(z["probe"] == "09:22:00" for z in meta["veto"]["zero_obs"])
    assert any("zero_obs" in f for f in auc.summarize_guards([meta], frames)["failures"])


def test_guard_frame_match_rate_rejects_non_matching_frame():
    """帧级撮合率 < 0.9 ⇒ 该帧不是撮合帧。用「一票撮合、一票不撮合」造，
    两票的 b1v 都 > 0，所以不会同时触发 zero_obs —— 两个判据各管一件事。"""
    a, b = "000001.SZ", "000002.SZ"
    ok = _quote(name="A", last=10.0, b1v=100.0, b1p=10.0, amt=0.0,
                stamp="20261112092158", shared=False)
    bad = _quote(name="B", last=10.0, b1v=50.0, b1p=9.90, amt=0.0,
                 stamp="20261112092158", shared=False)
    bad["a1p"] = 10.05      # 有价差 ⇒ 不在撮合模式
    bad["a1v"] = 30.0
    frames = [
        _frame("09:10:00", {**_pre(a), **_pre(b)}),
        _frame("09:22:00", {a: ok, b: bad}),
        _frame("09:25:01", {
            a: _quote(name="A", last=10.0, b1v=0.0, b1p=9.99, amt=1000.0,
                      stamp="20261112092500", shared=False),
            b: _quote(name="B", last=10.0, b1v=0.0, b1p=9.99, amt=800.0,
                      stamp="20261112092500", shared=False),
        }),
    ]
    _, meta = auc.analyze_day(frames)
    assert meta["probes"]["09:22:00"]["frame_match_rate"] == pytest.approx(0.5)
    # 09:20:00 读点会回落到 09:10 的 `_pre` 帧（b1v=0），那一条归 zero_obs 管；
    # 这里只要求 09:22:00 不触发 zero_obs —— 两个判据各管一件事。
    assert not [z for z in meta["veto"]["zero_obs"] if z["probe"] == "09:22:00"]
    assert any("frame_match_rate" in f
               for f in auc.summarize_guards([meta], frames)["failures"])


def test_p05_lower_bound_is_unidentifiable_at_n20_but_not_at_n200():
    """N=20 只能给 p05 的**上界**，给不出下界：`P(Bin(20,.05) >= 1) = 0.64 < 0.975`。
    于是「p05 ≥ 阈值」这个方向在 N=20 上不可证伪 —— 判据方向只有一侧能立。"""
    small = [1.0 + 0.1 * i for i in range(20)]
    ci = auc._order_stat_ci(small, 0.05)
    assert ci["lower"] is None and ci["lower_rank"] is None
    assert ci["upper"] is not None
    assert ci["two_sided_identifiable"] is False

    # n=100 时秩=1 ⇒「下界」退化成最小值，仍不算识别出来；n=200 才真的到秩 2
    mid = auc._order_stat_ci([1.0 + 0.01 * i for i in range(100)], 0.05)
    assert mid["lower_rank"] == 1 and mid["lower"] == 1.0
    assert mid["two_sided_identifiable"] is False

    # n=100 时秩=1 ⇒「下界」退化成最小值，仍不算识别出来；n=200 才到秩 4
    mid = auc._order_stat_ci([1.0 + 0.01 * i for i in range(100)], 0.05)
    assert mid["lower_rank"] == 1 and mid["lower"] == 1.0
    assert mid["two_sided_identifiable"] is False

    # 秩取「满足 P(Bin>=r) >= 0.975 的最大 r」⇒ 下界尽量高但仍然保守。
    # n=200, q=.05: P(Bin>=4)=0.987, P(Bin>=5)=0.965 ⇒ 秩 4。
    big = auc._order_stat_ci([1.0 + 0.01 * i for i in range(200)], 0.05)
    assert big["lower_rank"] == 4 and big["lower"] > 1.0
    assert big["two_sided_identifiable"] is True


def test_frame_interval_is_measured_not_declared():
    """「5 秒一帧」这种声明必须由实测覆盖：`--interval 2` 实际跑出 2 与 3 秒混用。"""
    frames = [
        _frame("09:19:00", _pre("000001.SZ")),
        _frame("09:19:02", _pre("000001.SZ")),
        _frame("09:19:05", _pre("000001.SZ")),
    ]
    st = auc.frame_interval_stats(frames)
    assert st["n_gaps"] == 2
    assert st["p50"] == 2.5 and st["max"] == 3
    assert st["hist"] == {"2": 1, "3": 1}


def test_archive_keeps_five_levels_and_open(tmp_path):
    """归档是证据本体：缺 `open` 就没有「竞价价 vs 开盘价」实验，缺四五档就没有横截面深度。"""
    import polars as pl

    sym = "600000.SH"
    q = _quote(name="测试", last=10.0, b1v=100.0, b1p=10.0, amt=0.0,
               stamp="20261112092158", shared=False)
    q.update({"b2p": 9.99, "b2v": 12.0, "b3p": 9.98, "b3v": 34.0,
              "b4p": 9.97, "b4v": 56.0, "b5p": 9.96, "b5v": 78.0,
              "a2p": 10.01, "a2v": 11.0, "a3p": 10.02, "a3v": 22.0,
              "a4p": 10.03, "a4v": 33.0, "a5p": 10.04, "a5v": 44.0})
    frames = [_frame(t, {sym: q}) for t in ("09:21:58", "09:22:00", "09:22:02")]
    p = auc.archive(frames, tmp_path, "t")
    df = pl.read_parquet(p)
    for c in ("open", "b2p", "b5v", "a2p", "a5v"):
        assert c in df.columns, f"归档缺 {c}"
    assert df.filter(pl.col("b3v") == 34.0).height == 3
