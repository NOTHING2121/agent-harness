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
    render_config,
    render_discovered,
    render_error,
    render_permissions,
    render_plugin_info,
    render_plugin_table,
    render_runs_table,
    render_tool_request,
    render_tool_result,
    render_tool_table,
    status_text,
)
from agent_harness.errors import HarnessError
from agent_harness.plugins.executor import ConfirmCallback
from agent_harness.plugins.manager import ApproveCallback, PluginInfo, PluginStatus
from agent_harness.plugins.manifest import Manifest
from agent_harness.plugins.registry import RegisteredTool

app = typer.Typer(
    name="harness",
    help="本地 AI Agent Harness：管理插件，并让 Agent 调用插件提供的工具。",
    no_args_is_help=True,
    add_completion=False,
)
plugin_app = typer.Typer(help="安装和管理插件。", no_args_is_help=True)
tool_app = typer.Typer(help="查看工具，或绕过 LLM 直接调用工具（调试用）。", no_args_is_help=True)
runs_app = typer.Typer(help="查看历史运行与执行追踪。", no_args_is_help=True)
app.add_typer(plugin_app, name="plugin")
app.add_typer(tool_app, name="tool")
app.add_typer(runs_app, name="runs")

YesOption = Annotated[bool, typer.Option("--yes", "-y", help="对需要确认的工具调用一律允许。")]
PluginIdArg = Annotated[str, typer.Argument(help="插件 ID。")]
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


def _approver(assume_yes: bool) -> ApproveCallback:
    def approve(manifest: Manifest) -> bool:
        render_permissions(manifest)
        if assume_yes:
            console.print("[dim]已通过 --yes 确认。[/]")
            return True
        if not sys.stdin.isatty():
            return False
        return typer.confirm("信任并启用这个插件吗？", default=False)

    return approve


def _print_next_step(info: PluginInfo) -> None:
    console.print(f"  状态：{status_text(info.status)}")
    if info.status is not PluginStatus.ACTIVE and info.hint:
        console.print(f"  下一步：[bold]{escape(info.hint)}[/]")


def _parse_assignments(items: list[str]) -> dict[str, Any]:
    """解析 --set key=value：值优先按 JSON 解析（数字、布尔、数组等），失败则当作字符串。"""
    values: dict[str, Any] = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter(f"{item!r} 不是 key=value 格式", param_hint="--set")
        try:
            values[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            values[key.strip()] = raw
    return values


@plugin_app.command("install")
def plugin_install(
    path: Annotated[Path, typer.Argument(help="插件目录（包含 plugin.json）。")],
    dev: Annotated[
        bool, typer.Option("--dev", help="开发模式：链接到源码目录，修改代码后无需重新安装。")
    ] = False,
) -> None:
    """安装本地目录中的插件（只读取清单和复制文件，不执行插件代码）。安装后需要启用才能使用。"""
    info = Harness.open().install_plugin(path, dev=dev)
    assert info.manifest is not None  # 安装成功时一定有清单
    mode = "开发模式，链接到" if info.dev else "已复制到"
    console.print(
        f"[green]✓[/] 已安装插件 [bold]{escape(info.id)}[/] {escape(info.manifest.version)}"
        f"（{mode} {escape(str(info.path))}）"
    )
    console.print(f"  提供 {len(info.tool_names)} 个工具：{escape(', '.join(info.tool_names))}")
    _print_next_step(info)


@plugin_app.command("uninstall")
def plugin_uninstall(
    plugin_id: PluginIdArg,
    purge: Annotated[bool, typer.Option("--purge", help="同时删除插件的数据目录。")] = False,
) -> None:
    """卸载插件。默认保留插件的数据目录。"""
    kept = Harness.open().uninstall_plugin(plugin_id, purge=purge)
    console.print(f"[green]✓[/] 已卸载插件 [bold]{escape(plugin_id)}[/]")
    if kept:
        console.print(f"  数据目录已保留：{escape(str(kept))}（加 --purge 可一并删除）")


@plugin_app.command("list")
def plugin_list() -> None:
    """列出已安装的插件、有效状态，以及不可用时的原因和下一步。"""
    render_plugin_table(Harness.open().list_plugins())


@plugin_app.command("info")
def plugin_info(plugin_id: PluginIdArg) -> None:
    """查看插件详情：状态、权限声明、配置项、目录和工具。"""
    render_plugin_info(Harness.open().plugin_info(plugin_id))


@plugin_app.command("enable")
def plugin_enable(
    plugin_id: PluginIdArg,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过授权确认（用于脚本）。")] = False,
) -> None:
    """启用插件：检查兼容性和配置，展示权限声明并请求确认。"""
    info = Harness.open().enable_plugin(plugin_id, approve=_approver(yes))
    console.print(
        f"[green]✓[/] 插件 [bold]{escape(info.id)}[/] 已启用，"
        f"Agent 现在可以使用它的 {len(info.tool_names)} 个工具。"
    )


@plugin_app.command("disable")
def plugin_disable(plugin_id: PluginIdArg) -> None:
    """禁用插件并撤销授权；配置保留，重新启用时需要再次确认。"""
    Harness.open().disable_plugin(plugin_id)
    console.print(f"[green]✓[/] 插件 [bold]{escape(plugin_id)}[/] 已禁用，授权已撤销（配置保留）。")


@plugin_app.command("config")
def plugin_config(
    plugin_id: PluginIdArg,
    set_items: Annotated[
        list[str] | None,
        typer.Option("--set", help="设置配置项，格式 key=value，可重复；值优先按 JSON 解析。"),
    ] = None,
    unset: Annotated[
        list[str] | None, typer.Option("--unset", help="删除配置项（恢复默认值），可重复。")
    ] = None,
) -> None:
    """查看或修改插件配置；修改后的配置整体按插件的 config_schema 校验。"""
    values = _parse_assignments(set_items or [])
    config = Harness.open().configure_plugin(plugin_id, set_values=values, unset=unset or [])
    if values or unset:
        console.print(f"[green]✓[/] 已保存 [bold]{escape(plugin_id)}[/] 的配置")
    render_config(config)


@plugin_app.command("validate")
def plugin_validate(
    path: Annotated[Path, typer.Argument(help="插件目录（包含 plugin.json）。")],
) -> None:
    """静态校验插件目录（清单、入口、版本兼容），不执行插件代码。"""
    manifest, incompatible = Harness.open().validate_plugin(path)
    if incompatible:
        console.print(f"[red]✗[/] 清单有效，但与当前 Harness 不兼容：{escape(incompatible)}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/] 清单有效：[bold]{escape(manifest.id)}[/] {escape(manifest.version)}，"
        f"{len(manifest.tools)} 个工具"
    )


@plugin_app.command("update")
def plugin_update(
    plugin_id: PluginIdArg,
    from_path: Annotated[
        Path | None,
        typer.Option("--from", help="新版本所在的插件目录，默认使用安装时记录的来源。"),
    ] = None,
) -> None:
    """更新复制安装的插件（新版本号必须更高）。权限声明变化时需要重新启用确认。"""
    info, old_version = Harness.open().update_plugin(plugin_id, source=from_path)
    assert info.manifest is not None  # 更新成功时一定有清单
    console.print(
        f"[green]✓[/] 插件 [bold]{escape(plugin_id)}[/] 已从 {escape(old_version or '?')} "
        f"更新到 {escape(info.manifest.version)}"
    )
    _print_next_step(info)


@plugin_app.command("discover")
def plugin_discover(
    directory: Annotated[Path, typer.Argument(help="要扫描的目录。")] = Path("plugins"),
) -> None:
    """扫描目录中可安装的插件，显示兼容性和安装状态（只读取清单）。"""
    render_discovered(str(directory), Harness.open().discover_plugins(directory))


@plugin_app.command("new")
def plugin_new(
    plugin_id: Annotated[str, typer.Argument(help="新插件的 ID，例如 hello。")],
    directory: Annotated[
        Path, typer.Option("--dir", help="在哪个目录下创建插件，默认为当前目录。")
    ] = Path("."),
) -> None:
    """从模板生成插件骨架：plugin.json、main.py 和 test_main.py。"""
    target = Harness.open().new_plugin(plugin_id, directory)
    console.print(f"[green]✓[/] 已生成插件 [bold]{escape(plugin_id)}[/]：{escape(str(target))}")
    for line in (
        f"harness plugin validate {target}",
        f"harness plugin install {target} --dev",
        f"harness plugin enable {plugin_id}",
        f'harness tool call {plugin_id}__greet --args \'{{"name": "Ada"}}\' -v',
    ):
        console.print(f"  [dim]$[/] {escape(line)}")


@tool_app.command("list")
def tool_list() -> None:
    """列出已启用插件提供的全部工具（即 Agent 能看到的工具）。"""
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


@runs_app.command("list")
def runs_list(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="最多显示多少条。")] = 20,
) -> None:
    """列出最近的运行（新的在前）。"""
    render_runs_table(Harness.open().list_runs(limit))


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="运行 ID，见 harness runs list。")],
) -> None:
    """回看一次运行的完整追踪：任务、模型回复、工具调用、结果摘要、错误和 stderr。"""
    harness = Harness.open()
    renderer = RunRenderer(replay=True)
    for event in harness.run_events(run_id):
        renderer(event)


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
