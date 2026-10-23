"""Plugin Manager：插件管理操作的唯一入口（DESIGN.md §4.4、§7）。

持久化的只有用户意图（是否启用、配置、已批准的权限声明），有效状态每次加载时计算。
所有修改操作都在 StateStore.transaction 的文件锁内执行。文件操作按 §7.2 的顺序进行：
“插件已安装”的唯一依据是 plugins/<id> 这个链接，链接通过 os.replace 原子地创建或删除；
任意一步之后中断都不会出现半装状态，残留的副本和记录由下一次管理操作自动清理。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from packaging.version import Version

from agent_harness.errors import HarnessError, ManifestError
from agent_harness.plugins.manifest import (
    PLUGIN_ID_PATTERN,
    Manifest,
    Permissions,
    check_compatibility,
    config_problems,
    default_config,
    load_manifest,
    qualified_tool_name,
)
from agent_harness.plugins.registry import ActivePlugin, ToolRegistry
from agent_harness.settings import HarnessPaths
from agent_harness.store import PluginState, State, StateStore
from harness_sdk.protocol import MANIFEST_FILE

logger = logging.getLogger(__name__)

ApproveCallback = Callable[[Manifest], bool]
"""启用时的授权确认回调：展示权限声明后返回用户是否同意。"""

COPY_IGNORED_DIRS = frozenset({".git", "__pycache__", ".venv"})
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
TMP_PREFIX = ".tmp-"


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
    """一个已安装插件的完整视图：有效状态、原因、下一步建议，以及 info 命令展示的细节。"""

    id: str
    status: PluginStatus
    path: Path
    manifest: Manifest | None = None
    reason: str | None = None
    hint: str | None = None
    dev: bool = False
    source: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    approved: Permissions | None = None
    data_dir: Path | None = None

    @property
    def tool_names(self) -> list[str]:
        """插件提供的工具全名（<plugin_id>__<tool_name>）；清单无效时为空。"""
        if self.manifest is None:
            return []
        return [qualified_tool_name(self.id, tool.name) for tool in self.manifest.tools]


class InstallStatus(StrEnum):
    """discover 中每个候选插件相对于已安装版本的状态。"""

    NOT_INSTALLED = "未安装"
    INSTALLED = "已安装"
    UPGRADABLE = "有新版本"
    DEV = "已安装（开发模式）"


@dataclass(frozen=True)
class DiscoveredPlugin:
    """discover 扫描到的一个插件目录。清单无效时 manifest 为 None，原因在 error 中。"""

    path: Path
    manifest: Manifest | None = None
    error: str | None = None
    incompatible: str | None = None
    install_status: InstallStatus | None = None
    installed_version: str | None = None


class PluginManager:
    def __init__(self, paths: HarnessPaths, store: StateStore, *, harness_version: str) -> None:
        self._paths = paths
        self._store = store
        self._harness_version = harness_version

    # ---- 查询：只读，不加锁 ----

    def list_plugins(self) -> list[PluginInfo]:
        state = self._store.load()
        return [self._describe(link, state.plugins.get(link.name)) for link in self._links()]

    def get(self, plugin_id: str) -> PluginInfo:
        """查询单个插件；未安装时抛出 HarnessError。"""
        link = self._installed_link(plugin_id)
        return self._describe(link, self._store.load().plugins.get(plugin_id))

    def find(self, plugin_id: str) -> PluginInfo | None:
        try:
            return self.get(plugin_id)
        except HarnessError:
            return None

    def active_plugins(self) -> list[ActivePlugin]:
        """只有 ACTIVE 的插件可用；清单损坏的插件跳过并记录警告。"""
        plugins: list[ActivePlugin] = []
        for info in self.list_plugins():
            if info.status is PluginStatus.BROKEN:
                logger.warning("跳过插件 %s：%s", info.id, info.reason)
            if info.status is not PluginStatus.ACTIVE:
                continue
            assert info.manifest is not None and info.data_dir is not None  # ACTIVE 时一定存在
            plugins.append(
                ActivePlugin(
                    manifest=info.manifest,
                    path=info.path,
                    config=info.config,
                    data_dir=info.data_dir,
                )
            )
        return plugins

    def build_registry(self) -> ToolRegistry:
        return ToolRegistry(self.active_plugins())

    def validate(self, source: Path) -> tuple[Manifest, str | None]:
        """静态校验插件目录：清单不合法时抛出 ManifestError；返回清单和不兼容的原因。"""
        manifest = load_manifest(source.expanduser().resolve())
        return manifest, check_compatibility(manifest, self._harness_version)

    # ---- 修改：都在文件锁内执行 ----

    def install(self, source: Path, *, dev: bool = False) -> PluginInfo:
        """安装本地目录中的插件，安装后处于未启用状态。只读取清单和复制文件，不执行插件代码。

        开发模式只创建指向源码目录的链接；重复安装同一来源视为成功（幂等）。
        """
        source = source.expanduser().resolve()
        manifest = load_manifest(source)
        reason = check_compatibility(manifest, self._harness_version)
        if reason:
            raise HarnessError(
                f"插件 {manifest.id} 与当前 Harness 不兼容：{reason}。",
                hint="换用与当前版本兼容的插件，或升级 Harness。",
            )
        with self._store.transaction() as state:
            self._cleanup(state)
            link = self._paths.plugins_dir / manifest.id
            if link.is_symlink() or link.exists():
                if dev and link.is_symlink() and Path(os.path.realpath(link)) == source:
                    state.plugins.setdefault(manifest.id, PluginState(source=str(source), dev=True))
                    return self._describe(link, state.plugins[manifest.id])
                raise HarnessError(
                    f"插件 {manifest.id} 已安装（{os.path.realpath(link)}）。",
                    hint=f"先执行 harness plugin uninstall {manifest.id}，再重新安装。",
                )
            self._paths.plugins_dir.mkdir(parents=True, exist_ok=True)
            target = source if dev else self._copy_package(source, manifest)
            _atomic_symlink(target, link)
            record = PluginState(source=str(source), dev=dev)
            state.plugins[manifest.id] = record
            return self._describe(link, record)

    def uninstall(self, plugin_id: str, *, purge: bool = False) -> Path | None:
        """卸载插件，返回保留下来的数据目录（不存在或已用 purge 删除时返回 None）。

        顺序：删除链接（插件即不存在）→ 删除副本（开发模式不删除用户源码）→ purge 数据目录；
        最后在锁内写回去掉记录的状态。任何一步之后中断，残留的副本或记录都会被自动清理。
        """
        with self._store.transaction() as state:
            link = self._installed_link(plugin_id)
            target = Path(os.path.realpath(link))
            state.plugins.pop(plugin_id, None)
            link.unlink()
            if _is_inside(target, self._paths.packages_dir):
                _remove_tree(target)
            data_dir = self._paths.plugin_data_dir(plugin_id)
            if purge and data_dir.exists():
                try:
                    shutil.rmtree(data_dir)
                except OSError as exc:
                    raise HarnessError(
                        f"插件已卸载，但删除数据目录 {data_dir} 失败：{exc}",
                        hint="检查该目录的权限后手动删除。",
                    ) from exc
            self._cleanup(state)
        return data_dir if data_dir.exists() else None

    def enable(self, plugin_id: str, *, approve: ApproveCallback) -> PluginInfo:
        """兼容检查 → 配置校验 → 展示权限声明并请求确认 → 记录已批准的声明并启用。

        确认在锁外进行，避免等待用户输入时阻塞其他管理操作；写入前重新核对声明没有变化。
        """
        info = self.get(plugin_id)
        if info.status is PluginStatus.ACTIVE:
            return info
        manifest = self._require_enableable(info)
        if not approve(manifest):
            raise HarnessError(
                f"没有确认授权，插件 {plugin_id} 保持未启用。",
                hint="在交互式终端中回答 y 确认；非交互环境中可以加 --yes。",
            )
        with self._store.transaction() as state:
            link = self._installed_link(plugin_id)
            current = self._describe(link, state.plugins.get(plugin_id))
            if current.manifest is None or current.manifest.permissions != manifest.permissions:
                raise HarnessError(
                    f"确认期间插件 {plugin_id} 的清单发生了变化，没有启用。",
                    hint=f"重新执行 harness plugin enable {plugin_id}。",
                )
            record = state.plugins.setdefault(plugin_id, PluginState())
            record.enabled = True
            record.approved = manifest.permissions
            return self._describe(link, record)

    def disable(self, plugin_id: str) -> PluginInfo:
        """禁用并撤销授权（清除已批准的声明）；配置保留，重新启用时需要再次确认。"""
        with self._store.transaction() as state:
            link = self._installed_link(plugin_id)
            record = state.plugins.setdefault(plugin_id, PluginState())
            record.enabled = False
            record.approved = None
            return self._describe(link, record)

    def configure(
        self,
        plugin_id: str,
        *,
        set_values: Mapping[str, Any] | None = None,
        unset: Iterable[str] = (),
    ) -> dict[str, Any]:
        """修改配置并返回生效的配置（含默认值）；合并后的配置不合法时拒绝，不做任何修改。"""
        set_values = dict(set_values or {})
        unset = list(unset)
        if not set_values and not unset:
            return self.get(plugin_id).config
        with self._store.transaction() as state:
            link = self._installed_link(plugin_id)
            info = self._describe(link, state.plugins.get(plugin_id))
            if info.manifest is None:
                raise HarnessError(
                    f"插件 {plugin_id} 的清单无效，无法配置：{info.reason}", hint=info.hint
                )
            record = state.plugins.setdefault(plugin_id, PluginState())
            config = {**record.config, **set_values}
            for key in unset:
                if key not in config:
                    raise HarnessError(
                        f"配置项 {key} 没有设置过（当前使用默认值或未配置），无需删除。"
                    )
                del config[key]
            problems = config_problems(info.manifest, {**default_config(info.manifest), **config})
            if problems:
                raise HarnessError(
                    "配置不合法，没有保存：\n" + "\n".join(f"  - {p}" for p in problems),
                    hint=f"用 harness plugin info {plugin_id} 查看插件支持的配置项。",
                )
            record.config = config
            return self._describe(link, record).config

    def discover(self, directory: Path) -> list[DiscoveredPlugin]:
        """扫描目录本身及其直接子目录中的插件，只读取清单，不执行代码。"""
        directory = directory.expanduser().resolve()
        if not directory.is_dir():
            raise HarnessError(f"{directory} 不是目录。")
        candidates = [directory] if (directory / MANIFEST_FILE).is_file() else []
        candidates += sorted(
            child
            for child in directory.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (child / MANIFEST_FILE).is_file()
        )
        found: list[DiscoveredPlugin] = []
        for path in candidates:
            try:
                manifest = load_manifest(path)
            except ManifestError as exc:
                found.append(DiscoveredPlugin(path=path, error=exc.message))
                continue
            installed = self.find(manifest.id)
            status = InstallStatus.NOT_INSTALLED
            installed_version = None
            if installed is not None:
                installed_version = installed.manifest.version if installed.manifest else None
                if installed.dev:
                    status = InstallStatus.DEV
                elif installed_version and Version(manifest.version) > Version(installed_version):
                    status = InstallStatus.UPGRADABLE
                else:
                    status = InstallStatus.INSTALLED
            found.append(
                DiscoveredPlugin(
                    path=path,
                    manifest=manifest,
                    incompatible=check_compatibility(manifest, self._harness_version),
                    install_status=status,
                    installed_version=installed_version,
                )
            )
        return found

    def update(
        self, plugin_id: str, *, source: Path | None = None
    ) -> tuple[PluginInfo, str | None]:
        """从安装来源（或 source）更新复制安装的插件，返回 (新状态, 旧版本号)。

        新版本号必须更高。流程同安装的前 4 步，然后用 os.replace 把链接原子地切换到新副本，
        最后删除旧副本。启用状态、配置保留；权限声明变化时有效状态自动变为 needs_approval。
        """
        with self._store.transaction() as state:
            self._cleanup(state)
            link = self._installed_link(plugin_id)
            old_target = Path(os.path.realpath(link))
            record = state.plugins.get(plugin_id)
            if not _is_inside(old_target, self._paths.packages_dir):
                raise HarnessError(
                    f"插件 {plugin_id} 是开发模式安装的，直接链接到源码目录，修改后立即生效，无需更新。"
                )
            raw_source = source or (Path(record.source) if record and record.source else None)
            if raw_source is None:
                raise HarnessError(
                    f"没有记录插件 {plugin_id} 的安装来源。",
                    hint=f"用 harness plugin update {plugin_id} --from <插件目录> 指定来源。",
                )
            new_source = raw_source.expanduser().resolve()
            manifest = load_manifest(new_source)
            if manifest.id != plugin_id:
                raise HarnessError(
                    f"{new_source} 中的插件 ID 为 {manifest.id}，不是 {plugin_id}，没有更新。"
                )
            reason = check_compatibility(manifest, self._harness_version)
            if reason:
                raise HarnessError(
                    f"新版本 {manifest.version} 与当前 Harness 不兼容：{reason}。没有更新。"
                )
            current = self._describe(link, record)
            old_version = current.manifest.version if current.manifest else None
            if old_version and Version(manifest.version) <= Version(old_version):
                raise HarnessError(
                    f"{new_source} 中的版本为 {manifest.version}，不高于已安装的 {old_version}，没有更新。",
                    hint="更新前先提高 plugin.json 中的 version。",
                )
            new_target = self._copy_package(new_source, manifest)
            _atomic_symlink(new_target, link)
            _remove_tree(old_target)
            record = state.plugins.setdefault(plugin_id, PluginState())
            record.source = str(new_source)
            return self._describe(link, record), old_version

    # ---- 内部实现 ----

    def _describe(self, link: Path, record: PluginState | None) -> PluginInfo:
        """按 DESIGN.md §4.4 的顺序计算有效状态，并给出原因和下一步建议。"""
        plugin_id = link.name
        path = Path(os.path.realpath(link))
        details: dict[str, Any] = {
            "dev": record.dev if record else False,
            "source": record.source if record else None,
            "approved": record.approved if record else None,
            "data_dir": self._paths.plugin_data_dir(plugin_id),
        }

        def make(
            status: PluginStatus, reason: str | None = None, hint: str | None = None
        ) -> PluginInfo:
            return PluginInfo(plugin_id, status, path, reason=reason, hint=hint, **details)

        broken_hint = (
            f"修复插件目录后重新安装：harness plugin uninstall {plugin_id}，"
            "再 harness plugin install <目录>。"
        )
        if not path.is_dir():
            return make(PluginStatus.BROKEN, f"插件目录不存在：{path}", broken_hint)
        try:
            manifest = load_manifest(path)
        except ManifestError as exc:
            return make(PluginStatus.BROKEN, exc.message, broken_hint)
        if manifest.id != plugin_id:
            reason = f"清单中的 id 为 {manifest.id}，与安装名 {plugin_id} 不一致"
            return make(PluginStatus.BROKEN, reason, broken_hint)

        details["manifest"] = manifest
        details["config"] = {**default_config(manifest), **(record.config if record else {})}
        reason = check_compatibility(manifest, self._harness_version)
        if reason:
            return make(PluginStatus.INCOMPATIBLE, reason, "换用兼容的插件版本，或升级 Harness。")
        problems = config_problems(manifest, details["config"])
        field_name = problems[0].partition(":")[0] if problems else "字段"
        if field_name == "(根)":
            field_name = "字段"
        config_hint = f"harness plugin config {plugin_id} --set {field_name}=值"
        if record is None or not record.enabled:
            if problems:
                reason = f"未启用；启用前需要先配置：{'；'.join(problems)}"
                return make(PluginStatus.DISABLED, reason, config_hint)
            return make(PluginStatus.DISABLED, "未启用", f"harness plugin enable {plugin_id}")
        if problems:
            return make(PluginStatus.NEEDS_CONFIG, "；".join(problems), config_hint)
        if record.approved != manifest.permissions:
            return make(
                PluginStatus.NEEDS_APPROVAL,
                "当前的权限声明与已批准的不一致，需要重新确认",
                f"harness plugin enable {plugin_id}",
            )
        return make(PluginStatus.ACTIVE)

    def _require_enableable(self, info: PluginInfo) -> Manifest:
        if info.status in (PluginStatus.BROKEN, PluginStatus.INCOMPATIBLE):
            raise HarnessError(f"无法启用插件 {info.id}：{info.reason}", hint=info.hint)
        assert info.manifest is not None  # 非 BROKEN 时一定有清单
        problems = config_problems(info.manifest, info.config)
        if problems:
            raise HarnessError(
                f"插件 {info.id} 的配置不完整或不合法，没有启用：\n"
                + "\n".join(f"  - {p}" for p in problems),
                hint=f"先用 harness plugin config {info.id} --set 字段=值 补全配置，再启用。",
            )
        return info.manifest

    def _installed_link(self, plugin_id: str) -> Path:
        link = self._paths.plugins_dir / plugin_id
        if not re.fullmatch(PLUGIN_ID_PATTERN, plugin_id) or not link.is_symlink():
            raise HarnessError(
                f"插件 {plugin_id} 未安装。", hint="用 harness plugin list 查看已安装的插件。"
            )
        return link

    def _links(self) -> list[Path]:
        if not self._paths.plugins_dir.is_dir():
            return []
        links: list[Path] = []
        for entry in sorted(self._paths.plugins_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                links.append(entry)
            else:
                logger.warning("忽略 %s：不是 harness 创建的插件链接", entry)
        return links

    def _copy_package(self, source: Path, manifest: Manifest) -> Path:
        """复制到临时目录 → 重新校验副本的清单 → 重命名为正式的副本目录（§7.2 第 2–4 步）。"""
        packages = self._paths.packages_dir
        packages.mkdir(parents=True, exist_ok=True)
        tmp = packages / f"{TMP_PREFIX}{secrets.token_hex(4)}"
        try:
            copy_plugin_tree(source, tmp)
            copied = load_manifest(tmp)
            if (copied.id, copied.version) != (manifest.id, manifest.version):
                raise HarnessError("复制过程中插件清单发生了变化，请重新安装。")
            final = packages / f"{manifest.id}-{manifest.version}-{secrets.token_hex(4)}"
            tmp.rename(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)  # 只删除本次创建的临时目录，失败时下次清理
            raise
        return final

    def _cleanup(self, state: State) -> None:
        """自动清理：未被引用的副本、残留的临时目录与链接、没有对应插件的状态记录。"""
        links = self._links()
        referenced = {Path(os.path.realpath(link)) for link in links}
        if self._paths.plugins_dir.is_dir():
            for entry in self._paths.plugins_dir.iterdir():
                if entry.name.startswith(TMP_PREFIX) and entry.is_symlink():
                    entry.unlink()
        if self._paths.packages_dir.is_dir():
            for entry in self._paths.packages_dir.iterdir():
                if Path(os.path.realpath(entry)) not in referenced:
                    logger.info("清理未被引用的插件副本 %s", entry)
                    _remove_tree(entry)
        installed = {link.name for link in links}
        for plugin_id in [pid for pid in state.plugins if pid not in installed]:
            del state.plugins[plugin_id]


def copy_plugin_tree(source: Path, destination: Path) -> None:
    """复制插件目录：跳过 .git、__pycache__、.venv；拒绝符号链接；总大小不超过 50 MB。"""
    destination.mkdir()
    total = 0
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        relative = root_path.relative_to(source)
        dirs[:] = sorted(name for name in dirs if name not in COPY_IGNORED_DIRS)
        for name in [*dirs, *files]:
            if (root_path / name).is_symlink():
                raise HarnessError(
                    f"插件目录中包含符号链接 {relative / name}，为安全起见拒绝安装。",
                    hint="把链接替换为实际文件，或使用 --dev 开发模式安装。",
                )
        for name in dirs:
            (destination / relative / name).mkdir()
        for name in sorted(files):
            total += (root_path / name).stat().st_size
            if total > MAX_PACKAGE_BYTES:
                raise HarnessError(
                    f"插件目录超过 {MAX_PACKAGE_BYTES // (1024 * 1024)} MB 的大小上限，拒绝安装。",
                    hint="删除插件目录中不需要的大文件（如数据、构建产物）后重试。",
                )
            shutil.copy2(root_path / name, destination / relative / name)


def _atomic_symlink(target: Path, link: Path) -> None:
    """先创建临时链接，再用 os.replace 原子地替换为正式链接。副本使用相对路径。"""
    if _is_inside(target, link.parent.parent):
        target_text = os.path.relpath(target, link.parent)
    else:
        target_text = str(target)
    temp_link = link.with_name(f"{TMP_PREFIX}{link.name}-{secrets.token_hex(4)}")
    os.symlink(target_text, temp_link, target_is_directory=True)
    os.replace(temp_link, link)


def _is_inside(path: Path, directory: Path) -> bool:
    root = Path(os.path.realpath(directory))
    resolved = Path(os.path.realpath(path))
    return resolved != root and resolved.is_relative_to(root)


def _remove_tree(path: Path) -> None:
    """删除副本目录；失败时记录警告，下一次管理操作会再次尝试清理。"""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except OSError as exc:
        logger.warning("删除 %s 失败（下次管理操作时会重试）：%s", path, exc)
