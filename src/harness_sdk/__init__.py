"""Harness 插件 SDK（DESIGN.md §6）。只依赖标准库，不引用宿主的任何模块。

用法::

    from harness_sdk import Context, Plugin

    plugin = Plugin()

    @plugin.tool("greet")
    def greet(ctx: Context, name: str) -> str:
        return f"Hello, {name}"

    if __name__ == "__main__":
        plugin.run()
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_sdk.protocol import API_VERSION, ErrorType, Response

__all__ = ["API_VERSION", "Context", "Plugin", "ToolError", "ToolOutput"]
__version__ = "0.1.0"

Handler = Callable[..., Any]


class ToolError(Exception):
    """插件主动抛出的可预期业务错误，会以 tool_error 类型返回给宿主。"""


@dataclass(frozen=True)
class ToolOutput:
    """处理函数的返回值：content 交给 LLM，display 是可选的给人看的 Markdown。"""

    content: Any
    display: str | None = None


@dataclass(frozen=True)
class Context:
    """一次工具调用的上下文。"""

    plugin_id: str
    call_id: str
    config: dict[str, Any]
    data_dir: Path
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("harness_sdk.plugin"))


class Plugin:
    """插件对象：注册工具处理函数，并在插件进程中处理宿主的一次请求。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def tool(self, name: str) -> Callable[[Handler], Handler]:
        """把函数注册为名为 name 的工具的处理函数，name 必须与 plugin.json 中的声明一致。"""

        def register(func: Handler) -> Handler:
            if name in self._handlers:
                raise ValueError(f"工具 {name!r} 被重复注册")
            self._handlers[name] = func
            return func

        return register

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._handlers)

    def invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        data_dir: Path,
        config: dict[str, Any] | None = None,
        plugin_id: str = "test",
        call_id: str = "test",
    ) -> Any:
        """在当前进程内直接调用处理函数，供插件作者编写单元测试；异常原样抛出。"""
        handler = self._handlers.get(tool)
        if handler is None:
            raise ValueError(f"插件没有注册工具 {tool!r}，已注册：{self.tool_names}")
        ctx = Context(
            plugin_id=plugin_id, call_id=call_id, config=dict(config or {}), data_dir=Path(data_dir)
        )
        return handler(ctx, **arguments)

    def run(self) -> None:
        """插件进程入口：读取 stdin 中的请求，执行处理函数，把响应写到协议通道。"""
        protocol_fd = _take_over_stdout()
        logging.basicConfig(
            stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
        )
        response = self.handle(sys.stdin.read())
        _write_response(protocol_fd, response)

    def handle(self, raw_request: str) -> Response:
        """处理一条原始请求并返回响应；处理函数的异常都转为错误响应。"""
        try:
            request = json.loads(raw_request)
        except json.JSONDecodeError as exc:
            return _error_response("", "exception", f"无法解析请求 JSON：{exc}")
        if not isinstance(request, dict):
            return _error_response("", "exception", "请求必须是 JSON 对象")

        call_id = str(request.get("call_id", ""))
        if request.get("api_version") != API_VERSION:
            return _error_response(
                call_id,
                "unsupported_api_version",
                f"SDK 支持的 API 版本为 {API_VERSION}，请求为 {request.get('api_version')!r}",
            )
        tool = request.get("tool")
        handler = self._handlers.get(tool) if isinstance(tool, str) else None
        if handler is None:
            return _error_response(call_id, "unknown_tool", f"插件没有注册工具 {tool!r}")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error_response(call_id, "invalid_arguments", "arguments 必须是 JSON 对象")

        ctx = Context(
            plugin_id=str(request.get("plugin_id", "")),
            call_id=call_id,
            config=dict(request.get("config") or {}),
            data_dir=Path(request.get("data_dir") or "."),
        )
        try:
            inspect.signature(handler).bind(ctx, **arguments)
        except TypeError as exc:
            return _error_response(call_id, "invalid_arguments", f"参数与处理函数不匹配：{exc}")

        try:
            result = handler(ctx, **arguments)
        except ToolError as exc:
            return _error_response(call_id, "tool_error", str(exc))
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return _error_response(call_id, "exception", f"{type(exc).__name__}: {exc}")

        output = result if isinstance(result, ToolOutput) else ToolOutput(content=result)
        response: Response = {
            "api_version": API_VERSION,
            "call_id": call_id,
            "ok": True,
            "content": output.content,
        }
        if output.display is not None:
            response["display"] = output.display
        try:
            json.dumps(response, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return _error_response(call_id, "exception", f"返回值无法序列化为 JSON：{exc}")
        return response


def _error_response(call_id: str, error_type: ErrorType, message: str) -> Response:
    return {
        "api_version": API_VERSION,
        "call_id": call_id,
        "ok": False,
        "error": {"type": error_type, "message": message},
    }


def _take_over_stdout() -> int:
    """先复制出协议专用的描述符，再让 fd 1 指向 stderr（DESIGN.md §5.3）。

    此后插件代码中的 print()、C 扩展以及子进程写到 stdout 的内容都会进入 stderr，
    协议输出只写入返回的描述符，不受影响。
    """
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    return protocol_fd


def _write_response(protocol_fd: int, response: Response) -> None:
    """先写一个换行，保证响应独占 stdout 的最后一行。"""
    with os.fdopen(protocol_fd, "w", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps(response, ensure_ascii=False) + "\n")
