"""测试公共 fixture。所有测试都使用临时目录，不读取开发者本机的 Harness 配置。"""

from __future__ import annotations

import copy
import json
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_harness.plugins.manifest import Manifest, default_config, load_manifest
from agent_harness.plugins.registry import ActivePlugin

HARNESS_ENV_VARS = (
    "HARNESS_HOME",
    "HARNESS_LLM_PROVIDER",
    "HARNESS_LLM_MODEL",
    "HARNESS_LLM_BASE_URL",
)

BASE_MANIFEST: dict[str, Any] = {
    "api_version": 1,
    "id": "demo",
    "name": "Demo",
    "version": "0.1.0",
    "description": "测试用插件",
    "entry": "main.py",
    "tools": [
        {
            "name": "echo",
            "description": "原样返回文本",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ],
}


@pytest.fixture(autouse=True)
def _isolate_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "harness-home"
    monkeypatch.setenv("HARNESS_HOME", str(home))
    return home


@pytest.fixture
def make_manifest_data() -> Callable[..., dict[str, Any]]:
    """返回清单字典的工厂，关键字参数覆盖顶层字段。"""

    def factory(**overrides: Any) -> dict[str, Any]:
        data = copy.deepcopy(BASE_MANIFEST)
        data.update(overrides)
        return data

    return factory


@pytest.fixture
def make_plugin(
    tmp_path: Path, make_manifest_data: Callable[..., dict[str, Any]]
) -> Callable[..., ActivePlugin]:
    """返回 ActivePlugin 的工厂，关键字参数覆盖清单的顶层字段。"""

    def factory(**overrides: Any) -> ActivePlugin:
        manifest = Manifest.model_validate(make_manifest_data(**overrides))
        return ActivePlugin(
            manifest=manifest,
            path=tmp_path / "plugins" / manifest.id,
            config={},
            data_dir=tmp_path / "data" / manifest.id,
        )

    return factory


@pytest.fixture
def write_plugin(tmp_path: Path) -> Callable[..., Path]:
    """在临时目录中生成插件：plugin.json 由参数构造，main.py 为给定代码。

    tools 中每项可以是工具名字符串，也可以是完整的工具声明字典。
    """

    def factory(
        plugin_id: str,
        code: str,
        *,
        tools: list[str | dict[str, Any]] | None = None,
        **manifest_overrides: Any,
    ) -> Path:
        plugin_dir = tmp_path / "sources" / plugin_id
        plugin_dir.mkdir(parents=True)
        tool_specs = [
            item
            if isinstance(item, dict)
            else {"name": item, "description": f"{item} 工具", "input_schema": {"type": "object"}}
            for item in (tools or ["run"])
        ]
        manifest = {
            "api_version": 1,
            "id": plugin_id,
            "name": plugin_id.title(),
            "version": "0.1.0",
            "description": f"{plugin_id} 测试插件",
            "entry": "main.py",
            "tools": tool_specs,
            **manifest_overrides,
        }
        (plugin_dir / "plugin.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        (plugin_dir / "main.py").write_text(textwrap.dedent(code), encoding="utf-8")
        return plugin_dir

    return factory


@pytest.fixture
def load_active_plugin(tmp_path: Path) -> Callable[[Path], ActivePlugin]:
    """把插件目录加载为 ActivePlugin（使用默认配置和临时数据目录）。"""

    def factory(plugin_dir: Path) -> ActivePlugin:
        manifest = load_manifest(plugin_dir)
        return ActivePlugin(
            manifest=manifest,
            path=plugin_dir.resolve(),
            config=default_config(manifest),
            data_dir=tmp_path / "data" / manifest.id,
        )

    return factory
