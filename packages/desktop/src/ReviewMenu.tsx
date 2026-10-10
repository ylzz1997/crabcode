import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChevronDown } from "lucide-react";

export function ReviewMenu({ label, ariaLabel, width = 220, disabled = false, role = "menu", triggerIcon, triggerClassName = "", children }: {
  label: ReactNode;
  ariaLabel: string;
  width?: number;
  disabled?: boolean;
  role?: "menu" | "dialog";
  triggerIcon?: ReactNode;
  triggerClassName?: string;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = () => { setOpen(false); triggerRef.current?.focus(); };

  useLayoutEffect(() => {
    if (!open) return;
    let frame: number;
    const place = () => {
      const anchor = triggerRef.current?.getBoundingClientRect();
      const menu = menuRef.current;
      if (anchor && menu) {
        const next = {
          top: Math.max(8, Math.min(anchor.bottom + 6, window.innerHeight - menu.offsetHeight - 8)),
          left: Math.max(8, Math.min(anchor.left, window.innerWidth - Math.min(width, window.innerWidth - 16) - 8)),
        };
        setPosition((current) => current.top === next.top && current.left === next.left ? current : next);
      }
      frame = requestAnimationFrame(place);
    };
    place();
    (menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]:not(:disabled), [aria-pressed="true"]:not(:disabled)')
      ?? menuRef.current?.querySelector<HTMLElement>('button:not(:disabled), input'))?.focus();
    return () => cancelAnimationFrame(frame);
  }, [open, width]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return <>
    <button ref={triggerRef} type="button" className={`change-review-turn change-review-turn-trigger ${triggerClassName}`}
      disabled={disabled} aria-label={ariaLabel} aria-haspopup={role} aria-expanded={open}
      aria-controls={open ? id : undefined} onClick={() => setOpen((value) => !value)}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); }
      }}>
      {label != null && <span>{label}</span>}{triggerIcon ?? <ChevronDown aria-hidden="true" />}
    </button>
    {open && !disabled && createPortal(
      <div ref={menuRef} id={id} className="change-review-turn-menu" role={role} aria-label={ariaLabel}
        style={{ ...position, width: Math.min(width, window.innerWidth - 16) }}
        onKeyDown={(event) => {
          if (event.key === "Tab" && role === "menu") {
            event.preventDefault();
            const controls = Array.from(document.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex], [contenteditable="true"]'))
              .filter((element) => element.tabIndex >= 0 && !element.matches(":disabled") && !element.closest("[inert]")
                && !menuRef.current?.contains(element) && element.getClientRects().length > 0 && getComputedStyle(element).visibility !== "hidden");
            const index = controls.indexOf(triggerRef.current!);
            setOpen(false);
            (controls[index + (event.shiftKey ? -1 : 1)] ?? triggerRef.current)?.focus();
            return;
          }
          if ((event.target as HTMLElement).matches("input, textarea") || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const options = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('button[data-menu-option]:not(:disabled)') ?? []);
          const index = options.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
            : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
          options[next]?.focus();
        }}>
        {children(close)}
      </div>, document.body,
    )}
  </>;
}
