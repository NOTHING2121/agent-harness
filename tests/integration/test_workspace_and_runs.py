"""跨插件场景、运行追踪与大结果截断（经由真实插件进程）。"""

import json
from pathlib import Path

from typer.testing import CliRunner

from agent_harness.app import Harness
from agent_harness.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def cli(*args: str) -> str:
    result = runner.invoke(cli_main.app, list(args))
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output


def test_cross_plugin_demo_and_runs(harness_home: Path) -> None:
    cli("plugin", "install", str(ROOT / "plugins" / "notes"))
    cli("plugin", "install", str(ROOT / "plugins" / "workspace"))
    cli("plugin", "config", "workspace", "--set", f"root={ROOT / 'demos' / 'sample-project'}")
    cli("plugin", "enable", "notes", "--yes")
    cli("plugin", "enable", "workspace", "--yes")

    out = cli("run", "读 README", "--script", str(ROOT / "demos" / "readme_to_note.json"))

    assert "workspace__read_file" in out and "notes__add_note" in out
    runs = Harness.open().list_runs()
    assert len(runs) == 1 and runs[0].status == "completed"
    assert "Aurora" in cli("runs", "show", runs[0].run_id)


def test_large_result_is_truncated_for_llm(harness_home: Path, write_plugin) -> None:  # type: ignore[no-untyped-def]
    code = (
        "from harness_sdk import Plugin\nplugin = Plugin()\n"
        "@plugin.tool('big')\ndef big(ctx):\n    return 'x' * 50000\n"
        "if __name__ == '__main__':\n    plugin.run()\n"
    )
    harness = Harness.open()
    harness.install_plugin(write_plugin("big", code, tools=["big"]))
    harness.enable_plugin("big", approve=lambda manifest: True)

    result = harness.call_tool("big__big", "{}")

    payload = json.loads(result.for_llm())
    assert payload["truncated"] is True and payload["original_chars"] > 50000
    assert len(payload["preview"]) == harness.settings.agent.tool_result_max_chars
