import type { ChatItem, GatewayEvent, SessionViewState } from "./types";

function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return stringify(content);
  return content
    .map((block) => {
      if (!block || typeof block !== "object") return String(block);
      const value = block as Record<string, unknown>;
      if (typeof value.text === "string") return value.text;
      if (value.type === "tool_use") return `调用工具 ${String(value.name ?? "")}`;
      if (value.type === "tool_result") return stringify(value.content ?? value.result);
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function appendStream(items: ChatItem[], text: string, now: number): ChatItem[] {
  const last = items.at(-1);
  if (last?.kind === "assistant" && last.status === "running") {
    return [
      ...items.slice(0, -1),
      { ...last, text: `${last.text ?? ""}${text}` },
    ];
  }
  return [
    ...items,
    { id: crypto.randomUUID(), kind: "assistant", text, status: "running", startedAt: now },
  ];
}

function appendThinking(items: ChatItem[], text: string, now: number): ChatItem[] {
  const last = items.at(-1);
  if (last?.kind === "thinking" && last.status === "running") {
    return [
      ...items.slice(0, -1),
      { ...last, text: `${last.text ?? ""}${text}` },
    ];
  }
  return [
    ...items,
    {
      id: crypto.randomUUID(),
      kind: "thinking",
      title: "思考过程",
      text,
      status: "running",
      collapsed: false,
      startedAt: now,
    },
  ];
}

function updateByToolId(
  items: ChatItem[],
  toolUseId: string,
  updater: (item: ChatItem) => ChatItem,
): ChatItem[] {
  return items.map((item) => item.tool_use_id === toolUseId ? updater(item) : item);
}

function completeRunning(items: ChatItem[], now: number): ChatItem[] {
  return items.map((item) => {
    if (item.status !== "running") return item;
    const durationMs = item.startedAt ? Math.max(0, now - item.startedAt) : item.durationMs;
    return {
      ...item,
      status: "complete",
      completedAt: now,
      ...(durationMs === undefined ? {} : { durationMs }),
    };
  });
}

function completeItem(item: ChatItem, now: number): ChatItem {
  const durationMs = item.startedAt ? Math.max(0, now - item.startedAt) : item.durationMs;
  return {
    ...item,
    status: item.status === "pending" ? item.status : "complete",
    completedAt: now,
    ...(durationMs === undefined ? {} : { durationMs }),
  };
}

export function applyGatewayEvent(
  state: SessionViewState,
  event: GatewayEvent,
): SessionViewState {
  const now = Date.now();
  switch (event.type) {
    case "server.connected":
    case "server.heartbeat":
      return { ...state, connected: true, error: null };
    case "session_history": {
      const messages = (event.messages ?? []).map((message): ChatItem => ({
        id: String(message.uuid ?? crypto.randomUUID()),
        kind: message.role === "user" ? "user" : message.role === "assistant" ? "assistant" : "system",
        text: messageText(message.content),
        status: "complete",
      }));
      return {
        ...state,
        connected: true,
        busy: false,
        operationId: null,
        error: null,
        items: messages,
        runStartedAt: null,
        currentStep: null,
      };
    }
    case "stream_text":
      return {
        ...state,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: state.currentStep?.kind === "response"
          ? state.currentStep
          : { kind: "response", label: "生成回复", startedAt: now },
        items: appendStream(
          state.currentStep?.kind === "response" ? state.items : completeRunning(state.items, now),
          event.text ?? "",
          now,
        ),
      };
    case "thinking":
      return {
        ...state,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: state.currentStep?.kind === "thinking"
          ? state.currentStep
          : { kind: "thinking", label: "思考中", startedAt: now },
        items: appendThinking(
          state.currentStep?.kind === "thinking" ? state.items : completeRunning(state.items, now),
          event.text ?? "",
          now,
        ),
      };
    case "tool_use":
      return {
        ...state,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: { kind: "tool", label: event.tool_name ?? "执行工具", startedAt: now },
        items: [
          ...completeRunning(state.items, now),
          {
            id: event.tool_use_id ?? crypto.randomUUID(),
            kind: "tool",
            title: event.tool_name ?? "Tool",
            detail: event.tool_input ?? {},
            tool_use_id: event.tool_use_id,
            status: "running",
            collapsed: true,
            startedAt: now,
          },
        ],
      };
    case "tool_result":
      return {
        ...state,
        currentStep: { kind: "response", label: "整理结果", startedAt: now },
        items: updateByToolId(state.items, event.tool_use_id ?? "", (item) => ({
          ...completeItem(item, now),
          detail: event.result_for_display ?? event.result ?? "",
        })),
      };
    case "permission_request":
      return {
        ...state,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: { kind: "permission", label: "等待权限确认", startedAt: now },
        items: [
          ...state.items,
          {
            id: event.tool_use_id ?? crypto.randomUUID(),
            kind: "permission",
            title: `允许 ${event.tool_name ?? "工具"}？`,
            text: event.reason,
            detail: event.tool_input,
            tool_use_id: event.tool_use_id,
            agent_id: event.agent_id,
            status: "pending",
            startedAt: now,
          },
        ],
      };
    case "permission_response":
      return {
        ...state,
        currentStep: { kind: "response", label: "继续执行", startedAt: now },
        items: updateByToolId(state.items, event.tool_use_id ?? "", (item) => ({
          ...completeItem(item, now),
          status: event.allowed ? "allowed" : "denied",
        })),
      };
    case "choice_request":
      return {
        ...state,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: { kind: "choice", label: "等待选择", startedAt: now },
        items: [
          ...state.items,
          {
            id: event.tool_use_id ?? crypto.randomUUID(),
            kind: "choice",
            title: event.question ?? "请选择",
            tool_use_id: event.tool_use_id,
            options: event.options ?? [],
            multiple: Boolean(event.multiple),
            selected: [],
            status: "pending",
            startedAt: now,
          },
        ],
      };
    case "choice_response":
      return {
        ...state,
        currentStep: { kind: "response", label: "继续执行", startedAt: now },
        items: updateByToolId(state.items, event.tool_use_id ?? "", (item) => ({
          ...completeItem(item, now),
          selected: event.selected ?? [],
          status: "complete",
        })),
      };
    case "plan_ready":
      return {
        ...state,
        busy: false,
        runStartedAt: null,
        currentStep: null,
        items: [
          ...completeRunning(state.items, now),
          {
            id: crypto.randomUUID(),
            kind: "plan",
            title: "实施计划",
            detail: event.plan ?? {},
            status: "pending",
          },
        ],
      };
    case "file_change":
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: crypto.randomUUID(),
            kind: "file_change",
            title: event.path,
            path: event.path,
            action: event.action,
            diff: event.diff,
            status: "complete",
            collapsed: true,
          },
        ],
      };
    case "mode_change":
      return state.status
        ? { ...state, status: { ...state.status, mode: event.mode ?? state.status.mode } }
        : state;
    case "model_change":
      return state.status
        ? {
            ...state,
            status: {
              ...state.status,
              model_profile: event.model_profile ?? state.status.model_profile,
            },
          }
        : state;
    case "permission_mode_change":
      return state.status
        ? {
            ...state,
            status: {
              ...state.status,
              permission_mode: event.permission_mode ?? state.status.permission_mode,
            },
          }
        : state;
    case "error":
      return {
        ...state,
        error: event.message ?? "Gateway error",
        // Gateway guarantees a turn_complete boundary after foreground errors.
        // Keep live cards and timers running until that boundary arrives.
        items: event.command_error
          ? state.items
          : [
              ...state.items,
              { id: crypto.randomUUID(), kind: "error", text: event.message ?? "Gateway error" },
            ],
      };
    case "turn_complete":
      return {
        ...state,
        busy: false,
        operationId: null,
        runStartedAt: null,
        currentStep: null,
        lastTurnUsage: event.usage ?? state.lastTurnUsage ?? null,
        items: completeRunning(state.items, now),
        status: state.status && event.context_used_tokens !== undefined
          ? {
              ...state.status,
              context_used_tokens: event.context_used_tokens,
              context_window_tokens: event.context_window_tokens ?? state.status.context_window_tokens,
              context_remaining_tokens: event.context_remaining_tokens ?? state.status.context_remaining_tokens,
              context_used_percent: event.context_used_percent ?? state.status.context_used_percent,
            }
          : state.status,
      };
    default:
      return state;
  }
}
