"""CLI 入口（DESIGN.md §13）。只做参数解析和展示，业务逻辑都在 Harness 中。"""

import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from rich.markup import escape

from agent_harness import __version__
from agent_harness.app import Harness
from agent_harness.cli.render import (
    RunRenderer,
    console,
    render_error,
    render_tool_request,
    render_tool_result,
    render_tool_table,
)
from agent_harness.errors import HarnessError
from agent_harness.plugins.executor import ConfirmCallback
from agent_harness.plugins.registry import RegisteredTool

app = typer.Typer(
    name="harness",
    help="本地 AI Agent Harness：管理插件，并让 Agent 调用插件提供的工具。",
    no_args_is_help=True,
    add_completion=False,
)
plugin_app = typer.Typer(help="安装和管理插件。", no_args_is_help=True)
tool_app = typer.Typer(help="查看工具，或绕过 LLM 直接调用工具（调试用）。", no_args_is_help=True)
app.add_typer(plugin_app, name="plugin")
app.add_typer(tool_app, name="tool")

YesOption = Annotated[bool, typer.Option("--yes", "-y", help="对需要确认的工具调用一律允许。")]
ENV_FILE = ".env"
LOGGER_NAME = "agent_harness"
"""-v 只打开宿主自己的调试日志，不打开第三方库（如 HTTP 客户端）的，避免输出请求头等敏感信息。"""


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
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="输出调试日志（DEBUG）到 stderr。")
    ] = False,
) -> None:
    if verbose:
        logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG)


def _confirmer(assume_yes: bool) -> ConfirmCallback:
    def confirm(tool: RegisteredTool, arguments: dict[str, Any]) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            return False
        console.print(
            f"[yellow]工具 {escape(tool.qualified_name)} 需要你的确认[/]，参数："
            f"{escape(json.dumps(arguments, ensure_ascii=False))}"
        )
        return typer.confirm("允许执行吗？", default=False)

    return confirm


@plugin_app.command("install")
def plugin_install(
    path: Annotated[Path, typer.Argument(help="插件目录（包含 plugin.json）。")],
    dev: Annotated[
        bool, typer.Option("--dev", help="开发模式：链接到源码目录，修改代码后无需重新安装。")
    ] = False,
) -> None:
    """安装本地目录中的插件（只读取清单，不执行插件代码）。"""
    info = Harness.open().install_plugin(path, dev=dev)
    assert info.manifest is not None  # 安装成功时一定有清单
    if info.reason:
        console.print(f"[dim]{escape(info.reason)}[/]")
    console.print(
        f"[green]✓[/] 插件 [bold]{escape(info.id)}[/] {escape(info.manifest.version)}"
        f"（开发模式，链接到 {escape(str(info.path))}）"
    )
    console.print(f"  提供 {len(info.tool_names)} 个工具：{escape(', '.join(info.tool_names))}")


@tool_app.command("list")
def tool_list() -> None:
    """列出当前可用插件提供的全部工具。"""
    render_tool_table(Harness.open().tools().tools)


@tool_app.command("call")
def tool_call(
    name: Annotated[str, typer.Argument(help="工具全名，形如 <plugin_id>__<tool_name>。")],
    args: Annotated[str, typer.Option("--args", "-a", help="JSON 格式的参数。")] = "{}",
    yes: YesOption = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="同时显示请求和插件的 stderr 输出。")
    ] = False,
) -> None:
    """绕过 LLM 直接调用一个工具，走与 Agent 完全相同的执行链路。"""
    result = Harness.open().call_tool(name, args, confirm=_confirmer(yes))
    if verbose:
        render_tool_request(result.call_id, name, args)
    render_tool_result(result, verbose=verbose)
    if not result.ok:
        raise typer.Exit(1)


@app.command("run")
def run(
    prompt: Annotated[str, typer.Argument(help="交给 Agent 的任务。")],
    llm: Annotated[
        str | None, typer.Option("--llm", help="LLM 提供方：openai（OpenAI 兼容接口）或 scripted。")
    ] = None,
    script: Annotated[
        Path | None, typer.Option("--script", help="离线场景文件，按顺序回放预设的模型回复。")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="覆盖配置中的模型 ID。")] = None,
    max_steps: Annotated[
        int | None, typer.Option("--max-steps", min=1, help="覆盖配置中的最大步数。")
    ] = None,
    yes: YesOption = False,
) -> None:
    """运行一次 Agent，实时显示模型回复与工具调用过程。"""
    harness = Harness.open()
    client = harness.create_llm(llm, script=script, model=model)
    result = harness.run(
        prompt,
        client,
        confirm=_confirmer(yes),
        subscribers=[RunRenderer()],
        max_steps=max_steps,
    )
    if result.status != "completed":
        raise typer.Exit(1)


def main() -> None:
    """console script 入口：加载当前目录的 .env（不覆盖已有环境变量），
    并把可预期的 HarnessError 显示为友好的错误信息，不打印堆栈。"""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(Path.cwd() / ENV_FILE, override=False)
    try:
        app()
    except HarnessError as error:
        render_error(error)
        raise SystemExit(1) from error
