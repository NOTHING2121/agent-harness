"""PluginRunner：为一次工具调用启动插件进程并判定结果（DESIGN.md §5）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_harness.errors import ErrorInfo, not_implemented
from agent_harness.plugins.manifest import ToolSpec
from agent_harness.plugins.registry import ActivePlugin
from agent_harness.settings import PluginSettings


@dataclass(frozen=True)
class RunOutcome:
    """一次插件进程调用的结果，由 ToolExecutor 整理为 ToolResult。"""

    ok: bool
    content: Any = None
    display: str | None = None
    error: ErrorInfo | None = None
    stderr_tail: str | None = None
    duration_ms: int = 0


class PluginRunner:
    """每次调用启动一个短生命周期进程：写请求、读响应、超时终止进程组。"""

    def __init__(self, settings: PluginSettings) -> None:
        self._settings = settings

    def call(
        self,
        plugin: ActivePlugin,
        tool: ToolSpec,
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> RunOutcome:
        """执行一次工具调用。插件的任何异常行为都以 RunOutcome 返回，不向上抛出。"""
        raise not_implemented("PluginRunner.call", "M1")
