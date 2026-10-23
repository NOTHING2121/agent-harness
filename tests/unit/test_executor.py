"""ToolExecutor 单元测试：用假的 runner，只验证查找、参数校验、确认与结果整理。"""

from collections.abc import Callable
from typing import Any

import pytest

from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.executor import ToolExecutor, parse_arguments
from agent_harness.plugins.registry import ActivePlugin, ToolRegistry
from agent_harness.plugins.runner import RunOutcome

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "count": {"type": "integer", "minimum": 1}},
    "required": ["text"],
    "additionalProperties": False,
}


class FakeRunner:
    def __init__(self, outcome: RunOutcome | None = None) -> None:
        self.outcome = outcome or RunOutcome(ok=True, content={"echo": "hi"}, duration_ms=5)
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []

    def call(self, plugin, tool, arguments, *, call_id):
        self.calls.append((plugin.id, tool.name, arguments, call_id))
        return self.outcome


@pytest.fixture
def registry(make_plugin: Callable[..., ActivePlugin]) -> ToolRegistry:
    plugin = make_plugin(
        tools=[
            {"name": "echo", "description": "echo", "input_schema": SCHEMA},
            {
                "name": "delete",
                "description": "delete",
                "input_schema": {"type": "object"},
                "requires_confirmation": True,
            },
        ]
    )
    return ToolRegistry([plugin])


def make_executor(registry: ToolRegistry, runner: FakeRunner, confirm=None) -> ToolExecutor:
    return ToolExecutor(registry, runner, max_result_chars=20_000, confirm=confirm)  # type: ignore[arg-type]


def test_successful_call_is_forwarded_to_runner(registry: ToolRegistry) -> None:
    runner = FakeRunner()

    result = make_executor(registry, runner).execute("c1", "demo__echo", '{"text": "hi"}')

    assert result.ok
    assert result.content == {"echo": "hi"}
    assert result.duration_ms == 5
    assert runner.calls == [("demo", "echo", {"text": "hi"}, "c1")]


def test_unknown_tool_lists_available_tools(registry: ToolRegistry) -> None:
    runner = FakeRunner()

    result = make_executor(registry, runner).execute("c1", "demo__nope", "{}")

    assert result.error is not None
    assert result.error.code == ErrorCode.UNKNOWN_TOOL
    assert "demo__echo" in (result.error.hint or "")
    assert runner.calls == []


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("{not json", "不是合法的 JSON"),
        ("[1]", "必须是 JSON 对象"),
        ("{}", "'text' is a required property"),
        ('{"text": "a", "count": 0}', "count:"),
        ('{"text": "a", "extra": 1}', "Additional properties"),
    ],
)
def test_invalid_arguments_are_rejected_before_running(
    registry: ToolRegistry, raw: str, fragment: str
) -> None:
    runner = FakeRunner()

    result = make_executor(registry, runner).execute("c1", "demo__echo", raw)

    assert result.error is not None
    assert result.error.code == ErrorCode.INVALID_ARGUMENTS
    assert fragment in result.error.message
    assert runner.calls == []


def test_empty_arguments_mean_empty_object() -> None:
    assert parse_arguments("  ", {"type": "object"}) == ({}, None)


@pytest.mark.parametrize("confirm", [None, lambda tool, args: False])
def test_confirmation_required_and_denied(registry: ToolRegistry, confirm) -> None:
    runner = FakeRunner()

    result = make_executor(registry, runner, confirm).execute("c1", "demo__delete", "{}")

    assert result.error is not None
    assert result.error.code == ErrorCode.USER_DENIED
    assert runner.calls == []


def test_confirmation_granted(registry: ToolRegistry) -> None:
    runner = FakeRunner()
    seen: list[str] = []

    def confirm(tool, arguments) -> bool:
        seen.append(tool.qualified_name)
        return True

    result = make_executor(registry, runner, confirm).execute("c1", "demo__delete", "{}")

    assert result.ok
    assert seen == ["demo__delete"]


def test_runner_failure_becomes_tool_result(registry: ToolRegistry) -> None:
    error = ErrorInfo(code=ErrorCode.PLUGIN_TIMEOUT, message="超时")
    runner = FakeRunner(RunOutcome(ok=False, error=error, stderr_tail="trace", duration_ms=9))

    result = make_executor(registry, runner).execute("c1", "demo__echo", '{"text": "x"}')

    assert not result.ok
    assert result.error == error
    assert result.stderr_tail == "trace"
    assert '"PLUGIN_TIMEOUT"' in result.for_llm()
    assert "trace" not in result.for_llm()
