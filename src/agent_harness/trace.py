"""运行追踪（DESIGN.md §12）。

当前只在内存中收集事件并转发给订阅者（如 CLI 渲染器）；写入 runs/<run_id>.jsonl 在 M3 实现。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    LLM_RESPONSE = "llm.response"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    RUN_FINISHED = "run.finished"


class Event(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


Subscriber = Callable[[Event], None]


def new_run_id() -> str:
    return f"r_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


class Trace:
    """一次运行的事件流。订阅者出错只记录日志，不影响运行本身。"""

    def __init__(self, run_id: str | None = None, subscribers: Iterable[Subscriber] = ()) -> None:
        self._run_id = run_id or new_run_id()
        self._subscribers = list(subscribers)
        self._events: list[Event] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def emit(self, event_type: EventType, **payload: Any) -> Event:
        event = Event(
            run_id=self._run_id,
            seq=len(self._events) + 1,
            ts=datetime.now(UTC),
            type=event_type,
            payload=payload,
        )
        self._events.append(event)
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception:
                logger.exception("追踪订阅者处理事件 %s 时出错", event.type)
        return event
