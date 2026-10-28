/* @vitest-environment jsdom */
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { ConversationTurns, type ProcessPresentation } from "./ConversationTurns";
import { applyGatewayEvent } from "./events";
import { normalizeSettings } from "./native";
import type { ChatItem, DesktopSettings, SessionViewState } from "./types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
function fixture(): ChatItem[] {
  return [
    { id: 'u', kind: 'user', text: 'task' },
    { id: 'pre', kind: 'assistant', text: 'checking', status: 'complete' },
    { id: 'tool', kind: 'tool', status: 'complete', result: 'ok' },
    { id: 'f', kind: 'assistant', text: 'finished', status: 'complete', messageUuid: 'final' },
    { id: 'edits', kind: 'file_edit_summary' },
    { id: 'end', kind: 'turn_duration', durationMs: 83000, turnEndReason: 'end_turn', finalMessageId: 'final' },
  ];
}
function StatefulItem({ item, presentation }: { item: ChatItem; presentation: ProcessPresentation }) {
  const [open, setOpen] = useState(false);
  return <button {...presentation} data-item={item.id} onClick={() => setOpen(!open)}>{item.text || item.kind}{open ? ':open' : ''}</button>;
}
function harness(sessionKey: string) {
  const node = document.createElement('div');
  node.className = 'messages';
  document.body.append(node);
  const root = createRoot(node);
  const render = (items: ChatItem[], enabled = true, busy = false, key = sessionKey, showToolCallCount = false) => act(() => root.render(
    <ConversationTurns items={items} busy={busy} sessionKey={key} enabled={enabled} showDuration showToolCallCount={showToolCallCount} durationFormat="hms"
      renderItem={(item, presentation) => <StatefulItem item={item} presentation={presentation} />} />));
  const hidden = (id: string) => node.querySelector<HTMLElement>(`[data-process-entry="${id}"]`)?.hidden;
  const close = () => { act(() => root.unmount()); node.remove(); };
  return { node, render, hidden, close };
}

describe('automatic process disclosure', () => {
  it('defaults on for existing settings and respects an explicit false', () => {
    expect(normalizeSettings({ connections: [] } as unknown as DesktopSettings).auto_collapse_process).toBe(true);
    expect(normalizeSettings({ connections: [], auto_collapse_process: false } as unknown as DesktopSettings).auto_collapse_process).toBe(false);
    expect(normalizeSettings({ connections: [] } as unknown as DesktopSettings).show_tool_call_count).toBe(false);
    expect(normalizeSettings({ connections: [], show_tool_call_count: true } as unknown as DesktopSettings).show_tool_call_count).toBe(true);
  });

  it('folds only the completed process and preserves card state across setting changes', () => {
    const h = harness('completed');
    try {
      h.render(fixture());
      expect(h.hidden('pre')).toBe(true);
      expect(h.hidden('tool')).toBe(true);
      for (const id of ['u', 'f', 'edits']) expect(h.hidden(id)).toBe(false);
      expect(h.node.querySelector('.process-summary')?.textContent).toContain('1分23秒');
      expect(h.node.querySelector('.process-summary')?.textContent).toContain('已完成');
      expect(h.node.querySelector('.process-summary')?.textContent).not.toContain('次工具调用');
      h.render(fixture(), true, false, 'completed', true);
      expect(h.node.querySelector('.process-summary')?.textContent).toContain('1次工具调用');
      expect(h.hidden('tool')).toBe(true);
      h.render(fixture());
      expect(h.node.querySelector('.process-summary')?.textContent).not.toContain('次工具调用');
      act(() => h.node.querySelector<HTMLButtonElement>('.process-summary')!.click());
      expect(h.hidden('tool')).toBe(false);
      act(() => h.node.querySelector<HTMLButtonElement>('[data-item="tool"]')!.click());
      h.render(fixture(), false);
      expect(h.node.querySelector('.process-summary')).toBeNull();
      expect(h.hidden('pre')).toBe(false);
      h.render(fixture());
      expect(h.hidden('tool')).toBe(false);
      expect(h.node.querySelector('[data-item="tool"]')?.textContent).toBe('tool:open');
      h.render([], true, false, 'other');
      h.render(fixture().map(item => item.id === 'u' ? { ...item, id: 'durable-u' } : item));
      expect(h.hidden('tool')).toBe(false);
    } finally { h.close(); }
  });

  it('shows running and interrupted turns and keeps approvals and pictures available when manually folded', () => {
    const h = harness('interruptions');
    try {
      h.render(fixture().slice(0, -2), true, true);
      expect(h.hidden('pre')).toBe(false);
      expect(h.node.querySelector('.process-summary')?.textContent).toContain('处理中');
      h.render(fixture().map(item => item.kind === 'turn_duration' ? { ...item, turnEndReason: 'interrupted' } : item));
      expect(h.hidden('tool')).toBe(false);
      const items = fixture();
      items.splice(3, 0, { id: 'approval', kind: 'permission', status: 'pending' },
        { id: 'picture', kind: 'tool', images: [{ data: 'YQ==', media_type: 'image/png' }], status: 'complete' });
      h.render(items);
      act(() => h.node.querySelector<HTMLButtonElement>('.process-summary')!.click());
      expect(h.hidden('tool')).toBe(true);
      expect(h.hidden('approval')).toBe(false);
      expect(h.hidden('picture')).toBe(false);
    } finally { h.close(); }
  });

  it('folds screenshots and read images with the process while keeping image outputs visible', () => {
    const h = harness('computer-use');
    const images = [{ data: 'YQ==', media_type: 'image/png' }];
    const items = fixture();
    items.splice(2, 1,
      ...['observe', 'click', 'scroll'].map((action): ChatItem => ({
        id: action, kind: 'tool', title: 'ComputerUse', input: { action }, images, status: 'complete',
      })),
      ...['Read', 'FileRead'].map((title): ChatItem => ({
        id: title, kind: 'tool', title, input: { file_path: '/work/cover.png' }, images, status: 'complete',
      })),
      { id: 'picture', kind: 'tool', title: 'Image', images, status: 'complete' });
    const processIds = ['observe', 'click', 'scroll', 'Read', 'FileRead'];
    try {
      h.render(items);
      for (const id of processIds) expect(h.hidden(id)).toBe(true);
      expect(h.hidden('picture')).toBe(false);
      expect(h.hidden('f')).toBe(false);
      act(() => h.node.querySelector<HTMLButtonElement>('.process-summary')!.click());
      for (const id of processIds) expect(h.hidden(id)).toBe(false);
      act(() => h.node.querySelector<HTMLButtonElement>('[data-item="scroll"]')!.click());
      act(() => h.node.querySelector<HTMLButtonElement>('[data-item="Read"]')!.click());
      act(() => h.node.querySelector<HTMLButtonElement>('.process-summary')!.click());
      for (const id of processIds) expect(h.hidden(id)).toBe(true);
      expect(h.hidden('picture')).toBe(false);
      expect(h.hidden('f')).toBe(false);
      h.render(items, false);
      expect(h.hidden('scroll')).toBe(false);
      expect(h.hidden('Read')).toBe(false);
      expect(h.node.querySelector('[data-item="scroll"]')?.textContent).toBe('tool:open');
      expect(h.node.querySelector('[data-item="Read"]')?.textContent).toBe('tool:open');
    } finally { h.close(); }
  });

  it('keeps the process folded when visible answer text is selected or focused', () => {
    const h = harness('answer-selection');
    const selection = window.getSelection()!;
    try {
      h.render(fixture());
      for (const id of ['f', 'u', 'edits']) {
        const item = h.node.querySelector<HTMLButtonElement>(`[data-item="${id}"]`)!;
        const range = document.createRange();
        range.selectNodeContents(item);
        act(() => {
          item.focus();
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
        });
        expect(h.hidden('pre')).toBe(true);
        expect(h.hidden('tool')).toBe(true);
        expect(h.node.querySelector('.process-summary')?.getAttribute('aria-expanded')).toBe('false');
        expect(selection.toString()).toBe(item.textContent);
      }
    } finally { selection.removeAllRanges(); h.close(); }
  });

  it('does not reveal hidden process entries when a selection spans the turn', () => {
    const h = harness('turn-selection');
    const selection = window.getSelection()!;
    try {
      h.render(fixture());
      for (const selector of ['.conversation-turn', '[data-item="pre"]']) {
        const range = document.createRange();
        range.selectNodeContents(h.node.querySelector(selector)!);
        act(() => {
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
        });
        expect(h.hidden('pre')).toBe(true);
        expect(h.hidden('tool')).toBe(true);
      }
    } finally { selection.removeAllRanges(); h.close(); }
  });

  it.each([false, true])('defers automatic collapse while process text is selected (cross-entry: %s)', (crossEntry) => {
    const h = harness(`selection-${crossEntry}`);
    try {
      h.render(fixture().slice(0, -1), true, true);
      const range = document.createRange();
      range.selectNodeContents(h.node.querySelector('[data-item="pre"]')!);
      if (crossEntry) range.setStart(h.node.querySelector('[data-item="u"]')!, 0);
      act(() => { window.getSelection()!.addRange(range); document.dispatchEvent(new Event('selectionchange')); });
      h.render(fixture());
      expect(h.hidden('pre')).toBe(false);
      act(() => { window.getSelection()!.removeAllRanges(); document.dispatchEvent(new Event('selectionchange')); });
      expect(h.hidden('pre')).toBe(true);
    } finally { h.close(); }
  });

  it('defers automatic collapse while a process control is focused and folds after blur', async () => {
    const h = harness('process-focus');
    try {
      h.render(fixture().slice(0, -1), true, true);
      act(() => h.node.querySelector<HTMLButtonElement>('[data-item="tool"]')!.focus());
      h.render(fixture());
      expect(h.hidden('tool')).toBe(false);
      await act(async () => h.node.querySelector<HTMLButtonElement>('[data-item="tool"]')!.blur());
      expect(h.hidden('tool')).toBe(true);
    } finally { h.close(); }
  });

  it('retains terminal reasons and steering origins in the actual event projection', () => {
    const state: SessionViewState = { id: 's', cwd: '/work', title: 'test', items: fixture().slice(0, -1),
      busy: true, connected: true, loading: false, error: null, operationId: 'op', status: null, runStartedAt: 1 };
    const stopped = applyGatewayEvent(state, { type: 'turn_complete', reason: 'interrupted', assistant_message_uuid: 'final' });
    expect(stopped.items.at(-1)).toMatchObject({ kind: 'turn_duration', turnEndReason: 'interrupted', finalMessageId: 'final' });
    const history = applyGatewayEvent(state, { type: 'session_history', messages: [
      { uuid: 'u', role: 'user', content: 'task' },
      { uuid: 'steer', role: 'user', origin: 'user-steering', content: 'correction' },
      { uuid: 'f', role: 'assistant', content: 'answer', turn_details: { session_id: 's', reason: 'interrupted' } },
    ] });
    expect(history.items.find(item => item.id === 'steer')?.origin).toBe('user-steering');
    const h = harness('projection');
    try { h.render(history.items); expect(h.node.querySelectorAll('.conversation-turn')).toHaveLength(1); }
    finally { h.close(); }
  });
});
