"""运行追踪（DESIGN.md §12）。

事件在内存中收集、实时转发给订阅者（如 CLI 渲染器），并逐行追加到 runs/<run_id>.jsonl，
之后可以用 harness runs list / show 回看。
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

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


RUN_ID_PATTERN = r"^r_\d{8}_\d{6}_[0-9a-f]{4}$"
TRACE_SUFFIX = ".jsonl"


def new_run_id() -> str:
    return f"r_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


def is_run_id(value: str) -> bool:
    return re.fullmatch(RUN_ID_PATTERN, value) is not None


class Trace:
    """一次运行的事件流。订阅者出错或追踪文件写入失败只记录日志，不影响运行本身。"""

    def __init__(
        self,
        run_id: str | None = None,
        subscribers: Iterable[Subscriber] = (),
        *,
        log_dir: Path | None = None,
    ) -> None:
        self._run_id = run_id or new_run_id()
        self._subscribers = list(subscribers)
        self._events: list[Event] = []
        self._file = None if log_dir is None else log_dir / f"{self._run_id}{TRACE_SUFFIX}"

    @property
    def file(self) -> Path | None:
        """追踪文件路径；不落盘时为 None。"""
        return self._file

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
        self._append(event)
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception:
                logger.exception("追踪订阅者处理事件 %s 时出错", event.type)
        return event

    def _append(self, event: Event) -> None:
        if self._file is None:
            return
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with self._file.open("a", encoding="utf-8") as file:
                file.write(event.model_dump_json() + "\n")
        except OSError as exc:
            logger.warning("写入追踪文件 %s 失败：%s", self._file, exc)


@dataclass(frozen=True)
class RunSummary:
    """runs list 的一行。status 为 None 表示运行没有正常结束（例如宿主进程被强制终止）。"""

    run_id: str
    started_at: datetime | None
    prompt: str | None
    model: str | None
    status: str | None
    steps: int | None


def read_events(path: Path) -> list[Event]:
    """读取追踪文件；无法解析的行跳过并记录警告（例如写到一半时宿主被终止）。"""
    events: list[Event] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(Event.model_validate_json(line))
        except ValidationError:
            logger.warning("追踪文件 %s 第 %d 行无法解析，已跳过", path, lineno)
    return events


def summarize_run(path: Path) -> RunSummary:
    events = read_events(path)
    started = next((e for e in events if e.type is EventType.RUN_STARTED), None)
    finished = next((e for e in events if e.type is EventType.RUN_FINISHED), None)
    return RunSummary(
        run_id=path.name.removesuffix(TRACE_SUFFIX),
        started_at=started.ts if started else None,
        prompt=started.payload.get("prompt") if started else None,
        model=started.payload.get("model") if started else None,
        status=finished.payload.get("status") if finished else None,
        steps=finished.payload.get("steps") if finished else None,
    )


def list_trace_files(runs_dir: Path) -> list[Path]:
    """按运行 ID（即开始时间）倒序列出追踪文件。"""
    if not runs_dir.is_dir():
        return []
    files = [
        path
        for path in runs_dir.glob(f"*{TRACE_SUFFIX}")
        if is_run_id(path.name.removesuffix(TRACE_SUFFIX))
    ]
    return sorted(files, key=lambda path: path.name, reverse=True)
