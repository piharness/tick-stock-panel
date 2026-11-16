"""策略池后端持久化服务契约。"""
import json

import pytest

from app.config import settings
from app.services import strategy_pool


def test_empty_pool_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    state = strategy_pool.get()
    assert state == {"ids": [], "updated_at": None, "revision": 0}


def test_set_pool_appends_history(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    strategy_pool.set_pool(["a", "b"])
    strategy_pool.set_pool(["a", "b", "c"])

    history = strategy_pool.get_history()
    assert len(history) == 2
    # 新在前
    assert history[0]["after"] == ["a", "b", "c"]
    assert history[1]["after"] == ["a", "b"]
    # 历史记录含 action 与时间戳
    assert history[0]["action"] == "set"
    assert history[0]["at"]


def test_add_dedupes_and_preserves_order(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    strategy_pool.set_pool(["a"])
    out = strategy_pool.add(["b", "a", "b"])
    assert out["ids"] == ["a", "b"]


def test_add_noop_does_not_append_history(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    strategy_pool.set_pool(["a"])
    strategy_pool.add(["a"])  # 已存在, 应 no-op
    assert len(strategy_pool.get_history()) == 1


def test_capture_pass_uses_dedicated_action(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    strategy_pool.add(["x"], action="capture_pass", reason="verdict PASS")
    record = strategy_pool.get_history()[0]
    assert record["action"] == "capture_pass"
    assert record["reason"] == "verdict PASS"


def test_remove(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    strategy_pool.set_pool(["a", "b", "c"])
    out = strategy_pool.remove(["b"])
    assert out["ids"] == ["a", "c"]


def test_reorder_requires_same_set(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    strategy_pool.set_pool(["a", "b", "c"])
    out = strategy_pool.reorder(["c", "a", "b"])
    assert out["ids"] == ["c", "a", "b"]
    with pytest.raises(ValueError, match="一致"):
        strategy_pool.reorder(["a", "b"])


def test_revision_increments_on_every_write(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert strategy_pool.get()["revision"] == 0
    strategy_pool.set_pool(["a"])
    assert strategy_pool.get()["revision"] == 1
    strategy_pool.add(["b"])
    assert strategy_pool.get()["revision"] == 2


def test_corrupted_pool_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "strategy_pool.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        strategy_pool.get()
