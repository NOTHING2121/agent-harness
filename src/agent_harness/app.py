"""Harness 应用服务：组合根（DESIGN.md §3.1）。CLI 和测试都通过它使用核心功能。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_harness import __version__
from agent_harness.agent.llm import LLMClient
from agent_harness.agent.loop import AgentLoop, RunResult
from agent_harness.plugins.executor import ConfirmCallback, ToolExecutor
from agent_harness.plugins.manager import PluginManager
from agent_harness.plugins.runner import PluginRunner
from agent_harness.settings import HarnessPaths, Settings, load_settings
from agent_harness.store import StateStore
from agent_harness.trace import Subscriber, Trace


class Harness:
    def __init__(self, paths: HarnessPaths, settings: Settings) -> None:
        self.paths = paths
        self.settings = settings
        self.store = StateStore(paths)
        self.manager = PluginManager(paths, self.store, harness_version=__version__)
        self.runner = PluginRunner(settings.plugins)

    @classmethod
    def open(cls, home: Path | None = None, env: Mapping[str, str] | None = None) -> Harness:
        """按 HARNESS_HOME（或显式传入的 home）和配置文件创建实例，不会创建任何目录。"""
        if home is None:
            paths = HarnessPaths.from_env(env)
        else:
            paths = HarnessPaths(home=home.expanduser().absolute())
        return cls(paths, load_settings(paths, env))

    def run(
        self,
        prompt: str,
        llm: LLMClient,
        *,
        confirm: ConfirmCallback | None = None,
        subscribers: Iterable[Subscriber] = (),
    ) -> RunResult:
        """运行一次 Agent：用当前 active 插件构建注册表，执行 Agent Loop。"""
        registry = self.manager.build_registry()
        executor = ToolExecutor(
            registry,
            self.runner,
            max_result_chars=self.settings.agent.tool_result_max_chars,
            confirm=confirm,
        )
        loop = AgentLoop(llm, registry, executor, max_steps=self.settings.agent.max_steps)
        return loop.run(prompt, Trace(subscribers=subscribers))
