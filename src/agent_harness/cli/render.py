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
from agent_harness.plugins.manager import DiscoveredPlugin, InstallStatus, PluginInfo, PluginStatus
from agent_harness.plugins.manifest import Manifest
from agent_harness.plugins.registry import RegisteredTool
from agent_harness.trace import Event, EventType, RunSummary

console = Console()
err_console = Console(stderr=True)

ARGUMENT_PREVIEW_CHARS = 200
"""终端里参数预览的长度；完整参数仍保存在追踪事件中。"""
STDERR_PREVIEW_LINES = 3
"""工具失败时在运行过程中展示的 stderr 行数；完整末尾保存在追踪中。"""
RUN_STATUS_STYLES = {
    "completed": "green",
    "failed": "red",
    "max_steps_exceeded": "yellow",
    "cancelled": "yellow",
}

STATUS_STYLES = {
    PluginStatus.ACTIVE: "green",
    PluginStatus.DISABLED: "dim",
    PluginStatus.NEEDS_CONFIG: "yellow",
    PluginStatus.NEEDS_APPROVAL: "yellow",
    PluginStatus.INCOMPATIBLE: "red",
    PluginStatus.BROKEN: "red",
}


def render_error(error: HarnessError) -> None:
    err_console.print(f"[bold red]错误：[/]{escape(error.message)}")
    if error.hint:
        err_console.print(f"[yellow]建议：[/]{escape(error.hint)}")


def render_tool_table(tools: list[RegisteredTool]) -> None:
    if not tools:
        console.print(
            "还没有可用的工具：只有已启用的插件会提供工具。\n"
            "用 [bold]harness plugin list[/] 查看插件状态，"
            "用 [bold]harness plugin enable ID[/] 启用插件。"
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


def status_text(status: PluginStatus) -> str:
    return f"[{STATUS_STYLES[status]}]{status.value}[/]"


def render_plugin_table(plugins: list[PluginInfo]) -> None:
    if not plugins:
        console.print("还没有安装插件。用 [bold]harness plugin install PATH[/] 安装。")
        return
    table = Table(title=f"已安装插件（{len(plugins)} 个）", title_justify="left")
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("版本", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("工具", justify="right")
    table.add_column("说明", overflow="fold")
    for info in plugins:
        manifest = info.manifest
        if info.status is PluginStatus.ACTIVE:
            note = escape(manifest.description if manifest else "")
        else:
            note = escape(info.reason or "")
            if info.hint:
                note += f"\n[dim]下一步：{escape(info.hint)}[/]"
        table.add_row(
            escape(info.id),
            escape(manifest.version) if manifest else "-",
            status_text(info.status),
            str(len(info.tool_names)) if manifest else "-",
            note,
        )
    console.print(table)


def render_discovered(directory: str, plugins: list[DiscoveredPlugin]) -> None:
    if not plugins:
        console.print(f"{escape(directory)} 中没有找到插件（包含 plugin.json 的目录）。")
        return
    table = Table(title=f"{escape(directory)} 中的插件（{len(plugins)} 个）", title_justify="left")
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("版本", no_wrap=True)
    table.add_column("兼容", no_wrap=True)
    table.add_column("安装状态", no_wrap=True)
    table.add_column("说明", overflow="fold")
    for item in plugins:
        manifest = item.manifest
        if manifest is None:
            table.add_row(
                escape(item.path.name), "-", "[red]清单无效[/]", "-", escape(item.error or "")
            )
            continue
        compatible = "[green]兼容[/]" if item.incompatible is None else "[red]不兼容[/]"
        status = item.install_status.value if item.install_status else "-"
        if item.install_status is InstallStatus.UPGRADABLE:
            status = f"[yellow]{status}[/]（已安装 {escape(item.installed_version or '')}）"
        note = escape(item.incompatible or manifest.description)
        note += f"\n[dim]{escape(str(item.path))}[/]"
        table.add_row(escape(manifest.id), escape(manifest.version), compatible, status, note)
    console.print(table)


def render_plugin_info(info: PluginInfo) -> None:
    manifest = info.manifest
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("ID", escape(info.id))
    if manifest:
        grid.add_row("名称", escape(f"{manifest.name} {manifest.version}"))
        grid.add_row("说明", escape(manifest.description))
    grid.add_row("状态", status_text(info.status))
    if info.reason:
        grid.add_row("原因", escape(info.reason))
    if info.hint:
        grid.add_row("下一步", escape(info.hint))
    grid.add_row("安装方式", "开发模式（链接到源码）" if info.dev else "复制安装")
    grid.add_row("代码目录", escape(str(info.path)))
    if info.source:
        grid.add_row("安装来源", escape(info.source))
    if info.data_dir:
        grid.add_row("数据目录", escape(str(info.data_dir)))
    if manifest:
        grid.add_row("声明的权限", escape(_permissions_summary(manifest)))
        approved = info.approved
        grid.add_row(
            "已批准",
            "未批准"
            if approved is None
            else ("与声明一致" if approved == manifest.permissions else "与声明不一致"),
        )
        grid.add_row("配置", escape(json.dumps(info.config, ensure_ascii=False)))
        if manifest.config_schema:
            properties = manifest.config_schema.get("properties", {})
            required = set(manifest.config_schema.get("required", []))
            for name, prop in properties.items():
                described = prop.get("description", "") if isinstance(prop, dict) else ""
                kind = prop.get("type", "") if isinstance(prop, dict) else ""
                flag = "必填" if name in required else "可选"
                grid.add_row("", escape(f"· {name}（{kind}，{flag}）{described}"))
        grid.add_row("工具", escape("、".join(info.tool_names)))
    console.print(Panel(grid, title=f"插件 {escape(info.id)}", title_align="left"))


def render_permissions(manifest: Manifest) -> None:
    """启用前展示权限声明，并说明哪些由宿主强制执行、哪些只是插件的自我声明。"""
    permissions = manifest.permissions
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(style="dim")
    env = "、".join(permissions.env) if permissions.env else "无"
    grid.add_row("环境变量", escape(env), "宿主强制执行：插件只能拿到这里列出的变量")
    for label, value in (
        ("网络", permissions.network),
        ("文件系统", permissions.filesystem),
        ("子进程", permissions.subprocess),
    ):
        grid.add_row(label, "需要" if value else "不需要", "插件的自我声明，宿主无法限制")
    console.print(
        Panel(
            grid,
            title=f"插件 {escape(manifest.id)} {escape(manifest.version)} 声明的权限",
            title_align="left",
        )
    )
    console.print(
        "[yellow]注意：[/]插件以你的用户身份运行，宿主没有沙箱，它在技术上可以访问你能访问的"
        "文件和网络。启用即表示你信任这个插件的代码。"
    )


def render_config(config: dict[str, Any]) -> None:
    console.print(Panel(JSON.from_data(config, ensure_ascii=False), title="生效的配置（含默认值）"))


def _permissions_summary(manifest: Manifest) -> str:
    permissions = manifest.permissions
    flags = [
        name
        for name, value in (
            ("网络", permissions.network),
            ("文件系统", permissions.filesystem),
            ("子进程", permissions.subprocess),
        )
        if value
    ]
    env = f"环境变量 {', '.join(permissions.env)}" if permissions.env else "不需要环境变量"
    return f"{'、'.join(flags) if flags else '不需要网络、文件系统或子进程'}；{env}"


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
    """追踪订阅者：把 Agent 运行过程实时打印到终端；replay=True 时用于 runs show 回看，展示更多细节。"""

    def __init__(self, out: Console | None = None, *, replay: bool = False) -> None:
        self._console = out or console
        self._replay = replay
        self._run_id = ""

    def __call__(self, event: Event) -> None:
        payload = event.payload
        if event.type == EventType.RUN_STARTED:
            self._run_id = event.run_id
            self._console.print(
                f"[dim]运行 {event.run_id} · 模型 {escape(str(payload.get('model')))} · "
                f"可用工具 {len(payload.get('tools', []))} 个[/]"
            )
            if self._replay:
                self._console.print(f"[bold]任务：[/]{escape(str(payload.get('prompt', '')))}")
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
            if self._replay and payload.get("result_preview") is not None:
                total = payload.get("result_chars") or 0
                more = f"…（共 {total} 字符）" if total > len(payload["result_preview"]) else ""
                self._console.print(f"    [dim]结果：{escape(payload['result_preview'])}{more}[/]")
            if payload.get("truncated"):
                saved = payload.get("full_result_path")
                where = f"完整结果：{escape(saved)}" if saved else "完整结果未能保存"
                self._console.print(f"    [yellow]结果过长，交给模型的是截断版本；{where}[/]")
        else:
            error = ErrorInfo.model_validate(payload["error"])
            _print_failure(self._console, payload["tool"], error, indent=2)
            stderr = payload.get("stderr_tail")
            if stderr:
                lines = stderr.splitlines()
                shown = lines if self._replay else lines[-STDERR_PREVIEW_LINES:]
                self._console.print(f"    [dim]stderr（最后 {len(shown)} 行）：[/]")
                for line in shown:
                    self._console.print(f"    [dim]{escape(line)}[/]")

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
        footer = f"共 {payload['steps']} 步"
        if not self._replay and self._run_id:
            footer += f" · 回看详情：harness runs show {self._run_id}"
        self._console.print(f"[dim]{footer}[/]")


def render_runs_table(runs: list[RunSummary]) -> None:
    if not runs:
        console.print('还没有运行记录。用 [bold]harness run "任务"[/] 运行一次 Agent。')
        return
    table = Table(title=f"最近的运行（{len(runs)} 个）", title_justify="left")
    table.add_column("运行 ID", style="bold", no_wrap=True)
    table.add_column("开始时间", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("步数", justify="right")
    table.add_column("模型", no_wrap=True)
    table.add_column("任务", overflow="fold")
    for run in runs:
        status = run.status or "未结束"
        style = RUN_STATUS_STYLES.get(status, "dim")
        started = (
            run.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if run.started_at else "-"
        )
        table.add_row(
            escape(run.run_id),
            started,
            f"[{style}]{escape(status)}[/]",
            "-" if run.steps is None else str(run.steps),
            escape(run.model or "-"),
            escape(_shorten(run.prompt or "", 60)),
        )
    console.print(table)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


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
