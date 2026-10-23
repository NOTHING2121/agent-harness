"""CLI 输出渲染。只负责展示，不包含业务逻辑。"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.json import JSON
from rich.markdown import Markdown
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table

from agent_harness.errors import ErrorInfo, HarnessError
from agent_harness.plugins.executor import ToolResult
from agent_harness.plugins.registry import RegisteredTool
from agent_harness.trace import Event, EventType

console = Console()
err_console = Console(stderr=True)

ARGUMENT_PREVIEW_CHARS = 200
"""终端里参数预览的长度；完整参数仍保存在追踪事件中。"""


def render_error(error: HarnessError) -> None:
    err_console.print(f"[bold red]错误：[/]{escape(error.message)}")
    if error.hint:
        err_console.print(f"[yellow]建议：[/]{escape(error.hint)}")


def render_tool_table(tools: list[RegisteredTool]) -> None:
    if not tools:
        console.print(
            "还没有可用的工具。先用 [bold]harness plugin install PATH --dev[/] 安装插件。"
        )
        return
    table = Table(title=f"可用工具（{len(tools)} 个）", title_justify="left")
    table.add_column("工具", style="bold", no_wrap=True)
    table.add_column("插件", no_wrap=True)
    table.add_column("说明", overflow="fold")
    table.add_column("需确认", justify="center")
    for tool in tools:
        manifest = tool.plugin.manifest
        table.add_row(
            escape(tool.qualified_name),
            escape(f"{manifest.name} {manifest.version}"),
            escape(tool.spec.description),
            "是" if tool.spec.requires_confirmation else "",
        )
    console.print(table)


def render_tool_request(call_id: str, tool: str, raw_arguments: str) -> None:
    """tool call -v：显示发给插件的工具名和参数（参数不是合法 JSON 时原样显示）。"""
    try:
        arguments: Any = json.loads(raw_arguments) if raw_arguments.strip() else {}
    except json.JSONDecodeError:
        arguments = raw_arguments
    request = {"call_id": call_id, "tool": tool, "arguments": arguments}
    console.print(Panel(JSON.from_data(request, ensure_ascii=False), title="请求"))


def render_tool_result(result: ToolResult, *, verbose: bool = False) -> None:
    if result.ok:
        console.print(f"[green]✓[/] {escape(result.tool)} [dim]{result.duration_ms} ms[/]")
        if result.display:
            console.print(Markdown(result.display))
        console.print(Panel(JSON.from_data(result.content, ensure_ascii=False), title="content"))
    else:
        assert result.error is not None  # ToolResult 的校验保证
        _print_failure(console, result.tool, result.error, indent=0)
    if verbose:
        stderr = escape(result.stderr_tail) if result.stderr_tail else "[dim]（没有输出）[/]"
        console.print(Panel(stderr, title="插件 stderr（末尾）", border_style="dim"))


class RunRenderer:
    """追踪订阅者：把 Agent 运行过程实时打印到终端。"""

    def __init__(self, out: Console | None = None) -> None:
        self._console = out or console

    def __call__(self, event: Event) -> None:
        payload = event.payload
        if event.type == EventType.RUN_STARTED:
            self._console.print(
                f"[dim]运行 {event.run_id} · 模型 {escape(str(payload.get('model')))} · "
                f"可用工具 {len(payload.get('tools', []))} 个[/]"
            )
        elif event.type == EventType.LLM_RESPONSE:
            if payload.get("tool_calls") and payload.get("content"):
                self._console.print(f"[cyan]模型：[/]{escape(payload['content'])}")
        elif event.type == EventType.TOOL_STARTED:
            self._console.print(
                f"[bold]→ {escape(payload['tool'])}[/] "
                f"[dim]{escape(_preview(payload.get('arguments', '')))}[/]"
            )
        elif event.type == EventType.TOOL_FINISHED:
            self._tool_finished(payload)
        elif event.type == EventType.RUN_FINISHED:
            self._run_finished(payload)

    def _tool_finished(self, payload: dict[str, Any]) -> None:
        if payload["ok"]:
            self._console.print(f"  [green]✓ 完成[/] [dim]{payload['duration_ms']} ms[/]")
            if payload.get("display"):
                self._console.print(Padding(Markdown(payload["display"]), (0, 0, 0, 4)))
        else:
            error = ErrorInfo.model_validate(payload["error"])
            _print_failure(self._console, payload["tool"], error, indent=2)

    def _run_finished(self, payload: dict[str, Any]) -> None:
        status = payload["status"]
        if status == "completed":
            answer = payload.get("final_answer") or "（模型没有返回文字内容）"
            self._console.print(Panel(Markdown(answer), title="回答", border_style="green"))
        else:
            self._console.print(
                f"[bold red]运行未完成（{escape(status)}）：[/]{escape(payload.get('error') or '')}"
            )
            if payload.get("hint"):
                self._console.print(f"[yellow]建议：[/]{escape(payload['hint'])}")
        self._console.print(f"[dim]共 {payload['steps']} 步[/]")


def _print_failure(out: Console, tool: str, error: ErrorInfo, *, indent: int) -> None:
    pad = " " * indent
    out.print(
        f"{pad}[red]✗ {escape(tool)}[/] [bold red]{escape(f'[{error.code.value}]')}[/] "
        f"{escape(error.message)}"
    )
    if error.hint:
        out.print(f"{pad}  [yellow]建议：[/]{escape(error.hint)}")


def _preview(raw_arguments: str) -> str:
    try:
        text = json.dumps(json.loads(raw_arguments), ensure_ascii=False)
    except json.JSONDecodeError:
        text = raw_arguments
    if len(text) <= ARGUMENT_PREVIEW_CHARS:
        return text
    return f"{text[:ARGUMENT_PREVIEW_CHARS]}…（共 {len(text)} 字符）"
