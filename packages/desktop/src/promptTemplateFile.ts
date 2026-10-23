import { t as tr } from "./i18n";
export interface PortablePromptTemplate {
  id?: string;
  name: string;
  sections: Record<string, string>;
}

const NAME_LIMIT = 80;
const SECTION_LIMIT = 20_000;
const FILE_LIMIT = 40;
const TEXT_LIMIT = 12 * 1024 * 1024;

export function promptTemplateFilename(name: string): string {
  const stem = name
    .trim()
    .replace(/[\\/:*?"<>|\u0000]/g, " ")
    .replace(/\s+/g, " ")
    .replace(/[. ]+$/g, "")
    .trim()
    .slice(0, NAME_LIMIT);
  const safe = stem && !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(stem) ? stem : "prompt-template";
  return `${safe}.json`;
}

export function serializePromptTemplate(template: PortablePromptTemplate): string {
  return `${JSON.stringify(portableTemplate(template), null, 2)}\n`;
}

export function serializePromptTemplates(templates: PortablePromptTemplate[]): string {
  return `${JSON.stringify({ templates: templates.map(portableTemplate) }, null, 2)}\n`;
}

export function parsePromptTemplateFile(text: string): PortablePromptTemplate[] {
  const source = text.replace(/^\uFEFF/, "");
  if (source.length > TEXT_LIMIT) throw new Error(tr("JSON 文件过大"));
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new Error(tr("不是有效的 JSON"));
  }
  const items = unwrapPromptTemplates(parsed);
  if (items.length === 0) throw new Error(tr("JSON 里没有提示词模版"));
  if (items.length > FILE_LIMIT) throw new Error(tr("提示词模版数量已达上限"));
  const names = new Set<string>();
  const ids = new Set<string>();
  return items.map((item, index) => normalizePromptTemplate(item, index, names, ids));
}

function portableTemplate(template: PortablePromptTemplate): PortablePromptTemplate {
  const sections: Record<string, string> = {};
  for (const [key, value] of Object.entries(template.sections)) {
    if (typeof value !== "string") continue;
    const text = value.trim();
    if (text) sections[key] = text;
  }
  const stored: PortablePromptTemplate = { name: template.name.trim(), sections };
  const id = template.id?.trim();
  if (id) stored.id = id;
  return stored;
}

function unwrapPromptTemplates(parsed: unknown): unknown[] {
  if (Array.isArray(parsed)) return parsed;
  if (!parsed || typeof parsed !== "object") {
    throw new Error(tr("JSON 需要是模版对象、模版数组，或包含 templates 的对象"));
  }
  const record = parsed as Record<string, unknown>;
  if (Array.isArray(record.templates)) return record.templates;
  if (Array.isArray(record.prompt_templates)) return record.prompt_templates;
  if ("name" in record || "sections" in record) return [parsed];
  throw new Error(tr("JSON 需要是模版对象、模版数组，或包含 templates 的对象"));
}

function normalizePromptTemplate(
  item: unknown,
  index: number,
  names: Set<string>,
  ids: Set<string>,
): PortablePromptTemplate {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    throw new Error(tr("第 {0} 个模版不是对象", { 0: index + 1 }));
  }
  const record = item as Record<string, unknown>;
  const name = typeof record.name === "string" ? record.name.trim() : "";
  if (!name) throw new Error(tr("第 {0} 个模版缺少名称", { 0: index + 1 }));
  if (name === "默认") throw new Error(tr("模版名称不能是「默认」"));
  if (name.length > NAME_LIMIT) throw new Error(tr("模版「{0}」的名称过长", { 0: name.slice(0, 20) }));
  if (names.has(name)) throw new Error(tr("模版「{0}」在文件里重复", { 0: name }));
  names.add(name);
  let id: string | undefined;
  if (record.id != null && record.id !== "") {
    if (typeof record.id !== "string") throw new Error(tr("模版「{0}」的 id 无效", { 0: name }));
    id = record.id.trim();
    if (!id || id.length > NAME_LIMIT || ids.has(id)) throw new Error(tr("模版「{0}」的 id 无效", { 0: name }));
    ids.add(id);
  }
  const sections: Record<string, string> = {};
  if (record.sections != null && (typeof record.sections !== "object" || Array.isArray(record.sections))) {
    throw new Error(tr("模版「{0}」的 sections 必须是对象", { 0: name }));
  }
  if (record.sections && typeof record.sections === "object") {
    for (const [key, value] of Object.entries(record.sections)) {
      if (typeof value !== "string") continue;
      const text = value.trim();
      if (!text) continue;
      if (text.length > SECTION_LIMIT) throw new Error(tr("模版「{0}」的 {1} 过长", { 0: name, 1: key }));
      sections[key] = text;
    }
  }
  return id ? { id, name, sections } : { name, sections };
}
