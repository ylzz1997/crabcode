import { CircleAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CopyButton } from "./CopyButton";
import { turnDetailRows, type TurnDetails } from "./turnDetails";

export function TurnDetailsButton({ details }: { details: TurnDetails }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
      }
      if (event.key === "Tab") {
        const buttons = dialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
        if (!buttons?.length) return;
        const first = buttons[0];
        const last = buttons[buttons.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {
          event.preventDefault(); first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    const returnFocus = trigger.current;
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKey, true);
      returnFocus?.focus();
    };
  }, [open]);

  return <>
    <button ref={trigger} className="copy-button turn-details-button" type="button" title="本轮详情" aria-label="本轮详情" aria-haspopup="dialog" onClick={() => setOpen(true)}><CircleAlert /></button>
    {open && createPortal(
      <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setOpen(false)}>
        <section ref={dialog} className="modal turn-details-dialog" role="dialog" aria-modal="true" aria-label="本轮详情" tabIndex={-1}>
          <header><h2>本轮详情</h2><button className="icon-button" title="关闭" aria-label="关闭本轮详情" onClick={() => setOpen(false)}><X /></button></header>
          <div className="modal-body">
            <dl className="session-detail-list">
              <div><dt>Session ID</dt><dd className="session-detail-copy-value"><code title={details.session_id}>{details.session_id}</code><CopyButton text={details.session_id} label="复制 Session ID" className="session-detail-copy" /></dd></div>
              {turnDetailRows(details).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
            </dl>
            <p className="turn-details-note">次数统计本轮主会话；连续思考内容计为一次。{details.source === "history" && "此轮未完整记录，时间和次数按可用历史恢复；缺失项标为未记录。"}</p>
          </div>
        </section>
      </div>, document.body,
    )}
  </>;
}
