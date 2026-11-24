"""拥挤度判决测试 (不依赖真实全市场数据)。

覆盖: build_crowding_verdict 的降级路径 (无日线/缺列/样本不足) 与
最小数据的正确性 (换手率分组单调、按市值分层字段齐全)。
日期用 2026-08 连续自然日构造 (与 test_dragon_tiger 风格一致)。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.services import market_percentiles as mp


class _FakeRepo:
    def __init__(self, data_dir: Path):
        self.store = type("S", (), {"data_dir": data_dir})()


def _write_daily(data_dir: Path, symbol: str, closes: list[float],
                 turnovers: list[float], amounts: list[float]) -> None:
    """按日分区写 enriched 日线（含 raw_close/turnover_rate/amount）。"""
    base = date(2026, 8, 1)
    root = data_dir / "kline_daily_enriched"
    for i, c in enumerate(closes):
        d = base + timedelta(days=i)
        part = root / f"date={d}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": [symbol], "date": [d],
            "open": [c], "high": [c], "low": [c], "close": [c],
            "raw_close": [c], "raw_high": [c], "raw_low": [c],
            "volume": [1.0], "amount": [amounts[i]], "turnover_rate": [turnovers[i]],
            "consecutive_limit_ups": [0], "consecutive_limit_downs": [0],
            "quote_ts": [0],
        }).write_parquet(part)


def test_crowding_verdict_empty_without_daily(tmp_path: Path):
    out = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert out["n"] == 0
    assert any("无日线" in c for c in out["caveats"])


def test_crowding_verdict_insufficient_samples(tmp_path: Path):
    # 只有 3 天数据 → 前向 20 日收益算不出，样本为空
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2],
                 [1.0, 1.0, 1.0], [1e8, 1e8, 1e8])
    out = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    # 前向收益算不出 → n=0，且无 quintiles
    assert out["n"] == 0
    assert "quintiles" not in out


def test_crowding_verdict_reports_quintiles(tmp_path: Path):
    """构造足够长（100 自然日）多票数据，验证分位与按市值分层字段齐全。"""
    n = 100
    # 8 只票，各自 100 自然日（前向 20 交易日可覆盖，样本 > 0）
    for j in range(8):
        closes = [10.0 + j + i * 0.05 for i in range(n)]
        turnovers = [1.0 + (j * 0.5) + (i % 5) * 0.1 for i in range(n)]  # 不同票换手率分层
        amounts = [(1 + j) * 1e8 for _ in range(n)]  # 不同票成交额分层（市值代理）
        _write_daily(tmp_path, f"60000{j}.SH", closes, turnovers, amounts)

    out = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert out["n"] >= 1
    assert len(out["quintiles"]) == 5
    assert len(out["by_size"]) == 5
    # 分位标签单调
    labels = [q["label"] for q in out["quintiles"]]
    assert labels == ["低换手", "中低", "中", "中高", "高换手"]
    # 字段 key 齐全（数值可能为 None，因为单元测试样本量小、分位组可能为空）
    for q in out["quintiles"]:
        assert "ex20_median_bp" in q and "ex20_mean_bp" in q
    for b in out["by_size"]:
        assert "hi_minus_lo_bp" in b


# ===== 缓存：静态判决不得每次重算 =====
#
# 回归动机（2026-11-24）：该端点对 5316 个 enriched 分区、1530 万行做窗口+分组，
# 容器内实测 30.8s（账本均值 37s）。结论只随"下一批分区落盘"变化，属于静态统计。


def test_verdict_is_cached_per_data_dir(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    calls = {"n": 0}
    real = mp._compute_crowding_verdict

    def counted(repo):
        calls["n"] += 1
        return real(repo)

    monkeypatch.setattr(mp, "_compute_crowding_verdict", counted)
    mp.clear_crowding_cache()

    first = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    second = mp.build_crowding_verdict(_FakeRepo(tmp_path))

    assert calls["n"] == 1, "同一目录的结果不得重算"
    assert first == second


def test_clear_cache_forces_recompute(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    calls = {"n": 0}
    real = mp._compute_crowding_verdict

    def counted(repo):
        calls["n"] += 1
        return real(repo)

    monkeypatch.setattr(mp, "_compute_crowding_verdict", counted)
    mp.clear_crowding_cache()
    mp.build_crowding_verdict(_FakeRepo(tmp_path))
    mp.clear_crowding_cache()
    mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert calls["n"] == 2


# ===== 缓存：静态判决不得每次重算 =====
#
# 回归动机（2026-11-24）：该端点对 5316 个 enriched 分区、1530 万行做窗口+分组，
# 容器内实测 30.8s（账本均值 37s）。结论只随"下一批分区落盘"变化，属于静态统计。


def test_verdict_is_cached_per_data_dir(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    calls = {"n": 0}
    real = mp._compute_crowding_verdict

    def counted(repo):
        calls["n"] += 1
        return real(repo)

    monkeypatch.setattr(mp, "_compute_crowding_verdict", counted)
    mp.clear_crowding_cache()

    first = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    second = mp.build_crowding_verdict(_FakeRepo(tmp_path))

    assert calls["n"] == 1, "同一目录的结果不得重算"
    assert first == second


def test_clear_cache_forces_recompute(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    calls = {"n": 0}
    real = mp._compute_crowding_verdict

    def counted(repo):
        calls["n"] += 1
        return real(repo)

    monkeypatch.setattr(mp, "_compute_crowding_verdict", counted)
    mp.clear_crowding_cache()
    mp.build_crowding_verdict(_FakeRepo(tmp_path))
    mp.clear_crowding_cache()
    mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert calls["n"] == 2


# ===== stale-while-revalidate：写入打脏，读永不阻塞 =====
#
# 回归动机（2026-11-24）：早期用 30 分钟 TTL，等于**每半小时就有一个请求同步等
# 35s**。任务化能解决阻塞但要求前端多一套状态机；这个判决是静态结论，晚一天
# 看到没有代价，所以选 SWR：命中就返回，脏了后台重算。

import time  # noqa: E402


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_stale_read_returns_old_value_then_refreshes(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()

    versions = {"n": 0}

    def fake_compute(_repo):
        versions["n"] += 1
        return {"n": 0, "version": versions["n"]}

    monkeypatch.setattr(mp, "_compute_crowding_verdict", fake_compute)

    first = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert first["version"] == 1

    mp.mark_crowding_stale(tmp_path)
    stale_read = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert stale_read["version"] == 1, "脏了也要先把旧值给出去，不许阻塞"

    assert _wait_for(
        lambda: mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 2
    ), "后台重算应当完成"


def test_not_stale_does_not_recompute(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()
    calls = {"n": 0}

    def fake_compute(_repo):
        calls["n"] += 1
        return {"n": 0}

    monkeypatch.setattr(mp, "_compute_crowding_verdict", fake_compute)
    mp.build_crowding_verdict(_FakeRepo(tmp_path))
    for _ in range(5):
        mp.build_crowding_verdict(_FakeRepo(tmp_path))

    assert calls["n"] == 1


def test_marking_stale_without_cache_is_noop(tmp_path: Path, monkeypatch):
    """还没有值的时候打脏没有意义 —— 更不能因此把下一次读变成同步重算的触发器。"""
    mp.clear_crowding_cache()
    mp.mark_crowding_stale(tmp_path)

    calls = {"n": 0}

    def fake_compute(_repo):
        calls["n"] += 1
        return {"n": 0}

    monkeypatch.setattr(mp, "_compute_crowding_verdict", fake_compute)
    mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert calls["n"] == 1


# ===== 结论物化：冷进程也不再算 30.8s =====
#
# 回归动机（2026-11-24）：进程内缓存只在本进程活着时有意义，重启后第一次读
# 仍要同步算一遍；启动预热只是抢在用户之前。把结论落盘后，冷进程的一次读
# 就是读一个 1KB 的 JSON。

import json as _json  # noqa: E402


def _snapshot_path(data: Path) -> Path:
    return data / "_cache" / "crowding_verdict.json"


def test_cold_process_reads_snapshot_without_computing(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()

    # 第一次：同步算，并物化
    monkeypatch.setattr(mp, "_compute_crowding_verdict", lambda _r: {"n": 7, "version": 1})
    assert mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 1
    assert _snapshot_path(tmp_path).is_file(), "算完必须物化"

    # 模拟重启：清掉进程内缓存（但要允许再读快照）
    with mp._CROWDING_LOCK:
        mp._CROWDING_CACHE.clear()
        mp._CROWDING_STALE.clear()
        mp._CROWDING_BUILT_AT.clear()
        mp._CROWDING_SNAPSHOT_IGNORED.clear()

    calls = {"n": 0}

    def fail_if_called(_repo):
        calls["n"] += 1
        return {"n": 7, "version": 99}

    monkeypatch.setattr(mp, "_compute_crowding_verdict", fail_if_called)
    verdict = mp.build_crowding_verdict(_FakeRepo(tmp_path))

    assert verdict["version"] == 1, "冷进程应直接读物化结论"
    assert calls["n"] == 0, "不得重算"


def test_corrupt_snapshot_falls_back_to_compute(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()
    _snapshot_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _snapshot_path(tmp_path).write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(mp, "_compute_crowding_verdict", lambda _r: {"n": 1, "version": 5})
    assert mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 5


def test_wrong_snapshot_version_is_ignored(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()
    _snapshot_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _snapshot_path(tmp_path).write_text(
        _json.dumps({"version": 0, "verdict": {"n": 0, "version": 42}}), encoding="utf-8",
    )

    monkeypatch.setattr(mp, "_compute_crowding_verdict", lambda _r: {"n": 1, "version": 5})
    assert mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 5


# ===== 结论物化：冷进程也不再算 30.8s =====
#
# 回归动机（2026-11-24）：进程内缓存只在本进程活着时有意义，重启后第一次读
# 仍要同步算一遍；启动预热只是抢在用户之前。把结论落盘后，冷进程的一次读
# 就是读一个 1KB 的 JSON。

import json as _json  # noqa: E402


def _snapshot_path(data: Path) -> Path:
    return data / "_cache" / "crowding_verdict.json"


def test_cold_process_reads_snapshot_without_computing(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()

    # 第一次：同步算，并物化
    monkeypatch.setattr(mp, "_compute_crowding_verdict", lambda _r: {"n": 7, "version": 1})
    assert mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 1
    assert _snapshot_path(tmp_path).is_file(), "算完必须物化"

    # 模拟重启：清掉进程内缓存（但要允许再读快照）
    with mp._CROWDING_LOCK:
        mp._CROWDING_CACHE.clear()
        mp._CROWDING_STALE.clear()
        mp._CROWDING_BUILT_AT.clear()
        mp._CROWDING_SNAPSHOT_IGNORED.clear()

    calls = {"n": 0}

    def fail_if_called(_repo):
        calls["n"] += 1
        return {"n": 7, "version": 99}

    monkeypatch.setattr(mp, "_compute_crowding_verdict", fail_if_called)
    verdict = mp.build_crowding_verdict(_FakeRepo(tmp_path))

    assert verdict["version"] == 1, "冷进程应直接读物化结论"
    assert calls["n"] == 0, "不得重算"


def test_corrupt_snapshot_falls_back_to_compute(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()
    _snapshot_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _snapshot_path(tmp_path).write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(mp, "_compute_crowding_verdict", lambda _r: {"n": 1, "version": 5})
    assert mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 5


def test_wrong_snapshot_version_is_ignored(tmp_path: Path, monkeypatch):
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2], [1.0] * 3, [1e8] * 3)
    mp.clear_crowding_cache()
    _snapshot_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _snapshot_path(tmp_path).write_text(
        _json.dumps({"version": 0, "verdict": {"n": 0, "version": 42}}), encoding="utf-8",
    )

    monkeypatch.setattr(mp, "_compute_crowding_verdict", lambda _r: {"n": 1, "version": 5})
    assert mp.build_crowding_verdict(_FakeRepo(tmp_path))["version"] == 5
