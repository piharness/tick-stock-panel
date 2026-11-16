"""竞价下单表的四条契约：定价源、缺失即跳过、回落可见、分母不编。

## 为什么要这一份

`analysis/auction_order.py` 从 `.workbuddy/tmp/auction_size.py` 搬出来之前，
它的三个守卫**没有一个能在测试里变红**：`prev_missing` 只在某天某只票真的
没前收时才出现，revert 探测只在有人改代码时才出现。而「临时验一次」的形态是
在 tmp 里手搓一个 `_check_lim_frame.py` —— 那份文件本身也在 `.workbuddy/` 里，
`.gitignore` 第一行就把它吃掉。于是「守卫会响」这件事，只活在某一次会话的记忆里。

每条规则下面配一个**故意做错的输入**：全绿的守卫不是守卫，是装饰。

## 与唯一定义点的绑定

挂单价不由这里断言公式，而是断言「`decide` 的结果 == `_limit_price(帧前收)`」。
把公式抄进测试就成了第三份拷贝 —— 而这份要防的恰恰是拷贝。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ao = _load("auction_order", "analysis/auction_order.py")

#: 帧里能拿到、但**不等于**负控（11-11 收盘 11.28 / 挂单价 11.16）的前收。
#: 用它当输入，才能把「读了帧」和「读了负控」区分开。
PREV_FRAME = 7.77
PREV_WITNESS = 11.28
LIMIT_WITNESS = 11.16

NOW = datetime(2026, 11, 13, 9, 20, 30)
STAMP = "20261113092030"


def _snap(**over) -> dict:
    row = {"last": 7.75, "prev": PREV_FRAME, "open": 0.0, "vol_hand": 12000.0,
           "amount_wan": 9300.0, "stamp": STAMP,
           "b1p": 7.75, "b1v": 500.0, "a1p": 7.75, "a1v": 500.0}
    row.update(over)
    return {"_pool": ["000530.SZ"], "000530.SZ": row}


def _witness() -> dict:
    return {"000530.SZ": {"prev_close": PREV_WITNESS, "limit": LIMIT_WITNESS}}


def _one(snap: dict, **kw) -> tuple[dict, dict]:
    rows, meta = ao.decide(snap, witness=kw.pop("witness", _witness()),
                           yday=kw.pop("yday", {"000530.SZ": 2.86e9}),
                           now=kw.pop("now", NOW))
    return rows[0], meta


# ------------------------------------------------------------- 定价源是帧 f[4]
def test_limit_comes_from_frame_not_from_witness():
    """前收取帧 f[4]，挂单价只由 `_limit_price` 决定。

    反例（原实现）：`lim = _limit_price(11.28)` ⇒ 11.16。用 7.77 的帧前收时，
    两者相差 0.53 元、约 69 个 tick —— 足以把「买不买得到」判反。
    """
    r, _ = _one(_snap())
    assert r["limit"] == ao._limit_price(PREV_FRAME)
    assert r["limit"] == 7.69, "7.77×0.99=7.6923，tick floor 应为 7.69"
    assert r["limit"] != LIMIT_WITNESS, "挂单价读到了负控，不是帧"
    assert r["prev_frame"] == PREV_FRAME


def test_lots_are_derived_from_the_frame_limit():
    """股数跟着帧挂单价走，不是跟着负控走 —— 这条才是「换了策略」那一句的复现。"""
    r, _ = _one(_snap())
    assert r["status"] == "OK"
    assert r["lots"] == int(ao.PER_NAME / (7.69 * ao.LOT))
    assert ao.PER_NAME / (7.69 * ao.LOT) - r["lots"] < 1.0


def test_witness_is_frozen_at_2026_11_11():
    """负控必须永不更新：它一旦被续命，revert 探测就再也响不了。"""
    import json

    doc = json.loads((REPO / "analysis" / "auction_order_witness_2026-11-11.json")
                     .read_text(encoding="utf-8"))
    assert doc["as_of"] == "2026-11-11"
    assert doc["n"] == len(doc["rows"]) == 20
    for row in doc["rows"]:
        assert row["limit"] == ao._limit_price(row["prev_close"]), (
            f"{row['symbol']} 的负控挂单价不是 _limit_price(负控前收)")


# --------------------------------------------------------- 缺失即跳过，不回落
def test_prev_missing_skips_and_never_falls_back():
    """帧里没有前收 ⇒ SKIP(prev_missing)，且 `limit` 必须为 None。

    反例：`prev = s.get("prev") or 负控`。它会给出一个**看起来合理**的 11.16，
    整张表是绿的，而那是 11-11 的价。
    """
    r, meta = _one(_snap(prev=None))
    assert r["status"] == "SKIP" and r["why"] == "prev_missing"
    assert r["limit"] is None, "回落到常量了"
    assert r["lots"] == 0
    assert meta["n_prev_missing"] == 1
    assert meta["n_prev_filled"] == 0


def test_prev_zero_is_missing_too():
    r, _ = _one(_snap(prev=0.0))
    assert r["why"] == "prev_missing" and r["limit"] is None


# ------------------------------------------------------------ 回落必须看得见
def test_fallback_detector_can_go_red():
    """「定价前收 ≠ 帧前收」是唯一不依赖计数器一致性的回落证据。

    为什么需要它：最常见的回退写法是 `prev = s.get("prev") or 常量`。
    一旦真回退，`prev` 就等于常量 ⇒ `n_prev_moved` 与 `n_limit_shifted`
    **同时**变 0，那两个观测量一起瞎掉。这条谓词看的是两个变量之间的不等式。
    """
    bad = [{"symbol": "X", "prev_frame": None, "prev_field": None,
            "prev_in_frame": False, "limit": 11.16}]
    assert ao.fallback_rows(bad) == ["X"], "回落形状没被点出来"
    good = [{"symbol": "X", "prev_frame": PREV_FRAME, "prev_field": PREV_FRAME,
             "prev_in_frame": True, "limit": 7.69}]
    assert ao.fallback_rows(good) == [], "正常行被误报"


def test_fallback_detector_catches_the_or_default():
    """`prev = s.get("prev") or 常量` 的具体形状：`prev_field` 有值，但帧里本来没有。

    这是 2026-11-12 用变异测试复现出来的：那一格 status=OK、limit=11.16、
    lots=896，四个计数器全 0，表头干净 —— 只看 `prev_frame` 为空的旧谓词一声不响。
    所以 `prev_in_frame` 必须**独立于 `prev_field`** 再算一遍。
    """
    or_default = [{"symbol": "X", "prev_frame": 11.28, "prev_field": 11.28,
                   "prev_in_frame": False, "limit": 11.16}]
    assert ao.fallback_rows(or_default) == ["X"], "or 兜底没被认出来"
    const_literal = [{"symbol": "Y", "prev_frame": 11.28, "prev_field": 6.0,
                      "prev_in_frame": True, "limit": 11.16}]
    assert ao.fallback_rows(const_literal) == ["Y"], "直接写死常量没被认出来"


def test_decide_keeps_the_frame_value_verbatim():
    """`decide` 必须原样保留帧给的值 —— 回落只会发生在「赋值」这一步。"""
    r, _ = _one(_snap())
    assert r["prev_field"] == PREV_FRAME == r["prev_frame"]
    assert r["prev_in_frame"] is True
    assert ao.fallback_rows([r]) == []
    r2, _ = _one(_snap(prev=None))
    assert r2["prev_in_frame"] is False


def test_revert_probe_trips_when_limit_ignores_the_frame(monkeypatch):
    """把定价换回常量 ⇒ prev 动了 N 只、限价一只没动。这是本文件唯一的 revert 观测量。

    反例走**同一个判据**：这里真的把 `_limit_price` 换成一个忽略入参的常量函数，
    探测必须从「安静」变「响」。
    """
    _, meta_ok = _one(_snap())
    assert meta_ok["n_prev_moved"] == 1 and meta_ok["n_limit_shifted"] == 1

    monkeypatch.setattr(ao, "_limit_price", lambda prev: LIMIT_WITNESS)
    _, meta_bad = _one(_snap())
    assert meta_bad["n_prev_moved"] == 1
    assert meta_bad["n_limit_shifted"] == 0, "限价跟着常量走了，探测却没看见"
    assert ao._header(meta_bad, {"final": True, "prev_trade_day": "2026-11-12"})[2].startswith(
        "!! prev 动了 1 只但限价一只没动")


# ------------------------------------------------------- 分母：不编、不错位
def test_guard2_uses_yuan_and_catches_yday_residual():
    """竞价额与昨全天额同量级 ⇒ 这一格是昨日残留。

    反例：分母用「万元」而分子用「元」⇒ 比值大 1e4 倍，全票 SKIP。
    """
    r, _ = _one(_snap(amount_wan=2.86e5), yday={"000530.SZ": 2.86e8})  # 28.6 亿 / 28.6 亿
    assert r["status"] == "SKIP" and r["why"] == "looks_like_yday_total"


def test_guard2_lets_normal_auction_through():
    r, _ = _one(_snap(amount_wan=9300.0), yday={"000530.SZ": 2.86e8})  # 0.32%
    assert r["status"] == "OK" and r["guard2"] == "ok"


def test_guard2_without_denominator_says_so_and_does_not_invent_one():
    """取不到定版的昨日成交额时，guard2 是**未生效**，不是「通过」。

    反例：退回到 11-11 那三个常量（或退回 p25 经验值），表会照常绿，
    而「拿昨全天当竞价量」这件事就再也识别不出来。
    """
    r, meta = _one(_snap(), yday={})
    assert r["status"] == "OK"
    assert r["guard2"] == "no_denominator"
    assert meta["n_guard2_no_denominator"] == 1
    head = ao._header(meta, {"final": False, "reason": "quote_ts=11:30:05 < 定版线"})
    assert "guard2 分母=不可用" in head[1]
    assert any("guard2 对它们未生效" in line for line in head)


def test_yday_amount_refuses_intraday_bar(monkeypatch):
    """日线那一格是盘中快照 ⇒ 一个数都不交（而不是交一个偏小的）。"""
    class _Burst:
        DAILY_DIR = None

        @staticmethod
        def prev_amount_from_daily(today):
            return {"final": False, "wan": {"000530.SZ": 1.0},
                    "reason": "quote_ts=11:30:05 < 定版线 15:30 ⇒ 盘中快照"}

    monkeypatch.setattr(ao, "_sibling", lambda name: _Burst)
    yuan, info = ao.yday_amount_yuan(NOW.date())
    assert yuan == {}, "判成快照却仍然交出了分母"
    assert info["final"] is False


def test_stale_stamp_is_skipped_before_the_ratio_guard():
    """时间戳不是今天 ⇒ SKIP，且病因写 stale_snapshot，不写 looks_like_yday_total。

    反例：先跑比值守卫，昨日残留的快照会以「量级可疑」的名义被跳过，
    把「源还没出今日数据」伪装成「数据尺寸不对」。
    """
    r, _ = _one(_snap(stamp="20261112161415", amount_wan=2.86e5))
    assert r["status"] == "SKIP"
    assert r["why"].startswith("stale_snapshot")


# ------------------------------------------------- 票池的名单也是每日变量
class _Burst:
    """`auction_burst` 的最小替身：只要分区枚举 + 定版判据。"""

    CLOSE_CUTOFF = dt_time(15, 30)

    def __init__(self, final: bool):
        self._final = final

    def _partition_dates(self):
        return [(date(2026, 11, 12), Path("."))], "daily"

    def _bar_is_final(self, qts):
        return self._final


def _patch_cohort(monkeypatch, *, final: bool = True):
    monkeypatch.setattr(ao, "_sibling", lambda name: _Burst(final=final))
    monkeypatch.setattr(ao, "_enriched_quote_ts", lambda day, data_dir=None: 41405000)
    monkeypatch.setattr(ao, "cohort_doc", lambda as_of, data_dir=None: {
        "candidates": [{"symbol": "000530.SZ", "prev_close_raw": 11.28,
                        "limit_price": 11.16}],
        "warnings": ["次日分区尚未落盘"]})


def test_pool_is_resolved_from_the_previous_trading_day(monkeypatch):
    """票池由 `candidates(前一交易日)` 现算，并自带同格收盘价当对照。

    反例（原实现）：读 `auction_pool_cohort_2026-11-12.json`。那份是 **11-12 入场**
    的 cohort，与 11-13 入场的只重合 16/20 —— 4 只出、4 只进，而表是绿的。
    """
    _patch_cohort(monkeypatch)
    syms, meta, refs = ao.resolve_pool(date(2026, 11, 13))
    assert syms == ["000530.SZ"]
    assert meta["mode"] == "today"
    assert meta["as_of"] == "2026-11-12", "票池的排序日不是前一交易日"
    assert meta["entry"] == "2026-11-13"
    assert refs["000530.SZ"] == {"prev_close": 11.28, "limit": 11.16}
    assert meta["warnings"] == ["次日分区尚未落盘"]


def test_pool_refuses_an_intraday_partition(monkeypatch):
    """前一交易日是盘中快照 ⇒ exit 4，**不退回冻结名单**。

    拿 11:30 的 close 排序，选出来的 cohort 连身份都是错的；这时候「用昨天的名单」
    看上去更保险，实际是把「排序错」换成「名单过期」，两种都是安静地错。
    """
    _patch_cohort(monkeypatch, final=False)
    with pytest.raises(SystemExit) as exc:
        ao.resolve_pool(date(2026, 11, 13))
    assert exc.value.code == 4


def test_frozen_pool_needs_rehearsal():
    """冻结名单在非演练模式下直接用 = exit 4，且这一步不该触网。"""
    assert ao.main(["run", "--pool", "frozen"]) == 4
    assert ao.main(["check", "--pool", "frozen"]) == 4


def test_check_says_unjudgeable_when_a_pool_name_has_no_reference(monkeypatch):
    """池子里有新票、对照表里没有它 ⇒ exit 6（判不了），不是崩掉、也不是 0。

    反例（原实现）：`math.floor(PER_NAME / (lim_c * LOT))`，`lim_c=None` ⇒ TypeError。
    而触发条件恰恰是「明天做对了」—— 换池之后负控必然对不上 4 只。
    """
    monkeypatch.setattr(ao, "resolve_pool", lambda *a, **k: (
        ["000530.SZ"],
        {"mode": "today", "as_of": "2026-11-12", "entry": "2026-11-13",
         "n": 1, "covered": 0, "warnings": []},
        {}))
    monkeypatch.setattr(ao, "fetch", lambda syms, timeout=8.0: {
        "000530.SZ": {"prev": PREV_FRAME, "last": 7.75, "stamp": STAMP,
                      "vol_hand": 1.0, "amount_wan": 1.0}})
    assert ao.cmd_check() == 6


def test_header_flags_frame_versus_daily_divergence():
    """帧前收 ≠ 日线收盘且限价随之变 ⇒ 必须出声。

    正常早晨这一列是 0（帧 f[4] 就是前一交易日收盘）。非零时若按旧逻辑只看
    「prev 动了但限价没动」，常量回退的**另一种写法**（`prev_field` 一起被写成常量）
    会让两个计数器同时变 1，表头一声不响 —— 那是本文件最贵的一种安静。
    """
    _, meta = _one(_snap())
    assert meta["n_prev_moved"] == 1 and meta["n_limit_shifted"] == 1
    lines = ao._header(meta, {"final": True, "prev_trade_day": "2026-11-12"})
    assert any("帧前收 ≠ 日线收盘" in line for line in lines)


# ------------------------------------------------- 「定版」不等于「是前一格」
def test_pool_refuses_a_stale_day_even_when_the_bar_is_final(monkeypatch):
    """前一交易日整格缺失 ⇒ `_prev_trade_day` 退回再前一天，而定版判据照样为真。

    反例（原实现）：`resolve_pool(2026-11-13)` 在只有 11-11 分区时原样返回
    11-12 那 20 只 —— mode=today、entry=2026-11-13、exit 0、零告警，正是本文件
    声称堵死的「用昨天的名单」。判据不新增数据源：`candidates()` 自己声明 `entry_date`。
    """
    monkeypatch.setattr(ao, "_sibling", lambda name: _Burst(final=True))
    monkeypatch.setattr(ao, "_enriched_quote_ts", lambda day, data_dir=None: 41405000)
    monkeypatch.setattr(ao, "cohort_doc", lambda as_of, data_dir=None: {
        "candidates": [{"symbol": "000530.SZ", "prev_close_raw": 11.28,
                        "limit_price": 11.16}],
        # as_of=11-11 的名单，下一格是 11-12：它买的是 11-12，不是今天
        "entry_date": "2026-11-12",
        "warnings": []})
    with pytest.raises(SystemExit) as exc:
        ao.resolve_pool(date(2026, 11, 13))
    assert exc.value.code == 4


def test_pool_accepts_entry_date_none_but_still_needs_the_frame_to_agree(monkeypatch):
    """`entry_date=None` 合法（次日分区还没写），所以闸门要把「两条线同时缺一天」交给帧。

    这一格是 `run` 的第三条轴：帧前收与名单日线收盘**逐票全不等** ⇒ 两张表说的不是
    同一天。少数几只不等才是除权（只动那几只），由表头逐票报，不拦表。
    """
    def _pool(*a, **k):
        return (["000530.SZ", "000678.SZ", "920895.BJ"],
                {"mode": "today", "as_of": "2026-11-12", "entry": "2026-11-13",
                 "n": 3, "covered": 3, "warnings": []},
                {"000530.SZ": {"prev_close": 11.28, "limit": 11.16},
                 "000678.SZ": {"prev_close": 9.09, "limit": 9.00},
                 "920895.BJ": {"prev_close": 17.50, "limit": 17.32}})

    def _snap_all(syms, timeout=8.0):
        # 故意少一只（北交所前缀错时就是这样）：分母若取 n_rows，判据会永远差一只
        return {s: {"prev": PREV_FRAME, "last": 7.75, "stamp": STAMP,
                    "vol_hand": 12000.0, "amount_wan": 9300.0, "open": 0.0}
                for s in syms if s != "920895.BJ"}

    monkeypatch.setattr(ao, "resolve_pool", _pool)
    monkeypatch.setattr(ao, "fetch", _snap_all)
    monkeypatch.setattr(ao, "yday_amount_yuan",
                        lambda today, daily_dir=None: ({}, {"final": True,
                                                            "prev_trade_day": "2026-11-12"}))
    assert ao.cmd_run(save=False) == 4, "逐票全不等必须是 exit 4，不是表头一行字"


def test_qq_symbol_binds_to_the_collector_map():
    """交易所前缀只许有一份映射：`auction_burst._qq` 三地都对，这里曾是第二份且少 `BJ`。

    北交所票在面板里出现过（近 41 个 as_of 有 15 天含 920 系）。`sz920895` 上游返回
    `v_pv_none_match` ⇒ `no_snapshot`(SKIP)、表绿、exit 0、少买 100 万。
    """
    burst = _load("auction_burst", "analysis/auction_burst.py")
    for sym in ("600000.SH", "000530.SZ", "920895.BJ", "688286.SH"):
        assert ao.qq_symbol(sym) == burst._qq(sym), sym


# ------------------------------- 第二轴必须在 `check` 里，因为明早第一步是 check
def _stub_pool(monkeypatch, refs: dict, snappy: dict):
    monkeypatch.setattr(ao, "resolve_pool", lambda *a, **k: (
        list(refs),
        {"mode": "today", "as_of": "2026-11-12", "entry": "2026-11-13",
         "n": len(refs), "covered": len(refs), "warnings": []},
        refs))
    monkeypatch.setattr(ao, "fetch", lambda syms, timeout=8.0: {
        s: snappy[s] for s in syms if s in snappy})


def test_check_refuses_a_table_whose_names_do_not_belong_to_the_frame(monkeypatch):
    """`check` 是明早第一道命令，所以它必须和 `run` 用同一条第二轴。

    反例（原实现）：第二轴只写在 `cmd_run` 里。20/20 全不等时 `check` 打印
    「分叉 20/20」然后 **return 0** —— 而人看的是退出码，不是那行字。
    """
    names = [f"60{i:04d}.SH" for i in range(4)]
    refs = {s: {"prev_close": 10.0 + i, "limit": 9.9 + i} for i, s in enumerate(names)}
    snap = {s: {"prev": 99.0 + i, "vol_hand": 1.0, "amount_wan": 1.0,
                "last": 9.0, "open": 9.0, "stamp": STAMP} for i, s in enumerate(names)}
    _stub_pool(monkeypatch, refs, snap)
    assert ao.cmd_check() == 4, "全票不等必须是 exit 4，不是表上一行字"


def test_check_counts_only_comparable_names(monkeypatch):
    """分母是「两列都有值」的只数，不是池子大小。

    反例（原实现）：分母取 `len(syms)`。少一只快照（北交所前缀错 / 停牌 / 上游漏票）
    ⇒ 2≠3 ⇒ 「全不等」永远差一只，判据在最需要它的那个早晨恰好不响。
    """
    names = ["60%04d.SH" % i for i in range(3)]
    refs = {names[0]: {"prev_close": 10.0, "limit": 9.9},
            names[1]: {"prev_close": 11.0, "limit": 10.89},
            names[2]: {"prev_close": 12.0, "limit": 11.88}}
    snap = {names[0]: {"prev": 99.0, "vol_hand": 1.0, "amount_wan": 1.0,
                       "last": 9.0, "open": 9.0, "stamp": STAMP},
            names[1]: {"prev": 98.0, "vol_hand": 1.0, "amount_wan": 1.0,
                       "last": 9.0, "open": 9.0, "stamp": STAMP},
            # 第三只上游没给 ⇒ 不可比，但它不该把「2/2 全不等」稀释成「2/3」
            names[2]: {"vol_hand": 1.0, "amount_wan": 1.0, "last": 9.0,
                       "open": 9.0, "stamp": STAMP}}
    _stub_pool(monkeypatch, refs, snap)
    assert ao.cmd_check() == 4


def test_pool_refuses_a_missing_enriched_partition(monkeypatch, tmp_path):
    """`_bar_is_final(None) is True` 回答的是「全空 quote_ts 算定版」，不是「分区存在」。

    反例（原实现）：`_enriched_quote_ts()` 在分区整格缺失时也返回 None ⇒ 缺失被读成
    定版 ⇒ 放行。这一层连「kline_daily 有没有这一格」都不需要，是 P0 之前的一层。
    """
    monkeypatch.setattr(ao, "_sibling", lambda name: _Burst(final=True))
    monkeypatch.setattr(ao, "cohort_doc", lambda as_of, data_dir=None: {
        "candidates": [{"symbol": "000530.SZ", "prev_close_raw": 11.28,
                        "limit_price": 11.16}],
        "entry_date": "2026-11-13", "warnings": []})
    with pytest.raises(SystemExit) as exc:
        ao.resolve_pool(date(2026, 11, 13), "today", tmp_path)
    assert exc.value.code == 4


def test_calendar_proves_adjacency_or_refuses_to_guess(monkeypatch):
    """「相邻」由绝对锚证明；证明不了就拒出表 —— 不留「不可判别但放行」的中间态。

    `entry_date=None` 同时兼容「今天分区还没落」（正常早晨，实测 11-12 的分区
    13:34 才建）与「前一交易日整格缺失」（P0），它在入参层面不可判别，所以
    2026-11-13 之前只能举一面旗。现在这面旗换成常量日历的读数：
    能证相邻就写 `✓相邻`，不能证（超出覆盖区间）就必须 exit 4。
    """
    _patch_cohort(monkeypatch)
    _, meta, _ = ao.resolve_pool(date(2026, 11, 13))
    # `axis` 是产物的自证：这个前格是**谁**算的。2026 年内恒为 `constant`；
    # 越过常量右端才是 `live` —— 两者的失败模式不同（会到期 / 会停更），
    # 所以它必须落盘，而不是留给读的人回忆今天常量还在不在覆盖区间内。
    assert meta["calendar"] == {"expected_prev": "2026-11-12", "gap": [],
                                "covered": "2025-05-07~2026-12-31",
                                "axis": "constant"}
    # 「今天分区未落」仍然记账，但它不再是「判不了」的同义词
    assert meta["today_partition_absent"] is True

    class _Uncovered:
        """日历没覆盖今天 ⇒ 判不了 ⇒ 不许放行。"""

        COVERED = (date(2025, 5, 7), date(2026, 12, 31))

        def gap_days(self, prev, today, live=None):
            return None

    monkeypatch.setattr(ao, "CAL", _Uncovered())
    with pytest.raises(SystemExit) as exc:
        ao.resolve_pool(date(2026, 11, 13))
    assert exc.value.code == 4, "未知不是通过：日历没覆盖这一天就必须拒出表"


# ------------------------------------------------ 共因格：两条线同时落后一天
def test_frame_axis_is_silent_when_both_sources_lag_the_same_day():
    """相对判据的结构性盲区：帧前收与名单日线收盘**同源落后一天**时差值为 0。

    这不是实现缺陷，是相对判据的定义决定的 —— 它比的是「两张表说的是不是同一
    天」，当两张表一起退回 11-11，它只会说「是的，同一天」。所以这一格只能由
    绝对锚（交易日历）来守，不能靠再加一条相对比较。
    """
    # 3 只可比、0 只移动 = 帧与名单逐票相等 = 11-13 早晨用的却是 11-11 的两份表
    assert ao.pool_frame_mismatch({"n_prev_comparable": 3, "n_prev_moved": 0},
                                  {"mode": "today"}) is None


def test_calendar_gate_refuses_a_co_caused_two_day_lag(monkeypatch):
    """本仓唯一能守住共因格的轴：**内置交易日历**。

    构造 11-11 的真实形态（名单 as_of=T-2，帧 f[4] 也是 T-2 收盘，两条线同源）：
    名单声明的入场日是 `None`（T-1 整格缺 ⇒ `as_of` 就是面板最后一格），帧那侧
    逐票相等 ⇒ `pool_frame_mismatch` 静默 ⇒ 旧实现 exit 0，买的是前天的 20 只。

    反例（原实现）：这一格全绿。`_prev_trade_day` 取「最后一格 < today」，
    前一交易日整格缺失时安静退回再前一天；`_bar_is_final` 照样为真。
    """
    _patch_cohort(monkeypatch)
    # T-1=11-12 整格没落：`_prev_trade_day(11-13)` 返回 11-11
    monkeypatch.setattr(ao, "_prev_trade_day", lambda today: "2026-11-11")

    # 变异对照：把日历换成「永远说没有缺口」的替身 = 旧实现
    class _NeverGap:
        COVERED = (date(2025, 5, 7), date(2026, 12, 31))

        def gap_days(self, prev, today, live=None):
            return []

        def prev_market_day(self, today, live=None):
            return date(2026, 11, 12)

    monkeypatch.setattr(ao, "CAL", _NeverGap())
    syms, meta, refs = ao.resolve_pool(date(2026, 11, 13))
    assert syms == ["000530.SZ"] and meta["as_of"] == "2026-11-11"
    assert meta["today_partition_absent"] is True, (
        "旧实现下这一格唯一的痕迹就是「entry_date=None」，而它不拦表")

    # 真日历：11-12 是交易日却没落盘 ⇒ 拒出表
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("trading_calendar",
                                       REPO / "analysis" / "trading_calendar.py")
    real = _iu.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(ao, "CAL", real)
    with pytest.raises(SystemExit) as exc:
        ao.resolve_pool(date(2026, 11, 13))
    assert exc.value.code == 4, "名单排序日不是今天的前一格 ⇒ 必须 exit 4"


def test_calendar_gate_does_not_mistake_a_holiday_for_a_gap():
    """负控：休市不是缺口。把节假日当缺口 ⇒ 每年国庆/春节后第一周都拒出表。

    2026-02-24（周二）的前一格是 02-13（周五），中间 02-16..02-23 全休市；
    2026-10-09（周五）的前一格是 10-08（周四），而 10-01..10-07 休市、10-08 开市。
    """
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("trading_calendar",
                                       REPO / "analysis" / "trading_calendar.py")
    cal = _iu.module_from_spec(spec)
    spec.loader.exec_module(cal)
    assert cal.gap_days("2026-02-13", date(2026, 2, 24)) == []
    assert cal.gap_days("2026-10-08", date(2026, 10, 9)) == []
    # 反过来：10-08 开市却没落盘 ⇒ 10-09 早晨必须看见这个缺口
    assert cal.gap_days("2026-09-30", date(2026, 10, 9)) == [date(2026, 10, 8)]
    # 周日不是交易日 ⇒ 前格即上一交易日
    assert cal.gap_days("2026-11-13", date(2026, 11, 15)) == []


def test_calendar_audit_flags_a_wrong_constant_in_both_directions():
    """故意错的常量：多算一天、少算一天，`audit()` 两个方向都要报出来。

    这是 `audit()` 的负控。没有它，`audit()` 返回两个空列表就永远是「对上了」——
    一个只会说「没问题」的对账函数，与没有对账函数是同一件事。

    输入是**合成**的：三个连续工作日，断言三种常量口径各自的读数。
    """
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("trading_calendar",
                                       REPO / "analysis" / "trading_calendar.py")
    cal = _iu.module_from_spec(spec)
    spec.loader.exec_module(cal)

    #: 2026-11-09/10/11 都是交易日，常量对它们没有声明 ⇒ 一致。
    parts = ["2026-11-09", "2026-11-10", "2026-11-11"]
    assert cal.audit(parts) == {"extra": [], "missing": []}

    #: 少一格。必须显式给 `hi`：`audit` 只在 `[lo, hi]` 内对账，**右端缺格
    #: 归停摆闹钟管**（这里看不见，不重复造一个半吊子判据）。
    got = cal.audit(parts[:2], hi=date(2026, 11, 11))
    assert got["missing"] == [date(2026, 11, 11)], got
    assert got["extra"] == []
    assert cal.audit(parts[:2]) == {"extra": [], "missing": []}, (
        "不给 hi 时不该凭空报出范围外的缺格")

    #: 反方向：把一个有分区的工作日声明成休市 ⇒ `extra`。
    real = cal.HOLIDAYS
    try:
        cal.HOLIDAYS = real | {date(2026, 11, 9)}
        got = cal.audit(parts)
        assert got["extra"] == [date(2026, 11, 9)], got
        assert got["missing"] == [], got
    finally:
        cal.HOLIDAYS = real

    #: 空输入不许炸，也不许返回「一致」以外的任何东西。
    assert cal.audit([]) == {"extra": [], "missing": []}


def _panel_days(base: Path) -> list[date]:
    """`data/kline_daily` 真实覆盖的交易日。

    kline_daily 自 2026-11 起按月分区, 目录名 `date=2026-11` 只给月份,
    `date.fromisoformat` 会直接抛 ValueError —— 真实交易日在数据的 date 列里。
    两种布局都要认, 否则这条"跟着面板长"的审计在切布局当天就断。
    """
    from app.tickflow.partitions import partition_dates

    out: set[date] = set()
    for p in base.glob("date=*"):
        token = p.name.split("=", 1)[1]
        if len(token) == 10:
            out.add(date.fromisoformat(token))
        else:
            out |= partition_dates(p)
    return sorted(out)


def test_calendar_constant_keeps_auditing_partitions_after_the_frozen_anchor():
    """冻结那条只证到 2026-11-12，之后新增的分区必须继续被审。

    动机：`COVERED` 右端 2026-12-31 到期后要补次年的法定节假日，而**补进去的
    那天当天没有任何分区可对照**。上面那条等式的上界写死在 2026-11-12，于是
    「补错一天」可以一路绿到那天真的到来 —— 到那时错的是闹钟的「应有」，
    它会在本该休市的日子判「缺一格」。

    所以这条不冻结数字、不设上界，只跟着面板长。今天新分区还没落盘 ⇒ 显式
    skip（junit 里是 skip 不是 pass），而不是假绿。
    """
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("trading_calendar",
                                       REPO / "analysis" / "trading_calendar.py")
    cal = _iu.module_from_spec(spec)
    spec.loader.exec_module(cal)
    base = REPO / "data" / "kline_daily"
    if not base.is_dir():
        pytest.skip("data/kline_daily 不在：判据由合成输入覆盖")
    have = _panel_days(base)
    anchor = date(2026, 11, 12)   # 冻结那条的最后一天
    fresh = [d for d in have if d > anchor]
    if not fresh:
        assert have[-1] <= anchor, "末分区越过了锚点却没进审计范围"
        pytest.skip(f"锚点之后还没有新分区（末格 {have[-1]}）：本条从第一个新分区起生效")
    lo = fresh[0]
    assert lo == anchor + timedelta(days=1) or cal.gap_days(anchor, lo) == [], (
        f"锚点与 {lo} 之间缺交易日却没被这条看见：{cal.gap_days(anchor, lo)}")
    got = cal.audit(have, lo=lo, hi=have[-1])
    assert not got["extra"], f"常量说休市但面板有分区：{got['extra']}"
    assert not got["missing"], f"常量说交易日但面板没有分区：{got['missing']}"


def test_calendar_constant_explains_every_panel_working_day():
    """自证：常量必须把面板 371 个分区解释干净（0 个多余、0 个遗漏）。

    这一条是日历能被当成锚、而不是当成注释的前提。面板区间内每个工作日，
    「有没有分区」与「常量说是不是交易日」必须逐日相等；差一天就说明要么常量
    写错了日期，要么那一天是真的缺 —— 两种都必须人看。
    """
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("trading_calendar",
                                       REPO / "analysis" / "trading_calendar.py")
    cal = _iu.module_from_spec(spec)
    spec.loader.exec_module(cal)
    have = set(_panel_days(REPO / "data" / "kline_daily"))
    lo, hi = date(2025, 5, 7), date(2026, 11, 12)
    diff_have, diff_miss = [], []
    n_weekday = 0
    d = lo
    while d <= hi:
        if d.weekday() < 5:
            n_weekday += 1
            if (d in have) != cal.is_market_day(d):
                (diff_have if d in have else diff_miss).append(d)
        d += timedelta(days=1)
    assert not diff_have, f"常量说是休市但面板有分区: {diff_have}"
    assert not diff_miss, f"常量说是交易日但面板没有分区: {diff_miss}"
    # 三个数一起钉：工作日 / 有分区 / 休市日。只钉其中一个，剩下两个可以互换而等式仍成立
    # （把一天从常量里删掉、同时它也缺分区 —— 这正是要防的那种「自证」）。
    assert (n_weekday, sum(1 for d in have if lo <= d <= hi)) == (397, 371)
    assert sum(1 for d in cal.HOLIDAYS if lo <= d <= hi) == 26


# ------------------------ 补丁 5：常量到期之后，这条轴靠实探接班（仍不放手）
#
# 上面那条等式证明的是「常量在它的覆盖区间内是权威」，证明不了「区间之后怎么办」。
# `COVERED` 止于 2026-12-31，此后每一天 `gap_days` 都返回 `None` ⇒ 09:20 的下单闸门
# 每天 `exit 4`。那是**下单路径**：不是某张诊断表，是人手里那张要下单的表。
#
# 接法是接实探轴，但只在常量覆盖之外才取（09:20 不该为一个 2027 年才用得到的轴
# 多绑一次外部服务的可用性）。于是这条分支在 2026 年不会被自动跑到 —— 下面这组
# 用例就是它的**演练**：把常量右端提前到昨天，用今天这份已知答案的数据跑 2027 的路。

def _cal_with_hi(hi: date):
    """真日历、只把覆盖右端提前到 `hi`。**不是新实现** —— 同一个模块换一个右端。

    `is_market_day` / `gap_days` / `expiry` 都把 `COVERED` 当全局读，换一个值就是
    换了一段证据边界，算子一个字节没动。用它的好处是：这条 `live` 分支不必等到
    2027 年才第一次被真的跑到，也不必在测试里自己造一份日历实现（那会变成第二份
    定义点，而测试要防的恰恰是拷贝）。
    """
    mod = _load("trading_calendar_trunc", "analysis/trading_calendar.py")
    mod.COVERED = (mod.COVERED[0], hi)
    return mod


class _Probe:
    """`calendar_probe` 的替身：只回答 `probe()`，不联网。"""

    def __init__(self, days, note="243 个交易日"):
        self._days, self._note = days, note

    def probe(self):
        return self._days, self._note


def _live_from(cal, lo: date, hi: date) -> set:
    """按 `cal` 的常量造一份实探集合（区间内）。形态与真端点一致：滚动、右端=今天。"""
    out, d = set(), lo
    while d <= hi:
        if cal.is_market_day(d):
            out.add(d)
        d += timedelta(days=1)
    return out


def test_beyond_the_constant_the_gate_hands_over_to_the_live_axis(monkeypatch):
    """2026-12-31 之后每个早晨要走的那条路：判定改由实探承担，且产物自证用了谁。

    输入形态是真端点的形状（滚动一年窗、右端=今天），答案已知（前格 11-12）。
    变异：`gap_days(prev, today)` / `prev_market_day(today)` 少传 `live` ⇒ 这里
    仍是 `None` ⇒ exit 4，测试红。
    """
    trunc = _cal_with_hi(date(2026, 11, 12))
    _patch_cohort(monkeypatch)
    monkeypatch.setattr(ao, "CAL", trunc)
    monkeypatch.setattr(ao, "_probe_module", lambda: _Probe(
        _live_from(trunc, date(2025, 11, 13), date(2026, 11, 13))))
    syms, meta, _ = ao.resolve_pool(date(2026, 11, 13))
    assert syms == ["000530.SZ"]
    assert meta["calendar"]["axis"] == "live", "越过常量右端却仍写着 axis=constant"
    assert meta["calendar"]["expected_prev"] == "2026-11-12"
    assert "（实探）" in ao.calendar_line(meta), (
        "读数里不写「谁算的」，读者就分不出这个前格是常量还是实探给的，"
        "而两者的失败模式不同（会到期 / 会停更）")
    assert trunc.COVERED[1] == date(2026, 11, 12)


def test_inside_the_constant_the_gate_never_touches_the_network(monkeypatch):
    """常量覆盖内**不发请求** —— 09:20 的闸门不该多绑一次外部服务的可用性。

    变异：`_live_calendar` 去掉 `today <= hi` 的早退 ⇒ 下面这个 `_boom` 立刻被调用，
    于是每个早晨的下单表都挂在一个跟它无关的网络上。
    """
    _patch_cohort(monkeypatch)

    def _boom():
        raise AssertionError("常量覆盖内取了实探：09:20 这条路上不该有网络")

    monkeypatch.setattr(ao, "_probe_module", _boom)
    syms, meta, _ = ao.resolve_pool(date(2026, 11, 13))
    assert syms == ["000530.SZ"]
    assert meta["calendar"]["axis"] == "constant"
    live, info = ao._live_calendar(date(2026, 11, 13))
    assert live is None and info["used"] is False


#: 四种「实探接不上」的病因。处置各不相同 —— 所以不能缩成同一句话。
#: 值是这一支必须说出的字；`never` 是全组都不许出现的字（那是常量分支的建议）。
_REFUSAL_NEEDLE = {
    # 没凭证 / 请求失败：先去配 key 或查网络，改常量是错的
    "no_key": "补常量在这里没用",
    # 两份权威打架：先去对账，谁也不许顺着自己结论挑
    "disagree": "分歧",
    # 上游停更：表右端往回缩 ⇒ prev_market_day 会安静返回更早的一天
    "stalled": "停更",
    # 体检通过但 today 仍在表外：先把右端追平
    "beyond_live_right": "仍在表外",
}


@pytest.mark.parametrize("case", sorted(_REFUSAL_NEEDLE))
def test_every_reason_the_live_axis_cannot_carry_is_named(monkeypatch, capsys, case):
    """接不上时仍 exit 4，但**病因必须分开写**，且不许给「补常量」这个错建议。

    把四种缩成一句「补日历」，就是让操作的人在 09:15 去改一份没有证据的常量表：
    2027 的休市日还没有任何来源，写进去就是把猜测当地址 —— 错误形态是「一年后某个
    本该休市的日子被判成缺一格」，而那时没人记得它是猜的。

    这一条同时是 `_refusal()` 四个分支的负控：任何一支退化成通用消息，`needle` 就没了。
    """
    needle = _REFUSAL_NEEDLE[case]
    trunc = _cal_with_hi(date(2026, 11, 12))
    base = _live_from(trunc, date(2025, 11, 13), date(2026, 11, 13))
    live = {
        "no_key": (None, "缺少 fuyao_api_key"),
        "disagree": (base | {date(2026, 11, 7)}, "243 个交易日"),      # 11-07 是周六
        "stalled": (_live_from(trunc, date(2025, 9, 1), date(2026, 11, 1)),
                    "243 个交易日"),
        "beyond_live_right": (_live_from(trunc, date(2025, 11, 14), date(2026, 11, 8)),
                              "243 个交易日"),
    }[case]
    _patch_cohort(monkeypatch)
    monkeypatch.setattr(ao, "CAL", trunc)
    monkeypatch.setattr(ao, "_probe_module", lambda: _Probe(*live))

    with pytest.raises(SystemExit) as exc:
        ao.resolve_pool(date(2026, 11, 13))
    assert exc.value.code == 4, "接不上必须是 exit 4，不许静默退回周末近似"
    err = capsys.readouterr().err
    assert needle in err, f"{case}: 病因没写出来 —— {err}"
    assert "补 analysis/trading_calendar.py 的休市日再跑" not in err, (
        f"{case}: 给的是「补常量」这个错建议 —— 常量到 {trunc.COVERED[1]} 就结束了，"
        f"再往后写进去的是猜测")


def test_expected_prev_comes_from_the_axis_that_can_answer(monkeypatch, tmp_path):
    """产物里的 `expected_prev` 必须由**能回答这一天**的那条轴算，不是由常量硬答。

    用 2027-01-04（周一）问：前格是 2026-12-31，因为 **2027-01-01 元旦休市**。
    而常量到 2026-12-31 就结束了，`HOLIDAYS` 里没有任何 2027 的字。
    若 `prev_market_day` 少传实探，兜底会把 01-01（周五）答成交易日 ——
    于是产物声明的前格是个**节日**，而 `gap` 是空的：这张表看上去完全正常，
    它的绝对锚却是错的。

    为什么不用 2026-09-25（中秋）当反例：那条兜底并不是「周末以外都算」，
    它还查 `HOLIDAYS`，而 2026 的假期常量是知道的 —— 用它做用例会绿，
    绿的原因却与实探无关（我第一版就是这么写的，变异 M18 存活暴露了这一点）。

    差异不在布尔上、也不在退出码上，只在**这一格日期**上。所以断言日期相等。
    """
    real = _load("trading_calendar_real", "analysis/trading_calendar.py")
    trunc = _cal_with_hi(date(2026, 12, 31))
    # 实探：真常量造的整年窗 + 元旦休市 + 右端落在今天（2027-01-04）
    live = _live_from(real, date(2025, 12, 31), date(2026, 12, 31)) | {date(2027, 1, 4)}
    _patch_cohort(monkeypatch)
    monkeypatch.setattr(ao, "CAL", trunc)
    monkeypatch.setattr(ao, "_prev_trade_day", lambda today: "2026-12-31")
    monkeypatch.setattr(ao, "_probe_module", lambda: _Probe(live))
    (tmp_path / "kline_daily_enriched" / "date=2026-12-31").mkdir(parents=True)

    _, meta, _ = ao.resolve_pool(date(2027, 1, 4), "today", tmp_path)
    assert meta["calendar"]["expected_prev"] == "2026-12-31", (
        "前格被表外的周末近似答成了 2027-01-01（元旦，休市）")
    assert meta["calendar"]["axis"] == "live"
    assert date(2027, 1, 1) not in live and date(2027, 1, 1).weekday() == 4, (
        "反例的前提是「元旦是周五」：表外近似会把它当交易日")


def test_one_definition_of_usable_binds_the_alarm_and_the_gate():
    """「接得上」只有一个定义点：闹钟存下的读数与闸门的判定必须同源。

    两边各写一遍「体检通过 且 零分歧」，同一份实探就会被一边判成接得上、一边判成
    判不了 —— 而人能分辨这两行字的窗口只有一个 09:20。变异：`_carries()` 改回
    自己算那个合取 ⇒ 本用例在 `stalled`/`disagree` 两格上分叉。
    """
    alarm = _load("panel_stall_alarm", "scripts/panel_stall_alarm.py")
    cal = ao.CAL
    today = date(2026, 11, 13)
    base = _live_from(cal, date(2025, 11, 13), date(2026, 11, 13))
    cases = {
        "healthy": base,
        "stalled": _live_from(cal, date(2025, 9, 1), date(2026, 11, 1)),
        "short": _live_from(cal, date(2026, 9, 1), date(2026, 11, 13)),
        "disagree": base | {date(2026, 11, 7)},
        "empty": set(),
    }
    for name, live in cases.items():
        usable, _ = cal.usable(live, today)
        rep = alarm.check([date(2026, 11, 12)], today, 0, cal, live)
        assert rep["live"]["usable"] is (usable is not None), (
            f"{name}: 闹钟与闸门对同一份实探给出了不同结论")
    assert cal.usable(cases["empty"], today)[0] is None, "空集不是「可用」，是与「没有实探」同形"

