import type { ChatItem, GatewayEvent, ImageAttachment, SessionViewState } from "./types";

function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function timestampMs(value: unknown): number | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function startsUserTurn(message: Record<string, unknown>): boolean {
  if (message.role !== "user") return false;
  if (typeof message.origin === "string" && message.origin) return false;
  if (!Array.isArray(message.content)) return true;
  return !message.content.some((block) => (
    block !== null
    && typeof block === "object"
    && (block as Record<string, unknown>).type === "tool_result"
  ));
}

function historyItems(messages: Array<Record<string, unknown>>): ChatItem[] {
  const items: ChatItem[] = [];
  const tools = new Map<string, number>();
  let turnStartedAt: number | null = null;
  let turnCompletedAt: number | null = null;
  let turnDurationId = "";

  const finishTurn = () => {
    if (turnStartedAt === null || turnCompletedAt === null) return;
    items.push({
      id: `${turnDurationId || crypto.randomUUID()}:turn-duration`,
      kind: "turn_duration",
      status: "complete",
      startedAt: turnStartedAt,
      completedAt: turnCompletedAt,
      durationMs: Math.max(0, turnCompletedAt - turnStartedAt),
    });
  };

  for (const message of messages) {
    // Synthetic task callbacks are model input. Their user-facing result is
    // the assistant reply that follows, so replaying the raw envelope leaks
    // protocol markup that was never shown in the live conversation.
    if (message.origin === "task-notification") {
      finishTurn();
      turnStartedAt = null;
      turnCompletedAt = null;
      turnDurationId = "";
      continue;
    }
    if (message.origin === "document-action") continue;

    const baseId = String(message.uuid ?? crypto.randomUUID());
    const messageTimestamp = timestampMs(message.timestamp);
    if (startsUserTurn(message)) {
      finishTurn();
      turnStartedAt = messageTimestamp;
      turnCompletedAt = null;
      turnDurationId = "";
    } else if (message.role === "assistant" && turnStartedAt !== null && messageTimestamp !== null) {
      turnCompletedAt = messageTimestamp;
      turnDurationId = baseId;
    }
    const kind = message.role === "user"
      ? "user"
      : message.role === "assistant"
        ? "assistant"
        : "system";
    const messageTiming = messageTimestamp === null
      ? {}
      : { startedAt: messageTimestamp, completedAt: messageTimestamp, durationMs: 0 };
    const content = message.content;
    if (typeof content === "string") {
      if (content) items.push({ id: baseId, kind, text: content, status: "complete", ...messageTiming });
      continue;
    }
    if (!Array.isArray(content)) continue;
    const hasToolResultBlock = content.some((rawBlock) => (
      rawBlock && typeof rawBlock === "object" && (rawBlock as Record<string, unknown>).type === "tool_result"
    ));

    let text = "";
    let images: ImageAttachment[] = [];
    let segment = 0;
    const flushText = () => {
      if (!text && images.length === 0) return;
      items.push({
        id: segment === 0 ? baseId : `${baseId}:part-${segment}`,
        kind,
        text,
        images: images.length > 0 ? images : undefined,
        status: "complete",
        ...messageTiming,
      });
      text = "";
      images = [];
      segment += 1;
    };

    content.forEach((rawBlock, index) => {
      if (!rawBlock || typeof rawBlock !== "object") return;
      const block = rawBlock as Record<string, unknown>;
      if (block.type === "text") {
        if (typeof block.text === "string") text += block.text;
        return;
      }
      if (block.type === "image") {
        if (hasToolResultBlock) return;
        const source = block.source;
        if (source && typeof source === "object" && typeof (source as Record<string, unknown>).data === "string") {
          images.push({
            media_type: typeof (source as Record<string, unknown>).media_type === "string"
              ? (source as Record<string, unknown>).media_type as string
              : "image/png",
            data: (source as Record<string, unknown>).data as string,
          });
        }
        return;
      }
      if (block.type === "thinking") {
        flushText();
        if (typeof block.thinking !== "string" || !block.thinking) return;
        items.push({
          id: `${baseId}:thinking-${index}`,
          kind: "thinking",
          title: "思考过程",
          text: block.thinking,
          status: "complete",
          collapsed: true,
          ...messageTiming,
        });
        return;
      }
      if (block.type === "tool_use") {
        flushText();
        const toolUseId = typeof block.id === "string" && block.id
          ? block.id
          : `${baseId}:tool-${index}`;
        const toolIndex = items.length;
        items.push({
          id: toolUseId,
          kind: "tool",
          title: typeof block.name === "string" ? block.name : "Tool",
          detail: block.input && typeof block.input === "object" ? block.input : {},
          input: block.input && typeof block.input === "object" && !Array.isArray(block.input)
            ? block.input as Record<string, unknown>
            : {},
          tool_use_id: toolUseId,
          status: "complete",
          collapsed: true,
          ...(messageTimestamp === null ? {} : { startedAt: messageTimestamp }),
        });
        tools.set(toolUseId, toolIndex);
        return;
      }
      if (block.type === "tool_result") {
        flushText();
        const toolUseId = typeof block.tool_use_id === "string" ? block.tool_use_id : "";
        if (!toolUseId) return;
        const result = stringify(block.content ?? block.result ?? "");
        const images = content
          .filter((rawImage) => rawImage && typeof rawImage === "object" && (rawImage as Record<string, unknown>).type === "image")
          .map((rawImage) => {
            const source = (rawImage as Record<string, unknown>).source;
            const record = source && typeof source === "object" ? source as Record<string, unknown> : {};
            return { media_type: typeof record.media_type === "string" ? record.media_type : "image/png", data: typeof record.data === "string" ? record.data : "" };
          })
          .filter((image) => image.data);
        const toolIndex = tools.get(toolUseId);
        if (toolIndex !== undefined) {
          items[toolIndex] = {
            ...items[toolIndex],
            detail: result,
            result,
            images,
            isError: block.is_error === true,
            status: "complete",
            collapsed: images.length === 0 && block.is_error !== true,
            ...(messageTimestamp === null ? {} : {
              completedAt: messageTimestamp,
              durationMs: items[toolIndex].startedAt === undefined
                ? 0
                : Math.max(0, messageTimestamp - items[toolIndex].startedAt!),
            }),
          };
        } else {
          tools.set(toolUseId, items.length);
          items.push({
            id: toolUseId,
            kind: "tool",
            title: "Tool",
            detail: result,
            input: {},
            result,
            isError: block.is_error === true,
            tool_use_id: toolUseId,
            status: "complete",
            collapsed: images.length === 0 && block.is_error !== true,
            ...(messageTimestamp === null ? {} : { completedAt: messageTimestamp, durationMs: 0 }),
          });
        }
      }
    });
    flushText();
  }

  finishTurn();

  return items;
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
  kind?: ChatItem["kind"],
): ChatItem[] {
  return items.map((item) => (
    item.tool_use_id === toolUseId && (kind === undefined || item.kind === kind)
      ? updater(item)
      : item
  ));
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

function appendTurnDuration(
  items: ChatItem[],
  startedAt: number | null | undefined,
  completedAt: number,
  operationId?: string,
): ChatItem[] {
  if (startedAt === null || startedAt === undefined) return items;
  return [
    ...items,
    {
      id: `${operationId || crypto.randomUUID()}:turn-duration`,
      kind: "turn_duration",
      status: "complete",
      startedAt,
      completedAt,
      durationMs: Math.max(0, completedAt - startedAt),
    },
  ];
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
      const messages = historyItems(event.messages ?? []);
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
            input: event.tool_input ?? {},
            tool_use_id: event.tool_use_id,
            status: "running",
            collapsed: true,
            startedAt: now,
          },
        ],
      };
    case "tool_result": {
      const toolUseId = event.tool_use_id ?? "";
      const result = event.result_for_display ?? event.result ?? "";
      const hasTool = state.items.some((item) => item.kind === "tool" && item.tool_use_id === toolUseId);
      const items = hasTool
        ? updateByToolId(state.items, toolUseId, (item) => ({
            ...completeItem(item, now),
            detail: result,
            result,
            images: event.images,
            isError: event.is_error ?? false,
            collapsed: (event.images?.length ?? 0) === 0,
          }), "tool")
        : [
            ...state.items,
            {
              id: toolUseId || crypto.randomUUID(),
              kind: "tool" as const,
              title: event.tool_name ?? "Tool",
              detail: result,
              input: event.tool_input ?? {},
              result,
              images: event.images,
              isError: event.is_error ?? false,
              tool_use_id: event.tool_use_id,
              status: "complete" as const,
              collapsed: (event.images?.length ?? 0) === 0,
              completedAt: now,
            },
          ];
      return {
        ...state,
        currentStep: { kind: "response", label: "整理结果", startedAt: now },
        items,
      };
    }
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
        }), "permission"),
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
        }), "choice"),
      };
    case "plan_ready":
      return {
        ...state,
        busy: false,
        runStartedAt: null,
        currentStep: null,
        items: appendTurnDuration([
          ...completeRunning(state.items, now),
          {
            id: crypto.randomUUID(),
            kind: "plan",
            title: "实施计划",
            detail: event.plan ?? {},
            status: "pending",
          },
        ], state.runStartedAt, now, event.operation_id),
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
    case "document_job": {
      const id = `${event.operation_id ?? event.action ?? "document"}:document-job`;
      const previous = state.items.find((item) => item.id === id);
      const status: NonNullable<ChatItem["status"]> = event.status === "completed"
        ? "complete"
        : event.status === "failed"
          ? "failed"
          : event.status === "cancelled"
            ? "cancelled"
            : event.status === "retrying"
              ? "retrying"
              : "running";
      const nextItem = {
        id,
        kind: "document_job" as const,
        title: event.action === "translate" ? "翻译文档" : "生成 Blog",
        text: event.message,
        action: event.action,
        locale: event.locale ?? previous?.locale,
        language: event.language ?? previous?.language,
        source: event.source ?? previous?.source,
        engine: event.engine ?? previous?.engine,
        current: event.current ?? previous?.current ?? 0,
        total: event.total ?? previous?.total ?? 0,
        status,
        startedAt: previous?.startedAt ?? now,
        ...(status === "complete" || status === "failed" || status === "cancelled"
          ? { completedAt: now }
          : {}),
      };
      const found = state.items.some((item) => item.id === id);
      return {
        ...state,
        operationId: event.operation_id ?? state.operationId,
        busy: status === "running" || status === "retrying" ? true : state.busy,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: status === "running" || status === "retrying"
          ? { kind: "document", label: nextItem.title, startedAt: nextItem.startedAt }
          : state.currentStep,
        items: found
          ? state.items.map((item) => item.id === id ? { ...item, ...nextItem } : item)
          : [...completeRunning(state.items, now), nextItem],
      };
    }
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
    case "error": {
      const documentItemId = event.operation_id ? `${event.operation_id}:document-job` : null;
      const documentCommandError = Boolean(
        event.command_error
        && documentItemId
        && state.items.some((item) => item.id === documentItemId),
      );
      const resumeCommandError = event.command_error && event.command === "resume_session";
      const staleForegroundCommandError = Boolean(
        event.command_error
        && event.operation_id
        && event.operation_id === state.operationId
        && (
          event.error_type === "operation_not_found"
          || event.error_type === "operation_inactive"
        )
      );
      const clearsTurn = documentCommandError || resumeCommandError || staleForegroundCommandError;
      return {
        ...state,
        error: event.message ?? "Gateway error",
        connected: resumeCommandError ? false : state.connected,
        busy: clearsTurn ? false : state.busy,
        operationId: clearsTurn ? null : state.operationId,
        runStartedAt: clearsTurn ? null : state.runStartedAt,
        currentStep: clearsTurn ? null : state.currentStep,
        // Gateway guarantees a turn_complete boundary after foreground errors.
        // Keep live cards and timers running until that boundary arrives.
        items: documentCommandError && documentItemId
          ? state.items.map((item) => item.id === documentItemId
            ? {
                ...item,
                text: event.message ?? "文档操作未能开始",
                status: "failed" as const,
                completedAt: now,
              }
            : item)
          : event.command_error
            ? state.items
          : [
              ...state.items,
              { id: crypto.randomUUID(), kind: "error", text: event.message ?? "Gateway error" },
            ],
      };
    }
    case "turn_complete": {
      const durableAssistantId = event.assistant_message_uuid;
      const runningAssistantIndex = state.items.reduce(
        (index, item, itemIndex) => (
          item.kind === "assistant" && item.status === "running" ? itemIndex : index
        ),
        -1,
      );
      const latestAssistantIndex = state.items.reduce(
        (index, item, itemIndex) => (item.kind === "assistant" ? itemIndex : index),
        -1,
      );
      const assistantIndex = runningAssistantIndex >= 0
        ? runningAssistantIndex
        : latestAssistantIndex;
      const completedItems = completeRunning(state.items, now).map((item, itemIndex) => {
        if (
          durableAssistantId
          && item.kind === "assistant"
          && itemIndex === assistantIndex
        ) {
          const partSuffix = item.id.match(/:part-\d+$/)?.[0] ?? "";
          return { ...item, id: `${durableAssistantId}${partSuffix}` };
        }
        return item;
      });
      return {
        ...state,
        busy: false,
        operationId: null,
        runStartedAt: null,
        currentStep: null,
        lastTurnUsage: event.usage ?? state.lastTurnUsage ?? null,
        items: appendTurnDuration(
          completedItems,
          state.runStartedAt,
          now,
          event.operation_id ?? state.operationId ?? undefined,
        ),
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
    }
    default:
      return state;
  }
}
