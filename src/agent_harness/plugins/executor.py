"""ToolExecutor：一次工具调用的完整链路（DESIGN.md §8.3）。

查找 → 参数校验 → （如需）用户确认 → PluginRunner 执行 → 大结果处理 → ToolResult。
插件错误一律转成 ToolResult，不向 Agent Loop 抛出。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, model_validator

from agent_harness.errors import ErrorInfo, not_implemented
from agent_harness.plugins.registry import RegisteredTool, ToolRegistry
from agent_harness.plugins.runner import PluginRunner

ConfirmCallback = Callable[[RegisteredTool, dict[str, Any]], bool]
"""用户确认回调：返回 True 表示允许执行该工具调用。"""


class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool
    content: Any = None
    display: str | None = None
    error: ErrorInfo | None = None
    stderr_tail: str | None = None
    """只用于展示和追踪，不发给 LLM。"""
    duration_ms: int = 0

    @model_validator(mode="after")
    def _check_error(self) -> ToolResult:
        if self.ok == (self.error is not None):
            raise ValueError("ok 为 False 时必须提供 error，ok 为 True 时不能提供 error")
        return self

    def for_llm(self) -> str:
        """交给 LLM 的内容：只有结果，或错误码与消息。"""
        if self.ok:
            payload: dict[str, Any] = {"ok": True, "content": self.content}
        else:
            assert self.error is not None  # 由 _check_error 保证
            payload = {
                "ok": False,
                "error": {"code": self.error.code.value, "message": self.error.message},
            }
        return json.dumps(payload, ensure_ascii=False)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        runner: PluginRunner,
        *,
        max_result_chars: int,
        confirm: ConfirmCallback | None = None,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._max_result_chars = max_result_chars
        self._confirm = confirm

    def execute(self, call_id: str, name: str, raw_arguments: str) -> ToolResult:
        """执行 LLM 发起的一次工具调用；raw_arguments 是 LLM 给出的原始 JSON 字符串。"""
        raise not_implemented("ToolExecutor.execute", "M1")
