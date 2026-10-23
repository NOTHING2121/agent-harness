"""检查 DESIGN.md §3.2 的依赖规则。"""

import ast
import dataclasses
import importlib
import re
import sys
import typing
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

SRC = Path(__file__).resolve().parents[2] / "src"
EXAMPLE_PLUGIN_IDS = ("notes", "workspace")
CLI_BEHAVIOR_MODULES = ("agent_harness.app", "agent_harness.cli")
"""CLI 只能从这些模块引用功能；从其他核心模块只能引用数据类型。"""


def python_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_host_core_does_not_reference_example_plugins() -> None:
    pattern = re.compile(r"\b(" + "|".join(EXAMPLE_PLUGIN_IDS) + r")\b")
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in python_files("agent_harness")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]

    assert offenders == []


def test_sdk_depends_only_on_stdlib() -> None:
    offenders = [
        f"{path.name}: {module}"
        for path in python_files("harness_sdk")
        for module in imported_modules(path)
        if module.split(".")[0] not in sys.stdlib_module_names | {"harness_sdk", "__future__"}
    ]

    assert offenders == []


def test_host_only_uses_sdk_protocol_module() -> None:
    offenders = [
        f"{path.name}: {module}"
        for path in python_files("agent_harness")
        for module in imported_modules(path)
        if module.split(".")[0] == "harness_sdk" and module != "harness_sdk.protocol"
    ]

    assert offenders == []


def is_data_type(obj: object) -> bool:
    """数据类型：Pydantic 模型、dataclass、枚举、异常、类型别名或字符串常量。"""
    if isinstance(obj, type):
        return issubclass(obj, BaseModel | Enum | BaseException) or dataclasses.is_dataclass(obj)
    return typing.get_origin(obj) is not None or isinstance(obj, str)


def test_cli_uses_core_behavior_only_through_app() -> None:
    offenders: list[str] = []
    for path in python_files("agent_harness/cli"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("agent_harness")
                ]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                module = node.module
                if not module.startswith("agent_harness") or module.startswith(
                    CLI_BEHAVIOR_MODULES
                ):
                    continue
                imported = importlib.import_module(module)
                offenders += [
                    f"{path.name}: {module}.{alias.name}"
                    for alias in node.names
                    if not is_data_type(getattr(imported, alias.name))
                ]

    assert offenders == []


def test_data_type_check_rejects_behavior() -> None:
    from agent_harness.plugins.executor import ConfirmCallback, ToolResult
    from agent_harness.plugins.manager import PluginManager
    from agent_harness.plugins.manifest import qualified_tool_name

    assert is_data_type(ToolResult)
    assert is_data_type(ConfirmCallback)
    assert not is_data_type(PluginManager)
    assert not is_data_type(qualified_tool_name)
