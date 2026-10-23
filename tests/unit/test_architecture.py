"""检查 DESIGN.md §3.2 的依赖规则。"""

import ast
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
EXAMPLE_PLUGIN_IDS = ("notes", "workspace")


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
