"""ToolExecutor：一次工具调用的完整链路（DESIGN.md §8.3）。

查找 → 参数校验 → （如需）用户确认 → PluginRunner 执行 → ToolResult。
插件错误一律转成 ToolResult，不向 Agent Loop 抛出。大结果处理（§8.4）在 M3 加入。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, model_validator

from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.registry import RegisteredTool, ToolRegistry
from agent_harness.plugins.runner import PluginRunner

ConfirmCallback = Callable[[RegisteredTool, dict[str, Any]], bool]
"""用户确认回调：返回 True 表示允许执行该工具调用。"""

MAX_REPORTED_SCHEMA_ERRORS = 5


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


def parse_arguments(raw: str, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """解析 LLM 给出的参数 JSON 并按 input_schema 校验；返回 (参数, 错误描述)。"""
    try:
        arguments = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return None, f"参数不是合法的 JSON：{exc.msg}（第 {exc.pos} 个字符）"
    if not isinstance(arguments, dict):
        return None, "参数必须是 JSON 对象"
    errors = list(Draft202012Validator(schema).iter_errors(arguments))
    if errors:
        details = "；".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '(根)'}: {error.message}"
            for error in errors[:MAX_REPORTED_SCHEMA_ERRORS]
        )
        return None, f"参数不符合 input_schema：{details}"
    return arguments, None


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
        tool = self._registry.get(name)
        if tool is None:
            available = ", ".join(t.qualified_name for t in self._registry.tools) or "无"
            return _failure(
                call_id,
                name,
                ErrorCode.UNKNOWN_TOOL,
                f"工具 {name} 不存在。",
                hint=f"当前可用的工具：{available}",
            )

        arguments, problem = parse_arguments(raw_arguments, tool.spec.input_schema)
        if arguments is None:
            return _failure(
                call_id,
                name,
                ErrorCode.INVALID_ARGUMENTS,
                problem or "参数不合法",
                hint="按工具的 input_schema 修正参数后重试。",
            )

        if tool.spec.requires_confirmation and not (
            self._confirm is not None and self._confirm(tool, arguments)
        ):
            return _failure(
                call_id,
                name,
                ErrorCode.USER_DENIED,
                f"用户没有允许执行 {name}。",
                hint="该工具每次调用都需要确认；非交互环境中可使用 --yes。",
            )

        outcome = self._runner.call(tool.plugin, tool.spec, arguments, call_id=call_id)
        return ToolResult(
            call_id=call_id,
            tool=name,
            ok=outcome.ok,
            content=outcome.content,
            display=outcome.display,
            error=outcome.error,
            stderr_tail=outcome.stderr_tail,
            duration_ms=outcome.duration_ms,
        )


def _failure(
    call_id: str, tool: str, code: ErrorCode, message: str, *, hint: str | None = None
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool=tool,
        ok=False,
        error=ErrorInfo(code=code, message=message, hint=hint),
    )
