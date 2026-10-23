"""state.json 的模型与读写（DESIGN.md §11.2）。

写入方式：持文件锁 → 读取 → 修改 → 写临时文件并 fsync → os.replace 原子替换。
读取方不加锁，也总能读到完整文件。
"""

from __future__ import annotations

import fcntl
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_harness.errors import ConfigError, format_validation_error
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
        """读取当前状态；文件不存在时返回空状态，文件损坏时报错并指出位置。"""
        path = self._paths.state_file
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return State()
        except OSError as exc:
            raise ConfigError(f"无法读取状态文件 {path}：{exc}") from exc
        try:
            return State.model_validate_json(text)
        except ValidationError as exc:
            raise ConfigError(
                f"状态文件 {path} 已损坏：\n{format_validation_error(exc)}",
                hint="修复该文件；无法修复时可以删除它，插件会回到未启用状态（插件数据不受影响）。",
            ) from exc

    @contextmanager
    def transaction(self) -> Iterator[State]:
        """持锁读取状态，正常退出上下文时原子写回；出现异常时不写回。

        所有管理操作都在其中执行。锁不可重入：同一进程内不要嵌套调用。
        """
        self._paths.home.mkdir(parents=True, exist_ok=True)
        with self._paths.lock_file.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = self.load()
                yield state
                self._write(state)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, state: State) -> None:
        path = self._paths.state_file
        tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
        try:
            with tmp.open("w", encoding="utf-8") as file:
                file.write(state.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
