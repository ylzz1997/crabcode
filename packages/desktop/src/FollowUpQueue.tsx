import { CornerDownRight, ListEnd, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { PendingFollowUp, QueuedMessageAction } from "./types";

interface Props {
  messages: PendingFollowUp[];
  canSteer: boolean;
  queueEnabled: boolean;
  onAction: (id: string, action: QueuedMessageAction) => void;
  onDisableQueue: () => void;
}

export function FollowUpQueue({ messages, canSteer, queueEnabled, onAction, onDisableQueue }: Props) {
  const [menuId, setMenuId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!menuId) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !triggerRef.current?.contains(target)) setMenuId(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuId(null);
        triggerRef.current?.focus();
      } else if (event.key === "Tab") {
        setMenuId(null);
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []);
        if (!items.length) return;
        event.preventDefault();
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        items[(index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    const close = () => setMenuId(null);
    window.addEventListener("resize", close);
    rootRef.current?.addEventListener("scroll", close);
    const root = rootRef.current;
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      root?.removeEventListener("scroll", close);
    };
  }, [menuId]);

  useEffect(() => {
    if (menuId && !messages.some((message) => message.item.id === menuId && !message.action)) setMenuId(null);
  }, [messages, menuId]);

  const visible = messages.filter((message) => message.status !== "editing");
  if (!visible.length) return null;
  return <div className="follow-up-queue" ref={rootRef} aria-label="后续消息队列" aria-live="polite">
    {visible.map((message) => <div className="follow-up-card" key={message.item.id} aria-busy={Boolean(message.action)}>
      <ListEnd className="follow-up-icon" aria-label={message.status === "cancelled" ? "未发送" : "排队中"} />
      <span className="follow-up-text" title={message.text}>
        {message.status === "cancelled" && <span className="follow-up-status">未发送 · </span>}
        {message.item.text || (message.images.length ? "图片消息" : "附件消息")}
      </span>
      <button type="button" className="follow-up-steer" title="引导当前运行" aria-label="引导当前运行"
        disabled={!canSteer || message.status !== "pending" || Boolean(message.action)}
        onClick={() => onAction(message.item.id, "steer")}><CornerDownRight />引导</button>
      <button type="button" aria-label="删除排队消息" title="删除消息" disabled={Boolean(message.action)}
        onClick={() => onAction(message.item.id, "remove")}><Trash2 /></button>
      <button type="button" aria-label="更多排队操作" title="更多" aria-haspopup="menu"
        aria-expanded={menuId === message.item.id} disabled={Boolean(message.action)}
        onClick={(event) => {
          triggerRef.current = event.currentTarget;
          const rect = event.currentTarget.getBoundingClientRect();
          setMenuPosition({ top: Math.min(rect.bottom + 4, window.innerHeight - 104), left: Math.max(8, rect.right - 152) });
          setMenuId((current) => current === message.item.id ? null : message.item.id);
        }}><MoreHorizontal /></button>
      {menuId === message.item.id && createPortal(<div className="follow-up-menu" ref={menuRef} style={menuPosition} role="menu" aria-label="排队消息操作">
        <button type="button" role="menuitem" onClick={() => {
          setMenuId(null);
          onAction(message.item.id, "edit");
        }}><Pencil />编辑消息</button>
        <button type="button" role="menuitem" disabled={!queueEnabled} title="后续消息默认改为引导，已排队消息保留"
          onClick={() => { setMenuId(null); onDisableQueue(); }}><ListEnd />关闭排队</button>
      </div>, document.body)}
    </div>)}
  </div>;
}
