"""LLM 接入（DESIGN.md §8.5）。

- LLMClient：Agent Loop 依赖的唯一接口，请求失败时抛出 LLMError。
- ScriptedClient：按顺序回放预设步骤的离线 fake LM，用于确定性演示与测试。
- OpenAICompatibleClient：通过 OpenAI 兼容接口调用真实模型（默认 OpenRouter）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_harness.errors import HarnessError, LLMError, format_validation_error
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
    """通过 OpenAI 兼容接口调用真实模型（默认 OpenRouter 上的 anthropic/claude-opus-4.8）。

    不发送 temperature 和 top_p，使用模型的默认值（部分模型如 openai/gpt-6.1-sol 不接受这两个参数）；
    429 与 5xx 由 SDK 有限重试。
    """

    MAX_RETRIES = 2

    def __init__(self, settings: LLMSettings, api_key: str, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or openai.OpenAI(
            api_key=api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout_s,
            max_retries=self.MAX_RETRIES,
        )

    @property
    def model(self) -> str:
        return self._settings.model

    def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [to_openai_message(message) for message in messages],
        }
        if tools:
            request["tools"] = list(tools)
        if self._settings.reasoning_effort:
            request["reasoning_effort"] = self._settings.reasoning_effort
        try:
            completion = self._client.chat.completions.create(**request)
        except openai.APIError as exc:
            message, hint = describe_api_error(exc, self._settings)
            raise LLMError(message, hint=hint) from exc
        return from_openai_completion(completion)


def to_openai_message(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def from_openai_completion(completion: Any) -> LLMResponse:
    choices = getattr(completion, "choices", None)
    if not choices:
        raise LLMError(
            "模型返回了空结果（没有 choices）。",
            hint="可能是服务端临时故障，稍后重试；如果持续出现，换一个模型试试。",
        )
    message = choices[0].message
    calls = [
        ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments or "{}")
        for call in (message.tool_calls or [])
        if getattr(call, "type", None) == "function"
    ]
    usage = getattr(completion, "usage", None)
    return LLMResponse(
        message=ChatMessage(role="assistant", content=message.content, tool_calls=calls),
        usage=TokenUsage(
            prompt_tokens=usage.prompt_tokens or 0, completion_tokens=usage.completion_tokens or 0
        )
        if usage
        else None,
    )


BATCH_SUFFIX = ":batch"
"""OpenRouter 的批处理变体后缀，这类模型不支持 chat/completions。"""


def describe_api_error(exc: openai.APIError, settings: LLMSettings) -> tuple[str, str | None]:
    """把 SDK 异常整理成给人看的消息和修复建议；不包含 API Key。"""
    detail = _error_detail(exc)
    if isinstance(exc, openai.AuthenticationError):
        return f"LLM 鉴权失败：{detail}", f"检查 .env 中的 {settings.api_key_env} 是否正确。"
    if isinstance(exc, openai.NotFoundError):
        if "guardrail" in detail.lower():
            return (
                f"模型 {settings.model} 被服务商账户的 guardrail 策略屏蔽：{detail}",
                "按错误信息中的链接调整 guardrail 放行该模型，"
                "或用 --model / HARNESS_LLM_MODEL 换一个账户允许的模型。",
            )
        if settings.model.endswith(BATCH_SUFFIX):
            return (
                f"模型 {settings.model} 不能用于对话接口：{detail}",
                f"{BATCH_SUFFIX} 变体只能用于异步批处理，Agent 每一步都要等模型回复，"
                f"请去掉后缀，改用 {settings.model.removesuffix(BATCH_SUFFIX)}。",
            )
        return (
            f"找不到模型或接口：{detail}",
            f"检查模型 ID（当前为 {settings.model}）和 base_url（{settings.base_url}）。",
        )
    if isinstance(exc, openai.RateLimitError):
        return f"LLM 请求被限流：{detail}", "稍后重试，或检查账户的调用额度。"
    if isinstance(exc, openai.APITimeoutError):
        return (
            f"LLM 请求超时（{settings.request_timeout_s:g}s）。",
            "检查网络，或在 config.toml 中调大 llm.request_timeout_s。",
        )
    if isinstance(exc, openai.APIConnectionError):
        return f"无法连接 LLM 服务（{settings.base_url}）：{detail}", "检查网络连接和 base_url。"
    if isinstance(exc, openai.APIStatusError):
        hint = "账户余额不足，请到服务商控制台充值。" if exc.status_code == 402 else None
        return f"LLM 请求失败（HTTP {exc.status_code}）：{detail}", hint
    return f"LLM 请求失败：{detail}", None


def _error_detail(exc: openai.APIError) -> str:
    """优先取服务端错误体中的 message，避免把整段 JSON 打到终端。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"].strip()
    return getattr(exc, "message", None) or str(exc)
