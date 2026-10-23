import { t as tr } from "./i18n";
export interface UserAttachmentChip {
  kind: "file" | "folder" | "document" | "ide";
  label: string;
  detail?: string;
  title?: string;
}

export interface PresentedUserMessage {
  text: string;
  attachments: UserAttachmentChip[];
}

const ATTACHMENT_BLOCK =
  /<file\b([^>]*)>[\s\S]*?<\/file>|<folder>([\s\S]*?)<\/folder>|<document-reference>\s*文档：([^\r\n]*)\r?\n位置：([^\r\n]*)[\s\S]*?<\/document-reference>|<crabcode-ide-context>([\s\S]*?)<\/crabcode-ide-context>/g;

export function attachmentName(value: string): string {
  const trimmed = value.trim().replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).pop() || value.trim() || value;
}

function unescapeAttr(value: string): string {
  return value.replace(/&quot;/g, "\"").replace(/&#39;/g, "'").replace(/&amp;/g, "&");
}

function readAttr(source: string, name: string): string | undefined {
  const match = new RegExp(`(?:^|\\s)${name}="([^"]*)"`).exec(source);
  return match ? unescapeAttr(match[1]) : undefined;
}

function fileChip(attrs: string): UserAttachmentChip {
  const filePath = readAttr(attrs, "path");
  if (filePath) {
    return { kind: "file", label: attachmentName(filePath), detail: tr("仅路径"), title: filePath };
  }
  const name = readAttr(attrs, "name");
  return { kind: "file", label: name || tr("文件"), title: name };
}

function ideChips(body: string): UserAttachmentChip[] {
  try {
    const payload = JSON.parse(body.trim()) as {
      current_file?: { path?: unknown };
      references?: Array<{ kind?: unknown; path?: unknown }>;
    };
    const chips: UserAttachmentChip[] = [];
    if (typeof payload.current_file?.path === "string" && payload.current_file.path) {
      chips.push({
        kind: "ide",
        label: attachmentName(payload.current_file.path),
        detail: tr("当前文件"),
        title: payload.current_file.path,
      });
    }
    for (const reference of payload.references ?? []) {
      if (typeof reference.path !== "string" || !reference.path) continue;
      const folder = reference.kind === "folder";
      chips.push({
        kind: folder ? "folder" : "file",
        label: attachmentName(reference.path),
        ...(folder ? {} : { detail: tr("仅路径") }),
        title: reference.path,
      });
    }
    return chips.length ? chips : [{ kind: "ide", label: tr("IDE 上下文") }];
  } catch {
    return [{ kind: "ide", label: tr("IDE 上下文") }];
  }
}

/** Pull file, folder, document, and IDE references out of a user prompt. */
export function presentUserMessage(source: string): PresentedUserMessage {
  if (!source.includes("<")) return { text: source, attachments: [] };
  const attachments: UserAttachmentChip[] = [];
  const stripped = source.replace(ATTACHMENT_BLOCK, (match, fileAttrs: string, folderBody: string, docName: string, docLocation: string, ideBody: string) => {
    if (match.startsWith("<file")) {
      attachments.push(fileChip(fileAttrs ?? ""));
      return "";
    }
    if (match.startsWith("<folder")) {
      const folderPath = (folderBody ?? "").trim();
      if (folderPath) attachments.push({ kind: "folder", label: attachmentName(folderPath), title: folderPath });
      return "";
    }
    if (match.startsWith("<document-reference")) {
      const location = (docLocation ?? "").trim();
      attachments.push({
        kind: "document",
        label: (docName ?? "").trim() || tr("文档引用"),
        ...(location ? { detail: location } : {}),
      });
      return "";
    }
    if (match.startsWith("<crabcode-ide-context")) {
      attachments.push(...ideChips(ideBody ?? ""));
      return "";
    }
    return match;
  });
  if (!attachments.length) return { text: source, attachments: [] };
  return {
    text: stripped.replace(/[ \t]*\n(?:[ \t]*\n){2,}/g, "\n\n").trim(),
    attachments,
  };
}
