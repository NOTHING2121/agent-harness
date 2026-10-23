"""CLI 输出渲染。"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from agent_harness.errors import HarnessError

console = Console()
err_console = Console(stderr=True)


def render_error(error: HarnessError) -> None:
    err_console.print(f"[bold red]错误：[/]{escape(error.message)}")
    if error.hint:
        err_console.print(f"[yellow]建议：[/]{escape(error.hint)}")
