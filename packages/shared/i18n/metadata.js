import { localized, getLocale } from './index.js';
// Localize only known Gateway UI metadata. Template names, prompt bodies and diagnostics are data.
const sources = localized({ userSettings: '用户配置', projectSettings: '项目配置', localSettings: '项目本地配置' });
const sections = localized({ native_system: '完整提示词', prefix: '身份前缀', intro: '介绍', system: '系统规则', doing_tasks: '任务执行', actions: '授权与安全', git_safety: 'Git 安全', using_tools: '工具使用', tone_and_style: '语气与风格', output_efficiency: '输出效率', session_guidance: '会话指引', agent_prompt: '子代理提示词', compact_prompt: '上下文压缩提示词', extra: '额外段落' });
export function configurationSourceLabel(source) { return sources[source.id] ?? source.label; }
export function promptSectionLabel(section) { return getLocale() === 'zh-CN' ? section.label : (sections[section.key] ?? section.label); }
