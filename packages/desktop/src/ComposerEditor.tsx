import { FileText, Folder, Image as ImageIcon, Quote } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";

export type ComposerReferenceKind = "image" | "file" | "folder" | "document";

export type ComposerReferenceOption = {
  key: string;
  kind: ComposerReferenceKind;
  label: string;
  detail: string;
};

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
  placeholder,
  onChange,
  onSubmit,
}: {
  value: string;
  references: ComposerReferenceOption[];
  placeholder: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const lastEmittedRef = useRef("");
  const triggerRef = useRef<MentionTrigger | null>(null);
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [activeOption, setActiveOption] = useState(0);
  const [menuPosition, setMenuPosition] = useState({ left: 12, top: 42 });

  const filteredReferences = useMemo(() => {
    const query = (mentionQuery ?? "").trim().toLocaleLowerCase();
    if (!query) return references;
    return references.filter((reference) => (
      `${reference.label} ${reference.detail}`.toLocaleLowerCase().includes(query)
    ));
  }, [mentionQuery, references]);

  const emitChange = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const next = extractComposerText(root);
    lastEmittedRef.current = next;
    onChange(next);
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

  useEffect(() => {
    const root = rootRef.current;
    if (!root || value === lastEmittedRef.current) return;
    root.textContent = value;
    lastEmittedRef.current = value;
    closeMentions();
  }, [closeMentions, value]);

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

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
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
      if (event.key === "Enter" || event.key === "Tab") {
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
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      closeMentions();
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
          emitChange();
          updateMentionTrigger();
        }}
        onKeyDown={handleKeyDown}
        onKeyUp={(event) => {
          if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) updateMentionTrigger();
        }}
        onClick={handleClick}
        onPaste={(event) => {
          event.preventDefault();
          const text = event.clipboardData.getData("text/plain");
          const selection = window.getSelection();
          if (!selection || selection.rangeCount === 0) return;
          const range = selection.getRangeAt(0);
          range.deleteContents();
          const textNode = document.createTextNode(text);
          range.insertNode(textNode);
          setCaret(textNode, text.length);
          emitChange();
          updateMentionTrigger();
        }}
        onBlur={() => window.setTimeout(closeMentions, 100)}
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
    </div>
  );
}
