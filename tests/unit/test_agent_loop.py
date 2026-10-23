"""用离线 fake LM（ScriptedClient）验证 Agent Loop 与 LLM、注册表、执行器之间的接口。"""

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from agent_harness.agent.llm import ChatMessage, LLMResponse, Script, ScriptedClient
from agent_harness.agent.loop import AgentLoop
from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.executor import ToolResult
from agent_harness.plugins.registry import ActivePlugin, ToolRegistry
from agent_harness.trace import EventType, Trace


class RecordingLLM(ScriptedClient):
    """在 ScriptedClient 的基础上记录每次收到的消息和工具定义。"""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__(Script.model_validate({"steps": steps}))
        self.requests: list[list[ChatMessage]] = []
        self.tools: list[dict[str, Any]] = []

    def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        self.requests.append(list(messages))
        self.tools = list(tools)
        return super().complete(messages, tools)


class FakeExecutor:
    """记录调用；名字在 failing 中的工具返回 TOOL_ERROR，其余原样返回参数。"""

    def __init__(self, failing: Sequence[str] = ()) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._failing = set(failing)

    def execute(self, call_id: str, name: str, raw_arguments: str) -> ToolResult:
        self.calls.append((call_id, name, raw_arguments))
        if name in self._failing:
            error = ErrorInfo(code=ErrorCode.TOOL_ERROR, message="模拟失败")
            return ToolResult(call_id=call_id, tool=name, ok=False, error=error)
        return ToolResult(call_id=call_id, tool=name, ok=True, content=json.loads(raw_arguments))


@pytest.fixture
def registry(make_plugin: Callable[..., ActivePlugin]) -> ToolRegistry:
    return ToolRegistry([make_plugin()])


def echo_step(text: str) -> dict[str, Any]:
    return {"tool_calls": [{"name": "demo__echo", "arguments": {"text": text}}]}


def test_answer_without_tools(registry: ToolRegistry) -> None:
    llm = RecordingLLM([{"content": "你好！"}])
    executor = FakeExecutor()
    trace = Trace()

    result = AgentLoop(llm, registry, executor, max_steps=5).run("你好", trace)

    assert result.status == "completed"
    assert result.final_answer == "你好！"
    assert result.steps == 1
    assert result.run_id == trace.run_id
    assert executor.calls == []
    assert [event.type for event in trace.events] == [
        EventType.RUN_STARTED,
        EventType.LLM_RESPONSE,
        EventType.RUN_FINISHED,
    ]


def test_llm_receives_tools_and_prompt_from_registry(registry: ToolRegistry) -> None:
    llm = RecordingLLM([{"content": "好的"}])

    AgentLoop(llm, registry, FakeExecutor(), max_steps=5).run("hi", Trace())

    system, user = llm.requests[0]
    assert system.role == "system"
    assert "Demo（demo）：测试用插件" in (system.content or "")
    assert user.content == "hi"
    assert [tool["function"]["name"] for tool in llm.tools] == ["demo__echo"]


def test_empty_registry_tells_model_no_tools() -> None:
    llm = RecordingLLM([{"content": "好的"}])

    AgentLoop(llm, ToolRegistry(), FakeExecutor(), max_steps=5).run("hi", Trace())

    assert "当前没有可用的插件工具" in (llm.requests[0][0].content or "")
    assert llm.tools == []


def test_tool_result_is_sent_back_to_llm(registry: ToolRegistry) -> None:
    llm = RecordingLLM([echo_step("hi"), {"content": "完成"}])
    executor = FakeExecutor()
    trace = Trace()

    result = AgentLoop(llm, registry, executor, max_steps=5).run("复述 hi", trace)

    assert result.status == "completed"
    assert result.steps == 2
    assert executor.calls == [("call_1_1", "demo__echo", '{"text": "hi"}')]
    assistant, tool_message = llm.requests[1][-2:]
    assert assistant.tool_calls[0].id == "call_1_1"
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call_1_1"
    assert json.loads(tool_message.content or "") == {"ok": True, "content": {"text": "hi"}}
    assert [r.ok for r in result.tool_results] == [True]
    assert EventType.TOOL_FINISHED in [event.type for event in trace.events]


def test_tool_failure_is_returned_to_model(registry: ToolRegistry) -> None:
    llm = RecordingLLM([echo_step("x"), {"content": "工具失败了，换个方式回答"}])
    executor = FakeExecutor(failing=["demo__echo"])

    result = AgentLoop(llm, registry, executor, max_steps=5).run("试试", Trace())

    assert result.status == "completed"
    tool_message = llm.requests[1][-1]
    assert json.loads(tool_message.content or "")["error"]["code"] == "TOOL_ERROR"
    assert result.tool_results[0].ok is False


def test_stops_at_max_steps(registry: ToolRegistry) -> None:
    llm = RecordingLLM([echo_step("1"), echo_step("2"), echo_step("3")])

    result = AgentLoop(llm, registry, FakeExecutor(), max_steps=2).run("循环", Trace())

    assert result.status == "max_steps_exceeded"
    assert result.steps == 2
    assert len(llm.requests) == 2


def test_llm_error_fails_the_run(registry: ToolRegistry) -> None:
    llm = RecordingLLM([echo_step("1")])
    trace = Trace()

    result = AgentLoop(llm, registry, FakeExecutor(), max_steps=5).run("hi", trace)

    assert result.status == "failed"
    assert "步骤已全部用完" in (result.error or "")
    assert result.hint is not None
    assert trace.events[-1].payload["status"] == "failed"
    assert trace.events[-1].payload["hint"] == result.hint


def test_max_steps_must_be_positive(registry: ToolRegistry) -> None:
    with pytest.raises(ValueError):
        AgentLoop(RecordingLLM([{"content": "x"}]), registry, FakeExecutor(), max_steps=0)


def test_trace_events_carry_design_fields(registry: ToolRegistry) -> None:
    llm = RecordingLLM([echo_step("x" * 600), {"content": "完成"}])
    trace = Trace()

    AgentLoop(llm, registry, FakeExecutor(), max_steps=5).run("hi", trace)

    llm_event = next(e for e in trace.events if e.type == EventType.LLM_RESPONSE)
    assert isinstance(llm_event.payload["duration_ms"], int)
    finished = next(e for e in trace.events if e.type == EventType.TOOL_FINISHED)
    full = json.dumps({"text": "x" * 600}, ensure_ascii=False)
    assert finished.payload["result_chars"] == len(full)
    assert finished.payload["result_preview"] == full[:500]
    assert "stderr_tail" in finished.payload


def test_failed_tool_has_no_result_preview(registry: ToolRegistry) -> None:
    llm = RecordingLLM([echo_step("x"), {"content": "完成"}])
    trace = Trace()

    AgentLoop(llm, registry, FakeExecutor(failing=["demo__echo"]), max_steps=5).run("hi", trace)

    finished = next(e for e in trace.events if e.type == EventType.TOOL_FINISHED)
    assert finished.payload["result_preview"] is None
    assert finished.payload["result_chars"] is None
    assert finished.payload["error"]["code"] == "TOOL_ERROR"
