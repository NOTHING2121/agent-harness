from pathlib import Path

import pytest

from agent_harness.errors import ConfigError
from agent_harness.settings import HarnessPaths, load_settings


def write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def test_defaults_without_config_file(harness_home: Path) -> None:
    paths = HarnessPaths.from_env()
    settings = load_settings(paths)

    assert paths.home == harness_home
    assert not harness_home.exists(), "加载配置不应创建任何目录"
    assert settings.llm.provider == "openai"
    assert settings.llm.model == "openai/gpt-6.1-sol"
    assert settings.llm.base_url == "https://openrouter.ai/api/v1"
    assert settings.llm.api_key_env == "OPENROUTER_API_KEY"
    assert settings.llm.reasoning_effort is None
    assert settings.agent.max_steps == 10
    assert settings.agent.tool_result_max_chars == 20_000
    assert settings.plugins.default_timeout_s == 30


def test_default_home_is_under_user_home() -> None:
    assert HarnessPaths.from_env({}).home == Path.home() / ".agent-harness"


def test_paths_layout(harness_home: Path) -> None:
    paths = HarnessPaths.from_env()

    assert paths.state_file == harness_home / "state.json"
    assert paths.lock_file == harness_home / ".lock"
    assert paths.plugin_data_dir("demo") == harness_home / "data" / "demo"


def test_env_overrides_config_file(harness_home: Path) -> None:
    write_config(
        harness_home,
        '[llm]\nmodel = "from-file"\nbase_url = "http://localhost:8000/v1"\n'
        "[agent]\nmax_steps = 5\n",
    )

    settings = load_settings(HarnessPaths.from_env(), env={"HARNESS_LLM_MODEL": "from-env"})

    assert settings.llm.model == "from-env"
    assert settings.llm.base_url == "http://localhost:8000/v1"
    assert settings.agent.max_steps == 5


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("[agent]\nmax_steps = 0\n", "agent.max_steps"),
        ('[llm]\nmodle = "typo"\n', "llm.modle"),
        ('[llm]\nprovider = "unknown"\n', "llm.provider"),
        ("[plugins]\ndefault_timeout_s = 200\n", "default_timeout_s 不能大于 max_timeout_s"),
    ],
)
def test_invalid_config_reports_field(harness_home: Path, config: str, expected: str) -> None:
    write_config(harness_home, config)

    with pytest.raises(ConfigError) as exc_info:
        load_settings(HarnessPaths.from_env())

    assert expected in exc_info.value.message
    assert exc_info.value.hint is not None


def test_malformed_toml_is_reported(harness_home: Path) -> None:
    write_config(harness_home, "llm = [\n")

    with pytest.raises(ConfigError, match="无法解析配置文件"):
        load_settings(HarnessPaths.from_env())
