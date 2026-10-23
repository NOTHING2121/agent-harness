"""ToolExecutor：一次工具调用的完整链路（DESIGN.md §8.3）。

查找 → 参数校验 → （如需）用户确认 → PluginRunner 执行 → 大结果处理（§8.4）→ ToolResult。
插件错误一律转成 ToolResult，不向 Agent Loop 抛出。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, model_validator

from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.registry import RegisteredTool, ToolRegistry
from agent_harness.plugins.runner import PluginRunner

ConfirmCallback = Callable[[RegisteredTool, dict[str, Any]], bool]
"""用户确认回调：返回 True 表示允许执行该工具调用。"""

MAX_REPORTED_SCHEMA_ERRORS = 5
TRUNCATION_HINT = "结果过长，已截断。请用分页参数或更小的范围重新调用以获取其余内容。"

logger = logging.getLogger(__name__)


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
    truncated: bool = False
    """结果超过 tool_result_max_chars：交给模型的是 preview，完整结果保留在 content 中。"""
    original_chars: int | None = None
    preview: str | None = None
    full_result_path: str | None = None
    """截断时完整结果的保存位置（只在 Agent 运行中保存）。"""

    @model_validator(mode="after")
    def _check_error(self) -> ToolResult:
        if self.ok == (self.error is not None):
            raise ValueError("ok 为 False 时必须提供 error，ok 为 True 时不能提供 error")
        return self

    def for_llm(self) -> str:
        """交给 LLM 的内容：结果（过长时为明确标注的截断版本），或错误码、消息与建议。"""
        if self.ok and self.truncated:
            payload: dict[str, Any] = {
                "ok": True,
                "truncated": True,
                "original_chars": self.original_chars,
                "preview": self.preview,
                "hint": TRUNCATION_HINT,
            }
        elif self.ok:
            payload = {"ok": True, "content": self.content}
        else:
            assert self.error is not None  # 由 _check_error 保证
            error = {"code": self.error.code.value, "message": self.error.message}
            if self.error.hint:
                error["hint"] = self.error.hint
            payload = {"ok": False, "error": error}
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
        results_dir: Path | None = None,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._max_result_chars = max_result_chars
        self._confirm = confirm
        self._results_dir = results_dir

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
        result = ToolResult(
            call_id=call_id,
            tool=name,
            ok=outcome.ok,
            content=outcome.content,
            display=outcome.display,
            error=outcome.error,
            stderr_tail=outcome.stderr_tail,
            duration_ms=outcome.duration_ms,
        )
        return self._limit_size(result) if result.ok else result

    def _limit_size(self, result: ToolResult) -> ToolResult:
        """结果过长时给模型一个明确标注的截断版本，完整结果保存到 results_dir（§8.4）。"""
        text = json.dumps(result.content, ensure_ascii=False)
        if len(text) <= self._max_result_chars:
            return result
        saved: str | None = None
        if self._results_dir is not None:
            # call_id 来自模型，只保留安全字符，避免写到 results_dir 之外。
            safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", result.call_id).lstrip(".") or "call"
            path = self._results_dir / f"{safe_id}.json"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                saved = str(path)
            except OSError as exc:
                logger.warning("保存完整结果到 %s 失败：%s", path, exc)
        return result.model_copy(
            update={
                "truncated": True,
                "original_chars": len(text),
                "preview": text[: self._max_result_chars],
                "full_result_path": saved,
            }
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
