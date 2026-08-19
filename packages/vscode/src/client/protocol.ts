/**
 * Helper functions for building WebSocket command messages sent to the
 * CrabCode Gateway.
 *
 * Every command is a JSON object with a `type` discriminator that the
 * gateway routes to the appropriate handler (see
 * packages/gateway/crabcode_gateway/routes/event.py).
 */

import type {
  InterruptRequest,
  NewSessionRequest,
  ResumeSessionRequest,
  SendMessageRequest,
  PermissionResponseRequest,
  ChoiceResponseRequest,
  ContextPushRequest,
  ImageAttachment,
  SetReasoningEffortRequest,
  SetUltraModeRequest,
} from "./types";

// ── Command envelope types ────────────────────────────────────────

export interface SendMessageCommand {
  type: "send_message";
  text: string;
  max_turns: number;
  session_id: string | null;
  operation_id?: string;
  images?: ImageAttachment[];
}

/** Add user guidance to the foreground turn at its next safe tool boundary. */
export interface SteerMessageCommand {
  type: "steer_message";
  text: string;
  session_id: string | null;
  operation_id?: string;
  images?: ImageAttachment[];
}

export interface PermissionResponseCommand {
  type: "permission_response";
  tool_use_id: string;
  allowed: boolean;
  always_allow: boolean;
  agent_id: string | null;
  feedback: string | null;
  session_id: string | null;
}

export interface ChoiceResponseCommand {
  type: "choice_response";
  tool_use_id: string;
  selected: string[];
  cancelled: boolean;
  agent_id: string | null;
  session_id: string | null;
}

export interface PushContextCommand {
  type: "push_context";
  session_id: string;
  active_file: string | null;
  selected_text: string | null;
  cursor_line: number | null;
  cursor_column: number | null;
  open_files: string[];
  language_id: string | null;
}

export interface SwitchModelCommand {
  type: "switch_model";
  name: string;
  session_id: string | null;
}

export interface NewSessionCommand extends NewSessionRequest {
  type: "new_session";
}

export interface ResumeSessionCommand extends ResumeSessionRequest {
  type: "resume_session";
}

export interface InterruptCommand extends InterruptRequest {
  type: "interrupt";
}

export interface SwitchModeCommand {
  type: "switch_mode";
  mode: "agent" | "plan";
  session_id: string | null;
}

export interface SetReasoningEffortCommand {
  type: "set_reasoning_effort";
  effort: SetReasoningEffortRequest["effort"];
  session_id: string | null;
}

export interface SetUltraModeCommand {
  type: "set_ultra_mode";
  enabled: boolean | null;
  session_id: string | null;
}

export type PermissionMode = "default" | "ask" | "run_everything" | "ai_review";

export interface SetPermissionModeCommand {
  type: "set_permission_mode";
  mode: PermissionMode;
  session_id: string | null;
}

export interface PlanActionCommand {
  type: "plan_action";
  action: "execute" | "revise" | "cancel";
  session_id: string | null;
  operation_id?: string;
  plan?: Record<string, unknown>;
}

/** Union of all commands the client can send over the WebSocket. */
export type WsCommand =
  | SendMessageCommand
  | SteerMessageCommand
  | NewSessionCommand
  | ResumeSessionCommand
  | InterruptCommand
  | PermissionResponseCommand
  | ChoiceResponseCommand
  | PushContextCommand
  | SwitchModelCommand
  | SwitchModeCommand
  | SetReasoningEffortCommand
  | SetUltraModeCommand
  | SetPermissionModeCommand
  | PlanActionCommand;

// ── Builder helpers ───────────────────────────────────────────────

/**
 * Build a `send_message` command to start a query loop on the server.
 */
export function buildSendMessageCommand(
  text: string,
  options: {
    maxTurns?: number;
    sessionId?: string;
    operationId?: string;
    images?: ImageAttachment[];
  } = {},
): SendMessageCommand {
  const cmd: SendMessageCommand = {
    type: "send_message",
    text,
    max_turns: options.maxTurns ?? 0,
    session_id: options.sessionId ?? null,
  };
  if (options.operationId) cmd.operation_id = options.operationId;
  if (options.images && options.images.length > 0) {
    cmd.images = options.images;
  }
  return cmd;
}

/** Build a message that steers an already-running foreground turn. */
export function buildSteerMessageCommand(
  text: string,
  options: { sessionId?: string; operationId?: string; images?: ImageAttachment[] } = {},
): SteerMessageCommand {
  const cmd: SteerMessageCommand = {
    type: "steer_message",
    text,
    session_id: options.sessionId ?? null,
  };
  if (options.operationId) cmd.operation_id = options.operationId;
  if (options.images && options.images.length > 0) {
    cmd.images = options.images;
  }
  return cmd;
}

/**
 * Build a `permission_response` command to approve or deny a tool use.
 */
export function buildPermissionResponseCommand(
  toolUseId: string,
  allowed: boolean,
  options: {
    alwaysAllow?: boolean;
    agentId?: string;
    feedback?: string;
    sessionId?: string;
  } = {},
): PermissionResponseCommand {
  return {
    type: "permission_response",
    tool_use_id: toolUseId,
    allowed,
    always_allow: options.alwaysAllow ?? false,
    agent_id: options.agentId ?? null,
    feedback: options.feedback ?? null,
    session_id: options.sessionId ?? null,
  };
}

/**
 * Build a `choice_response` command to answer a choice request.
 */
export function buildChoiceResponseCommand(
  toolUseId: string,
  selected: string[],
  options: { cancelled?: boolean; agentId?: string; sessionId?: string } = {},
): ChoiceResponseCommand {
  return {
    type: "choice_response",
    tool_use_id: toolUseId,
    selected,
    cancelled: options.cancelled ?? false,
    agent_id: options.agentId ?? null,
    session_id: options.sessionId ?? null,
  };
}

/**
 * Build a `push_context` command to push editor context to the server.
 */
export function buildPushContextCommand(
  context: ContextPushRequest,
): PushContextCommand {
  return {
    type: "push_context",
    session_id: context.session_id,
    active_file: context.active_file ?? null,
    selected_text: context.selected_text ?? null,
    cursor_line: context.cursor_line ?? null,
    cursor_column: context.cursor_column ?? null,
    open_files: context.open_files ?? [],
    language_id: context.language_id ?? null,
  };
}

/**
 * Serialize a command to a JSON string ready for `ws.send()`.
 */
export function serializeCommand(cmd: WsCommand): string {
  return JSON.stringify(cmd);
}

export function buildSwitchModelCommand(
  name: string,
  sessionId?: string,
): SwitchModelCommand {
  return { type: "switch_model", name, session_id: sessionId ?? null };
}

export function buildSetPermissionModeCommand(
  mode: PermissionMode,
  sessionId?: string,
): SetPermissionModeCommand {
  return { type: "set_permission_mode", mode, session_id: sessionId ?? null };
}

export function buildNewSessionCommand(
  cwd?: string | null,
  overrides: Omit<NewSessionRequest, "cwd"> = {},
): NewSessionCommand {
  return { type: "new_session", cwd: cwd ?? null, ...overrides };
}

export function buildResumeSessionCommand(
  sessionId: string,
  overrides: Omit<ResumeSessionRequest, "session_id"> = {},
): ResumeSessionCommand {
  return { type: "resume_session", session_id: sessionId, ...overrides };
}

export function buildInterruptCommand(
  sessionId: string,
  operationId?: string,
): InterruptCommand {
  return {
    type: "interrupt",
    session_id: sessionId,
    operation_id: operationId ?? null,
  };
}

export function buildSwitchModeCommand(
  mode: SwitchModeCommand["mode"],
  sessionId?: string,
): SwitchModeCommand {
  return { type: "switch_mode", mode, session_id: sessionId ?? null };
}

export function buildSetReasoningEffortCommand(
  effort: SetReasoningEffortCommand["effort"],
  sessionId?: string,
): SetReasoningEffortCommand {
  return {
    type: "set_reasoning_effort",
    effort,
    session_id: sessionId ?? null,
  };
}

export function buildSetUltraModeCommand(
  enabled: boolean | null = null,
  sessionId?: string,
): SetUltraModeCommand {
  return { type: "set_ultra_mode", enabled, session_id: sessionId ?? null };
}

export function buildPlanActionCommand(
  action: PlanActionCommand["action"],
  plan?: Record<string, unknown>,
  sessionId?: string,
  operationId?: string,
): PlanActionCommand {
  const cmd: PlanActionCommand = {
    type: "plan_action",
    action,
    session_id: sessionId ?? null,
  };
  if (operationId) cmd.operation_id = operationId;
  if (plan) cmd.plan = plan;
  return cmd;
}
