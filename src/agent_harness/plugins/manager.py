"""Plugin Manager：插件管理操作的唯一入口（DESIGN.md §4.4、§7）。

持久化的只有用户意图（是否启用、配置、已批准的权限声明），有效状态每次加载时计算。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_harness.errors import not_implemented
from agent_harness.plugins.manifest import Manifest
from agent_harness.plugins.registry import ActivePlugin, ToolRegistry
from agent_harness.settings import HarnessPaths
from agent_harness.store import StateStore

ApproveCallback = Callable[[Manifest], bool]
"""启用时的授权确认回调：展示权限声明后返回用户是否同意。"""


class PluginStatus(StrEnum):
    """有效状态，按此顺序判定（DESIGN.md §4.4）。只有 ACTIVE 的插件进入注册表。"""

    BROKEN = "broken"
    INCOMPATIBLE = "incompatible"
    DISABLED = "disabled"
    NEEDS_CONFIG = "needs_config"
    NEEDS_APPROVAL = "needs_approval"
    ACTIVE = "active"


@dataclass(frozen=True)
class PluginInfo:
    id: str
    status: PluginStatus
    path: Path
    manifest: Manifest | None = None
    reason: str | None = None


class PluginManager:
    def __init__(self, paths: HarnessPaths, store: StateStore, *, harness_version: str) -> None:
        self._paths = paths
        self._store = store
        self._harness_version = harness_version

    def list_plugins(self) -> list[PluginInfo]:
        raise not_implemented("PluginManager.list_plugins", "M2")

    def active_plugins(self) -> list[ActivePlugin]:
        raise not_implemented("PluginManager.active_plugins", "M1")

    def build_registry(self) -> ToolRegistry:
        return ToolRegistry(self.active_plugins())

    def install(self, source: Path, *, dev: bool = False) -> PluginInfo:
        raise not_implemented("PluginManager.install", "M1（--dev）/ M2（复制安装）")

    def uninstall(self, plugin_id: str, *, purge: bool = False) -> None:
        raise not_implemented("PluginManager.uninstall", "M2")

    def enable(self, plugin_id: str, *, approve: ApproveCallback) -> PluginInfo:
        raise not_implemented("PluginManager.enable", "M2")

    def disable(self, plugin_id: str) -> PluginInfo:
        raise not_implemented("PluginManager.disable", "M2")

    def configure(
        self,
        plugin_id: str,
        *,
        set_values: Mapping[str, Any] | None = None,
        unset: Iterable[str] = (),
    ) -> dict[str, Any]:
        raise not_implemented("PluginManager.configure", "M2")

    def update(self, plugin_id: str, *, source: Path | None = None) -> PluginInfo:
        raise not_implemented("PluginManager.update", "M4")
