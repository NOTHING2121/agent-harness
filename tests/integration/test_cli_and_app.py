"""CLI 冒烟测试与组合根的端到端连接测试（fake LM）。"""

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_harness import __version__
from agent_harness.agent.llm import Script, ScriptedClient
from agent_harness.app import Harness
from agent_harness.cli import main as cli_main
from agent_harness.errors import ConfigError, HarnessError
from agent_harness.trace import Event, EventType

runner = CliRunner()


def test_version_option() -> None:
    result = runner.invoke(cli_main.app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"harness {__version__}"


def test_help_option() -> None:
    result = runner.invoke(cli_main.app, ["--help"])

    assert result.exit_code == 0
    assert "Agent Harness" in result.output


def test_console_script_is_installed() -> None:
    executable = Path(sys.executable).parent / "harness"

    completed = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"harness {__version__}"


def test_main_renders_harness_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_app() -> None:
        raise HarnessError("插件 demo 不存在", hint="先运行 harness plugin list")

    monkeypatch.setattr(cli_main, "app", failing_app)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main()

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "插件 demo 不存在" in stderr
    assert "先运行 harness plugin list" in stderr


def test_harness_open_reports_invalid_config(harness_home: Path) -> None:
    harness_home.mkdir(parents=True)
    (harness_home / "config.toml").write_text("[agent]\nmax_steps = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="agent.max_steps"):
        Harness.open()


def test_harness_run_with_fake_llm(harness_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness_home.mkdir(parents=True)
    (harness_home / "config.toml").write_text("[agent]\nmax_steps = 3\n", encoding="utf-8")
    harness = Harness.open()
    # 插件加载（PluginManager.active_plugins）在 M1 实现；这里只验证组合根的连接。
    monkeypatch.setattr(harness.manager, "active_plugins", lambda: [])
    llm = ScriptedClient(Script.model_validate({"steps": [{"content": "你好！"}]}))
    events: list[Event] = []

    result = harness.run("你好", llm, subscribers=[events.append])

    assert harness.settings.agent.max_steps == 3
    assert result.status == "completed"
    assert result.final_answer == "你好！"
    assert events[0].type == EventType.RUN_STARTED
    assert events[0].payload["model"] == "scripted"
    assert events[-1].type == EventType.RUN_FINISHED
