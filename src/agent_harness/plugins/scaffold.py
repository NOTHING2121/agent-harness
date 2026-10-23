"""harness plugin new：从模板生成插件骨架（DESIGN.md §6.3）。

模板随 SDK 发布（harness_sdk/templates），这里只读取模板文件，不 import SDK 的实现模块。
"""

from __future__ import annotations

import re
from pathlib import Path

import harness_sdk.protocol
from agent_harness.errors import HarnessError
from agent_harness.plugins.manifest import PLUGIN_ID_PATTERN

TEMPLATE_DIR = Path(harness_sdk.protocol.__file__).resolve().parent / "templates"
TEMPLATE_SUFFIX = ".tmpl"


def create_plugin(plugin_id: str, parent: Path) -> Path:
    """在 parent/<plugin_id> 生成 plugin.json、main.py 和 test_main.py，返回插件目录。"""
    if not re.fullmatch(PLUGIN_ID_PATTERN, plugin_id):
        raise HarnessError(
            f"插件 ID {plugin_id!r} 不合法。",
            hint="只能包含小写字母、数字和连字符，以字母开头，最长 30 个字符，例如 my-tool。",
        )
    target = parent.expanduser().absolute() / plugin_id
    if target.exists():
        raise HarnessError(
            f"{target} 已存在，没有覆盖。", hint="换一个 ID，或用 --dir 指定其他目录。"
        )
    values = {
        "plugin_id": plugin_id,
        "plugin_name": plugin_id.replace("-", " ").title(),
        "module_name": f"{plugin_id.replace('-', '_')}_main",
    }
    templates = sorted(TEMPLATE_DIR.glob(f"*{TEMPLATE_SUFFIX}"))
    if not templates:
        raise HarnessError(f"找不到插件模板：{TEMPLATE_DIR}", hint="重新安装 agent-harness。")
    target.mkdir(parents=True)
    for template in templates:
        text = template.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace(f"{{{{{key}}}}}", value)
        (target / template.name.removesuffix(TEMPLATE_SUFFIX)).write_text(text, encoding="utf-8")
    return target
