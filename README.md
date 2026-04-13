# CrabCode

[中文](./README.zh-CN.md)

AI coding assistant in the terminal — a Python reimplementation with a clean, frontend-backend separated architecture, Compatible with Claude Code Agent and Skills.

> This project is inspired by the design of Claude Code.


## Architecture

- **crabcode-core**: The engine. Handles API calls, tool execution, prompt construction, session management, and MCP integration. Exposes a pure async event-stream interface — no I/O or terminal dependency.
- **crabcode-cli**: A terminal frontend. Uses `rich` + `prompt_toolkit` for interactive REPL, Markdown rendering, and streaming output.
- **crabcode-search** *(optional)*: Semantic codebase search. Embeds source files into a USearch vector index and exposes a `CodebaseSearch` tool that the agent can use for natural-language code lookup.

## Installation

```bash
# Basic install (no semantic search)
pip install crabcode

# With browser automation
pip install crabcode[browser]

# With semantic search
pip install crabcode[search]

# With cloud provider support
pip install crabcode[bedrock]   # AWS Bedrock
pip install crabcode[vertex]    # Google Vertex AI

# Combine extras
pip install crabcode[search,bedrock]
# Example: browser + search
pip install crabcode[browser,search]
```

### Development

```bash
# Install packages in editable mode
pip install -e packages/core packages/cli packages/search
# Minimal install (core + cli only, no semantic search)
pip install -e packages/core packages/cli
# Browser automation dependency
pip install -e packages/core[browser] packages/cli
# Install Chromium once after enabling browser support
playwright install chromium
```

## Quick Start

```bash
# Set your API key
export ANTHROPIC_API_KEY=YourKey

# Pipe mode
echo "explain this codebase" | crabcode -p

# Interactive REPL
crabcode
```

## Multi-API Support

CrabCode supports multiple API backends:

```bash
# Anthropic (default)
crabcode --provider anthropic --model claude-sonnet-4-20250514

# OpenAI
crabcode --provider openai --model gpt-4o
export OPENAI_API_KEY=YourKey

# OpenAI Codex / Responses API (o-series, codex-mini, etc.)
crabcode --provider codex --model codex-mini-latest
export OPENAI_API_KEY=YourKey

# Third-party router (OpenAI-compatible)
crabcode --provider router --base-url https://my-router.example.com/v1 --api-format openai

# Third-party router (Anthropic-compatible)
crabcode --provider router --base-url https://my-router.example.com --api-format anthropic

# Third-party router (Codex/Responses API-compatible)
crabcode --provider router --base-url https://my-router.example.com/v1 --api-format codex --model codex-mini-latest
```

Or configure in `~/.crabcode/settings.json`:

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

`api` field reference:

| Field | Description | Default |
|-------|-------------|---------|
| `provider` | Backend: `anthropic` \| `openai` \| `codex` \| `router` | `anthropic` |
| `model` | Model ID | — |
| `base_url` | Custom API endpoint (for routers or local deployments) | — |
| `api_key_env` | **Name** of the env var that holds the API key (not the key itself) | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `format` | Wire format for router mode: `anthropic` \| `openai` \| `codex` | — |
| `thinking_enabled` | Enable extended thinking (set `false` for models that don't support it) | `true` |
| `thinking_budget` | Thinking token budget | `10000` |
| `max_tokens` | Maximum output tokens | `16384` |
| `timeout` | API call timeout in seconds (prevents hanging on slow/unresponsive APIs) | `300` |

The `env` map lets you define environment variables directly in the config file — they are injected at startup so you don't need to `export` them in your shell.

### Logging

You can configure runtime logging in `settings.json`:

```json
{
  "logging": {
    "level": "WARNING",
    "file": ".crabcode/logs/crabcode.log"
  }
}
```

Notes:

- `level` supports `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`
- The default log file is `<project>/.crabcode/logs/crabcode.log`
- `file` can override the log path
- In the CLI, `/logs crabcode` shows the main runtime log

### Hooks (Tool Call Hooks)

`settings.json` supports hooks that run shell commands on these events:

- `user_prompt_submit`: fires when a user message is submitted
- `pre_tool_call`: fires before a tool call (can block that tool call)
- `post_tool_call`: fires after a tool call

Example:

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

Notes:

- A non-zero exit code means hook failure; a failing `pre_tool_call` blocks that tool execution.
- Use `continue_on_error: true` (or `continueOnError: true`) to keep going on hook failures.
- Claude-style `PreToolUse` / `PostToolUse` keys and nested `hooks: [{\"type\":\"command\", ...}]` are also supported.
- Runtime env vars include `CRABCODE_HOOK_EVENT`, `CRABCODE_HOOK_PAYLOAD`, `CRABCODE_HOOK_TOOL_NAME`, `CRABCODE_HOOK_TOOL_USE_ID`, and `CRABCODE_HOOK_AGENT_ID`.

### Multiple Named Models

Define multiple model profiles in `settings.json` and switch between them at runtime without restarting:

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

Each entry under `models` is a full `ApiConfig` and can have its own `provider`, `base_url`, `api_key_env`, etc.

**Select a profile at startup:**

```bash
crabcode --model-profile smart    # short: -M smart
```

**Switch inside the REPL:**

```
/model              # show active model, list all configured profiles
/model fast         # switch to the "fast" profile
/model local        # switch to the "local" (e.g. Ollama) profile
```

Switching does not clear conversation history — you can mix models freely within a single session.

## Built-in Tools

| Tool | Type | Description |
|------|------|-------------|
| `Bash` | write | Execute shell commands |
| `Read` | read | Read file contents |
| `Write` | write | Create or overwrite files |
| `StrReplace` | write | Precise in-place text replacement |
| `Glob` | read | Find files by glob pattern |
| `Grep` | read | Search file contents with regex |
| `WebSearch` | read | Search the public web for current external information |
| `Browser` | write | Open pages in headless Chromium, interact with DOM, extract content, and take screenshots |
| `Lint` | read | Run linters and type-checkers |
| `Memory` | write | Store and retrieve persistent notes |
| `AskUser` | read | Present choices to the user and wait for selection |

### Lint

The `Lint` tool runs the appropriate linter for the given language automatically:

| Language | Linters |
|----------|---------|
| Python | `ruff` (style), `pylint` (deep analysis), `mypy` (type check) |
| JavaScript / TypeScript | `eslint` |
| Go | `golangci-lint` |
| Rust | `cargo clippy` |
| C / C++ | `clang-tidy`, `cppcheck` |
| Java | `checkstyle`, `pmd` |

The agent calls `Lint` after editing files to verify there are no introduced errors. You can also invoke it directly in conversation: *"lint this file"*.

### Memory

The `Memory` tool gives the agent persistent notes that survive across sessions.

- **Global memory** — stored in `~/.crabcode/memory.json`
- **Project memory** — stored in `<project>/.crabcode/memory.json`

Memories are automatically injected at the top of each conversation. The agent uses them to remember user preferences, recurring conventions, and project-specific facts without re-explaining them every session.

### AskUser

The `AskUser` tool lets the agent present a question with multiple options and wait for the user's selection. This is useful when the agent is unsure about the best approach and wants the user's input before proceeding.

**When the agent calls it**, an interactive selection UI appears in the terminal:

```
  Which approach do you prefer?

    ○ Refactor to a class
  ❯ ● Add error handling
    ○ Write tests first

  ↑↓ navigate · enter select · esc cancel
```

- **Single select** (default): use ↑↓ to navigate, Enter to confirm
- **Multi select** (`multiple: true`): use Space to toggle options, Enter to confirm
- **Esc** / **Ctrl+C**: cancel the selection

**When the agent should use it:**
- Multiple reasonable approaches exist and user preference matters
- Confirming direction before making significant changes
- The user may have context the agent doesn't

**When NOT to use it:**
- The answer is obvious or there's a clear best approach
- The user already told you what to do
- A simple yes/no is enough (the agent can just ask in text)

In **pipe mode** (non-interactive), the first option is auto-selected.

### WebSearch

The `WebSearch` tool searches the public web and returns compact search results with titles, URLs, and snippets.

- **Default provider order** — Tavily first when `TAVILY_API_KEY` is configured, otherwise DuckDuckGo HTML search
- **Offline behavior** — if CrabCode cannot detect outbound network access during session startup, `WebSearch` is disabled for that session and is not exposed to the model
- **Permission behavior** — `WebSearch` is read-only, but still asks for confirmation before each network request

Configure via `tool_settings.WebSearch` in `settings.json`:

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

Supported `provider` values:

- `"auto"` — use Tavily when configured, otherwise DuckDuckGo; if Tavily fails at runtime, retry with DuckDuckGo once
- `"tavily"` — require a configured API key and use Tavily only
- `"ddg"` — use DuckDuckGo only

### Browser

The `Browser` tool drives a persistent Chromium session for page interaction and extraction. It runs headless by default, but `create_session` can override that with `headless: false`.

- Use `WebSearch` to discover URLs or current public-web results
- Use `Browser` when you need to open a specific page, click, fill, evaluate page-side JavaScript, or capture a screenshot
- Install support with `pip install crabcode[browser]` and run `playwright install chromium` once

Supported actions:

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

Example `create_session` inputs:

```json
{ "action": "create_session" }
```

```json
{ "action": "create_session", "headless": false }
```

Configure via `tool_settings.Browser` in `settings.json`:

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

Default permission behavior:

- `create_session` and `goto` ask for confirmation
- `fill`, `press`, and `evaluate` ask for confirmation
- `extract`, `wait_for`, `list_tabs`, `switch_tab`, `close_tab`, and `close_session` are allowed by default
- `screenshot` is allowed by default inside the working directory and asks when writing outside it

`headless` in `tool_settings.Browser` is the session default. The tool input can override it per session when calling `create_session`.

### Diff Display

When the agent edits a file via `StrReplace` or `Write`, the terminal shows a compact inline diff:

```
  ✎ src/auth.py  lines 42–55  (+8 / -3)
```

This gives you a precise audit trail of every change made.

## REPL Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all available commands and skills |
| `/status` | Show runtime status (model, context usage, compactions, agent summary) |
| `/logs` | List background logs (for example search index logs) |
| `/logs <name>` | Show tail of a specific log |
| `/logs -f <name>` | Follow a specific log in real time (`Ctrl+C` to stop) |
| `/logs --clear <name>` | Clear a specific log file |
| `/model` | Show active model and all configured named models |
| `/model <name>` | Switch to a named model from `settings.models` |
| `/agents` | List managed sub-agents in the current session |
| `/agent <id>` | Show details for one agent (`status`, `usage`, `result`, transcript path) |
| `/agent-log <id>` | Show the stored transcript for one agent |
| `/agent-send <id> <prompt>` | Send additional input to an existing agent |
| `/wait <id>` | Wait for one agent to finish and print its summary |
| `/cancel-agent <id>` | Cancel a running agent |
| `/new` | Start a fresh session (clear in-memory conversation history) |
| `/compact` | Manually compact conversation history to save context |
| `/clear` | Clear current in-memory conversation messages |
| `/sessions` | List recent saved sessions |
| `/resume <id>` | Resume a saved session by full/partial id or index |
| `/exit`, `/quit` | Exit CrabCode |
| `/<skill>` | Invoke a skill by name (optional user input can follow) |
| `! <shell command>` | Run a shell command directly from REPL (outside model tool loop) |

### Notes

- For commands that take `<id>`, you can usually pass the leading prefix shown by `/agents`.
- `/agent-send` live output is controlled by `agent.stream_send_input_output` in `settings.json`.
- `Ctrl+C` interrupts the current operation; pressing `Ctrl+C` again within a few seconds exits.

## Permissions

Before executing any tool that modifies files or runs shell commands, CrabCode prompts for confirmation:

```
╭─ ⚠ Bash ──────────────────╮
│ python main.py             │
╰────────────────────────────╯
  Allow Bash? (y)es / (n)o / (a)lways allow:
```

- **y** — allow this one call
- **n** — deny; the model is told the call was rejected and should not retry it
- **a** — always allow calls to this tool for the rest of the session (no more prompts)
  For `Browser`, this is scoped to the current action, such as `Browser:goto` or `Browser:fill`.

Read-only tools (`Read`, `Glob`, `Grep`) are always allowed without prompting. `WebSearch` is the exception: it is read-only, but still asks for confirmation before each network request.

### Permission rules

Fine-grained rules can be set in `settings.json` under `permissions`:

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

Each rule matches on `tool` name (glob `*` matches any), plus optional `command` or `path` filters.

### run_everything mode

Set `"run_everything": true` to skip all permission prompts and execute every tool call automatically. The CLI displays a warning at startup when this mode is active.

```json
{
  "permissions": {
    "run_everything": true
  }
}
```

> **Use with caution.** In this mode CrabCode will run shell commands and write files without asking.

## Configuration

Settings are loaded from multiple layers (later overrides earlier):

1. `~/.crabcode/settings.json` (user)
2. `<project>/.crabcode/settings.json` (project)
3. `<project>/.crabcode/settings.local.json` (local, gitignored)
4. Flag settings
5. `~/.crabcode/managed-settings.json` (policy)

## CLAUDE.md (project instructions)

`CLAUDE.md` is a Markdown file whose contents are **automatically injected** as context at the start of every conversation — no command needed. Use it to encode project conventions, coding style rules, common commands, and anything else the model should always know about.

### Where to put it

Files from the following locations are loaded and concatenated in order:

| Path | Scope |
|------|-------|
| `~/.claude/CLAUDE.md` | User-global, Claude Code compatible |
| `~/.crabcode/CLAUDE.md` | User-global, CrabCode native |
| `<each dir from git-root to cwd>/CLAUDE.md` | Project-level, walked downward |
| `<each dir from git-root to cwd>/.claude/CLAUDE.md` | Same, inside `.claude/` subdirectory |

### Example

```markdown
# Project conventions

- Use `ruff` for style checks — all commits must pass
- Every new function needs a docstring
- Database migrations go in `migrations/`, named `YYYYMMDD_description.sql`
- Do not touch files under `legacy/` unless the user explicitly asks

## Common commands

- Run tests: `pytest -x`
- Format: `ruff format .`
- Start dev server: `make dev`
```

A global `~/.crabcode/CLAUDE.md` is good for personal preferences (preferred language, style habits). The project-level `CLAUDE.md` is for team conventions.

## Skills

Skills are Markdown instruction files stored on disk that let you package common workflows into reusable slash commands.

### Creating a skill

Place a `SKILL.md` file inside `.crabcode/skills/<skill-name>/` (project-level) or `~/.crabcode/skills/<skill-name>/` (global):

```
.crabcode/
└── skills/
    └── commit/
        └── SKILL.md
```

`SKILL.md` format:

```markdown
---
name: commit
description: "Generate a conventional commit message and commit the staged changes"
when_to_use: "When the user wants to commit code"
---

Inspect the current git diff, craft a commit message following the conventional commits
specification, then run git commit.

User's additional request: $USER_INPUT
```

Frontmatter fields:

| Field | Description |
|-------|-------------|
| `name` | Skill name — also the slash-command trigger (defaults to directory name) |
| `description` | Short description shown to the model to decide when to invoke the skill |
| `when_to_use` | Additional trigger condition hint |
| `paths` | Comma-separated glob list; skill is only activated for matching file paths (optional) |

Use `$USER_INPUT` anywhere in the body — it will be replaced at runtime with whatever the user typed after the slash command.

### Invoking a skill

Type `/<skill-name>` in the REPL:

```
❯ /commit fix login page styling
```

`/help` automatically lists all available skills. The model can also invoke skills proactively when a task matches a skill's `description` or `when_to_use`.

### Load priority

Skills with the same name are merged in order (later entries override earlier ones):

1. `~/.claude/skills/` — Claude Code global skills (compatibility)
2. `~/.crabcode/skills/` — CrabCode global skills
3. `.claude/skills/` — project-level, searched upward from cwd (compatibility)
4. `.crabcode/skills/` — project-level, searched upward from cwd (highest priority)

### Auto-trigger

Skills can be **automatically triggered** based on the user's current context — no manual `/command` needed. When a user message matches a skill's patterns, the skill's instructions are injected into the conversation as a system reminder.

Add pattern fields to the frontmatter:

```markdown
---
name: python-dev
description: "Python development workflow"
pathPatterns: "**/*.py, **/*.pyi"
bashPatterns:
  - "pytest .*"
  - "ruff .*"
importPatterns:
  - "from django"
  - "import flask"
chainTo: "python-test"
---

Follow PEP 8 conventions and use type hints.
```

Pattern fields:

| Field | Type | Description |
|-------|------|-------------|
| `pathPatterns` | Comma-separated or YAML list | Glob patterns matched against file paths in the user's message (e.g. `"**/*.py"`, `"src/**/*.ts"`) |
| `bashPatterns` | Comma-separated or YAML list | Regex patterns matched against shell commands in the user's message (e.g. `"pytest .*"`, `"git commit.*"`) |
| `importPatterns` | Comma-separated or YAML list | Regex patterns matched against import/require lines in the user's message (e.g. `"from django"`, `"import React"`) |
| `chainTo` | Comma-separated or YAML list | Skill names to automatically chain after this skill (e.g. `"lint"` triggers the `lint` skill next) |

**How it works:**

1. When a user sends a message, CrabCode extracts file paths, bash commands, and import lines from the text.
2. Each skill's `pathPatterns`, `bashPatterns`, and `importPatterns` are checked against the extracted context.
3. Matching skills are activated — their content is injected as a `<system-reminder>` message.
4. If a matched skill has `chainTo`, the chained skills are also activated (circular chains are safely broken).

**Example chain:**

```markdown
# .crabcode/skills/python-dev/SKILL.md
---
name: python-dev
pathPatterns: "**/*.py"
chainTo: "python-test"
---
Follow PEP 8 and use type hints.

# .crabcode/skills/python-test/SKILL.md
---
name: python-test
bashPatterns: "pytest .*"
chainTo: "python-lint"
---
Run pytest with verbose output.

# .crabcode/skills/python-lint/SKILL.md
---
name: python-lint
---
Run ruff check and mypy.
```

When a user mentions `src/app.py`, all three skills activate in order: `python-dev` → `python-test` → `python-lint`.

## Agent Settings

The built-in `Agent` tool spawns sub-agents for parallel or isolated tasks. Its behavior can be configured via the `agent` field in `settings.json`:

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

| Field | Description | Default |
|-------|-------------|---------|
| `max_turns` | Maximum agentic turns per sub-agent invocation | `10` |
| `timeout` | Total wall-clock timeout in seconds for a sub-agent | `300` |
| `max_output_chars` | Truncate individual tool results beyond this many characters | `12000` |
| `stream_send_input_output` | Stream live output after `/agent-send` in REPL; set `false` to send input silently | `false` |

Example browser-focused sub-agent profile:

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

## Display Settings

The number of lines shown for tool results in the terminal can be configured via the `display` field in `settings.json`:

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

| Field | Description | Default |
|-------|-------------|---------|
| `default_max_lines` | Default maximum display lines for tool results | `50` |
| `max_chars` | Character count safety cap for display content | `50000` |
| `tool_max_lines` | Override `default_max_lines` per tool name; only configure tools you want to adjust | See below |

Built-in tool line limits:

| Tool | Default lines |
|------|---------------|
| `Agent` | `120` |
| `Bash` | `60` |
| `Grep` | `50` |
| `Glob` | `30` |
| `Read` | `80` |
| `Lint` | `60` |
| `WebSearch` | `50` |
| `Browser` | `60` |
| `CodebaseSearch` | `50` |
| Others | `50` (i.e. `default_max_lines`) |

Content exceeding the line limit is truncated with a note showing how many lines were omitted. Content exceeding `max_chars` is also truncated.

Sub-agents are concurrency-safe and run in parallel when the parent model issues multiple `Agent` calls in the same turn. Each sub-agent gets an isolated message history and the same tool set as the parent.

## Extra Tools

`extra_tools` lets you attach additional tool packages to the agent without modifying the core. Each entry is a Python import path to a `Tool` subclass.

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

When the session starts, each extra tool's `setup()` method is called with a `ToolContext` that includes:
- `cwd` — the current working directory
- `tool_config` — the matching entry from `tool_settings`
- `on_event` — a callback for emitting real-time progress events to the CLI

This is the extension point used by `crabcode-search` to kick off background indexing on startup.

## crabcode-search (Semantic Codebase Search)

`crabcode-search` is an optional package that adds semantic code search to the agent.

### Install

```bash
pip install -e packages/search          # AST chunking + all embedder backends
```

### How it works

1. **Chunking** — source files are split into semantic units (functions, classes, methods). Uses tree-sitter AST parsing when available, falls back to regex-based boundary detection.
2. **Embedding** — each chunk is embedded into a dense vector using a configurable model.
3. **Storage** — vectors are stored in a local USearch index under `.crabcode/search/`. Repos with fewer than 100k chunks use exact inner-product search; larger repos automatically switch to approximate HNSW traversal. 
4. **Search** — at query time, the query is embedded and the nearest chunks are returned with file path, line range, and relevance score.

### Indexing

Indexing starts automatically in the background when the agent session starts. The CLI shows a live progress bar. The agent can still search while indexing is in progress — it will get partial results and a note to use `Grep` if needed.

Subsequent runs use incremental mtime-based updates — only changed files are re-indexed.

### Embedding backends

Configure via `tool_settings.CodebaseSearch` in `settings.json`:

| Backend | `embedder` value | Notes |
|---------|-----------------|-------|
| Ollama (local) | `"ollama"` | Default. Requires a running Ollama instance. |
| OpenAI API | `"openai"` | Requires `OPENAI_API_KEY`. |
| Google Gemini API | `"gemini"` | Requires `GEMINI_API_KEY`. |
| HuggingFace (local) | `"huggingface"` | Requires `pip install sentence-transformers`. |
| ModelScope (local) | `"modelscope"` | Requires `pip install modelscope`. |

### CPU thread limit

Local backends (HuggingFace, ModelScope) use all available CPU cores by default, which can make your machine unresponsive during indexing. Use the `threads` option to cap the number of threads:

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

`threads` limits PyTorch (`torch.set_num_threads`) and the `OMP_NUM_THREADS` / `MKL_NUM_THREADS` environment variables simultaneously. A value of 2–4 is a good starting point on most laptops.

Example configuration using Ollama:

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

Example using Gemini:

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

## Prompt Profile

The system prompt is fully configurable via a `prompt_profile` in `settings.json`. This lets you swap the agent's identity and behavioral sections without touching the engine code — useful for building non-coding agents on top of `crabcode-core`.

Each section field follows the same rule:
- **omitted / `null`** → use the built-in default
- **`""`** → disable that section entirely
- **non-empty string** → replace with your own content

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

`prompt_profile` field reference:

| Field | Description | Default |
|-------|-------------|---------|
| `prefix` | First sentence that names the assistant | `"You are CrabCode…"` |
| `intro` | Full intro section override | built-in |
| `system` | System behaviour rules | built-in |
| `doing_tasks` | Task execution guidelines (coding-specific) | built-in |
| `actions` | Reversibility / blast-radius rules | built-in |
| `git_safety` | Git safety protocol | built-in |
| `using_tools` | Tool usage guidelines | built-in |
| `tone_and_style` | Tone and formatting rules | built-in |
| `output_efficiency` | Verbosity rules | built-in |
| `session_guidance` | Session-level hints | built-in |
| `agent_prompt` | System prompt for spawned sub-agents | built-in |
| `extra_sections` | Additional sections appended after all built-in ones | `[]` |

You can also build profiles in code using `PromptProfile` from `crabcode_core.prompts.profile`:

```python
from crabcode_core.prompts.profile import PromptProfile, minimal_profile
from crabcode_core.events import CoreSession
from crabcode_core.types.config import CrabCodeSettings

profile = minimal_profile()           # strips coding-specific sections
profile.prefix = "You are a data analysis assistant."
profile.extra_sections = ["Always use pandas for data manipulation."]

session = CoreSession(settings=CrabCodeSettings(prompt_profile=profile.model_dump()))
```

`minimal_profile()` is a convenience preset that removes the `doing_tasks`, `actions`, and `git_safety` sections — a clean starting point for non-coding domains.

## Project Structure

```
crabcode/
├── packages/
│   ├── core/crabcode_core/     # Core library
│   │   ├── types/              # Pydantic types (Message, Tool, Event, Config)
│   │   ├── api/                # API adapters (Anthropic, OpenAI, Router)
│   │   ├── query/              # Agentic turn loop
│   │   ├── tools/              # Built-in tools (Bash, Read, Edit, Write, Grep, Glob, WebSearch, Lint, Memory, AskUser)
│   │   ├── skills/             # Skill loading + auto-trigger matching (SkillDefinition, load_skills, auto_match)
│   │   ├── prompts/            # System prompt construction
│   │   ├── mcp/                # MCP server integration
│   │   ├── compact/            # Conversation compaction
│   │   ├── session/            # Session persistence (JSONL)
│   │   ├── config/             # Multi-layer settings
│   │   ├── permissions/        # Tool permission management
│   │   └── events.py           # CoreSession (main frontend interface)
│   ├── cli/crabcode_cli/       # CLI frontend
│   │   ├── app.py              # Entry point (typer)
│   │   ├── repl.py             # Interactive REPL
│   │   ├── pipe.py             # Pipe mode
│   │   └── render/             # Terminal rendering
│   └── search/crabcode_search/ # Semantic search (optional)
│       ├── chunker.py          # AST + regex code chunking
│       ├── embedder.py         # Embedding backends (Ollama, Gemini, OpenAI, HuggingFace, ModelScope)
│       ├── store.py            # USearch vector store (exact → HNSW at 100k chunks)
│       ├── indexer.py          # File scanning, change detection, batch indexing
│       └── tool.py             # CodebaseSearchTool (extra_tools entry point)
└── tests/
```
