# CrabCode

[English](./README.md)

终端中的 AI 编程助手 —— 基于 Python 重新实现，采用清晰的前后端分离架构，兼容 Claude Code Agent / Skill。

> 整体参考 Claude Code 设计


## 架构

- **crabcode-core**：核心引擎。负责 API 调用、工具执行、提示词构造、会话管理和 MCP 集成。对外暴露纯异步事件流接口，不依赖任何 I/O 或终端。
- **crabcode-cli**：终端前端。使用 `rich` + `prompt_toolkit` 实现交互式 REPL、Markdown 渲染和流式输出。
- **crabcode-search** *(可选)*：语义代码搜索。将源文件嵌入为向量并存入 USearch 索引，为 agent 提供 `CodebaseSearch` 工具，支持自然语言代码检索。

## 快速开始

```bash
# 以开发模式安装两个包
pip install -e packages/core -e packages/cli

# 设置 API Key
export ANTHROPIC_API_KEY=YourKey

# 管道模式
echo "explain this codebase" | crabcode -p

# 交互式 REPL
crabcode
```

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
| `provider` | API 后端：`anthropic` \| `openai` \| `codex` \| `router` | `anthropic` |
| `model` | 模型 ID | — |
| `base_url` | 自定义 API 地址（适用于第三方转发或本地部署） | — |
| `api_key_env` | 存放 API Key 的**环境变量名**（不是 Key 本身） | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `format` | Router 模式下的协议格式：`anthropic` \| `openai` \| `codex` | — |
| `thinking_enabled` | 是否启用思考模式（不支持该功能的模型需设为 `false`） | `true` |
| `thinking_budget` | 思考 token 预算 | `10000` |
| `max_tokens` | 最大输出 token 数 | `16384` |
| `timeout` | API 调用超时时间（秒），防止网络卡住时无限等待 | `300` |

`env` 字段用于直接在配置文件中定义环境变量，启动时会自动注入，无需在 shell 中 `export`。

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
| `Lint` | 读 | 运行代码检查器和类型检查器 |
| `Memory` | 写 | 存储和读取持久化笔记 |

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

### Memory（持久化记忆）

`Memory` 工具让 agent 拥有跨会话持久化的笔记能力。

- **全局记忆** — 存储在 `~/.crabcode/memory.json`
- **项目记忆** — 存储在 `<项目>/.crabcode/memory.json`

记忆内容会在每次对话开始时自动注入。agent 用它来记住用户偏好、常用约定、项目专有知识，无需每次重新说明。

### Diff 显示

通过 `StrReplace` 或 `Write` 修改文件时，终端会展示精简的内联 diff：

```
  ✎ src/auth.py  lines 42–55  (+8 / -3)
```

每次改动都有完整的审计记录。

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 列出所有命令和可用 Skill |
| `/model` | 查看当前使用的模型，并列出所有已配置的命名模型 |
| `/model <名称>` | 切换到 `settings.models` 中定义的某个命名模型 |
| `/new` | 新建会话（清空对话历史） |
| `/resume [id]` | 恢复历史会话；省略 `id` 时显示列表供选择 |
| `/compact` | 压缩当前对话内容，节省上下文窗口 |
| `/<skill>` | 按名称调用某个 Skill |

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

只读工具（`Read`、`Glob`、`Grep`）始终自动允许，不会弹出确认。

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

## Agent 配置

内置 `Agent` 工具用于生成子 agent 以并行或隔离执行任务。其行为可通过 `settings.json` 中的 `agent` 字段配置：

```json
{
  "agent": {
    "max_turns": 10,
    "timeout": 300,
    "max_output_chars": 12000
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `max_turns` | 每次子 agent 调用的最大 agentic 轮次 | `10` |
| `timeout` | 子 agent 的总超时时间（秒） | `300` |
| `max_output_chars` | 单个工具结果超过此字符数时截断 | `12000` |

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
│   │   ├── tools/              # 内置工具（Bash、Read、Edit、Write、Grep、Glob、Lint、Memory）
│   │   ├── skills/             # Skill 加载（SkillDefinition、load_skills）
│   │   ├── prompts/            # 系统提示词构造
│   │   ├── mcp/                # MCP 服务器集成
│   │   ├── compact/            # 对话压缩
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
└── tests/
```
