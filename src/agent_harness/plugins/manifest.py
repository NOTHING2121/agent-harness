"""插件清单 plugin.json 的模型与静态校验（DESIGN.md §4）。

清单是宿主了解插件的唯一来源：安装、查看、授权和生成工具列表都只读取清单，不执行插件代码。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agent_harness.errors import ManifestError, format_validation_error, not_implemented
from harness_sdk.protocol import API_VERSION, MANIFEST_FILE

__all__ = [
    "MANIFEST_FILE",
    "SUPPORTED_API_VERSIONS",
    "Manifest",
    "Permissions",
    "ToolSpec",
    "check_compatibility",
    "default_config",
    "load_manifest",
    "qualified_tool_name",
    "split_tool_name",
]

SUPPORTED_API_VERSIONS: frozenset[int] = frozenset({API_VERSION})
PLUGIN_ID_PATTERN = r"^[a-z][a-z0-9-]{0,29}$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"
TOOL_NAME_SEPARATOR = "__"
MAX_TOOL_TIMEOUT_S = 120.0


def check_object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """校验 schema 是合法的 JSON Schema，且顶层 type 为 object。"""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"不是合法的 JSON Schema：{exc.message}") from exc
    if schema.get("type") != "object":
        raise ValueError('顶层 type 必须为 "object"')
    return schema


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Permissions(_Model):
    """权限声明（DESIGN.md §10.3）。只有 env 由宿主强制执行，其余字段仅用于告知用户。"""

    network: bool = False
    filesystem: bool = False
    subprocess: bool = False
    env: list[Annotated[str, Field(pattern=ENV_NAME_PATTERN)]] = Field(default_factory=list)


class ToolSpec(_Model):
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]
    requires_confirmation: bool = False
    timeout_s: float | None = Field(default=None, gt=0, le=MAX_TOOL_TIMEOUT_S)

    @field_validator("input_schema")
    @classmethod
    def _check_input_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        return check_object_schema(schema)


class Manifest(_Model):
    api_version: int = Field(ge=1)
    id: str = Field(pattern=PLUGIN_ID_PATTERN)
    name: str = Field(min_length=1)
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = Field(min_length=1)
    harness_version: str | None = None
    entry: str = Field(min_length=1)
    permissions: Permissions = Field(default_factory=Permissions)
    config_schema: dict[str, Any] | None = None
    tools: list[ToolSpec] = Field(min_length=1)

    @field_validator("harness_version")
    @classmethod
    def _check_harness_version(cls, spec: str | None) -> str | None:
        if spec is not None:
            try:
                SpecifierSet(spec)
            except InvalidSpecifier as exc:
                raise ValueError(f"不是合法的 PEP 440 版本范围：{spec!r}") from exc
        return spec

    @field_validator("entry")
    @classmethod
    def _check_entry(cls, entry: str) -> str:
        path = PurePosixPath(entry)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("必须是插件目录内的相对路径")
        return entry

    @field_validator("config_schema")
    @classmethod
    def _check_config_schema(cls, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if schema is None else check_object_schema(schema)

    @model_validator(mode="after")
    def _check_unique_tool_names(self) -> Manifest:
        names = [tool.name for tool in self.tools]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"工具名重复：{', '.join(duplicates)}")
        return self

    def get_tool(self, name: str) -> ToolSpec | None:
        return next((tool for tool in self.tools if tool.name == name), None)


def qualified_tool_name(plugin_id: str, tool_name: str) -> str:
    """暴露给 LLM 的工具名：<plugin_id>__<tool_name>。"""
    return f"{plugin_id}{TOOL_NAME_SEPARATOR}{tool_name}"


def split_tool_name(qualified_name: str) -> tuple[str, str]:
    """按第一个 "__" 拆分；插件 ID 不含下划线，所以拆分没有歧义。"""
    plugin_id, separator, tool_name = qualified_name.partition(TOOL_NAME_SEPARATOR)
    if not separator or not plugin_id or not tool_name:
        raise ValueError(f"不是合法的工具名：{qualified_name!r}")
    return plugin_id, tool_name


def load_manifest(plugin_dir: Path) -> Manifest:
    """读取并静态校验插件目录中的 plugin.json，不执行插件代码；失败时抛出 ManifestError。"""
    manifest_path = plugin_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ManifestError(
            f"{plugin_dir} 中找不到 {MANIFEST_FILE}",
            hint="插件目录必须包含 plugin.json，可参考 plugins/ 下的示例插件。",
        )
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"无法读取 {manifest_path}：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"{manifest_path} 不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"{manifest_path} 校验失败：\n{format_validation_error(exc)}") from exc

    root = plugin_dir.resolve()
    entry = (root / manifest.entry).resolve()
    if not entry.is_relative_to(root):
        raise ManifestError(f"入口 {manifest.entry} 解析后位于插件目录之外：{entry}")
    if not entry.is_file():
        raise ManifestError(f"入口文件不存在：{root / manifest.entry}")
    return manifest


def default_config(manifest: Manifest) -> dict[str, Any]:
    """config_schema 顶层属性中声明的默认值。"""
    if manifest.config_schema is None:
        return {}
    properties = manifest.config_schema.get("properties", {})
    return {
        name: prop["default"]
        for name, prop in properties.items()
        if isinstance(prop, dict) and "default" in prop
    }


def check_compatibility(manifest: Manifest, harness_version: str) -> str | None:
    """检查 api_version 与 harness_version；兼容时返回 None，否则返回原因。"""
    raise not_implemented("check_compatibility", "M2")
