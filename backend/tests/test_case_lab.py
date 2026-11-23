"""操盘档案测试 — 只锁「会让结论变假」的四件事。

1. **展示文本与阈值同源**：判据的 `rule` 必须由 `params` 格式化而来。改了阈值
   却忘记改文案，是这类「方法论页面」最容易长期藏住的错——页面看起来仍然权威。
2. **可成交口径**：T+1 开盘一字板的样本不计入收益，但要计入命中总数。
   把买不进的样本算成收益，得到的全是 paper alpha（`table_game` 踩过同一条）。
3. **按日聚合**：同一天多只票命中只算一个观测日。不聚合时 t 会被横截面相关
   放大成假显著。
4. **案例 id 不许穿越目录**；`untraced` 只表示本地看不到，不写成「与事实不符」。

夹具是纯合成的 5 只 × 70 个交易日，不联网、不读用户数据。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import case_lab
from app.services.daily_panel import load_panel

SYMBOLS = ["600001.SH", "600002.SH", "000001.SZ", "300001.SZ", "002001.SZ"]


def _trading_days(n: int) -> list[date]:
    out: list[date] = []
    d = date(2026, 6, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _rows(n_days: int = 120) -> list[dict]:
    days = _trading_days(n_days)
    rows: list[dict] = []
    for si, sym in enumerate(SYMBOLS):
        close = 10.0 + si
        for ti, d in enumerate(days):
            prev = close
            ret = 0.004 * math.sin(ti * 0.7 + si)
            # 第 100 天：第 0 只票放量且收盘在当日低位 —— 天量滞涨的形态
            # （rolling 判据 min_samples=60，异常必须落在第 60 根之后）
            if si == 0 and ti == 100:
                ret = -0.005
                amount = 5e9
                high, low = prev * 1.06, prev * 0.90
                close = low + (high - low) * 0.2
            else:
                amount = 1e8 * (1 + 0.2 * math.sin(ti * 0.5 + si))
                high, low = max(prev, prev * (1 + ret)) * 1.01, min(prev, prev * (1 + ret)) * 0.99
                close = prev * (1 + ret)
            rows.append({
                "symbol": sym, "date": d,
                "open": prev, "high": high, "low": low, "close": close,
                "volume": amount / max(close, 1e-9), "amount": amount,
                "turnover_rate": 2.0 + 2.0 * abs(math.sin(ti * 0.4 + si)),
                "consecutive_limit_ups": 1 if (si == 0 and ti == 99) else 0,
                "consecutive_limit_downs": 0,
            })
    return rows


def _write_store(root: Path, n_days: int = 120) -> Path:
    """写一份最小可用的本地存储：日线分区 + 交易日目录 + 标的表。"""
    data = root / "data"
    rows = _rows(n_days)
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for d in sorted(frame["date"].unique().to_list()):
        tag = d.isoformat()
        out = data / "kline_daily_enriched" / f"date={tag}"
        out.mkdir(parents=True, exist_ok=True)
        frame.filter(pl.col("date") == d).write_parquet(out / "part.parquet")
        # 交易日集合的唯一来源是 kline_daily 分区目录
        (data / "kline_daily" / f"date={tag}").mkdir(parents=True, exist_ok=True)
    inst = data / "instruments"
    inst.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": SYMBOLS, "name": [f"测试{s}" for s in SYMBOLS],
        "float_shares": [1e8] * len(SYMBOLS),
    }).write_parquet(inst / "instruments.parquet")
    return data


# ===== 1. 展示文本与阈值同源 =====


def test_rule_text_is_formatted_from_params():
    for fp in case_lab.FINGERPRINTS:
        text = fp["rule"].format(**fp["params"])
        for key, value in fp["params"].items():
            forms = {str(value), f"{value:g}"}
            if isinstance(value, float) and 0 < value < 1:
                forms.add(f"{value:.0%}")
            assert any(f in text for f in forms), (
                f"{fp['id']} 的参数 {key}={value} 没有出现在规则文本里: {text}"
            )


def test_catalog_exposes_caveat_and_rule():
    for item in case_lab.fingerprint_catalog():
        assert item["rule"], item["id"]
        assert item["caveat"], f"{item['id']} 缺 caveat —— 没有反例的判据不许进库"


# ===== 2. 可成交口径 =====


def test_limit_gap_sample_is_not_counted_as_buyable():
    """T+1 开盘涨停 = 买不进。它必须留在 n_raw 里，但不能留在可成交收益里。"""
    days = _trading_days(10)
    base = []
    close = 10.0
    for i, d in enumerate(days):
        prev = close
        # 第 5 天之后：次日开盘直接 +10%（一字板）
        op = prev * 1.10 if i == 5 else prev
        close = op * 1.001
        base.append({
            "symbol": "600001.SH", "date": d, "open": op,
            "high": max(op, close), "low": min(op, close), "close": close,
            "volume": 1e6, "amount": 1e7, "turnover_rate": 3.0,
            "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
        })
    df = pl.DataFrame(base).with_columns(pl.col("date").cast(pl.Date))
    out = case_lab._attach_exec_returns(df)
    # d1_gap 是「T 日收盘 → T+1 开盘」：一字板体现在第 4 天这一行
    row = out.filter(pl.col("date") == days[4]).row(0, named=True)
    assert row["d1_gap"] > case_lab.LIMIT_GAP - 1e-9
    assert row["buyable"] is False
    # 普通日仍然可买
    normal = out.filter(pl.col("date") == days[0]).row(0, named=True)
    assert normal["buyable"] is True


# ===== 3. 按日聚合 =====


def test_day_series_counts_each_date_once():
    rows = [
        {"date": date(2026, 6, 1), "exc1": 0.01},
        {"date": date(2026, 6, 1), "exc1": 0.03},
        {"date": date(2026, 6, 2), "exc1": -0.01},
    ]
    df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    st = case_lab._day_series_stats(df, "exc1")
    assert st["n_days"] == 2
    # 6-1 的两条先取均值 (0.02)，再与 -0.01 一起算均值 → 0.5bp
    assert st["mean_bp"] == pytest.approx(0.5 * 100, abs=1)


# ===== 4. 存储与口径边界 =====


def test_case_id_cannot_escape_directory(tmp_path: Path):
    for bad in ("../evil", "a/b", "", "x" * 80, "有中文"):
        with pytest.raises(ValueError):
            case_lab._case_path(tmp_path, bad)


def test_case_crud_roundtrip(tmp_path: Path):
    data = tmp_path / "data"
    doc = case_lab.save_case(data, {
        "symbol": "600001.sh",
        "title": "往返测试",
        "start": "2026-06-01",
        "end": "2026-08-01",
        "evidence": [{"date": "2026-07-01", "kind": "penalty", "source": "决定书", "text": "…", "reliability": "official"}],
    })
    assert doc["symbol"] == "600001.SH"
    assert doc["id"].startswith("c-")

    again = case_lab.get_case(data, doc["id"])
    assert again is not None and again["title"] == "往返测试"

    # 更新保留 created_at，不产生第二个文件
    doc2 = case_lab.save_case(data, {**again, "title": "改过"})
    assert doc2["id"] == doc["id"]
    assert doc2["created_at"] == doc["created_at"]
    assert len(case_lab.list_cases(data)) == 1

    assert case_lab.delete_case(data, doc["id"]) is True
    assert case_lab.list_cases(data) == []


def test_dissect_and_reconcile_wording(tmp_path: Path):
    data = _write_store(tmp_path, 120)
    days = sorted(
        d.name[5:] for d in (data / "kline_daily_enriched").iterdir() if d.is_dir()
    )
    start, end = days[20], days[-1]

    dis = case_lab.dissect(data, "600001.SH", date.fromisoformat(start), date.fromisoformat(end))
    assert dis["state"] == "ok"
    assert len(dis["bars"]) > 0
    # 第 55 天构造了天量滞涨，必须被识别
    assert any(h["id"] == "blowoff_stall" for h in dis["hits"])

    doc = case_lab.save_case(data, {
        "symbol": "600001.SH", "title": "对账", "start": start, "end": end,
        "evidence": [
            {"date": days[100], "kind": "media", "source": "媒体", "text": "放量", "reliability": "media"},
            {"date": days[10], "kind": "leak", "source": "传闻", "text": "据说", "reliability": "hearsay"},
        ],
    })
    rec = case_lab.reconcile(data, doc["id"])
    assert rec["state"] == "ok"
    supports = [i["support"] for i in rec["items"]]
    assert "traced" in supports and "untraced" in supports
    # 文案底线：不能把「看不到」写成「假的」
    assert "不等于" in rec["note"]


# ===== 5. 月分区可读（历史教训：正则只认日分区） =====


def test_load_panel_reads_month_partitions(tmp_path: Path):
    data = tmp_path / "data"
    out = data / "kline_daily_enriched" / "date=2026-11"
    out.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH"], "date": [date(2026, 11, 3)],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
        "volume": [1e6], "amount": [1e7], "turnover_rate": [3.0],
        "consecutive_limit_ups": [0], "consecutive_limit_downs": [0],
    }).with_columns(pl.col("date").cast(pl.Date)).write_parquet(out / "part.parquet")

    df = load_panel(data, date(2026, 11, 1), date(2026, 11, 30))
    assert df.height == 1
    assert df["symbol"][0] == "600001.SH"


# ===== 6. 窗口日历不碰 kline_daily / 不扫 parquet（超时回归） =====


def test_panel_window_does_not_need_kline_daily(tmp_path: Path):
    """`kline_daily` 整个删掉，个股解剖仍然出结果。

    回归背景：`_panel` 曾用 `kline_trading_days`（读 `kline_daily` 的月分区）
    来截窗口。`list_partition_dates()` 对每个月分区都要 `scan_parquet` 读
    `date` 列去重 —— 容器 bind mount 上 246 个月分区实测 19.5s，把「20/40/
    60/120 日重扫」与「60/120/250 日解剖」全部推过前端 30s 默认超时。

    判据故意做成「删掉另一张表」而不是「计时」：计时在 CI 上没有稳定阈值，
    而这条分支只要被走回去就一定红（旧实现返回 state=no_data）。
    """
    import shutil

    data = _write_store(tmp_path, 120)
    days = sorted(d.name[5:] for d in (data / "kline_daily_enriched").iterdir() if d.is_dir())
    start, end = date.fromisoformat(days[20]), date.fromisoformat(days[-1])

    shutil.rmtree(data / "kline_daily")

    dis = case_lab.dissect(data, "600001.SH", start, end)
    assert dis["state"] == "ok"
    assert len(dis["bars"]) == 100


def test_calendar_reads_directory_names_only(tmp_path: Path, monkeypatch):
    """交易日历只读目录名，一次 parquet 都不打开。

    这是上一条的**因**：删除另一张表只能证明「不依赖那张表」，证明不了
    「不扫 parquet」。把 scan/read 全部换成抛异常，日历仍然要给出 120 天。
    """
    from app.services.daily_panel import enriched_partition_dates

    data = _write_store(tmp_path, 120)

    def boom(*_a, **_k):
        raise AssertionError("enriched_partition_dates 不应打开 parquet")

    monkeypatch.setattr(pl, "scan_parquet", boom)
    monkeypatch.setattr(pl, "read_parquet", boom)

    days = enriched_partition_dates(data)
    assert len(days) == 120
    assert days == sorted(days)


def test_nonstandard_partition_suffix_is_ignored(tmp_path: Path):
    """`date=2026-11-08.bak` 里的 parquet 不许被读进来。

    目录名与文件列表若用两套判据，就会「同一天读到两份」，把一天的成交额
    算成两天，页面不报错。**必须用跨区间查询**：单日区间下
    `'2026-11-08.bak' > '2026-11-08'` 恰好被字符串比较挡住，判据看起来
    永远是绿的 —— 只有 start != end 时才真的被包含。
    """
    data = _write_store(tmp_path, 5)
    days = sorted(d.name[5:] for d in (data / "kline_daily_enriched").iterdir() if d.is_dir())
    dup_day = days[2]
    src = data / "kline_daily_enriched" / f"date={dup_day}" / "part.parquet"
    dup = data / "kline_daily_enriched" / f"date={dup_day}.bak"
    dup.mkdir(parents=True)
    (dup / "part.parquet").write_bytes(src.read_bytes())

    df = load_panel(data, date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
    assert df.height == len(SYMBOLS) * len(days)  # 25 行，不是 30 行


def test_scan_as_of_is_the_last_bar_not_the_request_day(tmp_path: Path):
    """回看窗口的右端必须是数据里真有的那一天。

    回归：`as_of` 曾直接回显请求日。周末、盘中、数据未落盘时两者差一天，
    页面顶部写「10-23 → 11-20」而面板实际止于 11-19 —— 这种偏差不报错，
    只悄悄改变「回看 20 个交易日」的读法。
    """
    data = _write_store(tmp_path, 120)
    days = sorted(d.name[5:] for d in (data / "kline_daily_enriched").iterdir() if d.is_dir())

    out = case_lab.scan(data, date.fromisoformat(days[-1]) + timedelta(days=10), 20, 30)
    assert out["state"] == "ok"
    assert out["as_of"] == days[-1]
    assert out["start"] == days[-20]


# ===== 7. API 契约 =====

def _api_client(data_dir: Path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.case_lab import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    return TestClient(app)


def test_api_fingerprints_exposes_rule_and_caveat(tmp_path: Path):
    c = _api_client(tmp_path)
    r = c.get("/api/case-lab/fingerprints")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == len(case_lab.FINGERPRINTS)
    assert body["limit_gap"] == case_lab.LIMIT_GAP
    for item in body["items"]:
        assert item["rule"] and item["caveat"]


def test_api_case_roundtrip_and_404(tmp_path: Path):
    c = _api_client(tmp_path)
    r = c.post("/api/case-lab/cases", json={
        "symbol": "600001.sh", "title": "接口往返", "start": "2026-06-01", "end": "2026-08-01",
    })
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["symbol"] == "600001.SH"

    listing = c.get("/api/case-lab/cases").json()["items"]
    assert [x["id"] for x in listing] == [cid]

    assert c.delete(f"/api/case-lab/cases/{cid}").status_code == 200
    assert c.get(f"/api/case-lab/cases/{cid}").status_code == 404


def test_api_rejects_bad_enum_and_bad_date(tmp_path: Path):
    c = _api_client(tmp_path)
    bad_enum = c.post("/api/case-lab/cases", json={"symbol": "600001.SH", "actor_type": "庄家"})
    assert bad_enum.status_code == 422
    bad_date = c.get("/api/case-lab/dissect", params={
        "symbol": "600001.SH", "start": "2026-13-01", "end": "2026-11-01",
    })
    assert bad_date.status_code == 400
