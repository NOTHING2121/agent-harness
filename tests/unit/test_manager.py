"""PluginManager：安装、卸载、启用授权、禁用、配置、有效状态计算与自动清理。"""

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_harness.errors import HarnessError, ManifestError
from agent_harness.plugins import manager as manager_module
from agent_harness.plugins.manager import PluginManager, PluginStatus
from agent_harness.plugins.manifest import Manifest
from agent_harness.settings import HarnessPaths
from agent_harness.store import StateStore

WritePlugin = Callable[..., Path]
CODE = "from harness_sdk import Plugin\nplugin = Plugin()\n"
REQUIRED_CONFIG = {
    "type": "object",
    "properties": {
        "root": {"type": "string", "description": "根目录"},
        "limit": {"type": "integer", "minimum": 1, "default": 3},
    },
    "required": ["root"],
    "additionalProperties": False,
}


def approve_all(manifest: Manifest) -> bool:
    return True


@pytest.fixture
def paths(tmp_path: Path) -> HarnessPaths:
    return HarnessPaths(home=tmp_path / "home")


@pytest.fixture
def manager(paths: HarnessPaths) -> PluginManager:
    return PluginManager(paths, StateStore(paths), harness_version="0.1.0")


def reopen(paths: HarnessPaths, version: str = "0.1.0") -> PluginManager:
    """模拟重启：用全新的实例读取同一个数据目录。"""
    return PluginManager(paths, StateStore(paths), harness_version=version)


# ---- 安装 ----


def test_copy_install_creates_package_and_starts_disabled(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "x.pyc").write_bytes(b"0")

    info = manager.install(source)

    link = paths.plugins_dir / "alpha"
    package = Path(os.path.realpath(link))
    assert info.status == PluginStatus.DISABLED
    assert info.hint == "harness plugin enable alpha"
    assert package.parent == paths.packages_dir.resolve()
    assert package.name.startswith("alpha-0.1.0-")
    assert not os.readlink(link).startswith("/"), "副本链接使用相对路径"
    assert (package / "main.py").is_file()
    assert not (package / "__pycache__").exists(), "复制时跳过 __pycache__"
    assert manager.build_registry().tools == [], "未启用的插件不进入注册表"
    assert [p.name for p in paths.plugins_dir.iterdir()] == ["alpha"], "不应残留临时链接"


def test_copy_install_is_independent_of_source(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source)
    (source / "plugin.json").write_text("{}", encoding="utf-8")

    assert manager.get("alpha").status == PluginStatus.DISABLED


def test_dev_install_links_source(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)

    info = manager.install(source, dev=True)

    assert info.dev and info.status == PluginStatus.DISABLED
    assert info.tool_names == ["alpha__run"]
    assert (paths.plugins_dir / "alpha").resolve() == source.resolve()


def test_dev_install_is_idempotent(manager: PluginManager, write_plugin: WritePlugin) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source, dev=True)
    manager.enable("alpha", approve=approve_all)

    info = manager.install(source, dev=True)

    assert info.status == PluginStatus.ACTIVE, "重复安装同一来源不改变状态"


def test_installing_existing_plugin_is_rejected(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source)

    with pytest.raises(HarnessError, match="已安装") as exc_info:
        manager.install(source)
    assert "uninstall" in (exc_info.value.hint or "")


def test_install_invalid_plugin_changes_nothing(
    manager: PluginManager, paths: HarnessPaths, tmp_path: Path
) -> None:
    with pytest.raises(ManifestError):
        manager.install(tmp_path)

    assert not paths.home.exists()


def test_install_rejects_incompatible_plugin(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE, harness_version=">=2.0")

    with pytest.raises(HarnessError, match="需要 harness >=2.0，当前为 0.1.0"):
        manager.install(source)
    assert not paths.plugins_dir.exists()


def test_install_rejects_symlinks_and_cleans_up(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin, tmp_path: Path
) -> None:
    source = write_plugin("alpha", CODE)
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (source / "link.txt").symlink_to(tmp_path / "secret.txt")

    with pytest.raises(HarnessError, match="符号链接"):
        manager.install(source)
    assert list(paths.packages_dir.iterdir()) == [], "失败时删除临时副本"
    assert not (paths.plugins_dir / "alpha").exists()


def test_install_enforces_size_limit(
    manager: PluginManager,
    paths: HarnessPaths,
    write_plugin: WritePlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "MAX_PACKAGE_BYTES", 10)
    source = write_plugin("alpha", CODE)

    with pytest.raises(HarnessError, match="大小上限"):
        manager.install(source)
    assert list(paths.packages_dir.iterdir()) == []


# ---- 启用、禁用与授权 ----


def test_enable_records_approval_and_activates(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE, permissions={"env": ["ALPHA_TOKEN"]}))
    shown: list[Manifest] = []

    info = manager.enable("alpha", approve=lambda m: shown.append(m) or True)

    assert info.status == PluginStatus.ACTIVE
    assert shown[0].permissions.env == ["ALPHA_TOKEN"], "确认前应展示权限声明"
    assert [t.qualified_name for t in manager.build_registry().tools] == ["alpha__run"]
    state = json.loads(paths.state_file.read_text(encoding="utf-8"))
    assert state["plugins"]["alpha"]["approved"]["env"] == ["ALPHA_TOKEN"]


def test_enable_declined_keeps_plugin_disabled(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE))

    with pytest.raises(HarnessError, match="没有确认授权") as exc_info:
        manager.enable("alpha", approve=lambda m: False)

    assert "--yes" in (exc_info.value.hint or "")
    assert manager.get("alpha").status == PluginStatus.DISABLED


def test_enable_active_plugin_does_not_ask_again(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE))
    manager.enable("alpha", approve=approve_all)

    info = manager.enable("alpha", approve=lambda m: pytest.fail("不应再次请求确认"))

    assert info.status == PluginStatus.ACTIVE


def test_disable_revokes_approval_but_keeps_config(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE, config_schema=REQUIRED_CONFIG))
    manager.configure("alpha", set_values={"root": "/tmp"})
    manager.enable("alpha", approve=approve_all)

    info = manager.disable("alpha")

    assert info.status == PluginStatus.DISABLED
    assert info.approved is None
    assert info.config == {"root": "/tmp", "limit": 3}
    asked: list[str] = []
    manager.enable("alpha", approve=lambda m: asked.append(m.id) or True)
    assert asked == ["alpha"], "撤销授权后重新启用需要再次确认"


def test_changed_permissions_require_reapproval(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source, dev=True)
    manager.enable("alpha", approve=approve_all)
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    manifest["permissions"] = {"env": ["NEW_SECRET"]}
    (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    info = manager.get("alpha")

    assert info.status == PluginStatus.NEEDS_APPROVAL
    assert info.hint == "harness plugin enable alpha"
    assert manager.build_registry().tools == []
    assert manager.enable("alpha", approve=approve_all).status == PluginStatus.ACTIVE


# ---- 配置 ----


def test_enable_requires_valid_config(manager: PluginManager, write_plugin: WritePlugin) -> None:
    manager.install(write_plugin("alpha", CODE, config_schema=REQUIRED_CONFIG))

    info = manager.get("alpha")
    assert info.status == PluginStatus.DISABLED
    assert "root: 缺少必填配置项" in (info.reason or "")
    assert "config alpha --set" in (info.hint or "")
    with pytest.raises(HarnessError, match="root: 缺少必填配置项"):
        manager.enable("alpha", approve=lambda m: pytest.fail("配置不完整时不应请求确认"))

    assert manager.configure("alpha", set_values={"root": "/data"}) == {
        "root": "/data",
        "limit": 3,
    }
    assert manager.enable("alpha", approve=approve_all).status == PluginStatus.ACTIVE


def test_invalid_config_is_rejected_without_saving(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE, config_schema=REQUIRED_CONFIG))
    manager.configure("alpha", set_values={"root": "/data"})

    with pytest.raises(HarnessError, match="limit"):
        manager.configure("alpha", set_values={"limit": 0})
    with pytest.raises(HarnessError, match="unexpected"):
        manager.configure("alpha", set_values={"typo": 1})
    with pytest.raises(HarnessError, match="root: 缺少必填配置项"):
        manager.configure("alpha", unset=["root"])

    assert manager.configure("alpha") == {"root": "/data", "limit": 3}


def test_unset_restores_default(manager: PluginManager, write_plugin: WritePlugin) -> None:
    manager.install(write_plugin("alpha", CODE, config_schema=REQUIRED_CONFIG))
    manager.configure("alpha", set_values={"root": "/data", "limit": 9})

    assert manager.configure("alpha", unset=["limit"])["limit"] == 3
    with pytest.raises(HarnessError, match="没有设置过"):
        manager.configure("alpha", unset=["limit"])


def test_active_plugin_receives_effective_config(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE, config_schema=REQUIRED_CONFIG))
    manager.configure("alpha", set_values={"root": "/data"})
    manager.enable("alpha", approve=approve_all)

    [plugin] = manager.active_plugins()

    assert plugin.config == {"root": "/data", "limit": 3}
    assert plugin.data_dir == paths.plugin_data_dir("alpha")


# ---- 有效状态 ----


def test_state_survives_restart(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE))
    manager.install(write_plugin("beta", CODE))
    manager.enable("alpha", approve=approve_all)

    statuses = {info.id: info.status for info in reopen(paths).list_plugins()}

    assert statuses == {"alpha": PluginStatus.ACTIVE, "beta": PluginStatus.DISABLED}


def test_host_upgrade_can_make_plugin_incompatible(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE, harness_version=">=0.1,<1.0"))
    manager.enable("alpha", approve=approve_all)

    info = reopen(paths, version="1.0.0").get("alpha")

    assert info.status == PluginStatus.INCOMPATIBLE
    assert "需要 harness >=0.1,<1.0，当前为 1.0.0" in (info.reason or "")


def test_broken_plugin_is_reported_and_skipped(
    manager: PluginManager, write_plugin: WritePlugin, caplog: pytest.LogCaptureFixture
) -> None:
    good = write_plugin("alpha", CODE)
    broken = write_plugin("beta", CODE)
    for source in (good, broken):
        manager.install(source, dev=True)
        manager.enable(source.name, approve=approve_all)
    (broken / "plugin.json").write_text(json.dumps({"id": "beta"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        plugins = manager.active_plugins()

    assert [p.id for p in plugins] == ["alpha"]
    assert "跳过插件 beta" in caplog.text
    info = manager.get("beta")
    assert info.status == PluginStatus.BROKEN
    assert "校验失败" in (info.reason or "")
    with pytest.raises(HarnessError, match="无法启用"):
        manager.enable("beta", approve=approve_all)


def test_deleted_dev_source_is_broken(manager: PluginManager, write_plugin: WritePlugin) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source, dev=True)
    for child in source.iterdir():
        child.unlink()
    source.rmdir()

    info = manager.get("alpha")

    assert info.status == PluginStatus.BROKEN
    assert "插件目录不存在" in (info.reason or "")


def test_unknown_plugin_has_helpful_error(manager: PluginManager) -> None:
    with pytest.raises(HarnessError, match="未安装") as exc_info:
        manager.get("ghost")
    assert "plugin list" in (exc_info.value.hint or "")
    assert manager.find("../etc") is None


# ---- 卸载 ----


def test_uninstall_removes_package_and_keeps_data(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE))
    manager.enable("alpha", approve=approve_all)
    data_dir = paths.plugin_data_dir("alpha")
    data_dir.mkdir(parents=True)
    (data_dir / "db").write_text("x", encoding="utf-8")

    kept = manager.uninstall("alpha")

    assert kept == data_dir and (data_dir / "db").is_file()
    assert list(paths.packages_dir.iterdir()) == []
    assert manager.list_plugins() == []
    state = json.loads(paths.state_file.read_text(encoding="utf-8"))
    assert "alpha" not in state["plugins"]
    manager.install(write_plugin("alpha2", CODE, id="alpha"))
    assert manager.get("alpha").status == PluginStatus.DISABLED, "重新安装不继承旧的授权"


def test_uninstall_purge_deletes_data(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE))
    paths.plugin_data_dir("alpha").mkdir(parents=True)

    assert manager.uninstall("alpha", purge=True) is None
    assert not paths.plugin_data_dir("alpha").exists()


def test_uninstall_dev_plugin_keeps_source(
    manager: PluginManager, write_plugin: WritePlugin
) -> None:
    source = write_plugin("alpha", CODE)
    manager.install(source, dev=True)

    manager.uninstall("alpha")

    assert (source / "plugin.json").is_file(), "开发模式卸载不能删除用户源码"


# ---- 中断后的一致性（DESIGN.md §7.2）----


def test_leftovers_from_interrupted_operations_are_cleaned(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    manager.install(write_plugin("alpha", CODE))
    kept_package = Path(os.path.realpath(paths.plugins_dir / "alpha"))
    # 复制到一半中断：残留临时目录；重命名后、创建链接前中断：残留未被引用的副本
    (paths.packages_dir / ".tmp-deadbeef").mkdir()
    (paths.packages_dir / "beta-0.1.0-cafe").mkdir()
    # 创建临时链接后、替换前中断
    os.symlink(kept_package, paths.plugins_dir / ".tmp-alpha-0000")
    # 卸载时删除链接后、写回状态前中断：残留没有对应插件的记录
    state = json.loads(paths.state_file.read_text(encoding="utf-8"))
    state["plugins"]["ghost"] = {"enabled": True}
    paths.state_file.write_text(json.dumps(state), encoding="utf-8")

    manager.install(write_plugin("gamma", CODE))

    assert sorted(p.name for p in paths.plugins_dir.iterdir()) == ["alpha", "gamma"]
    packages = sorted(p.name.split("-")[0] for p in paths.packages_dir.iterdir())
    assert packages == ["alpha", "gamma"]
    assert kept_package.is_dir()
    state = json.loads(paths.state_file.read_text(encoding="utf-8"))
    assert sorted(state["plugins"]) == ["alpha", "gamma"]


def test_installed_plugin_without_state_record_is_disabled(
    manager: PluginManager, paths: HarnessPaths, write_plugin: WritePlugin
) -> None:
    """创建链接后、写入状态前中断：插件已安装但未启用，仍然是一致的状态。"""
    manager.install(write_plugin("alpha", CODE))
    paths.state_file.unlink()

    info = reopen(paths).get("alpha")

    assert info.status == PluginStatus.DISABLED
    assert reopen(paths).enable("alpha", approve=approve_all).status == PluginStatus.ACTIVE


def test_validate_reports_compatibility(manager: PluginManager, write_plugin: WritePlugin) -> None:
    manifest, reason = manager.validate(write_plugin("alpha", CODE, api_version=2))

    assert manifest.id == "alpha"
    assert reason is not None and "api_version=2" in reason
