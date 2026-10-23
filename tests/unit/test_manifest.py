from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from agent_harness.errors import format_validation_error
from agent_harness.plugins.manifest import (
    Manifest,
    Permissions,
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
