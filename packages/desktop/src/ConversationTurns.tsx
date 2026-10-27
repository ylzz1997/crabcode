import { t as tr } from "./i18n";
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { createTurnProcessPolicy, type ProcessTurn } from "../../shared/turnProcess.js";
import type { ChatItem, TurnDurationFormat } from "./types";

const policy = createTurnProcessPolicy();
export interface ProcessPresentation {
  hidden: boolean;
  "data-process-entry": string;
  "data-process-content"?: string;
}
const storageKey = "crabcode.process-expansion.v1";
let cachedChoices: Record<string, boolean> = {};
try { cachedChoices = JSON.parse(sessionStorage.getItem(storageKey) ?? "{}") ?? {}; } catch { /* Optional view state. */ }

function saveChoice(keys: string[], value: boolean) {
  for (const key of keys) cachedChoices[key] = value;
  cachedChoices = Object.fromEntries(Object.entries(cachedChoices).slice(-400));
  try { sessionStorage.setItem(storageKey, JSON.stringify(cachedChoices)); } catch { /* Storage can be unavailable. */ }
}

function ProcessTurnView({ turn, sessionKey, enabled, showDuration, showToolCallCount = false, durationFormat, renderItem }: {
  turn: ProcessTurn<ChatItem>; sessionKey: string; enabled: boolean;
  showDuration: boolean; showToolCallCount?: boolean; durationFormat: TurnDurationFormat; renderItem: (item: ChatItem, presentation: ProcessPresentation) => ReactNode;
}) {
  const keys = policy.viewKeys(sessionKey, turn);
  const [manual, setManual] = useState<boolean | undefined>(() => keys.map((key) => cachedChoices[key]).find((value) => typeof value === "boolean"));
  const [interacting, setInteracting] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const processIds = new Set(turn.processIds);
  const collapsed = policy.isCollapsed(turn, enabled, manual, interacting);
  const firstProcess = turn.items.findIndex((item) => processIds.has(item.id));
  const previousCollapsed = useRef(collapsed);
  const scrollSnapshot = useRef<{ container: HTMLElement; top: number; bottom: boolean; anchor: HTMLElement | null; offset: number } | null>(null);

  useEffect(() => {
    if (manual !== undefined) saveChoice(keys, manual);
  }, [manual, sessionKey, turn.id, turn.finalMessageId]);

  useEffect(() => {
    const check = () => {
      const selection = window.getSelection();
      const ranges = selection && !selection.isCollapsed
        ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index)) : [];
      // Interaction can postpone folding visible process content, never reveal
      // hidden entries when the answer (or the whole turn) is selected.
      const entries = root.current?.querySelectorAll<HTMLElement>("[data-process-content]") ?? [];
      setInteracting([...entries].some((entry) => !entry.hidden
        && (entry.contains(document.activeElement) || ranges.some((range) => range.intersectsNode(entry)))));
    };
    const checkAfterBlur = () => queueMicrotask(check);
    document.addEventListener("selectionchange", check);
    document.addEventListener("focusin", check);
    document.addEventListener("focusout", checkAfterBlur);
    return () => {
      document.removeEventListener("selectionchange", check);
      document.removeEventListener("focusin", check);
      document.removeEventListener("focusout", checkAfterBlur);
    };
  }, []);

  // Snapshot while the old DOM is still present; restore only when disclosure changes.
  if (previousCollapsed.current !== collapsed && root.current) {
    const container = root.current.closest<HTMLElement>(".messages");
    if (container) {
      const top = container.getBoundingClientRect().top;
      const anchor = [...container.querySelectorAll<HTMLElement>("[data-process-entry]")]
        .find((node) => node.getBoundingClientRect().bottom > top && node.getBoundingClientRect().height > 0
          && !(collapsed && root.current?.contains(node) && node.hasAttribute("data-process-content"))) ?? null;
      scrollSnapshot.current = { container, top: container.scrollTop,
        bottom: container.scrollHeight - container.scrollTop - container.clientHeight <= 24,
        anchor, offset: anchor ? anchor.getBoundingClientRect().top : 0 };
    }
  }
  useLayoutEffect(() => {
    previousCollapsed.current = collapsed;
    const snapshot = scrollSnapshot.current;
    scrollSnapshot.current = null;
    if (!snapshot) return;
    const { container, anchor } = snapshot;
    if (snapshot.bottom) container.scrollTop = container.scrollHeight;
    else if (anchor?.isConnected) {
      // Reading layout may already apply the browser's native scroll anchoring.
      const delta = anchor.getBoundingClientRect().top - snapshot.offset;
      container.scrollTop += delta;
    } else container.scrollTop = snapshot.top;
  }, [collapsed]);

  const seconds = Math.max(0, Math.floor((turn.durationMs ?? 0) / 1000));
  const duration = durationFormat === "seconds" || seconds < 60 ? tr("{0}秒", { 0: seconds })
    : tr("{0}分{1}秒", { 0: Math.floor(seconds / 60), 1: seconds % 60 });
  const label = !turn.completed ? tr("处理中") : turn.reason === "interrupted" ? tr("已中断")
    : turn.reason && !["end_turn", "stop"].includes(turn.reason) ? tr("处理已停止") : tr("已完成");
  return <div className="conversation-turn" ref={root} data-turn-id={turn.id}>
    {turn.items.map((item, index) => <Fragment key={item.id}>
      {enabled && index === firstProcess && <button type="button" className="process-summary"
        aria-expanded={!collapsed} aria-label={tr("{0}处理过程", { 0: collapsed ? tr("展开") : tr("收起") })}
        onClick={() => setManual(!collapsed)}>
        {collapsed ? <ChevronRight /> : <ChevronDown />}<span>{label}</span>
        {showDuration && turn.durationMs !== null && <span>· {duration}</span>}
        {showToolCallCount && turn.toolCount > 0 && <span>· {turn.toolCount}{tr("次工具调用")}</span>}
        {showToolCallCount && turn.errorCount > 0 && <span className="process-error-count">· {turn.errorCount}{tr("次工具错误")}</span>}
      </button>}
      {renderItem(item, { "data-process-entry": item.id,
        "data-process-content": processIds.has(item.id) ? "" : undefined,
        hidden: collapsed && processIds.has(item.id)
          || enabled && firstProcess >= 0 && item.kind === "turn_duration" })}
    </Fragment>)}
  </div>;
}

export function ConversationTurns({ items, busy, ...props }: {
  items: ChatItem[]; busy: boolean; sessionKey: string; enabled: boolean;
  showDuration: boolean; showToolCallCount?: boolean; durationFormat: TurnDurationFormat; renderItem: (item: ChatItem, presentation: ProcessPresentation) => ReactNode;
}) {
  return <>{policy.groupTurns(items, busy).map((turn) =>
    <ProcessTurnView key={`${props.sessionKey}:${turn.id}`} {...props} turn={turn} />)}</>;
}
