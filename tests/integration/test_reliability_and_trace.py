"""M3：输出上限、失败展示、追踪落盘与大结果保存（DESIGN.md §5.4、§8.4、§9、§12）。"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_harness.agent.llm import Script, ScriptedClient
from agent_harness.app import Harness
from agent_harness.cli import main as cli_main
from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.plugins.runner import MAX_STDOUT_BYTES
from agent_harness.trace import EventType

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()

FIXTURE = """\
import os
import sys

from harness_sdk import Plugin

plugin = Plugin()


@plugin.tool("crash")
def crash(ctx):
    print("about to crash", file=sys.stderr)
    sys.stderr.flush()
    os._exit(3)


@plugin.tool("big")
def big(ctx):
    return "x" * 30000


if __name__ == "__main__":
    plugin.run()
"""

FLOOD = f"""\
import os
chunk = b"y" * 65536
for _ in range({MAX_STDOUT_BYTES // 65536 + 20}):
    os.write(1, chunk)
"""


@pytest.fixture
def harness(harness_home: Path, write_plugin: Callable[..., Path]) -> Harness:
    harness = Harness.open()
    harness.install_plugin(write_plugin("fx", FIXTURE, tools=["crash", "big"]))
    harness.enable_plugin("fx", approve=lambda manifest: True)
    return harness


def scripted(*steps: dict) -> ScriptedClient:
    return ScriptedClient(Script.model_validate({"steps": list(steps)}))


def test_stdout_over_limit_is_result_too_large(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    harness.install_plugin(write_plugin("flood", FLOOD))
    harness.enable_plugin("flood", approve=lambda manifest: True)

    result = harness.call_tool("flood__run", "{}")

    assert result.error is not None and result.error.code is ErrorCode.RESULT_TOO_LARGE


def test_run_persists_trace_and_saves_full_truncated_result(harness: Harness) -> None:
    llm = scripted(
        {"tool_calls": [{"name": "fx__big"}]},
        {"tool_calls": [{"name": "fx__crash"}]},
        {"content": "完成"},
    )

    result = harness.run("测试", llm)

    assert result.status == "completed"
    big, crash = result.tool_results
    assert big.truncated and big.full_result_path is not None
    saved = Path(big.full_result_path)
    assert saved.parent == harness.run_results_dir(result.run_id)
    assert json.loads(saved.read_text(encoding="utf-8")) == "x" * 30000
    assert crash.error is not None and crash.error.code is ErrorCode.PLUGIN_CRASHED
    assert "about to crash" in (crash.stderr_tail or "")

    events = harness.run_events(result.run_id)
    assert [e.type for e in events][0] is EventType.RUN_STARTED
    assert events[-1].payload["status"] == "completed"
    finished = [e.payload for e in events if e.type is EventType.TOOL_FINISHED]
    assert finished[0]["truncated"] is True
    assert finished[1]["error"]["code"] == "PLUGIN_CRASHED"
    assert harness.list_runs()[0].run_id == result.run_id


def test_cli_run_shows_stderr_and_runs_show_replays(harness: Harness, tmp_path: Path) -> None:
    script = tmp_path / "s.json"
    script.write_text(
        json.dumps({"steps": [{"tool_calls": [{"name": "fx__crash"}]}, {"content": "结束"}]}),
        encoding="utf-8",
    )

    out = runner.invoke(cli_main.app, ["run", "崩溃测试", "--script", str(script)]).output
    run_id = harness.list_runs()[0].run_id
    shown = runner.invoke(cli_main.app, ["runs", "show", run_id])
    listed = runner.invoke(cli_main.app, ["runs", "list"])

    assert "PLUGIN_CRASHED" in out and "about to crash" in out
    assert f"harness runs show {run_id}" in out.replace("\n", "")
    assert shown.exit_code == 0 and "崩溃测试" in shown.output and "about to crash" in shown.output
    assert run_id in listed.output


def test_runs_show_rejects_unknown_or_unsafe_ids(harness: Harness) -> None:
    for run_id in ("r_20990101_000000_abcd", "../state", "r_x"):
        with pytest.raises(HarnessError, match="找不到运行记录"):
            harness.run_events(run_id)


def test_delete_note_requires_confirmation(harness_home: Path) -> None:
    harness = Harness.open()
    harness.install_plugin(ROOT / "plugins" / "notes")
    harness.enable_plugin("notes", approve=lambda manifest: True)
    note_id = harness.call_tool("notes__add_note", '{"title": "t", "body": "b"}').content["id"]
    args = json.dumps({"id": note_id})

    denied = harness.call_tool("notes__delete_note", args)
    refused = harness.call_tool("notes__delete_note", args, confirm=lambda tool, a: False)
    deleted = harness.call_tool("notes__delete_note", args, confirm=lambda tool, a: True)
    again = harness.call_tool("notes__delete_note", args, confirm=lambda tool, a: True)

    assert denied.error is not None and denied.error.code is ErrorCode.USER_DENIED
    assert refused.error is not None and refused.error.code is ErrorCode.USER_DENIED
    assert deleted.ok and deleted.content == {"id": note_id, "deleted": True}
    assert again.error is not None and again.error.code is ErrorCode.TOOL_ERROR


def test_plugin_that_ignores_large_request_does_not_hang_host(
    harness: Harness, write_plugin: Callable[..., Path]
) -> None:
    """请求写在后台线程：插件不读 stdin 时宿主不会阻塞，按退出结果判定。"""
    harness.install_plugin(
        write_plugin(
            "deaf",
            'print(\'{"api_version": 1, "call_id": "x", "ok": true}\')\n',
            tools=[
                {
                    "name": "run",
                    "description": "忽略请求",
                    "input_schema": {"type": "object"},
                    "timeout_s": 5,
                }
            ],
        )
    )
    harness.enable_plugin("deaf", approve=lambda manifest: True)

    result = harness.call_tool("deaf__run", json.dumps({"blob": "z" * 1_000_000}))

    assert result.error is not None and result.error.code is ErrorCode.PROTOCOL_ERROR
    assert result.duration_ms < 5000
