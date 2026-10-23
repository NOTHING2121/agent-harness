"""插件注册表：把 active 插件的工具整理成 Agent 可用的工具表（DESIGN.md §8.1）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.plugins.manifest import Manifest, ToolSpec, qualified_tool_name


@dataclass(frozen=True, eq=False)
class ActivePlugin:
    """一个有效状态为 active、可以被调用的插件。"""

    manifest: Manifest
    path: Path
    """插件代码所在目录（plugins/<id> 链接指向的目录）。"""
    config: dict[str, Any]
    """已校验并补齐默认值的配置。"""
    data_dir: Path

    @property
    def id(self) -> str:
        return self.manifest.id


@dataclass(frozen=True, eq=False)
class RegisteredTool:
    qualified_name: str
    plugin: ActivePlugin
    spec: ToolSpec


class ToolRegistry:
    """每次运行开始时由 active 插件构建，按 <plugin_id>__<tool_name> 查找工具。"""

    def __init__(self, plugins: Iterable[ActivePlugin] = ()) -> None:
        self._plugins: dict[str, ActivePlugin] = {}
        self._tools: dict[str, RegisteredTool] = {}
        for plugin in plugins:
            if plugin.id in self._plugins:
                raise ValueError(f"插件 ID 重复：{plugin.id}")
            self._plugins[plugin.id] = plugin
            for spec in plugin.manifest.tools:
                name = qualified_tool_name(plugin.id, spec.name)
                self._tools[name] = RegisteredTool(qualified_name=name, plugin=plugin, spec=spec)

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def plugins(self) -> list[ActivePlugin]:
        return list(self._plugins.values())

    @property
    def tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def get(self, qualified_name: str) -> RegisteredTool | None:
        return self._tools.get(qualified_name)

    def to_llm_tools(self) -> list[dict[str, Any]]:
        """导出 OpenAI Chat Completions 的函数工具定义，描述前加上插件名。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.qualified_name,
                    "description": f"[{tool.plugin.manifest.name}] {tool.spec.description}",
                    "parameters": tool.spec.input_schema,
                },
            }
            for tool in self._tools.values()
        ]
