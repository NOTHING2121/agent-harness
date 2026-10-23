# Local Agent Harness

本地运行、支持插件扩展的 AI Agent Harness。用户可以安装、查看、配置、启用并运行插件；插件开发者按照一套清晰的协议为宿主增加新的工具。

> 当前状态：**项目骨架**。模块接口已按 [DESIGN.md](DESIGN.md) 搭好，插件加载与调用、真实 LLM 接入等功能尚未实现，进度见 [PROGRESS.md](PROGRESS.md)。

## 环境要求

- macOS 或 Linux
- [uv](https://docs.astral.sh/uv/)：项目通过 `.python-version` 固定 Python 3.12，由 uv 自动下载和管理，不依赖系统自带的 Python

## 安装与运行

```bash
uv sync                      # 创建虚拟环境并安装依赖（含开发依赖）
uv run harness --version     # 显示版本
uv run harness --help        # 查看命令
```

## 测试与代码检查

```bash
uv run pytest                # 全部测试，使用离线 fake LM，不需要网络和 API Key
uv run ruff check .          # 代码检查
uv run ruff format --check . # 格式检查
```

## 目录结构

```
src/agent_harness/   宿主：CLI、应用服务、插件管理、Agent Loop、持久化
src/harness_sdk/     插件 SDK：只依赖标准库，插件通过它与宿主通信
plugins/             示例插件（待添加）
demos/               离线演示场景（待添加）
tests/               单元测试与集成测试
```

设计说明见 [DESIGN.md](DESIGN.md)，开发规范见 [Agent.md](Agent.md)。
