"""workspace 示例插件的单元测试：通过 plugin.invoke 在进程内调用，不启动宿主。"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from harness_sdk import ToolError, ToolOutput

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "workspace"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workspace_plugin_main", PLUGIN_DIR / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = load_module()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# Demo\n\n发布计划：周五\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("第一行\nRelease notes\n", encoding="utf-8")
    (root / ".secret").write_text("hidden release", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\0\0binary release")
    (tmp_path / "outside.txt").write_text("outside release", encoding="utf-8")
    return root


def call(root: Path, tool: str, config: dict | None = None, **arguments) -> ToolOutput:
    result = workspace.plugin.invoke(
        tool,
        arguments,
        data_dir=root.parent / "data",
        config={"root": str(root), "max_file_bytes": 1024 * 1024, **(config or {})},
    )
    assert isinstance(result, ToolOutput)
    return result


def test_list_files_skips_hidden_entries(root: Path) -> None:
    listed = call(root, "list_files")

    assert [e["path"] for e in listed.content["entries"]] == ["README.md", "docs", "logo.png"]
    assert listed.content["next_offset"] is None


def test_list_files_recursive_with_pagination(root: Path) -> None:
    first = call(root, "list_files", recursive=True, limit=2)
    rest = call(root, "list_files", recursive=True, limit=2, offset=2)

    assert first.content["total"] == 3
    assert first.content["next_offset"] == 2
    assert [e["path"] for e in rest.content["entries"]] == ["docs/guide.md"]


def test_read_file_by_line_range(root: Path) -> None:
    head = call(root, "read_file", path="README.md", max_lines=2)
    tail = call(root, "read_file", path="README.md", start_line=head.content["next_start_line"])

    assert head.content["text"] == "# Demo\n"
    assert head.content["total_lines"] == 3
    assert tail.content["text"] == "发布计划：周五"
    assert tail.content["next_start_line"] is None


def test_read_file_rejects_out_of_range_start(root: Path) -> None:
    with pytest.raises(ToolError, match="超出范围"):
        call(root, "read_file", path="README.md", start_line=10)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../outside.txt", "之外"),
        ("/etc/hosts", "之外"),
        ("missing.md", "不存在"),
        ("docs", "不是文件"),
        ("logo.png", "二进制"),
    ],
)
def test_read_file_rejections(root: Path, path: str, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        call(root, "read_file", path=path)


def test_symlink_pointing_outside_root_is_rejected(root: Path) -> None:
    (root / "escape.txt").symlink_to(root.parent / "outside.txt")

    with pytest.raises(ToolError, match="之外"):
        call(root, "read_file", path="escape.txt")
    listed = call(root, "list_files", recursive=True)
    assert "escape.txt" not in [e["path"] for e in listed.content["entries"]]


def test_large_files_are_not_read(root: Path) -> None:
    (root / "big.txt").write_text("x" * 5000, encoding="utf-8")

    with pytest.raises(ToolError, match="超过上限"):
        call(root, "read_file", {"max_file_bytes": 1024}, path="big.txt")


def test_search_text_is_case_insensitive_and_skips_binary_and_hidden(root: Path) -> None:
    found = call(root, "search_text", query="RELEASE")

    assert [(m["path"], m["line"]) for m in found.content["matches"]] == [("docs/guide.md", 2)]
    assert found.content["skipped_files"] == 1


def test_search_text_pagination(root: Path) -> None:
    (root / "many.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    first = call(root, "search_text", query="hit", limit=2)
    rest = call(root, "search_text", query="hit", limit=2, offset=2)

    assert (first.content["total"], first.content["next_offset"]) == (3, 2)
    assert len(rest.content["matches"]) == 1


def test_missing_or_invalid_root(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="没有配置 root"):
        workspace.plugin.invoke("list_files", {}, data_dir=tmp_path, config={})
    with pytest.raises(ToolError, match="不存在"):
        workspace.plugin.invoke(
            "list_files", {}, data_dir=tmp_path, config={"root": str(tmp_path / "nope")}
        )
