"""插件管理的 CLI 全流程：install → list / info → config → enable → 使用 → disable → uninstall。"""

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from agent_harness.cli import main as cli_main
from agent_harness.errors import HarnessError

runner = CliRunner()
CODE = """\
from harness_sdk import Plugin

plugin = Plugin()


@plugin.tool("whoami")
def whoami(ctx):
    return {"user": ctx.config["user"], "greeting": ctx.config["greeting"]}


if __name__ == "__main__":
    plugin.run()
"""
CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "user": {"type": "string", "description": "称呼"},
        "greeting": {"type": "string", "default": "你好"},
    },
    "required": ["user"],
    "additionalProperties": False,
}


def harness(*args: str, stdin: str | None = None) -> tuple[int, str]:
    """调用 CLI；HarnessError 在真实入口 main() 中渲染到 stderr，这里把消息和建议拼进输出。"""
    result = runner.invoke(cli_main.app, list(args), input=stdin)
    output = result.output
    if isinstance(result.exception, HarnessError):
        output += f"\n{result.exception.message}\n{result.exception.hint or ''}"
    return result.exit_code, output


def flat(text: str) -> str:
    """去掉空白和表格边框，避免 Rich 表格在窄终端中折行影响断言。"""
    return re.sub(r"[\s│┃]", "", text)


def make_plugin(write_plugin, **overrides) -> Path:  # type: ignore[no-untyped-def]
    return write_plugin(
        "hello",
        CODE,
        tools=[
            {
                "name": "whoami",
                "description": "返回配置中的称呼",
                "input_schema": {"type": "object"},
            }
        ],
        config_schema=CONFIG_SCHEMA,
        permissions={"env": ["HELLO_TOKEN"]},
        **overrides,
    )


def test_full_lifecycle(harness_home: Path, write_plugin) -> None:  # type: ignore[no-untyped-def]
    source = make_plugin(write_plugin)

    code, out = harness("plugin", "validate", str(source))
    assert code == 0 and "清单有效" in out

    code, out = harness("plugin", "install", str(source))
    assert code == 0, out
    assert "已复制到" in out and "harness plugin config hello --set" in out

    code, out = harness("plugin", "list")
    assert "disabled" in out and "user:缺少必填配置项" in flat(out)

    code, out = harness("plugin", "enable", "hello", "--yes")
    assert code == 1 and "配置不完整" in out

    code, out = harness("plugin", "config", "hello", "--set", "user=小明", "--set", "typo=1")
    assert code == 1 and "没有保存" in out
    code, out = harness("plugin", "config", "hello", "--set", "user=小明")
    assert code == 0 and '"greeting": "你好"' in out

    code, out = harness("tool", "call", "hello__whoami")
    assert code == 1 and "UNKNOWN_TOOL" in out
    assert "当前状态为disabled" in flat(out) and "harnesspluginenablehello" in flat(out)

    code, out = harness("plugin", "enable", "hello", stdin="n\n")
    assert code == 1 and "没有确认授权" in out
    assert "HELLO_TOKEN" in out and "宿主强制执行" in out

    code, out = harness("plugin", "enable", "hello", "--yes")
    assert code == 0 and "已启用" in out

    code, out = harness("tool", "call", "hello__whoami")
    assert code == 0, out
    assert '"user": "小明"' in out

    code, out = harness("plugin", "info", "hello")
    assert "active" in out and "与声明一致" in out and "user（string，必填）" in out

    code, out = harness("plugin", "disable", "hello")
    assert code == 0 and "授权已撤销" in out
    code, out = harness("tool", "list")
    assert "hello__whoami" not in out

    assert (harness_home / "data" / "hello").is_dir(), "调用过的插件已有数据目录"
    code, out = harness("plugin", "uninstall", "hello")
    assert code == 0 and "数据目录已保留" in out
    code, out = harness("plugin", "list")
    assert "还没有安装插件" in out
    assert source.is_dir(), "卸载复制安装的插件不影响源目录"


def test_state_file_records_only_user_intent(harness_home: Path, write_plugin) -> None:  # type: ignore[no-untyped-def]
    harness("plugin", "install", str(make_plugin(write_plugin)))
    harness("plugin", "config", "hello", "--set", "user=A")
    harness("plugin", "enable", "hello", "--yes")

    state = json.loads((harness_home / "state.json").read_text(encoding="utf-8"))

    record = state["plugins"]["hello"]
    assert record["enabled"] is True
    assert record["config"] == {"user": "A"}, "只保存用户设置的值，默认值来自清单"
    assert record["approved"]["env"] == ["HELLO_TOKEN"]
    assert "version" not in record


def test_errors_are_friendly(harness_home: Path, tmp_path: Path, write_plugin) -> None:  # type: ignore[no-untyped-def]
    code, out = harness("plugin", "info", "ghost")
    assert code == 1 and "未安装" in out

    code, out = harness("plugin", "validate", str(tmp_path))
    assert code == 1 and "找不到 plugin.json" in out

    code, out = harness("plugin", "validate", str(make_plugin(write_plugin, api_version=2)))
    assert code == 1 and "不兼容" in out

    code, out = harness("plugin", "config", "ghost", "--set", "novalue")
    assert code != 0 and "key=value" in out
