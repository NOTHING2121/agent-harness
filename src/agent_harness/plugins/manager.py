"""Plugin Manager：插件管理操作的唯一入口（DESIGN.md §4.4、§7）。

持久化的只有用户意图（是否启用、配置、已批准的权限声明），有效状态每次加载时计算。
M1 只支持开发模式安装，所有清单有效的已安装插件都视为可用；启用、配置与授权在 M2 加入。
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_harness.errors import HarnessError, ManifestError, not_implemented
from agent_harness.plugins.manifest import Manifest, default_config, load_manifest
from agent_harness.plugins.registry import ActivePlugin, ToolRegistry
from agent_harness.settings import HarnessPaths
from agent_harness.store import StateStore

logger = logging.getLogger(__name__)

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
        """M1：所有清单有效的已安装插件都视为可用；清单有问题的插件跳过并记录警告。"""
        plugins: list[ActivePlugin] = []
        for link in self._installed_links():
            try:
                manifest = load_manifest(link)
            except ManifestError as exc:
                logger.warning("跳过插件 %s：%s", link.name, exc.message)
                continue
            if manifest.id != link.name:
                logger.warning(
                    "跳过插件 %s：清单中的 id 为 %s，与安装名不一致", link.name, manifest.id
                )
                continue
            plugins.append(
                ActivePlugin(
                    manifest=manifest,
                    path=link.resolve(),
                    config=default_config(manifest),
                    data_dir=self._paths.plugin_data_dir(manifest.id),
                )
            )
        return plugins

    def build_registry(self) -> ToolRegistry:
        return ToolRegistry(self.active_plugins())

    def install(self, source: Path, *, dev: bool = False) -> PluginInfo:
        """安装本地目录中的插件。只读取清单、创建链接，不执行插件代码。"""
        if not dev:
            raise HarnessError(
                "当前版本只支持开发模式安装。",
                hint="使用 harness plugin install PATH --dev（复制安装将在 M2 提供）。",
            )
        source = source.expanduser().resolve()
        manifest = load_manifest(source)
        link = self._paths.plugins_dir / manifest.id
        if link.is_symlink() or link.exists():
            if link.resolve() == source:
                return PluginInfo(manifest.id, PluginStatus.ACTIVE, source, manifest, "已安装")
            raise HarnessError(
                f"插件 {manifest.id} 已安装，指向 {link.resolve()}。",
                hint=f"卸载功能将在 M2 提供；目前可删除 {link} 后重新安装。",
            )

        self._paths.plugins_dir.mkdir(parents=True, exist_ok=True)
        temp_link = link.with_name(f".tmp-{manifest.id}-{secrets.token_hex(4)}")
        os.symlink(source, temp_link, target_is_directory=True)
        os.replace(temp_link, link)
        return PluginInfo(manifest.id, PluginStatus.ACTIVE, source, manifest)

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

    def _installed_links(self) -> list[Path]:
        if not self._paths.plugins_dir.is_dir():
            return []
        return sorted(
            entry for entry in self._paths.plugins_dir.iterdir() if not entry.name.startswith(".")
        )
