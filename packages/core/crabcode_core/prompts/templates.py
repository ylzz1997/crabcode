"""Prompt text constants ported from Claude Code's src/constants/prompts.ts.

All text is kept verbatim to maintain behavioral parity.
"""

CYBER_RISK_INSTRUCTION = (
    "IMPORTANT: Assist with authorized security testing, defensive security, "
    "CTF challenges, and educational contexts. Refuse requests for destructive "
    "techniques, DoS attacks, mass targeting, supply chain compromise, or "
    "detection evasion for malicious purposes. Dual-use security tools (C2 "
    "frameworks, credential testing, exploit development) require clear "
    "authorization context: pentesting engagements, CTF competitions, security "
    "research, or defensive use cases."
)

DEFAULT_PREFIX = "You are CrabCode, an AI coding assistant in the terminal."

SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

FRONTIER_MODEL_NAME = "Claude Opus 4.6"

CLAUDE_MODEL_IDS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

SUMMARIZE_TOOL_RESULTS_SECTION = (
    "When working with tool results, write down any important information "
    "you might need later in your response, as the original tool result may "
    "be cleared later."
)

DEFAULT_AGENT_PROMPT = (
    "You are an agent for CrabCode, an AI coding assistant. Given the user's "
    "message, you should use the tools available to complete the task. Complete "
    "the task fully\u2014don't gold-plate, but don't leave it half-done. When "
    "you complete the task, respond with a concise report covering what was "
    "done and any key findings \u2014 the caller will relay this to the user, "
    "so it only needs the essentials."
)

TOOL_NAMES = {
    "bash": "Bash",
    "file_read": "Read",
    "file_edit": "Edit",
    "file_write": "Write",
    "glob": "Glob",
    "grep": "Grep",
    "lint": "Lint",
    "memory": "Memory",
    "agent": "Agent",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "browser": "Browser",
    "notebook_edit": "NotebookEdit",
    "todo_write": "TodoWrite",
    "checklist": "Checklist",
    "ask_user": "AskUserQuestion",
    "skill": "Skill",
    "codebase_search": "CodebaseSearch",
    "team_create": "TeamCreate",
    "team_spawn": "TeamSpawn",
    "team_message": "TeamMessage",
    "team_broadcast": "TeamBroadcast",
    "team_status": "TeamStatus",
    "team_task_add": "TeamTaskAdd",
    "team_task_claim": "TeamTaskClaim",
    "team_task_complete": "TeamTaskComplete",
    "team_shutdown": "TeamShutdown",
    "checkpoint": "Checkpoint",
    "revert": "Revert",
}
