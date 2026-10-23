# PROGRESS

## 当前状态

- 已完成：**Step 0 项目骨架**
- 下一步：**M1 端到端最小闭环**（DESIGN.md §17）

---

## Step 0 项目骨架（2026-10-23）

### 目标

按 DESIGN.md §15 搭好目录、依赖配置、核心模块和 CLI 入口，模块之间的接口（数据模型、类与方法签名）与设计文档一致；不实现完整业务逻辑，但项目能正常安装、启动并运行基础测试。

### 验收标准与结果

| 验收项 | 结果 |
|---|---|
| `uv sync` 能安装（Python 3.12 由 uv 管理） | 通过，Python 3.12.15 |
| `harness --version` / `harness --help` 能运行 | 通过 |
| wheel 同时包含 `agent_harness` 与 `harness_sdk` | 通过（`uv build --wheel` 检查） |
| Agent Loop 用 fake LM（`ScriptedClient`）跑通基础测试 | 通过 |
| `pytest` | 63 passed |
| `ruff check .` / `ruff format --check .` | 全部通过 |

### 已实现并有测试覆盖

- 工程配置：`pyproject.toml`（hatchling 构建，`harness` 命令入口，Ruff、pytest 配置）、`.python-version`、`uv.lock`。
- `settings`：`HarnessPaths` 目录布局；`config.toml` 加环境变量的合并与校验，非法配置报出字段路径。
- `errors`：错误码 `ErrorCode`、`ErrorInfo`、`HarnessError` 系列异常、`not_implemented` 占位工具。
- `harness_sdk.protocol`：协议常量与请求、响应消息结构。
- `plugins.manifest`：清单模型及字段级静态校验（ID 与工具名格式、语义化版本、入口路径、JSON Schema 合法性、PEP 440 版本范围、工具名唯一），以及工具全名的拼接与拆分。
- `plugins.registry`：`ActivePlugin`、`ToolRegistry`、导出 OpenAI 工具定义。
- `plugins.executor`：`ToolResult` 及 `for_llm()`（不把 stderr 发给 LLM）。
- `agent.llm`：消息类型、`LLMClient` 接口、`ScriptedClient`（离线 fake LM）。
- `agent.loop`：`AgentLoop`，包括工具结果回传模型、工具失败不中断运行、`max_steps`、LLM 错误时标记 failed、Ctrl+C 时标记 cancelled，以及系统提示词由注册表生成。
- `trace`：内存中的事件流与订阅者；订阅者出错只记录日志，不影响运行。
- `app.Harness`：组合根，`open()` 和 `run()` 的连接已用 fake LM 测试。
- `cli`：Typer 入口与 `--version`；`HarnessError` 统一显示为友好的错误信息。
- 架构测试：宿主不引用示例插件；SDK 只依赖标准库；宿主只引用 `harness_sdk.protocol`。

### 只有接口、尚未实现

调用以下方法会抛出 `NotImplementedError`，并注明计划完成的阶段：

| 接口 | 计划阶段 |
|---|---|
| `harness_sdk.Plugin.run` / `Plugin.invoke` | M1 |
| `manifest.load_manifest` | M1 |
| `PluginRunner.call` | M1 |
| `ToolExecutor.execute` | M1 |
| `OpenAICompatibleClient.complete` | M1 |
| `PluginManager.active_plugins`、`install --dev` | M1 |
| `PluginManager.install`（复制）/ `uninstall` / `enable` / `disable` / `configure` / `list_plugins` | M2 |
| `manifest.check_compatibility`、`StateStore.load` / `transaction` | M2 |
| `PluginManager.update` | M4 |

CLI 目前只有 `--version` 和 `--help`，各子命令在对应阶段加入。`plugins/`、`demos/`、`tests/fixtures/plugins/` 目前是空目录。

### 备注与遗留问题

1. **LLM 凭据（待确认）**：默认读取 `OPENAI_API_KEY`，通过 OpenAI 兼容接口调用 `gpt-6.1-sol`（DESIGN.md §18）。
2. **文档文件名**：仓库中的规范文件名为 `Agent.md`，与常见约定 `AGENTS.md` 不一致，未改动，待确认是否重命名。
3. **本地开发环境**：在当前 Manus 沙箱中 `uv run` 无法在 uv 缓存目录加锁，因此本阶段用 `.venv/bin/pytest` 和 `.venv/bin/ruff` 执行测试与检查，效果与 `uv run` 相同。正常终端中直接用 README 里的 `uv run ...` 即可。
4. CLI 不带参数时显示帮助并以退出码 2 结束，这是 Click 的默认行为。
