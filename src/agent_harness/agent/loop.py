"""Agent Loop（DESIGN.md §8.2）。

只依赖 LLMClient、ToolRegistry 和 Executor 接口，不知道插件进程的存在，也不包含任何具体插件。
"""

from __future__ import annotations

import json
import time
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from agent_harness.agent.llm import ChatMessage, LLMClient
from agent_harness.errors import LLMError
from agent_harness.plugins.executor import ToolResult
from agent_harness.plugins.registry import ToolRegistry
from agent_harness.trace import EventType, Trace

RunStatus = Literal["completed", "failed", "max_steps_exceeded", "cancelled"]

RESULT_PREVIEW_CHARS = 500
"""追踪事件中结果摘要的长度。只用于展示；完整结果仍交给模型，并保留在 RunResult 中。"""

SYSTEM_PROMPT = (
    "你是运行在本地 Agent Harness 中的助手。需要时调用工具完成任务，工具由用户安装的插件提供。"
    "工具调用失败时，结果中会包含错误码和原因，请据此调整参数或换一种方式，不要编造工具结果。"
)


class Executor(Protocol):
    """执行一次工具调用；插件错误以 ToolResult 返回，不抛出异常。"""

    def execute(self, call_id: str, name: str, raw_arguments: str) -> ToolResult: ...


class RunResult(BaseModel):
    run_id: str
    status: RunStatus
    steps: int
    final_answer: str | None = None
    error: str | None = None
    hint: str | None = None
    tool_results: list[ToolResult] = Field(default_factory=list)


def build_system_prompt(registry: ToolRegistry) -> str:
    """系统提示词完全由注册表内容生成，不包含写死的插件信息。"""
    plugins = registry.plugins
    if not plugins:
        return f"{SYSTEM_PROMPT}\n\n当前没有可用的插件工具，请直接回答用户。"
    lines = [SYSTEM_PROMPT, "", "当前可用的插件："]
    lines += [f"- {p.manifest.name}（{p.id}）：{p.manifest.description}" for p in plugins]
    return "\n".join(lines)


def summarize_result(result: ToolResult) -> tuple[str | None, int | None]:
    """返回 (结果摘要, 结果序列化后的总字符数)；失败的调用没有结果。"""
    if not result.ok:
        return None, None
    text = json.dumps(result.content, ensure_ascii=False)
    return text[:RESULT_PREVIEW_CHARS], len(text)


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        executor: Executor,
        *,
        max_steps: int,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于等于 1")
        self._llm = llm
        self._registry = registry
        self._executor = executor
        self._max_steps = max_steps

    def run(self, prompt: str, trace: Trace) -> RunResult:
        tools = self._registry.to_llm_tools()
        messages = [
            ChatMessage(role="system", content=build_system_prompt(self._registry)),
            ChatMessage(role="user", content=prompt),
        ]
        results: list[ToolResult] = []
        trace.emit(
            EventType.RUN_STARTED,
            prompt=prompt,
            model=self._llm.model,
            tools=[tool.qualified_name for tool in self._registry.tools],
        )
        step = 0
        try:
            while step < self._max_steps:
                step += 1
                started = time.monotonic()
                try:
                    response = self._llm.complete(messages, tools)
                except LLMError as exc:
                    return self._finish(
                        trace, "failed", step, results, error=exc.message, hint=exc.hint
                    )
                messages.append(response.message)
                trace.emit(
                    EventType.LLM_RESPONSE,
                    step=step,
                    content=response.content,
                    tool_calls=[call.model_dump() for call in response.tool_calls],
                    usage=response.usage.model_dump() if response.usage else None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                if not response.tool_calls:
                    return self._finish(
                        trace, "completed", step, results, final_answer=response.content
                    )
                for call in response.tool_calls:
                    trace.emit(
                        EventType.TOOL_STARTED,
                        call_id=call.id,
                        tool=call.name,
                        arguments=call.arguments,
                    )
                    result = self._executor.execute(call.id, call.name, call.arguments)
                    results.append(result)
                    preview, total_chars = summarize_result(result)
                    trace.emit(
                        EventType.TOOL_FINISHED,
                        call_id=call.id,
                        tool=call.name,
                        ok=result.ok,
                        display=result.display,
                        result_preview=preview,
                        result_chars=total_chars,
                        error=result.error.model_dump(mode="json") if result.error else None,
                        stderr_tail=result.stderr_tail,
                        duration_ms=result.duration_ms,
                        truncated=result.truncated,
                        full_result_path=result.full_result_path,
                    )
                    messages.append(
                        ChatMessage(role="tool", tool_call_id=call.id, content=result.for_llm())
                    )
            return self._finish(
                trace,
                "max_steps_exceeded",
                step,
                results,
                error=f"已达到最大步数 {self._max_steps}，模型仍在调用工具。",
            )
        except KeyboardInterrupt:
            return self._finish(trace, "cancelled", step, results, error="运行被用户中断。")

    @staticmethod
    def _finish(
        trace: Trace,
        status: RunStatus,
        steps: int,
        results: list[ToolResult],
        *,
        final_answer: str | None = None,
        error: str | None = None,
        hint: str | None = None,
    ) -> RunResult:
        trace.emit(
            EventType.RUN_FINISHED,
            status=status,
            steps=steps,
            final_answer=final_answer,
            error=error,
            hint=hint,
        )
        return RunResult(
            run_id=trace.run_id,
            status=status,
            steps=steps,
            final_answer=final_answer,
            error=error,
            hint=hint,
            tool_results=results,
        )
