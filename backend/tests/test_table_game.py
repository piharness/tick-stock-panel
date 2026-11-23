"""对手盘模型测试 — 只锁「会让结论变假」的五件事。

1. 对照组自检：非上榜股的匹配超额必须 ≈ 0。分桶失效时它会先炸，
   否则后面的席位排行全是波动率溢价的化妆。
2. 1 日榜与 3 日榜不能混：3 日榜的净额是三天累计，记到「次日收益」上
   就是把 3 天的钱记到 1 天。
3. 一字板必须单列：T+1 开盘涨停买不进，把它算进可成交收益是 paper alpha。
4. 空射程不落盘：没有连续跌停样本时「飞刀」是 no_data，不是 0%。
5. 单位：财务表 roe 已经是百分数（-5.45 表示 -5.45%），再乘 100 会变成 -545%。

夹具是纯合成的 40 只 × 80 个交易日，不依赖真实数据，也不联网。
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import table_game
from app.services.table_game import (
    SEAT_MIN_N,
    _attach_matched,
    _ledger,
    _load_events,
    _load_panel,
    _stock_lhb,
    get_board,
    get_overview,
)

# 40 只太少：10×5=50 个匹配桶，每格不到 1 只票时对照组退化成「自己和自己比」，
# 匹配超额会被事件本身污染。真实数据是 5500+ 只，夹具用 240 只保持同样的密度。
SYMBOLS = [f"{600000 + i:06d}.SH" for i in range(240)]
N_DAYS = 80
# LHB 事件的加成：次日「开盘买→收盘卖」多出 0.6%（60bp）
BONUS = 0.006


def _trading_days() -> list[date]:
    out: list[date] = []
    d = date(2026, 1, 5)
    while len(out) < N_DAYS:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _build_store(root: Path) -> tuple[Path, list[date]]:
    data = root / "data"
    days = _trading_days()
    for d in days:
        (data / "kline_daily" / f"date={d.isoformat()}").mkdir(parents=True, exist_ok=True)

    # 先算日内收益矩阵，再按 LHB 事件打加成，最后反推 OHLC —— 这样
    # 「次日开盘→收盘」是构造出来的已知量，可以直接和结论对账。
    intr = [[0.0] * N_DAYS for _ in SYMBOLS]
    gap = [[0.0] * N_DAYS for _ in SYMBOLS]
    for si in range(len(SYMBOLS)):
        for ti in range(N_DAYS):
            ret = 0.025 * math.sin(ti * 0.9 + si * 1.7) + 0.004 * ((si % 5) - 2)
            gap[si][ti] = 0.3 * ret
            intr[si][ti] = (1 + ret) / (1 + gap[si][ti]) - 1

    # 事件日：第 40~55 个交易日，前 10 只票上榜
    lhb_days = [days[t] for t in range(40, 56)]
    lhb_pairs: list[tuple[date, str]] = []
    for d in lhb_days:
        ti = days.index(d)
        for si in range(10):
            lhb_pairs.append((d, SYMBOLS[si]))
            if ti + 1 < N_DAYS:
                intr[si][ti + 1] += BONUS

    rows = []
    for si, sym in enumerate(SYMBOLS):
        close = 10.0 + si * 0.5
        for ti, d in enumerate(days):
            prev = close
            o = prev * (1 + gap[si][ti])
            c = prev * (1 + gap[si][ti]) * (1 + intr[si][ti])
            close = c
            vol = 1_000_000 * (1 + 0.5 * math.sin(ti * 0.4 + si))
            rows.append({
                "symbol": sym, "date": d,
                "open": o, "high": max(o, c) * 1.01, "low": min(o, c) * 0.99,
                "close": c, "volume": vol, "amount": vol * c,
                "turnover_rate": 3.0 + 2.0 * abs(math.sin(ti * 0.6 + si)),
                "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
            })
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for d in days:
        part = frame.filter(pl.col("date") == d)
        out = data / "kline_daily_enriched" / f"date={d.isoformat()}"
        out.mkdir(parents=True, exist_ok=True)
        part.write_parquet(out / "part.parquet")

    # 龙虎榜：1 日榜 + 一条 3 日榜（用来验证口径隔离）
    by_day: dict[date, list[str]] = {}
    for d, sym in lhb_pairs:
        by_day.setdefault(d, []).append(sym)
    dt_dir = data / "dragon_tiger"
    dt_dir.mkdir(parents=True, exist_ok=True)
    for d, syms in by_day.items():
        items = [{
            "thscode": s, "name": s, "change": 0.05,
            "net_value": 1e8, "org_net_value": 5e7, "hot_money_net_value": 5e7,
            "range_days": 1,
        } for s in syms]
        # 3 日榜：同一天、另一只票，净额极大 —— 若被混进来会明显抬高事件数
        items.append({
            "thscode": SYMBOLS[39], "name": SYMBOLS[39], "change": 0.21,
            "net_value": 9e8, "org_net_value": 9e8, "hot_money_net_value": None,
            "range_days": 3,
        })
        payload = {
            "state": "ok", "trade_date": d.isoformat(),
            "all": {"trade_date": d.isoformat(), "stock_items": items},
            "org": {"trade_date": d.isoformat(), "stock_items": []},
            "hot_money": {
                "trade_date": d.isoformat(),
                "hot_money_items": [
                    {"name": "甲席位", "buying": 1e8, "rows": [
                        {"thscode": s, "range_days": 1, "hot_money_item_net_value": 1e7}
                        for s in syms
                    ]},
                    # 与甲席位同覆盖的量化席位：验证「量化」这个对象真的能算出数字，
                    # 而不是只标一个 observed 就完事。两者的 bp 必须逐位相等。
                    {"name": "量化抢筹", "buying": 1e8, "rows": [
                        {"thscode": s, "range_days": 1, "hot_money_item_net_value": 1e7}
                        for s in syms
                    ]},
                    # 只出现 1 次的小样本席位：不得入榜
                    {"name": "乙席位", "buying": 1e7, "rows": [{
                        "thscode": SYMBOLS[1], "range_days": 1,
                        "hot_money_item_net_value": 1e7,
                    }]},
                ],
            },
        }
        (dt_dir / f"date={d.isoformat()}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    inst = pl.DataFrame({
        "symbol": SYMBOLS,
        "name": [f"票{i}" for i in range(len(SYMBOLS))],
        "float_shares": [1e8] * len(SYMBOLS),
        "total_shares": [1.2e8] * len(SYMBOLS),
    })
    (data / "instruments").mkdir(parents=True, exist_ok=True)
    inst.write_parquet(data / "instruments" / "instruments.parquet")

    fin = pl.DataFrame({
        "symbol": [SYMBOLS[0], SYMBOLS[1]],
        "period_end": ["2026-09-30", "2026-09-30"],
        # 单位是百分数：-5.45 = -5.45%
        "roe": [-5.45, 12.3],
    })
    (data / "financials" / "metrics").mkdir(parents=True, exist_ok=True)
    fin.write_parquet(data / "financials" / "metrics" / "part.parquet")
    return data, days


def _reset_caches() -> None:
    table_game._CACHE.clear()
    table_game._PANEL_CACHE.clear()
    table_game._EVENTS_CACHE.clear()
    table_game._CORE_CACHE.clear()


@pytest.fixture()
def store(tmp_path: Path):
    data, days = _build_store(tmp_path)
    _reset_caches()
    yield data, days
    _reset_caches()


# ===== 账本口径 =====


def test_control_group_is_flat_so_bucketing_works(store):
    """非上榜股的匹配超额必须 ≈ 0。这一条比任何收益结论都重要：
    它失效就说明分桶没起作用，后面的席位排行全是波动率溢价的化妆。"""
    data, days = store
    panel = _load_panel(data, days[-1])
    events, _seats = _load_events(data, days[-1])
    led = _ledger(_attach_matched(panel, events), _seats)
    assert led["control_bp"] is not None
    assert abs(led["control_bp"]) < 8, led


def test_bonus_is_recovered_by_matched_excess(store):
    """构造时给上榜股次日加了 60bp，匹配超额应该把它还原出来。"""
    data, days = store
    panel = _load_panel(data, days[-1])
    events, seats = _load_events(data, days[-1])
    led = _ledger(_attach_matched(panel, events), seats)
    assert 30 <= led["excess_bp"] <= 90, led
    assert led["t"] is not None and led["t"] > 2


def test_three_day_board_is_excluded(store):
    """3 日榜的净额是三天累计，混进 1 日事件会把 3 天的钱记到 1 天。"""
    data, days = store
    events, _ = _load_events(data, days[-1])
    # 80 天里 16 个事件日 × 10 只 = 160 条；SYMBOLS[39] 的 3 日榜必须不在其中
    assert events.height == 160
    assert SYMBOLS[39] not in events["symbol"].to_list()


def test_seat_below_min_sample_is_not_ranked(store):
    """只出现一次的席位不进榜 —— 否则「3 次全中」的故事会变成结论。"""
    data, days = store
    panel = _load_panel(data, days[-1])
    events, seats = _load_events(data, days[-1])
    led = _ledger(_attach_matched(panel, events), seats)
    names = [s["seat"] for s in led["seats"]]
    assert "乙席位" not in names
    for s in led["seats"]:
        assert s["n"] >= SEAT_MIN_N


def test_limit_gap_bucket_is_separated(store):
    """一字板必须单列：它是「买不进」，不能混进可成交收益。"""
    data, days = store
    panel = _load_panel(data, days[-1])
    events, seats = _load_events(data, days[-1])
    led = _ledger(_attach_matched(panel, events), seats)
    labels = [b["label"] for b in led["gap_buckets"]]
    assert any("一字板" in x for x in labels)
    # 四档的只数之和 == 事件总数（没有样本掉在档外）
    assert sum(b["n"] for b in led["gap_buckets"]) == led["n_event"]


# ===== 个股规则 =====


def test_knife_without_samples_is_no_data_not_zero(store):
    """空射程不落盘：没有连续跌停样本时给 no_data，不能显示 0%。"""
    data, days = store
    board = get_board(data, SYMBOLS[0], as_of=days[-1])
    knife = next(r for r in board["rules"] if r["id"] == "knife")
    assert knife["state"] == "no_data"
    assert knife.get("value") is None


def test_reduce_rule_is_always_no_data_with_explanation(store):
    """减持压力在本地无表，必须显式 no_data 并说清缺哪几张表，
    不能用「机构专用净额」冒充。"""
    data, days = store
    board = get_board(data, SYMBOLS[0], as_of=days[-1])
    rule = next(r for r in board["rules"] if r["id"] == "reduce")
    assert rule["state"] == "no_data"
    assert "十大股东" in rule["detail"]


def test_board_always_returns_exactly_nine_rules(store):
    """九条判定必须永远九条，且顺序与 RULE_IDS 一致。

    这条曾经真的漏过：没上过龙虎榜的票，⑨「机构 / 游资分歧」整条不追加，
    接口只返回 8 条。前端不会报错，只是少画一格——读者看到的是「本来就只有 8 条」。
    所以这里同时断言条数、顺序，和「缺的那条要解释自己为什么缺」。
    """
    data, days = store
    for sym in SYMBOLS[:5]:
        board = get_board(data, sym, as_of=days[-1])
        ids = [r["id"] for r in board["rules"]]
        assert ids == list(table_game.RULE_IDS), (sym, ids)
        for r in board["rules"]:
            assert r["state"] in {"triggered", "clear", "derived", "no_data"}
            # no_data 必须说清为什么（否则等于什么也没说）
            if r["state"] == "no_data":
                assert r.get("detail"), f"{sym} 的 {r['id']} 是 no_data 却没给原因"


def test_divergence_without_lhb_is_no_data_not_missing(store):
    """没上榜 ≠ 两类钱没分歧：要标 no_data 并说明是「看不见」。"""
    data, days = store
    # 夹具里只有部分票有 LHB 事件，挑一只从未上榜的
    quiet = next(sym for sym in SYMBOLS if _stock_lhb(data, days[-1], sym).is_empty())
    board = get_board(data, quiet, as_of=days[-1])
    div = next(r for r in board["rules"] if r["id"] == "divergence")
    assert div["state"] == "no_data"
    assert "看不见" in div["detail"]



def test_roe_unit_is_percent_not_fraction(store):
    """财务表 roe 已是百分数：-5.45 必须显示成 -5.5%，不是 -545%。"""
    data, days = store
    board = get_board(data, SYMBOLS[0], as_of=days[-1])
    assert board["meta"]["roe"] == pytest.approx(-5.45)
    shell = next(r for r in board["rules"] if r["id"] == "shell")
    assert "-5.5%" in shell["detail"]


# ===== 九条判定：口径与「不许消失」 =====


def _flat_series(n: int = 60, churn_day: int = -3, churn_amp: float = 0.4) -> pl.DataFrame:
    """一只「平时几乎不动、某一天换手暴增」的票。用来单测判据本身，不依赖夹具数据。

    churn_amp 是那一天振幅分位高低的开关：0.4 表示当天振幅极低（量增价不动），
    8.0 表示当天振幅也很大（放量拉升/砸盘）——后者就不是「对敲」的形状。
    """
    closes = [10.0] * n
    turns = [1.0] * n
    amps = [2.0] * n
    turns[churn_day] = 6.0
    amps[churn_day] = churn_amp
    dates = [date(2026, 1, 5) + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({
        "date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1e6] * n, "amount": [1e8] * n,
        "turnover_rate": turns, "amp": amps, "ret_t": [0.0] * n,
        "consecutive_limit_ups": [0] * n, "consecutive_limit_downs": [0] * n,
    })


def test_wash_rule_fires_on_same_day_churn():
    """对敲判定要的是「同一天量增价不动」。

    这是修过的一个真缺陷：原实现比的是「近 5 日均换手」与「近 5 日均振幅」的分位，
    两边都是均值 ⇒ 一天的异常被四天稀释，联合条件落在分布的空区域上（793 只票命中 0）。
    正常票（下面这种单日换手暴增）当时被判「不成立」，等于探测器永远沉默。
    """
    rules = {r["id"]: r for r in table_game._stock_rules(_flat_series(), pl.DataFrame(), {})}
    assert rules["wash"]["state"] == "triggered", rules["wash"]
    assert rules["wash"]["value"] == 1


def test_wash_rule_not_triggered_when_volume_and_range_move_together():
    """负控：放量的那天振幅也大 ⇒ 不是「量增价不动」，不许判成立。"""
    rules = {
        r["id"]: r
        for r in table_game._stock_rules(_flat_series(churn_amp=8.0), pl.DataFrame(), {})
    }
    assert rules["wash"]["state"] == "clear", rules["wash"]


def test_stock_rules_always_nine_even_with_no_meta_and_no_events():
    """缺股本、缺龙虎榜时也必须是九条：算不出来的那条标 no_data，而不是消失。"""
    rules = table_game._stock_rules(_flat_series(), pl.DataFrame(), {})
    assert [r["id"] for r in rules] == list(table_game.RULE_IDS)
    by_id = {r["id"]: r for r in rules}
    assert by_id["control"]["state"] == "no_data"
    assert by_id["divergence"]["state"] == "no_data"
    assert by_id["reduce"]["state"] == "no_data"


def test_board_events_carry_seen_at(store):
    """每个事件都必须回答「你什么时候能看到」——这是本页存在的理由。"""
    data, days = store
    board = get_board(data, SYMBOLS[0], as_of=days[-1])
    assert board["events"], board
    for e in board["events"]:
        assert e["seen_at"]
        assert e["actor"] in {
            "national", "social_security", "huijin", "insurance", "broker",
            "mutual_fund", "private_fund", "controlling_shareholder",
            "fund_manager", "hot_money", "quant", "retail",
        }


def test_unknown_symbol_is_not_silently_empty(store):
    data, days = store
    board = get_board(data, "999999.SH", as_of=days[-1])
    assert board["state"] == "not_in_universe"


# ===== 名册 =====


def test_overview_marks_evidence_state_honestly(store):
    """本地没有股东表 → 相关对象必须标 no_data，而不是给一个看似精确的数字。"""
    data, days = store
    ov = get_overview(data, as_of=days[-1])
    by_id = {o["id"]: o for o in ov["objects"]}
    for key in ("national", "social_security", "huijin", "controlling_shareholder"):
        assert by_id[key]["evidence"] == "no_data"
    assert by_id["hot_money"]["evidence"] == "observed"
    assert by_id["hot_money"]["stats"], "游资有观测数据，必须带数字"
    assert len(ov["missing_tables"]) >= 4


def test_quant_object_has_numbers_because_it_says_observed(store):
    """标了 observed 就必须给数字。

    旧实现把「量化 / 程序化」标成 observed、正文写「本地可单独统计其次日结果」，
    但 stats 是空的——承诺和数据对不上。这里把「量化抢筹」席位拼回匹配超额，
    它与同覆盖的「甲席位」是同一批事件，bp 必须逐位相等。
    """
    data, days = store
    ov = get_overview(data, as_of=days[-1])
    by_id = {o["id"]: o for o in ov["objects"]}
    quant = by_id["quant"]
    assert quant["evidence"] == "observed"
    assert quant["stats"], "observed 的对象没有数字，就是把承诺当结论"
    hot = next(s for s in ov["ledger"]["seats"] if s["seat"] == "甲席位")
    assert quant["stats"][0]["bp"] == hot["bp"]
    assert quant["stats"][0]["n"] == hot["n"]


# ===== 扫描层 =====

def test_scan_lists_today_board_with_seat_history(store):
    """扫描层必须给出「今天谁上榜 + 买方席位的历史」，否则它只是另一个详情页。"""
    data, days = store
    scan = table_game.get_scan(data, as_of=days[56])
    assert scan["state"] == "ok", scan
    assert scan["trade_date"] == days[55].isoformat()
    assert scan["names"], "有榜单的日子不能给出空名单"
    # 净额降序：第一条是当天净额最大的那只
    nets = [n["net_yi"] for n in scan["names"]]
    assert nets == sorted(nets, reverse=True)
    top = scan["names"][0]
    assert top["buy_seats"], "上榜且游资净买的票必须能列出席位"
    seat = top["buy_seats"][0]
    assert seat["n"] and seat["bp"] is not None, "席位历史必须带样本量与超额"


def test_scan_says_stale_when_board_lags_two_days(store):
    """滞后 ≥2 个交易日不可能是发布节奏。

    夹具的榜单只到第 56 个交易日，面板走到第 80 个 —— 这正是 2026-11 月分区
    回归的形状（数据在，取数停在某一天）。它必须变成会响的状态，而不是
    「T 日盘后才公布」这类把缺陷讲成规律的文案。
    """
    data, days = store
    scan = table_game.get_scan(data, as_of=days[-1])
    assert scan["state"] == "stale"
    assert scan["lag_days"] >= 2
    assert scan["trade_date"] == days[55].isoformat()


def test_scan_does_not_invent_names_without_events(store):
    """没有榜单数据时是 no_data，不是「今天没有机会」。"""
    data, days = store
    scan = table_game.get_scan(data, as_of=days[10])
    assert scan["state"] == "no_data"
    assert scan["names"] == []
    assert scan["counts"]["total"] == 0


def test_scan_own_history_declares_thin_sample(store):
    """单票自己的历史样本不够时必须标出来，不能拿 n=16 的均值当结论。

    夹具里每只票 16 个事件日 < OWN_MIN_N，所以 enough 必须是 False——
    这一条同时锁住「页面只能说全市场基准」这个说法。
    """
    data, days = store
    scan = table_game.get_scan(data, as_of=days[56])
    own = scan["names"][0]["own"]
    assert own["n"] == 16
    assert own["enough"] is False
    assert table_game.OWN_MIN_N > own["n"]


def test_gap_bucket_verdict_comes_from_one_definition(store):
    """分档结论由服务端判，前端不许重算阈值。

    一字板只能是 unbuyable（买不进），不能因为它的 bp 是负的就被写成「回避」——
    两者对读者的含义完全不同：一个是策略选择，一个是物理不可成交。
    """
    data, days = store
    led = table_game.get_scan(data, as_of=days[56])["gate"]["buckets"]
    by_kind = {("unbuyable" if "买不进" in b["label"] else "normal"): b for b in led}
    assert by_kind["unbuyable"]["verdict"] == "unbuyable"
    for b in led:
        assert b["verdict"] in {"take", "weak", "avoid", "unbuyable", "thin"}
        assert b["stable"] in {True, False, None}
        assert len(b["halves"]) in {0, 2}

