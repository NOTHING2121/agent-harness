import pytest
from pydantic import ValidationError

from agent_harness.agent.llm import Script, ScriptedClient
from agent_harness.errors import LLMError
from agent_harness.trace import Event, EventType, Trace


def test_trace_numbers_events_and_notifies_subscribers() -> None:
    received: list[Event] = []
    trace = Trace(run_id="r_test", subscribers=[received.append])

    trace.emit(EventType.RUN_STARTED, prompt="hi")
    trace.emit(EventType.RUN_FINISHED, status="completed")

    assert [(e.seq, e.type) for e in received] == [
        (1, EventType.RUN_STARTED),
        (2, EventType.RUN_FINISHED),
    ]
    assert received[0].run_id == "r_test"
    assert received[0].payload == {"prompt": "hi"}
    assert trace.events == received


def test_failing_subscriber_does_not_break_trace(caplog: pytest.LogCaptureFixture) -> None:
    def broken(event: Event) -> None:
        raise RuntimeError("renderer bug")

    received: list[Event] = []
    trace = Trace(subscribers=[broken, received.append])

    trace.emit(EventType.RUN_STARTED)

    assert len(received) == 1
    assert "追踪订阅者处理事件" in caplog.text


def test_generated_run_ids_are_unique() -> None:
    assert Trace().run_id != Trace().run_id


def test_scripted_client_replays_steps_in_order() -> None:
    client = ScriptedClient(
        Script.model_validate(
            {
                "steps": [
                    {"tool_calls": [{"name": "demo__echo", "arguments": {"text": "你好"}}]},
                    {"content": "完成"},
                ]
            }
        )
    )

    first = client.complete([], [])
    second = client.complete([], [])

    assert first.tool_calls[0].name == "demo__echo"
    assert first.tool_calls[0].arguments == '{"text": "你好"}'
    assert second.content == "完成"
    assert second.tool_calls == []
    with pytest.raises(LLMError):
        client.complete([], [])


@pytest.mark.parametrize("data", [{"steps": []}, {"steps": [{}]}, {"steps": [{"text": "x"}]}])
def test_invalid_scripts_rejected(data: dict) -> None:
    with pytest.raises(ValidationError):
        Script.model_validate(data)
