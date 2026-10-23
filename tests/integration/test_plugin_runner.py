"""PluginRunner 集成测试：每个用例都启动真实的插件进程。"""

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_harness.errors import ErrorCode
from agent_harness.plugins.registry import ActivePlugin
from agent_harness.plugins.runner import PluginRunner, RunOutcome, build_plugin_env
from agent_harness.settings import PluginSettings

WritePlugin = Callable[..., Path]
LoadPlugin = Callable[[Path], ActivePlugin]

ECHO = """
from harness_sdk import Context, Plugin, ToolError, ToolOutput

plugin = Plugin()

@plugin.tool("echo")
def echo(ctx: Context, text: str) -> ToolOutput:
    return ToolOutput(content={"text": text, "plugin_id": ctx.plugin_id}, display=f"echo: {text}")

@plugin.tool("context")
def context(ctx: Context) -> dict:
    import os
    return {"config": ctx.config, "data_dir": str(ctx.data_dir), "cwd": os.getcwd(), "call_id": ctx.call_id}

@plugin.tool("fail")
def fail(ctx: Context) -> None:
    raise ToolError("笔记 7 不存在")

@plugin.tool("boom")
def boom(ctx: Context) -> None:
    raise ValueError("bad value")

@plugin.tool("not_json")
def not_json(ctx: Context) -> object:
    return object()

if __name__ == "__main__":
    plugin.run()
"""


def call(plugin: ActivePlugin, tool: str, arguments: dict | None = None, **settings) -> RunOutcome:
    runner = PluginRunner(PluginSettings(**settings))
    spec = plugin.manifest.get_tool(tool)
    assert spec is not None
    return runner.call(plugin, spec, arguments or {}, call_id="c_test")


@pytest.fixture
def echo_plugin(write_plugin: WritePlugin, load_active_plugin: LoadPlugin) -> ActivePlugin:
    plugin_dir = write_plugin(
        "echo",
        ECHO,
        tools=["echo", "context", "fail", "boom", "not_json", "missing"],
        config_schema={
            "type": "object",
            "properties": {"greeting": {"type": "string", "default": "hi"}},
        },
    )
    return load_active_plugin(plugin_dir)


def test_successful_call_returns_content_and_display(echo_plugin: ActivePlugin) -> None:
    outcome = call(echo_plugin, "echo", {"text": "你好"})

    assert outcome.ok, outcome.error
    assert outcome.content == {"text": "你好", "plugin_id": "echo"}
    assert outcome.display == "echo: 你好"
    assert outcome.duration_ms > 0


def test_plugin_receives_config_and_runs_in_data_dir(echo_plugin: ActivePlugin) -> None:
    outcome = call(echo_plugin, "context")

    assert outcome.ok, outcome.error
    assert outcome.content["config"] == {"greeting": "hi"}
    assert outcome.content["call_id"] == "c_test"
    assert Path(outcome.content["data_dir"]) == echo_plugin.data_dir
    assert Path(outcome.content["cwd"]).resolve() == echo_plugin.data_dir.resolve()


@pytest.mark.parametrize(
    ("tool", "arguments", "code", "message"),
    [
        ("fail", {}, ErrorCode.TOOL_ERROR, "笔记 7 不存在"),
        ("boom", {}, ErrorCode.TOOL_ERROR, "插件内部异常：ValueError: bad value"),
        ("not_json", {}, ErrorCode.TOOL_ERROR, "无法序列化为 JSON"),
        ("echo", {"wrong": 1}, ErrorCode.INVALID_ARGUMENTS, "参数与处理函数不匹配"),
        ("missing", {}, ErrorCode.PROTOCOL_ERROR, "unknown_tool"),
    ],
)
def test_plugin_errors_are_mapped(
    echo_plugin: ActivePlugin, tool: str, arguments: dict, code: ErrorCode, message: str
) -> None:
    outcome = call(echo_plugin, tool, arguments)

    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.code == code
    assert message in outcome.error.message


def test_exception_traceback_goes_to_stderr(echo_plugin: ActivePlugin) -> None:
    outcome = call(echo_plugin, "boom")

    assert "Traceback" in (outcome.stderr_tail or "")


def test_prints_do_not_break_protocol(
    write_plugin: WritePlugin, load_active_plugin: LoadPlugin
) -> None:
    plugin = load_active_plugin(
        write_plugin(
            "noisy",
            """
            import subprocess
            print("printed while importing")
            from harness_sdk import Plugin
            plugin = Plugin()

            @plugin.tool("run")
            def run(ctx):
                print("printed by handler")
                subprocess.run(["echo", "printed by child process"], check=True)
                return "ok"

            if __name__ == "__main__":
                plugin.run()
            """,
        )
    )

    outcome = call(plugin, "run")

    assert outcome.ok, outcome.error
    assert outcome.content == "ok"
    assert "printed by handler" in (outcome.stderr_tail or "")
    assert "printed by child process" in (outcome.stderr_tail or "")


def test_crash_is_reported(write_plugin: WritePlugin, load_active_plugin: LoadPlugin) -> None:
    plugin = load_active_plugin(
        write_plugin(
            "crashy",
            """
            import os, sys
            from harness_sdk import Plugin
            plugin = Plugin()

            @plugin.tool("run")
            def run(ctx):
                print("about to crash", file=sys.stderr, flush=True)
                os._exit(3)

            if __name__ == "__main__":
                plugin.run()
            """,
        )
    )

    outcome = call(plugin, "run")

    assert outcome.error is not None
    assert outcome.error.code == ErrorCode.PLUGIN_CRASHED
    assert "退出码 3" in outcome.error.message
    assert "about to crash" in (outcome.stderr_tail or "")


def test_import_error_is_reported(
    write_plugin: WritePlugin, load_active_plugin: LoadPlugin
) -> None:
    plugin = load_active_plugin(write_plugin("broken", "import not_a_real_module\n"))

    outcome = call(plugin, "run")

    assert outcome.error is not None
    assert outcome.error.code == ErrorCode.PLUGIN_CRASHED
    assert "ModuleNotFoundError" in (outcome.stderr_tail or "")


def test_non_sdk_plugin_without_response_is_protocol_error(
    write_plugin: WritePlugin, load_active_plugin: LoadPlugin
) -> None:
    plugin = load_active_plugin(write_plugin("plain", 'print("hello")\n'))

    outcome = call(plugin, "run")

    assert outcome.error is not None
    assert outcome.error.code == ErrorCode.PROTOCOL_ERROR
    assert "hello" in outcome.error.message


def test_timeout_kills_plugin_and_its_children(
    write_plugin: WritePlugin, load_active_plugin: LoadPlugin
) -> None:
    plugin = load_active_plugin(
        write_plugin(
            "sleepy",
            """
            import subprocess, time
            from harness_sdk import Plugin
            plugin = Plugin()

            @plugin.tool("run")
            def run(ctx):
                child = subprocess.Popen(["sleep", "30"])
                (ctx.data_dir / "child.pid").write_text(str(child.pid))
                time.sleep(30)

            if __name__ == "__main__":
                plugin.run()
            """,
            tools=[
                {
                    "name": "run",
                    "description": "sleep",
                    "input_schema": {"type": "object"},
                    "timeout_s": 1,
                }
            ],
        )
    )

    started = time.monotonic()
    outcome = call(plugin, "run")

    assert time.monotonic() - started < 10
    assert outcome.error is not None
    assert outcome.error.code == ErrorCode.PLUGIN_TIMEOUT
    child_pid = int((plugin.data_dir / "child.pid").read_text())
    assert _wait_until_gone(child_pid), "插件派生的子进程应随进程组一起被终止"


def test_timeout_is_capped_by_host_limit(echo_plugin: ActivePlugin) -> None:
    runner = PluginRunner(PluginSettings(default_timeout_s=5, max_timeout_s=10))
    spec = echo_plugin.manifest.tools[0].model_copy(update={"timeout_s": 60})

    assert runner.timeout_for(spec) == 10
    assert runner.timeout_for(echo_plugin.manifest.tools[0]) == 5


def test_plugin_env_is_minimal(
    write_plugin: WritePlugin,
    load_active_plugin: LoadPlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-host-secret")
    monkeypatch.setenv("DECLARED_TOKEN", "allowed")
    plugin = load_active_plugin(
        write_plugin(
            "envy",
            """
            import os
            from harness_sdk import Plugin
            plugin = Plugin()

            @plugin.tool("run")
            def run(ctx):
                return dict(os.environ)

            if __name__ == "__main__":
                plugin.run()
            """,
            permissions={"env": ["DECLARED_TOKEN"]},
        )
    )

    outcome = call(plugin, "run")

    assert outcome.ok, outcome.error
    assert "OPENROUTER_API_KEY" not in outcome.content
    assert outcome.content["DECLARED_TOKEN"] == "allowed"


def test_build_plugin_env_only_passes_declared_variables(echo_plugin: ActivePlugin) -> None:
    env = build_plugin_env(echo_plugin.manifest, {"PATH": "/bin", "SECRET": "x"})

    assert env == {"PATH": "/bin", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _wait_until_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False
