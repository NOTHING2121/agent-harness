"""harness_sdk 的进程内行为：注册、invoke 与 handle（不启动子进程）。"""

import json
from pathlib import Path

import pytest

from harness_sdk import Context, Plugin, ToolError, ToolOutput


def make_plugin() -> Plugin:
    plugin = Plugin()

    @plugin.tool("greet")
    def greet(ctx: Context, name: str, punctuation: str = "!") -> ToolOutput:
        return ToolOutput(content=f"Hello, {name}{punctuation}", display=f"**{name}**")

    @plugin.tool("fail")
    def fail(ctx: Context) -> None:
        raise ToolError("业务错误")

    return plugin


def request(tool: str, arguments: dict | None = None, **overrides) -> str:
    data = {
        "api_version": 1,
        "call_id": "c1",
        "plugin_id": "demo",
        "tool": tool,
        "arguments": arguments or {},
        "config": {},
        "data_dir": "/tmp",
    }
    data.update(overrides)
    return json.dumps(data)


def test_duplicate_tool_registration_rejected() -> None:
    plugin = make_plugin()

    with pytest.raises(ValueError, match="重复注册"):
        plugin.tool("greet")(lambda ctx: None)


def test_invoke_calls_handler_in_process(tmp_path: Path) -> None:
    result = make_plugin().invoke("greet", {"name": "Ada"}, data_dir=tmp_path)

    assert result == ToolOutput(content="Hello, Ada!", display="**Ada**")


def test_invoke_propagates_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="业务错误"):
        make_plugin().invoke("fail", {}, data_dir=tmp_path)


def test_invoke_unknown_tool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="没有注册工具"):
        make_plugin().invoke("nope", {}, data_dir=tmp_path)


def test_handle_success() -> None:
    response = make_plugin().handle(request("greet", {"name": "Ada", "punctuation": "?"}))

    assert response == {
        "api_version": 1,
        "call_id": "c1",
        "ok": True,
        "content": "Hello, Ada?",
        "display": "**Ada**",
    }


@pytest.mark.parametrize(
    ("raw", "error_type"),
    [
        ("not json", "exception"),
        ("[1, 2]", "exception"),
        (request("greet", {"name": "x"}, api_version=99), "unsupported_api_version"),
        (request("nope"), "unknown_tool"),
        (request("greet", {}), "invalid_arguments"),
        (request("greet", {"name": "x", "extra": 1}), "invalid_arguments"),
        (json.dumps({**json.loads(request("greet")), "arguments": [1]}), "invalid_arguments"),
        (request("fail"), "tool_error"),
    ],
)
def test_handle_errors(raw: str, error_type: str) -> None:
    response = make_plugin().handle(raw)

    assert response["ok"] is False
    assert response["error"]["type"] == error_type
