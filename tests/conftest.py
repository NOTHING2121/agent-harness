"""测试公共 fixture。所有测试都使用临时目录，不读取开发者本机的 Harness 配置。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_harness.plugins.manifest import Manifest
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
