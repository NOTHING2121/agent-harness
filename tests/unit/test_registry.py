import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.executor import ToolResult
from agent_harness.plugins.registry import ActivePlugin, ToolRegistry

PluginFactory = Callable[..., ActivePlugin]


def test_empty_registry() -> None:
    registry = ToolRegistry()

    assert len(registry) == 0
    assert registry.plugins == []
    assert registry.to_llm_tools() == []


def test_registry_exposes_qualified_tools(make_plugin: PluginFactory) -> None:
    plugin = make_plugin()
    registry = ToolRegistry([plugin])

    tool = registry.get("demo__echo")
    assert tool is not None
    assert tool.plugin is plugin
    assert tool.spec.name == "echo"
    assert registry.get("demo__missing") is None
    assert registry.to_llm_tools() == [
        {
            "type": "function",
            "function": {
                "name": "demo__echo",
                "description": "[Demo] 原样返回文本",
                "parameters": plugin.manifest.tools[0].input_schema,
            },
        }
    ]


def test_registry_with_multiple_plugins(make_plugin: PluginFactory) -> None:
    registry = ToolRegistry([make_plugin(), make_plugin(id="other", name="Other")])

    assert [tool.qualified_name for tool in registry.tools] == ["demo__echo", "other__echo"]


def test_registry_rejects_duplicate_plugin_ids(make_plugin: PluginFactory) -> None:
    with pytest.raises(ValueError, match="插件 ID 重复"):
        ToolRegistry([make_plugin(), make_plugin()])


def test_successful_result_for_llm_excludes_stderr() -> None:
    result = ToolResult(
        call_id="c1", tool="demo__echo", ok=True, content={"a": 1}, stderr_tail="debug log"
    )

    assert json.loads(result.for_llm()) == {"ok": True, "content": {"a": 1}}


def test_failed_result_for_llm_has_code_message_and_hint() -> None:
    result = ToolResult(
        call_id="c1",
        tool="demo__echo",
        ok=False,
        error=ErrorInfo(code=ErrorCode.TOOL_ERROR, message="笔记不存在", hint="先列出笔记"),
    )

    assert json.loads(result.for_llm()) == {
        "ok": False,
        "error": {"code": "TOOL_ERROR", "message": "笔记不存在", "hint": "先列出笔记"},
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"ok": False},
        {"ok": True, "error": ErrorInfo(code=ErrorCode.TOOL_ERROR, message="x")},
    ],
)
def test_result_ok_and_error_must_agree(fields: dict) -> None:
    with pytest.raises(ValidationError):
        ToolResult(call_id="c1", tool="demo__echo", **fields)
