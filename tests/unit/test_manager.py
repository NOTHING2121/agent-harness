"""PluginManager（M1 范围）：开发模式安装与可用插件加载。"""

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_harness.errors import HarnessError, ManifestError
from agent_harness.plugins.manager import PluginManager, PluginStatus
from agent_harness.settings import HarnessPaths
from agent_harness.store import StateStore

WritePlugin = Callable[..., Path]
CODE = "from harness_sdk import Plugin\nplugin = Plugin()\n"


@pytest.fixture
def paths(tmp_path: Path) -> HarnessPaths:
    return HarnessPaths(home=tmp_path / "home")


@pytest.fixture
def manager(paths: HarnessPaths) -> PluginManager:
    return PluginManager(paths, StateStore(paths), harness_version="0.1.0")


def test_dev_install_links_source(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)

    info = manager.install(source, dev=True)

    link = paths.plugins_dir / "alpha"
    assert info.id == "alpha"
    assert info.status == PluginStatus.ACTIVE
    assert info.tool_names == ["alpha__run"]
    assert link.is_symlink()
    assert link.resolve() == source.resolve()
    assert [p.name for p in paths.plugins_dir.iterdir()] == ["alpha"], "不应残留临时链接"


def test_dev_install_is_idempotent(manager: PluginManager, write_plugin: WritePlugin) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source, dev=True)

    info = manager.install(source, dev=True)

    assert info.reason is not None


def test_install_conflicting_source_is_rejected(
    manager: PluginManager, write_plugin: WritePlugin, tmp_path: Path
) -> None:
    manager.install(write_plugin("alpha", CODE), dev=True)
    other = tmp_path / "other" / "alpha"
    other.mkdir(parents=True)
    for name in ("plugin.json", "main.py"):
        (other / name).write_text((tmp_path / "sources" / "alpha" / name).read_text())

    with pytest.raises(HarnessError, match="已安装"):
        manager.install(other, dev=True)


def test_copy_install_not_available_yet(manager: PluginManager, write_plugin: WritePlugin) -> None:
    with pytest.raises(HarnessError, match="只支持开发模式"):
        manager.install(write_plugin("alpha", CODE))


def test_install_invalid_plugin_does_not_create_link(
    manager: PluginManager, paths: HarnessPaths, tmp_path: Path
) -> None:
    with pytest.raises(ManifestError):
        manager.install(tmp_path, dev=True)

    assert not paths.plugins_dir.exists()


def test_active_plugins_loads_installed_plugins(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(
        write_plugin(
            "alpha",
            CODE,
            config_schema={
                "type": "object",
                "properties": {"n": {"type": "integer", "default": 3}},
            },
        ),
        dev=True,
    )
    manager.install(write_plugin("beta", CODE), dev=True)

    plugins = manager.active_plugins()

    assert [p.id for p in plugins] == ["alpha", "beta"]
    assert plugins[0].config == {"n": 3}
    assert plugins[0].data_dir == paths.plugin_data_dir("alpha")
    assert len(manager.build_registry()) == 2


def test_broken_plugin_is_skipped_with_warning(
    manager: PluginManager,
    write_plugin: WritePlugin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = write_plugin("alpha", CODE)
    broken = write_plugin("beta", CODE)
    manager.install(good, dev=True)
    manager.install(broken, dev=True)
    (broken / "plugin.json").write_text(json.dumps({"id": "beta"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        plugins = manager.active_plugins()

    assert [p.id for p in plugins] == ["alpha"]
    assert "跳过插件 beta" in caplog.text


def test_no_plugins_dir_means_no_plugins(manager: PluginManager) -> None:
    assert manager.active_plugins() == []
