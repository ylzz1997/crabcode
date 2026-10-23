import { t as tr } from "./i18n";
export interface PermissionRuleInfo {
  tool: string;
  path?: string | null;
  command?: string | null;
}

export interface PermissionPolicyInfo {
  configured_mode: string;
  effective_mode: string;
  allow: PermissionRuleInfo[];
  ask: PermissionRuleInfo[];
  deny: PermissionRuleInfo[];
  runtime_allow_count: number;
}

export function permissionModeLabel(mode: string | undefined): string {
  const labels: Record<string, string> = {
    default: tr("按规则询问"), ask: tr("按规则询问"),
    bypassPermissions: tr("完全访问"), run_everything: tr("完全访问"),
    aiReview: tr("AI 审查"), ai_review: tr("AI 审查"),
    plan: tr("计划模式（只读）"), acceptEdits: tr("自动允许编辑"), dontAsk: tr("未授权则拒绝"),
  };
  return mode ? labels[mode] ?? tr("未知权限模式") : tr("暂无法读取");
}

export function permissionPolicyText(policy: PermissionPolicyInfo | null | undefined) {
  if (!policy) return {
    inheritedLabel: tr("暂无法读取"),
    inheritedDescription: tr("当前生效：暂无法读取，请连接支持权限详情的 Gateway"),
    current: tr("当前生效：暂无法读取"),
    rules: "",
    details: "",
    inheritedDanger: false,
    effectiveDanger: false,
  };
  const inheritedLabel = permissionModeLabel(policy.configured_mode);
  const isBypass = (mode: string) => mode === "bypassPermissions" || mode === "run_everything";
  const descriptions: Record<string, string> = {
    default: tr("按允许、询问、禁止规则执行；未匹配时只读工具自动放行，其他操作询问"),
    ask: tr("按允许、询问、禁止规则执行；未匹配时只读工具自动放行，其他操作询问"),
    bypassPermissions: tr("自动批准工具权限请求，跳过配置中的允许、询问和禁止规则"),
    run_everything: tr("自动批准工具权限请求，跳过配置中的允许、询问和禁止规则"),
    aiReview: tr("按工具规则执行；未匹配规则的常规操作交由 AI 审查"),
    ai_review: tr("按工具规则执行；未匹配规则的常规操作交由 AI 审查"),
    plan: tr("只允许只读操作，写入操作被禁止"),
  };
  const rules = tr("规则：允许 {0} · 询问 {1} · 禁止 {2}", { 0: policy.allow.length, 1: policy.ask.length, 2: policy.deny.length });
  const details = (["deny", "allow", "ask"] as const).flatMap((kind) => {
    const label = { deny: tr("禁止"), allow: tr("允许"), ask: tr("询问") }[kind];
    return policy[kind].map((rule) =>
      `${label}：${rule.tool}${rule.path ? tr(" · 路径 {0}", { 0: rule.path }) : ""}${rule.command ? tr(" · 命令 {0}", { 0: rule.command }) : ""}`);
  });
  if (details.length) details.unshift(tr("规则匹配顺序：禁止 → 允许 → 询问"));
  if (policy.runtime_allow_count) details.push(tr("本会话另有 {0} 项“始终允许”授权", { 0: policy.runtime_allow_count }));
  return {
    inheritedLabel,
    inheritedDescription: tr("当前生效：{0}。{1}", { 0: inheritedLabel, 1: descriptions[policy.configured_mode] ?? "" }),
    current: tr("当前生效：{0}。{1}", { 0: permissionModeLabel(policy.effective_mode), 1: descriptions[policy.effective_mode] ?? "" }),
    rules,
    details: details.join("\n"),
    inheritedDanger: isBypass(policy.configured_mode),
    effectiveDanger: isBypass(policy.effective_mode),
  };
}
