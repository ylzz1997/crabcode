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
    default: "按规则询问", ask: "按规则询问",
    bypassPermissions: "完全访问", run_everything: "完全访问",
    aiReview: "AI 审查", ai_review: "AI 审查",
    plan: "计划模式（只读）", acceptEdits: "自动允许编辑", dontAsk: "未授权则拒绝",
  };
  return mode ? labels[mode] ?? "未知权限模式" : "暂无法读取";
}

export function permissionPolicyText(policy: PermissionPolicyInfo | null | undefined) {
  if (!policy) return {
    inheritedLabel: "暂无法读取",
    inheritedDescription: "当前生效：暂无法读取，请连接支持权限详情的 Gateway",
    current: "当前生效：暂无法读取",
    rules: "",
    details: "",
    inheritedDanger: false,
    effectiveDanger: false,
  };
  const inheritedLabel = permissionModeLabel(policy.configured_mode);
  const isBypass = (mode: string) => mode === "bypassPermissions" || mode === "run_everything";
  const descriptions: Record<string, string> = {
    default: "按允许、询问、禁止规则执行；未匹配时只读工具自动放行，其他操作询问",
    ask: "按允许、询问、禁止规则执行；未匹配时只读工具自动放行，其他操作询问",
    bypassPermissions: "自动批准工具权限请求，跳过配置中的允许、询问和禁止规则",
    run_everything: "自动批准工具权限请求，跳过配置中的允许、询问和禁止规则",
    aiReview: "按工具规则执行；未匹配规则的常规操作交由 AI 审查",
    ai_review: "按工具规则执行；未匹配规则的常规操作交由 AI 审查",
    plan: "只允许只读操作，写入操作被禁止",
  };
  const rules = `规则：允许 ${policy.allow.length} · 询问 ${policy.ask.length} · 禁止 ${policy.deny.length}`;
  const details = (["deny", "allow", "ask"] as const).flatMap((kind) => {
    const label = { deny: "禁止", allow: "允许", ask: "询问" }[kind];
    return policy[kind].map((rule) =>
      `${label}：${rule.tool}${rule.path ? ` · 路径 ${rule.path}` : ""}${rule.command ? ` · 命令 ${rule.command}` : ""}`);
  });
  if (details.length) details.unshift("规则匹配顺序：禁止 → 允许 → 询问");
  if (policy.runtime_allow_count) details.push(`本会话另有 ${policy.runtime_allow_count} 项“始终允许”授权`);
  return {
    inheritedLabel,
    inheritedDescription: `当前生效：${inheritedLabel}。${descriptions[policy.configured_mode] ?? ""}`,
    current: `当前生效：${permissionModeLabel(policy.effective_mode)}。${descriptions[policy.effective_mode] ?? ""}`,
    rules,
    details: details.join("\n"),
    inheritedDanger: isBypass(policy.configured_mode),
    effectiveDanger: isBypass(policy.effective_mode),
  };
}
