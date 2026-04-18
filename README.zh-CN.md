# CrabCode

[English](./README.md)

终端中的 AI 编程助手 —— 基于 Python 重新实现，采用清晰的前后端分离架构，兼容 Claude Code Agent / Skill。

> 整体参考 Claude Code 设计


## 架构

- **crabcode-core**：核心引擎。负责 API 调用、工具执行、提示词构造、会话管理和 MCP 集成。对外暴露纯异步事件流接口，不依赖任何 I/O 或终端。
- **crabcode-cli**：终端前端。使用 `rich` + `prompt_toolkit` 实现交互式 REPL、Markdown 渲染和流式输出。
- **crabcode-search** *(可选)*：语义代码搜索。将源文件嵌入为向量并存入 USearch 索引，为 agent 提供 `CodebaseSearch` 工具，支持自然语言代码检索。
- **crabcode-gateway** *(可选)*：多协议（HTTP/gRPC）网关服务器。将 `crabcode-core` 暴露为网络服务，提供 REST API、SSE 事件流、WebSocket 双向通道和 gRPC —— 可与 VSCode 扩展、Web UI 等外部客户端集成。

## 安装

```bash
# 基础安装（不含语义搜索）
pip install crabcode

# 含浏览器自动化
pip install crabcode[browser]

# 含语义搜索
pip install crabcode[search]

# 含云端 Provider 支持
pip install crabcode[bedrock]   # AWS Bedrock
pip install crabcode[vertex]    # Google Vertex AI

# 含网关服务器支持
pip install crabcode[gateway]

# 一键安装全部可选特性
pip install crabcode[all]

# 组合安装
pip install crabcode[search,bedrock]
pip install crabcode[browser,search]
```

### 开发模式

```bash
# 以可编辑模式安装所有包
pip install -e packages/core packages/cli packages/search
# 最小安装
pip install -e packages/core packages/cli
# 含浏览器自动化依赖
pip install -e packages/core[browser] packages/cli
# 启用后首次安装 Chromium
playwright install chromium
```

## 快速开始

```bash
# 设置 API Key
export ANTHROPIC_API_KEY=YourKey

# 管道模式
echo "explain this codebase" | crabcode -p

# 交互式 REPL
crabcode

# 继续上次会话
crabcode --continue      # 或 -c

# 恢复指定会话
crabcode --resume <id>   # 或 -r <id>
```

### 会话管理 CLI

```bash
# 列出当前项目的会话
crabcode sessions list

# 列出所有项目的会话
crabcode sessions list --all

# 按关键词搜索会话
crabcode sessions search "重构认证"

# 导出会话为 Markdown 或 JSON
crabcode sessions export <id> --format md --output chat.md

# 归档旧会话（超过 30 天）并清理文件
crabcode sessions prune --days 30 --delete-files

# 查看使用统计
crabcode stats
crabcode stats --project   # 仅当前项目
```

## 多 API 支持

### 网关服务器（Gateway）

CrabCode 可以作为多协议网络服务运行，支持 VSCode 扩展、Web UI 等外部客户端接入。

```bash
# 启动 HTTP 网关（默认端口 4096）
crabcode gateway

# 自定义端口和地址
crabcode gateway --port 8080 --host 0.0.0.0

# 同时启用 gRPC
crabcode gateway --port 4096 --grpc-port 50051

# 启用 Basic Auth
crabcode gateway --password secret
```

**HTTP API 端点：**

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/session/new` | POST | 创建新会话 |
| `/session/send` | POST | 发送消息（触发 query loop，事件通过 SSE 推送） |
| `/session/interrupt` | POST | 中断当前轮次 |
| `/session/compact` | POST | 手动触发对话压缩 |
| `/session/list` | GET | 列出活跃会话 |
| `/session/resume` | POST | 恢复会话 |
| `/agent/spawn` | POST | 生成子 agent |
| `/agent/{id}` | GET | 获取 agent 状态 |
| `/agent/list` | GET | 列出所有 agent |
| `/agent/{id}/cancel` | POST | 取消 agent |
| `/agent/{id}/input` | POST | 向 agent 发送输入 |
| `/agent/wait` | POST | 等待 agent 完成 |
| `/permission/respond` | POST | 回复权限请求 |
| `/choice/respond` | POST | 回复选择请求 |
| `/config/models` | GET | 列出可用模型 |
| `/config/switch-model` | POST | 切换模型 |
| `/config/switch-mode` | POST | 切换 agent/plan 模式 |
| `/tools` | GET | 列出可用工具（含 MCP） |
| `/context` | POST | 推送工作区上下文（活动文件、选中内容、光标位置） |
| `/snapshot/checkpoint` | POST | 创建带文件快照的检查点 |
| `/snapshot/list` | GET | 列出会话的检查点 |
| `/snapshot/revert` | POST | 回退文件 + 对话到检查点 |
| `/snapshot/rollback` | POST | 仅回滚对话（不还原文件） |
| `/event` | GET (SSE) | 实时事件流（10 秒心跳） |
| `/ws` | WebSocket | 双向通信（VSCode 扩展首选） |

**WebSocket `/ws`** 支持收发命令（`send_message`、`permission_response`、`choice_response`、`push_context`）和事件推送 —— 单个连接即可完成所有交互，适合 VSCode 扩展使用。

**gRPC** 在启用 `--grpc-port` 后可用，提供流式 `SendMessage` 和 `SubscribeEvents` RPC。完整服务定义见 `packages/gateway/crabcode_gateway/grpc/proto/crabcode.proto`。

### ACP（Agent Client Protocol）支持

CrabCode 支持 **Agent Client Protocol (ACP)**，这是一种开放的 JSON-RPC 协议，标准化了代码编辑器与 AI 编码 Agent 之间的通信。可让你直接在 Zed、JetBrains 等 ACP 兼容编辑器中使用 CrabCode。

```bash
# 启动 CrabCode 作为 ACP Agent（通过 stdio 通信）
crabcode acp
```

**工作原理：**

1. `crabcode acp` 启动内部 Gateway HTTP 服务器，然后在 stdio 上启动 ACP Agent
2. 编辑器（Zed、JetBrains）将 `crabcode acp` 作为子进程启动
3. 通过 stdin/stdout 以 JSON-RPC（ndjson）格式通信
4. ACP 事件（工具调用、权限请求、流式文本）从 CrabCode 的 EventBus 实时翻译

**Zed 配置** — 在 `settings.json` 中添加：

```json
{
  "agent": {
    "profiles": {
      "crabcode": {
        "command": "crabcode",
        "args": ["acp"]
      }
    }
  }
}
```

**支持的 ACP 能力：**

| 能力 | 详情 |
|------|------|
| 会话管理 | 新建、加载、列表、分叉、恢复 |
| 提示 | 文本、图片、资源链接、嵌入上下文 |
| MCP 集成 | 从编辑器传入 HTTP/SSE MCP 服务器 |
| 权限请求 | 允许一次 / 始终允许 / 拒绝 |
| 工具更新 | 实时状态（pending → in_progress → completed/failed） |
| 流式输出 | Agent 消息块、思考块 |
| 模型切换 | 通过配置选项在会话中切换模型 |
| 模式切换 | agent 模式 / plan 模式 |

**架构：** ACP 层是薄适配层——它在 ACP JSON-RPC 和 CrabCode Gateway REST API 之间做翻译，核心逻辑零重复。

## 多 API 支持

CrabCode 支持多种 API 后端：

```bash
# Anthropic（默认）
crabcode --provider anthropic --model claude-sonnet-4-20250514

# OpenAI
crabcode --provider openai --model gpt-4o
export OPENAI_API_KEY=YourKey

# OpenAI Codex / Responses API（o-series、codex-mini 等）
crabcode --provider codex --model codex-mini-latest
export OPENAI_API_KEY=YourKey

# 第三方转发（OpenAI 兼容格式）
crabcode --provider router --base-url https://my-router.example.com/v1 --api-format openai

# 第三方转发（Anthropic 兼容格式）
crabcode --provider router --base-url https://my-router.example.com --api-format anthropic

# 第三方转发（Codex/Responses API 兼容格式）
crabcode --provider router --base-url https://my-router.example.com/v1 --api-format codex --model codex-mini-latest

# Ollama（本地）
crabcode --provider ollama --model qwen3:32b
# 或在 settings.json 中配置：
# {"api": {"provider": "ollama", "model": "qwen3:32b"}}

# Google Gemini
crabcode --provider gemini --model gemini-2.5-flash
export GEMINI_API_KEY=YourKey

# Azure OpenAI
crabcode --provider azure --model my-gpt4o-deployment
export AZURE_OPENAI_API_KEY=YourKey
export AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/
```

也可以在 `~/.crabcode/settings.json` 中配置：

```json
{
  "api": {
    "provider": "openai",
    "model": "gpt-4o",
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "OPENAI_API_KEY",
    "thinking_enabled": false,
    "max_tokens": 16384
  },
  "env": {
    "OPENAI_API_KEY": "YourKey"
  }
}
```

`api` 字段说明：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `provider` | API 后端：`anthropic` \| `openai` \| `codex` \| `router` \| `ollama` \| `gemini` \| `azure` | `anthropic` |
| `model` | 模型 ID | — |
| `base_url` | 自定义 API 地址（适用于第三方转发或本地部署） | — |
| `api_key_env` | 存放 API Key 的**环境变量名**（不是 Key 本身） | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `format` | Router 模式下的协议格式：`anthropic` \| `openai` \| `codex` \| `ollama` \| `gemini` \| `azure` | — |
| `thinking_enabled` | 是否启用思考模式（不支持该功能的模型需设为 `false`） | `true` |
| `thinking_budget` | 思考 token 预算 | `10000` |
| `max_tokens` | 最大输出 token 数 | `16384` |
| `timeout` | API 调用超时时间（秒），防止网络卡住时无限等待 | `300` |
| `context_window` | 覆盖模型的上下文窗口大小（token 数）。当自动检测失败或不准确时使用——详见下方[上下文窗口管理](#上下文窗口管理)。 | 自动检测 |

`env` 字段用于直接在配置文件中定义环境变量，启动时会自动注入，无需在 shell 中 `export`。

### 上下文窗口管理

CrabCode 会自动管理上下文窗口，防止因 token 超限导致 `400` 报错。

**上下文窗口大小的解析优先级：**

1. `api` 配置中显式指定的 `context_window` 字段
2. 内置已知模型查找表（例如 `glm-5.1-fp8` → 202752、`gpt-4o` → 128000）
3. 默认兜底值：`200000`

如果你的模型不在内置表中，且未显式配置，则使用 128k 兜底。对于上下文窗口更大的模型（如智谱 GLM），建议手动指定：

```json
{
  "api": {
    "provider": "openai",
    "model": "glm-5.1-fp8",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "context_window": 202752
  }
}
```

**自动压缩（Auto Compact）**

当估算 token 数接近上下文限制时，CrabCode 会自动：

1. 调用 API 将历史消息总结为一条 `[Conversation summary: ...]` 消息
2. 保留最近的若干条消息不变
3. **在同一个 session 内继续执行当前任务**，无需用户干预

也可以使用 `/compact` 命令手动触发压缩，或通过 `settings.json` 中的 `max_context_length` 字段自定义触发阈值。

> **注意**：自动压缩使用的是 `api` 中配置的同一个模型。如果你的模型通过自定义接口访问，请确保 `api.model` 字段填写正确——否则摘要生成请求可能静默失败。

### Logging（运行日志）

可在 `settings.json` 中配置运行日志级别：

```json
{
  "logging": {
    "level": "WARNING",
    "file": ".crabcode/logs/crabcode.log"
  }
}
```

说明：

- `level` 支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`
- 默认日志文件为 `<项目>/.crabcode/logs/crabcode.log`
- 也可通过 `file` 指定自定义日志路径
- CLI 中可通过 `/logs crabcode` 查看核心运行日志

### Hooks（工具调用钩子）

`settings.json` 支持配置 hooks，在以下事件触发 shell 命令：

- `user_prompt_submit`：用户消息提交时触发
- `pre_tool_call`：工具调用前触发（可阻断本次工具调用）
- `post_tool_call`：工具调用后触发

示例：

```json
{
  "hooks": {
    "pre_tool_call": [
      {
        "matcher": "Bash",
        "command": "echo '[pre] tool=$CRABCODE_HOOK_TOOL_NAME'"
      }
    ],
    "post_tool_call": [
      {
        "matcher": "Bash",
        "command": "echo '[post] tool=$CRABCODE_HOOK_TOOL_NAME'"
      }
    ],
    "user_prompt_submit": [
      {
        "command": "echo '[submit] ok'"
      }
    ]
  }
}
```

说明：

- hook 命令退出码非 0 视为失败，`pre_tool_call` 失败会阻断该次工具执行。
- 可通过 `continue_on_error: true`（或 `continueOnError: true`）让失败不阻断流程。
- 支持 Claude 风格的 `PreToolUse` / `PostToolUse` 及嵌套 `hooks: [{\"type\":\"command\", ...}]` 写法。
- 运行时会注入环境变量：`CRABCODE_HOOK_EVENT`、`CRABCODE_HOOK_PAYLOAD`、`CRABCODE_HOOK_TOOL_NAME`、`CRABCODE_HOOK_TOOL_USE_ID`、`CRABCODE_HOOK_AGENT_ID`。

### 多模型配置与切换

在 `settings.json` 中预定义多个命名模型，无需重启即可在会话中随时切换：

```json
{
  "default_model": "fast",
  "models": {
    "fast": {
      "provider": "anthropic",
      "model": "claude-haiku-4-20250514"
    },
    "smart": {
      "provider": "anthropic",
      "model": "claude-opus-4-20250514"
    },
    "local": {
      "provider": "openai",
      "base_url": "http://localhost:11434/v1",
      "model": "qwen3:32b",
      "thinking_enabled": false
    }
  }
}
```

`models` 下的每个条目都是完整的 `ApiConfig`，可以有各自独立的 `provider`、`base_url`、`api_key_env` 等配置。

**启动时选择模型：**

```bash
crabcode --model-profile smart    # 简写：-M smart
```

**在 REPL 中切换：**

```
/model              # 查看当前使用的模型，并列出所有已配置的模型
/model fast         # 切换到 "fast" 模型
/model local        # 切换到 "local"（如 Ollama 本地模型）
```

切换模型不会清空对话历史，可以在同一会话中混用不同模型。

## 内置工具

| 工具 | 类型 | 说明 |
|------|------|------|
| `Bash` | 写 | 执行 shell 命令 |
| `Read` | 读 | 读取文件内容 |
| `Write` | 写 | 创建或覆盖文件 |
| `StrReplace` | 写 | 精确的原地文本替换 |
| `Glob` | 读 | 按 glob 模式查找文件 |
| `Grep` | 读 | 用正则表达式搜索文件内容 |
| `WebSearch` | 读 | 搜索公共互联网中的当前信息 |
| `Browser` | 写 | 在无头 Chromium 中打开网页、交互 DOM、抽取内容和截图 |
| `Lint` | 读 | 运行代码检查器和类型检查器 |
| `Memory` | 写 | 存储和读取持久化笔记 |
| `AskUser` | 读 | 向用户展示选项并等待选择 |

### Lint（代码检查）

`Lint` 工具会根据文件语言自动选择合适的检查器：

| 语言 | 检查器 |
|------|--------|
| Python | `ruff`（风格）、`pylint`（深度分析）、`mypy`（类型检查） |
| JavaScript / TypeScript | `eslint` |
| Go | `golangci-lint` |
| Rust | `cargo clippy` |
| C / C++ | `clang-tidy`、`cppcheck` |
| Java | `checkstyle`、`pmd` |

编辑文件后，agent 会自动调用 `Lint` 验证是否引入了错误。你也可以在对话中直接说"检查这个文件"来触发。

### LSP 集成

CrabCode 集成了 **Language Server Protocol (LSP)** 服务器，为 AI agent 提供实时代码智能——诊断、悬停信息、跳转定义和引用查找。

**工作原理：** 当 agent 写入或编辑文件时，CrabCode 会自动通知对应的 LSP 服务器（如 Python 用 `pyright`，TypeScript 用 `typescript-language-server`）。服务器分析代码后返回诊断信息（错误、警告），这些信息会被注入到工具结果中，LLM 可以立即看到并修复问题——形成闭环反馈。

**默认行为：** LSP **默认开启**。语言服务器按需懒启动——仅在首次访问对应类型文件时才启动。启动失败会被记录，后续不再重试。对于不需要 LSP 的项目，零开销。

**内置服务器**（从 `PATH` 检测）：

| 语言 | 服务器 | 文件扩展名 |
|------|--------|-----------|
| Python | `pyright-langserver` | `.py`、`.pyi`、`.pyw` |
| TypeScript / JS | `typescript-language-server` | `.ts`、`.tsx`、`.js`、`.jsx`、`.mjs`、`.cjs` |
| Go | `gopls` | `.go` |
| Rust | `rust-analyzer` | `.rs` |
| C / C++ | `clangd` | `.c`、`.cpp`、`.h`、`.hpp`、`.cc`、`.cxx` |
| C# | `omnisharp` | `.cs` |
| Java | `jdtls` | `.java` |
| Ruby | `solargraph` | `.rb` |
| PHP | `phpactor` | `.php` |
| Dart | `dart language-server` | `.dart` |
| Lua | `lua-language-server` | `.lua` |
| Kotlin | `kotlin-language-server` | `.kt`、`.kts` |
| Swift | `sourcekit-lsp` | `.swift` |
| Zig | `zls` | `.zig` |
| Elixir | `lexical` | `.ex`、`.exs` |
| Scala | `metals` | `.scala` |

如果服务器未安装，会自动跳过——不会报错，不会延迟。

**在 `settings.json` 中配置：**

```json
{
  "lsp": {
    "python": { "disabled": true },
    "my-custom-server": {
      "command": ["my-lsp", "--stdio"],
      "extensions": [".xyz"],
      "env": {},
      "initialization": {}
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `lsp` | `true` = 启用（默认），`false` = 禁用所有，`{}` = 启用并自定义覆盖 |
| `command` | 启动 LSP 服务器的命令（必须支持 `--stdio` 模式） |
| `extensions` | 该服务器处理的文件扩展名 |
| `env` | 传递给服务器进程的额外环境变量 |
| `initialization` | 传给 LSP `initialize` 请求的 `initializationOptions` |
| `disabled` | 设为 `true` 可禁用特定内置服务器 |

**按 agent 类型控制：** 子 agent 可以通过 `enable_lsp` 关闭 LSP：

```json
{
  "agent": {
    "types": {
      "explore": {
        "allowed_tools": ["Read", "Grep", "Glob"],
        "enable_lsp": false
      }
    }
  }
}
```

`enable_lsp` 默认为 `true`。设为 `false` 时，子 agent 的 `ToolContext.lsp_manager` 为 `None`，不会触发任何 LSP 操作。

### Memory（持久化记忆）

`Memory` 工具让 agent 拥有跨会话持久化的笔记能力。

- **全局记忆** — 存储在 `~/.crabcode/memory.json`
- **项目记忆** — 存储在 `<项目>/.crabcode/memory.json`

记忆内容会在每次对话开始时自动注入。agent 用它来记住用户偏好、常用约定、项目专有知识，无需每次重新说明。

### AskUser（用户选择）

`AskUser` 工具让 agent 在拿不准下一步时，向用户展示选项并等待选择。

**agent 调用该工具时**，终端会弹出交互式选择界面：

```
  你更倾向哪种方案？

    ○ 重构为 class
  ❯ ● 添加错误处理
    ○ 先写测试

  ↑↓ 导航 · enter 选择 · esc 取消
```

- **单选**（默认）：↑↓ 移动光标，Enter 确认
- **多选**（`multiple: true`）：Space 切换勾选，Enter 确认
- **Esc** / **Ctrl+C**：取消选择

**适用场景：**
- 存在多种可行方案，需要用户偏好决定
- 做重大改动前确认方向
- 用户可能掌握 agent 不了解的上下文

**不适用场景：**
- 答案显而易见，或存在明确最优解
- 用户已经告诉你要怎么做
- 只需要简单的是/否确认（agent 直接文字询问即可）

在 **管道模式**（非交互）下，会自动选择第一个选项。

### WebSearch（联网搜索）

`WebSearch` 工具用于搜索公共互联网，并返回包含标题、URL 和摘要的精简结果。

- **默认后端顺序**：如果配置了 `TAVILY_API_KEY`，优先使用 Tavily；否则使用 DuckDuckGo HTML 搜索
- **离线行为**：如果 CrabCode 在会话启动时检测不到外网连通性，则该会话内会禁用 `WebSearch`，并且不会把它暴露给模型
- **权限行为**：`WebSearch` 虽然是只读工具，但每次发起网络请求前仍会请求确认

可通过 `settings.json` 中的 `tool_settings.WebSearch` 配置：

```json
{
  "tool_settings": {
    "WebSearch": {
      "provider": "auto",
      "api_key_env": "TAVILY_API_KEY",
      "timeout_seconds": 8,
      "max_results": 5
    }
  }
}
```

`provider` 支持以下取值：

- `"auto"`：配置了 Tavily 就先用 Tavily，否则用 DuckDuckGo；如果 Tavily 运行时失败，会回退到 DuckDuckGo 一次
- `"tavily"`：要求必须配置 API key，只使用 Tavily
- `"ddg"`：只使用 DuckDuckGo

### Browser（无头浏览器）

`Browser` 工具会启动并复用一个持久的 Chromium 会话，用来打开页面、交互和抽取内容。默认无头运行，但 `create_session` 时可以用 `headless: false` 改成有头。

- 用 `WebSearch` 查找 URL 或公共互联网结果
- 需要真正打开页面、点击、填写、执行页面内 JavaScript、截图时，用 `Browser`
- 启用方式：`pip install crabcode[browser]`，然后执行一次 `playwright install chromium`

支持的 action：

- `create_session`
- `goto`
- `click`
- `fill`
- `press`
- `wait_for`
- `extract`
- `screenshot`
- `evaluate`
- `list_tabs`
- `new_tab`
- `switch_tab`
- `close_tab`
- `close_session`

`create_session` 输入示例：

```json
{ "action": "create_session" }
```

```json
{ "action": "create_session", "headless": false }
```

可通过 `settings.json` 中的 `tool_settings.Browser` 配置：

```json
{
  "tool_settings": {
    "Browser": {
      "enabled": true,
      "default_browser": "chromium",
      "headless": true,
      "default_timeout_seconds": 15,
      "max_sessions": 3,
      "launch_options": {},
      "context_options": {},
      "storage_dir": ".crabcode/browser",
      "block_downloads": true,
      "allowed_domains": [],
      "blocked_domains": []
    }
  }
}
```

默认权限行为：

- `create_session` 和 `goto` 会请求确认
- `fill`、`press`、`evaluate` 会请求确认
- `extract`、`wait_for`、`list_tabs`、`switch_tab`、`close_tab`、`close_session` 默认允许
- `screenshot` 在工作目录内默认允许；写到工作目录外时会请求确认

`tool_settings.Browser.headless` 是默认值；调用 `create_session` 时可以通过输入参数 `headless` 按会话覆盖。

### Diff 显示

通过 `StrReplace` 或 `Write` 修改文件时，终端会展示精简的内联 diff：

```
  ✎ src/auth.py  lines 42–55  (+8 / -3)
```

每次改动都有完整的审计记录。

### 快照与回退（Snapshot & Revert）

CrabCode 会自动追踪会话期间的文件变更，让你可以**撤销**代码改动，回退到之前的检查点。

**工作原理：**

1. 每次创建检查点（`/checkpoint`），CrabCode 会使用 git 内部机制（或非 git 项目的文件拷贝备份）对工作目录做一次快照。
2. 修改文件的工具（`Edit`、`Write`、`Bash`）在每次变更前也会记录单文件快照。
3. 你可以回退到任意检查点，同时恢复对话**和**文件到该时刻的状态。

**命令：**

```
/checkpoint "重构前"          # 创建带文件快照的检查点
/checkpoints                   # 列出检查点（✓ = 含文件快照）
/revert 1                      # 回退文件 + 对话到检查点 #1
/undo                          # 撤销最近一次检查点（回退文件 + 对话）
/rollback 1                    # 仅回滚对话（不还原文件）
```

**`/revert` 与 `/rollback` 的区别：**

| 命令 | 对话 | 文件 |
|------|------|------|
| `/revert` | 回滚 | 还原到快照状态 |
| `/rollback` | 回滚 | 不变 |
| `/undo` | 同 `/revert`（针对最近一次检查点） | 还原到快照状态 |

**快照存储方式：**

- **Git 仓库**（首选）：使用 `git write-tree` + `git update-ref` 在 `refs/crabcode/` 下存储轻量级快照，不污染你的 git 历史。
- **非 Git 目录**：文件被拷贝到 `.crabcode/snapshots/` 进行追踪。

**网关 API：**

| 端点 | 方法 | 说明 |
|------|------|------|
| `/snapshot/checkpoint` | POST | 创建带文件快照的检查点 |
| `/snapshot/list` | GET | 列出会话的检查点 |
| `/snapshot/revert` | POST | 回退文件 + 对话到检查点 |
| `/snapshot/rollback` | POST | 仅回滚对话（不还原文件） |

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示所有可用命令和技能 |
| `/status` | 显示运行状态（模型、上下文占用、压缩次数、agent 摘要） |
| `/logs` | 列出后台日志（例如搜索索引日志） |
| `/logs <名称>` | 查看指定日志尾部 |
| `/logs -f <名称>` | 实时跟随指定日志（`Ctrl+C` 停止） |
| `/logs --clear <名称>` | 清空指定日志文件 |
| `/model` | 查看当前模型与全部命名模型 |
| `/model <名称>` | 切换到 `settings.models` 中的命名模型 |
| `/agents` | 列出当前会话中的托管子 agent |
| `/agent <id>` | 查看单个 agent 的详情（状态、用量、结果、transcript 路径） |
| `/agent-log <id>` | 查看单个 agent 的持久化 transcript |
| `/agent-send <id> <提示词>` | 给已有 agent 继续发送输入 |
| `/plan` | 切换到 plan 模式（只读分析与计划生成） |
| `/agent` | 切换到 agent 模式（正常执行模式） |
| `/plan-status` | 查看当前计划与模式状态 |
| `/wait <id>` | 等待某个 agent 完成并输出摘要 |
| `/cancel-agent <id>` | 取消运行中的 agent |
| `/team list` | 列出活跃的 agent 团队 |
| `/team status <team_id>` | 显示团队状态表 |
| `/team messages <team_id>` | 显示团队消息历史 |
| `/team shutdown <team_id>` | 关闭团队 |
| `/new` | 新建会话（清空内存中的对话历史） |
| `/compact` | 手动压缩对话历史，节省上下文 |
| `/clear` | 清空当前内存对话消息 |
| `/sessions` | 列出当前项目最近保存的会话 |
| `/recent` | 列出所有项目的最近会话 |
| `/search <关键词>` | 按标题或消息内容搜索会话 |
| `/resume <id>` | 通过完整/前缀 id 或序号恢复会话（支持跨项目） |
| `/archive <id>` | 归档会话（从列表中隐藏） |
| `/export [md\|json] [路径]` | 将当前会话导出为 Markdown 或 JSON |
| `/stats` | 显示使用统计（token 消耗、会话数、模型分布） |
| `/checkpoint [标签]` | 在当前对话位置创建检查点（含文件快照） |
| `/checkpoints` | 列出当前会话的检查点（显示文件快照状态） |
| `/rollback <id\|序号>` | 仅回滚对话到指定检查点（不还原文件） |
| `/revert <id\|序号>` | 回退文件 + 对话到指定检查点 |
| `/undo` | 撤销最近一次检查点 — 回退文件 + 对话 |
| `/exit`, `/quit` | 退出 CrabCode |
| `/<skill>` | 按名称调用技能（后面可附加用户输入） |
| `! <shell 命令>` | 在 REPL 里直接执行 shell 命令（不走模型工具循环） |

### 说明

- 需要 `<id>` 的命令一般都支持使用 `/agents` 展示的短前缀。
- `/agent-send` 是否实时回显由 `settings.json` 中 `agent.stream_send_input_output` 控制。
- `Ctrl+C` 会中断当前操作；在短时间内再次按 `Ctrl+C` 会退出。
- `/resume` 支持跨项目会话恢复——如果会话 ID 属于其他项目，会通过元数据数据库自动定位。

### Plan 模式流程

当 plan 模式产出执行计划后，CrabCode 不会立即自动执行。REPL 会先展示完整计划，并询问下一步操作：

- `y` / `yes`：进入 agent 模式并通过 DAG 调度器执行计划
- `m` / `modify`：保持在 plan 模式，继续修改计划
- `n` / `no`：取消并清空当前待执行计划

这样可以把最终执行决策交给用户，同时在确认后仍保留 DAG 并行编排能力。

## 权限控制

每次执行会修改文件或运行 shell 命令的工具前，CrabCode 都会暂停并询问：

```
╭─ ⚠ Bash ──────────────────╮
│ python main.py             │
╰────────────────────────────╯
  Allow Bash? (y)es / (n)o / (a)lways allow:
```

- **y** — 允许本次调用
- **n** — 拒绝；模型会收到"已被拒绝，不要重试"的提示
- **a** — 本次会话内始终允许该工具，不再询问
  对 `Browser` 来说，这里的“始终允许”按 action 生效，例如 `Browser:goto` 与 `Browser:fill` 会分别记忆。

只读工具（`Read`、`Glob`、`Grep`）始终自动允许，不会弹出确认。`WebSearch` 是例外：它虽然只读，但每次网络请求前仍会请求确认。

### 权限规则

可在 `settings.json` 的 `permissions` 下配置精细化规则：

```json
{
  "permissions": {
    "allow": [
      { "tool": "Bash", "command": "git *" }
    ],
    "deny": [
      { "tool": "Bash", "command": "rm *" }
    ],
    "ask": [
      { "tool": "Write" }
    ]
  }
}
```

每条规则通过 `tool` 名称匹配（`*` 通配任意工具），可附加 `command` 或 `path` 过滤条件。

### run_everything 模式

将 `"run_everything"` 设为 `true` 可跳过所有权限询问，所有工具调用自动执行。启用后 CLI 启动时会显示醒目警告。

```json
{
  "permissions": {
    "run_everything": true
  }
}
```

> **请谨慎使用。** 此模式下 CrabCode 将不经确认直接执行 shell 命令和写入文件。

## 配置

配置按以下层级加载（后者覆盖前者）：

1. `~/.crabcode/settings.json`（用户级）
2. `<项目>/.crabcode/settings.json`（项目级）
3. `<项目>/.crabcode/settings.local.json`（本地级，已加入 .gitignore）
4. 命令行参数
5. `~/.crabcode/managed-settings.json`（策略级）

## CLAUDE.md（项目指令文件）

`CLAUDE.md` 是一个 Markdown 文本文件，内容会在每次对话开始时**自动注入**为上下文，无需任何命令。适合用来写项目约定、代码风格要求、常用命令等，让模型在整个项目中始终遵守这些规则。

### 加载位置

以下路径的文件会按顺序加载并合并（后加载的追加在后面）：

| 路径 | 说明 |
|------|------|
| `~/.claude/CLAUDE.md` | 用户全局，Claude Code 兼容 |
| `~/.crabcode/CLAUDE.md` | 用户全局，CrabCode 原生 |
| `<git-root 到 cwd 各级>/CLAUDE.md` | 项目级，从 git 根向下逐级查找 |
| `<git-root 到 cwd 各级>/.claude/CLAUDE.md` | 同上，放在 `.claude/` 子目录中 |

### 示例

```markdown
# 项目约定

- 使用 `ruff` 检查代码风格，提交前必须通过
- 所有新函数必须有 docstring
- 数据库迁移文件放在 `migrations/` 目录，文件名格式：`YYYYMMDD_description.sql`
- 不要修改 `legacy/` 目录下的文件，除非用户明确要求

## 常用命令

- 运行测试：`pytest -x`
- 格式化代码：`ruff format .`
- 启动开发服务器：`make dev`
```

全局 `~/.crabcode/CLAUDE.md` 适合写个人习惯（如偏好的语言、代码风格），项目级 `CLAUDE.md` 适合写团队约定。

## Skills（技能）

Skills 是存储在文件系统中的 Markdown 指令集，可让你将常用工作流封装成可复用的命令。

### 创建 Skill

在 `.crabcode/skills/<技能名>/SKILL.md` 中创建文件（项目级），或放在 `~/.crabcode/skills/<技能名>/SKILL.md`（全局）：

```
.crabcode/
└── skills/
    └── commit/
        └── SKILL.md
```

`SKILL.md` 格式：

```markdown
---
name: commit
description: "按照 conventional commits 规范生成提交信息并提交"
when_to_use: "当用户需要提交代码时"
---

检查当前 git diff，按照 conventional commits 规范拟定提交信息，然后执行 git commit。

用户附加要求：$USER_INPUT
```

frontmatter 字段说明：

| 字段 | 说明 |
|------|------|
| `name` | 技能名称，也是 `/` 命令的调用名（省略时取目录名） |
| `description` | 对模型展示的简短描述，用于判断何时调用该技能 |
| `when_to_use` | 触发条件补充说明 |
| `paths` | 逗号分隔的 glob 列表，限定只在匹配路径时激活（可选） |

正文中可使用 `$USER_INPUT` 占位符，运行时会替换为 `/命令` 后面跟随的内容。

### 调用 Skill

在 REPL 中直接输入 `/<技能名>` 即可触发：

```
❯ /commit 修复登录页面的样式问题
```

`/help` 会自动列出当前所有可用技能。

模型在对话中也可以根据 `description` / `when_to_use` 主动调用相关技能。

### 加载优先级

同名技能按以下顺序加载，后加载的覆盖前面的（优先级从低到高）：

1. `~/.claude/skills/`（兼容 Claude Code 全局技能）
2. `~/.crabcode/skills/`（CrabCode 全局技能）
3. `.claude/skills/`（从项目目录向上逐级查找，兼容 Claude Code）
4. `.crabcode/skills/`（从项目目录向上逐级查找，最高优先级）

### 自动触发

Skills 可以根据用户当前上下文**自动触发**——无需手动输入 `/命令`。当用户消息匹配到技能的模式时，技能指令会以系统提醒的形式注入对话。

在 frontmatter 中添加模式匹配字段：

```markdown
---
name: python-dev
description: "Python 开发工作流"
pathPatterns: "**/*.py, **/*.pyi"
bashPatterns:
  - "pytest .*"
  - "ruff .*"
importPatterns:
  - "from django"
  - "import flask"
chainTo: "python-test"
---

遵循 PEP 8 规范，使用类型注解。
```

模式匹配字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `pathPatterns` | 逗号分隔或 YAML 列表 | 与用户消息中的文件路径匹配的 glob 模式（如 `"**/*.py"`、`"src/**/*.ts"`） |
| `bashPatterns` | 逗号分隔或 YAML 列表 | 与用户消息中的 shell 命令匹配的正则表达式（如 `"pytest .*"`、`"git commit.*"`） |
| `importPatterns` | 逗号分隔或 YAML 列表 | 与用户消息中的 import/require 语句匹配的正则表达式（如 `"from django"`、`"import React"`） |
| `chainTo` | 逗号分隔或 YAML 列表 | 当前技能触发后自动链接的后续技能名（如 `"lint"` 会接着触发 `lint` 技能） |

**工作原理：**

1. 用户发送消息时，CrabCode 从文本中提取文件路径、bash 命令和 import 语句。
2. 依次检查每个技能的 `pathPatterns`、`bashPatterns` 和 `importPatterns` 是否匹配。
3. 匹配的技能会被激活——其内容以 `<system-reminder>` 消息注入对话。
4. 如果被匹配的技能设置了 `chainTo`，链式技能也会一并激活（循环链会被安全截断）。

**链式触发示例：**

```markdown
# .crabcode/skills/python-dev/SKILL.md
---
name: python-dev
pathPatterns: "**/*.py"
chainTo: "python-test"
---
遵循 PEP 8 规范，使用类型注解。

# .crabcode/skills/python-test/SKILL.md
---
name: python-test
bashPatterns: "pytest .*"
chainTo: "python-lint"
---
以 verbose 模式运行 pytest。

# .crabcode/skills/python-lint/SKILL.md
---
name: python-lint
---
运行 ruff check 和 mypy。
```

当用户提到 `src/app.py` 时，三个技能会按顺序全部激活：`python-dev` → `python-test` → `python-lint`。

## Agent Teams（团队协作）

Agent Teams 允许一个 Lead Agent 生成多个 Teammate，通过消息传递和共享任务板进行协作。每个 Teammate 运行在独立的上下文窗口中，并且可以使用不同的模型——实现多模型协作（例如 Claude 写代码、Gemini 做研究、GPT 做审查）。

### 工作原理

1. Lead agent 调用 `TeamCreate` 创建团队，再用 `TeamSpawn` 添加不同角色和模型的 teammate。
2. Teammate 之间通过 `TeamMessage`（点对点）或 `TeamBroadcast`（广播）通信。
3. 共享任务板让 Lead 分配工作，teammate 通过原子性领取（asyncio.Lock）认领任务，并发安全。
4. 消息以 JSONL 格式存储（O(1) 追加写入），注入到接收者 session 并自动唤醒空闲 agent。
5. 背压控制：每个 teammate 有界队列（默认 100 条），溢出时丢弃最旧未读消息并发出警告。

### 内置团队工具

| 工具 | 说明 |
|------|------|
| `TeamCreate` | 创建新团队 |
| `TeamSpawn` | 生成 teammate，指定角色（worker/researcher/reviewer）和可选模型 |
| `TeamMessage` | 向指定 teammate 发送消息 |
| `TeamBroadcast` | 向所有 teammate 广播消息 |
| `TeamStatus` | 查看团队与成员状态 |
| `TeamTaskAdd` | 向共享任务板添加任务 |
| `TeamTaskClaim` | 原子性领取未认领的任务 |
| `TeamTaskComplete` | 标记已领取任务为完成 |
| `TeamShutdown` | 关闭团队并取消所有 teammate |

### 配置

通过 `settings.json` 中的 `team` 字段配置：

```json
{
  "team": {
    "max_teammates": 8,
    "inbox_dir": null,
    "backpressure_queue_size": 100,
    "message_size_limit": 10240
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `max_teammates` | 每个团队最大成员数 | `8` |
| `inbox_dir` | JSONL 收件箱自定义目录（默认：`~/.crabcode/team_inbox/`） | `null` |
| `backpressure_queue_size` | 每个 teammate 的消息队列大小 | `100` |
| `message_size_limit` | 单条消息最大字节数 | `10240`（10KB） |

### 跨团队通信

团队默认隔离。`TeamBridge` 支持受控的跨团队消息传递，策略可配置：

- `allow_all` — 允许所有跨团队消息
- `allow_tagged` — 只转发带特定标签的消息
- `deny` — 禁止跨团队通信（默认）

### 崩溃恢复

服务器重启时，正在运行的 busy teammate 会被强制转为 `ready` 状态（不会自动重启），Lead 会收到通知需手动重新激活。这防止失控 agent 在无人值守时持续消耗 API 额度。

### REPL 命令

| 命令 | 说明 |
|------|------|
| `/team list` | 列出活跃的团队 |
| `/team status <team_id>` | 显示团队状态表 |
| `/team messages <team_id>` | 显示团队消息历史 |
| `/team shutdown <team_id>` | 关闭团队 |

### 多模型混编示例

```json
{
  "models": {
    "coder": { "provider": "anthropic", "model": "claude-opus-4-20250514" },
    "researcher": { "provider": "gemini", "model": "gemini-2.5-pro" },
    "reviewer": { "provider": "openai", "model": "gpt-4o" }
  }
}
```

Lead 生成 teammate 时指定不同的 `model_profile`，每个 teammate 使用对应的 provider 和模型。

## Agent 配置

内置 `Agent` 工具用于生成子 agent 以并行或隔离执行任务。其行为可通过 `settings.json` 中的 `agent` 字段配置：

```json
{
  "agent": {
    "max_turns": 10,
    "timeout": 300,
    "max_output_chars": 12000,
    "stream_send_input_output": false
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `max_turns` | 每次子 agent 调用的最大 agentic 轮次 | `10` |
| `timeout` | 子 agent 的总超时时间（秒） | `300` |
| `max_output_chars` | 单个工具结果超过此字符数时截断 | `12000` |
| `stream_send_input_output` | REPL 执行 `/agent-send` 后是否实时流式回显；设为 `false` 时仅发送输入，不自动回显 | `false` |

浏览器型子 agent 配置示例：

```json
{
  "agent": {
    "types": {
      "browser": {
        "allowed_tools": ["Browser", "WebSearch", "Read", "Glob", "Grep"],
        "prompt": "You are a browser-focused sub-agent. Reuse any existing session_id when possible and avoid creating duplicate browser sessions."
      }
    }
  }
}
```

## 显示配置

工具结果在终端中的显示行数可通过 `settings.json` 中的 `display` 字段配置：

```json
{
  "display": {
    "default_max_lines": 50,
    "max_chars": 50000,
    "tool_max_lines": {
      "Agent": 120,
      "Bash": 60,
      "Read": 80,
      "Grep": 50
    }
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `default_max_lines` | 工具结果的默认最大显示行数 | `50` |
| `max_chars` | 显示内容的字符数安全上限 | `50000` |
| `tool_max_lines` | 按工具名覆盖 `default_max_lines`，仅配置需要调整的工具即可 | 见下表 |

内置工具的默认行数上限：

| 工具 | 默认行数 |
|------|----------|
| `Agent` | `120` |
| `Bash` | `60` |
| `Grep` | `50` |
| `Glob` | `30` |
| `Read` | `80` |
| `Lint` | `60` |
| `WebSearch` | `50` |
| `Browser` | `60` |
| `CodebaseSearch` | `50` |
| 其他 | `50`（即 `default_max_lines`） |

超出行数上限的内容会被截断，并提示剩余行数。超出 `max_chars` 的内容同样会被截断。

子 agent 是并发安全的，当主模型在同一轮中发起多个 `Agent` 调用时，它们会并行执行。每个子 agent 拥有独立的消息历史，并使用与主 agent 相同的工具集。

## 额外工具（Extra Tools）

`extra_tools` 允许你将额外的工具包挂载到 agent，无需修改核心代码。每个条目是指向某个 `Tool` 子类的 Python 导入路径。

```json
{
  "extra_tools": [
    "crabcode_search.CodebaseSearchTool"
  ],
  "tool_settings": {
    "CodebaseSearch": {
      "embedder": "ollama",
      "model": "nomic-embed-text"
    }
  }
}
```

会话启动时，每个额外工具的 `setup()` 方法会被调用，传入包含以下内容的 `ToolContext`：
- `cwd` — 当前工作目录
- `tool_config` — `tool_settings` 中对应该工具的配置项
- `on_event` — 向 CLI 发送实时进度事件的回调

`crabcode-search` 就是通过这个扩展点在启动时触发后台索引建立的。

## crabcode-search（语义代码搜索）

`crabcode-search` 是一个可选包，为 agent 添加语义代码搜索能力。

### 安装

```bash
pip install -e packages/search            # AST 分块 + 全部嵌入后端
```

### 工作原理

1. **分块（Chunking）** — 将源文件按语义单元（函数、类、方法）切分。优先使用 tree-sitter AST 解析，不可用时退回正则边界检测。
2. **嵌入（Embedding）** — 通过可配置的模型将每个 chunk 转为稠密向量。
3. **存储（Storage）** — 向量保存在 `.crabcode/search/` 目录的 USearch 本地索引中。chunks 数不足 10 万时使用精确内积搜索；超过阈值后自动切换为近似 HNSW 遍历。
4. **搜索（Search）** — 查询时将问题嵌入为向量，返回最相近的 chunks，附带文件路径、行号和相关性分数。

### 后台索引

agent 会话启动后，索引立即在后台异步建立，CLI 会显示实时进度条。索引期间 agent 仍可搜索——会返回当前已有的部分结果，并提示必要时使用 `Grep`。

后续启动只对 mtime 变化的文件做增量更新，速度极快。

### 嵌入后端

通过 `settings.json` 中的 `tool_settings.CodebaseSearch` 配置：

| 后端 | `embedder` 值 | 说明 |
|------|--------------|------|
| Ollama（本地） | `"ollama"` | 默认。需要本地运行 Ollama 服务。 |
| OpenAI API | `"openai"` | 需要 `OPENAI_API_KEY`。 |
| Google Gemini API | `"gemini"` | 需要 `GEMINI_API_KEY`。 |
| HuggingFace（本地） | `"huggingface"` | 需要 `pip install sentence-transformers`。 |
| ModelScope（本地） | `"modelscope"` | 需要 `pip install modelscope`。 |

### CPU 线程数限制

本地后端（HuggingFace、ModelScope）默认会占用所有 CPU 核心，导致索引期间整机响应迟缓。可通过 `threads` 选项限制线程数：

```json
{
  "tool_settings": {
    "CodebaseSearch": {
      "embedder": "huggingface",
      "model": "Qwen/Qwen3-Embedding-0.6B",
      "threads": 4
    }
  }
}
```

`threads` 同时限制 PyTorch（`torch.set_num_threads`）以及 `OMP_NUM_THREADS` / `MKL_NUM_THREADS` 环境变量。在大多数笔记本上，设为 2–4 是一个较好的起点。

使用 Ollama 的示例配置：

```json
{
  "extra_tools": ["crabcode_search.CodebaseSearchTool"],
  "tool_settings": {
    "CodebaseSearch": {
      "embedder": "ollama",
      "model": "nomic-embed-text",
      "base_url": "http://localhost:11434"
    }
  }
}
```

使用 Gemini 的示例配置：

```json
{
  "tool_settings": {
    "CodebaseSearch": {
      "embedder": "gemini",
      "model": "text-embedding-004",
      "api_key_env": "GEMINI_API_KEY",
      "dimension": 768
    }
  }
}
```

## Prompt Profile（提示词配置）

系统提示词可通过 `settings.json` 中的 `prompt_profile` 字段完整配置。这让你可以在不修改引擎代码的情况下，替换 agent 的身份定位与行为约束——适合在 `crabcode-core` 之上构建非编程领域的 agent。

每个字段的规则一致：
- **省略 / `null`** → 使用内置默认值
- **`""`** → 禁用该段
- **非空字符串** → 替换为自定义内容

```json
{
  "prompt_profile": {
    "prefix": "You are a customer support agent for Acme Inc.",
    "doing_tasks": "",
    "git_safety": "",
    "actions": "",
    "agent_prompt": "You are a support sub-agent. Answer concisely from the knowledge base.",
    "extra_sections": [
      "# Domain Rules\nAlways check the knowledge base before answering.\nNever share internal pricing."
    ]
  }
}
```

`prompt_profile` 字段说明：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `prefix` | 助手名称及定位的第一句话 | `"You are CrabCode…"` |
| `intro` | 完整介绍段落覆盖 | 内置 |
| `system` | 系统行为规则 | 内置 |
| `doing_tasks` | 任务执行指南（编程专用） | 内置 |
| `actions` | 可逆性 / 影响范围规则 | 内置 |
| `git_safety` | Git 安全协议 | 内置 |
| `using_tools` | 工具使用指南 | 内置 |
| `tone_and_style` | 语气与格式规范 | 内置 |
| `output_efficiency` | 输出简洁度规则 | 内置 |
| `session_guidance` | 会话级提示 | 内置 |
| `agent_prompt` | 子 agent 的 system prompt | 内置 |
| `extra_sections` | 追加在所有内置段之后的自定义段落 | `[]` |

也可以在代码中直接使用 `crabcode_core.prompts.profile` 中的 `PromptProfile` 构建配置：

```python
from crabcode_core.prompts.profile import PromptProfile, minimal_profile
from crabcode_core.events import CoreSession
from crabcode_core.types.config import CrabCodeSettings

profile = minimal_profile()           # 去除编程专用段落
profile.prefix = "You are a data analysis assistant."
profile.extra_sections = ["Always use pandas for data manipulation."]

session = CoreSession(settings=CrabCodeSettings(prompt_profile=profile.model_dump()))
```

`minimal_profile()` 是一个便捷预设，会移除 `doing_tasks`、`actions` 和 `git_safety` 段落——适合作为非编程领域 agent 的起点。

## 项目结构

```
crabcode/
├── packages/
│   ├── core/crabcode_core/     # 核心库
│   │   ├── types/              # Pydantic 类型定义（Message、Tool、Event、Config）
│   │   ├── api/                # API 适配器（Anthropic、OpenAI、Router）
│   │   ├── query/              # Agent 对话循环
│   │   ├── tools/              # 内置工具（Bash、Read、Edit、Write、Grep、Glob、WebSearch、Lint、Memory、AskUser、Team）
│   │   ├── team/               # Agent Teams（数据模型、消息总线、管理器、收件箱、崩溃恢复、跨团队桥接）
│   │   ├── lsp/                # LSP 客户端集成（LSPClient、LSPManager、诊断格式化、服务器注册表）
│   │   ├── skills/             # Skill 加载 + 自动触发匹配（SkillDefinition、load_skills、auto_match）
│   │   ├── prompts/            # 系统提示词构造
│   │   ├── mcp/                # MCP 服务器集成
│   │   ├── compact/            # 对话压缩
│   │   ├── snapshot/           # 文件快照与回退（SnapshotManager, tracker）
│   │   ├── session/            # 会话持久化（JSONL）
│   │   ├── config/             # 多层级配置
│   │   ├── permissions/        # 工具权限管理
│   │   └── events.py           # CoreSession（主要前端接口）
│   ├── cli/crabcode_cli/       # CLI 前端
│   │   ├── app.py              # 入口（typer）
│   │   ├── repl.py             # 交互式 REPL
│   │   ├── pipe.py             # 管道模式
│   │   └── render/             # 终端渲染
│   └── search/crabcode_search/ # 语义搜索（可选）
│       ├── chunker.py          # AST + 正则代码分块
│       ├── embedder.py         # 嵌入后端（Ollama、Gemini、OpenAI、HuggingFace、ModelScope）
│       ├── store.py            # USearch 向量存储（精确搜索 → HNSW，阈值 10 万 chunks）
│       ├── indexer.py          # 文件扫描、变更检测、批量索引
│       └── tool.py             # CodebaseSearchTool（extra_tools 挂载入口）
│   └── gateway/crabcode_gateway/ # 网关服务器（可选）
│       ├── server.py           # GatewayServer 主入口
│       ├── adapter.py          # ProtocolAdapter 抽象（HTTP、gRPC）
│       ├── schemas.py          # Pydantic 请求/响应模型 + CoreEvent 序列化
│       ├── middleware.py       # 认证、日志、CORS、错误处理中间件
│       ├── event_bus.py        # 多订阅者事件总线（SSE + WS）
│       ├── acp/                # ACP（Agent Client Protocol）层
│       │   ├── agent.py        # CrabCodeACPAgent — ACP Agent 实现
│       │   ├── session.py      # ACPSessionManager — ACP 会话状态
│       │   ├── types.py        # ACP 类型定义 + 工具类型映射
│       │   └── transport.py    # stdio 传输层（run_agent 封装）
│       ├── routes/             # FastAPI 路由组（session、agent、config、event、health）
│       └── grpc/               # gRPC 服务 + proto 定义
└── tests/
```
