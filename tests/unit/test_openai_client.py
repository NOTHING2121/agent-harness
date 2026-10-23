"""OpenAICompatibleClient 单元测试：注入假的 SDK 客户端，不发真实请求。"""

from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from agent_harness.agent.llm import ChatMessage, OpenAICompatibleClient, ToolCall
from agent_harness.errors import LLMError
from agent_harness.settings import LLMSettings

TOOLS = [{"type": "function", "function": {"name": "demo__echo", "parameters": {}}}]


def completion(message: dict[str, Any], usage: dict[str, int] | None = None) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "anthropic/claude-opus-4.8",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": usage,
        }
    )


class FakeSDK:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._result = result
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._result


def make_client(sdk: FakeSDK, **settings: Any) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(LLMSettings(**settings), "sk-test", client=sdk)


def test_request_shape() -> None:
    sdk = FakeSDK(completion({"role": "assistant", "content": "好的"}))
    history = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="t1", name="demo__echo", arguments='{"text": "x"}')],
        ),
        ChatMessage(role="tool", tool_call_id="t1", content='{"ok": true}'),
    ]

    make_client(sdk).complete(history, TOOLS)

    request = sdk.requests[0]
    assert request["model"] == "anthropic/claude-opus-4.8"
    assert request["tools"] == TOOLS
    assert "temperature" not in request
    assert "top_p" not in request
    assert "reasoning_effort" not in request
    assert request["messages"][2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "demo__echo", "arguments": '{"text": "x"}'},
            }
        ],
    }
    assert request["messages"][3] == {
        "role": "tool",
        "content": '{"ok": true}',
        "tool_call_id": "t1",
    }


def test_optional_parameters() -> None:
    sdk = FakeSDK(completion({"role": "assistant", "content": "ok"}))

    make_client(sdk, reasoning_effort="low", model="other/model").complete(
        [ChatMessage(role="user", content="hi")], []
    )

    assert sdk.requests[0]["reasoning_effort"] == "low"
    assert sdk.requests[0]["model"] == "other/model"
    assert "tools" not in sdk.requests[0]


def test_tool_calls_and_usage_are_parsed() -> None:
    sdk = FakeSDK(
        completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "demo__echo", "arguments": '{"text": "x"}'},
                    }
                ],
            },
            usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        )
    )

    response = make_client(sdk).complete([ChatMessage(role="user", content="hi")], TOOLS)

    assert response.tool_calls == [ToolCall(id="t1", name="demo__echo", arguments='{"text": "x"}')]
    assert response.usage is not None
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (12, 5)


def test_empty_choices_is_llm_error() -> None:
    sdk = FakeSDK(SimpleNamespace(choices=[], usage=None))

    with pytest.raises(LLMError, match="空结果"):
        make_client(sdk).complete([ChatMessage(role="user", content="hi")], [])


REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=REQUEST)
    return cls(f"HTTP {status}", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "message", "hint"),
    [
        (status_error(openai.AuthenticationError, 401), "鉴权失败", "OPENROUTER_API_KEY"),
        (status_error(openai.NotFoundError, 404), "找不到模型", "anthropic/claude-opus-4.8"),
        (status_error(openai.RateLimitError, 429), "限流", "稍后重试"),
        (status_error(openai.APIStatusError, 402), "HTTP 402", "余额不足"),
        (openai.APITimeoutError(request=REQUEST), "超时", "request_timeout_s"),
        (openai.APIConnectionError(request=REQUEST), "无法连接", "网络"),
    ],
)
def test_api_errors_become_llm_errors(error: Exception, message: str, hint: str) -> None:
    sdk = FakeSDK(error=error)

    with pytest.raises(LLMError) as exc_info:
        make_client(sdk).complete([ChatMessage(role="user", content="hi")], [])

    assert message in exc_info.value.message
    assert hint in (exc_info.value.hint or "")
    assert "sk-test" not in exc_info.value.message


def test_guardrail_block_uses_server_message_and_specific_hint() -> None:
    body = {
        "message": "0 endpoints available. Model blocked by guardrail: configurable at https://x",
        "code": 404,
        "metadata": {"ineligibility_reasons": [{"reason": "model-ignored-by-guardrail"}]},
    }
    response = httpx2.Response(404, request=REQUEST)
    error = openai.NotFoundError(f"Error code: 404 - {body}", response=response, body=body)

    with pytest.raises(LLMError) as exc_info:
        make_client(FakeSDK(error=error)).complete([ChatMessage(role="user", content="hi")], [])

    assert "guardrail" in exc_info.value.message
    assert "metadata" not in exc_info.value.message, "只展示服务端的 message，不打印整段 JSON"
    assert "--model" in (exc_info.value.hint or "")


def test_batch_variant_gets_specific_hint() -> None:
    body = {
        "message": "anthropic/claude-opus-4.8:batch cannot be used with the chat/completions "
        "endpoint (adapter AnthropicBatchAdapter)",
        "code": 404,
    }
    response = httpx2.Response(404, request=REQUEST)
    error = openai.NotFoundError(f"Error code: 404 - {body}", response=response, body=body)
    client = make_client(FakeSDK(error=error), model="anthropic/claude-opus-4.8:batch")

    with pytest.raises(LLMError) as exc_info:
        client.complete([ChatMessage(role="user", content="hi")], [])

    assert "不能用于对话接口" in exc_info.value.message
    assert "改用 anthropic/claude-opus-4.8。" in (exc_info.value.hint or "")
