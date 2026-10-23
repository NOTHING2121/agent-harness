"""端到端：安装真实的 notes 插件，经由真实插件进程完成工具调用和 Agent 运行。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_harness.app import Harness
from agent_harness.cli import main as cli_main
from agent_harness.errors import ConfigError, ErrorCode, HarnessError
from agent_harness.trace import Event, EventType

ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = ROOT / "plugins" / "notes"
DEMO_SCRIPT = ROOT / "demos" / "notes_demo.json"

runner = CliRunner()


@pytest.fixture
def harness(harness_home: Path) -> Harness:
    harness = Harness.open()
    harness.install_plugin(NOTES_DIR, dev=True)
    return harness


def test_tools_are_discovered_from_manifest(harness: Harness) -> None:
    names = [tool.qualified_name for tool in harness.tools().tools]

    assert names == ["notes__add_note", "notes__list_notes", "notes__search_notes"]


def test_call_tool_through_plugin_process(harness: Harness) -> None:
    added = harness.call_tool("notes__add_note", '{"title": "会议", "body": "周五发布"}')
    listed = harness.call_tool("notes__list_notes", "{}")

    assert added.ok, added.error
    assert added.content["title"] == "会议"
    assert listed.ok, listed.error
    assert listed.content["total"] == 1
    assert (harness.paths.plugin_data_dir("notes") / "notes.db").is_file()


def test_invalid_arguments_never_reach_plugin(harness: Harness) -> None:
    result = harness.call_tool("notes__add_note", '{"title": ""}')

    assert result.error is not None
    assert result.error.code == ErrorCode.INVALID_ARGUMENTS


def test_scripted_agent_run_calls_real_plugin(harness: Harness) -> None:
    events: list[Event] = []
    llm = harness.create_llm("scripted", script=DEMO_SCRIPT)

    result = harness.run("记一下周五发布的事", llm, subscribers=[events.append])

    assert result.status == "completed", result.error
    assert [r.tool for r in result.tool_results] == ["notes__add_note", "notes__search_notes"]
    assert all(r.ok for r in result.tool_results)
    assert result.tool_results[1].content["total"] == 1
    finished = [e for e in events if e.type == EventType.TOOL_FINISHED]
    assert finished[0].payload["display"].startswith("已保存笔记")


def test_notes_persist_across_harness_instances(harness: Harness) -> None:
    harness.call_tool("notes__add_note", '{"title": "持久化", "body": "重启后仍在"}')

    reopened = Harness.open()
    result = reopened.call_tool("notes__search_notes", '{"query": "持久化"}')

    assert result.ok, result.error
    assert result.content["total"] == 1


def test_concurrent_writes_are_not_lost(harness: Harness) -> None:
    def add(index: int) -> bool:
        arguments = json.dumps({"title": f"并发 {index}", "body": ""})
        return harness.call_tool("notes__add_note", arguments).ok

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(add, range(16)))

    listed = harness.call_tool("notes__list_notes", '{"limit": 50}')
    assert listed.content["total"] == 16
    assert len({note["id"] for note in listed.content["notes"]}) == 16


def test_create_llm_requires_script_for_scripted(harness_home: Path) -> None:
    with pytest.raises(HarnessError, match="场景文件"):
        Harness.open().create_llm("scripted")


def test_create_llm_without_api_key_points_to_env_file(harness_home: Path) -> None:
    harness = Harness.open(env={"HARNESS_HOME": str(harness_home)})

    with pytest.raises(ConfigError) as exc_info:
        harness.create_llm("openai")

    assert harness.settings.llm.api_key_env in exc_info.value.message
    assert ".env" in (exc_info.value.hint or "")


def test_create_llm_rejects_unknown_provider(harness_home: Path) -> None:
    with pytest.raises(HarnessError, match="未知的 LLM 提供方"):
        Harness.open().create_llm("nope")


def test_cli_end_to_end(harness_home: Path) -> None:
    install = runner.invoke(cli_main.app, ["plugin", "install", str(NOTES_DIR), "--dev"])
    assert install.exit_code == 0, install.output
    assert "notes__add_note" in install.output

    listed = runner.invoke(cli_main.app, ["tool", "list"])
    assert listed.exit_code == 0, listed.output
    assert "notes__search_notes" in listed.output

    called = runner.invoke(
        cli_main.app,
        ["tool", "call", "notes__add_note", "--args", '{"title": "CLI", "body": "来自命令行"}'],
    )
    assert called.exit_code == 0, called.output
    assert "已保存笔记" in called.output

    run = runner.invoke(cli_main.app, ["run", "记一下周五发布的事", "--script", str(DEMO_SCRIPT)])
    assert run.exit_code == 0, run.output
    assert "notes__add_note" in run.output
    assert "回答" in run.output


def test_cli_tool_call_failure_exits_nonzero(harness_home: Path) -> None:
    runner.invoke(cli_main.app, ["plugin", "install", str(NOTES_DIR), "--dev"])

    result = runner.invoke(cli_main.app, ["tool", "call", "notes__missing"])

    assert result.exit_code == 1
    assert "UNKNOWN_TOOL" in result.output
