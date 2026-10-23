export interface SlashCommandAction {
  type: string;
  [key: string]: unknown;
}
export interface SlashCommandDefinition {
  name: string;
  desc: string;
  badge: string;
}
export function createSlashCommands(context: {
  t?: (source: string, values?: Record<string | number, unknown>) => string;
  postMessage: (message: SlashCommandAction) => void;
  showMessage: (text: string) => void;
  shellCommandHelp?: string;
  skills?: Array<{ name: string; description?: string }>;
}): {
  commands: SlashCommandDefinition[];
  handlers: Record<string, (args: string) => boolean>;
};
