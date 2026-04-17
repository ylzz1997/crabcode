# CrabCode

[中文](./README.zh-CN.md)

AI coding assistant in the terminal — a Python reimplementation with a clean, frontend-backend separated architecture, Compatible with Claude Code Agent and Skills.

> This project is inspired by the design of Claude Code.


## Architecture

- **crabcode-core**: The engine. Handles API calls, tool execution, prompt construction, session management, and MCP integration. Exposes a pure async event-stream interface — no I/O or terminal dependency.
- **crabcode-cli**: A terminal frontend. Uses `rich` + `prompt_toolkit` for interactive REPL, Markdown rendering, and streaming output.
- **crabcode-search** *(optional)*: Semantic codebase search. Embeds source files into a USearch vector index and exposes a `CodebaseSearch` tool that the agent can use for natural-language code lookup.
- **crabcode-gateway** *(optional)*: Multi-protocol (HTTP/gRPC) gateway server. Exposes `crabcode-core` as a network service with REST API, SSE event streaming, WebSocket bidirectional channel, and gRPC — enabling integration with VSCode extensions, web UIs, and other clients.

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
# With gateway server support
pip install crabcode[gateway]
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

# Resume last session
crabcode --continue      # or -c

# Resume a specific session
crabcode --resume <id>   # or -r <id>
```

### Session Management CLI

```bash
# List sessions for the current project
crabcode sessions list

# List sessions across all projects
crabcode sessions list --all

# Search sessions by keyword
crabcode sessions search "refactor auth"

# Export a session to Markdown or JSON
crabcode sessions export <id> --format md --output chat.md

# Archive old sessions (older than 30 days) and clean up
crabcode sessions prune --days 30 --delete-files

# Show usage statistics
crabcode stats
crabcode stats --project   # current project only
```

## Multi-API Support

### Gateway Server

CrabCode can run as a multi-protocol network service, enabling integration with VSCode extensions, web UIs, and other external clients.

```bash
# Start HTTP gateway on port 4096
crabcode gateway

# Custom port and host
crabcode gateway --port 8080 --host 0.0.0.0

# With gRPC support
crabcode gateway --port 4096 --grpc-port 50051

# With Basic Auth
crabcode gateway --password secret
```

**HTTP API endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/session/new` | POST | Create a new session |
| `/session/send` | POST | Send a message (starts query loop, events via SSE) |
| `/session/interrupt` | POST | Interrupt current turn |
| `/session/compact` | POST | Trigger manual compaction |
| `/session/list` | GET | List active sessions |
| `/session/resume` | POST | Resume a session |
| `/agent/spawn` | POST | Spawn a sub-agent |
| `/agent/{id}` | GET | Get agent status |
| `/agent/list` | GET | List all agents |
| `/agent/{id}/cancel` | POST | Cancel an agent |
| `/agent/{id}/input` | POST | Send input to an agent |
| `/agent/wait` | POST | Wait for agent(s) to complete |
| `/permission/respond` | POST | Respond to a permission request |
| `/choice/respond` | POST | Respond to a choice request |
| `/config/models` | GET | List available models |
| `/config/switch-model` | POST | Switch model |
| `/config/switch-mode` | POST | Switch agent/plan mode |
| `/tools` | GET | List available tools (including MCP) |
| `/context` | POST | Push workspace context (active file, selection, cursor) |
| `/snapshot/checkpoint` | POST | Create checkpoint with file snapshot |
| `/snapshot/list` | GET | List checkpoints for a session |
| `/snapshot/revert` | POST | Revert files + conversation to a checkpoint |
| `/snapshot/rollback` | POST | Rollback conversation only (no file restore) |
| `/event` | GET (SSE) | Real-time event stream with 10s heartbeat |
| `/ws` | WebSocket | Bidirectional channel (preferred for VSCode) |

**WebSocket `/ws`** supports incoming commands (`send_message`, `permission_response`, `choice_response`, `push_context`) and outgoing event payloads — a single connection handles all interaction, making it ideal for VSCode extensions.

**gRPC** service is available when `--grpc-port` is set, with streaming `SendMessage` and `SubscribeEvents` RPCs. See `packages/gateway/crabcode_gateway/grpc_/proto/crabcode.proto` for the full service definition.

### ACP (Agent Client Protocol) Support

CrabCode supports the **Agent Client Protocol (ACP)**, an open JSON-RPC protocol that standardizes communication between code editors and AI coding agents. This lets you use CrabCode directly inside ACP-compatible editors like Zed and JetBrains IDEs.

```bash
# Start CrabCode as an ACP agent (communicates over stdio)
crabcode acp
```

**How it works:**

1. `crabcode acp` starts an internal Gateway HTTP server, then launches the ACP agent on stdio
2. The editor (Zed, JetBrains) spawns `crabcode acp` as a subprocess
3. Communication flows over stdin/stdout via JSON-RPC (ndjson)
4. ACP events (tool calls, permissions, streaming text) are translated from CrabCode's EventBus in real-time

**Zed configuration** — add to `settings.json`:

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

**Supported ACP capabilities:**

| Capability | Details |
|-----------|---------|
| Session management | new, load, list, fork, resume |
| Prompt | text, image, resource links, embedded context |
| MCP integration | HTTP and SSE MCP servers from the editor |
| Permission requests | Allow once / Always allow / Reject |
| Tool updates | real-time status (pending → in_progress → completed/failed) |
| Streaming | agent message chunks, thought chunks |
| Model switching | change model mid-session via config options |
| Mode switching | agent mode / plan mode |

**Architecture:** The ACP layer is a thin adapter — it translates between ACP JSON-RPC and CrabCode's Gateway REST API, so no core logic is duplicated.

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

# Ollama (local)
crabcode --provider ollama --model qwen3:32b
# Or configure in settings.json:
# {"api": {"provider": "ollama", "model": "qwen3:32b"}}

# Google Gemini
crabcode --provider gemini --model gemini-2.5-flash
export GEMINI_API_KEY=YourKey

# Azure OpenAI
crabcode --provider azure --model my-gpt4o-deployment
export AZURE_OPENAI_API_KEY=YourKey
export AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/
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
| `provider` | Backend: `anthropic` \| `openai` \| `codex` \| `router` \| `ollama` \| `gemini` \| `azure` | `anthropic` |
| `model` | Model ID | — |
| `base_url` | Custom API endpoint (for routers or local deployments) | — |
| `api_key_env` | **Name** of the env var that holds the API key (not the key itself) | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `format` | Wire format for router mode: `anthropic` \| `openai` \| `codex` | — |
| `thinking_enabled` | Enable extended thinking (set `false` for models that don't support it) | `true` |
| `thinking_budget` | Thinking token budget | `10000` |
| `max_tokens` | Maximum output tokens | `16384` |
| `timeout` | API call timeout in seconds (prevents hanging on slow/unresponsive APIs) | `300` |
| `context_window` | Override the model's context window size (tokens). Used when auto-detection fails or is inaccurate — see [Context Window](#context-window) below. | auto-detected |

The `env` map lets you define environment variables directly in the config file — they are injected at startup so you don't need to `export` them in your shell.

### Context Window

CrabCode automatically manages the context window to prevent `400 token limit exceeded` errors.

**How context window size is resolved** (in priority order):

1. `context_window` field in your `api` config (explicit override)
2. Built-in lookup table for known models (e.g. `glm-5.1-fp8` → 202752, `gpt-4o` → 128000)
3. `DEFAULT_CONTEXT_WINDOW` fallback: `200000`

If your model is not in the built-in table and you don't set an override, the fallback of 128k is used. For models with a larger window (e.g. Zhipu GLM), set `context_window` explicitly:

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

**Automatic compaction**

When the estimated token count approaches the context limit, CrabCode automatically:

1. Summarizes old conversation messages into a single compact `[Conversation summary: ...]` message via an API call
2. Keeps the most recent messages intact
3. Continues the current task in the same session without interruption

You can also trigger compaction manually with `/compact`, or configure the threshold via `max_context_length` in `settings.json`.

> **Note**: Auto-compaction uses the same model configured in `api`. If your model is served through a custom endpoint, make sure the `model` field in `api` is set correctly — otherwise the summarization call may fail silently.

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
      "provider": "ollama",
      "model": "qwen3:32b",
      "thinking_enabled": false
    },
    "smart": {
      "provider": "anthropic",
      "model": "claude-opus-4-20250514"
    },
    "code": {
      "provider": "openai",
      "model": "gpt-4o"
    },
    "local": {
      "provider": "ollama",
      "model": "deepseek-coder-v2:236b",
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
/model fast         # switch to the "fast" (ollama) profile
/model smart        # switch to the "smart" (anthropic) profile
/model code         # switch to the "code" (openai) profile
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

### LSP Integration

CrabCode integrates with **Language Server Protocol (LSP)** servers to provide real-time code intelligence — diagnostics, hover info, go-to-definition, and references — directly to the AI agent.

**How it works:** When the agent writes or edits a file, CrabCode automatically notifies the relevant LSP server (e.g. `pyright` for Python, `typescript-language-server` for TS). The server analyzes the code and sends back diagnostics (errors, warnings). These are injected into the tool result so the LLM can see and fix issues immediately — forming a closed feedback loop.

**Default behavior:** LSP is **enabled by default**. Language servers are lazily started — only when a file of the matching type is first accessed. Failed starts are remembered so they aren't retried. This means zero overhead for projects where LSP isn't needed.

**Built-in servers** (detected from `PATH`):

| Language | Server | Extensions |
|----------|--------|------------|
| Python | `pyright-langserver` | `.py`, `.pyi`, `.pyw` |
| TypeScript / JS | `typescript-language-server` | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` |
| Go | `gopls` | `.go` |
| Rust | `rust-analyzer` | `.rs` |
| C / C++ | `clangd` | `.c`, `.cpp`, `.h`, `.hpp`, `.cc`, `.cxx` |
| C# | `omnisharp` | `.cs` |
| Java | `jdtls` | `.java` |
| Ruby | `solargraph` | `.rb` |
| PHP | `phpactor` | `.php` |
| Dart | `dart language-server` | `.dart` |
| Lua | `lua-language-server` | `.lua` |
| Kotlin | `kotlin-language-server` | `.kt`, `.kts` |
| Swift | `sourcekit-lsp` | `.swift` |
| Zig | `zls` | `.zig` |
| Elixir | `lexical` | `.ex`, `.exs` |
| Scala | `metals` | `.scala` |

If a server isn't installed, it's simply skipped — no error, no delay.

**Configuration in `settings.json`:**

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

| Field | Description |
|-------|-------------|
| `lsp` | `true` = enable (default), `false` = disable all, `{}` = enable with custom overrides |
| `command` | Command to start the LSP server (must support `--stdio` mode) |
| `extensions` | File extensions this server handles |
| `env` | Extra environment variables for the server process |
| `initialization` | `initializationOptions` passed to the LSP `initialize` request |
| `disabled` | Set `true` to disable a specific built-in server |

**Per-agent-type control:** Sub-agents can opt out of LSP via `enable_lsp`:

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

`enable_lsp` defaults to `true`. When `false`, the sub-agent's `ToolContext.lsp_manager` is `None` and no LSP operations are triggered.

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

### Snapshot & Revert

CrabCode automatically tracks file-system changes made during a session, allowing you to **undo** code changes by reverting to a previous checkpoint.

**How it works:**

1. Every time you create a checkpoint (`/checkpoint`), CrabCode takes a snapshot of the working directory state using git internals (or file-copy fallback for non-git projects).
2. Tools that modify files (`Edit`, `Write`, `Bash`) also record per-file snapshots before each change.
3. You can revert to any checkpoint to restore both the conversation **and** the files to that point.

**Commands:**

```
/checkpoint "before refactor"    # create checkpoint with file snapshot
/checkpoints                     # list checkpoints (✓ = has file snapshot)
/revert 1                        # revert files + conversation to checkpoint #1
/undo                            # revert the most recent checkpoint
/rollback 1                      # rollback conversation only (no file restore)
```

**Difference between `/revert` and `/rollback`:**

| Command | Conversation | Files |
|---------|-------------|-------|
| `/revert` | Rolled back | Restored to snapshot |
| `/rollback` | Rolled back | Not touched |
| `/undo` | Same as `/revert` (targets most recent checkpoint) | Restored to snapshot |

**How snapshots are stored:**

- **Git repos** (preferred): uses `git write-tree` + `git update-ref` under `refs/crabcode/` — lightweight, zero-commit snapshots that don't pollute your git history.
- **Non-git directories**: files are copied to `.crabcode/snapshots/` for tracking.

**Gateway API:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/snapshot/checkpoint` | POST | Create checkpoint with file snapshot |
| `/snapshot/list` | GET | List checkpoints for a session |
| `/snapshot/revert` | POST | Revert files + conversation to a checkpoint |
| `/snapshot/rollback` | POST | Rollback conversation only |

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
| `/plan` | Switch to plan mode (read-only analysis and plan generation) |
| `/agent` | Switch to agent mode (normal execution mode) |
| `/plan-status` | Show current plan and mode status |
| `/wait <id>` | Wait for one agent to finish and print its summary |
| `/cancel-agent <id>` | Cancel a running agent |
| `/team list` | List active agent teams |
| `/team status <team_id>` | Show team status table |
| `/team messages <team_id>` | Show team message history |
| `/team shutdown <team_id>` | Shut down a team |
| `/new` | Start a fresh session (clear in-memory conversation history) |
| `/compact` | Manually compact conversation history to save context |
| `/clear` | Clear current in-memory conversation messages |
| `/sessions` | List recent saved sessions for current project |
| `/recent` | List recent sessions across all projects |
| `/search <query>` | Search sessions by title or message content |
| `/resume <id>` | Resume a saved session by full/partial id or index (supports cross-project) |
| `/archive <id>` | Archive a session (hide from listings) |
| `/export [md\|json] [path]` | Export current session to Markdown or JSON |
| `/stats` | Show usage statistics (tokens, sessions, models) |
| `/checkpoint [label]` | Create a checkpoint at the current conversation position (includes file snapshot) |
| `/checkpoints` | List checkpoints for the current session (with file snapshot status) |
| `/rollback <id\|#>` | Rollback conversation to a checkpoint (conversation only, no file restore) |
| `/revert <id\|#>` | Revert both files AND conversation to a checkpoint |
| `/undo` | Undo last checkpoint — revert files + conversation to the most recent checkpoint |
| `/exit`, `/quit` | Exit CrabCode |
| `/<skill>` | Invoke a skill by name (optional user input can follow) |
| `! <shell command>` | Run a shell command directly from REPL (outside model tool loop) |

### Notes

- For commands that take `<id>`, you can usually pass the leading prefix shown by `/agents`.
- `/agent-send` live output is controlled by `agent.stream_send_input_output` in `settings.json`.
- `Ctrl+C` interrupts the current operation; pressing `Ctrl+C` again within a few seconds exits.
- `/resume` supports cross-project sessions — if the session ID belongs to another project it will be resolved automatically via the metadata database.

### Plan Mode Workflow

When plan mode produces an execution plan, CrabCode does not auto-run it immediately. The REPL prints the full plan and asks what to do next:

- `y` / `yes`: execute the plan via DAG scheduler in agent mode
- `m` / `modify`: stay in plan mode and revise the plan
- `n` / `no`: cancel and clear the pending plan

This keeps the final execution decision with the user while preserving parallel DAG orchestration after confirmation.

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

## Agent Teams

Agent Teams let a lead AI spawn multiple teammates that coordinate through message passing and a shared task board. Each teammate runs in its own context window and can use a different model — enabling multi-model collaboration (e.g. Claude for coding, Gemini for research, GPT for review) within a single team.

### How it works

1. The lead agent calls `TeamCreate` to create a team, then `TeamSpawn` to add teammates with specific roles and optional model profiles.
2. Teammates communicate via `TeamMessage` (peer-to-peer) or `TeamBroadcast` (to all teammates).
3. The shared task board lets the lead assign work and teammates atomically claim tasks — concurrent claims are serialized via a lock.
4. Messages are stored as JSONL (O(1) append writes) and injected into the recipient's session with auto-wake for idle agents.
5. Backpressure: each teammate has a bounded queue (default 100 messages); overflow drops the oldest unread message with a warning.

### Built-in Team Tools

| Tool | Description |
|------|-------------|
| `TeamCreate` | Create a new team |
| `TeamSpawn` | Spawn a teammate with a role (worker/researcher/reviewer), optional model profile |
| `TeamMessage` | Send a message to a specific teammate |
| `TeamBroadcast` | Broadcast a message to all teammates |
| `TeamStatus` | View team and teammate status |
| `TeamTaskAdd` | Add a task to the shared task board |
| `TeamTaskClaim` | Atomically claim an unclaimed task |
| `TeamTaskComplete` | Mark a claimed task as completed |
| `TeamShutdown` | Shut down the team and cancel all teammates |

### Configuration

Configure via the `team` field in `settings.json`:

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

| Field | Description | Default |
|-------|-------------|---------|
| `max_teammates` | Maximum teammates per team | `8` |
| `inbox_dir` | Custom directory for JSONL inbox files (default: `~/.crabcode/team_inbox/`) | `null` |
| `backpressure_queue_size` | Per-teammate message queue size | `100` |
| `message_size_limit` | Max message size in bytes | `10240` (10KB) |

### Cross-team communication

Teams are isolated by default. `TeamBridge` allows controlled messaging between teams with configurable policies:

- `allow_all` — all cross-team messages pass through
- `allow_tagged` — only messages with a specific tag are forwarded
- `deny` — no cross-team messages (default)

### Crash recovery

When the server restarts while teammates are running, busy teammates are force-transitioned to `ready` (not auto-restarted) and the lead is notified to re-engage them manually. This prevents runaway agents from burning API credits unattended.

### REPL commands

| Command | Description |
|---------|-------------|
| `/team list` | List active teams |
| `/team status <team_id>` | Show team status table |
| `/team messages <team_id>` | Show team message history |
| `/team shutdown <team_id>` | Shut down a team |

### Multi-model example

```json
{
  "models": {
    "coder": { "provider": "anthropic", "model": "claude-opus-4-20250514" },
    "researcher": { "provider": "gemini", "model": "gemini-2.5-pro" },
    "reviewer": { "provider": "openai", "model": "gpt-4o" }
  }
}
```

The lead spawns teammates with `model_profile` pointing to different named models — each teammate uses the corresponding provider and model.

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
│   │   ├── tools/              # Built-in tools (Bash, Read, Edit, Write, Grep, Glob, WebSearch, Lint, Memory, AskUser, Team)
│   │   ├── team/               # Agent Teams (models, message bus, manager, inbox, recovery, bridge)
│   │   ├── lsp/                # LSP client integration (LSPClient, LSPManager, diagnostics formatting, server registry)
│   │   ├── skills/             # Skill loading + auto-trigger matching (SkillDefinition, load_skills, auto_match)
│   │   ├── prompts/            # System prompt construction
│   │   ├── mcp/                # MCP server integration
│   │   ├── compact/            # Conversation compaction
│   │   ├── snapshot/           # File-system snapshots & revert (SnapshotManager, tracker)
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
│   └── gateway/crabcode_gateway/ # Gateway server (optional)
│       ├── server.py           # GatewayServer main entry
│       ├── adapter.py          # ProtocolAdapter ABC (HTTP, gRPC)
│       ├── schemas.py          # Pydantic request/response + CoreEvent serialization
│       ├── middleware.py       # Auth, Logger, CORS, Error middleware
│       ├── event_bus.py        # Multi-subscriber event bus (SSE + WS)
│       ├── acp/                # ACP (Agent Client Protocol) layer
│       │   ├── agent.py        # CrabCodeACPAgent — ACP Agent implementation
│       │   ├── session.py      # ACPSessionManager — ACP session state
│       │   ├── types.py        # ACP types + tool kind mapping
│       │   └── transport.py    # stdio transport (run_agent wrapper)
│       ├── routes/             # FastAPI route groups (session, agent, config, event, health)
│       └── grpc_/              # gRPC service + proto definition
└── tests/
```
