"""M4：update、discover、plugin new，以及“新增第三个插件不需要修改宿主”。"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_harness.app import Harness
from agent_harness.cli import main as cli_main
from agent_harness.errors import HarnessError
from agent_harness.plugins.manager import InstallStatus, PluginStatus

runner = CliRunner()
CODE = """\
from harness_sdk import Plugin

plugin = Plugin()


@plugin.tool("run")
def run(ctx):
    return {"ok": True}


if __name__ == "__main__":
    plugin.run()
"""


def bump(plugin_dir: Path, version: str, **changes: object) -> None:
    manifest_path = plugin_dir / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(version=version, **changes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def harness(harness_home: Path) -> Harness:
    return Harness.open()


def approve_all(manifest: object) -> bool:
    return True


def test_update_switches_version_and_keeps_user_intent(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    source = write_plugin("demo", CODE)
    harness.install_plugin(source)
    harness.enable_plugin("demo", approve=approve_all)
    old_package = harness.plugin_info("demo").path

    bump(source, "0.2.0")
    info, old_version = harness.update_plugin("demo")

    assert (old_version, info.manifest.version) == ("0.1.0", "0.2.0")
    assert info.status is PluginStatus.ACTIVE, "权限声明没变，启用状态保留"
    assert not old_package.exists(), "旧副本已删除"
    assert harness.call_tool("demo__run", "{}").ok


def test_update_requires_higher_version(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    source = write_plugin("demo", CODE)
    harness.install_plugin(source)

    with pytest.raises(HarnessError, match="不高于"):
        harness.update_plugin("demo")
    assert harness.plugin_info("demo").manifest.version == "0.1.0"


def test_update_with_new_permissions_needs_approval(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    source = write_plugin("demo", CODE)
    harness.install_plugin(source)
    harness.enable_plugin("demo", approve=approve_all)

    bump(source, "0.2.0", permissions={"network": True})
    info, _ = harness.update_plugin("demo")

    assert info.status is PluginStatus.NEEDS_APPROVAL
    assert harness.tools().get("demo__run") is None
    assert harness.enable_plugin("demo", approve=approve_all).status is PluginStatus.ACTIVE


def test_update_from_other_source_and_dev_mode_refusal(
    harness: Harness, write_plugin: Callable[..., Path], tmp_path: Path
) -> None:
    source = write_plugin("demo", CODE)
    harness.install_plugin(source)
    other = tmp_path / "other"
    other.mkdir()
    (other / "main.py").write_text(CODE, encoding="utf-8")
    (other / "plugin.json").write_text((source / "plugin.json").read_text(), encoding="utf-8")
    bump(other, "1.0.0")

    info, _ = harness.update_plugin("demo", source=other)
    assert info.manifest.version == "1.0.0"
    assert info.source == str(other.resolve())

    dev_source = write_plugin("devdemo", CODE)
    harness.install_plugin(dev_source, dev=True)
    with pytest.raises(HarnessError, match="开发模式"):
        harness.update_plugin("devdemo")


def test_update_rejects_incompatible_new_version(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    source = write_plugin("demo", CODE)
    harness.install_plugin(source)

    bump(source, "0.2.0", harness_version=">=99")
    with pytest.raises(HarnessError, match="不兼容"):
        harness.update_plugin("demo")


def test_discover_reports_install_status(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    installed = write_plugin("aaa", CODE)
    write_plugin("bbb", CODE)
    broken = write_plugin("ccc", CODE)
    (broken / "plugin.json").write_text("{not json", encoding="utf-8")
    harness.install_plugin(installed)
    bump(installed, "0.3.0")

    found = {item.path.name: item for item in harness.discover_plugins(installed.parent)}

    assert found["aaa"].install_status is InstallStatus.UPGRADABLE
    assert found["aaa"].installed_version == "0.1.0"
    assert found["bbb"].install_status is InstallStatus.NOT_INSTALLED
    assert found["ccc"].manifest is None and "不是合法的 JSON" in (found["ccc"].error or "")


def test_new_plugin_is_a_third_plugin_without_host_changes(
    harness_home: Path, tmp_path: Path
) -> None:
    """plugin new 生成的插件走同一套 validate → install → enable → call → run 流程。"""

    def cli(*args: str) -> str:
        result = runner.invoke(cli_main.app, list(args))
        assert result.exit_code == 0, (result.output, result.exception)
        return result.output

    cli("plugin", "new", "hello-world", "--dir", str(tmp_path))
    plugin_dir = tmp_path / "hello-world"
    assert {p.name for p in plugin_dir.iterdir()} == {"plugin.json", "main.py", "test_main.py"}

    cli("plugin", "validate", str(plugin_dir))
    cli("plugin", "install", str(plugin_dir))
    cli("plugin", "enable", "hello-world", "--yes")
    assert "Hello, Ada!" in cli("tool", "call", "hello-world__greet", "--args", '{"name": "Ada"}')

    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            {
                "steps": [
                    {"tool_calls": [{"name": "hello-world__greet", "arguments": {"name": "Bo"}}]},
                    {"content": "打过招呼了"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert "打过招呼了" in cli("run", "打个招呼", "--script", str(script))

    # 生成的单元测试本身也能运行。
    spec = importlib.util.spec_from_file_location("generated_test", plugin_dir / "test_main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_greet(tmp_path)


def test_new_plugin_rejects_bad_id_and_existing_dir(harness: Harness, tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="不合法"):
        harness.new_plugin("Bad_Id", tmp_path)
    harness.new_plugin("dup", tmp_path)
    with pytest.raises(HarnessError, match="已存在"):
        harness.new_plugin("dup", tmp_path)
