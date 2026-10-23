# Local Agent Harness

本地运行、支持插件扩展的 AI Agent Harness（CLI：`harness`）。Agent 通过注册表动态发现已启用插件声明的工具并调用；插件开发者按 `plugin.json` + `harness_sdk` 协议扩展能力，无需修改宿主代码。

## 环境要求

- macOS 或 Linux
- [uv](https://docs.astral.sh/uv/)（自动下载并使用 Python 3.12）

## 安装

```bash
uv sync
source .venv/bin/activate
export HARNESS_HOME=$PWD/.harness-home
```

`HARNESS_HOME` 是 Harness 的数据目录（默认 `~/.agent-harness`），里面保存插件副本、状态、插件数据和运行追踪。

## 快速验收（离线，不需要 API Key）

```bash
harness plugin discover plugins
harness plugin install plugins/notes
harness plugin install plugins/workspace
harness plugin list
harness plugin config workspace --set root=$PWD/demos/sample-project
harness plugin enable notes --yes
harness plugin enable workspace --yes
harness tool list
harness run "读一下 README.md，把要点存成笔记" --script demos/readme_to_note.json
harness runs list
```

`--script` 用预设的模型回复代替真实 LLM，但每次工具调用都会启动真实的插件进程。最后一条命令列出运行记录，可以用 `harness runs show <运行ID>` 回看完整过程。

## 使用真实 LLM（OpenRouter）

```bash
cp .env.example .env
harness run "列出 sample-project 里的文件，读 README.md，把要点存成笔记"
```

需要先在 `.env` 中填写 `OPENROUTER_API_KEY`。默认模型是 `anthropic/claude-opus-4.8`，可以用 `--model` 或 `HARNESS_LLM_MODEL` 更换（模型必须支持 tool calling，不能用 `:batch` 变体）。API Key 不会传给插件进程。

## 命令一览

| 命令 | 说明 |
|---|---|
| `harness plugin discover [DIR]` | 扫描目录中的插件：版本、兼容性、安装状态 |
| `harness plugin install PATH [--dev]` | 复制安装（`--dev` 链接到源码目录） |
| `harness plugin list` / `info ID` | 已安装插件、有效状态、原因和下一步 |
| `harness plugin config ID [--set k=v] [--unset k]` | 查看或修改配置（按插件的 `config_schema` 校验） |
| `harness plugin enable ID [--yes]` / `disable ID` | 启用（展示权限声明并确认）/ 禁用（撤销授权） |
| `harness plugin update ID [--from PATH]` | 更新到更高版本；权限声明变化后需重新启用 |
| `harness plugin uninstall ID [--purge]` | 卸载（`--purge` 同时删除数据） |
| `harness plugin validate PATH` | 静态校验插件清单，不执行代码 |
| `harness plugin new ID [--dir DIR]` | 从模板生成插件骨架 |
| `harness tool list` | Agent 当前可用的工具 |
| `harness tool call NAME --args JSON [--yes] [-v]` | 绕过 LLM 直接调用工具（调试用） |
| `harness run PROMPT [--script FILE] [--model M] [--yes]` | 运行一次 Agent |
| `harness runs list` / `runs show ID` | 历史运行与执行追踪 |

插件有效状态：`broken`（清单损坏）→ `incompatible`（版本不兼容）→ `disabled` → `needs_config` → `needs_approval`（更新后权限声明变化）→ `active`。只有 `active` 的插件会进入 Agent 的工具表。

## 示例插件

| | `notes` | `workspace` |
|---|---|---|
| 能力 | 本地笔记：`add_note`、`list_notes`、`search_notes`、`delete_note` | 只读访问指定目录：`list_files`、`read_file`、`search_text` |
| 数据 | 插件数据目录下的 SQLite | 用户配置的 `root` 目录 |
| 配置 | `max_notes`（有默认值） | `root`（必填，绝对路径） |
| 安全 | `delete_note` 每次调用都需要用户确认 | 路径（含符号链接）必须位于 `root` 内；跳过二进制文件和过大的文件 |

两个插件使用完全相同的机制：安装、启用、配置、注册、调用。

## 写一个插件

```bash
harness plugin new hello --dir /tmp
harness plugin install /tmp/hello --dev
harness plugin enable hello --yes
harness tool call hello__greet --args '{"name": "Ada"}' -v
```

插件目录包含 `plugin.json`（清单）和入口 `main.py`：

```python
from harness_sdk import Context, Plugin, ToolError, ToolOutput

plugin = Plugin()


@plugin.tool("greet")
def greet(ctx: Context, name: str) -> ToolOutput:
    return ToolOutput(content={"message": f"Hello, {name}"}, display=f"Hello, **{name}**")


if __name__ == "__main__":
    plugin.run()
```

- 清单字段：`api_version`（当前为 1）、`id`（`^[a-z][a-z0-9-]{0,29}$`）、`name`、`version`（语义化版本）、`description`、`harness_version`（PEP 440 范围，可选）、`entry`、`permissions`、`config_schema`（JSON Schema，可选）、`tools[]`（`name`、`description`、`input_schema`，可选 `requires_confirmation`、`timeout_s`）。
- 暴露给模型的工具名是 `<plugin_id>__<tool_name>`。宿主先按 `input_schema` 校验参数，再启动插件进程。
- 业务错误抛出 `ToolError`；持久数据写到 `ctx.data_dir`；`ctx.config` 是校验后并补齐默认值的配置。
- 返回结果应当有界：列表类工具支持 `limit`/`offset` 并返回 `total`/`next_offset`。超过 20000 字符的结果会被宿主截断，模型会被明确告知；完整结果保存在 `runs/<run_id>/results/`。
- 插件中的 `print()` 不会破坏协议。单元测试可以用 `plugin.invoke(tool, args, data_dir=..., config=...)`。

**协议**：每次工具调用启动一个新进程（`sys.executable entry`）。宿主向 stdin 写一行请求 JSON，插件在 stdout 的最后一行写出响应 `{"api_version":1,"call_id":...,"ok":true,"content":...}`，stderr 是日志。不使用 SDK 的插件只需遵守这条规则即可。

## 设计要点

1. **插件发现与注册**：插件能力在 `plugin.json` 中静态声明，发现、安装、查看、授权都只读取清单，不执行代码。每次运行开始时，用 active 插件重建注册表，系统提示词和工具列表都由注册表生成，宿主代码中没有任何示例插件的名字（有测试检查）。
2. **宿主与插件的通信**：插件进程是一次性的，通过 stdin/stdout 传 JSON，一次请求对应一个响应。数据通过请求参数、配置和数据目录传递；插件不渲染 UI，只返回 `content`（给模型）和 `display`（给人看）；插件贡献的是工具，不能注册 CLI 命令。
3. **生命周期与兼容性**：只持久化用户意图（是否启用、配置、已批准的权限声明），有效状态每次加载时重新计算。安装和更新先复制到新目录，再用符号链接原子切换，中途崩溃不会出现半装状态。`api_version` 和 `harness_version` 不兼容时拒绝安装，已安装的插件标记为 `incompatible`。
4. **稳定性**：插件崩溃、超时（默认 30s，终止整个进程组）、输出非法、输出超过 10 MB、参数错误都转换为带错误码的结果（`PLUGIN_CRASHED`、`PLUGIN_TIMEOUT`、`PROTOCOL_ERROR`、`RESULT_TOO_LARGE`、`INVALID_ARGUMENTS`、`TOOL_ERROR`），交给模型自行修正，不影响宿主。`max_steps` 防止死循环；Ctrl+C 会终止插件进程。
5. **安全边界（如实说明）**：**插件是以当前用户身份运行的任意代码，宿主没有沙箱。** 宿主真正能强制执行的只有：未启用的插件不会被执行；启用时展示权限声明并要求确认；插件进程只拿到最小环境变量和它声明并被批准的变量（宿主的 API Key 不会传入）；`requires_confirmation` 的工具每次调用都需要确认；安装时拒绝符号链接、限制大小、入口不能逃逸出插件目录；超时和输出大小限制。`network`、`filesystem`、`subprocess` 声明只用于告知用户，宿主无法拦截。启用插件即表示信任它的代码。

## 测试

```bash
uv run pytest
uv run ruff check .
```

测试不需要网络和 API Key，覆盖插件进程协议、生命周期、崩溃一致性、重启后状态恢复、两个示例插件、跨插件 Agent 运行和架构依赖规则。

## 目录结构

```
src/agent_harness/   宿主：CLI、应用服务、插件管理、注册表、执行器、Agent Loop、追踪
src/harness_sdk/     插件 SDK（只依赖标准库）及 plugin new 模板
plugins/             示例插件 notes、workspace
demos/               离线场景与示例项目目录
tests/               单元测试、插件测试、集成测试
```
