export const SECTIONS = [
  {
    id: "general",
    title: "常规",
    icon: "settings",
    description: "让 CrabCode 按你的习惯工作",
  },
  {
    id: "models",
    title: "模型",
    icon: "model",
    description: "管理聊天模型、配置组与生效参数",
  },
  {
    id: "tools",
    title: "权限与工具",
    icon: "shield",
    description: "设置 Agent 的权限与运行工具",
  },
  {
    id: "context",
    title: "上下文压缩",
    icon: "context",
    description: "为长任务保留合适的上下文空间",
  },
  {
    id: "prompts",
    title: "提示词",
    icon: "prompt",
    description: "定制系统、压缩和用户提示词",
  },
  {
    id: "usage",
    title: "使用情况",
    icon: "chart",
    description: "查看当前 Gateway 记录的 Token 用量",
  },
] as const;
export type Section = (typeof SECTIONS)[number]["id"];
export type LocalScope = "user" | "workspace" | "folder";
export interface LocalDefinition {
  key: string;
  section: Section;
  title: string;
  description: string;
  default: unknown;
  options?: readonly (readonly [string, string])[];
  min?: number;
  max?: number;
  array?: boolean;
}
export const LOCAL_SETTINGS: LocalDefinition[] = [
  {
    key: "composerSendKey",
    section: "general",
    title: "发送快捷键",
    description: "Shift+Enter 始终换行；运行中 Enter 按跟进处理方式发送。",
    default: "enter",
    options: [
      ["enter", "Enter 发送"],
      ["mod_enter", "Ctrl / Cmd + Enter 发送"],
    ],
  },
  {
    key: "followUpMode",
    section: "general",
    title: "跟进处理方式",
    description:
      "运行中按 Enter 使用此方式；Ctrl / Cmd + Enter 对单条消息执行相反操作。",
    default: "queue",
    options: [
      ["queue", "排队"],
      ["steer", "引导"],
    ],
  },
  {
    key: "fileUploadMaxSizeMb",
    section: "general",
    title: "最大文件大小",
    description: "发送非图片文件完整内容时的单文件上限（MB）。",
    default: 5,
    min: 1,
    max: 100,
  },
  {
    key: "chatModelDefault",
    section: "models",
    title: "默认聊天模型",
    description: "填写模型名称；留空沿用当前模型列表的首项。",
    default: "",
  },
  {
    key: "chatModels",
    section: "models",
    title: "聊天可选模型",
    description: "每行一个模型名称；留空使用 Gateway 提供的全部模型。",
    default: [],
    array: true,
  },
  {
    key: "permissionMode",
    section: "tools",
    title: "默认权限模式",
    description: "控制工具审批方式。Computer Use 前台权限单独设置。",
    default: "default",
    options: [
      ["default", "跟随项目规则"],
      ["ask", "操作前询问"],
      ["ai_review", "AI 审查"],
      ["run_everything", "完全访问"],
    ],
  },
  {
    key: "computerUseTargetScope",
    section: "tools",
    title: "Computer Use 操作目标覆盖",
    description: "仅覆盖 VS Code 发起的操作；移除覆盖后跟随 Gateway。",
    default: "",
    options: [
      ["", "跟随 Gateway"],
      ["app_window", "应用窗口"],
      ["desktop", "整个桌面"],
    ],
  },
  {
    key: "computerUseDeliveryPolicy",
    section: "tools",
    title: "Computer Use 前台权限覆盖",
    description: "允许前台可能切换窗口并影响当前操作；更改从下一次发送起生效。",
    default: "",
    options: [
      ["", "跟随 Gateway"],
      ["strict_background", "严格后台"],
      ["allow_foreground", "允许前台"],
    ],
  },
  {
    key: "computerUseMode",
    section: "tools",
    title: "旧版 Computer Use 覆盖",
    description: "兼容旧配置。建议移除后使用上面的操作目标设置。",
    default: "",
    options: [
      ["", "跟随 Gateway"],
      ["background_app", "后台应用（旧版）"],
      ["foreground_desktop", "前台桌面（旧版）"],
    ],
  },
];
export const EXTENSION_SETTINGS_QUERY =
  "@ext:crabcode.crabcode @tag:crabcodeExtension";
export function validateLocalSetting(key: string, value: unknown): void {
  const spec = LOCAL_SETTINGS.find((item) => item.key === key);
  if (!spec) throw new Error("未知的 CrabCode 设置");
  if (value === undefined) return;
  if (spec.options && !spec.options.some((option) => option[0] === value))
    throw new Error("无效的设置选项");
  if (
    spec.min !== undefined &&
    (typeof value !== "number" ||
      !Number.isSafeInteger(value) ||
      value < spec.min ||
      value > spec.max!)
  )
    throw new Error(`请输入 ${spec.min}–${spec.max} 之间的整数`);
  if (
    spec.array &&
    (!Array.isArray(value) ||
      !value.every((item) => typeof item === "string" && item.trim()))
  )
    throw new Error("模型列表需要非空的模型名称");
  if (
    !spec.options &&
    !spec.array &&
    spec.min === undefined &&
    typeof value !== "string"
  )
    throw new Error("设置值必须是文本");
}
