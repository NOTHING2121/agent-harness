# PROGRESS

## 当前状态

| 阶段 | 状态 |
|---|---|
| Step 0 项目骨架 | 已完成 |
| **M1 端到端最小闭环** | **已完成**（默认模型已改为 `anthropic/claude-opus-4.8`，真实运行通过） |
| M1 设计一致性核对与修正 | 已完成（DESIGN.md 升至 v0.3） |
| 默认模型切换为 Claude Opus 4.8 | 已完成（DESIGN.md 升至 v0.4） |
| 交付范围 | 已确认：只交付 README 和最终产物（README 必须自包含） |
| M2 插件管理 | 已完成 |
| M3 异常处理与追踪 | 已完成（stdout 10 MB 上限、大结果截断、追踪落盘、runs list/show、失败时显示 stderr） |
| M4 第二个插件与扩展性 | 已完成（workspace、跨插件 demo、plugin new/update/discover、notes delete_note） |
| M5 交付 | 已完成：README 自包含；在全新目录按 README 验收通过；产物在 `dist/`（DESIGN.md 升至 v0.6） |

---

## M2–M5 插件管理、异常处理、第二个插件与交付（2026-10-23）

### 本阶段实现

- **M2 插件管理**：复制安装与卸载（先复制再原子切换符号链接）、`state.json` 与文件锁、有效状态计算、兼容检查、`enable`（展示权限声明并确认）/ `disable`（撤销授权）、`config`、`list`、`info`、`validate`、`permissions.env` 授权后才传入。
- **M3 异常处理与追踪**：PluginRunner 改为后台线程读写管道，stdout 超过 10 MB 返回 `RESULT_TOO_LARGE`；结果超过 `tool_result_max_chars` 时交给模型明确标注的截断版本，完整结果保存在 `runs/<run_id>/results/`；追踪写入 `runs/<run_id>.jsonl`，新增 `runs list / show`；`run` 中工具失败时显示 stderr 末尾和回看命令；交给模型的错误带上修复建议。
- **M4 第二个插件与扩展性**：`workspace` 插件（只读、路径限制在 `root` 内、跳过二进制和过大文件、三个工具都可分页）；跨插件离线场景 `demos/readme_to_note.json` 与示例目录 `demos/sample-project/`；`notes` 增加需要确认的 `delete_note`；`plugin update / discover / new`（模板在 `harness_sdk/templates/`）。
- **M5 交付**：README 重写为自包含（安装、离线验收、命令、示例插件、插件开发指南、设计要点与真实安全边界）；`dist/` 中生成源码 zip、wheel 和 sdist。

### 接手时发现的问题

接手时工作区中有约 1400 行未提交的 M2 代码（来自之前中断的会话），其中两个测试失败：测试插件缺少 `plugin.run()`；CLI 测试没有考虑 `HarnessError` 由 `main()` 渲染、Rich 表格会在 80 列折行。已修复测试本身，实现无需改动。

### 验收结果

| 验收项 | 结果 |
|---|---|
| 在全新目录解压源码 zip，按 README 执行 `uv sync` → discover → install → config → enable → 离线跨插件运行 → `runs list` → `plugin new` → install → call | 通过 |
| 手动：`delete_note` 非交互时拒绝、`--yes` 时执行；`../` 和指向 `root` 外的符号链接被拒绝；`update` 版本不升高时拒绝、权限声明变化后进入 `needs_approval`、旧副本被删除 | 通过 |
| 真实 LLM 运行 | **未通过：`.env` 中的 OpenRouter Key 已失效**（服务端返回 “User not found”）。CLI 显示鉴权失败和修复建议，运行记录为 failed，行为符合预期；换有效 Key 后需重新验证 |
| `pytest` | 225 passed |
| `ruff check .` / `ruff format --check .` | 全部通过 |

### 遗留问题

1. README “设计要点”按假设回答题目的 5 个思考点（原题目不在仓库中），需要对照原题确认。
2. 需要有效的 OpenRouter Key 才能重新验证真实 LLM 运行。

---

## 默认模型切换为 Claude Opus 4.8（2026-10-23）

用户指定使用 `anthropic/claude-opus-4.8:batch`。先查询 OpenRouter 的模型目录和账户 guardrail：这个 ID 存在，支持工具调用，账户也允许使用。但真实调用返回 404，原文是 `cannot be used with the chat/completions endpoint (adapter AnthropicBatchAdapter)`。`:batch` 变体只能走 OpenRouter 的异步批处理接口，而 Agent Loop 每一步都要同步等待模型回复，所以默认模型改为同一模型的同步版本 `anthropic/claude-opus-4.8`（价格为批处理版的两倍：输入 $5/M，输出 $25/M）。

| 验收项 | 结果 |
|---|---|
| `harness run --model anthropic/claude-opus-4.8:batch "你好"` | 按预期失败，并显示新的专门提示：`:batch` 变体只能用于异步批处理，请去掉后缀，改用 `anthropic/claude-opus-4.8` |
| 不加 `--model` 真实运行“记一条笔记，然后列出所有笔记” | 通过：模型依次调用 `notes__add_note`（66 ms）和 `notes__list_notes`（68 ms），共 3 步，约 9 秒，退出码 0 |
| `pytest` | 160 passed |
| `ruff check .` | 通过 |

---

## M1 设计一致性核对与修正（2026-10-23）

### 目标

M1 完成后对照 DESIGN.md 逐节核对实现。实现偏离设计的地方，修改代码向设计看齐；更合理的实现做法，则写回 DESIGN.md（v0.3），保证文档与代码一致。

### 发现的偏差与处理

| 位置 | 偏差 | 处理 |
|---|---|---|
| §3.2 | CLI 直接调用了 `plugins.manifest.qualified_tool_name` | 改用 `PluginInfo.tool_names`；DESIGN.md 写明“CLI 只通过 `Harness` 调用功能，其他模块只能引用数据类型”，并新增架构测试（已验证它能拦住违规导入） |
| §12 | `llm.response` 缺耗时；`tool.finished` 缺结果摘要和 stderr 末尾 | 补齐 `duration_ms`、`result_preview` / `result_chars`、`stderr_tail` |
| §12、§6.3 | 没有全局 `-v`；`tool call -v` 不显示请求 | 全局 `-v` 只打开宿主自己的 DEBUG 日志，不记录参数、配置和环境变量；`tool call -v` 显示请求、响应和 stderr |
| §8.4、§14 | `search_notes` 不支持 `offset`，也不返回 `next_offset`（核对时漏掉，手动验证时发现） | 补齐分页；新增经由宿主的分页端到端测试（已验证清单缺少 `offset` 时这条测试会失败） |
| §7.2 | 开发模式重复安装同一来源时视为成功，与文档不一致 | 保留实现，写入 DESIGN.md |
| §10.2 | 额外传了 `LC_ALL`、`PYTHONUTF8`、`PYTHONDONTWRITEBYTECODE` | 保留实现，写入 DESIGN.md 并说明理由 |
| §15 | `tests/plugins/` 目录和 `python-dotenv` 依赖没有记录 | 写入 DESIGN.md |

### 验证结果

| 验收项 | 结果 |
|---|---|
| `pytest` | 159 passed |
| `ruff check .` / `ruff format --check .` | 全部通过 |
| 手动运行 `harness -v tool call notes__search_notes --args ... -v` | DEBUG 日志、请求、响应、stderr 都按 §6.3 和 §12 显示，调试日志里没有工具参数 |

### 经验

`plugin.invoke` 不经过宿主的 Schema 校验，所以只用它测试插件，发现不了“清单与实现不一致”。这一点已写进 DESIGN.md §6.2 和 §16，后续的插件（包括 `workspace` 和 `plugin new` 模板）都要有经由宿主的测试。

### 仍然按计划推迟的部分（不是偏差）

大结果处理和输出大小上限（M3）：目前完整结果会交给模型，stdout 的读取量也没有上限。启用、授权和 `permissions.env` 的批准（M2）：目前所有已安装的插件都可以调用。`run` 失败时显示 stderr（M3）。

---

## M1 端到端最小闭环（2026-10-23）

### 目标

用最小的插件协议、一个简单插件（`notes`）和 Agent Loop 跑通完整链路：插件能动态接入，Agent 能通过注册表发现并真实调用插件工具（DESIGN.md §17）。

### 验收标准与结果

| 验收项 | 结果 |
|---|---|
| `harness plugin install plugins/notes --dev` 后 `tool list` 显示 3 个工具 | 通过 |
| `harness tool call` 经真实插件进程执行，参数错误在进入插件前被拦截 | 通过 |
| 离线：`harness run ... --script demos/notes_demo.json` 完成两次真实插件调用并给出回答 | 通过 |
| 真实 LLM：OpenRouter 上的模型自主调用 `add_note` 与 `search_notes`，并根据结果作答 | 通过（使用 `--model openai/gpt-5.6-sol`，见下文） |
| 默认模型 `openai/gpt-6.1-sol` 真实运行 | **未通过：被 OpenRouter 账户的 guardrail 屏蔽**（不是代码问题，见遗留问题 1） |
| `pytest` | 150 passed（约 2.5 s，不需要网络和 API Key） |
| `ruff check .` / `ruff format --check .` | 全部通过 |

### 真实调用记录

用 `.env` 中的 OpenRouter Key 在本机执行了两次运行：

1. `harness run "..."`（默认模型 `openai/gpt-6.1-sol`）：鉴权通过，但 OpenRouter 返回 404，原因是 “Model blocked by guardrail”。经 `/api/v1/models/user` 确认，该账户允许 439 个模型，其中不包含 GPT-6.x 系列（OpenAI 模型最高到 GPT-5.6）；该模型本身的 8 个端点状态正常。CLI 现在会显示服务端的错误消息和修复建议（调整 guardrail 或用 `--model` 换模型）。
2. `harness run --model openai/gpt-5.6-sol "帮我记一条笔记……再按关键词 README 搜索确认……"`：模型依次调用 `notes__add_note`（61 ms）和 `notes__search_notes`（62 ms），两次都是真实的插件子进程；最终回答正确引用了新笔记 ID 和搜索结果，共 3 步，退出码 0。

### 本阶段实现

- **插件协议链路**：SDK 的 `Plugin.run / handle / invoke`。先复制出协议专用的描述符，再把 fd 1 重定向到 stderr，插件中的 `print()` 和子进程输出都不会破坏协议。`load_manifest` 做静态校验（JSON、字段、入口必须在插件目录内）。`PluginRunner` 每次调用启动一个短生命周期进程，只传最小环境变量（宿主的 API Key 不会传给插件）；超时会终止整个进程组；结果判定区分崩溃、协议错误和插件错误。请求中增加了 `plugin_id`（DESIGN.md §5.2）。
- **插件管理（M1 范围）**：`install --dev` 通过临时链接加 `os.replace` 原子地创建链接，重复安装同一来源是幂等的；`active_plugins` 加载所有清单有效的插件，清单损坏的插件跳过并记录警告。
- **工具执行**：`ToolExecutor` 依次做查找、JSON 解析、`input_schema` 校验、确认检查、执行，插件错误一律转成 `ToolResult`。
- **notes 插件**：基于 SQLite 实现 `add_note`、`list_notes`（分页，返回 `total` 和 `next_offset`）、`search_notes`（字面匹配，转义 `%` 和 `_`）。写操作在 `BEGIN IMMEDIATE` 事务中进行。
- **LLM**：`ScriptedClient.from_file` 读取离线场景。`OpenAICompatibleClient` 默认走 OpenRouter：不发送 temperature 和 top_p，SDK 异常转成带建议的 `LLMError`，错误信息里不含 API Key。Agent 运行失败时，修复建议会写入运行结果并显示在终端。
- **CLI**：`plugin install --dev`、`tool list`、`tool call [--args] [--yes] [-v]`、`run [--llm] [--script] [--model] [--max-steps] [--yes]`。`run` 会实时显示模型回复、工具调用和结果；启动时读取当前目录的 `.env`，不覆盖已有的环境变量。
- **测试**：插件运行器测试都启动真实进程，覆盖成功、业务错误、异常、参数不匹配、未注册工具、崩溃、导入失败、非 SDK 插件、超时后子进程被终止、环境变量隔离等情况。端到端测试覆盖安装、调用、离线 Agent 运行、重启后数据仍在、16 个并发写入不丢数据，以及 CLI 全流程。

### 提前实现的安全默认值

为避免能力先于保护上线，M1 中提前加入了两项很小的检查（已记录在 DESIGN.md §18）：
- **工具确认**：`requires_confirmation` 的工具在没有确认回调时一律拒绝。
- **最小环境变量**：插件进程只拿到最小环境。M1 阶段插件声明的 `permissions.env` 会直接传入，授权确认在 M2 加入。

### 尚未实现（按计划在后续阶段完成）

| 功能 | 阶段 |
|---|---|
| 复制安装、卸载、`state.json` 与文件锁、启用/禁用、配置、兼容检查、`list` / `info` / `validate`、权限授权确认 | M2 |
| 大结果处理（§8.4）、输出大小上限、追踪落盘与 `runs list / show`、错误展示中的 stderr 末尾 | M3 |
| `workspace` 插件、`plugin new` 模板、`update`、`discover`、第三个插件的扩展性测试 | M4 |

### 遗留问题

1. ~~默认模型被账户 guardrail 屏蔽~~ **已解决**：默认模型改为账户允许的 `anthropic/claude-opus-4.8`，见上文“默认模型切换为 Claude Opus 4.8”。
2. ~~文档文件名~~ **不再需要处理**：交付物只有 README 和最终产物，`Agent.md` 作为开发文档保留原名。
3. **本地开发环境**：在当前 Manus 环境中，`uv run` 无法在 uv 缓存目录加锁，所以测试和检查直接用 `.venv/bin/pytest`、`.venv/bin/ruff`，效果与 `uv run` 相同。

---

## Step 0 项目骨架（2026-10-23）

按 DESIGN.md §15 搭好目录、依赖配置、核心模块和 CLI 入口，模块接口与设计文档一致。验收结果：`uv sync`（Python 3.12.15）、`harness --version` / `--help`、wheel 同时包含 `agent_harness` 与 `harness_sdk`、63 个测试、Ruff 检查全部通过。当时只有接口的部分已在 M1 实现，剩余部分见上表。CLI 不带参数时显示帮助并以退出码 2 结束，这是 Click 的默认行为。
