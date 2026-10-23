"""PluginRunner：为一次工具调用启动插件进程并判定结果（DESIGN.md §5）。"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_harness.errors import ErrorCode, ErrorInfo
from agent_harness.plugins.manifest import Manifest, ToolSpec
from agent_harness.plugins.registry import ActivePlugin
from agent_harness.settings import PluginSettings
from harness_sdk.protocol import API_VERSION, Request

BASE_ENV_VARS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
"""插件进程总能拿到的环境变量；其余只有插件在 permissions.env 中声明的才会传入。"""

STDERR_TAIL_LINES = 20
STDERR_TAIL_CHARS = 2000
STDOUT_PREVIEW_CHARS = 300

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

        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            _, stderr = _collect_after_kill(process)
            return self._failure(
                ErrorCode.PLUGIN_TIMEOUT,
                f"插件 {plugin.id} 在 {timeout:g}s 内没有响应，已终止该进程。",
                started,
                hint="缩小请求范围，或在 plugin.json 中调大该工具的 timeout_s。",
                stderr=stderr,
            )
        except BaseException:
            # 包括 Ctrl+C：先终止插件进程组再向上传递，不留下孤儿进程。
            _kill_process_group(process)
            process.wait()
            raise

        # 正常结束时插件进程已被回收，不再按进程组发信号（PID 可能已被复用）。
        # 仍占用管道的子进程会让 communicate 一直等待，最终走上面的超时分支被终止。
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
        stderr: bytes = b"",
    ) -> RunOutcome:
        return RunOutcome(
            ok=False,
            error=ErrorInfo(code=code, message=message, hint=hint),
            stderr_tail=tail(stderr.decode("utf-8", errors="replace")),
            duration_ms=_elapsed_ms(started),
        )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    # 进程组已经不存在（插件和它的子进程都已退出）时无需处理。
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _collect_after_kill(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        # 有子进程脱离了进程组仍占用管道：放弃读取剩余输出，避免宿主被卡住。
        process.kill()
        return b"", b""


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
