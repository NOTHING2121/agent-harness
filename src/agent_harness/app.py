"""Harness 应用服务：组合根（DESIGN.md §3.1）。CLI 和测试都通过它使用核心功能。"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_harness import __version__
from agent_harness.agent.llm import LLMClient, OpenAICompatibleClient, ScriptedClient
from agent_harness.agent.loop import AgentLoop, RunResult
from agent_harness.errors import ConfigError, ErrorCode, HarnessError
from agent_harness.plugins.executor import ConfirmCallback, ToolExecutor, ToolResult
from agent_harness.plugins.manager import (
    ApproveCallback,
    DiscoveredPlugin,
    PluginInfo,
    PluginManager,
    PluginStatus,
)
from agent_harness.plugins.manifest import Manifest, split_tool_name
from agent_harness.plugins.registry import ToolRegistry
from agent_harness.plugins.runner import PluginRunner
from agent_harness.plugins.scaffold import create_plugin
from agent_harness.settings import HarnessPaths, Settings, load_settings
from agent_harness.store import StateStore
from agent_harness.trace import (
    Event,
    RunSummary,
    Subscriber,
    Trace,
    is_run_id,
    list_trace_files,
    read_events,
    summarize_run,
)

LLM_PROVIDERS = ("openai", "scripted")


class Harness:
    def __init__(
        self, paths: HarnessPaths, settings: Settings, env: Mapping[str, str] | None = None
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.env = os.environ if env is None else env
        self.store = StateStore(paths)
        self.manager = PluginManager(paths, self.store, harness_version=__version__)
        self.runner = PluginRunner(settings.plugins, self.env)

    @classmethod
    def open(cls, home: Path | None = None, env: Mapping[str, str] | None = None) -> Harness:
        """按 HARNESS_HOME（或显式传入的 home）和配置文件创建实例，不会创建任何目录。"""
        if home is None:
            paths = HarnessPaths.from_env(env)
        else:
            paths = HarnessPaths(home=home.expanduser().absolute())
        return cls(paths, load_settings(paths, env), env)

    def install_plugin(self, source: Path, *, dev: bool = False) -> PluginInfo:
        return self.manager.install(source, dev=dev)

    def uninstall_plugin(self, plugin_id: str, *, purge: bool = False) -> Path | None:
        """卸载插件，返回保留下来的数据目录（没有保留时为 None）。"""
        return self.manager.uninstall(plugin_id, purge=purge)

    def list_plugins(self) -> list[PluginInfo]:
        return self.manager.list_plugins()

    def plugin_info(self, plugin_id: str) -> PluginInfo:
        return self.manager.get(plugin_id)

    def enable_plugin(self, plugin_id: str, *, approve: ApproveCallback) -> PluginInfo:
        return self.manager.enable(plugin_id, approve=approve)

    def disable_plugin(self, plugin_id: str) -> PluginInfo:
        return self.manager.disable(plugin_id)

    def configure_plugin(
        self,
        plugin_id: str,
        *,
        set_values: Mapping[str, Any] | None = None,
        unset: Iterable[str] = (),
    ) -> dict[str, Any]:
        return self.manager.configure(plugin_id, set_values=set_values, unset=unset)

    def validate_plugin(self, source: Path) -> tuple[Manifest, str | None]:
        return self.manager.validate(source)

    def update_plugin(
        self, plugin_id: str, *, source: Path | None = None
    ) -> tuple[PluginInfo, str | None]:
        """更新复制安装的插件，返回 (新状态, 旧版本号)。"""
        return self.manager.update(plugin_id, source=source)

    def discover_plugins(self, directory: Path) -> list[DiscoveredPlugin]:
        return self.manager.discover(directory)

    def new_plugin(self, plugin_id: str, parent: Path) -> Path:
        """从模板生成插件骨架，返回插件目录。"""
        return create_plugin(plugin_id, parent)

    def tools(self) -> ToolRegistry:
        """当前可用插件提供的全部工具。"""
        return self.manager.build_registry()

    def call_tool(
        self, name: str, raw_arguments: str, *, confirm: ConfirmCallback | None = None
    ) -> ToolResult:
        """绕过 LLM 直接调用一个工具，走与 Agent 完全相同的执行链路（调试用）。"""
        executor = self._executor(self.tools(), confirm)
        result = executor.execute(f"c_{secrets.token_hex(4)}", name, raw_arguments)
        if result.error is not None and result.error.code is ErrorCode.UNKNOWN_TOOL:
            hint = self._inactive_plugin_hint(name)
            if hint:
                error = result.error.model_copy(update={"hint": hint})
                result = result.model_copy(update={"error": error})
        return result

    def create_llm(
        self,
        provider: str | None = None,
        *,
        script: Path | None = None,
        model: str | None = None,
    ) -> LLMClient:
        """按参数和配置创建 LLM 客户端；只给了场景文件时使用离线回放。"""
        provider = provider or ("scripted" if script else self.settings.llm.provider)
        if provider == "scripted":
            if script is None:
                raise HarnessError(
                    "离线回放模式需要指定场景文件。",
                    hint="使用 --script 指定场景文件，例如 demos/ 目录下的 JSON 文件。",
                )
            return ScriptedClient.from_file(script)
        if provider == "openai":
            settings = self.settings.llm
            if model:
                settings = settings.model_copy(update={"model": model})
            api_key = self.env.get(settings.api_key_env, "").strip()
            if not api_key:
                raise ConfigError(
                    f"没有找到 API Key：环境变量 {settings.api_key_env} 未设置。",
                    hint=(
                        f"在项目根目录的 .env 文件中填写 {settings.api_key_env}=...（参考 "
                        ".env.example），或使用 --llm scripted --script <场景文件> 离线运行。"
                    ),
                )
            return OpenAICompatibleClient(settings, api_key)
        raise HarnessError(
            f"未知的 LLM 提供方：{provider}", hint=f"可选值：{'、'.join(LLM_PROVIDERS)}"
        )

    def run(
        self,
        prompt: str,
        llm: LLMClient,
        *,
        confirm: ConfirmCallback | None = None,
        subscribers: Iterable[Subscriber] = (),
        max_steps: int | None = None,
    ) -> RunResult:
        """运行一次 Agent：用当前可用插件构建注册表，执行 Agent Loop。"""
        registry = self.tools()
        trace = Trace(subscribers=subscribers, log_dir=self.paths.runs_dir)
        loop = AgentLoop(
            llm,
            registry,
            self._executor(registry, confirm, results_dir=self.run_results_dir(trace.run_id)),
            max_steps=max_steps or self.settings.agent.max_steps,
        )
        return loop.run(prompt, trace)

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        """最近的运行，新的在前。"""
        return [summarize_run(path) for path in list_trace_files(self.paths.runs_dir)[:limit]]

    def run_events(self, run_id: str) -> list[Event]:
        """读取一次运行的全部追踪事件。"""
        path = self.paths.runs_dir / f"{run_id}.jsonl"
        if not is_run_id(run_id) or not path.is_file():
            raise HarnessError(
                f"找不到运行记录 {run_id}。", hint="用 harness runs list 查看最近的运行。"
            )
        return read_events(path)

    def run_results_dir(self, run_id: str) -> Path:
        """被截断的大结果的完整内容保存在这里（DESIGN.md §8.4）。"""
        return self.paths.runs_dir / run_id / "results"

    def _executor(
        self,
        registry: ToolRegistry,
        confirm: ConfirmCallback | None,
        *,
        results_dir: Path | None = None,
    ) -> ToolExecutor:
        return ToolExecutor(
            registry,
            self.runner,
            max_result_chars=self.settings.agent.tool_result_max_chars,
            confirm=confirm,
            results_dir=results_dir,
        )

    def _inactive_plugin_hint(self, tool_name: str) -> str | None:
        """工具找不到、但所属插件已安装却不可用时，说明插件的状态和下一步。"""
        try:
            plugin_id, _ = split_tool_name(tool_name)
        except ValueError:
            return None
        info = self.manager.find(plugin_id)
        if info is None or info.status is PluginStatus.ACTIVE:
            return None
        return (
            f"插件 {plugin_id} 当前状态为 {info.status.value}（{info.reason}）。下一步：{info.hint}"
        )
