"""错误码与宿主层面的异常（DESIGN.md §5.4、§9）。"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ValidationError


class ErrorCode(StrEnum):
    """工具调用失败时的错误码，出现在 ToolResult 与追踪中。"""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    USER_DENIED = "USER_DENIED"
    PLUGIN_TIMEOUT = "PLUGIN_TIMEOUT"
    PLUGIN_CRASHED = "PLUGIN_CRASHED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"


class ErrorInfo(BaseModel):
    """一次失败的结构化描述：错误码、给人看的消息和可选的修复建议。"""

    code: ErrorCode
    message: str
    hint: str | None = None


class HarnessError(Exception):
    """宿主层面可预期的错误（配置非法、插件不存在等），由 CLI 统一展示，不打印堆栈。"""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(HarnessError):
    """宿主配置文件或环境变量不合法。"""


class ManifestError(HarnessError):
    """插件清单缺失或不合法。"""


class LLMError(HarnessError):
    """LLM 请求失败（网络、鉴权、限流、脚本步骤用完等），本次运行会标记为 failed。"""


def not_implemented(feature: str, milestone: str) -> NotImplementedError:
    """骨架阶段的占位：明确标注尚未实现的功能及计划完成的阶段。"""
    return NotImplementedError(f"{feature} 尚未实现，计划在 {milestone} 完成（见 DESIGN.md §17）")


def format_location(loc: Sequence[int | str]) -> str:
    """把 Pydantic 的错误位置转成字段路径，例如 ("tools", 0, "name") -> "tools[0].name"。"""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path = f"{path}.{part}" if path else str(part)
    return path or "(根)"


def format_validation_error(exc: ValidationError) -> str:
    """把 Pydantic 校验错误整理成每行一条的“字段路径: 原因”。"""
    return "\n".join(f"{format_location(err['loc'])}: {err['msg']}" for err in exc.errors())
