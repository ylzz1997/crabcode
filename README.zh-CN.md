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

### 持久目标

使用 `/goal` 可让一个可验收的目标在多轮对话、上下文压缩和会话恢复后继续生效。
Goal 与执行计划相互独立：Goal 描述最终结果，`/plan` 描述实现步骤。

```text
/goal 修复认证问题，并通过 pytest tests/auth 验证
/goal                         # 查看当前目标
/goal edit <目标>             # 修改目标
/goal pause                   # 暂停向模型上下文注入目标
/goal resume
/goal complete                # 标记结果已验证
/goal blocked                 # 标记当前无法继续
/goal clear
```

设置或修改 Goal 时可用 `--budget N` 跟踪模型 token 用量；使用
`/goal edit --no-budget <目标>` 可移除已有预算。

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

#### Gateway 安全模式

Gateway 默认不启用认证。可在用户级 `~/.crabcode/settings.json` 中设置
`none`、`password`、`publickey` 或 `mixed`；项目级配置不能覆盖该安全设置。

```json
{
  "gateway": {
    "security": {
      "mode": "mixed",
      "password": "change-me",
      "authorized_keys": "~/.ssh/authorized_keys",
      "token_ttl_seconds": 900
    }
  }
}
```

生产环境可用 `CRABCODE_GATEWAY_PASSWORD` 代替配置中的明文 `password`，也可设置
`password_hash`（`pbkdf2_sha256$...`）。`publickey` 与 `mixed` 默认读取
`~/.ssh/authorized_keys`，支持 Ed25519、RSA-SHA256 和 ECDSA-SHA256。`mixed`
表示密码或公钥任意一种认证成功即可，因此密码侧仍应使用强密码。

```bash
python -c 'from getpass import getpass; from crabcode_gateway.auth import hash_password; print(hash_password(getpass("Password: ")))'
```

密码客户端向 `POST /auth/token` 提交
`{"grant_type":"password","password":"..."}`。公钥客户端先请求
`GET /auth/challenge`，使用私钥签署响应中的 UTF-8 `signing_payload`，再向
`POST /auth/token` 提交 `grant_type=publickey`、公钥注释或 `SHA256:` 指纹形式的
`key_id`、`challenge` 和 Base64 签名。私钥始终留在客户端。两种方式都会返回短期
JWT，后续 HTTP、WebSocket 和 gRPC 请求使用 `Authorization: Bearer <token>`。
challenge 60 秒失效且只能使用一次；密码连续失败 5 次会限流 60 秒。
如显式配置 `jwt_secret`，其长度必须至少为 32 字节；未配置时每次启动自动生成。

旧的 `--password` 和静态 Bearer/Basic 密码仍然兼容，但新客户端应使用
`/auth/token` 获取 JWT。监听非本机地址时还应使用 HTTPS/WSS。

**HTTP API 端点：**

| 端点 | 方法 | 说明 |
| ------ | ------ | ------ |
| `/health` | GET | 健康检查 |
| `/session/new` | POST | 创建会话；除 `cwd` 外可传 `model`、`provider`、`base_url`、`api_format`、`model_profile` 覆盖 |
| `/session/send` | POST | 发送消息（触发 query loop，事件通过 SSE 推送） |
| `/session/interrupt` | POST | 中断当前轮次 |
| `/session/compact` | POST | 手动触发对话压缩 |
| `/session/clear` | POST | 清空活动上下文并持久化清空边界 |
| `/session/messages` | GET | 读取结构化的活动消息投影 |
| `/session/list` | GET | 列出当前项目的持久化会话 |
| `/session/recent` | GET | 跨项目列出最近会话 |
| `/session/search` | POST | 按标题或消息搜索会话 |
| `/session/resolve` | GET | 解析完整 ID、唯一前缀或当前项目序号 |
| `/session/resume` | POST | 按完整 ID、唯一前缀或序号恢复冷会话，并支持同样的 API 覆盖字段 |
| `/session/status` | GET | 读取模型、模式、推理强度和上下文窗口状态 |
| `/session/archive` | POST | 归档已加载或仅持久化的会话 |
| `/session/prune` | POST | 归档过期的非活动会话，并可选清理其文件 |
| `/session/export` | POST | 按 ID、前缀或序号导出 Markdown/JSON |
| `/session/stats` | GET | 读取全局、项目和模型维度用量统计 |
| `/agent/spawn` | POST | 生成子 agent |
| `/agent/{id}` | GET | 获取 agent 状态 |
| `/agent/list` | GET | 列出所有 agent |
| `/agent/{id}/transcript` | GET | 读取 agent transcript/日志尾部 |
| `/agent/{id}/cancel` | POST | 取消 agent |
| `/agent/{id}/input` | POST | 向 agent 发送输入 |
| `/agent/wait` | POST | 等待 agent 完成 |
| `/permission/respond` | POST | 回复权限请求 |
| `/choice/respond` | POST | 回复选择请求 |
| `/config/models` | GET | 列出可用模型 |
| `/config/switch-model` | POST | 切换模型 |
| `/config/switch-mode` | POST | 切换 agent/plan 模式 |
| `/config/reasoning-effort` | POST | 设置推理强度 |
| `/config/ultra-mode` | POST | 切换或设置 Ultra mode |
| `/config/permission-mode` | POST | 设置客户端工具权限覆盖 |
| `/config/goal` | GET/POST | 查看或管理会话 Goal |
| `/config/plan-status` | GET | 读取当前计划执行状态 |
| `/tools` | GET | 列出可用工具（含 MCP） |
| `/skills` | GET | 列出可用 Skills |
| `/skills/expand` | POST | 使用用户输入展开 Skill 调用 |
| `/context` | POST | 推送工作区上下文（活动文件、选中内容、光标位置） |
| `/context/{session_id}` | GET | 读取最新客户端工作区上下文 |
| `/logs` | GET | 列出、读取尾部或清空后台日志 |
| `/logs/follow` | GET | 通过 SSE 实时跟随后台日志 |
| `/tasks`, `/tasks/{id}` | GET | 列出后台任务或读取单个任务 |
| `/tasks/{id}/output` | GET | 读取持久化的后台任务输出 |
| `/tasks/stop` | POST | 停止后台任务 |
| `/schedule`, `/schedule/{id}` | GET | 列出定时任务，或按 ID/唯一前缀读取单个任务 |
| `/schedule/{id}/runs` | GET | 读取定时任务的持久化执行历史 |
| `/schedule/create` | POST | 创建 cron、固定间隔或单次任务；支持启用状态、下次执行时间覆盖、复用执行会话、标签和扩展元数据 |
| `/schedule/pause`, `/schedule/resume` | POST | 暂停或恢复定时任务 |
| `/schedule/trigger`, `/schedule/cancel` | POST | 立即执行或永久删除定时任务 |
| `/peer/list`, `/peer/send` | GET/POST | 列出可通信会话或发送跨会话消息 |
| `/team/list`, `/team/{id}/*` | GET | 读取 Team、状态、消息和任务板 |
| `/team/create`, `/team/spawn`, `/team/shutdown` | POST | 管理 Team 生命周期 |
| `/team/message`, `/team/broadcast` | POST | 发送 teammate 消息 |
| `/team/remove`, `/team/messages/read` | POST | 移除 teammate 或将其消息标记为已读 |
| `/team/task/*` | POST | 添加、认领、完成或标记失败 Team 任务 |
| `/team/bridge`, `/team/{a}/bridge/{b}` | POST/GET | 注册或查看跨 Team Bridge 策略 |
| `/team/cross-message` | POST | 发送经过策略检查的跨 Team 消息 |
| `/snapshot/checkpoint` | POST | 创建带文件快照的检查点 |
| `/snapshot/list` | GET | 列出会话的检查点 |
| `/snapshot/revert` | POST | 回退文件 + 对话到检查点 |
| `/snapshot/rollback` | POST | 仅回滚对话（不还原文件） |
| `/snapshot/undo` | POST | 回退最近的检查点 |
| `/event` | GET (SSE) | 实时事件流（10 秒心跳） |
| `/ws` | WebSocket | 双向通信（VSCode 扩展首选） |

**WebSocket `/ws`** 覆盖完整交互命令链路：会话新建/恢复（`new_session`、`resume_session`）、消息与 steering、中断、权限/选择回复、工作区上下文、模型/模式/权限切换和计划操作。`new_session` 与 `resume_session` 接受和 HTTP 生命周期端点相同的五个 API 覆盖字段；目标会话已经加载时会拒绝覆盖，避免一个客户端静默替换另一个客户端正在使用的 runtime。一个连接会持续订阅它显式选择过的所有 session，因此界面切换后仍能收到旧 session 的后台事件，同时不会暴露未选择的其他 session。前台与计划命令携带 `operation_id`；steering 和 interrupt 应回传该 ID，避免误投到更新的轮次。命令校验失败使用有类型、非终止性的 error envelope；每个已受理 operation 最终只以一个 `turn_complete` 结束。完整客户端还必须消费结构化会话历史，以及带 session 标识的 `agent_state`、`agent_output`、`team_message`、`team_state`、`task_update`、`schedule_run`、`compact`、权限/选择回复、文件变更、snapshot 和 revert 事件。Schedule 的增删改查使用上面的 HTTP 端点，客户端无需轮询执行结果。

直接执行 `! <cmd>` 被刻意限定为可信客户端能力：CLI 在本地进程中执行，VSCode 扩展发送到本地集成终端。Gateway 不提供该端点，因为这会形成绕过权限系统的远程 Shell。

**gRPC** 在启用 `--grpc-port` 后可用，提供流式对话/事件，以及 `packages/gateway/crabcode_gateway/grpc/proto/crabcode.proto` 中定义的 agent、权限、选择、模型和模式 RPC。

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
| ------ | ------ |
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
    "http_headers": {
      "X-Workspace": "crabcode"
    },
    "thinking_enabled": false,
    "pass_reasoning_content": false,
    "max_tokens": 16384
  },
  "env": {
    "OPENAI_API_KEY": "YourKey"
  }
}
```

`api` 字段说明：

| 字段 | 说明 | 默认值 |
| ------ | ------ | -------- |
| `provider` | API 后端：`anthropic` \| `openai` \| `codex` \| `router` \| `ollama` \| `gemini` \| `azure` | `anthropic` |
| `model` | 模型 ID | — |
| `base_url` | 自定义 API 地址（适用于第三方转发或本地部署） | — |
| `api_key_env` | 存放 API Key 的**环境变量名**（不是 Key 本身） | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `codex_auth_path` | Codex CLI OAuth 登录文件路径。仅在 `codex` provider 且未配置 API Key 与 `base_url` 时使用。 | `$CODEX_HOME/auth.json` 或 `~/.codex/auth.json` |
| `http_headers` | 为该配置的每次 API 请求附加额外 HTTP Header | `{}` |
| `anthropic_stream_transport` | Anthropic 流式传输实现：`auto` \| `sdk` \| `httpx`。`auto` 下官方 Anthropic 使用 SDK，自定义 `base_url` 使用直接 SSE，适合会拒绝 SDK stream helper 头的转发服务。 | `auto` |
| `format` | Router 模式下的协议格式：`anthropic` \| `openai` \| `codex` \| `ollama` \| `gemini` \| `azure` | — |
| `thinking_enabled` | 是否启用思考模式（不支持该功能的模型需设为 `false`） | `true` |
| `thinking_budget` | 思考 token 预算 | `10000` |
| `pass_reasoning_content` | 在 OpenAI 兼容 Chat 请求中回传已保存的 assistant `reasoning_content`。DeepSeek V4 思考模式配合工具调用时需要开启。 | `false` |
| `reasoning_effort` | 模型推理强度。OpenAI Responses/Codex 通过 `reasoning.effort` 发送；Anthropic 将其支持的值通过 `output_config.effort` 发送。显式配置时优先于 Codex 的 `thinking_budget` 映射。可选：`none` \| `minimal` \| `low` \| `medium` \| `high` \| `xhigh` \| `max`（具体可用值取决于模型和 provider）。 | — |
| `max_tokens` | 最大输出 token 数 | `16384` |
| `timeout` | API 调用超时时间（秒），防止网络卡住时无限等待 | `300` |
| `max_retries` | 瞬时 API、限流、超时及服务端故障时的自动重连次数。仅在尚未收到正文或工具调用时重放请求。 | `5` |
| `context_window` | 覆盖模型的上下文窗口大小（token 数）。当自动检测失败或不准确时使用——详见下方[上下文窗口管理](#上下文窗口管理)。 | 自动检测 |
| `prompt_cache_key` | OpenAI Responses/Codex 请求的 Prompt Cache 路由 key；未配置时默认使用 `http_headers.session_id` | — |
| `prompt_cache_retention` | OpenAI Responses/Codex Prompt Cache 保留策略：`in_memory` \| `24h` | — |
| `extra_body` | 追加到请求 JSON body 的 provider 专用字段 | `{}` |

`env` 字段用于直接在配置文件中定义环境变量，启动时会自动注入，无需在 shell 中 `export`。

Codex OAuth 用法：将 `provider` 设为 `codex`，并省略 `base_url` 和 API Key 配置。CrabCode 会默认读取 `$CODEX_HOME/auth.json` 或 `~/.codex/auth.json`；需要指定其它位置时配置 `codex_auth_path`：

```json
{
  "api": {
    "provider": "codex",
    "model": "gpt-5.5",
    "codex_auth_path": "/Users/you/.codex/auth.json"
  }
}
```

DeepSeek V4 思考模式配合工具调用时，需要开启 reasoning 回传：

```json
{
  "models": {
    "deepseek": {
      "provider": "openai",
      "model": "deepseek-v4-flash",
      "base_url": "https://api.deepseek.com",
      "api_key_env": "DEEPSEEK_API_KEY",
      "pass_reasoning_content": true,
      "extra_body": {
        "thinking": {
          "type": "enabled"
        }
      }
    }
  }
}
```

### 上下文窗口管理

CrabCode 会自动管理上下文窗口，防止因 token 超限导致 `400` 报错。

**上下文窗口大小的解析优先级：**

1. `api` 配置中显式指定的 `context_window` 字段
2. 内置已知模型查找表（例如 `glm-5.1-fp8` → 202752、`gpt-4o` → 128000）
3. 默认兜底值：`200000`

如果模型不在内置表中且未显式配置，则使用 200k 兜底；当服务商暴露的限制不同时，请手动指定 `context_window`：

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

CrabCode 会估算完整请求（system prompt、消息、工具调用/结果、工具 schema 和图片载荷）。默认会在输入超过 `context_window - max(max_tokens, 20000)` 前触发压缩：

1. 分块处理全部较旧历史，生成结构化、可恢复的 checkpoint
2. 最多保留最近两个完整用户轮次，保证工具调用与对应结果不会被拆开
3. 将压缩后的有效上下文原子追加到 JSONL；重启后从该边界恢复，导出时仍保留完整审计历史
4. **在同一个 session 内继续执行当前任务**，无需用户干预

可使用 `/compact [可选指令]` 手动触发。`auto_compact_enabled` 控制自动压缩；`max_context_length` 可设置更早的输入触发阈值，但不能突破服务商安全上限。

> **注意**：压缩使用当前配置的 API 模型。若 checkpoint 生成失败，CrabCode 会原样保留旧对话，不会用有损兜底摘要替换历史。只有在响应尚未产生任何输出时才会针对服务商超限做恢复重试，避免部分响应后重复执行工具。

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
- `pre_compact`：手动、自动或超限压缩前触发（可阻断本次压缩）
- `post_compact`：checkpoint 成功生成后触发

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
    ],
    "pre_compact": [
      {
        "matcher": "manual",
        "command": "echo '[compact] trigger 位于 $CRABCODE_HOOK_PAYLOAD'"
      }
    ]
  }
}
```

说明：

- hook 命令退出码非 0 视为失败，`pre_tool_call` 失败会阻断该次工具执行。
- 可通过 `continue_on_error: true`（或 `continueOnError: true`）让失败不阻断流程。
- 支持 Claude 风格的 `PreToolUse` / `PostToolUse` / `PreCompact` / `PostCompact` 及嵌套 `hooks: [{\"type\":\"command\", ...}]` 写法。
- 压缩 hook 的 matcher 会收到 `auto`、`manual` 或 `overflow`。
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

`models` 下的每个条目都是完整的 `ApiConfig`，可以有各自独立的 `provider`、`base_url`、`api_key_env`、`codex_auth_path` 等配置。

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
| ------ | ------ | ------ |
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
| ------ | -------- |
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
| ------ | -------- | ----------- |
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
| ------ | ------ |
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

- **全局记忆** — 存储在 `~/.crabcode/memories.json`
- **项目记忆** — 存储在 `<项目>/.crabcode/memories.json`

每次对话只会自动注入一份有数量上限的记忆目录，其中包含记忆 ID 和标题摘要。完整正文不会直接占用上下文；agent 会通过 `Memory` 的 `search` 和 `read` 操作按需检索。该工具同时支持 `create`、`update`、`delete` 和只显示标题的 `list` 操作。

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
| ------ | ------ | ------ |
| `/revert` | 回滚 | 还原到快照状态 |
| `/rollback` | 回滚 | 不变 |
| `/undo` | 同 `/revert`（针对最近一次检查点） | 还原到快照状态 |

**快照存储方式：**

- **Git 仓库**（首选）：使用 `git write-tree` + `git update-ref` 在 `refs/crabcode/` 下存储轻量级快照，不污染你的 git 历史。
- **非 Git 目录**：文件被拷贝到 `.crabcode/snapshots/` 进行追踪。

**网关 API：**

| 端点 | 方法 | 说明 |
| ------ | ------ | ------ |
| `/snapshot/checkpoint` | POST | 创建带文件快照的检查点 |
| `/snapshot/list` | GET | 列出会话的检查点 |
| `/snapshot/revert` | POST | 回退文件 + 对话到检查点 |
| `/snapshot/rollback` | POST | 仅回滚对话（不还原文件） |

## REPL 命令

| 命令 | 说明 |
| ------ | ------ |
| `/help` | 显示所有可用命令和技能 |
| `/status` | 显示运行状态（模型、effort、ultra mode、上下文占用、压缩次数、agent 摘要） |
| `/logs` | 列出后台日志（例如搜索索引日志） |
| `/logs <名称>` | 查看指定日志尾部 |
| `/logs -f <名称>` | 实时跟随指定日志（`Ctrl+C` 停止） |
| `/logs --clear <名称>` | 清空指定日志文件 |
| `/model` | 查看当前模型与全部命名模型 |
| `/model <名称>` | 切换到 `settings.models` 中的命名模型 |
| `/effort` | 查看当前 reasoning effort |
| `/effort <none\|minimal\|low\|medium\|high\|xhigh\|max>` | 设置当前运行会话后续请求的 reasoning effort |
| `/ultra` | 切换后续请求的 ultra mode |
| `/ultra <true\|false>` | 显式开启或关闭 ultra mode |
| `/agents` | 列出当前会话中的托管子 agent |
| `/peers` | 列出可进行跨 session 通信的其他活跃 session |
| `/tasks` | 列出后台 agent 与命令/WebSocket monitor |
| `/tasks stop <id>` | 停止运行中的后台任务 |
| `/agent <id>` | 查看单个 agent 的详情（状态、用量、结果、transcript 路径） |
| `/agent-log <id>` | 查看单个 agent 的持久化 transcript |
| `/agent-send <id> <提示词>` | 给已有 agent 继续发送输入 |
| `/plan` | 切换到 plan 模式（只读分析与计划生成） |
| `/agent` | 切换到 agent 模式（正常执行模式） |
| `/plan-status` | 查看当前计划与模式状态 |
| `/wait <id>` | 等待某个 agent 完成并输出摘要 |
| `/cancel-agent <id>` | 取消运行中的 agent |
| `/team list` | 列出活跃的 agent 团队 |
| `/team create <name> [max]` | 创建团队 |
| `/team status <team_id>` | 显示团队状态表 |
| `/team messages <team_id>` | 显示团队消息历史 |
| `/team tasks <team_id>` | 显示共享任务板 |
| `/team spawn <team_id> [options] <prompt>` | 添加 teammate |
| `/team message <team_id> <agent_id> <text>` | 向 teammate 发消息 |
| `/team broadcast <team_id> <text>` | 向所有 teammate 广播 |
| `/team task-add <team_id> <description>` | 添加任务 |
| `/team task-claim <team_id> <task_id> [agent_id]` | 认领任务 |
| `/team task-complete <team_id> <task_id> [result]` | 完成任务 |
| `/team shutdown <team_id>` | 关闭团队 |
| `/schedule list` | 列出定时任务 |
| `/schedule show <job_id>` | 查看定时任务详情 |
| `/schedule runs <job_id>` | 查看定时任务执行历史 |
| `/schedule create <name> <type> <schedule> <prompt>` | 创建 cron/interval/once 定时任务 |
| `/schedule pause\|resume\|run\|cancel <job_id>` | 管理定时任务生命周期 |
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

### 默认权限模式

`permissions.default_mode` 控制显式 `allow`、`deny`、`ask` 规则之后的默认行为。支持的值：

- `"ask"` — 默认值；需要权限的工具调用会询问用户。
- `"run_everything"` — 跳过权限询问，自动允许工具调用。
- `"aiReview"` / `"ai_review"` — 交给 reviewer 模型判断。

旧的布尔开关 `permissions.run_everything: true` 仍然兼容，等价于 `"default_mode": "run_everything"`。

### AI 审查模式

AI 审查模式会让一个 reviewer 模型判断待执行的工具调用应该直接允许、询问用户，还是拒绝。显式 `allow`、`deny`、`ask` 规则仍然优先。如果 reviewer 失败、超时、返回非法 JSON，或返回了配置不允许的决策，CrabCode 默认回退到 `ask`。

```json
{
  "permissions": {
    "default_mode": "aiReview",
    "ai_review": {
      "model": "review-model",
      "decisions": ["allow", "ask"],
      "fallback": "ask",
      "timeout": 30
    }
  },
  "models": {
    "review-model": {
      "provider": "openai",
      "model": "your-model-name",
      "base_url": "https://your-api-endpoint/v1",
      "api_key_env": "YOUR_API_KEY_ENV",
      "thinking_enabled": false,
      "max_tokens": 8000
    }
  }
}
```

`ai_review.model` 填的是 `models` 里的配置名，例如上面的 `"review-model"`；省略时会使用当前 agent 正在使用的模型。

- 默认 `decisions` 为 `["allow", "ask"]`，因此 reviewer 不会直接拒绝工具调用，除非你显式开启。
- 可将 `decisions` 设为 `["allow", "ask", "deny"]` 启用更严格审查，或设为 `["allow", "deny"]` 用于非交互式 allow/deny 行为。

### run_everything 模式

将 `"default_mode"` 设为 `"run_everything"` 可跳过所有权限询问，所有工具调用自动执行。启用后 CLI 启动时会显示醒目警告。

```json
{
  "permissions": {
    "default_mode": "run_everything"
  }
}
```

旧写法 `"run_everything": true` 仍然兼容。

> **请谨慎使用。** 此模式下 CrabCode 将不经确认直接执行 shell 命令和写入文件。

VS Code 扩展默认使用“跟随配置”（`crabcode.permissionMode: "default"`），因此会遵循
`settings.json` 中的 `permissions.default_mode`。如果在扩展底部菜单明确选择了其他模式，
该选择会覆盖当前网关会话的文件配置。

## 配置

配置按以下层级加载（后者覆盖前者）：

1. `~/.crabcode/settings.json`（用户级）
2. `<项目>/.crabcode/settings.json`（项目级）
3. `<项目>/.crabcode/settings.local.json`（本地级，已加入 .gitignore）
4. 命令行参数
5. `~/.crabcode/managed-settings.json`（策略级）

### Ultra 模式

将顶层 `ultra_mode` 字段设为 `true` 后，主模型会主动把非简单任务拆分给大量子 agent，并行执行彼此独立的工作：

```json
{
  "ultra_mode": true
}
```

`ultra_mode` 默认为 `false`。该配置会改变模型的任务委派指引；实际并发量仍受 `agent.max_concurrency` 和 `agent.max_active_agents_per_run` 限制。

### 工具调用超时

默认情况下，CrabCode 不会对工具调用施加全局超时。可以通过顶层 `tool_call_timeout` 字段限制每次工具执行的最长时间：

```json
{
  "tool_call_timeout": 300
}
```

省略 `tool_call_timeout` 或设为 `null` 时，工具调用不会因为全局配置而超时。工具自身的超时配置（例如 `Bash.timeout` 或 `agent.timeout`）仍会独立生效，并且可能更短。

## CLAUDE.md（项目指令文件）

`CLAUDE.md` 是一个 Markdown 文本文件，内容会在每次对话开始时**自动注入**为上下文，无需任何命令。适合用来写项目约定、代码风格要求、常用命令等，让模型在整个项目中始终遵守这些规则。

### 加载位置

以下路径的文件会按顺序加载并合并（后加载的追加在后面）：

| 路径 | 说明 |
| ------ | ------ |
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
| ------ | ------ |
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
| ------ | ------ | ------ |
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

## Session 间通信

同一台 macOS/Linux 机器上的独立 CrabCode session 可以互相发现和发送消息，
但不会共享完整对话历史。每个活跃 session 会在 `~/.crabcode/peers/` 发布最小
注册信息，并通过每个 session 独立的随机令牌认证连接到仅当前系统用户可访问的
Unix domain socket。模型通过 `ListAgents` 发现其他 session，再通过 `SendMessage`
按名称或 session ID 投递纯文本。

- 接收方正在执行 turn 时，消息会在当前工具批次完成后、下一次模型请求前注入；
  空闲时消息会触发一个 synthetic turn。
- 接收方能看到发送方名称、session ID 和工作目录。Peer 消息不代表用户授权，
  也不能绕过本 session 的权限策略。
- `SendMessage` 遵循普通写工具的权限确认；注册文件和 socket 仅当前系统用户可访问。
- 默认 `auto` 入站策略直接接收权限等级相同的 session；权限等级不同时先 `hold`，
  由用户批准。使用 `hold` 可审批所有消息，可信的无人值守 worker 可显式配为
  `accept`，完全关闭入站则使用 `refuse`。

```json
{
  "cross_session": {
    "enabled": true,
    "name": "api-worker",
    "inbound": "auto",
    "queue_size": 50,
    "max_message_size_bytes": 10000
  }
}
```

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
| ------ | ------ |
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
| ------ | ------ | -------- |
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

恢复辅助函数只能在已有的 `TeamManager` 进程内，将过期的 `busy`/`cancelling` teammate 状态规范化为 `ready`（不会自动重启任务）。它目前没有接入网关启动流程，团队成员关系和状态也只保存在内存中；新进程无法从上一个进程发现并恢复团队。调用方需要自行重建运行时并显式调用恢复逻辑。

### REPL 命令

| 命令 | 说明 |
| ------ | ------ |
| `/team list` | 列出活跃的团队 |
| `/team create <name> [max]` | 创建团队 |
| `/team status <team_id>` | 显示团队状态表 |
| `/team messages <team_id>` | 显示团队消息历史 |
| `/team tasks <team_id>` | 显示共享任务板 |
| `/team spawn <team_id> [options] <prompt>` | 添加 teammate |
| `/team message <team_id> <agent_id> <text>` | 向 teammate 发消息 |
| `/team broadcast <team_id> <text>` | 向所有 teammate 广播 |
| `/team task-add <team_id> <description>` | 添加任务 |
| `/team task-claim <team_id> <task_id> [agent_id]` | 认领任务 |
| `/team task-complete <team_id> <task_id> [result]` | 完成任务 |
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
    "stream_send_input_output": false,
    "max_concurrency": 4,
    "max_depth": 2,
    "max_active_agents_per_run": 16,
    "types": {}
  }
}
```

| 字段 | 说明 | 默认值 |
| ------ | ------ | -------- |
| `max_turns` | 每次子 agent 调用的最大 agentic 轮次 | `10` |
| `timeout` | 子 agent 的总超时时间（秒） | `300` |
| `max_output_chars` | 单个工具结果超过此字符数时截断 | `12000` |
| `stream_send_input_output` | REPL 执行 `/agent-send` 后是否实时流式回显；设为 `false` 时仅发送输入，不自动回显 | `false` |
| `max_concurrency` | 可同时执行的子 agent 数量上限；超出后继续排队 | `4` |
| `max_depth` | 最大嵌套深度。主 agent 深度为 0，因此 `2` 允许深度为 1 的子 agent 和深度为 2 的孙级 agent | `2` |
| `max_active_agents_per_run` | 当前 session 中未完成 managed agent 的数量上限，包括排队中和运行中的 agent | `16` |
| `types` | 按 `subagent_type` 覆盖配置，字段见下表 | `{}` |

`types` 中的每个条目支持以下字段：

```json
{
  "agent": {
    "types": {
      "explore": {
        "model_profile": "fast-model",
        "allowed_tools": ["Read", "Grep", "Glob"],
        "prompt": "探索代码库并简洁报告依据。",
        "enable_lsp": false
      }
    }
  }
}
```

| 字段 | 说明 | 默认值 |
| ------ | ------ | -------- |
| `model_profile` | 该 agent 类型使用的 `models` 命名配置；显式传入的 `Agent.model_profile` 优先 | 当前模型 |
| `allowed_tools` | 工具名称白名单。普通 agent 的空列表表示不限制；`explore` 的空列表默认只允许只读工具 | `[]` |
| `prompt` | 该 agent 类型使用的系统提示词覆盖 | 当前提示词配置 |
| `enable_lsp` | 是否向该 agent 类型提供已配置的 LSP manager | `true` |

与 Claude Code 一样，`Agent` 默认在后台运行并立即返回 `async_launched`；只有当前轮次
必须同步取得结果时才设置 `run_in_background: false`。子 agent 完成、失败或被停止后，
CrabCode 会注入 `<task-notification>`，自动
恢复其直接父 agent；顶层任务则恢复主 agent。嵌套 callback 会沿父链逐级路由。
callback 的投递记录可跨会话恢复和对话压缩保存，并暴露 `pending`、`injected`、
`delivered` 三种状态。进程终止后遗留的 queued/running agent 会在恢复时被标记为
`stopped`，不会显示成实际已不存在的运行中任务。

“子 agent”只描述 managed agent 的父子关系，并不是另一种运行时类型。`TeamSpawn` 创建的
仍是同一种 managed agent，只是在此基础上附加团队成员身份、消息通信和团队生命周期状态。

REPL 会在空闲时显示自动续跑过程。Gateway 客户端可通过 `/event` 或 `/ws` 接收相同的
后台事件；调用 `POST /agent/spawn` 时传入 `"callback": true` 即可开启。Agent 状态响应
会返回 callback 状态、epoch、关联消息 ID、完成时间和 transcript 路径，便于监控与恢复。

独立的 `Monitor` 工具也遵循 Claude Code 的事件驱动 watch 模式。传入 `command` 或
`ws` 之一以及简短 `description`；合并后的 stdout/stderr 每一行或每条 WebSocket 消息
都会以 `<monitor-event>` 注入并自动唤醒主会话。默认期限为 300,000 ms（最大
3,600,000 ms），设置 `persistent: true` 后会一直运行到 `TaskStop` 或会话结束。完整
输出保存在当前会话的 task 文件中。`TaskList` 同时列出 monitor 和 agent，`TaskStop`
可按 ID 停止任一类型。命令 monitor 复用 Bash 权限规则；WebSocket monitor 单独审批，
并拒绝带凭据、私网、本机或保留地址的目标。

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
| ------ | ------ | -------- |
| `default_max_lines` | 工具结果的默认最大显示行数 | `50` |
| `max_chars` | 显示内容的字符数安全上限 | `50000` |
| `tool_max_lines` | 按工具名覆盖 `default_max_lines`，仅配置需要调整的工具即可 | 见下表 |

内置工具的默认行数上限：

| 工具 | 默认行数 |
| ------ | ---------- |
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

## crabcode-debugger（DAP 与进程级调试）

`crabcode-debugger` 是一个可选包，让 agent 可以调用 Debug Adapter Protocol
调试器和进程级诊断能力。默认不启用。

### 安装与启用

```json
{
  "extra_tools": [
    "crabcode_debugger.DebuggerTool",
    "crabcode_debugger.ProcessDebuggerTool"
  ],
  "tool_settings": {
    "Debugger": {
      "allow_evaluate": false,
      "default_timeout_seconds": 30
    },
    "ProcessDebugger": {
      "default_timeout_seconds": 15
    }
  }
}
```

```bash
pip install -e packages/debugger
```

该包默认依赖本机已安装的官方或官方维护 debug adapter。可以先调用 `Debugger`
的 `adapters` 动作和 `ProcessDebugger` 的 `capabilities` 动作查看当前环境可用能力。

### `Debugger` 工具

`Debugger` 通过 Debug Adapter Protocol（DAP）做源码级调试。安装对应 adapter 后，
支持 C/C++、Rust、Python、Go、Java、TypeScript、JavaScript。

| 能力 | Actions |
| --- | --- |
| Adapter 发现 | `adapters` |
| 会话生命周期 | `start`, `attach`, `stop`, `sessions`, `events` |
| 断点与运行控制 | `set_breakpoints`, `configuration_done`, `continue`, `pause`, `step_over`, `step_in`, `step_out` |
| 运行态检查 | `threads`, `stack`, `scopes`, `variables`, `evaluate` |

内置 adapter 发现优先级：

| 语言 | 优先 adapter |
| --- | --- |
| Python | `debugpy` |
| Go | `dlv dap` |
| C/C++ | `lldb-dap`，其次 GDB DAP |
| Rust | `lldb-dap` |
| Java | `vscode-java-debug` |
| TypeScript/JavaScript | `vscode-js-debug` |

自定义 adapter 命令可以放在 `tool_settings.Debugger.adapters`。
`launch_config` 和 `attach_config` 会透传给选中的 adapter，用于语言特定选项。

### `ProcessDebugger` 工具

`ProcessDebugger` 面向本机授权进程，提供 best-effort 的进程诊断和进程内存操作。

| 能力 | Actions |
| --- | --- |
| 能力与进程清单 | `capabilities`, `list_processes`, `inspect_process` |
| 进程调试与控制 | `attach_debugger`, `detach`, `terminate`, `kill` |
| 进程诊断 | `sample_stack`, `dump_core`, `memory_maps`, `trace_syscalls` |
| 内存区域与值 | `memory_regions`, `memory_read`, `memory_search`, `memory_refine`, `memory_write` |
| 持续值保持 | `memory_freeze`, `memory_unfreeze`, `memory_freezes` |
| 特征与指针定位 | `aob_scan`, `pointer_scan`, `pointer_resolve` |
| 字节级代码 patch | `code_read`, `code_patch`, `code_restore`, `code_patches` |

当前平台支持：

| 平台 | 进程诊断 | 直接内存操作 |
| --- | --- | --- |
| Linux | `/proc`，安装后可用 `gdb`/`gcore`、`strace` | 通过 `/proc/<pid>/maps` 和 `/proc/<pid>/mem` 实现 |
| macOS | 可用时使用 `sample`、`vmmap`、`lldb` | 通过 Mach `task_for_pid`、`mach_vm_region_recurse`、`mach_vm_read_overwrite`、`mach_vm_write`、`mach_vm_protect` 实现 |
| Windows | PowerShell 进程 API，可用时使用 `cdb`/ProcDump | 通过 `VirtualQueryEx`、`ReadProcessMemory`、`WriteProcessMemory`、`VirtualProtectEx` 实现 |

内存与 patch 细节：

- `memory_search` 支持数值、字符串和原始 bytes。
- `memory_refine` 可用 `equals`、`changed`、`unchanged`、`increased`、`decreased` 缩小上次搜索。
- `aob_scan` 支持带 wildcard 的字节特征，例如 `48 8B ?? 89`。
- `pointer_scan` 会在受限 depth、offset、结果数和扫描大小内查找进程指针链。
- `pointer_resolve` 可解析绝对 base address，或 `module_path` + `module_offset` + offsets。
- 基于模块路径的指针解析取决于当前 backend 是否能为内存区域提供模块路径。
- `code_patch` 只写入明确 bytes，会保存原始 bytes，可在写入前校验 `expected_hex`，
  并在 backend 支持时使用页面保护和指令缓存刷新原语。
- Patch 十六进制输入会被严格校验；活动中的 patch ID 必须唯一，并拒绝相互重叠的
  活动 patch，以确保原始 bytes 仍可恢复。
- `code_restore` 可按 `patch_id` 恢复，也可用 `all=true` 恢复全部。
- 直接内存访问受操作系统进程权限限制。Windows 需要足够的 `OpenProcess` 权限，
  遇到提权或受保护进程可能失败。macOS 需要通过 `task_for_pid` 获得调试权限或足够
  权限，且 SIP 或受保护进程仍可能拒绝访问。

### 权限与能力边界

进程和 debuggee 相关动作默认触发权限确认。工具对进程检查、内存读写/冻结、attach、
dump、trace、代码 patch 等动作返回 `ASK`。当 `permissions.default_mode` 为 `"run_everything"` 或启用旧配置 `permissions.run_everything` 时，
这些 `ASK` 权限会通过 CrabCode 现有权限模式自动放行。
当自定义 adapter 配置了 `probe_command` 时，adapter 枚举也会请求确认，因为枚举过程会执行该命令。

该功能面向本机授权进程调试、诊断、测试和研究。不提供隐蔽注入、反调试绕过、
DRM 绕过、持久化、凭据提取或远程进程攻击能力。代码 patch 是字节级原语；agent
必须提供明确要写入的 bytes，修改进程代码前应使用 `expected_hex` 做写前校验。

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
| ------ | -------------- | ------ |
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
| ------ | ------ | -------- |
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
