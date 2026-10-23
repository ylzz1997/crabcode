import { t as tr } from "./i18n";
import { useState } from "react";
import { Pencil, RotateCcw } from "lucide-react";
import { approvalShortcutFromKey, approvalShortcutKeyLabels, DEFAULT_APPROVAL_SHORTCUTS } from "./approvalShortcuts";
import type { ApprovalShortcutPreferences } from "./types";

export function ApprovalShortcutSettings({ value, onChange, registrationError }: {
  value: ApprovalShortcutPreferences;
  onChange: (value: ApprovalShortcutPreferences) => void;
  registrationError?: string | null;
}) {
  const [recording, setRecording] = useState<"approve" | "deny" | "always_allow" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const actions = [
    { key: "approve", label: tr("允许一次"), scope: null },
    { key: "deny", label: tr("拒绝"), scope: null },
    { key: "always_allow", label: tr("始终允许"), scope: tr("当前会话") },
  ] as const;
  const defaultKeys = actions.every(({ key }) => value[key] === DEFAULT_APPROVAL_SHORTCUTS[key]);
  return (
    <section className="approval-shortcut-section general-spaced-heading" aria-labelledby="approval-shortcut-title">
      <div className="settings-section-heading approval-shortcut-heading">
        <div>
          <h2 id="approval-shortcut-title">{tr("权限快捷键")}</h2>
          <p>{tr("在其他应用中审批，保持当前窗口在前台。")}</p>
        </div>
        <button
          className={`settings-switch ${value.enabled ? "on" : ""}`}
          type="button" role="switch" aria-checked={value.enabled} aria-label={tr("启用权限快捷键")}
          title={tr("仅一条待审批请求时生效；打开设置时暂停响应。")}
          onClick={() => onChange({ ...value, enabled: !value.enabled })}
        ><span /></button>
      </div>
      <div className="approval-shortcut-list" data-enabled={value.enabled}>
        {actions.map(({ key: action, label, scope }) => (
          <div className="approval-shortcut-row" key={action}>
            <div className="approval-shortcut-label">
              <strong>{label}</strong>
              {scope && <span>{scope}</span>}
            </div>
            <button
              className="approval-shortcut-recorder"
              type="button"
              aria-label={tr("{0}快捷键", { 0: label })}
              aria-describedby={`approval-shortcut-${action}-value approval-shortcut-help`}
              aria-pressed={recording === action}
              data-recording={recording === action}
              disabled={!value.enabled}
              title={tr("修改{0}快捷键", { 0: label })}
              onClick={(event) => { event.currentTarget.focus(); setRecording(action); setError(null); }}
              onBlur={() => setRecording(null)}
              onKeyDown={(event) => {
                if (recording !== action || event.key === "Tab") return;
                event.preventDefault();
                event.stopPropagation();
                if (event.key === "Escape") { setRecording(null); return; }
                if (event.repeat || ["Control", "Alt", "Shift", "Meta"].includes(event.key)) return;
                const shortcut = approvalShortcutFromKey(event);
                if (!shortcut) {
                  setError(tr("请使用 {0} 中至少一个键，搭配字母、数字或 F1–F24。", { 0: approvalShortcutKeyLabels("Ctrl+Alt+Super").join("、") }));
                  return;
                }
                if (actions.some(({ key }) => key !== action && value[key] === shortcut)) {
                  setError(tr("三个操作必须使用不同的快捷键。"));
                  return;
                }
                onChange({ ...value, [action]: shortcut });
                setError(null);
                setRecording(null);
              }}
            >
              <span className="approval-shortcut-keys" id={`approval-shortcut-${action}-value`}>
                {recording === action ? <span className="approval-shortcut-prompt">{tr("请按组合键…")}</span>
                  : approvalShortcutKeyLabels(value[action]).map((key, index) => <kbd key={key} title={value[action].split("+")[index]}>{key}</kbd>)}
              </span>
              {recording === action ? <kbd className="approval-shortcut-cancel">Esc</kbd> : <Pencil aria-hidden="true" />}
            </button>
          </div>
        ))}
      </div>
      <div className="approval-shortcut-footer">
        <span id="approval-shortcut-help" title={tr("仅一条待审批请求时生效；打开设置时暂停响应。")}>{tr("点击按键修改 · Esc 取消")}</span>
        <button className="approval-shortcut-reset" type="button" disabled={defaultKeys} aria-label={tr("恢复默认权限快捷键")} onClick={() => {
            onChange({ ...DEFAULT_APPROVAL_SHORTCUTS, enabled: value.enabled });
            setError(null);
          }}><RotateCcw aria-hidden="true" />{tr("恢复默认")}</button>
      </div>
      {(error || registrationError) && <p className="approval-shortcut-error" role="alert">{error || registrationError}</p>}
    </section>
  );
}
