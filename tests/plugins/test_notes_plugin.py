"""notes 示例插件的单元测试：通过 plugin.invoke 在进程内调用，不启动宿主。"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from harness_sdk import ToolError, ToolOutput

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "notes"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("notes_plugin_main", PLUGIN_DIR / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notes = load_module()


def call(tmp_path: Path, tool: str, config: dict | None = None, **arguments) -> ToolOutput:
    result = notes.plugin.invoke(
        tool, arguments, data_dir=tmp_path, config=config or {"max_notes": 1000}
    )
    assert isinstance(result, ToolOutput)
    return result


def test_add_then_list(tmp_path: Path) -> None:
    added = call(tmp_path, "add_note", title=" 周五发布 ", body="完成 README")
    listed = call(tmp_path, "list_notes")

    assert added.content == {"id": 1, "title": "周五发布"}
    assert listed.content["total"] == 1
    assert listed.content["next_offset"] is None
    assert listed.content["notes"][0]["body"] == "完成 README"
    assert "周五发布" in (listed.display or "")


def test_list_is_newest_first_and_paginated(tmp_path: Path) -> None:
    for index in range(5):
        call(tmp_path, "add_note", title=f"n{index}", body="")

    page = call(tmp_path, "list_notes", limit=2, offset=1).content

    assert [n["title"] for n in page["notes"]] == ["n3", "n2"]
    assert page["total"] == 5
    assert page["next_offset"] == 3


def test_search_matches_title_and_body(tmp_path: Path) -> None:
    call(tmp_path, "add_note", title="Release plan", body="周五")
    call(tmp_path, "add_note", title="购物", body="买牛奶，release 前别忘了")
    call(tmp_path, "add_note", title="其他", body="无关")

    result = call(tmp_path, "search_notes", query="RELEASE").content

    assert result["total"] == 2
    assert {n["title"] for n in result["notes"]} == {"Release plan", "购物"}


def test_search_treats_wildcards_literally(tmp_path: Path) -> None:
    call(tmp_path, "add_note", title="完成 100%", body="")
    call(tmp_path, "add_note", title="完成 1000", body="")

    result = call(tmp_path, "search_notes", query="100%").content

    assert [n["title"] for n in result["notes"]] == ["完成 100%"]


def test_search_without_match(tmp_path: Path) -> None:
    result = call(tmp_path, "search_notes", query="不存在")

    assert result.content["total"] == 0
    assert "没有找到" in (result.display or "")


def test_max_notes_limit(tmp_path: Path) -> None:
    call(tmp_path, "add_note", config={"max_notes": 1}, title="a", body="")

    with pytest.raises(ToolError, match="上限 1"):
        call(tmp_path, "add_note", config={"max_notes": 1}, title="b", body="")


def test_blank_title_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="标题不能为空"):
        call(tmp_path, "add_note", title="   ", body="x")


def test_manifest_tools_match_registered_handlers() -> None:
    import json

    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    assert sorted(tool["name"] for tool in manifest["tools"]) == notes.plugin.tool_names
