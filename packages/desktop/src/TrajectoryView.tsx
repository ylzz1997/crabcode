import { ChevronDown, ChevronRight, Clock3, LoaderCircle, Search, Wrench } from "lucide-react";
import { useMemo, useState, type CSSProperties } from "react";
import { getToolPresentation } from "./toolPresentation";
import type { ChatItem } from "./types";

export type TrajectoryLane = "input" | "model" | "tools";

export interface TrajectoryRecord {
  index: number;
  turn: number;
  step: number;
  lane: TrajectoryLane;
  label: string;
  title: string;
  preview: string;
  searchable: string;
  durationMs: number | null;
  startedAt: number | null;
  item: ChatItem;
}

export interface TrajectoryTurn {
  number: number;
  durationMs: number | null;
  records: TrajectoryRecord[];
}

type PositionedRecord = TrajectoryRecord & { left: number; width: number };

function textFromUnknown(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function compactText(value: unknown): string {
  return textFromUnknown(value).replace(/\s+/g, " ").trim();
}

function itemDuration(item: ChatItem, now: number): number | null {
  if (item.durationMs !== undefined) return Math.max(0, item.durationMs);
  if (item.startedAt === undefined) return null;
  return Math.max(0, (item.completedAt ?? now) - item.startedAt);
}

function recordDetail(item: ChatItem): unknown {
  if (item.kind === "tool") return item.input ?? item.detail;
  if (item.kind === "choice") return item.selected ?? item.options;
  if (item.kind === "file_change") return item.diff;
  return item.text ?? item.detail ?? item.question ?? item.title;
}

function recordIdentity(item: ChatItem): Pick<TrajectoryRecord, "lane" | "label" | "title" | "preview"> {
  if (item.kind === "user") {
    return { lane: "input", label: "USER", title: "用户", preview: compactText(item.text) };
  }
  if (item.kind === "assistant") {
    return { lane: "model", label: "ASSISTANT", title: "回复", preview: compactText(item.text) };
  }
  if (item.kind === "thinking") {
    return { lane: "model", label: "THINKING", title: "思考", preview: compactText(item.text) };
  }
  if (item.kind === "tool") {
    const input = item.input ?? (
      item.detail && typeof item.detail === "object" && !Array.isArray(item.detail)
        ? item.detail as Record<string, unknown>
        : {}
    );
    const presentation = getToolPresentation(item.title ?? "Tool", input);
    const request = compactText(input);
    const result = compactText(item.result ?? (typeof item.detail === "string" ? item.detail : ""));
    return {
      lane: "tools",
      label: "TOOL",
      title: item.title ?? presentation.label,
      preview: [request, result ? `→ ${result}` : ""].filter(Boolean).join(" "),
    };
  }
  if (item.kind === "file_change") {
    return {
      lane: "tools",
      label: "FILE",
      title: `${item.action ?? "修改"} ${item.path ?? item.title ?? "文件"}`,
      preview: compactText(item.diff),
    };
  }
  if (item.kind === "permission") {
    return {
      lane: "input",
      label: "APPROVAL",
      title: item.title ?? "权限确认",
      preview: compactText(item.detail ?? item.text),
    };
  }
  if (item.kind === "choice") {
    return {
      lane: "input",
      label: "CHOICE",
      title: item.title ?? item.question ?? "用户选择",
      preview: compactText(item.selected?.length ? item.selected : item.options),
    };
  }
  if (item.kind === "plan") {
    return { lane: "model", label: "PLAN", title: item.title ?? "计划", preview: compactText(item.detail) };
  }
  if (item.kind === "error") {
    return { lane: "model", label: "ERROR", title: "错误", preview: compactText(item.text) };
  }
  return {
    lane: "input",
    label: "SYSTEM",
    title: item.title ?? "系统",
    preview: compactText(item.text ?? item.detail),
  };
}

export function deriveTrajectory(items: ChatItem[], now: number): TrajectoryTurn[] {
  const turns: TrajectoryTurn[] = [];
  let turn: TrajectoryTurn | null = null;
  let turnNumber = 0;
  let step = 0;
  let previousLane: TrajectoryLane | null = null;
  let index = 0;

  const ensureTurn = () => {
    if (turn) return turn;
    turn = { number: ++turnNumber, durationMs: null, records: [] };
    turns.push(turn);
    return turn;
  };

  for (const item of items) {
    if (item.kind === "turn_duration") {
      if (turn) turn.durationMs = itemDuration(item, now);
      continue;
    }
    if (item.kind === "user") {
      turn = { number: ++turnNumber, durationMs: null, records: [] };
      turns.push(turn);
      step = 0;
      previousLane = null;
    }
    const currentTurn = ensureTurn();
    const identity = recordIdentity(item);
    if (identity.lane === "model" && previousLane !== "model") step += 1;
    if (identity.lane === "tools" && step === 0) step = 1;
    const detail = recordDetail(item);
    currentTurn.records.push({
      index: ++index,
      turn: currentTurn.number,
      step,
      ...identity,
      searchable: [identity.label, identity.title, identity.preview, textFromUnknown(detail), item.result]
        .filter(Boolean)
        .join("\n")
        .toLocaleLowerCase(),
      durationMs: itemDuration(item, now),
      startedAt: item.startedAt ?? null,
      item,
    });
    previousLane = identity.lane;
  }
  return turns.filter((entry) => entry.records.length > 0);
}

function formatDuration(durationMs: number | null): string {
  if (durationMs === null) return "";
  if (durationMs < 1_000) return `${Math.round(durationMs)}ms`;
  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(durationMs < 10_000 ? 1 : 0)}s`;
  const minutes = Math.floor(durationMs / 60_000);
  const seconds = Math.floor(durationMs % 60_000 / 1_000);
  return `${minutes}m ${seconds}s`;
}

function timelinePositions(records: TrajectoryRecord[], actualDuration: boolean): PositionedRecord[] {
  const weights = records.map((record) => {
    if (!actualDuration) return 1;
    if (record.durationMs === null) return 1;
    return Math.max(0.75, Math.min(8, Math.log2(1 + record.durationMs / 180)));
  });
  const total = weights.reduce((sum, weight) => sum + weight, 0) || 1;
  let elapsed = 0;
  return records.map((record, index) => {
    const left = elapsed / total * 100;
    const width = weights[index] / total * 100;
    elapsed += weights[index];
    return { ...record, left, width };
  });
}

function TrajectoryRecordDetails({ record }: { record: TrajectoryRecord }) {
  const { item } = record;
  const started = record.startedAt === null
    ? "未记录"
    : new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: 3,
    }).format(record.startedAt);
  const input = item.kind === "tool" ? textFromUnknown(item.input ?? item.detail) : "";
  const output = item.kind === "tool" ? textFromUnknown(item.result ?? (typeof item.detail === "string" ? item.detail : "")) : "";
  const content = item.kind === "tool" ? "" : textFromUnknown(recordDetail(item));
  return (
    <div className="trajectory-record-details">
      <dl>
        <div><dt>Turn / Step</dt><dd>{record.turn} / {record.step || "Message"}</dd></div>
        <div><dt>开始</dt><dd>{started}</dd></div>
        <div><dt>耗时</dt><dd>{formatDuration(record.durationMs) || "未记录"}</dd></div>
        <div><dt>状态</dt><dd>{item.isError ? "失败" : item.status === "running" ? "运行中" : item.status ?? "完成"}</dd></div>
      </dl>
      {input && <section><h4>调用参数</h4><pre>{input}</pre></section>}
      {content && <section><h4>内容</h4><pre>{content}</pre></section>}
      {output && <section><h4>{item.isError ? "错误" : "执行结果"}</h4><pre>{output}</pre></section>}
    </div>
  );
}

function TrajectoryLedgerRow({ record }: { record: TrajectoryRecord }) {
  const [expanded, setExpanded] = useState(false);
  const running = record.item.status === "running";
  return (
    <article
      className={`trajectory-record lane-${record.lane} ${record.item.isError ? "is-error" : ""}`}
      id={`trajectory-record-${record.index}`}
    >
      <button
        type="button"
        className="trajectory-record-summary"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="trajectory-record-index">#{record.index}</span>
        <span className="trajectory-record-node" aria-hidden="true" />
        <span className="trajectory-record-label">{record.label}</span>
        <span className="trajectory-record-content">
          <strong>{record.title}</strong>
          {record.preview && <code>{record.preview}</code>}
        </span>
        {running && <LoaderCircle className="trajectory-record-running spin" />}
        <time>{formatDuration(record.durationMs)}</time>
        {expanded ? <ChevronDown className="trajectory-record-chevron" /> : <ChevronRight className="trajectory-record-chevron" />}
      </button>
      {expanded && <TrajectoryRecordDetails record={record} />}
    </article>
  );
}

export function TrajectoryView({ items, now }: { items: ChatItem[]; now: number }) {
  const [actualDuration, setActualDuration] = useState(true);
  const [collapsedTurns, setCollapsedTurns] = useState<ReadonlySet<number>>(() => new Set());
  const [callsCollapsed, setCallsCollapsed] = useState(false);
  const [query, setQuery] = useState("");
  const turns = useMemo(() => deriveTrajectory(items, now), [items, now]);
  const records = useMemo(() => turns.flatMap((turn) => turn.records), [turns]);
  const positioned = useMemo(
    () => timelinePositions(records, actualDuration),
    [actualDuration, records],
  );
  const needle = query.trim().toLocaleLowerCase();
  const allTurnsCollapsed = turns.length > 0 && turns.every((turn) => collapsedTurns.has(turn.number));
  const toggleAllTurns = () => {
    setCollapsedTurns(allTurnsCollapsed
      ? new Set()
      : new Set(turns.map((turn) => turn.number)));
  };
  const toggleTurn = (turnNumber: number) => {
    setCollapsedTurns((current) => {
      const next = new Set(current);
      if (next.has(turnNumber)) next.delete(turnNumber);
      else next.add(turnNumber);
      return next;
    });
  };
  const visibleTurns = turns.map((turn) => ({
    ...turn,
    records: turn.records.filter((record) => (
      (!callsCollapsed || record.lane !== "tools")
      && (!needle || record.searchable.includes(needle))
    )),
  })).filter((turn) => turn.records.length > 0);

  return (
    <section className="trajectory-view" aria-label="Agent 轨迹">
      <div className="trajectory-toolbar" role="toolbar" aria-label="轨迹显示设置">
        <div className="trajectory-toolbar-actions">
          <button
            type="button"
            className={actualDuration ? "active" : ""}
            aria-pressed={actualDuration}
            title={actualDuration ? "按事件顺序等宽显示" : "按实际耗时显示"}
            onClick={() => setActualDuration((value) => !value)}
          ><Clock3 />Duration</button>
          <button
            type="button"
            className={allTurnsCollapsed ? "active" : ""}
            aria-pressed={allTurnsCollapsed}
            title={allTurnsCollapsed ? "展开全部 Turn" : "折叠全部 Turn"}
            onClick={toggleAllTurns}
          ><span aria-hidden="true">{allTurnsCollapsed ? "⊞" : "⊟"}</span>Turns</button>
          <button
            type="button"
            className={callsCollapsed ? "active" : ""}
            aria-pressed={callsCollapsed}
            title={callsCollapsed ? "显示工具调用" : "隐藏工具调用"}
            onClick={() => setCallsCollapsed((value) => !value)}
          ><Wrench />Calls</button>
        </div>
        <label className="trajectory-search">
          <Search />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索轨迹"
            aria-label="搜索轨迹"
          />
          {needle && <span>{visibleTurns.reduce((sum, turn) => sum + turn.records.length, 0)}</span>}
        </label>
      </div>

      <div className="trajectory-overview" aria-label="轨迹总览">
        <div className="trajectory-lane-labels" aria-hidden="true">
          <span>Input</span><span>Model</span><span>Tools</span>
        </div>
        <div className="trajectory-timeline">
          <i className="trajectory-lane-rule lane-input" />
          <i className="trajectory-lane-rule lane-model" />
          <i className="trajectory-lane-rule lane-tools" />
          {positioned.map((record) => {
            const style = {
              "--trajectory-left": `${record.left}%`,
              "--trajectory-width": `${record.width}%`,
            } as CSSProperties;
            const muted = Boolean(needle && !record.searchable.includes(needle));
            return (
              <button
                type="button"
                key={record.item.id}
                className={`trajectory-timeline-block lane-${record.lane} ${muted ? "muted" : ""}`}
                style={style}
                title={`#${record.index} ${record.label} · ${record.title}${record.durationMs === null ? "" : ` · ${formatDuration(record.durationMs)}`}`}
                aria-label={`定位到轨迹 ${record.index}：${record.title}`}
                onClick={() => document.getElementById(`trajectory-record-${record.index}`)?.scrollIntoView({ block: "center" })}
              />
            );
          })}
        </div>
      </div>

      <div className="trajectory-ledger">
        {records.length === 0 ? (
          <div className="trajectory-empty"><Wrench /><strong>还没有轨迹</strong><span>发送消息后，模型与工具调用会实时出现在这里。</span></div>
        ) : visibleTurns.length === 0 ? (
          <div className="trajectory-empty"><Search /><strong>没有匹配的轨迹</strong><span>试试工具名、命令、文件路径或回复内容。</span></div>
        ) : visibleTurns.map((turn) => {
          const collapsed = collapsedTurns.has(turn.number);
          return (
            <section className="trajectory-turn" key={turn.number}>
              <button
                type="button"
                className="trajectory-turn-header"
                aria-expanded={!collapsed}
                onClick={() => toggleTurn(turn.number)}
              >
                {collapsed ? <ChevronRight /> : <ChevronDown />}
                <strong>Turn {turn.number}</strong>
                <span>{turn.records.length} 条记录</span>
                {turn.durationMs !== null && <time>{formatDuration(turn.durationMs)}</time>}
              </button>
              {!collapsed && <div className="trajectory-turn-records">{turn.records.map((record) => <TrajectoryLedgerRow key={record.item.id} record={record} />)}</div>}
            </section>
          );
        })}
      </div>
    </section>
  );
}
