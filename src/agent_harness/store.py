"""state.json 的模型与读写（DESIGN.md §11.2）。

写入方式：持文件锁 → 读取 → 修改 → 写临时文件并 fsync → os.replace 原子替换。
读取方不加锁，也总能读到完整文件。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.errors import not_implemented
from agent_harness.plugins.manifest import Permissions
from agent_harness.settings import HarnessPaths

STATE_SCHEMA_VERSION = 1


class PluginState(BaseModel):
    """单个插件的用户意图。版本号不在这里，以 plugins/<id> 指向的清单为准。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    approved: Permissions | None = None
    source: str | None = None
    dev: bool = False


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = STATE_SCHEMA_VERSION
    plugins: dict[str, PluginState] = Field(default_factory=dict)


class StateStore:
    def __init__(self, paths: HarnessPaths) -> None:
        self._paths = paths

    def load(self) -> State:
        """读取当前状态；文件不存在时返回空状态。"""
        raise not_implemented("StateStore.load", "M2")

    def transaction(self) -> AbstractContextManager[State]:
        """持锁读取状态，退出上下文时原子写回；所有管理操作都在其中执行。"""
        raise not_implemented("StateStore.transaction", "M2")
