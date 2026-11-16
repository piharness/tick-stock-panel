"""停摆闹钟的判据契约：右端不动才红，其余一律不许红。

## 为什么这一份必须存在

`scripts/panel_stall_alarm.py` 的判据有三种死法，且全部表现为「绿」：

  1. **绑 `today` 而不是 `prev_market_day(today)`** —— 每个交易日早晨必红，
     于是没人再看它（11-12 的分区实测 13:34 才建）。
  2. **把 `today` 自己算进 lag** —— 与 (1) 同病，只是方向相反：把「今天下午
     才该出现的分区」当成「今天早上就该在」。
  3. **超出日历覆盖时返回绿** —— `trading_calendar.COVERED` 止于 2026-12-31，
     跨年后 `prev_market_day` 会在表外用「周末以外都是交易日」自信作答。

所以每条规则下面配一个**故意做错的输入**，并断言退出码，而不只是断言 `status`：
一个只断言布尔值的测试，抓不住「红了但红错了因」。

## 不测什么

不测 `render()` 的排版，也不测 `--json` 的键序。判据与退出码是契约，排版不是。
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


alarm = _load("panel_stall_alarm", "scripts/panel_stall_alarm.py")
cal = _load("trading_calendar", "analysis/trading_calendar.py")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认切断实探轴：判据测试不该联网，也不该被客户端超时拖慢。

    `main()` 默认 `--live` 开，若不切断，跑一次测试就是一次真实 HTTP 请求 ——
    既是外部依赖（网络断了测试就红，且红得与判据无关），也是 20 秒级的墙钟。
    实探轴本身由下面第 7 组用**注入的集合**测，不靠网络。

    切在 `_probe()` 而不是 `_live_sessions()`：前者是真正的网络缝，后者只是转发。
    切在转发层的话，`_live_sessions()` 就永远测不到它自己（本文件最后一条要测的
    正是「转发不许改写结果」）—— 切在缝上，转发才可测。
    """
    class _NoProbe:
        def probe(self):
            return None, "测试不联网"

    monkeypatch.setattr(alarm, "_probe", lambda: _NoProbe())


def _live_from_constant(lo: date, hi: date) -> set:
    """按常量造一份「与常量完全一致」的实探集合（区间内）。"""
    out, d = set(), lo
    while d <= hi:
        if cal.is_market_day(d):
            out.add(d)
        d += timedelta(days=1)
    return out


def _weekdays(lo: date, hi: date) -> set:
    out, d = set(), lo
    while d <= hi:
        if d.weekday() < 5:
            out.add(d)
        d += timedelta(days=1)
    return out


def _code(rep, dirty=()) -> int:
    return alarm.verdict(rep, dirty)


# --- 1. 今天不红：上一格在就算够新 -------------------------------------------

def test_today_itself_does_not_count_as_a_gap():
    """`max(have) == prev_market_day(today)` 是**绿**，且 `missing` 必须为空。

    这是「盘中不误报」的唯一支点。若实现把 `today` 算进 lag，2026-11-13 早晨
    （分区 13:34 才建）这条会红，而它每天都会红一次 —— 一年 240 次假闹钟。
    """
    rep = alarm.check([date(2026, 11, 12)], date(2026, 11, 13), 0, cal)
    assert rep["status"] == alarm.OK
    assert rep["lag"] == 0 and rep["missing"] == []
    assert rep["expect"] == "2026-11-12", "expect 回答的是「上一格」，不是 today"


def test_a_long_holiday_is_not_a_stall():
    """国庆后第一个交易日，右端停在 09-30 ⇒ 绿。休市不是缺口。

    负控：右端再退一格（09-29）⇒ 09-30 是开市的，必须红且只报这一天。
    """
    rep = alarm.check([date(2026, 9, 30)], date(2026, 10, 8), 0, cal)
    assert rep["status"] == alarm.OK and rep["lag"] == 0

    bad = alarm.check([date(2026, 9, 29)], date(2026, 10, 8), 0, cal)
    assert bad["status"] == alarm.STALLED
    assert bad["missing"] == ["2026-09-30"], "国庆 7 天不能被算成 7 个缺口"
    assert _code(bad) == 1


def test_weekend_does_not_add_to_the_lag():
    """周五的右端 + 周一的基准日 ⇒ 0，不是 3（自然日跨度）。"""
    rep = alarm.check([date(2026, 11, 13)], date(2026, 11, 16), 0, cal)
    assert rep["status"] == alarm.OK and rep["lag"] == 0


# --- 2. 停摆必须红，且数的是交易日 -------------------------------------------

def test_dropping_the_last_three_partitions_turns_red():
    """删掉末 3 格（11-10/11/12）后，`today=11-12` 必须红。

    lag 是 2 而不是 3：`today` 自己不在 (max_have, today) 开区间里。少报一格是
    定义决定的，不是漏洞 —— 这个闹钟问的是「上一格在不在」，不是「今天有没有」。
    """
    have = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
    rep = alarm.check(have, date(2026, 11, 12), 0, cal)
    assert rep["status"] == alarm.STALLED
    assert rep["missing"] == ["2026-11-10", "2026-11-11"]
    assert rep["lag"] == 2 and _code(rep) == 1
    assert rep["max_have"] == "2026-11-09" and rep["expect"] == "2026-11-11"


def test_lag_counts_market_days_not_calendar_days():
    """11-06（周五）→ 11-11（周三）：日历 5 天，交易日 2 个（11-09、11-10）。

    用日历天会报 5，把「跨了个周末」写成「停了整整一周」—— 那正是
    `_diag_pool_freshness` 里 `gap_days == 1` 那条口径要防的同一个错。
    """
    rep = alarm.check([date(2026, 11, 6)], date(2026, 11, 11), 0, cal)
    assert rep["lag"] == 2, f"got {rep['lag']}"
    assert rep["missing"] == ["2026-11-09", "2026-11-10"]


# --- 3. 容忍度：绿也必须报出落后量 -------------------------------------------

def test_tolerance_green_still_reports_the_lag():
    """`max_lag=2` 时同一输入是绿，但 `lag` 必须仍是 2 且 `missing` 非空。

    只打印「✓」会把 `0/0`（没落后）与 `2/3`（落后但放过）合成一种。绿要携带
    它为什么绿 —— 这是「绿但无感知比红更贵」的唯一解药。

    负控：`max_lag=1` ⇒ 同一输入必须红。证明容忍度真的在起作用，不是装饰。
    """
    have = [date(2026, 11, 6)]
    green = alarm.check(have, date(2026, 11, 11), 2, cal)
    assert green["status"] == alarm.OK and _code(green) == 0
    assert green["lag"] == 2 and green["missing"] == ["2026-11-09", "2026-11-10"]
    assert "落后 2 个交易日" in green["detail"]

    red = alarm.check(have, date(2026, 11, 11), 1, cal)
    assert red["status"] == alarm.STALLED and _code(red) == 1


def test_max_lag_beyond_cap_is_refused():
    """容忍 > 5 个交易日 = 「一周不更新也算正常」= 取消闹钟 ⇒ 直接拒绝（exit 2）。"""
    assert alarm.main(["--max-lag", "9", "--today", "2026-11-12"]) == 2
    assert alarm.main(["--max-lag", "-1"]) == 2


# --- 4. 判不了必须是非零，不许放行 -------------------------------------------

def test_outside_calendar_coverage_is_unjudgeable_not_green():
    """2027-01-05 在 `COVERED` 之外 ⇒ `unjudgeable` 且退出码 3（非零）。

    `prev_market_day(2027-01-05)` 会自信地回答 2027-01-04 —— 那是把「不知道」
    写成「没问题」。这里唯一的正确行为是拒绝给答案，让 CI 红。
    """
    rep = alarm.check([date(2026, 12, 31)], date(2027, 1, 5), 0, cal)
    assert rep["status"] == alarm.UNJUDGEABLE
    assert rep["lag"] is None and rep["expect"] is None
    assert _code(rep) == 3


def test_empty_panel_is_red_not_green():
    """一个分区都没有不是「落后 0 天」⇒ 红，且 detail 要点出「可能是路径错」。"""
    rep = alarm.check([], date(2026, 11, 12), 0, cal)
    assert rep["status"] == alarm.STALLED and _code(rep) == 1
    assert "一个 date= 分区都没有" in rep["detail"]


# --- 5. 分区名的解析是严格的 ---------------------------------------------------

def test_dirty_partition_name_is_red_and_nonzero_padded_is_not_a_partition(tmp_path):
    """`date=2026-1-1` 进 dirty（红）；`date=20261112` 根本不算分区。

    第二条防的是 Python 3.11 起 `date.fromisoformat("20261112")` 成功 ——
    若直接拿它解析，`date=20261112` 会与 `date=2026-11-12` 落在同一个集合里，
    两个名字一个日期，`max(have)` 谁都对不上谁。
    """
    base = tmp_path / "data" / "kline_daily"
    for name in ("date=2026-11-11", "date=2026-11-12", "date=2026-1-1",
                 "date=20261109", ".tmp"):
        (base / name).mkdir(parents=True)

    have, dirty = alarm.sessions(tmp_path / "data")
    assert [d.isoformat() for d in have] == ["2026-11-11", "2026-11-12"]
    #: `20261109` 也不算分区 —— 它在 3.11 的 `fromisoformat` 里**能**解析成功，
    #: 所以只靠解析不靠正则的话，它会混进 `have` 与 `date=2026-11-12` 抢 `max()`。
    #: 两个坏名字都要报出来，不是静默跳过：面板里有解析不了的分区名 ⇒ 红。
    assert dirty == ["date=2026-1-1", "date=20261109"]

    rep = alarm.check(have, date(2026, 11, 13), 0, cal)
    assert _code(rep, dirty) == 1, "面板里出现解析不了的分区名 ⇒ 不许绿"


def test_missing_table_is_exit_2_not_a_stall(tmp_path):
    """表目录不存在是**路径错**，不是面板停摆 —— 两者的处置完全不同。"""
    assert alarm.main(["--data-dir", str(tmp_path), "--today", "2026-11-12"]) == 2


# --- 6. 真实树与回放 -----------------------------------------------------------

def test_real_panel_at_today_is_green_and_three_days_later_is_red():
    """真实树：今天绿，基准日推到 3 个交易日之后必红。

    这一条是「今天 → 必绿」的验收判据。它读盘，所以 `data/` 不在时跳过 ——
    跳过是显式的，不是静默绿（`pytest.skip` 在 junit 里是 skip 不是 pass）。
    """
    if not (REPO / "data" / "kline_daily").is_dir():
        pytest.skip("data/kline_daily 不在（干净克隆/CI 无数据）：判据由合成输入覆盖")
    have, dirty = alarm.sessions(REPO / "data")
    assert not dirty, f"真实面板里有解析不了的分区名：{dirty[:3]}"
    assert have, "真实面板一个分区都没有"

    today = date.today()
    rep = alarm.check(have, today, 0, cal)
    if rep["status"] == alarm.UNJUDGEABLE:
        pytest.skip(f"today={today} 超出日历覆盖区间 {cal.COVERED}（跨年需补表）")
    assert rep["status"] == alarm.OK, rep["detail"]

    #: 推到 have 右端之后的第 3 个交易日（跳过周末/节假日），必须红。
    d = have[-1]
    for _ in range(3):
        d += timedelta(days=1)
        while not cal.is_market_day(d):
            d += timedelta(days=1)
    later = alarm.check(have, d, 0, cal)
    assert later["status"] == alarm.STALLED, (
        f"基准日 {d} 时右端仍是 {have[-1]}，必须红：{later['detail']}")
    assert later["lag"] == 2, (
        "推到第 3 个交易日只报 2 个缺口：today 自己不在 (max_have, today) 开区间里")


# --- 7. 三条变异存活的支路（补于 11-12 变异复核）--------------------------------
#
# 前 12 条在下面三处置错时**全绿**，所以它们守不住这三处。每条都对应一次实测
# 存活的变异（脚本 `.workbuddy/tmp/mutate_panel_alarm2.py` 的 M6/M10/M11）。

def test_injected_have_need_not_be_sorted():
    """`have` 由调用方注入，契约里没有「必须先排序」—— `check` 必须自己防御。

    变异 M10：`days = sorted(set(have))` → `days = list(have)`。
    `sessions()` 返回的已是升序，所以真实路径永远看不出差别；只有乱序注入才暴露：
    `days[-1]` 会取到 `11-09` 而不是 `11-12`，一个**没停摆**的面板被判 `stalled`。
    这正是「纯函数可注入」的代价 —— 注入口比内部调用点宽。
    """
    shuffled = [date(2026, 11, 6), date(2026, 11, 12), date(2026, 11, 9)]
    rep = alarm.check(shuffled, date(2026, 11, 13), 0, cal)
    assert rep["max_have"] == "2026-11-12", "末格必须取最大值，不是最后一个元素"
    assert rep["status"] == alarm.OK and _code(rep) == 0

    #: 重复日期同理：`max()` 不受影响，但 `n_sessions` 会被虚增 —— 那是要报出去的读数。
    dup = [date(2026, 11, 12), date(2026, 11, 12), date(2026, 11, 11)]
    rep2 = alarm.check(dup, date(2026, 11, 13), 0, cal)
    assert rep2["n_sessions"] == 2, f"分区数被重复计了：{rep2['n_sessions']}"
    assert rep2["status"] == alarm.OK


def test_last_covered_day_is_still_judgeable():
    """`COVERED` 右端 `2026-12-31`（周四，交易日）当天必须**判得了**。

    变异 M6：`today > hi` → `today >= hi`。改错后跨年前最后一个交易日就 exit 3，
    而其余 12 条全绿 —— 没有任何一条踩在这个日期上。边界日期在测试里必须出现，
    否则「两端都查」只写对了一半。
    """
    rep = alarm.check([date(2026, 12, 30)], date(2026, 12, 31), 0, cal)
    assert rep["status"] == alarm.OK, rep["detail"]
    assert rep["expect"] == "2026-12-30" and rep["lag"] == 0

    #: 负控：往后一天才该判不了。证明这条守的是**边界位置**，不是「右端永远绿」。
    after = alarm.check([date(2026, 12, 30)], date(2027, 1, 1), 0, cal)
    assert after["status"] == alarm.UNJUDGEABLE and _code(after) == 3


def test_max_lag_cap_boundary_is_exact(tmp_path):
    """上限 5 必须放行、6 必须拒绝 —— 只测 9 和 -1 守不住这个边界。

    变异 M11：`<= MAX_TOLERABLE_LAG` → `<= MAX_TOLERABLE_LAG + 1`。
    改错后 6 被接受，而 9 仍被拒，原测试照样绿。这里用 tmp 造最小表而不碰真实树：
    否则这条在无 `data/` 的干净克隆上会因路径错返回 2，把「边界」测成「路径」。
    """
    (tmp_path / "data" / "kline_daily" / "date=2026-11-12").mkdir(parents=True)
    d = ["--data-dir", str(tmp_path / "data"), "--today", "2026-11-13"]
    assert alarm.main(d + ["--max-lag", "5"]) == 0, "5 在上限内，必须放行"
    assert alarm.main(d + ["--max-lag", "6"]) == 2, "6 超越上限，必须拒绝"


# --- 8. 通知：红而没送到 ≠ 红，且「没配」≠「配了但坏了」 ----------------------
#
# 这两件事必须占两个退出码。合成一个 `1`，cron 只看退出码时就分不出
# 「面板停了」和「闹钟哑了」—— 后者会让前者一起静默。

class _StubHook:
    """假的 `webhook_adapter`：只记调用，返回预设成败。"""

    def __init__(self, ok: bool):
        self.ok = ok
        self.calls: list[tuple[str, str, str]] = []

    def send_custom(self, url, title, body, event_type, data=None, secret="", max_attempts=1):
        self.calls.append((url, title, body))
        return self.ok

    def send_wecom_markdown(self, url, title, body_md):
        self.calls.append((url, title, body_md))
        return self.ok

    def send_feishu(self, url, title, body, secret="", max_attempts=1):
        self.calls.append((url, title, body))
        return self.ok


class _StubPrefs:
    def get_feishu_webhook_secret(self) -> str:
        return ""


def _stalled_tree(tmp_path):
    """一个右端停在 11-10、基准日 11-13 的最小表 —— 判据必红。"""
    (tmp_path / "data" / "kline_daily" / "date=2026-11-10").mkdir(parents=True)
    return ["--data-dir", str(tmp_path / "data"), "--today", "2026-11-13"]


def test_red_without_any_channel_exits_2(monkeypatch, tmp_path):
    """要求推送但一个通道都没配 ⇒ `2`（配置问题，一次性），不是 `1`（面板停了）。

    若把「没配」映射成 `1`，一个还没配 Webhook 的部署每天收到的是一条「面板停摆」，
    而真相是「闹钟还没接线」—— 两种病因同形，人会先怀疑数据。
    """
    monkeypatch.setattr(alarm, "channels", lambda: [])
    assert alarm.main(_stalled_tree(tmp_path)) == 2


def test_red_with_broken_channel_exits_4_not_1(monkeypatch, tmp_path):
    """配了通道但适配器返回 False ⇒ `4`。这是「闹钟哑了」，与「面板停了」分开。

    变异验证：把 `UNSENT → 4` 改成 `UNSENT → 1`，这条必须红。
    """
    hook = _StubHook(ok=False)
    monkeypatch.setattr(alarm, "channels", lambda: [("custom", "http://127.0.0.1:9/hook")])
    monkeypatch.setattr(alarm, "_backend_modules", lambda: (_StubPrefs(), hook))
    assert alarm.main(_stalled_tree(tmp_path)) == 4
    assert hook.calls, "应当至少尝试过一次发送"


def test_red_with_working_channel_exits_1(monkeypatch, tmp_path):
    """通道正常 ⇒ `1`，且真的把「缺哪几天」发出去了，不是一个光秃秃的「停了」。"""
    hook = _StubHook(ok=True)
    monkeypatch.setattr(alarm, "channels", lambda: [("custom", "http://127.0.0.1:9/hook")])
    monkeypatch.setattr(alarm, "_backend_modules", lambda: (_StubPrefs(), hook))
    assert alarm.main(_stalled_tree(tmp_path)) == 1
    assert len(hook.calls) == 1, "应当恰好推一条，不重不漏"
    url, title, body = hook.calls[0]
    assert "2 个交易日" in title, f"标题要带落后量，通知栏只看得到它：{title}"
    assert "2026-11-11" in body and "2026-11-12" in body, (
        f"正文要列出缺哪几天，否则收件人还得自己推日历：{body!r}")


def test_green_never_touches_the_channel(monkeypatch, tmp_path):
    """绿的时候不许发消息 —— 否则「每天一条正常」会和「停摆」抢注意力。

    这条用 `channels()` 是否被调用来判定：判据绿时连通道都不该去探测。
    """
    touched: list[int] = []
    monkeypatch.setattr(alarm, "channels", lambda: touched.append(1) or [("custom", "u")])
    (tmp_path / "data" / "kline_daily" / "date=2026-11-12").mkdir(parents=True)
    code = alarm.main(["--data-dir", str(tmp_path / "data"), "--today", "2026-11-13"])
    assert code == 0
    assert not touched, "判据为绿时不应探测通知通道"


# --- 9. 日历余额：`5` 是「没停，但快瞎了」 -------------------------------------
#
# `COVERED` 右端是日程表上的悬崖，不是随机故障。三个坑：
#   * 只打印天数不升级 ⇒ 绿的输出没人读，等于没有；
#   * 用 `5` 盖过 `1` ⇒ 真实停摆被读成「只是日历快过期」；
#   * 把自然日当交易日 ⇒ 2026-11-12 的 49 天里只有 35 个交易日能判。

def test_expiry_tiers_hit_both_boundaries():
    """`covered/warn/blind` 的分界必须精确在 30 与 0 上，两侧都踩。

    变异：`days_left <= 30` → `< 30`，则第 30 天仍是绿的，且没有任何其他测试
    踩在这个日期上；`today > hi` → `>=` 则最后一个交易日就报失明。
    """
    hi = date(2026, 12, 31)
    cases = [
        (hi - timedelta(days=31), "covered"),   # 2026-11-30
        (hi - timedelta(days=30), "warn"),      # 2026-12-01
        (hi - timedelta(days=1), "warn"),
        (hi, "warn"),                           # 右端当天仍判得了，但已是最后一天
        (hi + timedelta(days=1), "blind"),
    ]
    for d, want in cases:
        got = cal.expiry(d)
        assert got["state"] == want, f"{d} 应为 {want}，实得 {got['state']}"
    assert cal.expiry(hi)["days_left"] == 0
    assert cal.expiry(hi + timedelta(days=4))["days_left"] == -4, "失明后要报出已过多久"


def test_expiry_reports_sessions_not_calendar_days():
    """2026-11-12 距右端 **49 自然日 / 35 个交易日**，两个数必须都在且不同。

    变异：`sessions_left` 返回 `(hi - today).days` ⇒ 49。差 14 天不是排版问题：
    那 14 天是周末，面板本来就不该长，报进「还剩几天」等于虚报缓冲。
    """
    e = cal.expiry(date(2026, 11, 12))
    assert (e["days_left"], e["sessions_left"]) == (49, 35), e
    assert e["state"] == "covered" and e["hi"] == "2026-12-31"
    assert e["warn_days"] == 30


def test_warn_window_exits_5_not_0_and_not_1(tmp_path):
    """面板健康 + 落在 warn 窗 ⇒ `5`。既不是 0（会静默过期），也不是 1（会误导成停摆）。

    同一棵树、只换基准日：`2026-11-30`（距右端 31 天）判 0，`2026-12-15`（16 天）
    判 5。分区落在基准日之后是允许的（回放时必然出现，上界归 calendar 等式管），
    所以这里不需要造假表就能把基准日跨过 12-01。
    """
    (tmp_path / "data" / "kline_daily" / "date=2026-12-14").mkdir(parents=True)
    base = ["--data-dir", str(tmp_path / "data")]
    assert alarm.main(base + ["--today", "2026-11-30"]) == 0
    assert alarm.main(base + ["--today", "2026-12-15"]) == alarm.EXPIRING
    #: `--no-notify` 不影响它：`5` 不是红，不走推送分支。
    assert alarm.main(base + ["--today", "2026-12-15", "--no-notify"]) == alarm.EXPIRING


def test_stall_outranks_expiring(tmp_path):
    """warn 窗里真的停摆 ⇒ 必须是 `1`，不是 `5`。

    变异：`verdict()` 里把 warn 分支挪到 `STALLED` 之后仍绿（顺序错），或者让
    `5` 覆盖 `1` ⇒ 一次真实停摆被读成「只是日历快过期」，人去改常量表，
    而管道还躺着。这是本组里代价最高的一条。
    """
    (tmp_path / "data" / "kline_daily" / "date=2026-11-10").mkdir(parents=True)
    code = alarm.main(["--data-dir", str(tmp_path / "data"),
                       "--today", "2026-12-15", "--no-notify"])
    assert code == 1, f"停摆必须报 1，实得 {code}"


def test_blind_outranks_expiring(tmp_path):
    """越过右端 ⇒ `3`（判不了），不再造第六个码。"""
    (tmp_path / "data" / "kline_daily" / "date=2026-12-30").mkdir(parents=True)
    code = alarm.main(["--data-dir", str(tmp_path / "data"),
                       "--today", "2027-01-04", "--no-notify"])
    assert code == 3, f"越过 COVERED 必须 3，实得 {code}"


def test_calendar_block_is_present_even_when_unjudgeable():
    """判不了的那一天最需要余额读数，所以 `calendar` 不许只在绿分支里写。

    变异：把 `out["calendar"] = ...` 挪到 `days[-1] >= expect` 那一支之后 ——
    跨年后 `--json` 里连「还剩几天」都没有，只剩一句「判不了」。
    """
    rep = alarm.check([date(2026, 12, 30)], date(2027, 1, 4), 0, cal)
    assert rep["status"] == alarm.UNJUDGEABLE
    assert rep["calendar"]["state"] == "blind"
    assert rep["calendar"]["sessions_left"] == 0


# --- 7. 实探轴：常量到期后还能不能判 -----------------------------------------
#
# 2026-11-12 实测 fuyao `/calendar/trading-days` 固定返回 [当日-365 天, 当日]，
# 无入参、无未来表。所以「2027 年的表」拿不到，也不必要：本脚本的判据只问过去。
# 下面每一组都配一个「故意做错」的对照，否则「接得上」与「接不上」在绿字里同形。


def test_constant_stays_authoritative_inside_its_range():
    """重叠区内常量优先 —— 它是被面板 371 个分区证过的离线权威，实探只是审计。

    变异：把 `is_market_day` 的优先级改成「实探优先」⇒ 这条红。
    那一改的后果不是算术错，是把一年的分区级证据降级成一次网络请求。
    """
    d = date(2026, 6, 19)  # 端午，休市
    assert cal.is_market_day(d) is False
    assert cal.is_market_day(d, {d}) is False


def test_outside_its_range_the_constant_falls_back_to_the_weekday_guess():
    """常量覆盖外没有实探时，只能退回「周几近似」—— 这正是必须被挡住的兜底。

    2027-01-01 是元旦。没有实探时它被当成交易日（错），所以那一天绝不能让
    `prev_market_day` 单独作答；有实探就必须答否。
    """
    d = date(2027, 1, 1)
    assert cal.is_market_day(d) is True, "无实探 ⇒ 周几近似（这是兜底，不是答案）"
    live = {date(2026, 12, 31), date(2027, 1, 4)}
    assert cal.is_market_day(d, live) is False


def test_prev_market_day_crosses_the_constant_edge_only_with_live():
    """跨过 2026-12-31 后，`prev_market_day` 的答案取决于有没有实探。

    没有实探时它会自信地答 2027-01-01（元旦，其实休市）—— 错误方向是**多算一格**，
    于是 `expect` 变成一个不存在的交易日，面板永远"落后"，红得没有病因。
    """
    live = {date(2026, 12, 31), date(2027, 1, 4)}
    assert cal.prev_market_day(date(2027, 1, 4), live) == date(2026, 12, 31)
    assert cal.prev_market_day(date(2027, 1, 4)) == date(2027, 1, 1), \
        "无实探时的这个答案是错的 —— 它把元旦当成了交易日"


def test_live_carries_judgement_across_the_constant_expiry():
    """头号场景：2027-01-04 早上，常量早过期了，实探接上 ⇒ 仍然 0。

    这就是「悬崖」被消掉的证据。负控是同一天同一份面板、没有实探 ⇒ 3。
    """
    today = date(2027, 1, 4)
    live = _live_from_constant(date(2026, 1, 4), date(2026, 12, 31)) | {today}
    have = sorted(_live_from_constant(date(2026, 12, 1), date(2026, 12, 31)))

    rep = alarm.check(have, today, 0, cal, live)
    assert rep["status"] == alarm.OK, rep["detail"]
    assert rep["expect"] == "2026-12-31"
    assert alarm.verdict(rep) == 0

    assert alarm.verdict(alarm.check(have, today, 0, cal, None)) == 3, \
        "同一天没有实探轴必须判不了 —— 否则这条测试证明不了实探有用"


def test_stall_still_outranks_a_carrying_live():
    """实探接得上也不许盖过停摆：面板真停了就是 1。

    变异：让 `5`/`0` 的分支排在 `STALLED` 之前 ⇒ 这条红。
    """
    today = date(2027, 1, 4)
    live = _live_from_constant(date(2026, 1, 4), date(2026, 12, 31)) | {today}
    have = sorted(_live_from_constant(date(2026, 12, 1), date(2026, 12, 29)))

    rep = alarm.check(have, today, 0, cal, live)
    assert rep["status"] == alarm.STALLED, rep["detail"]
    assert rep["lag"] == 2, f"12-30、12-31 两个交易日缺失，实得 {rep['lag']}"
    assert alarm.verdict(rep) == 1


def test_healthy_live_suppresses_the_expiry_alarm():
    """实探接得上时，常量余额告警必须**不响**。

    否则 `5` 会从 2026-12-01 起每天响一次，而每一天其实都照常可判 ——
    永久噪音的结局是被关掉。负控：同一份余额、没有实探 ⇒ 必须 5。
    """
    today = date(2026, 12, 15)
    live = _live_from_constant(date(2025, 12, 15), date(2026, 12, 15))
    have = sorted(_live_from_constant(date(2026, 12, 1), date(2026, 12, 14)))

    rep = alarm.check(have, today, 0, cal, live)
    assert rep["calendar"]["state"] == "warn", "这一天的常量余额在 30 天内，前提得真成立"
    assert alarm.verdict(rep) == 0

    assert alarm.verdict(alarm.check(have, today, 0, cal, None)) == alarm.EXPIRING


def test_stale_live_cannot_carry_even_though_it_agrees_with_the_constant():
    """上游停更 15 天：与常量零分歧，但右端没贴着今天 ⇒ 不许接替。

    这是实探轴唯一的**新增**风险：表停在几天前时 `prev_market_day` 会安静返回
    几天前的日子，`expect` 变松 ⇒ 面板停了几天也判绿。因此体检只看右端不够，
    必须同时卡窗口长度与右端新鲜度。
    """
    today = date(2026, 12, 15)
    live = _live_from_constant(date(2025, 11, 30), date(2026, 11, 30))
    have = sorted(_live_from_constant(date(2026, 12, 1), date(2026, 12, 14)))

    rep = alarm.check(have, today, 0, cal, live)
    assert rep["live"]["ok"] is False
    assert rep["reconcile"]["disagree"] == [], "这份集合与常量零分歧，只该由右端否掉它"
    assert rep["expect"] == "2026-12-14", "停更的实探不许挪动 expect"
    assert alarm.verdict(rep) == alarm.EXPIRING


def test_a_half_year_live_window_cannot_carry():
    """窗口只有 105 天：右端贴着今天也不合格 —— 它够不到远一点的过去。"""
    today = date(2026, 12, 15)
    live = _live_from_constant(date(2026, 9, 1), date(2026, 12, 15))
    have = sorted(_live_from_constant(date(2026, 12, 1), date(2026, 12, 14)))

    rep = alarm.check(have, today, 0, cal, live)
    assert rep["live"]["span_days"] < cal.LIVE_MIN_SPAN_DAYS
    assert rep["live"]["ok"] is False
    assert alarm.verdict(rep) == alarm.EXPIRING


def test_disagreeing_live_is_not_used_and_does_not_move_the_verdict():
    """两份权威打架 ⇒ 不接替，但也不许改判：重叠区内常量仍是权威。

    分歧只说明**至少一边错**，不说明面板停了。把它接到判定上，等于用一条
    不信任的轴去扩展右端。构造：删掉常量说是交易日的一天、加进一个周六。
    """
    today = date(2026, 12, 15)
    live = _live_from_constant(date(2025, 12, 15), date(2026, 12, 15))
    live -= {date(2026, 12, 1)}
    live |= {date(2026, 12, 5)}  # 周六
    have = sorted(_live_from_constant(date(2026, 12, 1), date(2026, 12, 14)))

    rep = alarm.check(have, today, 0, cal, live)
    assert set(rep["reconcile"]["disagree"]) == {"2026-12-01", "2026-12-05"}
    assert rep["expect"] == "2026-12-14", "分歧不许改变 expect"
    assert alarm.verdict(rep) == alarm.EXPIRING, "接不了班 ⇒ 余额告警必须响"


def test_reconcile_is_empty_without_live():
    """没有实探时 `reconcile` 必须是「没有重叠区」，不是「零分歧」。

    两者在布尔上都是「没分歧」，但前者是「没测」，后者是「测了且一致」。
    把没测写成一致，就是又一处「把不知道写成没问题」。
    """
    assert cal.reconcile(None) == {"overlap": None, "disagree": [], "n_overlap": 0}
    assert cal.reconcile(set())["overlap"] is None


def test_replaying_a_future_date_never_consults_the_live_window(monkeypatch, tmp_path):
    """`--today` 回放未来日期时不许联网：实探窗口以真实今天为锚，混用会红出错误病因。

    变异：去掉 `today > real_today` 那道守卫 ⇒ 本测试调 `_live_sessions` 时抛，
    于是它红。若不做成「调用即抛」，守卫被删掉也只是多一次网络请求，
    测试照样绿 —— 那就不算守住。
    """
    (tmp_path / "data" / "kline_daily" / "date=2026-11-10").mkdir(parents=True)

    def _boom():
        raise AssertionError("回放未来日期不许查实探窗口")

    monkeypatch.setattr(alarm, "_live_sessions", _boom)
    code = alarm.main(["--data-dir", str(tmp_path / "data"),
                       "--today", "2099-01-05", "--no-notify"])
    assert code == 3, f"超出可判区间必须 3，实得 {code}"


def test_the_live_fetch_has_exactly_one_home(monkeypatch):
    """取日历这件事只许有一份实现：闹钟转发 `analysis/calendar_probe.py`。

    第二个消费者是 `analysis/auction_order.py` 的 09:20 闸门 —— 取法写两遍，就只
    会有一份被改到（`qq_symbol` 少 `bj` 前缀那次付过这个代价），而差异只会在某一天
    发作。这条钉的是「转发」这件事本身：`_live_sessions()` 的返回值必须是
    `probe()` 的原样输出，不得在这一层再包一层判断或默认值。
    """
    calls = []

    class _Fake:
        def probe(self):
            calls.append(1)
            return {date(2026, 11, 12)}, "1 个交易日"

    monkeypatch.setattr(alarm, "_probe", lambda: _Fake())
    assert alarm._live_sessions() == ({date(2026, 11, 12)}, "1 个交易日")
    assert calls == [1], "转发层自己改写了取数结果（或调了不止一次）"
