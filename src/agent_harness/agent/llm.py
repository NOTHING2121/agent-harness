"""LLM 接入（DESIGN.md §8.5）。

- LLMClient：Agent Loop 依赖的唯一接口，请求失败时抛出 LLMError。
- ScriptedClient：按顺序回放预设步骤的离线 fake LM，用于确定性演示与测试。
- OpenAICompatibleClient：通过 OpenAI 兼容接口调用真实模型（M1 实现）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_harness.errors import HarnessError, LLMError, format_validation_error, not_implemented
from agent_harness.settings import LLMSettings

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """LLM 发起的一次工具调用。arguments 保留原始 JSON 字符串，由 ToolExecutor 解析与校验。"""

    id: str
    name: str
    arguments: str


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMResponse(BaseModel):
    message: ChatMessage
    usage: TokenUsage | None = None

    @property
    def content(self) -> str | None:
        return self.message.content

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse: ...


class ScriptedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ScriptStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_not_empty(self) -> ScriptStep:
        if self.content is None and not self.tool_calls:
            raise ValueError("每个步骤至少需要 content 或 tool_calls 之一")
        return self


class Script(BaseModel):
    """离线场景：按顺序回放的模型回复。"""

    model_config = ConfigDict(extra="forbid")

    steps: list[ScriptStep] = Field(min_length=1)


class ScriptedClient:
    """离线 fake LM：忽略输入，按顺序返回脚本中的下一步。工具调用仍走真实的执行链路。"""

    model = "scripted"

    def __init__(self, script: Script) -> None:
        self._steps = list(script.steps)
        self._next = 0

    @classmethod
    def from_file(cls, path: Path) -> ScriptedClient:
        """读取场景文件（JSON）；文件不存在或格式不对时抛出 HarnessError 并指出原因。"""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HarnessError(f"无法读取场景文件 {path}：{exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                f"场景文件 {path} 不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
            ) from exc
        try:
            script = Script.model_validate(data)
        except ValidationError as exc:
            raise HarnessError(
                f"场景文件 {path} 格式不正确：\n{format_validation_error(exc)}"
            ) from exc
        return cls(script)

    def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        if self._next >= len(self._steps):
            raise LLMError(
                f"脚本中的 {len(self._steps)} 个步骤已全部用完，但 Agent 仍在请求模型。",
                hint="在场景文件末尾添加一个只包含 content 的步骤作为最终回答。",
            )
        step = self._steps[self._next]
        self._next += 1
        calls = [
            ToolCall(
                id=f"call_{self._next}_{index}",
                name=call.name,
                arguments=json.dumps(call.arguments, ensure_ascii=False),
            )
            for index, call in enumerate(step.tool_calls, start=1)
        ]
        return LLMResponse(
            message=ChatMessage(role="assistant", content=step.content, tool_calls=calls)
        )


class OpenAICompatibleClient:
    """通过 OpenAI 兼容接口调用真实模型（默认 gpt-6.1-sol）。不发送 temperature 和 top_p。"""

    def __init__(self, settings: LLMSettings, api_key: str) -> None:
        self._settings = settings
        self._api_key = api_key

    @property
    def model(self) -> str:
        return self._settings.model

    def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        raise not_implemented("OpenAICompatibleClient.complete", "M1")
