"""Harness 应用服务：组合根（DESIGN.md §3.1）。CLI 和测试都通过它使用核心功能。"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_harness import __version__
from agent_harness.agent.llm import LLMClient, OpenAICompatibleClient, ScriptedClient
from agent_harness.agent.loop import AgentLoop, RunResult
from agent_harness.errors import ConfigError, HarnessError
from agent_harness.plugins.executor import ConfirmCallback, ToolExecutor, ToolResult
from agent_harness.plugins.manager import PluginInfo, PluginManager
from agent_harness.plugins.registry import ToolRegistry
from agent_harness.plugins.runner import PluginRunner
from agent_harness.settings import HarnessPaths, Settings, load_settings
from agent_harness.store import StateStore
from agent_harness.trace import Subscriber, Trace

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

    def tools(self) -> ToolRegistry:
        """当前可用插件提供的全部工具。"""
        return self.manager.build_registry()

    def call_tool(
        self, name: str, raw_arguments: str, *, confirm: ConfirmCallback | None = None
    ) -> ToolResult:
        """绕过 LLM 直接调用一个工具，走与 Agent 完全相同的执行链路（调试用）。"""
        executor = self._executor(self.tools(), confirm)
        return executor.execute(f"c_{secrets.token_hex(4)}", name, raw_arguments)

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
        loop = AgentLoop(
            llm,
            registry,
            self._executor(registry, confirm),
            max_steps=max_steps or self.settings.agent.max_steps,
        )
        return loop.run(prompt, Trace(subscribers=subscribers))

    def _executor(self, registry: ToolRegistry, confirm: ConfirmCallback | None) -> ToolExecutor:
        return ToolExecutor(
            registry,
            self.runner,
            max_result_chars=self.settings.agent.tool_result_max_chars,
            confirm=confirm,
        )
