import { FileText, Folder, Image as ImageIcon, Quote } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import type { ComposerSendKey } from "./types";

export type ComposerReferenceKind = "image" | "file" | "folder" | "document";

export type ComposerReferenceOption = {
  key: string;
  kind: ComposerReferenceKind;
  label: string;
  detail: string;
};

export type ComposerCommandOption = {
  name: string;
  description: string;
  kind: "command" | "skill" | "subcommand";
  children?: ComposerCommandOption[];
};

type SlashCompletion = {
  title: string;
  query: string;
  parent: ComposerCommandOption | null;
  items: ComposerCommandOption[];
};

const BUILTIN_COMMANDS: Array<Omit<ComposerCommandOption, "kind" | "children">> = [
  { name: "/help", description: "显示帮助" },
  { name: "/plan", description: "切换到计划模式（只读分析）" },
  { name: "/agent", description: "切换到 Agent 模式" },
  { name: "/plan-status", description: "显示当前计划状态" },
  { name: "/agents", description: "列出托管的 Agent" },
  { name: "/agent-log", description: "查看 Agent transcript" },
  { name: "/agent-send", description: "向 Agent 追加输入" },
  { name: "/wait", description: "等待 Agent 完成" },
  { name: "/cancel-agent", description: "取消 Agent" },
  { name: "/spawn-agent", description: "启动后台 Agent（支持类型/名称/模型）" },
  { name: "/goal", description: "设置或管理持久 Goal" },
  { name: "/tasks", description: "列出、查看、停止后台任务" },
  { name: "/peers", description: "列出其他 CrabCode 会话" },
  { name: "/peer-send", description: "向其他会话发送消息" },
  { name: "/status", description: "显示会话状态" },
  { name: "/effort", description: "查看/设置推理强度" },
  { name: "/ultra", description: "切换/设置 Ultra mode" },
  { name: "/model", description: "显示/切换模型" },
  { name: "/new", description: "开始新会话" },
  { name: "/compact", description: "压缩对话上下文" },
  { name: "/clear", description: "清除历史记录" },
  { name: "/sessions", description: "列出所有会话" },
  { name: "/recent", description: "列出最近的会话" },
  { name: "/search", description: "搜索会话" },
  { name: "/archive", description: "归档会话" },
  { name: "/prune", description: "归档/清理过期会话" },
  { name: "/export", description: "导出会话（md/json，可指定路径）" },
  { name: "/stats", description: "使用统计" },
  { name: "/checkpoint", description: "创建检查点（含文件快照）" },
  { name: "/checkpoints", description: "列出检查点" },
  { name: "/rollback", description: "回滚对话到检查点" },
  { name: "/revert", description: "还原文件和对话到检查点" },
  { name: "/undo", description: "撤销最后一个检查点" },
  { name: "/resume", description: "恢复会话" },
  { name: "/logs", description: "显示后台日志" },
  { name: "/team", description: "团队管理（创建/协作/任务板）" },
  { name: "/schedule", description: "定时任务管理（创建/执行/历史）" },
  { name: "/image", description: "附加图片到下一条消息" },
];

function subcommands(items: Array<[string, string]>): ComposerCommandOption[] {
  return items.map(([name, description]) => ({ name, description, kind: "subcommand" }));
}

const STATIC_SUBCOMMANDS: Record<string, ComposerCommandOption[]> = {
  "/tasks": subcommands([
    ["list", "列出后台任务"], ["show", "查看任务详情"], ["output", "查看任务输出"], ["stop", "停止后台任务"],
  ]),
  "/team": subcommands([
    ["list", "列出 Team"], ["create", "创建 Team"], ["status", "查看 Team 状态"],
    ["messages", "查看 Team 消息"], ["tasks", "查看 Team 任务板"], ["spawn", "添加 teammate"],
    ["remove", "移除 teammate"], ["message", "发送 Team 消息"], ["broadcast", "广播 Team 消息"],
    ["mark-read", "标记 Team 消息已读"], ["task-add", "添加 Team 任务"], ["task-claim", "认领 Team 任务"],
    ["task-complete", "完成 Team 任务"], ["task-fail", "将 Team 任务标记失败"],
    ["bridge", "设置跨 Team Bridge"], ["bridge-status", "查看跨 Team Bridge"],
    ["cross-message", "发送跨 Team 消息"], ["shutdown", "关闭 Team"],
  ]),
  "/schedule": subcommands([
    ["list", "列出定时任务"], ["show", "查看定时任务详情"], ["runs", "查看定时任务执行历史"],
    ["create", "创建定时任务"], ["pause", "暂停定时任务"], ["resume", "恢复定时任务"],
    ["run", "立即执行定时任务"], ["cancel", "删除定时任务"],
  ]),
};

export function createComposerCommandOptions(
  models: Array<{ name: string; description?: string }>,
  skills: Array<{ name: string; description?: string }>,
  supportedCommandNames?: ReadonlySet<string>,
): ComposerCommandOption[] {
  const builtins = BUILTIN_COMMANDS
    .filter((command) => !supportedCommandNames || supportedCommandNames.has(command.name))
    .map((command) => ({
      ...command,
      kind: "command" as const,
      children: command.name === "/model"
        ? models.map((model) => ({
          name: model.name,
          description: model.description || model.name,
          kind: "subcommand" as const,
        }))
        : STATIC_SUBCOMMANDS[command.name],
    }));
  const builtinNames = new Set(builtins.map((command) => command.name.slice(1).toLocaleLowerCase()));
  return [
    ...builtins,
    ...skills
      .filter((skill) => !builtinNames.has(skill.name.toLocaleLowerCase()))
      .map((skill) => ({
        name: `/${skill.name}`,
        description: skill.description || "Skill",
        kind: "skill" as const,
      })),
  ];
}

export function resolveComposerCommandCompletion(
  text: string,
  commands: ComposerCommandOption[],
): SlashCompletion | null {
  if (!text.startsWith("/") || text.includes("\n")) return null;
  const spaceIndex = text.indexOf(" ");
  if (spaceIndex < 0) {
    const query = text.slice(1).toLocaleLowerCase();
    const items = commands.filter((command) => (
      command.name.slice(1).toLocaleLowerCase().startsWith(query)
      || command.description.toLocaleLowerCase().includes(query)
    ));
    return items.length > 0 ? { title: "快捷命令", query, parent: null, items } : null;
  }
  const commandName = text.slice(0, spaceIndex).toLocaleLowerCase();
  const parent = commands.find((command) => command.name.toLocaleLowerCase() === commandName);
  if (!parent?.children?.length) return null;
  const query = text.slice(spaceIndex + 1).toLocaleLowerCase();
  const items = parent.children.filter((child) => child.name.toLocaleLowerCase().startsWith(query));
  return items.length > 0 ? { title: parent.name, query, parent, items } : null;
}

type MentionTrigger = {
  query: string;
  range: Range;
};

function referenceIcon(kind: ComposerReferenceKind) {
  if (kind === "image") return <ImageIcon />;
  if (kind === "folder") return <Folder />;
  if (kind === "document") return <Quote />;
  return <FileText />;
}

function isMention(node: Node | null): node is HTMLElement {
  return node instanceof HTMLElement && node.classList.contains("composer-inline-mention");
}

function nodeText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent ?? "").replace(/\u200b/g, "");
  if (!(node instanceof HTMLElement)) return "";
  if (node.classList.contains("composer-inline-mention")) return `@${node.dataset.mentionLabel ?? "引用"}`;
  if (node.tagName === "BR") return "\n";
  const text = Array.from(node.childNodes).map(nodeText).join("");
  return (node.tagName === "DIV" || node.tagName === "P") ? `${text}\n` : text;
}

export function extractComposerText(root: HTMLElement): string {
  return Array.from(root.childNodes).map(nodeText).join("").replace(/\n+$/, "");
}

function findMentionTrigger(root: HTMLElement): MentionTrigger | null {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || !selection.isCollapsed) return null;
  const caret = selection.getRangeAt(0);
  if (!root.contains(caret.startContainer) || caret.startContainer.nodeType !== Node.TEXT_NODE) return null;
  const textNode = caret.startContainer as Text;
  const beforeCaret = textNode.data.slice(0, caret.startOffset).replace(/\u200b/g, "");
  const match = beforeCaret.match(/(^|\s)@([^\s@]*)$/);
  if (!match) return null;
  const range = caret.cloneRange();
  range.setStart(textNode, caret.startOffset - match[2].length - 1);
  return { query: match[2], range };
}

function setCaret(node: Node, offset: number) {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.setStart(node, offset);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Macintosh|MacIntel|MacPPC|Mac68K/i.test(navigator.platform || navigator.userAgent);
}

export function composerModifierLabel(): string {
  return isMacPlatform() ? "Command+Enter" : "Ctrl+Enter";
}

function insertComposerLineBreak(root: HTMLElement): void {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || !root.contains(selection.getRangeAt(0).startContainer)) {
    const breakNode = document.createElement("br");
    root.append(breakNode);
    setCaret(root, root.childNodes.length);
    return;
  }
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return;
  range.deleteContents();
  const breakNode = document.createElement("br");
  range.insertNode(breakNode);
  const parent = breakNode.parentNode ?? root;
  setCaret(parent, Array.from(parent.childNodes).indexOf(breakNode) + 1);
}

function createMention(option: ComposerReferenceOption): HTMLElement {
  const pill = document.createElement("span");
  pill.className = `composer-inline-mention ${option.kind}`;
  pill.contentEditable = "false";
  pill.dataset.mentionKey = option.key;
  pill.dataset.mentionLabel = option.label;
  pill.dataset.mentionKind = option.kind;
  pill.title = option.detail;

  const marker = document.createElement("span");
  marker.className = "composer-inline-mention-marker";
  marker.textContent = "@";
  pill.append(marker);

  const label = document.createElement("span");
  label.className = "composer-inline-mention-label";
  label.textContent = option.label;
  pill.append(label);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.tabIndex = -1;
  remove.dataset.removeMention = "true";
  remove.setAttribute("aria-label", `移除引用 ${option.label}`);
  remove.textContent = "×";
  pill.append(remove);
  return pill;
}

export function ComposerEditor({
  value,
  references,
  commands,
  placeholder,
  sendKey = "enter",
  onChange,
  onImages,
  onSubmit,
}: {
  value: string;
  references: ComposerReferenceOption[];
  commands?: ComposerCommandOption[];
  placeholder: string;
  sendKey?: ComposerSendKey;
  onChange: (value: string) => void;
  onImages?: (files: File[]) => void;
  onSubmit: () => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const commandMenuRef = useRef<HTMLDivElement | null>(null);
  const lastEmittedRef = useRef("");
  const triggerRef = useRef<MentionTrigger | null>(null);
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [activeOption, setActiveOption] = useState(0);
  const [menuPosition, setMenuPosition] = useState({ left: 12, top: 42 });
  const [slashCompletion, setSlashCompletion] = useState<SlashCompletion | null>(null);
  const [activeCommandOption, setActiveCommandOption] = useState(0);

  const filteredReferences = useMemo(() => {
    const query = (mentionQuery ?? "").trim().toLocaleLowerCase();
    if (!query) return references;
    return references.filter((reference) => (
      `${reference.label} ${reference.detail}`.toLocaleLowerCase().includes(query)
    ));
  }, [mentionQuery, references]);

  const emitChange = useCallback((): string => {
    const root = rootRef.current;
    if (!root) return "";
    const next = extractComposerText(root);
    lastEmittedRef.current = next;
    onChange(next);
    return next;
  }, [onChange]);

  const closeMentions = useCallback(() => {
    triggerRef.current = null;
    setMentionQuery(null);
    setActiveOption(0);
  }, []);

  const updateMentionTrigger = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const trigger = findMentionTrigger(root);
    triggerRef.current = trigger;
    if (!trigger) {
      setMentionQuery(null);
      return;
    }
    setMentionQuery(trigger.query);
    setActiveOption(0);
    const bounds = trigger.range.getBoundingClientRect?.();
    const rootBounds = root.getBoundingClientRect();
    if (bounds && (bounds.left || bounds.top)) {
      setMenuPosition({
        left: Math.max(8, Math.min(rootBounds.width - 268, bounds.left - rootBounds.left)),
        top: Math.max(34, bounds.bottom - rootBounds.top + 8),
      });
    }
  }, []);

  const closeCommands = useCallback(() => {
    setSlashCompletion(null);
    setActiveCommandOption(0);
  }, []);

  const updateSlashCompletion = useCallback((text: string) => {
    const next = resolveComposerCommandCompletion(text, commands ?? []);
    setSlashCompletion(next);
    setActiveCommandOption(0);
    if (next) closeMentions();
  }, [closeMentions, commands]);

  const removeMention = useCallback((pill: HTMLElement) => {
    const parent = pill.parentNode;
    const next = pill.nextSibling;
    const index = parent ? Array.from(parent.childNodes).indexOf(pill) : 0;
    pill.remove();
    if (next?.nodeType === Node.TEXT_NODE && next.textContent?.startsWith("\u200b")) {
      next.textContent = next.textContent.slice(1);
    }
    if (next?.nodeType === Node.TEXT_NODE) setCaret(next, 0);
    else if (parent) setCaret(parent, Math.max(0, index));
    emitChange();
  }, [emitChange]);

  const insertMention = useCallback((option: ComposerReferenceOption) => {
    const root = rootRef.current;
    const trigger = triggerRef.current;
    if (!root || !trigger) return;
    root.focus();
    const pill = createMention(option);
    trigger.range.deleteContents();
    trigger.range.insertNode(pill);
    const spacer = document.createTextNode("\u200b");
    pill.after(spacer);
    setCaret(spacer, 1);
    closeMentions();
    emitChange();
  }, [closeMentions, emitChange]);

  const insertCommand = useCallback((option: ComposerCommandOption) => {
    const root = rootRef.current;
    if (!root || !slashCompletion) return;
    const next = slashCompletion.parent
      ? `${slashCompletion.parent.name} ${option.name} `
      : `${option.name} `;
    root.textContent = next;
    root.focus();
    setCaret(root, root.childNodes.length);
    lastEmittedRef.current = next;
    onChange(next);
    const following = resolveComposerCommandCompletion(next, commands ?? []);
    setSlashCompletion(following);
    setActiveCommandOption(0);
  }, [commands, onChange, slashCompletion]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || value === lastEmittedRef.current) return;
    root.textContent = value;
    lastEmittedRef.current = value;
    closeMentions();
    closeCommands();
  }, [closeCommands, closeMentions, value]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const available = new Set(references.map((reference) => reference.key));
    let removed = false;
    root.querySelectorAll<HTMLElement>(".composer-inline-mention").forEach((pill) => {
      if (!available.has(pill.dataset.mentionKey ?? "")) {
        const next = pill.nextSibling;
        pill.remove();
        if (next?.nodeType === Node.TEXT_NODE && next.textContent?.startsWith("\u200b")) {
          next.textContent = next.textContent.slice(1);
        }
        removed = true;
      }
    });
    if (removed) emitChange();
  }, [emitChange, references]);

  useEffect(() => {
    const active = commandMenuRef.current?.querySelector<HTMLElement>("button.active");
    active?.scrollIntoView?.({ block: "nearest" });
  }, [activeCommandOption, slashCompletion]);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    const modifierSubmit = event.key === "Enter"
      && (isMacPlatform() ? event.metaKey : event.ctrlKey);
    if (mentionQuery !== null) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (filteredReferences.length > 0) {
          event.preventDefault();
          setActiveOption((current) => (
            (current + (event.key === "ArrowDown" ? 1 : -1) + filteredReferences.length) % filteredReferences.length
          ));
          return;
        }
      }
      if ((event.key === "Enter" || event.key === "Tab") && !modifierSubmit) {
        event.preventDefault();
        const reference = filteredReferences[activeOption] ?? filteredReferences[0];
        if (reference) insertMention(reference);
        else closeMentions();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeMentions();
        return;
      }
    }
    if (slashCompletion !== null) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setActiveCommandOption((current) => (
          (current + (event.key === "ArrowDown" ? 1 : -1) + slashCompletion.items.length)
          % slashCompletion.items.length
        ));
        return;
      }
      if ((event.key === "Enter" || event.key === "Tab") && !modifierSubmit) {
        event.preventDefault();
        const option = slashCompletion.items[activeCommandOption] ?? slashCompletion.items[0];
        if (option) insertCommand(option);
        else closeCommands();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeCommands();
        return;
      }
    }
    if (event.key === "Backspace") {
      const selection = window.getSelection();
      if (selection?.isCollapsed && selection.rangeCount > 0) {
        const caret = selection.getRangeAt(0);
        if (caret.startContainer.nodeType === Node.TEXT_NODE) {
          const text = caret.startContainer as Text;
          if (caret.startOffset > 0 && text.data[caret.startOffset - 1] === "\u200b" && isMention(text.previousSibling)) {
            event.preventDefault();
            const pill = text.previousSibling;
            const nextOffset = caret.startOffset - 1;
            text.deleteData(nextOffset, 1);
            pill.remove();
            setCaret(text, nextOffset);
            emitChange();
            return;
          }
          if (caret.startOffset === 0 && isMention(text.previousSibling)) {
            event.preventDefault();
            removeMention(text.previousSibling);
            return;
          }
        } else if (caret.startContainer === rootRef.current && caret.startOffset > 0) {
          const previous = caret.startContainer.childNodes[caret.startOffset - 1];
          if (isMention(previous)) {
            event.preventDefault();
            removeMention(previous);
            return;
          }
        }
      }
    }
    const shouldInsertLineBreak = event.key === "Enter" && (
      (sendKey === "mod_enter" && !modifierSubmit)
      || (sendKey === "enter" && modifierSubmit)
    );
    if (shouldInsertLineBreak) {
      event.preventDefault();
      closeMentions();
      closeCommands();
      const root = rootRef.current;
      if (!root) return;
      insertComposerLineBreak(root);
      emitChange();
      return;
    }
    const shouldSubmit = sendKey === "mod_enter"
      ? modifierSubmit
      : event.key === "Enter" && !event.shiftKey && !modifierSubmit;
    if (shouldSubmit) {
      event.preventDefault();
      closeMentions();
      closeCommands();
      onSubmit();
    }
  };

  const handleClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    const button = target.closest<HTMLButtonElement>("[data-remove-mention]");
    const pill = button?.closest<HTMLElement>(".composer-inline-mention");
    if (!pill) return;
    event.preventDefault();
    removeMention(pill);
  };

  return (
    <div className="composer-editor">
      <div
        ref={rootRef}
        className="composer-editor-input"
        contentEditable
        role="textbox"
        aria-label="任务输入"
        aria-multiline="true"
        data-placeholder={placeholder}
        suppressContentEditableWarning
        onInput={() => {
          const next = emitChange();
          updateMentionTrigger();
          updateSlashCompletion(next);
        }}
        onKeyDown={handleKeyDown}
        onKeyUp={(event) => {
          if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) updateMentionTrigger();
        }}
        onClick={handleClick}
        onPaste={(event) => {
          const imageFiles = [
            ...Array.from(event.clipboardData.items)
              .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
              .map((item) => item.getAsFile())
              .filter((file): file is File => file !== null),
            ...Array.from(event.clipboardData.files)
              .filter((file) => file.type.startsWith("image/")),
          ].filter((file, index, files) => files.findIndex((candidate) => candidate === file) === index);
          if (imageFiles.length > 0 && onImages) {
            event.preventDefault();
            closeMentions();
            closeCommands();
            onImages(imageFiles);
            return;
          }
          event.preventDefault();
          const text = event.clipboardData.getData("text/plain");
          const selection = window.getSelection();
          if (!selection || selection.rangeCount === 0) return;
          const range = selection.getRangeAt(0);
          range.deleteContents();
          const textNode = document.createTextNode(text);
          range.insertNode(textNode);
          setCaret(textNode, text.length);
          const next = emitChange();
          updateMentionTrigger();
          updateSlashCompletion(next);
        }}
        onBlur={() => window.setTimeout(() => {
          closeMentions();
          closeCommands();
        }, 100)}
      />
      {mentionQuery !== null && (
        <div
          className="composer-mention-menu"
          role="listbox"
          aria-label="选择引用"
          style={{ left: menuPosition.left, top: menuPosition.top }}
        >
          <div className="composer-mention-heading"><span>@ 引用</span><small>↑↓ 选择 · Enter 添加</small></div>
          {filteredReferences.length > 0 ? filteredReferences.map((reference, index) => (
            <button
              key={reference.key}
              type="button"
              role="option"
              aria-selected={index === activeOption}
              className={index === activeOption ? "active" : ""}
              onMouseDown={(event) => {
                event.preventDefault();
                insertMention(reference);
              }}
            >
              <span className={`composer-mention-icon ${reference.kind}`}>{referenceIcon(reference.kind)}</span>
              <span className="composer-mention-copy"><strong>{reference.label}</strong><small>{reference.detail}</small></span>
            </button>
          )) : (
            <div className="composer-mention-empty">
              {references.length > 0 ? "没有匹配的引用" : "先通过左下角 + 添加图片、文件或文件夹"}
            </div>
          )}
        </div>
      )}
      {slashCompletion !== null && (
        <div ref={commandMenuRef} className="composer-command-menu" role="listbox" aria-label="快捷命令">
          <div className="composer-command-heading">
            <span>{slashCompletion.title}</span>
            <small>↑↓ 选择 · Tab / Enter 补全</small>
          </div>
          {slashCompletion.items.map((command, index) => (
            <button
              key={`${slashCompletion.parent?.name ?? "root"}:${command.name}`}
              type="button"
              role="option"
              aria-selected={index === activeCommandOption}
              className={index === activeCommandOption ? "active" : ""}
              onMouseEnter={() => setActiveCommandOption(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                insertCommand(command);
              }}
            >
              <span className="composer-command-symbol">/</span>
              <span className="composer-command-copy">
                <strong>{command.name}</strong>
                <small>{command.description}</small>
              </span>
              {command.kind === "skill" && <span className="composer-command-badge">skill</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
