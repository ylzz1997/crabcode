import type { LocalScope, Section } from "./catalog";
export type Resource = "runtime" | "models" | "prompts" | "usage";
export interface LocalValue {
  value: unknown;
  user?: unknown;
  workspace?: unknown;
  folder?: unknown;
}
export interface SettingsSnapshot {
  generation: number;
  cwd?: string;
  gateway: string;
  hasWorkspace: boolean;
  hasFolder: boolean;
  local: Record<string, LocalValue>;
}
export interface SettingsRequest {
  id: number;
  generation: number;
  type: "request";
  action:
    | "initialize"
    | "read"
    | "mutate"
    | "saveLocal"
    | "import"
    | "export"
    | "confirm"
    | "extension";
  resource?: Resource;
  mutation?: Record<string, unknown>;
  key?: string;
  value?: unknown;
  reset?: boolean;
  scope?: LocalScope;
  query?: Record<string, string>;
  source?: string;
  templates?: unknown;
  message?: string;
}
export type SettingsEvent =
  | {
      type: "response";
      id: number;
      generation: number;
      data?: unknown;
      error?: string;
    }
  | { type: "snapshot"; data: SettingsSnapshot }
  | { type: "navigate"; section: Section };
