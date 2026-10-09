import { fileEditSummaryItem, summarizeFileEdits } from "./fileEditSummary";
import type { ChatItem, GatewayEvent, ImageAttachment, SessionViewState } from "./types";
import { presentUserMessage } from "./userPromptDisplay";
import { randomUuid } from "./uuid";
import { computerUseDisplayResult } from "./toolPresentation";

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

function displayedUserText(kind: ChatItem["kind"], source: string): Pick<ChatItem, "text" | "attachments"> {
  if (kind !== "user") return { text: source };
  const presented = presentUserMessage(source);
  return {
    text: presented.text,
    ...(presented.attachments.length ? { attachments: presented.attachments } : {}),
  };
}

function historyItems(messages: Array<Record<string, unknown>>, cwd: string): ChatItem[] {
  const items: ChatItem[] = [];
  const tools = new Map<string, number>();
  let turnStartedAt: number | null = null;
  let turnCompletedAt: number | null = null;
  let turnDurationId = "";
  let summarizedUntil = 0;

  const finishTurn = () => {
    if (turnStartedAt === null || turnCompletedAt === null) return;
    const summary = summarizeFileEdits(items.slice(summarizedUntil), cwd);
    const summaryId = `${turnDurationId || randomUuid()}:file-edits`;
    if (summary) items.push(fileEditSummaryItem(summaryId, summary));
    items.push({
      id: `${turnDurationId || randomUuid()}:turn-duration`,
      kind: "turn_duration",
      status: "complete",
      startedAt: turnStartedAt,
      completedAt: turnCompletedAt,
      durationMs: Math.max(0, turnCompletedAt - turnStartedAt),
    });
    summarizedUntil = items.length;
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

    const baseId = String(message.uuid ?? randomUuid());
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
      if (content) items.push({ id: baseId, kind, status: "complete", ...messageTiming, ...displayedUserText(kind, content) });
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
      const displayed = displayedUserText(kind, text);
      if (!displayed.text && images.length === 0 && !displayed.attachments?.length) return;
      items.push({
        id: segment === 0 ? baseId : `${baseId}:part-${segment}`,
        kind,
        ...displayed,
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
            ...(typeof block.description === "string" && block.description ? { description: block.description } : {}),
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
        const toolIndex = tools.get(toolUseId);
        const rawResult = block.content ?? block.result ?? "";
        const result = computerUseDisplayResult(toolIndex === undefined ? "Tool" : items[toolIndex].title ?? "Tool", rawResult)
          ?? stringify(rawResult);
        const images = content
          .filter((rawImage) => rawImage && typeof rawImage === "object" && (rawImage as Record<string, unknown>).type === "image")
          .map((rawImage) => {
            const source = (rawImage as Record<string, unknown>).source;
            const record = source && typeof source === "object" ? source as Record<string, unknown> : {};
            const description = (rawImage as Record<string, unknown>).description;
            return {
              media_type: typeof record.media_type === "string" ? record.media_type : "image/png",
              data: typeof record.data === "string" ? record.data : "",
              ...(typeof description === "string" && description ? { description } : {}),
            };
          })
          .filter((image) => image.data);
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
            images,
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
    { id: randomUuid(), kind: "assistant", text, status: "running", startedAt: now },
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
      id: randomUuid(),
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

function currentTurnItems(items: ChatItem[]): ChatItem[] {
  let start = 0;
  items.forEach((item, index) => {
    if (item.kind === "turn_duration" || item.kind === "file_edit_summary") start = index + 1;
    else if (item.kind === "user") start = index;
  });
  return items.slice(start);
}

function closeTurn(
  items: ChatItem[],
  startedAt: number | null | undefined,
  completedAt: number,
  operationId: string | null | undefined,
  cwd: string,
): ChatItem[] {
  const summary = summarizeFileEdits(currentTurnItems(items), cwd);
  const withSummary = summary
    ? [...items, fileEditSummaryItem(`${operationId || "turn"}:${items.length}:file-edits`, summary)]
    : items;
  return appendTurnDuration(withSummary, startedAt, completedAt, operationId ?? undefined);
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
      id: `${operationId || randomUuid()}:turn-duration`,
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
    case "queued_message_updated": {
      if (state.operationId && event.operation_id && state.operationId !== event.operation_id) return state;
      const pending = state.pendingFollowUps ?? [];
      const entry = pending.find((item) => item.item.id === event.request_id);
      if (!entry) return state;
      if (event.action === "edit") {
        return { ...state, pendingFollowUps: pending.map((item) => item === entry
          ? { ...item, status: "editing", action: undefined } : item) };
      }
      if (event.action !== "remove" && event.action !== "steer") return state;
      return {
        ...state,
        pendingFollowUps: pending.filter((item) => item !== entry),
        items: event.action === "steer" ? [...state.items, entry.item] : state.items,
      };
    }
    case "queued_message_started": {
      if (state.operationId && event.operation_id && state.operationId !== event.operation_id) return state;
      const pending = state.pendingFollowUps ?? [];
      const index = event.request_id
        ? pending.findIndex((entry) => entry.item.id === event.request_id)
        : pending.findIndex((entry) => entry.status === "pending");
      const userItem: ChatItem = index >= 0 ? pending[index].item : {
        id: event.request_id ?? randomUuid(), kind: "user",
        ...displayedUserText("user", event.text ?? ""), images: event.images,
      };
      return {
        ...state, busy: true, operationId: event.operation_id ?? state.operationId,
        runStartedAt: now,
        currentStep: { kind: "response", label: "执行排队消息", startedAt: now },
        pendingFollowUps: pending.filter((_, itemIndex) => itemIndex !== index),
        items: [...closeTurn(completeRunning(state.items, now), state.runStartedAt, now, state.operationId, state.cwd), userItem],
      };
    }
    case "server.connected":
    case "server.heartbeat":
      return { ...state, connected: true, error: null };
    case "session_history": {
      const messages = historyItems(event.messages ?? [], state.cwd);
      return {
        ...state,
        loading: false,
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
        error: null,
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
        error: null,
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
    case "stream_retry":
      if (event.agent_id) return state;
      return {
        ...state,
        error: null,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: {
          kind: "retry",
          label: event.message ?? "Reconnecting...",
          startedAt: now,
        },
        items: completeRunning(state.items, now),
      };
    case "tool_use":
      return {
        ...state,
        error: null,
        busy: true,
        runStartedAt: state.runStartedAt ?? now,
        currentStep: { kind: "tool", label: event.tool_name ?? "执行工具", startedAt: now },
        items: [
          ...completeRunning(state.items, now),
          {
            id: event.tool_use_id ?? randomUuid(),
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
      const toolName = event.tool_name ?? state.items.find((item) => item.kind === "tool" && item.tool_use_id === toolUseId)?.title ?? "Tool";
      const result = computerUseDisplayResult(toolName, event.result)
        ?? event.result_for_display ?? event.result ?? "";
      const hasTool = state.items.some((item) => item.kind === "tool" && item.tool_use_id === toolUseId);
      const items = hasTool
        ? updateByToolId(state.items, toolUseId, (item) => ({
            ...completeItem(item, now),
            detail: result,
            input: event.tool_input ?? item.input,
            result,
            images: event.images,
            isError: event.is_error ?? false,
            collapsed: (event.images?.length ?? 0) === 0,
          }), "tool")
        : [
            ...state.items,
            {
              id: toolUseId || randomUuid(),
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
            id: event.tool_use_id ?? randomUuid(),
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
            id: event.tool_use_id ?? randomUuid(),
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
        items: closeTurn([
          ...completeRunning(state.items, now),
          {
            id: randomUuid(),
            kind: "plan",
            title: "实施计划",
            detail: event.plan ?? {},
            status: "pending",
          },
        ], state.runStartedAt, now, event.operation_id, state.cwd),
      };
    case "file_change":
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: randomUuid(),
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
        pendingFollowUps: state.pendingFollowUps?.map((entry) => (
          (clearsTurn && entry.status === "pending") || (event.command_error && event.command === "queue_message"
            && (!event.request_id || entry.item.id === event.request_id))
            ? { ...entry, status: "cancelled", action: undefined }
            : event.command_error && event.command === "queued_message_action" && entry.item.id === event.request_id
              ? { ...entry, action: undefined } : entry
        )),
        loading: false,
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
              { id: randomUuid(), kind: "error", text: event.message ?? "Gateway error" },
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
        pendingFollowUps: state.pendingFollowUps?.map((entry) => entry.status === "pending"
          ? { ...entry, status: "cancelled", action: undefined } : entry),
        operationId: null,
        runStartedAt: null,
        currentStep: null,
        lastTurnUsage: event.usage ?? state.lastTurnUsage ?? null,
        items: closeTurn(
          completedItems,
          state.runStartedAt,
          now,
          event.operation_id ?? state.operationId,
          state.cwd,
        ),
        status: state.status && event.context_used_tokens !== undefined
          ? {
              ...state.status,
              context_used_tokens: event.context_used_tokens,
              context_token_source: event.context_token_source ?? "estimated",
              context_window_tokens: event.context_window_tokens ?? state.status.context_window_tokens,
              context_remaining_tokens: event.context_remaining_tokens ?? state.status.context_remaining_tokens,
              context_used_percent: event.context_used_percent ?? state.status.context_used_percent,
              prompt_budget: event.prompt_budget ?? state.status.prompt_budget,
            }
          : state.status,
      };
    }
    default:
      return state;
  }
}
