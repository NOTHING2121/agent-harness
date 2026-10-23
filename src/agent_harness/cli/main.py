"""CLI 入口（DESIGN.md §13）。子命令在各自的开发阶段加入。"""

from typing import Annotated

import typer

from agent_harness import __version__
from agent_harness.cli.render import console, render_error
from agent_harness.errors import HarnessError

app = typer.Typer(
    name="harness",
    help="本地 AI Agent Harness：管理插件，并让 Agent 调用插件提供的工具。",
    no_args_is_help=True,
    add_completion=False,
)


def _show_version(value: bool) -> None:
    if value:
        console.print(f"harness {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="显示版本号并退出。"),
    ] = False,
) -> None:
    pass


def main() -> None:
    """console script 入口：把可预期的 HarnessError 显示为友好的错误信息，不打印堆栈。"""
    try:
        app()
    except HarnessError as error:
        render_error(error)
        raise SystemExit(1) from error
