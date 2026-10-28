/** Pure presentation policy, also embedded in the VS Code webview. Keep self-contained. */
export function createTurnProcessPolicy() {
  const startsTurn = (item) => item.kind === 'user' && !item.origin && item.followUpMode !== 'steer';
  const messageId = (item) => item.messageUuid || item.id.replace(/:part-\d+$/, '');

  function describeTurn(items, options = {}) {
    const boundary = [...items].reverse().find((item) => item.kind === 'turn_duration');
    const recorded = [...items].reverse().find((item) => item.turnDetails);
    const reason = options.reason ?? boundary?.turnEndReason ?? recorded?.turnDetails?.reason;
    const completed = options.completed === true;
    const lastActivity = items.reduce((last, item, index) =>
      ['tool', 'thinking', 'file_change'].includes(item.kind) && !item.agent_id ? index : last, -1);
    const assistants = items.filter((item, index) => item.kind === 'assistant' && !item.agent_id
      && index > lastActivity && (item.text?.trim() || item.images?.length));
    const explicitFinal = options.finalMessageId ?? boundary?.finalMessageId;
    // Keep every block of the terminal message. With legacy data, retain the entire
    // trailing text sequence instead of guessing which paragraph is the answer.
    const finalIds = new Set(assistants.filter((item) => !explicitFinal
      || messageId(item) === explicitFinal || item.id === explicitFinal).map((item) => item.id));
    const processIds = items.filter((item) => {
      // Screenshots and images read from files are intermediate observations.
      // Fold them with their tool cards; keep image outputs visible.
      const observation = item.kind === 'tool' && ['computeruse', 'read', 'fileread']
        .includes(item.title?.trim().toLowerCase().replace(/[\s.-]/g, ''));
      if (item.images?.length && !observation) return false;
      if (['thinking', 'tool', 'file_change'].includes(item.kind)) return true;
      return item.kind === 'assistant' && finalIds.size > 0 && !finalIds.has(item.id);
    }).map((item) => item.id);
    const pending = items.some((item) => item.status === 'running' || item.status === 'pending');
    const successfulEnd = reason == null || reason === 'end_turn' || reason === 'stop';
    const tools = items.filter((item) => item.kind === 'tool' && !item.agent_id);
    return {
      processIds,
      finalMessageId: finalIds.size ? messageId(assistants.find((item) => finalIds.has(item.id))) : null,
      canAutoCollapse: completed && successfulEnd && !pending && finalIds.size > 0
        && !items.some((item) => item.kind === 'error'),
      toolCount: new Set(tools.map((item) => item.tool_use_id || item.id)).size,
      errorCount: tools.filter((item) => item.isError).length,
      durationMs: boundary?.durationMs ?? recorded?.turnDetails?.duration_ms ?? null,
      completed, reason,
    };
  }

  function groupTurns(items, busy = false) {
    const turns = [];
    let current = [];
    const finish = (completed) => {
      if (!current.length) return;
      turns.push({ id: current[0].id, items: current, ...describeTurn(current, { completed }) });
      current = [];
    };
    for (const item of items) {
      if (startsTurn(item)) finish(true);
      current.push(item);
      if (item.kind === 'turn_duration') finish(true);
    }
    finish(!busy);
    return turns;
  }

  const isCollapsed = (turn, enabled, manual, interacting = false) => enabled
    && turn.processIds.length > 0 && (manual === undefined ? turn.canAutoCollapse && !interacting : manual);
  const viewKeys = (sessionKey, turn) => [sessionKey + ':turn:' + turn.id,
    ...(turn.finalMessageId ? [sessionKey + ':reply:' + turn.finalMessageId] : [])];
  return { startsTurn, describeTurn, groupTurns, isCollapsed, viewKeys };
}
