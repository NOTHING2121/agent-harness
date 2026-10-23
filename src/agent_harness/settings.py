"""路径、宿主配置文件与环境变量的合并（DESIGN.md §11）。

优先级：命令行参数 > 环境变量 > config.toml > 默认值。命令行参数由 CLI 在此基础上覆盖。
API Key 不在这里读取，只记录存放它的环境变量名。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_harness.errors import ConfigError, format_validation_error

HOME_ENV = "HARNESS_HOME"
DEFAULT_HOME = "~/.agent-harness"

ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "HARNESS_LLM_PROVIDER": ("llm", "provider"),
    "HARNESS_LLM_MODEL": ("llm", "model"),
    "HARNESS_LLM_BASE_URL": ("llm", "base_url"),
}
"""环境变量 -> (配置段, 字段)。"""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMSettings(_Section):
    """默认通过 OpenRouter 的 OpenAI 兼容接口调用 openai/gpt-6.1-sol。"""

    provider: Literal["openai", "scripted"] = "openai"
    model: str = Field(default="openai/gpt-6.1-sol", min_length=1)
    base_url: str = Field(default="https://openrouter.ai/api/v1", min_length=1)
    api_key_env: str = Field(default="OPENROUTER_API_KEY", min_length=1)
    reasoning_effort: str | None = None
    request_timeout_s: float = Field(default=120, gt=0)


class AgentSettings(_Section):
    max_steps: int = Field(default=10, ge=1)
    tool_result_max_chars: int = Field(default=20_000, ge=1_000)


class PluginSettings(_Section):
    default_timeout_s: float = Field(default=30, gt=0)
    max_timeout_s: float = Field(default=120, gt=0)

    @model_validator(mode="after")
    def _check_timeouts(self) -> PluginSettings:
        if self.default_timeout_s > self.max_timeout_s:
            raise ValueError("default_timeout_s 不能大于 max_timeout_s")
        return self


class Settings(_Section):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)


@dataclass(frozen=True)
class HarnessPaths:
    """$HARNESS_HOME 下的目录布局（DESIGN.md §11.1）。只计算路径，不创建目录。"""

    home: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HarnessPaths:
        env = os.environ if env is None else env
        raw = env.get(HOME_ENV) or DEFAULT_HOME
        return cls(home=Path(raw).expanduser().absolute())

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def state_file(self) -> Path:
        return self.home / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.home / ".lock"

    @property
    def plugins_dir(self) -> Path:
        return self.home / "plugins"

    @property
    def packages_dir(self) -> Path:
        return self.home / "packages"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    def plugin_data_dir(self, plugin_id: str) -> Path:
        return self.data_dir / plugin_id


def load_settings(paths: HarnessPaths, env: Mapping[str, str] | None = None) -> Settings:
    """读取 config.toml 并叠加环境变量覆盖项；不合法时抛出 ConfigError 并指出字段。"""
    env = os.environ if env is None else env
    data = _read_config_file(paths.config_file)
    for name, (section, key) in ENV_OVERRIDES.items():
        value = env.get(name)
        if not value:
            continue
        section_data = data.setdefault(section, {})
        if not isinstance(section_data, dict):
            raise ConfigError(f"配置文件 {paths.config_file} 中的 [{section}] 必须是一个表")
        section_data[key] = value
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"配置不合法：\n{format_validation_error(exc)}",
            hint=f"检查 {paths.config_file} 和 HARNESS_LLM_* 环境变量。",
        ) from exc


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"无法解析配置文件 {path}：{exc}") from exc
