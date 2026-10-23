# Local Agent Harness

本地运行、支持插件扩展的 AI Agent Harness。用户安装插件后，Agent 通过注册表动态发现插件声明的工具并调用；插件开发者按照一套清晰的协议（`plugin.json` + `harness_sdk`）为宿主增加新能力，不需要修改宿主代码。

> 当前状态：**M1 端到端最小闭环已完成**。可以用开发模式安装插件、列出和直接调用工具，并让 Agent（离线脚本或真实 LLM）调用插件。插件启用/禁用/配置等管理功能在 M2 加入，进度见 [PROGRESS.md](PROGRESS.md)。

## 环境要求

- macOS 或 Linux
- [uv](https://docs.astral.sh/uv/)：项目通过 `.python-version` 固定 Python 3.12，由 uv 自动下载和管理，不依赖系统自带的 Python

## 快速开始（离线，不需要 API Key）

依次执行：安装依赖、以开发模式安装 notes 插件、查看它提供的工具、离线运行一次 Agent。

```bash
uv sync
uv run harness plugin install plugins/notes --dev
uv run harness tool list
uv run harness run "记一下周五发布的事" --script demos/notes_demo.json
```

本文中的代码块都不含行内注释，可以直接复制到 bash 或 zsh 中执行（macOS 默认的 zsh 在交互模式下不把 `#` 当作注释）。如果不想每次都输入 `uv run`，可以先执行 `source .venv/bin/activate`，之后直接使用 `harness`。

`--script` 会按顺序回放预设的模型回复，用来代替真实 LLM；工具调用仍然会启动真实的插件进程。Harness 的数据默认保存在 `~/.agent-harness`，可以用环境变量 `HARNESS_HOME` 指定其他目录，例如 `export HARNESS_HOME=$PWD/.harness-home`。

## 使用真实 LLM（OpenRouter）

先复制配置模板，然后在 `.env` 中填写 `OPENROUTER_API_KEY`，再运行：

```bash
cp .env.example .env
uv run harness run "帮我记一条笔记：周五发布 v0.1，然后搜索“发布”确认能找到"
```

默认模型是 `anthropic/claude-opus-4.8`，可以用 `--model` 临时替换，或在 `.env` 中设置 `HARNESS_LLM_MODEL`。模型需要支持工具调用（tool calling）。OpenRouter 的 `:batch` 变体（如 `anthropic/claude-opus-4.8:batch`）只支持异步批处理接口，不能用于 Agent。如果 OpenRouter 返回“被 guardrail 屏蔽”，说明账户的 guardrail 设置不允许该模型，需要在 OpenRouter 控制台放行，或换一个账户允许的模型。harness 只读取**当前目录**下的 `.env`，已经存在的同名环境变量优先。API Key 不会传给插件进程。

## 直接调用工具（调试插件）

```bash
uv run harness tool call notes__add_note --args '{"title": "买牛奶", "body": "下班路上"}'
uv run harness tool call notes__search_notes --args '{"query": "牛奶"}' -v
```

`tool call` 绕过 LLM，但走与 Agent 完全相同的执行链路（参数校验、确认、插件进程、错误处理）。子命令的 `-v` 会额外显示请求和插件的 stderr；全局的 `harness -v ...` 会输出宿主的调试日志。需要确认的工具会在终端询问；非交互环境中可以加 `--yes`。

## 写一个插件（最小示例）

插件是一个目录，包含 `plugin.json`（清单）和入口文件：

```python
# main.py
from harness_sdk import Context, Plugin

plugin = Plugin()


@plugin.tool("greet")
def greet(ctx: Context, name: str) -> str:
    return f"Hello, {name}"


if __name__ == "__main__":
    plugin.run()
```

清单中声明 `id`、`version`、`entry` 和每个工具的 `input_schema`，完整字段见 [DESIGN.md §4](DESIGN.md)，可以参考 `plugins/notes/`。用 `harness plugin install <目录> --dev` 安装后，修改代码立即生效。插件里的 `print()` 不会破坏协议；单元测试可以用 `plugin.invoke(...)` 在进程内直接调用处理函数。

## 测试与代码检查

依次是：全部测试（不需要网络和 API Key）、代码检查、格式检查。

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## 目录结构

```
src/agent_harness/   宿主：CLI、应用服务、插件管理、Agent Loop
src/harness_sdk/     插件 SDK：只依赖标准库，插件通过它与宿主通信
plugins/notes/       示例插件：笔记（SQLite）
demos/               离线演示场景
tests/               单元测试、插件测试与集成测试
```

设计说明见 [DESIGN.md](DESIGN.md)，开发规范见 [Agent.md](Agent.md)。
