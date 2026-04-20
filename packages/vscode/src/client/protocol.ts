/**
 * Helper functions for building WebSocket command messages sent to the
 * CrabCode Gateway.
 *
 * Every command is a JSON object with a `type` discriminator that the
 * gateway routes to the appropriate handler (see
 * packages/gateway/crabcode_gateway/routes/event.py).
 */

import type {
  SendMessageRequest,
  PermissionResponseRequest,
  ChoiceResponseRequest,
  ContextPushRequest,
} from "./types";

// ── Command envelope types ────────────────────────────────────────

export interface SendMessageCommand {
  type: "send_message";
  text: string;
  max_turns: number;
  session_id: string | null;
}

export interface PermissionResponseCommand {
  type: "permission_response";
  tool_use_id: string;
  allowed: boolean;
  always_allow: boolean;
  agent_id: string | null;
}

export interface ChoiceResponseCommand {
  type: "choice_response";
  tool_use_id: string;
  selected: string[];
  cancelled: boolean;
  agent_id: string | null;
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

/** Union of all commands the client can send over the WebSocket. */
export type WsCommand =
  | SendMessageCommand
  | PermissionResponseCommand
  | ChoiceResponseCommand
  | PushContextCommand;

// ── Builder helpers ───────────────────────────────────────────────

/**
 * Build a `send_message` command to start a query loop on the server.
 */
export function buildSendMessageCommand(
  text: string,
  options: { maxTurns?: number; sessionId?: string } = {},
): SendMessageCommand {
  return {
    type: "send_message",
    text,
    max_turns: options.maxTurns ?? 0,
    session_id: options.sessionId ?? null,
  };
}

/**
 * Build a `permission_response` command to approve or deny a tool use.
 */
export function buildPermissionResponseCommand(
  toolUseId: string,
  allowed: boolean,
  options: { alwaysAllow?: boolean; agentId?: string } = {},
): PermissionResponseCommand {
  return {
    type: "permission_response",
    tool_use_id: toolUseId,
    allowed,
    always_allow: options.alwaysAllow ?? false,
    agent_id: options.agentId ?? null,
  };
}

/**
 * Build a `choice_response` command to answer a choice request.
 */
export function buildChoiceResponseCommand(
  toolUseId: string,
  selected: string[],
  options: { cancelled?: boolean; agentId?: string } = {},
): ChoiceResponseCommand {
  return {
    type: "choice_response",
    tool_use_id: toolUseId,
    selected,
    cancelled: options.cancelled ?? false,
    agent_id: options.agentId ?? null,
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
