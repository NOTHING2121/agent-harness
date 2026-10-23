"""StateStore：state.json 的加锁事务与原子写回。"""

import json
import threading
import time
from pathlib import Path

import pytest

from agent_harness.errors import ConfigError
from agent_harness.settings import HarnessPaths
from agent_harness.store import PluginState, StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(HarnessPaths(home=tmp_path / "home"))


def test_missing_file_means_empty_state(store: StateStore) -> None:
    assert store.load().plugins == {}


def test_transaction_writes_back_atomically(store: StateStore, tmp_path: Path) -> None:
    with store.transaction() as state:
        state.plugins["notes"] = PluginState(enabled=True, config={"n": 1})

    home = tmp_path / "home"
    data = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert data["plugins"]["notes"]["enabled"] is True
    assert store.load().plugins["notes"].config == {"n": 1}
    assert [p.name for p in home.iterdir() if p.name.startswith(".state")] == [], "不残留临时文件"


def test_failed_transaction_does_not_write(store: StateStore) -> None:
    with store.transaction() as state:
        state.plugins["notes"] = PluginState()

    with pytest.raises(RuntimeError), store.transaction() as state:
        state.plugins["notes"].enabled = True
        raise RuntimeError("中途失败")

    assert store.load().plugins["notes"].enabled is False


def test_corrupted_file_is_reported(store: StateStore, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "state.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="已损坏") as exc_info:
        store.load()
    assert "删除" in (exc_info.value.hint or "")


def test_transactions_are_serialized(tmp_path: Path) -> None:
    """两个“进程”（独立的 StateStore 实例）并发修改，任何一次修改都不会丢失。"""
    paths = HarnessPaths(home=tmp_path / "home")

    def add(index: int) -> None:
        with StateStore(paths).transaction() as state:
            snapshot = dict(state.plugins)
            time.sleep(0.01)  # 放大读-改-写的竞争窗口
            snapshot[f"p{index}"] = PluginState()
            state.plugins = snapshot

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(StateStore(paths).load().plugins) == sorted(f"p{i}" for i in range(8))
