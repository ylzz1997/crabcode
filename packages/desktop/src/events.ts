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

function appendStream(items: ChatItem[], text: string): ChatItem[] {
  const last = items.at(-1);
  if (last?.kind === "assistant" && last.status === "running") {
    return [
      ...items.slice(0, -1),
      { ...last, text: `${last.text ?? ""}${text}` },
    ];
  }
  return [
    ...items,
    { id: crypto.randomUUID(), kind: "assistant", text, status: "running" },
  ];
}

function appendThinking(items: ChatItem[], text: string): ChatItem[] {
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

function completeRunning(items: ChatItem[]): ChatItem[] {
  return items.map((item) => item.status === "running" ? { ...item, status: "complete" } : item);
}

export function applyGatewayEvent(
  state: SessionViewState,
  event: GatewayEvent,
): SessionViewState {
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
      return { ...state, items: messages };
    }
    case "stream_text":
      return { ...state, busy: true, items: appendStream(state.items, event.text ?? "") };
    case "thinking":
      return { ...state, busy: true, items: appendThinking(state.items, event.text ?? "") };
    case "tool_use":
      return {
        ...state,
        busy: true,
        items: [
          ...completeRunning(state.items),
          {
            id: event.tool_use_id ?? crypto.randomUUID(),
            kind: "tool",
            title: event.tool_name ?? "Tool",
            detail: event.tool_input ?? {},
            tool_use_id: event.tool_use_id,
            status: "running",
            collapsed: true,
          },
        ],
      };
    case "tool_result":
      return {
        ...state,
        items: updateByToolId(state.items, event.tool_use_id ?? "", (item) => ({
          ...item,
          status: "complete",
          detail: event.result_for_display ?? event.result ?? "",
        })),
      };
    case "permission_request":
      return {
        ...state,
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
          },
        ],
      };
    case "permission_response":
      return {
        ...state,
        items: updateByToolId(state.items, event.tool_use_id ?? "", (item) => ({
          ...item,
          status: event.allowed ? "allowed" : "denied",
        })),
      };
    case "choice_request":
      return {
        ...state,
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
          },
        ],
      };
    case "choice_response":
      return {
        ...state,
        items: updateByToolId(state.items, event.tool_use_id ?? "", (item) => ({
          ...item,
          selected: event.selected ?? [],
          status: "complete",
        })),
      };
    case "plan_ready":
      return {
        ...state,
        busy: false,
        items: [
          ...completeRunning(state.items),
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
    case "error":
      return {
        ...state,
        busy: event.command_error ? state.busy : false,
        error: event.message ?? "Gateway error",
        items: [
          ...completeRunning(state.items),
          { id: crypto.randomUUID(), kind: "error", text: event.message ?? "Gateway error" },
        ],
      };
    case "turn_complete":
      return {
        ...state,
        busy: false,
        operationId: null,
        items: completeRunning(state.items),
        status: state.status && event.context_used_tokens !== undefined
          ? {
              ...state.status,
              context_used_tokens: event.context_used_tokens,
              context_window_tokens: event.context_window_tokens ?? state.status.context_window_tokens,
              context_used_percent: event.context_used_percent ?? state.status.context_used_percent,
            }
          : state.status,
      };
    default:
      return state;
  }
}
