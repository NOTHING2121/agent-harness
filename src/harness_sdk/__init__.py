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

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_sdk.protocol import API_VERSION

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
    ) -> Any:
        """在当前进程内直接调用处理函数，供插件作者编写单元测试。"""
        raise NotImplementedError("Plugin.invoke 尚未实现，计划在 M1 完成（见 DESIGN.md §6.2）")

    def run(self) -> None:
        """插件进程入口：读取 stdin 中的请求，执行处理函数，把响应写到协议通道后退出。"""
        raise NotImplementedError("Plugin.run 尚未实现，计划在 M1 完成（见 DESIGN.md §5.3）")
