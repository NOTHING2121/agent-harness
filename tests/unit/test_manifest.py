import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_harness.errors import ManifestError, format_validation_error
from agent_harness.plugins.manifest import (
    Manifest,
    Permissions,
    check_compatibility,
    config_problems,
    default_config,
    load_manifest,
    qualified_tool_name,
    split_tool_name,
)

ManifestFactory = Callable[..., dict[str, Any]]


def tool(name: str = "echo", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "description": "测试工具",
        "input_schema": {"type": "object"},
    }
    data.update(overrides)
    return data


def test_valid_manifest(make_manifest_data: ManifestFactory) -> None:
    manifest = Manifest.model_validate(make_manifest_data())

    assert manifest.id == "demo"
    assert manifest.permissions == Permissions()
    assert manifest.get_tool("echo") is not None
    assert manifest.get_tool("missing") is None


def test_full_manifest_fields(make_manifest_data: ManifestFactory) -> None:
    manifest = Manifest.model_validate(
        make_manifest_data(
            harness_version=">=0.1,<1.0",
            permissions={"filesystem": True, "env": ["SOME_API_KEY"]},
            config_schema={
                "type": "object",
                "properties": {"root": {"type": "string"}},
                "required": ["root"],
            },
            tools=[tool(requires_confirmation=True, timeout_s=60)],
        )
    )

    assert manifest.permissions.env == ["SOME_API_KEY"]
    assert manifest.tools[0].requires_confirmation is True
    assert manifest.tools[0].timeout_s == 60


@pytest.mark.parametrize(
    ("overrides", "location"),
    [
        ({"id": "Demo"}, "id"),
        ({"id": "has_underscore"}, "id"),
        ({"version": "1.0"}, "version"),
        ({"entry": "../main.py"}, "entry"),
        ({"entry": "/abs/main.py"}, "entry"),
        ({"harness_version": "not a version"}, "harness_version"),
        ({"tools": []}, "tools"),
        ({"tools": [tool(name="Echo")]}, "tools[0].name"),
        ({"tools": [tool(input_schema={"type": "string"})]}, "tools[0].input_schema"),
        (
            {"tools": [tool(input_schema={"type": "object", "properties": 5})]},
            "tools[0].input_schema",
        ),
        ({"tools": [tool(timeout_s=500)]}, "tools[0].timeout_s"),
        ({"permissions": {"env": ["BAD-NAME"]}}, "permissions.env[0]"),
        ({"config_schema": {"type": "array"}}, "config_schema"),
        ({"unknown_field": 1}, "unknown_field"),
    ],
)
def test_invalid_manifest_reports_location(
    make_manifest_data: ManifestFactory, overrides: dict[str, Any], location: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Manifest.model_validate(make_manifest_data(**overrides))

    assert f"{location}:" in format_validation_error(exc_info.value)


def test_duplicate_tool_names_rejected(make_manifest_data: ManifestFactory) -> None:
    with pytest.raises(ValidationError, match="工具名重复：echo"):
        Manifest.model_validate(make_manifest_data(tools=[tool(), tool()]))


def test_qualified_tool_name_round_trip() -> None:
    name = qualified_tool_name("my-plugin", "do_it")

    assert name == "my-plugin__do_it"
    assert split_tool_name(name) == ("my-plugin", "do_it")


@pytest.mark.parametrize("bad", ["no_separator", "__tool", "plugin__"])
def test_split_tool_name_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        split_tool_name(bad)


def test_longest_names_fit_openai_function_limit(make_manifest_data: ManifestFactory) -> None:
    manifest = Manifest.model_validate(make_manifest_data(id="a" * 30, tools=[tool(name="b" * 32)]))

    assert len(qualified_tool_name(manifest.id, manifest.tools[0].name)) == 64


def write_plugin_dir(path: Path, manifest: dict[str, Any] | str, *, entry: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (path / "plugin.json").write_text(text, encoding="utf-8")
    if entry:
        (path / "main.py").write_text("", encoding="utf-8")
    return path


def test_load_manifest(tmp_path: Path, make_manifest_data: ManifestFactory) -> None:
    plugin_dir = write_plugin_dir(tmp_path / "demo", make_manifest_data())

    assert load_manifest(plugin_dir).id == "demo"


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="找不到 plugin.json"):
        load_manifest(tmp_path)


def test_load_manifest_invalid_json(tmp_path: Path) -> None:
    plugin_dir = write_plugin_dir(tmp_path / "demo", '{"id": "demo",\n  oops}')

    with pytest.raises(ManifestError, match="不是合法的 JSON：第 2 行"):
        load_manifest(plugin_dir)


def test_load_manifest_reports_field_errors(
    tmp_path: Path, make_manifest_data: ManifestFactory
) -> None:
    plugin_dir = write_plugin_dir(tmp_path / "demo", make_manifest_data(id="Bad_Id"))

    with pytest.raises(ManifestError) as exc_info:
        load_manifest(plugin_dir)

    assert "校验失败" in exc_info.value.message
    assert "id:" in exc_info.value.message


def test_load_manifest_missing_entry(tmp_path: Path, make_manifest_data: ManifestFactory) -> None:
    plugin_dir = write_plugin_dir(tmp_path / "demo", make_manifest_data(), entry=False)

    with pytest.raises(ManifestError, match="入口文件不存在"):
        load_manifest(plugin_dir)


def test_load_manifest_rejects_entry_symlink_outside(
    tmp_path: Path, make_manifest_data: ManifestFactory
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    plugin_dir = write_plugin_dir(tmp_path / "demo", make_manifest_data(), entry=False)
    (plugin_dir / "main.py").symlink_to(outside)

    with pytest.raises(ManifestError, match="位于插件目录之外"):
        load_manifest(plugin_dir)


def test_default_config(make_manifest_data: ManifestFactory) -> None:
    manifest = Manifest.model_validate(
        make_manifest_data(
            config_schema={
                "type": "object",
                "properties": {
                    "max_notes": {"type": "integer", "default": 1000},
                    "root": {"type": "string"},
                },
            }
        )
    )

    assert default_config(manifest) == {"max_notes": 1000}
    assert default_config(Manifest.model_validate(make_manifest_data())) == {}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, None),
        ({"harness_version": ">=0.1,<1.0"}, None),
        ({"harness_version": ">=2.0"}, "需要 harness >=2.0，当前为 0.1.0"),
        ({"api_version": 2}, "api_version=2"),
    ],
)
def test_check_compatibility(
    make_manifest_data: ManifestFactory, overrides: dict[str, Any], expected: str | None
) -> None:
    manifest = Manifest.model_validate(make_manifest_data(**overrides))

    reason = check_compatibility(manifest, "0.1.0")

    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_config_problems_lists_each_field(make_manifest_data: ManifestFactory) -> None:
    schema = {
        "type": "object",
        "properties": {"root": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}},
        "required": ["root"],
    }
    manifest = Manifest.model_validate(make_manifest_data(config_schema=schema))

    problems = config_problems(manifest, {"limit": 0})

    assert "root: 缺少必填配置项" in problems
    assert any(p.startswith("limit:") for p in problems)
    assert config_problems(manifest, {"root": "/x"}) == []


def test_config_problems_without_schema_rejects_any_value(
    make_manifest_data: ManifestFactory,
) -> None:
    manifest = Manifest.model_validate(make_manifest_data())

    assert config_problems(manifest, {}) == []
    assert config_problems(manifest, {"x": 1}) == ["x: 插件没有声明这个配置项"]
