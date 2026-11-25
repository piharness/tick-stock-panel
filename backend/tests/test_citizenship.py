"""短线雷达（计算内核，模块名沿用 citizenship）测试 — 锁「会让分类变假」的五件事。

1. **主场是结构，不是当日快照**：当日换手突变不得改变 arena；窗口内持续突变必须改变。
2. **四格完备互斥**：每只票恰好一个 arena，四格计数加总等于总数（漏票不会报错）。
3. **属性标签可共现**：蓝筹/白马/龙头/妖股不是互斥分类。若哪天有人把它们改成
   `when/otherwise` 的单值，这个测试会红 —— 那正是 18 只三标签票开始消失的时候。
4. **白马必须 PIT**：财务公告日之后才可用。公告日之前不得有白马（否则是前视）。
5. **tags 数组与四个 bool 列不许有两套口径**（同一真相的第二个定义点是最贵的 bug）。

夹具是纯合成的 8 只 × 40 个交易日 + 一份单期财务 + 两个行业，不联网、不读用户数据。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import citizenship

SYMBOLS = [
    "600001.SH", "600002.SH", "000001.SZ", "000002.SZ",
    "300001.SZ", "002001.SZ", "002002.SZ", "688001.SH",
]

# 8 只票覆盖四格（市值分位 × 20 日换手中位分位）。切点是 scale≥0.50 / turn≥0.75，
# n=8 时 p 取 k/7：大市值 = p∈{4/7,5/7,6/7,1}，高换手 = p∈{6/7,1}。
# 换手值刻意互不相同，避免 ordinal rank 的 tie 让边界变成偶然。
#   institution 600001 / 000001 / 000002      quant 600002
#   hotmoney    300001                         nowhere 002001 / 002002 / 688001
#
# vol  = 日收益正弦振幅（决定 60 日波动率分位，蓝筹要「低波动」）
# drift= 40 日累计趋势（决定 20 日涨幅分位，龙头要「行业内涨得好」）
# lu   = 是否有连续涨停（妖股要「60 日内 ≥3 个涨停」）
#
# drift 必须显式写到每只票：若全组 drift 都是 0，ret20 的差异只剩正弦相位，
# 「谁是行业内涨幅第一」就变成偶然，龙头的测试会随机红绿。
PROFILE = {
    "600001.SH": {"shares": 1.0e10, "turn": 0.50, "vol": 0.0005, "drift": 0.00, "lu": False},
    "600002.SH": {"shares": 8.0e9, "turn": 7.50, "vol": 0.0120, "drift": 0.03, "lu": False},
    "000001.SZ": {"shares": 6.0e9, "turn": 0.60, "vol": 0.0040, "drift": 0.03, "lu": False},
    "000002.SZ": {"shares": 4.0e9, "turn": 0.40, "vol": 0.0080, "drift": 0.03, "lu": False},
    "300001.SZ": {"shares": 2.0e8, "turn": 8.50, "vol": 0.0150, "drift": 0.20, "lu": False},
    "002001.SZ": {"shares": 1.8e8, "turn": 0.70, "vol": 0.0030, "drift": 0.00, "lu": False},
    "002002.SZ": {"shares": 1.6e8, "turn": 0.55, "vol": 0.0100, "drift": 0.00, "lu": True},
    "688001.SH": {"shares": 1.5e8, "turn": 0.30, "vol": 0.0020, "drift": 0.00, "lu": False},
}

EXPECTED_ARENA = {
    "600001.SH": "institution",
    "600002.SH": "quant",
    "000001.SZ": "institution",
    "000002.SZ": "institution",
    "300001.SZ": "hotmoney",
    "002001.SZ": "nowhere",
    "002002.SZ": "nowhere",
    "688001.SH": "nowhere",
}

# 行业：两个 L1，每个 4 只。龙头的内部排名在这一层算。
INDUSTRY = {
    "600001.SH": "银行", "600002.SH": "银行", "000001.SZ": "银行", "000002.SZ": "银行",
    "300001.SZ": "半导体", "002001.SZ": "半导体", "002002.SZ": "半导体", "688001.SH": "半导体",
}

# 财务：只给两只，且毛利率高于本行业中位（银行 35 / 半导体 35）。
FIN = {
    "600001.SH": {"roe": 15.0, "revenue_yoy": 10.0, "net_income_yoy": 8.0,
                  "gross_margin": 45.0, "debt_to_asset_ratio": 40.0},
    "000001.SZ": {"roe": 14.0, "revenue_yoy": 6.0, "net_income_yoy": 5.0,
                  "gross_margin": 45.0, "debt_to_asset_ratio": 45.0},
}


def _trading_days(n: int) -> list[date]:
    out: list[date] = []
    d = date(2026, 6, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write_store(root: Path, n_days: int = 40, announce: str = "2026-06-01") -> Path:
    data = root / "data"
    days = _trading_days(n_days)
    rows: list[dict] = []
    inst_rows = []
    for idx, sym in enumerate(SYMBOLS):
        prof = PROFILE[sym]
        inst_rows.append({
            "symbol": sym, "name": f"测试{sym}", "float_shares": prof["shares"],
        })
        base = 10.0
        for ti, d in enumerate(days):
            close = base * (1 + prof["vol"] * math.sin(ti * 0.7 + idx)) \
                         * (1 + prof["drift"] * ti / n_days)
            mcap = close * prof["shares"]
            lu = 1 if (prof["lu"] and ti >= n_days - 5) else 0
            rows.append({
                "symbol": sym, "date": d,
                "open": close, "high": close, "low": close, "close": close,
                "volume": mcap * prof["turn"] / 100 / close,
                "amount": mcap * prof["turn"] / 100,
                "turnover_rate": prof["turn"],
                "consecutive_limit_ups": lu, "consecutive_limit_downs": 0,
            })
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for d in sorted(frame["date"].unique().to_list()):
        tag = d.isoformat()
        out = data / "kline_daily_enriched" / f"date={tag}"
        out.mkdir(parents=True, exist_ok=True)
        frame.filter(pl.col("date") == d).write_parquet(out / "part.parquet")
    inst = data / "instruments"
    inst.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(inst_rows).write_parquet(inst / "instruments.parquet")

    # 财务（单期，带公告日 —— PIT 测试靠它）
    fin_dir = data / "financials" / "metrics"
    fin_dir.mkdir(parents=True, exist_ok=True)
    fin_rows = [
        {"symbol": sym, "period_end": "2026-06-30", "announce_date": announce,
         "roe": v["roe"], "revenue_yoy": v["revenue_yoy"],
         "net_income_yoy": v["net_income_yoy"], "gross_margin": v["gross_margin"],
         "debt_to_asset_ratio": v["debt_to_asset_ratio"]}
        for sym, v in FIN.items()
    ]
    pl.DataFrame(fin_rows).with_columns(
        pl.col("period_end").str.to_date(), pl.col("announce_date").str.to_date()
    ).write_parquet(fin_dir / "part.parquet")

    # 行业
    hy_dir = data / "ext_data" / "ext_hy_ths"
    hy_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([
        {"symbol": s, "所属同花顺行业": f"{INDUSTRY[s]}-细分"} for s in SYMBOLS
    ]).write_parquet(hy_dir / "part.parquet")
    return data


def _patch_day(data: Path, day: date, sym: str, **cols) -> None:
    """改某只票在某一天的一列或多列。"""
    f = data / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    df = pl.read_parquet(f)
    for col, value in cols.items():
        df = df.with_columns(
            pl.when(pl.col("symbol") == sym).then(pl.lit(value))
            .otherwise(pl.col(col)).alias(col)
        )
    df.write_parquet(f)


# ===== 1. 主场是结构，不是当日快照 =====


def test_arena_matches_structure(tmp_path: Path):
    """四格映射必须由「中位市值分位 × 中位换手分位」决定。"""
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    got = {r["symbol"]: r["arena"] for r in out["rows"]}
    assert got == EXPECTED_ARENA, got


def test_one_day_spike_does_not_move_arena(tmp_path: Path):
    """当日换手突然放大 10 倍，不得改变主场 —— 否则它读的是快照，不是结构。

    反例场景：一只 0.7% 换手的冷门票某天被拉了一个涨停，换手冲到 7%。
    如果 arena 读当日值，它会立刻跳进「游资主场」，而它其实没有主场。
    """
    data = _write_store(tmp_path)
    days = _trading_days(40)
    last = days[-1]
    before = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    assert before["002001.SZ"]["arena"] == "nowhere"

    _patch_day(data, last, "002001.SZ", turnover_rate=7.0)

    after = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    assert after["002001.SZ"]["arena"] == "nowhere", (
        f"单日换手突变不应改主场，实际 {after['002001.SZ']['arena']} "
        f"(p_turn_med={after['002001.SZ']['p_turn_med']:.3f})"
    )
    # 当日读数必须变化（否则说明当日口径根本没进来）。
    assert after["002001.SZ"]["turn_ratio"] > 5, after["002001.SZ"]["turn_ratio"]


def test_sustained_shift_does_move_arena(tmp_path: Path):
    """换手在整个窗口内持续抬升，主场必须跟着变 —— 证明中位确实生效。

    这是上一条的负控：如果两只测试都只断言「不变」，一个永远返回常量的
    实现也能通过。
    """
    data = _write_store(tmp_path)
    days = _trading_days(40)
    # 让 002001.SZ 最后 20 天换手都变成 8%（覆盖整个窗口）。
    for d in days[-20:]:
        _patch_day(data, d, "002001.SZ", turnover_rate=8.0)

    out = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    assert out["002001.SZ"]["arena"] == "hotmoney", (
        f"窗口内持续高换手应进游资主场，实际 {out['002001.SZ']['arena']}"
    )
    assert abs(out["002001.SZ"]["turn_med"] - 8.0) < 0.01, out["002001.SZ"]["turn_med"]


# ===== 2. 四格完备互斥 =====


def test_arenas_are_exhaustive_and_disjoint(tmp_path: Path):
    """每只票恰好落在一格，四格计数加总等于总数。漏票不会报错，只会静默消失。"""
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    total = out["summary"]["total"]
    assert total == len(out["rows"])
    assert {r["arena"] for r in out["rows"]} <= set(citizenship.ARENA_ORDER)
    counts = {a["arena"]: a["count"] for a in out["summary"]["arenas"]}
    assert set(counts) == set(citizenship.ARENA_ORDER), "四个主场都必须出现（含空格）"
    assert sum(counts.values()) == total
    assert all(a["share"] >= 0 for a in out["summary"]["arenas"])
    # 各格内的等级计数也必须加总到该格。
    for a in out["summary"]["arenas"]:
        assert sum(a["levels"].values()) == a["count"], (a["arena"], a["levels"])


def test_arena_risk_is_reported_per_cell(tmp_path: Path):
    """每个主场必须带 dd20/med_ret20（外部验证的读数），且量化主场最危险。"""
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    by = {a["arena"]: a for a in out["summary"]["arenas"]}
    for key in citizenship.ARENA_ORDER:
        assert 0 < by[key]["dd20"] < 1, (key, by[key]["dd20"])
    # 实测：大市值 × 高换手（quant）左尾最厚，不能低于两个低换手格。
    assert by["quant"]["dd20"] > by["institution"]["dd20"]
    assert by["quant"]["dd20"] > by["nowhere"]["dd20"]


# ===== 3. 等级仍是当日口径 =====


def test_level_is_time_varying_and_uses_same_day(tmp_path: Path):
    """等级仍是当日分位：同一只票在不同交易日的等级必须不同。

    构造 300001.SZ（小市值高换手，平时 active/background）某天成交额暴增
    → 当日 core；次日恢复 → 不再是 core。若等级跟着中位走，这个测试失败。
    """
    data = _write_store(tmp_path)
    days = _trading_days(40)
    spike = days[20]
    _patch_day(data, spike, "300001.SZ", amount=5e10)

    at = {r["symbol"]: r for r in citizenship.compute(data, as_of=spike)["rows"]}
    after = {r["symbol"]: r for r in citizenship.compute(data, as_of=days[25])["rows"]}
    assert at["300001.SZ"]["level"] == "core", at["300001.SZ"]["p_amount"]
    assert after["300001.SZ"]["level"] != "core", after["300001.SZ"]["p_amount"]
    # 当日突变不应改主场（同一次突变，两个口径要分开）。
    assert at["300001.SZ"]["arena"] == "hotmoney", at["300001.SZ"]["arena"]


def test_turn_ratio_is_today_over_median(tmp_path: Path):
    """turn_ratio 必须 = 当日换手 / 窗口换手中位（不是分位、不是排名）。"""
    data = _write_store(tmp_path)
    days = _trading_days(40)
    _patch_day(data, days[-1], "600001.SH", turnover_rate=2.0)  # 平时 0.5 → 4 倍
    out = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    row = out["600001.SH"]
    assert abs(row["turn_ratio"] - 2.0 / 0.5) < 0.05, (row["turn_ratio"], row["turn_med"])
    # 未突变的票 turn_ratio ≈ 1。
    assert abs(out["600002.SH"]["turn_ratio"] - 1.0) < 0.01


# ===== 4. 空数据与次新降级 =====


def test_empty_data_degrades_gracefully(tmp_path: Path):
    data = tmp_path / "data"
    (data / "kline_daily_enriched").mkdir(parents=True, exist_ok=True)
    out = citizenship.compute(data)
    assert out["rows"] == []
    assert out["summary"]["total"] == 0
    assert out["summary"]["arenas"] == []


def test_new_listing_falls_back_to_same_day(tmp_path: Path):
    """窗口内只有 1 天数据的次新必须仍被分类，用当日值兜底。

    反例场景：新股上市首日换手 60%，若中位窗口要求满 20 天，它会被整只丢掉 ——
    而被丢掉的新股恰好是最活跃的一批。
    """
    data = _write_store(tmp_path)
    days = _trading_days(40)
    # 删掉 688001.SH 除最后一天外的所有分区行（模拟次新）。
    for d in days[:-1]:
        f = data / "kline_daily_enriched" / f"date={d.isoformat()}" / "part.parquet"
        pl.read_parquet(f).filter(pl.col("symbol") != "688001.SH").write_parquet(f)

    out = citizenship.compute(data)
    row = {r["symbol"]: r for r in out["rows"]}.get("688001.SH")
    assert row is not None, "次新不应被静默丢弃"
    assert row["n_days"] == 1, row["n_days"]
    assert abs(row["turn_med"] - row["turnover_rate"]) < 1e-6, "兜底应等于当日值"
    assert row["arena"] in citizenship.ARENA_ORDER


# ===== 5. 风险标签（行内，不再是独立模块）=====


def test_st_keeps_risk_flag_without_changing_arena(tmp_path: Path):
    """ST 一票否决：改名为 *ST 后 risk=high，但主场分类不受名称影响。

    锁住 2026 年最贵的错：把「成交额进前 10% 的 ST 票」当成核心资产。
    同时锁住一个更隐蔽的错：让名称参与分类，等于把制度性身份混进流动性结构。
    """
    data = _write_store(tmp_path)
    before = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}["600002.SH"]
    assert before["risk"] == "none"

    inst_path = data / "instruments" / "instruments.parquet"
    pl.read_parquet(inst_path).with_columns(
        pl.when(pl.col("symbol") == "600002.SH")
        .then(pl.lit("*ST测试")).otherwise(pl.col("name")).alias("name")
    ).write_parquet(inst_path)

    after = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}["600002.SH"]
    assert after["risk"] == "high"
    assert after["risk_reason"] == "ST"
    assert after["arena"] == before["arena"], "风险标记不得改写主场"
    assert after["level"] == before["level"], "风险标记不得改写等级（历史需可比）"


def test_risk_counts_add_up(tmp_path: Path):
    """summary.risk 各档加总等于总票数。"""
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    assert sum(out["summary"]["risk"].values()) == out["summary"]["total"]


def test_st_without_trading_is_reported_not_silently_dropped(tmp_path: Path):
    """停牌/退市整理的 ST 票当日无成交，进不了 rows —— 必须在 risk_absent 里报出来。

    反例场景：全量 ST 里有一部分当日无成交。若只统计 rows，页面会显示
    「红灯 192」而让人以为这就是全部；持仓者手里的停牌 ST 完全不可见。
    """
    data = _write_store(tmp_path)
    days = _trading_days(40)
    last = days[-1]

    inst_path = data / "instruments" / "instruments.parquet"
    pl.read_parquet(inst_path).with_columns(
        pl.when(pl.col("symbol") == "600001.SH")
        .then(pl.lit("*ST停牌")).otherwise(pl.col("name")).alias("name")
    ).write_parquet(inst_path)
    f = data / "kline_daily_enriched" / f"date={last.isoformat()}" / "part.parquet"
    pl.read_parquet(f).filter(pl.col("symbol") != "600001.SH").write_parquet(f)

    out = citizenship.compute(data, as_of=last)
    assert out["summary"]["risk"]["high"] == 0, "当日无成交的 ST 不应出现在 rows 的红灯里"
    assert out["summary"]["risk_absent"] == 1, "无成交的 ST 必须在 risk_absent 里报出"


# ===== 6. 属性标签：不是分类，是过滤条件 =====


def test_tags_are_not_mutually_exclusive(tmp_path: Path):
    """600001 必须同时拿到「蓝筹」和「白马」。

    实测全市场有 18 只同时命中 3 个标签、222 只命中 2 个。若有人把这四个词
    改成 when/otherwise 的单值分类，这个测试会红 —— 那正是共现票开始消失的时刻。
    """
    data = _write_store(tmp_path)
    by = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    tags = by["600001.SH"]["tags"]
    assert "bluechip" in tags, tags
    assert "whitehorse" in tags, tags
    assert len(tags) >= 2, tags


def test_tags_array_matches_bool_columns(tmp_path: Path):
    """tags 数组与四个 bool 列必须完全一致。

    这是「同一真相的第二个定义点」的典型位置：数组给前端筛选，bool 列给 summary
    计数。两套口径一旦漂移，页面会显示「命中 600 只」但筛出 580 只。
    """
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    for r in out["rows"]:
        expect = [k for k in citizenship.TAG_ORDER if r.get(f"t_{k}")]
        assert r["tags"] == expect, (r["symbol"], r["tags"], expect)


def test_bluechip_is_subset_of_institution(tmp_path: Path):
    """蓝筹的条件（市值前 20% + 换手后 50% + 低波动）逻辑上蕴含机构主场。

    这不是巧合，是两条规则的关系：机构 = 市值 p≥0.5 且换手 p<0.75，
    蓝筹 = 市值 p≥0.8 且换手 p≤0.5 —— 后者严格更窄。若哪天蓝筹出现在量化主场，
    说明有人的阈值改反了。
    """
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    blue = [r for r in out["rows"] if r["t_bluechip"]]
    assert blue, "夹具里应至少有一只蓝筹"
    for r in blue:
        assert r["arena"] == "institution", (r["symbol"], r["arena"])


def test_yaogu_requires_limit_ups(tmp_path: Path):
    """60 日内有 ≥3 个涨停的票必须被标为妖股；没有涨停的不得被标。"""
    data = _write_store(tmp_path)
    by = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    assert by["002002.SZ"]["lu_days"] >= 3, by["002002.SZ"]["lu_days"]
    assert "yaogu" in by["002002.SZ"]["tags"], by["002002.SZ"]["tags"]
    assert "yaogu" not in by["600001.SH"]["tags"], by["600001.SH"]["tags"]


def test_leader_uses_industry_rank(tmp_path: Path):
    """龙头是「行业内」的相对位置，不是全市场：行业内市值前 20% 且涨幅前 20%。

    半导体组里 300001 市值最大且 20 日涨幅最高 → 龙头；银行组里没人同时满足
    （市值最大的是 600001 但涨幅为 0）→ 银行组出不了龙头。
    """
    data = _write_store(tmp_path)
    by = {r["symbol"]: r for r in citizenship.compute(data)["rows"]}
    assert "leader" in by["300001.SZ"]["tags"], by["300001.SZ"]["tags"]
    assert "leader" not in by["600001.SH"]["tags"], by["600001.SH"]["tags"]
    assert by["300001.SZ"]["industry"] == "半导体", by["300001.SZ"]["industry"]


def test_whitehorse_respects_pit(tmp_path: Path):
    """财报公告日之后才允许出白马。

    反例场景：财报是 8 月 1 日公告的，回看 7 月 24 日。若直接用最新一期财务，
    就是把「未来才知道的 ROE」算进当时的判断 —— 页面会凭空多出一批白马。
    """
    # 公告日在所有交易日之后 → 全部不可见
    late = _write_store(tmp_path / "late", announce="2026-08-01")
    out_late = citizenship.compute(late)
    assert out_late["summary"]["financial_period"] is None, out_late["summary"]["financial_period"]
    assert all(not r["t_whitehorse"] for r in out_late["rows"]), "公告日之前不得有白马"
    assert out_late["summary"]["tags"][1]["count"] == 0
    assert out_late["summary"]["tags"][1]["validated"] is False

    # 公告日在所有交易日之前 → 可见
    early = _write_store(tmp_path / "early", announce="2026-06-01")
    out_early = citizenship.compute(early)
    assert out_early["summary"]["financial_period"] == "2026-06-30"
    assert out_early["summary"]["tags"][1]["count"] >= 1, "公告之后应能算出白马"


def test_tagless_rows_have_empty_array(tmp_path: Path):
    """四个条件都不满足时 tags 必须是空数组，不是 null、不是缺失字段。"""
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    empty = [r for r in out["rows"] if r["tags"] == []]
    assert empty, "夹具里应有至少一只无标签的票"
    for r in empty:
        assert all(not r[f"t_{k}"] for k in citizenship.TAG_ORDER), r["symbol"]


def test_tag_risk_is_declared_honestly(tmp_path: Path):
    """白马必须显式标注「无历史背书」；另外三个必须有 dd20。

    页面靠 validated=False 决定显示「无背书」而不是一个编出来的数字。
    """
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    by = {t["tag"]: t for t in out["summary"]["tags"]}
    assert by["whitehorse"]["dd20"] is None
    assert by["whitehorse"]["validated"] is False
    for k in ("bluechip", "leader", "yaogu"):
        assert by[k]["dd20"] is not None, k
        assert by[k]["validated"] is True, k
        assert 0 < by[k]["dd20"] < 1, (k, by[k]["dd20"])
    # 跨 34 期实测：蓝筹是最安全的标签，妖股最危险。若这条反了，说明常量被改错。
    assert by["bluechip"]["dd20"] < by["yaogu"]["dd20"]


# ===== 触点（focus）：注意力预算的漏斗 =====
#
# 夹具只有 8 只票，heat 分位在这么小的横截面上没有意义，所以这里直接对
# `_focus()` 喂合成 rows —— 测的是**漏斗的排除规则**，不是分位算得准不准。
# 这几条一旦破，页面会把「已经在炒的」「ST」「次新」当成今日候选，
# 而那正好是 _diag_youzi_validate 里 95.9% 追高榜的来源。

FOCUS_COLS = [
    "symbol", "name", "arena", "stage", "industry", "risk", "heat", "amp20",
    "turn_med", "amount", "mcap", "ret20", "ret60", "lu_days", "n_days", "listing_days",
    "p_amp", "p_turn_med", "p_amount", "p_scale",
]


def _rows(*specs: dict) -> pl.DataFrame:
    """合成 rows。[symbol, stage, risk, heat, amount, ret20, ret60, listing_days] 可覆盖。"""
    base = {
        "name": "测试", "arena": "hotmoney", "industry": "电子", "lu_days": 0,
        "amp20": 0.08, "turn_med": 8.0, "mcap": 3e9,
        "p_amp": 0.95, "p_turn_med": 0.9, "p_amount": 0.9, "p_scale": 0.2,
    }
    out = []
    for i, s in enumerate(specs):
        r = dict(base)
        r.setdefault("symbol", f"30000{i}.SZ")
        r.update({"stage": "cold", "risk": "none", "heat": 0.9,
                  "amount": 1e8, "ret20": 0.0, "ret60": 0.0,
                  "n_days": 60, "listing_days": 1000})
        r.update(s)
        out.append(r)
    return pl.DataFrame(out).select(FOCUS_COLS)


def test_stage_is_exhaustive_and_disjoint(tmp_path: Path):
    """每只票恰好一个 stage，四段计数加总等于总数（漏票不会报错，只会静默少一行）。"""
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    rows = pl.DataFrame(out["rows"])
    assert set(rows["stage"].unique()).issubset(set(citizenship.STAGE_ORDER))
    assert len(rows) == out["summary"]["total"]
    assert sum(out["summary"]["stages"].values()) == out["summary"]["total"]
    # 加总一致还不够：若同一票被判两次，总数会对不上。stage 是单值列，这里再锁一次非空。
    assert rows["stage"].null_count() == 0


def test_focus_excludes_already_hot():
    """已涨停的票不得进触点。这是本模块最贵的一条：全市场高分票 95.9% 在这一段。"""
    f = citizenship._focus(_rows({"symbol": "300001.SZ", "stage": "hot"}))
    assert f["picks"] == []
    assert f["excluded"]["hot"] == 1
    assert f["budget"]["cold"] == 0


def test_focus_excludes_st_bse_young_thin_falling():
    """五道排除各自生效，且都记在 excluded 里（不静默丢票）。

    这五道是「逻辑否定」，不靠模型：
      ST（制度异类）、北交所（样本不足）、次新（未解禁虚高）、
      成交额不足（容量出不来）、下跌中继（高波动来自出货）。
    """
    f = citizenship._focus(_rows(
        {"symbol": "600001.SH", "risk": "high"},              # ST
        {"symbol": "920001.BJ"},                               # 北交所
        {"symbol": "300002.SZ", "listing_days": 5},            # 次新（36 天上市的联亚型）
        {"symbol": "300003.SZ", "ret20": None},                # 无 20 日收益
        {"symbol": "300004.SZ", "amount": 1e7},                # 成交额 1000 万
        {"symbol": "300006.SZ", "ret60": -0.20},               # 下跌中继
        {"symbol": "300005.SZ"},                               # 唯一合格
    ))
    got = [p["symbol"] for p in f["picks"]]
    assert got == ["300005.SZ"], got
    assert f["excluded"]["st"] == 1
    assert f["excluded"]["bse"] == 1
    assert f["excluded"]["young"] == 2  # 次新 1 + 无 ret20 1（都进不了成熟候选）
    assert f["excluded"]["thin"] == 1
    assert f["excluded"]["falling"] == 1


def test_focus_excluded_partitions_the_drop():
    """漏斗单步的六项必须互斥且覆盖：non_cold + 五道否定 = struct − cold。

    页面直接把这六项展示成「519 → 55 删掉了 464」。旧版把 st/bse 计在 struct 上
    （含已涨停的票）、thin 又与 young 重叠，六项之和可以超过实际删除量 ——
    漏斗会在页面上自己加不平。
    """
    f = citizenship._focus(_rows(
        {"symbol": "300001.SZ", "stage": "hot"},     # 非未启动
        {"symbol": "600001.SH", "risk": "high"},     # ST
        {"symbol": "920001.BJ"},                      # 北交所
        {"symbol": "300002.SZ", "listing_days": 5},   # 次新
        {"symbol": "300003.SZ", "amount": 1e7},       # 成交额不足
        {"symbol": "300004.SZ", "ret60": -0.20},      # 下跌中继
        {"symbol": "300005.SZ"},                      # 唯一合格
    ))
    b, ex = f["budget"], f["excluded"]
    parts = ex["non_cold"] + ex["st"] + ex["bse"] + ex["thin"] + ex["young"] + ex["falling"]
    assert parts == b["struct"] - b["cold"], (parts, b)
    assert b["struct"] == 7 and b["cold"] == 1


def test_focus_excluded_partitions_the_drop():
    """漏斗单步的六项必须互斥且覆盖：non_cold + 五道否定 = struct − cold。

    页面直接把这六项展示成「519 → 55 删掉了 464」。旧版把 st/bse 计在 struct 上
    （含已涨停的票）、thin 又与 young 重叠，六项之和可以超过实际删除量 ——
    漏斗会在页面上自己加不平。
    """
    f = citizenship._focus(_rows(
        {"symbol": "300001.SZ", "stage": "hot"},     # 非未启动
        {"symbol": "600001.SH", "risk": "high"},     # ST
        {"symbol": "920001.BJ"},                      # 北交所
        {"symbol": "300002.SZ", "listing_days": 5},   # 次新
        {"symbol": "300003.SZ", "amount": 1e7},       # 成交额不足
        {"symbol": "300004.SZ", "ret60": -0.20},      # 下跌中继
        {"symbol": "300005.SZ"},                      # 唯一合格
    ))
    b, ex = f["budget"], f["excluded"]
    parts = ex["non_cold"] + ex["st"] + ex["bse"] + ex["thin"] + ex["young"] + ex["falling"]
    assert parts == b["struct"] - b["cold"], (parts, b)
    assert b["struct"] == 7 and b["cold"] == 1


def test_focus_budget_is_monotone():
    """漏斗必须逐层收窄：market ≥ struct ≥ cold ≥ picks。"""
    f = citizenship._focus(_rows(*[{"symbol": f"3000{i:02d}.SZ"} for i in range(30)]))
    b = f["budget"]
    assert b["market"] == 30
    assert b["market"] >= b["struct"] >= b["cold"] >= b["picks"]
    assert b["picks"] <= citizenship.FOCUS_TOP_N
    assert b["picks"] == len(f["picks"])


def test_focus_respects_heat_cut_and_uniqueness():
    """低于结构门槛的不进；同一票不得出现两次。"""
    f = citizenship._focus(_rows(
        {"symbol": "300001.SZ", "heat": citizenship.HEAT_CUT - 0.01},
        {"symbol": "300002.SZ", "heat": citizenship.HEAT_CUT + 0.01},
    ))
    assert [p["symbol"] for p in f["picks"]] == ["300002.SZ"]
    syms = [p["symbol"] for p in f["picks"]]
    assert len(syms) == len(set(syms))


def test_focus_ranking_is_by_heat():
    """排序按结构分降序：页面第一行必须是分数最高的那只，不能是文件顺序。"""
    f = citizenship._focus(_rows(
        {"symbol": "300001.SZ", "heat": 0.80},
        {"symbol": "300002.SZ", "heat": 0.99},
        {"symbol": "300003.SZ", "heat": 0.90},
    ))
    assert [p["symbol"] for p in f["picks"]] == ["300002.SZ", "300003.SZ", "300001.SZ"]
    heats = [p["heat"] for p in f["picks"]]
    assert heats == sorted(heats, reverse=True)


def test_empty_focus_degrades_gracefully():
    """空数据时 focus 的每个键都必须存在 —— 前端读 budget.picks 不能抛。"""
    f = citizenship._focus(pl.DataFrame())
    assert f["budget"] == {"market": 0, "struct": 0, "cold": 0, "picks": 0}
    assert f["picks"] == []
    assert set(f["stages"]) == set(citizenship.STAGE_ORDER)


# ===== 重点关注（锁仓横盘）：独立口径，不套观察池门槛 =====
#
# 这一块的正当性来自「与旧标准互斥」：池要 heat≥0.75（换手项高），本形态要
# 60 日均换手 ≤1.3%。测试要锁住三件事：
#   1) 命中票确实进不了池，且冲突项被逐条写出来（不是静默丢弃）；
#   2) 六道硬门槛互斥且覆盖，漏斗能加平；
#   3) 排序按离参照读数的距离，不是按 heat。

LOCK_COLS = [
    "symbol", "name", "industry", "arena", "risk", "heat", "stage", "mcap",
    "turn60", "turnover_rate", "amt60", "ret20", "ret60", "amount", "lu20",
    "listing_days", "lu250", "lhb_days",
]


def _lock_rows(*specs: dict) -> pl.DataFrame:
    """合成 df。[turn60, ret20, mcap, amt60, heat, amount, ret60, lu20, listing_days] 可覆盖。"""
    base = {
        "name": "测试", "industry": "电子", "arena": "nowhere", "risk": "none",
        "heat": 0.30, "stage": "cold", "mcap": 2.0e9,
        "turn60": 0.96, "turnover_rate": 0.96, "amt60": 3.0e7, "ret20": -0.035,
        "ret60": -0.02, "amount": 5.0e7, "lu20": 0, "listing_days": 2000,
        "lu250": 0, "lhb_days": 0,
    }
    out = []
    for i, s in enumerate(specs):
        r = dict(base)
        r.setdefault("symbol", f"30000{i}.SZ")
        r.update(s)
        out.append(r)
    return pl.DataFrame(out).select(LOCK_COLS)


def test_lock_watch_hits_band_and_lists_conflicts():
    """形态命中的票必须给出「为什么进不了观察池」，而不是悄悄不给。"""
    f = citizenship._lock_watch(_lock_rows({"symbol": "603309.SH"}))
    assert len(f["picks"]) == 1
    p = f["picks"][0]
    assert p["in_pool"] is False
    # heat 0.30 < 0.75 必然冲突（这两条口径构造上互斥）
    assert any("热度分" in c for c in p["conflicts"])
    # 冲突项是人话，且带实际数值，能复核
    assert all("<" in c or "超" in c or "「" in c for c in p["conflicts"])
    # 命中票一定同时满足三个形态条件
    assert abs(p["turn60"] - citizenship.LOCK_TURN60_TARGET) <= citizenship.LOCK_TURN60_TOL
    assert abs(p["ret20"] - citizenship.LOCK_RET20_TARGET) <= citizenship.LOCK_RET20_TOL


def test_lock_watch_hard_gates_partition_the_market():
    """六道硬门槛互斥且覆盖：st+bse+young+moved+thin + hard = market。"""
    f = citizenship._lock_watch(_lock_rows(
        {"symbol": "600001.SH", "risk": "high"},                 # ST
        {"symbol": "920001.BJ"},                                  # 北交所
        {"symbol": "300002.SZ", "listing_days": 30},              # 次新
        {"symbol": "300003.SZ", "lu20": 1},                       # 已启动（20 日内涨停）
        {"symbol": "300004.SZ", "amt60": 5e6},                    # 缩量不足
        {"symbol": "300005.SZ"},                                  # 合格
    ))
    b, ex = f["budget"], f["excluded"]
    assert b["market"] == 6
    assert ex["st"] + ex["bse"] + ex["young"] + ex["moved"] + ex["thin"] + ex["band"] + b["matched"] == b["market"]
    assert b["hard"] == 1
    assert [p["symbol"] for p in f["picks"]] == ["300005.SZ"]


def test_lock_watch_band_rejects_near_misses():
    """三个形态条件各自生效：换手太高/涨幅太正/盘子太大都不算。"""
    f = citizenship._lock_watch(_lock_rows(
        {"symbol": "300001.SZ", "turn60": 2.50},        # 换手太高（观察池那一类）
        {"symbol": "300002.SZ", "ret20": 0.02},         # 已经涨了
        {"symbol": "300003.SZ", "mcap": 1.0e10},        # 锁 20% 要 20 亿
        {"symbol": "300004.SZ"},                         # 唯一合格
    ))
    assert [p["symbol"] for p in f["picks"]] == ["300004.SZ"]
    assert f["excluded"]["band"] == 3


def test_lock_watch_ranks_by_distance_to_reference():
    """排序按离参照读数（0.96% / −3.5%）的距离，不是 heat：本块没有 heat 概念。"""
    f = citizenship._lock_watch(_lock_rows(
        {"symbol": "300001.SZ", "turn60": 0.65, "ret20": -0.059, "heat": 0.99},
        {"symbol": "300002.SZ", "turn60": 0.96, "ret20": -0.035, "heat": 0.10},
        {"symbol": "300003.SZ", "turn60": 1.20, "ret20": -0.015, "heat": 0.50},
    ))
    assert [p["symbol"] for p in f["picks"]] == ["300002.SZ", "300003.SZ", "300001.SZ"]


def test_lock_watch_marks_relative_recognition():
    """辨识度 = 一年涨停 + 龙虎榜的综合分位；只有分位够高的那只带标记。"""
    f = citizenship._lock_watch(_lock_rows(
        {"symbol": "300001.SZ", "lu250": 0, "lhb_days": 0},
        {"symbol": "300002.SZ", "lu250": 6, "lhb_days": 3},
        {"symbol": "300003.SZ", "lu250": 1, "lhb_days": 0},
    ))
    by = {p["symbol"]: p for p in f["picks"]}
    assert by["300002.SZ"]["high_recognition"] is True
    assert by["300001.SZ"]["high_recognition"] is False
    assert by["300002.SZ"]["recognition"] > by["300001.SZ"]["recognition"]


def test_lock_watch_readout_is_present_and_negative():
    """历史读数必须跟着走：形态的未来涨停率显著低于同池对照，页面不能只写「重点」。"""
    f = citizenship._lock_watch(_lock_rows({"symbol": "300001.SZ"}))
    r = f["readout"]
    assert r["lu_rate"] < r["ctrl_lu_rate"]
    assert r["lu_t"] < -2
    assert r["base_periods"] >= 60


def test_lock_watch_exposes_turn_quintile():
    """换手档 = 当日 turn60 的横截面五分位（Q1 最低），与 prereg-surge-all 同口径。

    页面用它把「锁仓」锚到可复核的读数上；如果这只票的档位和它的 turn60 排序不一致，
    卡片会把人指向错的那一档。
    """
    rows = _lock_rows(*[
        {"symbol": f"3000{i:02d}.SZ", "turn60": 0.5 + i * 0.1,
         "turnover_rate": (0.5 + i * 0.1) * 1.5}
        for i in range(10)
    ])
    f = citizenship._lock_watch(rows)
    picks = sorted(f["picks"], key=lambda p: p["turn60"])
    assert len(picks) >= 3
    qs = [p["turn_q"] for p in picks]
    assert all(q is not None and 1 <= q <= 5 for q in qs), qs
    # turn60 越大，档位不得更小（秩映射必须单调）
    assert qs == sorted(qs), (qs, [round(p["turn60"], 2) for p in picks])
    # 放量倍数 = 当日换手 / 60 日均换手（夹具里固定 1.5 倍）
    for p in picks:
        assert abs(p["tr_ratio"] - 1.5) < 1e-6


def test_empty_lock_watch_degrades_gracefully():
    f = citizenship._lock_watch(pl.DataFrame())
    assert f["picks"] == []
    assert f["budget"]["matched"] == 0
    assert "criteria" in f and "readout" in f
