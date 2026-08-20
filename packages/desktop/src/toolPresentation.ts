export type ToolKind =
  | "file"
  | "terminal"
  | "search"
  | "web"
  | "debug"
  | "memory"
  | "task"
  | "agent"
  | "message"
  | "checkpoint"
  | "checklist"
  | "goal"
  | "mode"
  | "team"
  | "schedule"
  | "skill"
  | "generic";

export type ToolFieldVariant = "text" | "code" | "path" | "list" | "json";

export interface ToolField {
  key: string;
  label: string;
  value: string;
  variant: ToolFieldVariant;
}

export interface ToolPresentation {
  known: boolean;
  kind: ToolKind;
  label: string;
  glyph: string;
  action: string | null;
  summary: string;
  fields: ToolField[];
}

export interface ChecklistResult {
  title: string;
  items: Array<{ text: string; checked: boolean }>;
  done: number;
  total: number;
}

type ToolDefinition = Pick<ToolPresentation, "kind" | "label" | "glyph">;

const DEFINITIONS: Record<string, ToolDefinition> = {
  read: { kind: "file", label: "读取文件", glyph: "R" },
  fileread: { kind: "file", label: "读取文件", glyph: "R" },
  edit: { kind: "file", label: "编辑文件", glyph: "E" },
  fileedit: { kind: "file", label: "编辑文件", glyph: "E" },
  write: { kind: "file", label: "写入文件", glyph: "W" },
  filewrite: { kind: "file", label: "写入文件", glyph: "W" },
  bash: { kind: "terminal", label: "运行命令", glyph: ">_" },
  lint: { kind: "terminal", label: "检查诊断", glyph: "!" },
  grep: { kind: "search", label: "搜索内容", glyph: "G" },
  glob: { kind: "search", label: "查找文件", glyph: "*" },
  codebasesearch: { kind: "search", label: "语义搜索", glyph: "S" },
  websearch: { kind: "web", label: "搜索网页", glyph: "↗" },
  browser: { kind: "web", label: "浏览器操作", glyph: "◎" },
  debugger: { kind: "debug", label: "调试程序", glyph: "D" },
  processdebugger: { kind: "debug", label: "进程调试", glyph: "P" },
  memory: { kind: "memory", label: "管理记忆", glyph: "M" },
  monitor: { kind: "task", label: "启动监控", glyph: "◉" },
  tasklist: { kind: "task", label: "查看后台任务", glyph: "≡" },
  taskstop: { kind: "task", label: "停止后台任务", glyph: "■" },
  agent: { kind: "agent", label: "启动 Agent", glyph: "A" },
  agentstatus: { kind: "agent", label: "查看 Agent", glyph: "A" },
  agentwait: { kind: "agent", label: "等待 Agent", glyph: "A" },
  agentcancel: { kind: "agent", label: "取消 Agent", glyph: "A" },
  agentsendinput: { kind: "agent", label: "向 Agent 发送输入", glyph: "A" },
  listagents: { kind: "message", label: "查看会话", glyph: "↔" },
  sendmessage: { kind: "message", label: "发送会话消息", glyph: "↗" },
  askuser: { kind: "message", label: "请求用户选择", glyph: "?" },
  checkpoint: { kind: "checkpoint", label: "创建检查点", glyph: "◆" },
  revert: { kind: "checkpoint", label: "回退检查点", glyph: "↶" },
  checklist: { kind: "checklist", label: "任务清单", glyph: "✓" },
  create_goal: { kind: "goal", label: "创建 Goal", glyph: "◎" },
  get_goal: { kind: "goal", label: "查看 Goal", glyph: "◎" },
  update_goal: { kind: "goal", label: "更新 Goal", glyph: "◎" },
  switchmode: { kind: "mode", label: "切换工作模式", glyph: "⇄" },
  teamcreate: { kind: "team", label: "创建 Team", glyph: "T" },
  teamspawn: { kind: "team", label: "添加 Team 成员", glyph: "T" },
  teammessage: { kind: "team", label: "发送 Team 消息", glyph: "T" },
  teambroadcast: { kind: "team", label: "广播 Team 消息", glyph: "T" },
  teamstatus: { kind: "team", label: "查看 Team", glyph: "T" },
  teamtaskadd: { kind: "team", label: "添加 Team 任务", glyph: "T" },
  teamtaskclaim: { kind: "team", label: "认领 Team 任务", glyph: "T" },
  teamtaskcomplete: { kind: "team", label: "完成 Team 任务", glyph: "T" },
  teamshutdown: { kind: "team", label: "关闭 Team", glyph: "T" },
  schedulecreate: { kind: "schedule", label: "创建定时任务", glyph: "◷" },
  schedulelist: { kind: "schedule", label: "查看定时任务", glyph: "◷" },
  schedulecancel: { kind: "schedule", label: "删除定时任务", glyph: "◷" },
  schedulestatus: { kind: "schedule", label: "查看任务状态", glyph: "◷" },
  schedulepause: { kind: "schedule", label: "暂停定时任务", glyph: "◷" },
  scheduleresume: { kind: "schedule", label: "恢复定时任务", glyph: "◷" },
  schedulerun: { kind: "schedule", label: "立即运行任务", glyph: "◷" },
  skill: { kind: "skill", label: "加载 Skill", glyph: "◇" },
};

const FIELD_LABELS: Record<string, string> = {
  action: "操作",
  file_path: "文件",
  path: "路径",
  target_file: "文件",
  target_directory: "目录",
  cwd: "工作目录",
  program: "程序",
  command: "命令",
  timeout: "超时",
  timeout_seconds: "超时",
  timeout_ms: "超时",
  offset: "起始行",
  limit: "行数",
  old_string: "替换前",
  new_string: "替换后",
  content: "内容",
  replace_all: "全部替换",
  pattern: "匹配模式",
  glob: "文件过滤",
  query: "查询",
  scope: "范围",
  num_results: "结果数",
  case_insensitive: "忽略大小写",
  url: "网址",
  selector: "选择器",
  script: "脚本",
  text: "消息",
  prompt: "任务",
  description: "说明",
  objective: "目标",
  token_budget: "Token 预算",
  status: "状态",
  target_mode: "目标模式",
  explanation: "原因",
  plan: "执行计划",
  session_id: "Session",
  tab_id: "标签页",
  headless: "无界面模式",
  wait_until: "等待条件",
  return_format: "返回格式",
  agent_id: "Agent",
  agent_ids: "Agents",
  task_id: "任务 ID",
  team_id: "Team",
  to: "接收方",
  role: "角色",
  name: "名称",
  model_profile: "模型",
  run_in_background: "后台运行",
  wait_any: "任一完成即返回",
  interrupt: "中断当前任务",
  checklist_id: "清单 ID",
  item: "清单项",
  items: "清单项",
  remove_items: "移除项",
  checkpoint_id: "检查点",
  label: "标签",
  schedule: "时间规则",
  schedule_type: "调度类型",
  enabled: "启用",
  max_runs: "最大次数",
  job_id: "任务 ID",
  run_limit: "运行记录数",
  next_run: "下次运行",
  tags: "标签",
  language: "语言",
  adapter_id: "调试适配器",
  paths: "检查路径",
  linter: "检查器",
  pid: "进程 ID",
  lines: "行号",
  thread_id: "线程",
  frame_id: "栈帧",
  expression: "表达式",
  args: "参数",
  levels: "栈深度",
  variables_reference: "变量引用",
  start: "起始位置",
  count: "数量",
  context: "上下文",
  terminate_debuggee: "终止被调试程序",
  launch_config: "启动配置",
  attach_config: "附加配置",
  duration_seconds: "持续时间",
  interval_seconds: "采样间隔",
  output_path: "输出文件",
  module_filter: "模块过滤",
  target_address: "目标地址",
  address: "地址",
  base_address: "基址",
  module_path: "模块路径",
  module_offset: "模块偏移",
  max_depth: "最大深度",
  max_offset: "最大偏移",
  offsets: "偏移链",
  pointer_size: "指针大小",
  align: "对齐",
  size: "大小",
  value_type: "值类型",
  value: "值",
  value_hex: "十六进制值",
  patch_hex: "补丁字节",
  expected_hex: "预期字节",
  patch_id: "补丁 ID",
  endian: "字节序",
  readable: "可读",
  writable: "可写",
  executable: "可执行",
  writable_only: "仅可写内存",
  executable_only: "仅可执行内存",
  max_results: "结果数",
  max_scan_bytes: "最大扫描字节",
  search_id: "搜索 ID",
  freeze_id: "冻结 ID",
  comparison: "比较方式",
  all: "全部",
  ws: "WebSocket",
  persistent: "持续监控",
  memory_id: "记忆 ID",
  max_teammates: "成员上限",
  extra: "附加配置",
  skill_name: "Skill",
  user_input: "用户要求",
  title: "标题",
};

const FIELD_ORDER: Partial<Record<ToolKind, string[]>> = {
  file: ["file_path", "path", "target_file", "offset", "limit", "replace_all", "old_string", "new_string", "content"],
  terminal: ["command", "paths", "linter", "file_path", "path", "language", "timeout"],
  search: ["query", "pattern", "path", "target_directory", "glob", "num_results", "case_insensitive"],
  web: ["action", "url", "selector", "text", "script", "path", "session_id", "tab_id", "headless", "wait_until", "return_format", "timeout_seconds", "options"],
  debug: ["action", "session_id", "program", "pid", "language", "path", "address", "base_address", "lines", "thread_id", "frame_id", "expression", "query", "pattern", "value", "value_hex", "patch_hex", "args", "cwd"],
  memory: ["action", "title", "query", "content", "memory_id", "id"],
  task: ["action", "task_id", "description", "command", "ws", "persistent", "interval", "timeout_ms", "timeout"],
  agent: ["agent_id", "agent_ids", "name", "description", "prompt", "subagent_type", "model_profile", "run_in_background", "wait_any", "interrupt", "timeout_seconds", "timeout_ms"],
  message: ["to", "from_name", "question", "options", "multiple", "text"],
  checkpoint: ["checkpoint_id", "label"],
  checklist: ["action", "title", "checklist_id", "item", "items", "remove_items"],
  goal: ["objective", "status", "token_budget"],
  mode: ["target_mode", "explanation", "plan"],
  team: ["team_id", "task_id", "name", "role", "to", "description", "prompt", "text", "result", "model_profile", "max_teammates"],
  schedule: ["job_id", "name", "status", "schedule_type", "schedule", "prompt", "description", "cwd", "enabled", "max_runs", "next_run", "run_limit", "tags", "timeout", "model_profile", "session_id", "extra"],
  skill: ["skill_name", "skill", "name", "user_input", "path", "args"],
};

const ACTION_LABELS: Record<string, string> = {
  create: "创建",
  update: "更新",
  delete: "删除",
  list: "查看列表",
  search: "搜索",
  read: "读取",
  check: "标记完成",
  uncheck: "取消完成",
  clear: "清空",
  launch: "启动",
  attach: "附加",
  navigate: "打开网页",
  click: "点击",
  type: "输入文本",
  extract: "提取内容",
  screenshot: "截图",
  evaluate: "执行脚本",
  pause: "暂停",
  resume: "继续",
  stop: "停止",
  status: "查看状态",
  create_session: "创建浏览器会话",
  goto: "打开网页",
  fill: "填写内容",
  press: "按键",
  wait_for: "等待元素",
  list_tabs: "查看标签页",
  new_tab: "新建标签页",
  switch_tab: "切换标签页",
  close_tab: "关闭标签页",
  close_session: "关闭浏览器会话",
  start: "启动调试",
  set_breakpoints: "设置断点",
  continue: "继续执行",
  step_over: "单步跳过",
  step_in: "单步进入",
  step_out: "单步跳出",
  threads: "查看线程",
  stack: "查看调用栈",
  scopes: "查看作用域",
  variables: "查看变量",
  events: "查看事件",
  list_processes: "查看进程",
  inspect_process: "检查进程",
  attach_debugger: "附加调试器",
  sample_stack: "采样调用栈",
  dump_core: "导出 Core Dump",
  memory_maps: "查看内存映射",
  memory_regions: "查看内存区域",
  memory_read: "读取内存",
  memory_search: "搜索内存",
  memory_refine: "筛选内存搜索",
  memory_write: "写入内存",
  memory_freeze: "冻结内存值",
  memory_unfreeze: "取消冻结",
  memory_freezes: "查看冻结项",
  aob_scan: "扫描字节特征",
  pointer_scan: "扫描指针",
  pointer_resolve: "解析指针",
  code_read: "读取机器码",
  code_patch: "修改机器码",
  code_restore: "恢复机器码",
  code_patches: "查看机器码补丁",
  trace_syscalls: "跟踪系统调用",
  detach: "断开调试器",
  terminate: "终止进程",
  kill: "强制结束进程",
};

function normalizedName(toolName: string): string {
  return toolName.trim().toLowerCase().replace(/[\s.-]/g, "");
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === null) return "null";
  if (Array.isArray(value)) {
    return value.every((item) => ["string", "number", "boolean"].includes(typeof item))
      ? value.map(String).join(" · ")
      : JSON.stringify(value, null, 2);
  }
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function fieldVariant(key: string, value: unknown): ToolFieldVariant {
  if (Array.isArray(value) && value.every((item) => ["string", "number", "boolean"].includes(typeof item))) return "list";
  if (value !== null && typeof value === "object") return "json";
  if (["file_path", "path", "target_file", "target_directory", "cwd", "program", "output_path", "module_path"].includes(key)) return "path";
  if (["command", "script", "old_string", "new_string", "content", "pattern", "expression"].includes(key)) return "code";
  return "text";
}

function orderedEntries(kind: ToolKind, input: Record<string, unknown>): Array<[string, unknown]> {
  const order = FIELD_ORDER[kind] ?? [];
  const rank = new Map(order.map((key, index) => [key, index]));
  return Object.entries(input)
    .filter(([, value]) => value !== undefined && value !== "" && value !== null)
    .sort(([left], [right]) => (rank.get(left) ?? 10_000) - (rank.get(right) ?? 10_000));
}

function summaryFor(kind: ToolKind, input: Record<string, unknown>, action: string | null): string {
  const keysByKind: Partial<Record<ToolKind, string[]>> = {
    file: ["file_path", "path", "target_file"],
    terminal: ["command", "paths", "linter", "file_path", "path"],
    search: ["query", "pattern", "glob"],
    web: ["url", "selector", "text", "path"],
    debug: ["program", "pid", "path", "expression", "session_id"],
    memory: ["title", "query", "content"],
    task: ["description", "command", "task_id"],
    agent: ["description", "name", "prompt", "agent_id", "agent_ids"],
    message: ["to", "text", "question"],
    checkpoint: ["label", "checkpoint_id"],
    checklist: ["title", "checklist_id", "item"],
    goal: ["objective", "status"],
    mode: ["target_mode", "explanation"],
    team: ["team_id", "name", "description", "to", "prompt"],
    schedule: ["name", "job_id", "schedule"],
    skill: ["skill_name", "skill", "name", "path"],
  };
  const key = (keysByKind[kind] ?? []).find((candidate) => input[candidate] !== undefined && input[candidate] !== "");
  const raw = key ? displayValue(input[key]) : "";
  const singleLine = raw.replace(/\s+/g, " ").trim();
  const shortened = singleLine.length > 120 ? `${singleLine.slice(0, 117)}…` : singleLine;
  return [action, shortened].filter(Boolean).join(" · ");
}

export function getToolPresentation(toolName: string, input: Record<string, unknown> = {}): ToolPresentation {
  const definition = DEFINITIONS[normalizedName(toolName)] ?? {
    kind: "generic" as const,
    label: toolName || "工具",
    glyph: "⚙",
  };
  const rawAction = typeof input.action === "string" ? input.action : null;
  const action = rawAction ? (ACTION_LABELS[rawAction.toLowerCase()] ?? rawAction) : null;
  return {
    ...definition,
    known: definition.kind !== "generic",
    action,
    summary: summaryFor(definition.kind, input, action),
    fields: orderedEntries(definition.kind, input).map(([key, value]) => ({
      key,
      label: FIELD_LABELS[key] ?? key.replaceAll("_", " "),
      value: displayValue(value),
      variant: fieldVariant(key, value),
    })),
  };
}

export function parseChecklistResult(value: unknown): ChecklistResult[] {
  if (typeof value !== "string") return [];
  const blocks: ChecklistResult[] = [];
  let current: ChecklistResult | null = null;
  for (const rawLine of value.split("\n")) {
    const line = rawLine.trim();
    const title = line.match(/^📋\s+(.+)$/);
    if (title) {
      if (current) blocks.push(current);
      current = { title: title[1], items: [], done: 0, total: 0 };
      continue;
    }
    const item = line.match(/^(✅|◻)\s+(\d+)\.\s+(.+)$/);
    if (item && current) {
      current.items.push({ text: item[3], checked: item[1] === "✅" });
      continue;
    }
    const progress = line.match(/^\((\d+)\/(\d+)\s+completed\)$/i);
    if (progress && current) {
      current.done = Number(progress[1]);
      current.total = Number(progress[2]);
    }
  }
  if (current) blocks.push(current);
  return blocks;
}
