"""竞价采集器的四条契约：定名、拒绝启动、A 的两产地、批次上限。

## 为什么要这一份

采集器从 `.workbuddy/tmp` 搬进 `analysis/` 之前，它的三个缺陷都不是「会报错」型：

1. **固定名 + append**：`out = HERE/"auction_burst.jsonl"` 且 `open("a")`。次日直接跑
   会把新帧接在旧帧后面，`interval_hist` 与 `days[0]` 同时被污染，退出码 0。
2. **没有起点**：只有 `stop = now.replace(9,26,30)`。09:10 之后开抓 ⇒ 首帧 stamp
   已是当日 ⇒ `A` 变成「今日成交额」，而 `mult_092430` 仍然是 2.09
   （A 只进 kappa）⇒ 数字分辨不出，唯一信号是分析侧退出码。
3. **每日变量写进代码**：`NAMES` 里的 `prev_close` / 涨停价 / 昨日成交额都是
   2026-11-11 口径。2026-11-12 实测 20/20 只收盘价相对 11-11 都动了
   （002086 +10.02%、000678 +10.00%），拿它们算下单股数是安静地错。

每条规则下面都配一个**故意做错的输入**：全绿的守卫不是守卫，是装饰。

## 与后端判据的绑定

`A` 来自日线时最大的坑是「那一格是盘中快照」：2026-11-12 盘上 `quote_ts=11:30:03`，
用它算 A 会偏小（该池 11:30 累计额 / 11-11 全天额 中位 0.712）⇒ kappa 偏大 ⇒
容量被高估，正好错在危险那侧。所以这里不发明新阈值，而是把
`backend/app/services/data_integrity.py` 的判据搬过来，并用测试**逐点断言互补**。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, time as dt_time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load("auction_burst", "analysis/auction_burst.py")

POOL = json.loads((REPO / "analysis" / "auction_pool_cohort_2026-11-12.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 造输入
def _gtimg_line(qq: str, *, stamp: str = "20261111161415", amount_wan: float = 286000.0,
                levels: list[str] | None = None, n_fields: int = 45) -> str:
    """造一条 `v_sz000530="..."`。字段索引与项目口径一致：30=内容时间，37=成交额(万)。"""
    f = [""] * n_fields
    f[0], f[1], f[3], f[4], f[5], f[6] = "51", "测试股", "11.52", "11.28", "11.33", "1742267"
    if n_fields > 37:
        f[30] = stamp
        f[37] = str(amount_wan)
    for i, v in enumerate(levels or []):
        f[9 + i] = v
    return f'v_{qq}="' + "~".join(f) + '";'


FIVE_LEVELS = ["11.51", "170", "11.49", "35", "11.47", "10", "11.45", "5", "11.43", "1",
               "11.50", "494", "11.54", "122", "11.56", "60", "11.58", "30", "11.60", "9"]


def _write_partition(root: Path, name: str, rows: list[dict]) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    cols = {k: [r.get(k) for r in rows] for k in rows[0]}
    pq.write_table(pa.table(cols), d / "part.parquet")
    return d


# ------------------------------------------------------------------ 池子
def test_pool_carries_no_daily_variables():
    """池子只能有「身份」（哪些票），不能有「每日变量」（昨收/涨停价/昨额）。

    反例就在池子自己的 `why_forbidden` 里：旧 NAMES 把这三个量写成常量，
    值是 11-11 口径，而 11-12 有 20/20 只收盘价变了。
    """
    assert len(POOL["symbols"]) == 20
    assert len(set(POOL["symbols"])) == 20
    leaked = set(POOL) & set(POOL["forbidden_keys"])
    assert not leaked, f"池子里出现了每日变量键：{leaked}"
    for s in POOL["symbols"]:
        assert len(s) == 9 and s[6] == ".", f"代码形态不对：{s}"


# ------------------------------------------------------------ 与后端判据绑定
def test_close_cutoff_and_finality_agree_with_backend():
    """`_bar_is_final` 必须与 `data_integrity._is_snapshot` 严格互补。

    两处各自算一遍 15:30 这条线是刻意的（采集器要能在不装后端依赖的机器上跑），
    代价就是必须有一条测试把它们钉在一起 —— 否则改了一处，另一处安静地漂。
    """
    import sys

    sys.path.insert(0, str(REPO / "backend"))
    try:
        from app.services.data_integrity import CLOSE_CUTOFF, _is_snapshot
    finally:
        sys.path.pop(0)

    assert ab.CLOSE_CUTOFF == CLOSE_CUTOFF

    day = date(2026, 11, 12)
    probes = [
        datetime(2026, 11, 12, 11, 30, 3),
        datetime(2026, 11, 12, 15, 29, 59),
        datetime(2026, 11, 12, 15, 30, 0),
        datetime(2026, 11, 12, 16, 58, 26),
    ]
    for ts in probes:
        ms = int(ts.timestamp() * 1000)
        assert ab._bar_is_final(ms) is (not _is_snapshot(day, ms)), f"{ts} 两处判据不一致"
    # 空 quote_ts = 权威 batch 行，两边都必须判「不是快照 / 是定版」
    assert ab._bar_is_final(None) is True
    assert _is_snapshot(day, None) is False


def test_deadline_matches_analyzer():
    """申报截止只该有一个值。分析侧叫 `DEADLINE`，采集侧叫 `AUCTION_DEADLINE`。"""
    diag = _load("diag_auction_indicative_deadline", "analysis/_diag_auction_indicative.py")
    assert ab._hhmmss(diag.DEADLINE) == ab.AUCTION_DEADLINE


# -------------------------------------------------------------- 定名与拒绝启动
def test_output_path_is_dated_so_cross_day_cannot_mix():
    a_frames, a_meta = ab.out_paths(date(2026, 11, 12))
    b_frames, b_meta = ab.out_paths(date(2026, 11, 13))
    assert a_frames != b_frames and a_meta != b_meta
    assert a_frames.name == "20261112_burst.jsonl"
    assert b_frames.name == "20261113_burst.jsonl"
    assert a_frames.parent == ab.RAW_DIR / "raw" or a_frames.parent.name == "raw"


def test_refuses_after_auction_deadline(tmp_path, monkeypatch, capsys):
    """12:30 启动必须拒绝，而不是写一个空/半截文件让下游以为跑过了。"""
    monkeypatch.setattr(ab, "RAW_DIR", tmp_path)
    rc = ab.collect(date(2026, 11, 12), POOL["symbols"], 2.0, dt_time(9, 5), dt_time(9, 26, 30),
                    resume=False, now_fn=lambda: datetime(2026, 11, 12, 12, 30, 0))
    assert rc == 2
    assert not list(tmp_path.glob("*.jsonl")), "拒绝启动却写了帧文件"
    assert "已过申报截止" in capsys.readouterr().err


def test_refuses_weekend(monkeypatch):
    assert datetime(2026, 11, 14).weekday() == 5
    rc = ab.main(["--dry-run"], now_fn=lambda: datetime(2026, 11, 14, 9, 4, 0))
    assert rc == 2


def test_refuses_existing_file_without_resume(tmp_path, monkeypatch):
    """同日重跑默认拒绝：旧脚本的 append 会把两次的间隔混进同一个直方图。"""
    monkeypatch.setattr(ab, "RAW_DIR", tmp_path)
    (tmp_path / "20261112_burst.jsonl").write_text('{"ts":"2026-11-12T09:06:43","snap":{}}\n',
                                                   encoding="utf-8")
    rc = ab.collect(date(2026, 11, 12), POOL["symbols"], 2.0, dt_time(9, 5), dt_time(9, 26, 30),
                    resume=False, now_fn=lambda: datetime(2026, 11, 12, 9, 0, 0))
    assert rc == 2
    lines = (tmp_path / "20261112_burst.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "拒绝启动却往既有文件里追加了"


# ---------------------------------------------------------------- 解析与分批
def test_batches_never_exceed_upstream_limit():
    for n in (0, 1, 20, 60, 61, 200, 2312):
        parts = ab._chunks(["x"] * n)
        assert all(len(p) <= ab.BATCH for p in parts)
        assert sum(len(p) for p in parts) == n


def test_parse_snapshot_keeps_five_levels_and_stamp():
    """二~五档必须真的落进 dict：补字段的目的就是让 11-13 那一跑能证伪/证实
    「竞价期虚拟簿到底有没有二~五档」，解析层丢了就永远问不出来。"""
    snap = ab.parse_snapshot(_gtimg_line("sz000530", levels=FIVE_LEVELS))
    assert set(snap) == {"000530"}
    r = snap["000530"]
    assert (r["b1p"], r["b1v"], r["b2p"], r["b2v"]) == (11.51, 170.0, 11.49, 35.0)
    assert (r["b5p"], r["b5v"], r["a1p"], r["a1v"], r["a5v"]) == (11.43, 1.0, 11.5, 494.0, 9.0)
    assert r["stamp"] == "20261111161415"
    assert r["amount_wan"] == 286000.0


def test_parse_snapshot_drops_short_lines():
    """字段太少的行不是「空数据」，是口径变了 —— 必须整条丢掉，不能带着 None 进产物。"""
    assert ab.parse_snapshot(_gtimg_line("sz000530", n_fields=30)) == {}


def test_symbol_key_matches_fetch_lookup():
    """回归：`parse_snapshot` 的键曾经带着交易所前缀（`sz000530`），
    而 `fetch` 按 `_qq(s)[2:]`（`000530`）取 ⇒ 每轮取到 0 只、且 `failed=0`，
    整整 21 分钟写不下一帧也不会报错。"""
    snap = ab.parse_snapshot(_gtimg_line("sz000530", levels=FIVE_LEVELS))
    assert ab._qq("000530.SZ")[2:] in snap
    assert ab._qq("000530.SZ") == "sz000530"


def test_qq_maps_beijing_to_bj_not_sz():
    assert ab._qq("830799.BJ") == "bj830799"
    assert ab._qq("688836.SH") == "sh688836"


# ------------------------------------------------------------ 间隔与断点可见
def test_interval_hist_exposes_resume_gap():
    """`--resume` 续跑留下的洞必须出现在直方图与 `gaps_s` 里。

    反例：把 60 秒的洞当噪音去掉，就会得到「间隔很稳」的假象 —— 而那 60 秒
    里可能正好是 09:24 的读点。
    """
    stamps = [0.0, 2.0, 4.0, 64.0]
    hist = ab._interval_hist(stamps)
    assert hist == {"2": 2, "60": 1}
    assert ab._gaps_s(stamps, 2.0) == [60]
    assert ab._gaps_s(stamps, 25.0) == [], "3 倍声明间隔以内不算断点"


# ------------------------------------------------------------------- 日线 A
def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_a_daily_rejects_intraday_bar(tmp_path, monkeypatch):
    """盘中快照的日线不许出 A。

    用的就是盘上真实形态：2026-11-12 那一格 `quote_ts=11:30:03`。
    该池 11:30 累计额 / 11-11 全天额 中位 0.712、最高 2.57 ⇒ 当成 A 会让
    kappa 中位虚高 1/0.712 ≈ 1.40 倍，而且是往「容量变大」的方向错。
    """
    monkeypatch.setattr(ab, "DAILY_DIR", tmp_path)
    _write_partition(tmp_path, "date=2026-11-12",
                     [{"symbol": "600000.SH", "date": date(2026, 11, 12),
                       "amount": 5.16e8, "quote_ts": _ms(datetime(2026, 11, 12, 11, 30, 3))}])
    info = ab.prev_amount_from_daily(date(2026, 11, 13))
    assert info["prev_trade_day"] == "2026-11-12"
    assert info["final"] is False
    assert info["wan"] == {}, "判成快照却仍然交出了数值"
    assert "盘中快照" in info["reason"]


def test_a_daily_accepts_final_bar_and_converts_to_wan(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "DAILY_DIR", tmp_path)
    _write_partition(tmp_path, "date=2026-11-11",
                     [{"symbol": "600000.SH", "date": date(2026, 11, 11),
                       "amount": 5.16e8, "quote_ts": _ms(datetime(2026, 11, 11, 16, 58, 26))}])
    info = ab.prev_amount_from_daily(date(2026, 11, 12))
    assert info["final"] is True
    assert info["wan"] == {"600000.SH": 51600.0}, "元 → 万元没换，或换了两次"


def test_a_daily_handles_partition_without_quote_ts(tmp_path, monkeypatch):
    """老分区没有 `quote_ts` 列 ⇒ 权威 batch 行，不是读失败。"""
    monkeypatch.setattr(ab, "DAILY_DIR", tmp_path)
    _write_partition(tmp_path, "date=2026-11-10",
                     [{"symbol": "600000.SH", "date": date(2026, 11, 10), "amount": 4.0e8}])
    info = ab.prev_amount_from_daily(date(2026, 11, 12))
    assert info["final"] is True
    assert info["reason"].startswith("quote_ts 全空")
    assert info["wan"] == {"600000.SH": 40000.0}


def test_a_daily_handles_monthly_partition(tmp_path, monkeypatch):
    """月分区（`backend/app/decision/bars.py` 声明 2026-11 起按月）必须按 date 列过滤，
    否则会把整月的行都算成同一天，行数与额度一起翻几倍。"""
    monkeypatch.setattr(ab, "DAILY_DIR", tmp_path)
    _write_partition(tmp_path, "date=2026-11", [
        {"symbol": "600000.SH", "date": date(2026, 11, 11), "amount": 4.0e8,
         "quote_ts": _ms(datetime(2026, 11, 11, 16, 0, 0))},
        {"symbol": "600000.SH", "date": date(2026, 11, 12), "amount": 6.0e8,
         "quote_ts": _ms(datetime(2026, 11, 12, 16, 0, 0))},
    ])
    info = ab.prev_amount_from_daily(date(2026, 11, 13))
    assert info["partition_form"] == "monthly"
    assert info["prev_trade_day"] == "2026-11-12"
    assert info["wan"] == {"600000.SH": 60000.0}, "月分区没过 date 过滤，取到了别的日子"


def test_a_daily_says_why_when_no_earlier_partition(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "DAILY_DIR", tmp_path)
    _write_partition(tmp_path, "date=2026-11-12",
                     [{"symbol": "600000.SH", "date": date(2026, 11, 12), "amount": 1.0,
                       "quote_ts": None}])
    info = ab.prev_amount_from_daily(date(2026, 11, 12))
    assert info["final"] is False
    assert info["wan"] == {}
    assert "没有早于今天" in info["reason"]
