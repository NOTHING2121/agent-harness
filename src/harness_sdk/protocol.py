"""宿主与插件之间的协议契约（DESIGN.md §5）。

本模块只依赖标准库，是宿主唯一引用的 SDK 模块。
每次工具调用启动一个插件进程：宿主向 stdin 写入一行 Request，
插件在 stdout 的最后一个非空行写出 Response，stderr 为自由日志。
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

API_VERSION = 1
"""插件 API 版本，同时覆盖清单格式与消息格式。"""

MANIFEST_FILE = "plugin.json"
"""插件目录中清单文件的文件名。"""

ErrorType = Literal[
    "tool_error",
    "exception",
    "invalid_arguments",
    "unknown_tool",
    "unsupported_api_version",
]
"""插件在响应中可以报告的错误类型。"""


class Request(TypedDict):
    """宿主写入插件 stdin 的请求。"""

    api_version: int
    call_id: str
    tool: str
    arguments: dict[str, Any]
    config: dict[str, Any]
    data_dir: str


class ResponseError(TypedDict):
    type: ErrorType
    message: str


class Response(TypedDict):
    """插件写在 stdout 最后一个非空行的响应。"""

    api_version: int
    call_id: str
    ok: bool
    content: NotRequired[Any]
    display: NotRequired[str | None]
    error: NotRequired[ResponseError]
