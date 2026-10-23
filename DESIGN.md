# Local Agent Harness 设计文档

> 状态：v0.3（M1 实现后同步）。作为后续开发的依据，变更需同步更新本文档。
> 日期：2026-10-23

本文档定义 Harness 的模块架构、插件协议、生命周期、数据流与安全边界。原则是：**先保证插件能动态接入、Agent 能实际调用、整条链路能跑通**，工程能力够用即可，不提前做满。

### 修订记录

**v0.3**（M1 完成后对照实现逐节核对，记录调整与澄清）

- §3.2：明确 CLI 的依赖规则——功能只通过 `Harness` 调用，从其他核心模块只能引用数据类型，并由架构测试检查。
- §6.2、§16：说明 `plugin.invoke` 不做 `input_schema` 校验，清单与实现是否一致需要经由宿主的测试来保证。
- §7.2：开发模式下重复安装同一来源视为成功（幂等）。
- §10.2：补全传给插件进程的环境变量（`LC_ALL`、`PYTHONUTF8`、`PYTHONDONTWRITEBYTECODE`）。
- §11.3、§15、§18：LLM 默认走 OpenRouter，CLI 读取当前目录的 `.env`，新增依赖 `python-dotenv`。
- §12：补全事件字段，明确 `-v` 只打开宿主自己的调试日志。
- §15：插件测试放在 `tests/plugins/`。

**v0.2**（根据评审意见修改）

- §17：开发顺序改为先用最小协议、一个插件和 Agent Loop 跑通端到端，再补插件管理、异常处理和第二个插件。
- §2、§5：去掉 JSON-RPC、常驻进程、后台线程和自动重启，改为"每次调用启动一个短生命周期进程，一个请求对应一个响应"。
- §5.3：修正 stdout 重定向问题——SDK 先复制出协议专用的文件描述符，再把 fd 1 指向 stderr，协议输出不受影响。
- §10：不再模拟无法强制执行的细粒度权限，只保留宿主真正能做到的授权确认与安全检查，并把真实的安全边界写清楚。
- §7、§11：去掉"SQLite 事务保证安装原子性"的错误说法。安装和更新改为"先复制到新目录，再用符号链接原子切换"；`state.json` 采用原子替换加文件锁。
- §14：`notes` 改用 SQLite 存储，解决多进程同时写的问题；第二个插件由 `web` 改为 `workspace`（只读访问本地目录）。
- §8.4：大结果不再直接截断丢弃——插件按分页约定返回结果，宿主兜底时保留完整结果，并明确告诉模型结果被截断。
- 删减：`chat`、`doctor`、`revoke`（并入 `disable`）、`plugin check`、单独的日志文件、敏感配置存储。

---

## 1. 目标与范围

**MVP 包含**

- 产品形态为 CLI（命令 `harness`），核心逻辑与 CLI 解耦，可脱离 CLI 测试。
- 插件管理：发现、安装、卸载、启用、禁用、配置、更新。
- 插件以独立子进程运行，宿主与插件通过 stdin/stdout 交换 JSON。
- Agent Loop：从注册表动态获取工具交给 LLM，执行 LLM 发起的工具调用并回传结果。
- LLM 接入：OpenAI 兼容接口（默认走 OpenRouter，模型 `openai/gpt-6.1-sol`），以及离线脚本回放（无需 API Key 的确定性演示）。
- 执行追踪、本地持久化、版本兼容检查、启用时的授权确认。
- 两个示例插件：`notes`（自有数据的读写）和 `workspace`（只读访问用户指定目录）。

**MVP 刻意不做**

- 在线插件市场、从 URL 或 Git 安装（只支持本地目录）。
- 操作系统级沙箱，以及文件、网络的访问控制（见 §10）。
- 插件常驻进程、插件注册 CLI 子命令、插件自定义 UI、插件间互相调用。
- 多轮交互 `chat`、并行工具调用、流式输出、多用户、Windows 支持。

---

## 2. 关键设计决策

| # | 决策 | 理由 | 代价 / 放弃的方案 |
|---|---|---|---|
| D1 | 插件运行在**独立子进程**中，**每次工具调用启动一个新进程**，处理完一个请求即退出 | 不需要后台线程、进程状态机和重启逻辑；崩溃或卡死只影响这一次调用，下一次调用天然是新进程；超时直接用 `subprocess` 实现 | 每次调用多出约 50–100ms 的启动时间，相对 LLM 秒级的延迟可以接受；插件不能在内存中保留跨调用的状态，需要状态就写入数据目录。放弃：常驻进程 + JSON-RPC |
| D2 | 协议为 **stdin 输入一个请求 JSON，stdout 最后一行输出一个响应 JSON** | 一个进程只处理一个请求，不需要请求复用、方法分发和通知机制，JSON-RPC 的复杂度用不上 | 以后需要常驻模式时，可通过 `api_version` 升级协议 |
| D3 | 插件能力在清单中**静态声明**（`plugin.json`） | 安装、查看、授权、生成工具列表都不需要执行第三方代码；工具参数本身就是 JSON Schema，放在 JSON 里最自然 | 放弃：运行时由插件上报能力 |
| D4 | **"是否已安装"以文件系统为准**：安装和更新先复制到新目录，再用符号链接原子切换；`state.json` 只存用户意图 | 文件操作无法放进数据库事务。让每一步都是原子操作，并按顺序执行，任意时刻崩溃都只会留下可清理的垃圾目录，不会出现半装状态 | 依赖符号链接，MVP 只支持 macOS / Linux |
| D5 | **插件的有效状态每次加载时计算**，不持久化 | 更新、宿主升级、配置变化之后不会出现过期状态，相关逻辑集中在一个函数里 | — |
| D6 | **不模拟无法强制执行的权限系统** | 没有操作系统沙箱，细粒度的文件或网络权限只是摆设。宿主只做真正能强制的事，其余的如实告知用户 | 见 §10 |
| D7 | LLM 提供方是**宿主内部接口**，不做成插件 | 插件只有一种职责：给 Agent 增加工具 | — |

---

## 3. 总体架构

```
                 ┌──────────────────────────────────────┐
  用户 ────────► │ CLI（Typer + Rich）                   │  参数解析 / 确认交互 / 渲染
                 └──────────────────┬───────────────────┘
                                    ▼
                 ┌──────────────────────────────────────┐
                 │ Harness 应用服务（组合根）             │  装配模块，提供用例 API
                 └───┬──────────────┬──────────────┬────┘
                     ▼              ▼              ▼
           ┌────────────────┐ ┌─────────────┐ ┌──────────────┐
           │ Agent Runtime  │ │ Plugin      │ │ Persistence  │
           │  AgentLoop     │ │ Manager     │ │  StateStore  │
           │  LLMClient     │ │             │ │  Settings    │
           └───────┬────────┘ └──────┬──────┘ │  Trace       │
                   │                 │        └──────────────┘
                   │                 │ 提供 active 插件的清单
                   ▼                 ▼
           ┌────────────────┐  ┌──────────────────┐
           │ ToolExecutor   │─►│ Plugin Registry  │  工具名 → (插件, 工具定义)
           └───────┬────────┘  └──────────────────┘
                   ▼
           ┌────────────────┐   stdin：请求 JSON          ┌────────────────────────┐
           │ PluginRunner   │ ─────────────────────────► │ 插件进程（每次调用一个） │
           │ 启动/超时/判定   │ ◄───────────────────────── │ main.py + harness_sdk  │
           └────────────────┘   stdout：响应 JSON          └────────────────────────┘
                                stderr：日志
```

### 3.1 模块职责

| 模块 | 位置 | 职责 |
|---|---|---|
| CLI | `agent_harness.cli` | 命令解析、Rich 渲染、交互确认；不含业务规则 |
| Harness 应用服务 | `agent_harness.app` | 组合根：按配置创建并连接各模块，提供 `install / enable / run / call_tool` 等用例方法 |
| Plugin Manager | `agent_harness.plugins.manager` | 安装、卸载、启用、禁用、配置、更新；计算有效状态；管理操作的唯一入口 |
| 清单 | `agent_harness.plugins.manifest` | 清单模型、静态校验、版本兼容检查 |
| Plugin Registry | `agent_harness.plugins.registry` | 由 active 插件构建工具表，解析工具名，导出 LLM 工具定义 |
| ToolExecutor | `agent_harness.plugins.executor` | 一次工具调用的完整链路：查找、参数校验、确认、执行、结果整理；插件错误一律转成结果，不向上抛出 |
| PluginRunner | `agent_harness.plugins.runner` | 启动插件进程、写请求、读响应、超时终止、判定结果类型 |
| Agent Runtime | `agent_harness.agent` | Agent Loop、LLM 客户端（OpenAI 兼容 / 脚本回放） |
| Persistence | `agent_harness.store`、`agent_harness.settings`、`agent_harness.trace` | `state.json` 读写、路径与配置合并、运行追踪 |
| Plugin SDK | `harness_sdk` | 插件侧：注册工具、处理请求与响应、上下文对象；只依赖标准库 |

### 3.2 依赖规则

- 单向依赖：`cli → app → {agent, plugins, store, trace}`；`agent` 只通过构造函数注入的 `Registry` 和 `ToolExecutor` 使用插件；`errors` 是叶子模块。
- **CLI 的功能调用只通过 `Harness`**。CLI 可以从其他核心模块引用数据类型（Pydantic 模型、dataclass、枚举、异常、类型别名），用来做类型注解和渲染（如 `ToolResult`、`Event`、`ErrorInfo`），但不能直接调用其中的函数或业务类。由架构测试检查。
- **宿主核心不 import 任何具体插件，也不出现示例插件的名字**。有测试检查 `src/agent_harness` 中不含示例插件标识。
- `harness_sdk` 不 import `agent_harness`；宿主只引用 `harness_sdk.protocol`（协议常量与消息结构是双方唯一共享的契约）。

### 3.3 一次 `harness run` 的数据流

```
用户 ── "读一下 README.md，把要点存成笔记" ──► CLI ──► Harness.run(prompt)
  1. Manager 计算各插件有效状态 → Registry 只收录 active 插件的工具
  2. AgentLoop 把 [system, user] 消息和工具定义发给 LLMClient
  3. LLM 返回 tool_call：workspace__read_file {"path": "README.md"}
  4. ToolExecutor：查注册表 → 按 JSON Schema 校验参数 →（如需）用户确认
       → PluginRunner：启动 workspace 进程 → 写请求 → 等待退出 → 解析响应
       → 得到 ToolResult（成功，或带错误码的失败）
  5. ToolResult 作为 tool 消息回给 LLM；LLM 接着调用 notes__add_note……
  6. LLM 不再调用工具时得到最终回答；或达到 max_steps 后停止
每一步都写入追踪：CLI 实时显示，同时落盘到 runs/<run_id>.jsonl
```

---

## 4. 插件模型

### 4.1 插件目录

```
my-plugin/
├── plugin.json      # 必需：清单
├── main.py          # 必需：入口（由 plugin.json 的 entry 指定）
└── ...              # 其他模块和资源，入口可以正常 import 同目录模块
```

### 4.2 清单 `plugin.json`

```json
{
  "api_version": 1,
  "id": "notes",
  "name": "Notes",
  "version": "1.0.0",
  "description": "在本地保存、列出、搜索和删除笔记",
  "harness_version": ">=0.1,<1.0",
  "entry": "main.py",
  "permissions": { "network": false, "filesystem": false, "subprocess": false, "env": [] },
  "config_schema": {
    "type": "object",
    "properties": { "max_notes": { "type": "integer", "minimum": 1, "default": 1000 } },
    "additionalProperties": false
  },
  "tools": [
    {
      "name": "add_note",
      "description": "保存一条笔记，返回笔记 ID",
      "input_schema": {
        "type": "object",
        "properties": { "title": { "type": "string" }, "body": { "type": "string" } },
        "required": ["title", "body"],
        "additionalProperties": false
      }
    },
    {
      "name": "delete_note",
      "description": "按 ID 删除笔记",
      "input_schema": {
        "type": "object",
        "properties": { "id": { "type": "integer" } },
        "required": ["id"]
      },
      "requires_confirmation": true
    }
  ]
}
```

| 字段 | 必需 | 规则 |
|---|---|---|
| `api_version` | 是 | 插件 API（清单格式与消息格式）的版本，宿主当前支持 `1` |
| `id` | 是 | 全局唯一，`^[a-z][a-z0-9-]{0,29}$` |
| `name` / `description` | 是 | 展示用；`description` 也会提供给 LLM |
| `version` | 是 | 语义化版本 `MAJOR.MINOR.PATCH` |
| `harness_version` | 否 | PEP 440 版本范围，缺省表示不限制 |
| `entry` | 是 | 入口文件的相对路径，解析后必须位于插件目录内 |
| `permissions` | 否 | 见 §10。只有 `env` 由宿主强制执行，其余字段用于告知用户 |
| `config_schema` | 否 | JSON Schema（`type: object`），用于校验用户配置和补全默认值 |
| `tools[]` | 是 | 至少一个；`name` 匹配 `^[a-z][a-z0-9_]{0,31}$`，在插件内唯一；`input_schema` 顶层必须是 `object` |
| `tools[].requires_confirmation` | 否 | 为 `true` 时每次调用前都需要用户确认 |
| `tools[].timeout_s` | 否 | 单次调用的超时时间，默认 30s，最长 120s |

**工具命名**：暴露给 LLM 的名字是 `<plugin_id>__<tool_name>`（如 `notes__add_note`）。插件 ID 里不含下划线，所以按第一个 `__` 拆分就能无歧义地还原；长度限制保证总长不超过 OpenAI 函数名的 64 字符上限。

**静态校验**（`validate`、`install`、`update` 以及每次加载时执行，**不运行插件代码**）：Pydantic 字段校验；`input_schema` 和 `config_schema` 本身必须是合法的 JSON Schema；入口文件存在且不逃逸出插件目录。错误信息带字段路径，例如 `tools[1].input_schema.type: 必须为 "object"`。

### 4.3 版本兼容

| 检查 | 时机 | 不通过时 |
|---|---|---|
| `api_version` 在宿主支持范围内 | 安装、更新、加载 | 拒绝安装；已安装的插件标记为 `incompatible` |
| `harness_version` 包含当前宿主版本 | 安装、更新、加载（宿主升级后旧插件可能变得不兼容） | 同上，并显示需要的版本和当前版本 |
| 更新时新版本号更高 | 更新 | 拒绝更新 |
| 请求中的 `api_version` 被 SDK 支持 | 每次调用 | SDK 返回 `unsupported_api_version` 错误 |

### 4.4 生命周期

**管理层面**：只持久化三项用户意图——是否启用、配置值、已批准的权限声明。有效状态在加载时按顺序计算：

```
清单缺失或非法             → broken          （显示原因）
版本不兼容                 → incompatible    （显示需要的版本）
用户未启用                 → disabled
配置不满足 config_schema    → needs_config    （列出缺失或错误的字段）
当前声明 ≠ 已批准的声明      → needs_approval  （更新后声明变化，需重新确认）
否则                       → active          ← 只有 active 插件进入注册表
```

```
 (未安装) ── install ──► disabled ── enable（校验配置 + 确认授权）──► active
    ▲                      ▲                                        │
    └──── uninstall ───────┴──────────────── disable ◄──────────────┘
```

**运行层面**：每次工具调用的流程是"启动 → 读请求 → 执行处理函数 → 写响应 → 退出"。没有常驻进程，也就没有进程状态需要管理。插件需要的初始化（如建表）在每次调用时按需执行，并且要能安全地重复执行。

---

## 5. 宿主与插件的协议

### 5.1 调用方式

```
宿主（PluginRunner）                                   插件进程
 │ 启动：sys.executable <entry>                          │
 │   cwd = 插件数据目录，env = 最小环境 + 已批准的变量       │
 │   以新进程组启动                                        │
 │── stdin：一行请求 JSON，然后关闭 stdin ───────────────►│
 │                                                      │ SDK 分发到处理函数
 │◄── stdout：最后一行是响应 JSON ────────────────────────│
 │◄── stderr：插件日志（自由文本）─────────────────────────│
 │ 等待退出；超时则终止整个进程组                            │ 退出
```

- 使用宿主自己的解释器（`sys.executable`）启动插件，**不使用系统的 `python3`**（本机系统 Python 是 3.9）。
- 宿主用 `Popen(..., start_new_session=True)` 加 `communicate(timeout=...)` 实现；超时后用 `os.killpg` 终止整个进程组（包括插件可能派生的子进程）。

### 5.2 消息

请求（stdin）：

```json
{"api_version": 1, "call_id": "c_01", "plugin_id": "notes", "tool": "add_note",
 "arguments": {"title": "会议", "body": "周五发布"}, "config": {"max_notes": 1000}, "data_dir": "..."}
```

响应（stdout 的最后一个非空行）：

```json
{"api_version": 1, "call_id": "c_01", "ok": true, "content": {"id": 4}, "display": "已保存笔记「会议」"}
{"api_version": 1, "call_id": "c_01", "ok": false, "error": {"type": "tool_error", "message": "笔记 7 不存在"}}
```

- `content`：任意 JSON，交给 LLM。`display`：可选，给人看的 Markdown，由宿主决定如何展示。
- `error.type` 的取值：`tool_error`（插件主动抛出的 `ToolError`，属于可预期的业务错误）、`exception`（未捕获的异常，消息里包含异常类型，堆栈写入 stderr）、`invalid_arguments`（参数与处理函数签名不匹配）、`unknown_tool`、`unsupported_api_version`。

### 5.3 stdout 的使用规则

- **响应必须是 stdout 的最后一个非空行。** 之前的行（例如插件在导入阶段 `print` 的内容）被宿主当作日志记录，不影响解析。
- SDK 在 `plugin.run()` 开始时按以下顺序处理文件描述符：
  1. `protocol_fd = os.dup(1)`：**先**把原始 stdout 复制成一个协议专用的描述符；
  2. `os.dup2(2, 1)`：**再**让 fd 1 指向 stderr。此后插件代码里的 `print()`、C 扩展、派生的子进程写到 stdout 的内容都会进入 stderr；
  3. 响应**只写入 `protocol_fd`**，写之前先输出一个换行，保证响应独占一行；写完立即退出。

  这样重定向只影响插件代码自己的输出，SDK 的协议输出不受影响。
- 不使用 SDK 的插件（例如用其他语言编写）只需遵守一条规则：在 stdout 的最后一行写出响应。

### 5.4 宿主如何判定调用结果

| 进程结果 | 判定 |
|---|---|
| 超过超时时间 | `PLUGIN_TIMEOUT`（已终止进程组） |
| stdout 超过 10 MB | `RESULT_TOO_LARGE` |
| 最后一行是合法响应且 `call_id` 匹配 | 按响应中的 `ok` / `error` 处理 |
| 没有合法响应，退出码不为 0 | `PLUGIN_CRASHED`（附退出码和 stderr 末尾） |
| 没有合法响应，退出码为 0 | `PROTOCOL_ERROR`（附 stdout 末尾预览） |

### 5.5 宿主与插件之间传递什么

| 类别 | 方式 |
|---|---|
| 数据 | 请求中携带工具参数、经过校验的配置、数据目录路径；响应中返回 JSON 结果；插件的持久数据放在它自己的数据目录中 |
| 事件 | 没有事件总线。插件通过 stderr 输出日志，宿主将其记录到追踪中；宿主的运行事件不暴露给插件 |
| UI | 插件不渲染界面，只返回 `content`（给 LLM）和可选的 `display`（给人看） |
| 命令 | 插件贡献的是工具，既可以由 Agent 调用，也可以由用户通过 `harness tool call` 直接调用。插件不能注册 CLI 子命令 |

---

## 6. Plugin SDK 与开发者体验

### 6.1 插件代码示例

```python
import sqlite3
from harness_sdk import Context, Plugin, ToolError, ToolOutput

plugin = Plugin()


def open_db(ctx: Context) -> sqlite3.Connection:
    db = sqlite3.connect(ctx.data_dir / "notes.db", timeout=5)
    db.execute(
        "CREATE TABLE IF NOT EXISTS notes ("
        "id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    return db


@plugin.tool("add_note")
def add_note(ctx: Context, title: str, body: str) -> ToolOutput:
    db = open_db(ctx)
    with db:  # 一个事务
        (count,) = db.execute("SELECT COUNT(*) FROM notes").fetchone()
        if count >= ctx.config["max_notes"]:
            raise ToolError("笔记数量已达上限，请先删除旧笔记")
        cur = db.execute(
            "INSERT INTO notes (title, body, created_at) VALUES (?, ?, datetime('now'))",
            (title, body),
        )
    return ToolOutput(content={"id": cur.lastrowid}, display=f"已保存笔记「{title}」")


if __name__ == "__main__":
    plugin.run()
```

### 6.2 SDK 负责的事

- 读取请求、检查 `api_version`、按工具名找到处理函数，并以关键字参数调用。
- 按 §5.3 处理 stdout，写出响应后退出。
- 返回普通值即作为 `content`，返回 `ToolOutput` 可以附带 `display`；结果不能序列化为 JSON 时返回 `exception` 错误。
- 捕获异常：`ToolError` 转为 `tool_error`；其他异常转为 `exception`，堆栈写入 stderr。
- `Context` 提供 `plugin_id`、`call_id`、`config`（已校验并补齐默认值）、`data_dir`，以及写到 stderr 的 `log`。
- `plugin.invoke(tool, arguments, config=..., data_dir=...)`：在进程内直接调用处理函数，插件作者写单元测试时不需要启动宿主。注意：`invoke` **不按 `input_schema` 校验参数**（校验由宿主完成），所以清单与处理函数是否一致（例如参数名、可选参数），需要用 `harness tool call` 或经由宿主的测试来确认。

### 6.3 写出并调试第一个插件

```bash
harness plugin new hello              # 生成 plugin.json、main.py、test_main.py
harness plugin validate ./hello       # 静态检查清单，不执行代码
harness plugin install ./hello --dev  # 开发模式：链接到源码目录，修改代码后无需重新安装
harness plugin enable hello
harness tool call hello__greet --args '{"name": "Ada"}' -v
                                      # 不经过 LLM 直接调用；-v 显示请求、响应、stderr 和耗时
harness run "跟 Ada 打个招呼"          # 经由 Agent 调用
```

---

## 7. 插件管理

### 7.1 操作

| 操作 | 流程 |
|---|---|
| **发现** `discover DIR` | 扫描目录下的插件，列出 ID、版本、描述、兼容性，以及"未安装 / 已安装 / 有新版本"。只读取清单 |
| **安装** `install PATH [--dev]` | 见 §7.2。只复制文件，不执行代码。安装后默认处于未启用状态 |
| **启用** `enable ID [--yes]` | 兼容检查 → 配置校验（缺少必填项时拒绝，并给出 `config --set` 的命令示例）→ 展示权限声明和安全提示并请求确认（见 §10）→ 记录已批准的声明 → 标记为启用 |
| **禁用** `disable ID` | 标记为未启用，同时清除已批准的声明（即撤销授权）；配置保留。重新启用时需要再次确认 |
| **配置** `config ID [--set k=v]... [--unset k]...` | 不带参数时显示当前配置；`--set` 的值优先按 JSON 解析，失败则当作字符串；合并后的配置整体按 `config_schema` 校验，不合法则拒绝并指出字段 |
| **更新** `update ID [--from PATH]` | 来源默认是安装时记录的路径；新版本号必须更高；流程见 §7.2。有效状态会自动重新计算，权限声明变化后进入 `needs_approval` |
| **卸载** `uninstall ID [--purge]` | 流程见 §7.2。默认保留数据目录并提示其路径，`--purge` 时一并删除 |

所有管理操作都在同一把文件锁（`$HARNESS_HOME/.lock`，使用 `fcntl.flock`）内串行执行，避免两个 CLI 进程同时修改。Agent 运行时只读取状态，不需要加锁。

### 7.2 安装、更新、卸载的原子性

文件操作无法和状态文件放在同一个事务里，所以设计成：**每一步都是原子操作，并按特定顺序执行，任意一步之后崩溃都不会出现半装状态，最多留下可以自动清理的目录。**

目录结构（详见 §11）：`packages/` 下存放每次安装的完整副本，内容不再修改；`plugins/<id>` 是指向当前版本副本的符号链接。**"插件已安装"的唯一判断依据是 `plugins/<id>` 这个链接存在。**

**安装**

1. 校验源目录的清单和兼容性；`plugins/<id>` 已存在时拒绝，并提示使用 `update`。
2. 复制到 `packages/.tmp-<随机串>/`。跳过 `.git`、`__pycache__`、`.venv`；遇到符号链接则拒绝；总大小上限 50 MB。
3. 重新校验副本中的清单，防止复制过程中源目录被修改。
4. 将副本重命名为 `packages/<id>-<version>-<随机串>/`。
5. 创建临时链接，再用 `os.replace` 原子地替换为 `plugins/<id>`。**这一步完成，插件即视为已安装。**
6. 在 `state.json` 中记录来源路径。

第 5 步之前崩溃：插件未安装，只留下孤立的副本目录。第 5 步之后、第 6 步之前崩溃：插件已安装但未启用，只是缺少来源记录，此时 `update` 需要用 `--from` 指定来源。两种情况都是一致的状态。

**更新**：前 4 步相同，然后用 `os.replace` 把 `plugins/<id>` 原子地切换到新副本，最后删除旧副本。切换之前崩溃，旧版本仍然有效；切换之后崩溃，新版本已经生效，只多出一个旧副本。

**卸载**：① 删除 `state.json` 中的记录（插件回到"已安装、未启用"的安全状态）→ ② 删除 `plugins/<id>` 链接（插件即不存在）→ ③ 删除副本目录（开发模式不会删除用户的源码）→ ④ 如果指定了 `--purge`，删除数据目录。

**开发模式**（`--dev`）：`plugins/<id>` 直接链接到源码目录，不复制，也不需要更新。链接通过"创建临时链接 + `os.replace`"原子地生成。重复安装同一个源码目录视为成功（幂等），方便反复执行安装脚本；同一 ID 已链接到其他目录时拒绝。

**自动清理**：每次执行管理操作时（在锁内），删除 `packages/` 中没有被任何链接引用的目录，以及残留的 `.tmp-*` 目录；同时删除 `state.json` 中已不存在对应插件的记录。

`plugin list` 示例：

```
ID         版本    状态            工具  说明
notes      1.0.0   active          4
workspace  1.0.0   needs_config    3     缺少必填配置项 root
demo       0.1.0   incompatible    -     需要 harness >=2.0，当前为 0.1.0
```

---

## 8. 注册表与 Agent 调用插件

### 8.1 Plugin Registry

- 每次运行开始时，用 Manager 给出的 active 插件构建注册表，因此启用、禁用、更新都在下一次运行时立即生效。
- 条目是 `RegisteredTool(qualified_name, plugin_id, spec)`；`to_llm_tools()` 导出 OpenAI 函数定义，描述前面加上插件名。
- 系统提示词由注册表内容生成，**不包含任何写死的插件信息**。

### 8.2 Agent Loop

```python
def run(prompt: str) -> RunResult:
    registry = manager.build_registry()
    messages = [system_message(registry), user_message(prompt)]
    for step in range(settings.agent.max_steps):
        response = llm.complete(messages, registry.to_llm_tools())
        messages.append(response.message)
        if not response.tool_calls:
            return finish("completed", response.content)
        for call in response.tool_calls:  # 串行执行
            result = executor.execute(call)  # 不会抛出插件错误
            messages.append(tool_message(call.id, result.for_llm()))
    return finish("max_steps_exceeded")
```

工具失败不会终止循环：错误以结构化结果交给模型，模型可以修改参数重试或换一种方式。`max_steps`（默认 10）防止无限循环。

### 8.3 ToolExecutor

1. 查注册表，找不到时返回 `UNKNOWN_TOOL`。
2. 解析参数 JSON，并用 `jsonschema` 按 `input_schema` 校验，失败时返回 `INVALID_ARGUMENTS`（附字段路径）。
3. 工具标记了 `requires_confirmation` 时，调用 CLI 注入的确认回调；用户拒绝时返回 `USER_DENIED`。非交互环境默认拒绝，除非显式传入 `--yes`。
4. 交给 PluginRunner 执行（§5.4）。
5. 处理大结果（§8.4），写入追踪，返回 `ToolResult`。

```python
class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool
    content: Any | None = None
    display: str | None = None
    error: ErrorInfo | None = None  # code、message、hint
    stderr_tail: str | None = None  # 只用于展示和追踪，不发给 LLM
    duration_ms: int
```

### 8.4 大结果处理：不静默丢失信息

1. **首选做法是插件返回有界、可分页的结果。** 这条约定写进插件开发指南，两个示例插件都会遵守：列表和搜索类工具接受 `limit` / `offset`，返回 `total` 和 `next_offset`；读文件按行区间读取，返回 `total_lines` 和 `next_start_line`。模型可以据此继续获取后面的内容。
2. **宿主兜底**：结果序列化后超过 `tool_result_max_chars`（默认 20000 字符）时：
   - 完整结果保存到 `runs/<run_id>/results/<call_id>.json`，用户可以通过 `harness runs show` 查看；
   - 交给模型的是**明确标注**的截断版本：
     ```json
     {"ok": true, "truncated": true, "original_chars": 53210,
      "preview": "……前 20000 字符……",
      "hint": "结果过长，已截断。请用分页参数或更小的范围重新调用以获取其余内容。"}
     ```
   - 追踪中记录一条截断事件。
3. stdout 超过 10 MB 时返回 `RESULT_TOO_LARGE` 错误，不会静默丢弃。

### 8.5 LLM 接入

```python
class LLMClient(Protocol):
    def complete(self, messages: list[ChatMessage], tools: list[dict]) -> LLMResponse: ...
```

| 实现 | 用途 | 说明 |
|---|---|---|
| `OpenAICompatibleClient` | 真实推理 | 使用官方 `openai` SDK，`base_url` 可配置；默认走 OpenRouter（`https://openrouter.ai/api/v1`），模型 `openai/gpt-6.1-sol`；**不发送 `temperature` 和 `top_p`**（该模型不接受）；`reasoning_effort` 可选；非流式；对 429 和 5xx 错误由 SDK 有限重试；SDK 异常转换为带修复建议的 `LLMError`（鉴权、模型不存在或被账户 guardrail 屏蔽、限流、超时、余额不足） |
| `ScriptedClient` | 离线确定性演示和测试 | 读取场景文件，按顺序返回预设的工具调用和最终回答。**仍然走真实的 Registry → Executor → 插件进程链路**，只替换"模型决策"这一环 |

场景文件示例（`demos/readme_to_note.json`）：

```json
{
  "steps": [
    { "tool_calls": [{ "name": "workspace__read_file", "arguments": { "path": "README.md" } }] },
    { "tool_calls": [{ "name": "notes__add_note", "arguments": { "title": "README 要点", "body": "示例项目说明" } }] },
    { "content": "已读取 README.md 并保存为笔记。" }
  ]
}
```

---

## 9. 稳定性与异常处理

| 情况 | 检测 | 结果 |
|---|---|---|
| 清单非法 / 版本不兼容 / 缺配置 / 未批准 | 有效状态计算 | 不进入注册表；`plugin list` 显示状态和原因 |
| 插件导入报错、启动后立即退出 | 没有合法响应，退出码不为 0 | `PLUGIN_CRASHED`，附 stderr 末尾 |
| 处理函数抛出异常 | SDK 捕获 | `TOOL_ERROR`，消息里包含异常类型 |
| 业务错误（如笔记不存在） | `ToolError` | `TOOL_ERROR`，消息由插件给出 |
| 进程崩溃（`os._exit`、段错误） | 没有合法响应，退出码不为 0 | `PLUGIN_CRASHED`；下一次调用自然是新进程 |
| 卡死 | 超时（默认 30s） | 终止进程组，返回 `PLUGIN_TIMEOUT` |
| stdout 内容混乱 | 最后一行不是合法响应 | `PROTOCOL_ERROR`，附 stdout 末尾预览 |
| 输出过大 | stdout 超过 10 MB | `RESULT_TOO_LARGE` |
| 模型调用不存在的工具、参数不合法 | 注册表查找、Schema 校验 | 错误结果交还给模型，由它自行修正 |
| LLM 请求失败（网络、鉴权、限流） | SDK 重试后仍然失败 | 本次运行标记为 `failed`，并给出可操作的提示（如检查 API Key） |
| 模型反复调用工具 | 达到 `max_steps` | 运行标记为 `max_steps_exceeded` |
| Ctrl+C | `KeyboardInterrupt` | 终止正在执行的插件进程组，运行标记为 `cancelled` |
| 宿主自身崩溃 | — | 插件都是一次性进程，不会留下常驻的孤儿进程 |

失败时的展示示例：

```
✗ workspace__read_file 失败 [PLUGIN_TIMEOUT]
  插件 workspace 在 30s 内没有响应，已终止该进程。
  建议：缩小读取范围，或在 plugin.json 中调大该工具的 timeout_s。
  stderr（最后 3 行）：...
  详情：harness runs show r_20261023_151200
```

---

## 10. 安全边界与授权

### 10.1 真实的安全边界

- **插件是以当前操作系统用户身份运行的任意代码。** 宿主不提供沙箱，插件在技术上可以读写用户能访问的任何文件、访问网络、启动进程。
- 子进程隔离带来的是**故障隔离**（崩溃、卡死不会拖垮宿主）和**环境变量隔离**，而不是安全隔离。
- 因此，启用一个插件的实质是**信任这个插件的代码**。宿主在启用时会把这一点明确告诉用户。

### 10.2 宿主真正做到的事（都可以强制执行、可以测试）

1. **未启用的插件不执行。** 发现、安装、查看、校验都只读取清单和复制文件；只有 active 插件会被启动。
2. **启用时确认授权。** `enable` 会展示插件声明的能力，并附上"这些是插件的自我声明，宿主无法限制"的提示；用户确认后记录已批准的声明。`disable` 即撤销授权。更新后声明发生变化时，插件进入 `needs_approval`，需要重新确认后才会运行。
3. **环境变量隔离。** 插件进程只拿到最小环境（`PATH`、`HOME`、`LANG`、`LC_ALL`、`TMPDIR`）加上它声明且被批准的变量；宿主自己的 `OPENROUTER_API_KEY` 等变量不会传给插件。宿主另外设置 `PYTHONUTF8=1`（插件的输入输出统一使用 UTF-8）和 `PYTHONDONTWRITEBYTECODE=1`（不在插件目录中生成 `__pycache__`，因为开发模式下这是用户的源码目录，复制安装的副本也不应被修改）。插件需要的密钥一律通过环境变量提供，**宿主不存储任何密钥**。
4. **危险操作逐次确认。** 标记了 `requires_confirmation` 的工具每次调用前都要用户确认。
5. **安装时的安全检查。** 入口不得逃逸出插件目录；复制时拒绝符号链接；限制总大小。
6. **资源兜底。** 调用超时后终止进程组；限制输出大小。**不限制** CPU 和内存。

### 10.3 权限声明

```json
"permissions": { "network": false, "filesystem": true, "subprocess": false, "env": ["SOME_API_KEY"] }
```

- `env`：**由宿主强制执行**，只有列出的变量会被传给插件。
- `network`、`filesystem`（是否访问自己数据目录以外的文件）、`subprocess`：**只用于告知**。宿主在启用时展示这些声明，但不会也无法拦截相应的行为。

### 10.4 明确不做、也不宣称的能力

文件和网络的访问控制、CPU 和内存限制、插件签名与来源校验。后续加固方向：macOS `sandbox-exec`、Linux bubblewrap 等系统沙箱，或者用容器运行插件。

---

## 11. 持久化与配置

### 11.1 本地目录

根目录 `HARNESS_HOME` 默认是 `~/.agent-harness`，可以用环境变量覆盖（每个测试用例使用临时目录）。

```
$HARNESS_HOME/
├── config.toml                          # 宿主配置（可选，缺省时全部使用默认值）
├── state.json                           # 用户意图：enabled、config、approved、source、dev
├── .lock                                # 管理操作的文件锁
├── plugins/<id> -> ../packages/<...>    # 符号链接，指向当前版本；开发模式下指向源码目录
├── packages/<id>-<version>-<随机串>/     # 每次安装 / 更新的副本，内容不再修改
├── data/<id>/                           # 插件私有数据目录（如 notes.db）
└── runs/<run_id>.jsonl                  # 执行追踪；大结果存放在 runs/<run_id>/results/
```

### 11.2 `state.json`

```json
{
  "schema_version": 1,
  "plugins": {
    "notes": {
      "enabled": true,
      "config": { "max_notes": 1000 },
      "approved": { "network": false, "filesystem": false, "subprocess": false, "env": [] },
      "source": "/path/to/agent-harness/plugins/notes",
      "dev": false
    }
  }
}
```

- 插件的版本号不存在这里，而是以 `plugins/<id>` 指向的清单为准，避免两处数据不一致。
- 写入方式：持锁 → 读取 → 修改 → 写入同目录下的临时文件并 `fsync` → `os.replace` 原子替换。读取方不加锁，也总能读到完整的文件。

### 11.3 宿主配置

```toml
[llm]
provider = "openai"                 # openai | scripted
model = "openai/gpt-6.1-sol"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"  # 只记录环境变量的名字
reasoning_effort = "medium"         # 可选，不填则不发送
request_timeout_s = 120

[agent]
max_steps = 10
tool_result_max_chars = 20000

[plugins]
default_timeout_s = 30
max_timeout_s = 120
```

优先级：命令行参数 > 环境变量（`HARNESS_HOME`、`HARNESS_LLM_PROVIDER`、`HARNESS_LLM_MODEL`、`HARNESS_LLM_BASE_URL`）> `config.toml` > 默认值。配置文件由 Pydantic 校验，不合法时启动即报错并指出字段。API Key 只从环境变量读取，不写入 `config.toml`、追踪或日志。

**`.env` 文件。** CLI 启动时用 `python-dotenv` 读取**当前目录**下的 `.env`（通常是项目根目录），已存在的同名环境变量优先、不会被覆盖。`.env` 被 `.gitignore` 忽略，仓库中只提交模板 `.env.example`。只读当前目录而不向上查找，是为了避免意外读到上级目录中其他项目的 `.env`。

---

## 12. 执行追踪与日志

- **执行追踪**：每次运行写一个 `runs/<run_id>.jsonl`，每行一个事件。CLI 在运行时实时渲染这些事件，之后可以用 `harness runs show <id>` 回看。

  | 事件 | 主要字段 |
  |---|---|
  | `run.started` | prompt、模型、可用工具列表 |
  | `llm.response` | 步数（`step`）、回复内容（`content`）、工具调用（`tool_calls`）、token 用量（`usage`）、耗时（`duration_ms`） |
  | `tool.started` | `call_id`、工具名、参数 |
  | `tool.finished` | `call_id`、工具名、成功与否、`display`、结果摘要（`result_preview`，前 500 字符）和结果总长度（`result_chars`）、错误（含错误码）、`stderr_tail`、耗时；M3 加入"是否截断" |
  | `run.finished` | 状态（completed / failed / max_steps_exceeded / cancelled）、步数、最终回答、错误和修复建议（`hint`） |

  结果摘要只用于展示和回看。完整结果照常交给模型，并保留在 `RunResult.tool_results` 中。

- **日志**：Python `logging` 输出到 stderr，默认级别为 WARNING。全局选项 `harness -v ...` 把宿主自己的日志（`agent_harness.*`）调到 DEBUG，例如插件进程的启动、退出码和耗时；第三方库（HTTP 客户端等）的日志不会打开，以免输出请求头。调试日志不记录工具参数、插件配置和环境变量。不单独写日志文件，排障主要依靠追踪。另外，`tool call -v` 是子命令自己的选项，用来显示请求、响应、stderr 和耗时（§6.3）。

---

## 13. CLI 命令

| 命令 | 说明 | 阶段 |
|---|---|---|
| `harness tool list` | 当前 Agent 可用的工具 | M1 |
| `harness tool call NAME [--args JSON] [--yes] [-v]` | 不经过 LLM，直接调用工具 | M1 |
| `harness run PROMPT [--llm openai\|scripted] [--script FILE] [--model M] [--yes] [--max-steps N]` | 运行一次 Agent | M1 |
| `harness plugin install PATH --dev` | 链接安装（M1 只支持开发模式） | M1 |
| `harness plugin install PATH` / `uninstall ID [--purge]` | 复制安装 / 卸载 | M2 |
| `harness plugin list` / `info ID` | 已安装插件及其有效状态 / 详情 | M2 |
| `harness plugin enable ID [--yes]` / `disable ID` | 启用（含授权确认）/ 禁用（含撤销授权） | M2 |
| `harness plugin config ID [--set k=v] [--unset k]` | 查看或修改配置 | M2 |
| `harness plugin validate PATH` | 静态校验插件目录 | M2 |
| `harness runs list` / `runs show ID` | 历史运行与追踪 | M3 |
| `harness plugin discover DIR` | 发现可安装的插件 | M4 |
| `harness plugin update ID [--from PATH]` | 更新 | M4 |
| `harness plugin new ID [--dir DIR]` | 从模板生成插件骨架 | M4 |

---

## 14. 示例插件

两个插件使用**完全相同**的扩展机制，但提供的能力、数据来源、配置和失败模式都不同。两者都只依赖标准库和 `harness_sdk`，可以完全离线、确定性地运行。

| | `notes` | `workspace` |
|---|---|---|
| 新增的能力 | Agent 的本地记忆：`add_note`、`list_notes`、`search_notes`、`delete_note` | 读取用户已有的文件：`list_files`、`read_file`、`search_text` |
| 数据 | 插件自己的数据：数据目录下的 SQLite（`notes.db`）。每次调用都在一个事务中完成，多个进程同时写由 SQLite 的锁处理（`timeout=5`），不会出现 JSON 文件那样的覆盖丢失 | 用户指定的目录，只读 |
| 配置 | `max_notes`（有默认值，启用后即可使用） | `root`（**必填**，绝对路径）：未配置时处于 `needs_config`，用来演示配置校验 |
| 权限声明 | 不需要额外权限 | `filesystem: true`（告知用户它会读取数据目录以外的文件） |
| 安全检查 | — | 由插件自己实现：所有路径解析后必须位于 `root` 之内（拒绝 `..` 和指向外部的符号链接）；跳过二进制文件和超大文件 |
| 大结果 | 列表和搜索支持 `limit` / `offset` | 按行区间读取，返回 `total_lines` 和 `next_start_line` |
| 需确认的工具 | `delete_note` | 无 |

两个插件组合起来可以完成"读取 README 并把要点存成笔记"的跨插件任务，正好用来演示一次运行中调用多个插件。

**第三个插件**：由 `harness plugin new` 生成的模板插件，用于验证"新增插件不需要修改宿主代码"。

**测试专用插件**（`tests/fixtures/plugins/`，不随产品发布）：`crashy`（调用中途 `os._exit`）、`sleepy`（卡死）、`noisy`（导入阶段和处理函数中都 `print`，验证 §5.3）、`bad_manifest`、`incompatible`。

---

## 15. 代码目录

```
agent-harness/
├── AGENTS.md  DESIGN.md  PROGRESS.md  README.md
├── pyproject.toml              # uv 管理；console script：harness
├── .python-version             # 3.12
├── src/
│   ├── agent_harness/
│   │   ├── __init__.py         # __version__
│   │   ├── app.py              # Harness 组合根
│   │   ├── settings.py         # 路径、config.toml、环境变量
│   │   ├── errors.py           # 错误码、ErrorInfo
│   │   ├── store.py            # state.json 读写与文件锁
│   │   ├── trace.py            # 运行追踪（JSONL）
│   │   ├── plugins/
│   │   │   ├── manifest.py     # 清单模型、静态校验、兼容检查
│   │   │   ├── manager.py      # 安装 / 启用 / 配置 / 更新 / 卸载 / 有效状态
│   │   │   ├── registry.py
│   │   │   ├── runner.py       # 启动插件进程与结果判定
│   │   │   └── executor.py     # ToolExecutor
│   │   ├── agent/
│   │   │   ├── llm.py          # LLMClient、OpenAI 兼容实现、Scripted 实现
│   │   │   └── loop.py         # AgentLoop
│   │   └── cli/
│   │       ├── main.py
│   │       └── render.py
│   └── harness_sdk/
│       ├── __init__.py         # Plugin、Context、ToolError、ToolOutput
│       ├── protocol.py         # 协议常量与消息结构（宿主唯一引用的 SDK 模块）
│       └── templates/          # plugin new 使用的模板
├── plugins/                    # 示例插件
│   ├── notes/
│   └── workspace/
├── demos/                      # 离线场景文件与示例目录
└── tests/
    ├── unit/
    ├── integration/
    ├── plugins/                # 示例插件自己的单元测试（用 plugin.invoke）
    └── fixtures/plugins/
```

**依赖**：`typer`、`rich`、`pydantic`、`openai`、`jsonschema`（校验插件声明的 Schema）、`packaging`（PEP 440 版本范围）、`python-dotenv`（读取 `.env`）；开发依赖 `pytest`、`ruff`。其余都使用标准库。

---

## 16. 测试策略

所有测试都使用临时的 `HARNESS_HOME`，不依赖网络和 API Key。

| AGENTS.md 中的要求 | 对应测试 |
|---|---|
| 核心逻辑脱离 CLI 测试 | 单元测试直接调用 `Harness` 和各模块；CLI 只做冒烟测试（Typer `CliRunner`） |
| 动态注册与 Tool Calling | 用 `ScriptedClient` 驱动 AgentLoop，经由真实的插件进程完成调用 |
| 插件完整生命周期 | 安装 → 配置 → 启用 → 调用 → 禁用 → 更新 → 卸载 |
| 非法配置、版本不兼容、执行失败 | 测试专用插件覆盖 §9 中与插件相关的每一行 |
| 重启后状态正确恢复 | 用同一个 `HARNESS_HOME` 新建 `Harness` 实例，检查启用状态、配置、授权 |
| 安装与更新的崩溃一致性 | 在 §7.2 的各个步骤之间模拟中断，检查状态一致，且自动清理生效 |
| 两个示例插件使用同一机制 | 两者走同一条 `install` / `enable` / `tool call` / `run` 路径 |
| 插件清单与实现一致 | 经由宿主调用示例插件的每个参数组合（如分页），而不只是用 `plugin.invoke` 测试处理函数 |
| 新增第三个插件无需修改宿主 | 用 `plugin new` 生成插件 → 安装 → 启用 → 调用；并检查宿主源码不含示例插件标识 |

每个阶段结束时运行 `uv run pytest` 和 `uv run ruff check`。

---

## 17. 开发顺序

原则：**M1 结束时就要有一个能完整演示的产品**，之后每个阶段都在可运行的基础上增量扩展。每个阶段都遵循 AGENTS.md 的流程：计划 → 实现 → 测试 → 小粒度提交 → 更新 PROGRESS.md。

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M1 端到端最小闭环** | 工程骨架（uv、Ruff、pytest）；最小清单模型；SDK 与 PluginRunner（含超时）；`install --dev`（只创建链接，此阶段所有已安装插件都视为可用）；Registry；ToolExecutor；AgentLoop；`ScriptedClient` 和 `OpenAICompatibleClient`；`notes` 插件（`add / list / search`）；`tool list`、`tool call`、`run`；运行过程的实时输出 | 离线执行 `harness run --llm scripted --script demos/notes_demo.json "..."`，完成真实的插件调用；有 API Key 时用 `gpt-6.1-sol` 真实运行成功；对应测试通过 |
| **M2 插件管理** | 复制安装与卸载（§7.2）、`state.json` 与文件锁、有效状态计算、兼容检查、`enable`（含授权确认）/ `disable`、`config`、`list`、`info`、`validate`、环境变量隔离 | 生命周期测试、重启恢复测试、崩溃一致性测试通过；Registry 只收录 active 插件 |
| **M3 异常处理与追踪** | §5.4 的完整结果判定与错误码、错误展示（含 stderr 末尾）、测试专用插件、`requires_confirmation`、大结果处理（§8.4）、追踪落盘与 `runs list / show` | §9 中与插件相关的场景测试全部通过 |
| **M4 第二个插件与扩展性** | `workspace` 插件、跨插件场景、`plugin new` 模板、第三个插件的扩展性测试、`update`、`discover` | 两个插件和跨插件场景测试通过；新增插件不改宿主代码 |
| **M5 交付** | README（安装、运行、插件开发指南、验收步骤）、PROGRESS.md、在全新目录中按 README 完整验收一遍 | 按 README 从零执行，全部通过 |

**时间不够时的裁剪顺序**（越靠前越先砍）：`discover` → `plugin new` → `update` → `runs list / show`（保留运行时的实时输出）→ `workspace` 的 `search_text`。M1、M2 的核心能力、两个示例插件和基本的错误处理不裁剪。

---

## 18. 假设与待确认问题

**已采用的假设**

1. 产品形态为 CLI。核心与 CLI 解耦，以后可以在 `Harness` 应用服务之上增加本地 Web UI，无需修改核心。
2. 插件用 Python 编写，由宿主的解释器运行，只能依赖标准库和 `harness_sdk`。协议本身与语言无关（§5.3）。
3. 只支持从本地目录安装。
4. 平台为 macOS 和 Linux（依赖符号链接、`fcntl` 文件锁和进程组），Windows 不在 MVP 范围内。
5. 单用户、单机。允许多个 CLI 进程同时运行：管理操作由文件锁串行化；插件数据的并发安全由插件自己负责（`notes` 使用 SQLite）。
6. 工具调用串行执行；插件不在内存中保留跨调用的状态。
7. 不提供操作系统级沙箱，安全边界以 §10 为准。
8. 本机系统 Python 是 3.9.6，不满足要求；项目用 `.python-version` 固定 3.12，由 uv 自动下载和管理。
9. 真实 LLM 通过 OpenRouter 接入，API Key 放在 `.env` 的 `OPENROUTER_API_KEY` 中；换成其他 OpenAI 兼容服务只需修改 `base_url`、`api_key_env` 和 `model`。
10. M1 中提前实现了两项很小的安全默认值，避免能力先于保护上线：`requires_confirmation` 的确认检查（没有确认回调时一律拒绝），以及插件进程的最小环境变量。M1 阶段插件声明的 `permissions.env` 会直接传入，授权确认在 M2 加入。

**待确认**

1. **默认模型被账户 guardrail 屏蔽。** 当前 OpenRouter 账户的 guardrail 不允许 `openai/gpt-6.1-sol`（允许的 OpenAI 模型最高到 GPT-5.6 系列），请求会返回 404。需要在 OpenRouter 的 guardrail 设置中放行该模型；在此之前可以用 `--model openai/gpt-5.6-sol` 或 `.env` 中的 `HARNESS_LLM_MODEL` 临时替换。
