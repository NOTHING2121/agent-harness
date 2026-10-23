"""PluginRunner：为一次工具调用启动插件进程并判定结果（DESIGN.md §5）。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any

from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.manifest import Manifest, ToolSpec
from agent_harness.plugins.registry import ActivePlugin
from agent_harness.settings import PluginSettings
from harness_sdk.protocol import API_VERSION, Request

logger = logging.getLogger(__name__)

BASE_ENV_VARS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
"""插件进程总能拿到的环境变量；其余只有插件在 permissions.env 中声明的才会传入。"""

STDERR_TAIL_LINES = 20
STDERR_TAIL_CHARS = 2000
STDOUT_PREVIEW_CHARS = 300
MAX_STDOUT_BYTES = 10 * 1024 * 1024
"""stdout 的读取上限（DESIGN.md §5.4）：超过即终止进程组并返回 RESULT_TOO_LARGE。"""
STDERR_KEEP_BYTES = 64 * 1024
"""stderr 只保留末尾这么多字节，用于展示和追踪。"""
PIPE_CHUNK_BYTES = 64 * 1024

_PLUGIN_ERROR_CODES = {
    "tool_error": ErrorCode.TOOL_ERROR,
    "exception": ErrorCode.TOOL_ERROR,
    "invalid_arguments": ErrorCode.INVALID_ARGUMENTS,
}


@dataclass(frozen=True)
class RunOutcome:
    """一次插件进程调用的结果，由 ToolExecutor 整理为 ToolResult。"""

    ok: bool
    content: Any = None
    display: str | None = None
    error: ErrorInfo | None = None
    stderr_tail: str | None = None
    duration_ms: int = 0


def build_plugin_env(manifest: Manifest, environ: Mapping[str, str]) -> dict[str, str]:
    """最小环境 + 插件声明的变量；宿主自己的 API Key 等不会传给插件。"""
    names = (*BASE_ENV_VARS, *manifest.permissions.env)
    env = {name: environ[name] for name in names if name in environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def parse_response(stdout: str, call_id: str) -> dict[str, Any] | None:
    """取 stdout 最后一个非空行作为响应；格式不对或 call_id 不匹配时返回 None。"""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(data, dict)
        or data.get("call_id") != call_id
        or not isinstance(data.get("ok"), bool)
    ):
        return None
    return data


def tail(text: str) -> str | None:
    lines = text.strip().splitlines()[-STDERR_TAIL_LINES:]
    result = "\n".join(lines)[-STDERR_TAIL_CHARS:]
    return result or None


class PluginRunner:
    """每次调用启动一个短生命周期进程：写请求、读响应、超时终止进程组。"""

    def __init__(self, settings: PluginSettings, environ: Mapping[str, str] | None = None) -> None:
        self._settings = settings
        self._environ = os.environ if environ is None else environ

    def timeout_for(self, tool: ToolSpec) -> float:
        """工具声明的超时优先，但不超过宿主上限。"""
        timeout = tool.timeout_s or self._settings.default_timeout_s
        return min(timeout, self._settings.max_timeout_s)

    def call(
        self,
        plugin: ActivePlugin,
        tool: ToolSpec,
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> RunOutcome:
        """执行一次工具调用。插件的任何异常行为都以 RunOutcome 返回，不向上抛出。"""
        request: Request = {
            "api_version": API_VERSION,
            "call_id": call_id,
            "plugin_id": plugin.id,
            "tool": tool.name,
            "arguments": arguments,
            "config": plugin.config,
            "data_dir": str(plugin.data_dir),
        }
        payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        timeout = self.timeout_for(tool)
        started = time.monotonic()
        # 调试日志不包含参数、配置和环境变量，避免泄露敏感信息。
        logger.debug(
            "启动插件 %s 的工具 %s（call_id=%s，超时 %gs）", plugin.id, tool.name, call_id, timeout
        )

        try:
            plugin.data_dir.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                [sys.executable, str(plugin.path / plugin.manifest.entry)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=plugin.data_dir,
                env=build_plugin_env(plugin.manifest, self._environ),
                start_new_session=True,
            )
        except OSError as exc:
            return self._failure(
                ErrorCode.PLUGIN_CRASHED, f"无法启动插件 {plugin.id} 的进程：{exc}", started
            )

        stdout_reader = _PipeReader(
            process.stdout, limit=MAX_STDOUT_BYTES, on_overflow=lambda: _kill_process_group(process)
        )
        stderr_reader = _PipeReader(process.stderr, limit=STDERR_KEEP_BYTES, keep_tail=True)
        threading.Thread(target=_write_stdin, args=(process.stdin, payload), daemon=True).start()
        deadline = started + timeout
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
            # 进程已退出，但派生的子进程可能仍占用管道：等到超时为止，仍未结束则按超时处理。
            stdout_reader.join(max(0.0, deadline - time.monotonic()))
            stderr_reader.join(max(0.0, deadline - time.monotonic()))
            timed_out = stdout_reader.alive or stderr_reader.alive
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException:
            # 包括 Ctrl+C：先终止插件进程组再向上传递，不留下孤儿进程。
            _kill_process_group(process)
            process.wait()
            raise

        if timed_out:
            logger.debug("插件 %s 超时，终止进程组 %d", plugin.id, process.pid)
            _kill_process_group(process)
            process.wait()
            stderr_reader.join(5)
            return self._failure(
                ErrorCode.PLUGIN_TIMEOUT,
                f"插件 {plugin.id} 在 {timeout:g}s 内没有响应，已终止该进程。",
                started,
                hint="缩小请求范围，或在 plugin.json 中调大该工具的 timeout_s。",
                stderr=stderr_reader.data,
            )
        if stdout_reader.overflowed:
            return self._failure(
                ErrorCode.RESULT_TOO_LARGE,
                f"插件 {plugin.id} 的输出超过 {MAX_STDOUT_BYTES // (1024 * 1024)} MB，已终止该进程。",
                started,
                hint="让工具返回有界的结果（分页或缩小范围），见插件开发指南中的分页约定。",
                stderr=stderr_reader.data,
            )

        stdout, stderr = bytes(stdout_reader.data), bytes(stderr_reader.data)
        logger.debug(
            "插件 %s 已退出（退出码 %d，耗时 %d ms，stdout %d 字节，stderr %d 字节）",
            plugin.id,
            process.returncode,
            _elapsed_ms(started),
            len(stdout),
            len(stderr),
        )
        return self._interpret(plugin.id, call_id, process.returncode, stdout, stderr, started)

    def _interpret(
        self,
        plugin_id: str,
        call_id: str,
        returncode: int,
        stdout: bytes,
        stderr: bytes,
        started: float,
    ) -> RunOutcome:
        out_text = stdout.decode("utf-8", errors="replace")
        response = parse_response(out_text, call_id)
        if response is None:
            if returncode != 0:
                return self._failure(
                    ErrorCode.PLUGIN_CRASHED,
                    f"插件 {plugin_id} 异常退出（退出码 {returncode}），没有返回结果。",
                    started,
                    hint="查看 stderr 中的错误信息，或用 harness tool call -v 单独调试该工具。",
                    stderr=stderr,
                )
            preview = out_text.strip()[-STDOUT_PREVIEW_CHARS:] or "（空）"
            return self._failure(
                ErrorCode.PROTOCOL_ERROR,
                f"插件 {plugin_id} 没有在 stdout 最后一行输出合法的响应。stdout 末尾：{preview}",
                started,
                hint="插件应调用 harness_sdk 的 plugin.run()，或保证 stdout 最后一行是响应 JSON。",
                stderr=stderr,
            )

        stderr_tail = tail(stderr.decode("utf-8", errors="replace"))
        duration_ms = _elapsed_ms(started)
        if response["ok"]:
            display = response.get("display")
            return RunOutcome(
                ok=True,
                content=response.get("content"),
                display=display if isinstance(display, str) else None,
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )

        error = response.get("error")
        error = error if isinstance(error, dict) else {}
        error_type = str(error.get("type", ""))
        message = str(error.get("message") or "插件没有提供错误信息")
        code = _PLUGIN_ERROR_CODES.get(error_type, ErrorCode.PROTOCOL_ERROR)
        if error_type == "exception":
            message = f"插件内部异常：{message}"
        elif code is ErrorCode.PROTOCOL_ERROR:
            message = f"插件返回了协议错误（{error_type or '未知类型'}）：{message}"
        return RunOutcome(
            ok=False,
            error=ErrorInfo(code=code, message=message),
            stderr_tail=stderr_tail,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _failure(
        code: ErrorCode,
        message: str,
        started: float,
        *,
        hint: str | None = None,
        stderr: bytes | bytearray = b"",
    ) -> RunOutcome:
        return RunOutcome(
            ok=False,
            error=ErrorInfo(code=code, message=message, hint=hint),
            stderr_tail=tail(bytes(stderr).decode("utf-8", errors="replace")),
            duration_ms=_elapsed_ms(started),
        )


class _PipeReader:
    """后台线程读取一个管道，内存占用有上限。

    默认保留开头：超过 limit 时标记 overflowed、丢弃已读内容并调用 on_overflow，之后继续读取丢弃，
    避免插件阻塞在写管道上。keep_tail=True 时只保留最后 limit 字节（用于 stderr）。
    """

    def __init__(
        self,
        stream: IO[bytes] | None,
        *,
        limit: int,
        keep_tail: bool = False,
        on_overflow: Callable[[], None] | None = None,
    ) -> None:
        assert stream is not None  # Popen 使用了 PIPE
        self.data = bytearray()
        self.overflowed = False
        self._thread = threading.Thread(
            target=self._run, args=(stream, limit, keep_tail, on_overflow), daemon=True
        )
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(
        self,
        stream: IO[bytes],
        limit: int,
        keep_tail: bool,
        on_overflow: Callable[[], None] | None,
    ) -> None:
        with stream:
            while chunk := os.read(stream.fileno(), PIPE_CHUNK_BYTES):
                if keep_tail:
                    self.data += chunk
                    del self.data[:-limit]
                elif self.overflowed:
                    continue
                elif len(self.data) + len(chunk) > limit:
                    self.overflowed = True
                    self.data.clear()
                    if on_overflow is not None:
                        on_overflow()
                else:
                    self.data += chunk


def _write_stdin(stream: IO[bytes] | None, payload: bytes) -> None:
    """在后台线程写请求：插件不读取 stdin 时也不会阻塞宿主，由超时兜底。"""
    assert stream is not None
    # 插件提前退出或关闭了 stdin 时写入会失败，结果由退出码和 stdout 判定。
    with contextlib.suppress(OSError):
        stream.write(payload)
        stream.close()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    # 进程组已经不存在（插件和它的子进程都已退出）时无需处理。
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
