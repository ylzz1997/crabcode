import { getLocale } from "./i18n";
import { t as tr } from "./i18n";
import { useCallback, useEffect, useMemo, useState } from "react";
import { GatewayApi } from "./gateway";
import type { ConnectionPreset, ProjectPreset, UsageDailyResponse } from "./types";
import "./UsageSettingsPanel.css";

function localDate(day: Date): string {
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

function initialRanges() {
  const today = new Date();
  const week = new Date(today);
  week.setDate(today.getDate() - 6);
  return { end: localDate(today), monthStart: localDate(new Date(today.getFullYear(), today.getMonth(), 1)),
           weekStart: localDate(week) };
}

function rangeError(start: string, end: string): string | null {
  const length = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000;
  if (!Number.isFinite(length)) return tr("请选择有效的开始和结束日期");
  if (length < 0) return tr("结束日期不能早于开始日期");
  if (length >= 366) return tr("一次最多查询 366 天");
  if (end > localDate(new Date())) return tr("结束日期不能晚于今天");
  return null;
}

const COLORS = ["var(--accent)", "var(--orange)", "var(--green)", "var(--red)",
                "#ad85d4", "#baa044"];
const numberFormat = { format: (value: number) => new Intl.NumberFormat(getLocale()).format(value) };
const DISMISSED_MODELS_KEY = "crabcode.usage.dismissed-models.v1";

function readDismissedModels(key: string): Record<string, number> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(key) ?? "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter((entry): entry is [string, number] =>
      typeof entry[1] === "number" && Number.isSafeInteger(entry[1]) && entry[1] >= 0));
  } catch { return {}; }
}

function saveDismissedModels(key: string, dismissed: Record<string, number>) {
  try {
    if (Object.keys(dismissed).length) localStorage.setItem(key, JSON.stringify(dismissed));
    else localStorage.removeItem(key);
  } catch { /* Storage may be unavailable; keep this page's state usable. */ }
}

function useUsage(api: GatewayApi | null, online: boolean, start: string, end: string,
                  timezone: string, scope: "global" | "project", cwd?: string) {
  const [result, setResult] = useState<{ key: string; data: UsageDailyResponse } | null>(null);
  const [failure, setFailure] = useState<{ key: string; error: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const invalid = rangeError(start, end);
  const dataKey = `${api?.connection.id ?? ""}|${api?.baseUrl ?? ""}|${api?.connection.credential_ref ?? ""}|${start}|${end}|${timezone}|${scope}|${cwd ?? ""}`;
  const retry = useCallback(() => setRefresh((value) => value + 1), []);
  useEffect(() => {
    if (!api || !online || invalid || (scope === "project" && !cwd)) return;
    const controller = new AbortController();
    setLoading(true);
    void api.usageDaily(start, end, timezone, scope, cwd, controller.signal)
      .then((data) => { if (!controller.signal.aborted) { setResult({ key: dataKey, data }); setFailure(null); } })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          const message = reason instanceof Error ? reason.message : String(reason);
          setFailure({ key: dataKey, error: message.startsWith("404") || message === "Not Found"
            ? tr("当前 Gateway 版本暂不支持使用情况，请升级 Gateway") : message });
        }
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, online, start, end, timezone, scope, cwd, invalid, refresh, dataKey]);
  return { data: result?.key === dataKey && online && !invalid ? result.data : null,
           error: invalid ?? (failure?.key === dataKey ? failure.error : null),
           loading: loading && online, retry };
}

function DateRange({ label, start, end, onStart, onEnd }: {
  label: string; start: string; end: string;
  onStart: (value: string) => void; onEnd: (value: string) => void;
}) {
  return <div className="usage-date-range" aria-label={tr("{0}日期范围", { 0: label })}>
    <label>{tr("开始 ")}<input type="date" max={localDate(new Date())} aria-label={tr("{0}开始日期", { 0: label })} value={start} onChange={(event) => onStart(event.target.value)} /></label>
    <span aria-hidden="true">—</span>
    <label>{tr("结束 ")}<input type="date" max={localDate(new Date())} aria-label={tr("{0}结束日期", { 0: label })} value={end} onChange={(event) => onEnd(event.target.value)} /></label>
  </div>;
}

function CoverageNote({ data }: { data: UsageDailyResponse }) {
  const unavailable = data.days.filter((day) => day.coverage === "unavailable").length;
  const partial = data.days.filter((day) => day.coverage === "partial").length;
  return <p className="usage-coverage">{tr("仅统计本机 Gateway 开始记录后、服务商实际返回的 Token；历史记录不回填。")}{data.tracking_started_at && tr(" 精确统计开始于 {0}。", { 0: new Date(data.tracking_started_at).toLocaleString(getLocale()) })}
    {unavailable > 0 && tr(" {0} 天无完整历史记录。", { 0: unavailable })}
    {partial > 0 && tr(" {0} 天数据不完整。", { 0: partial })}
    {data.summary.unknown_requests > 0 && tr(" {0} 次请求未返回完整用量。", { 0: data.summary.unknown_requests })}
  </p>;
}

function Heatmap({ data }: { data: UsageDailyResponse }) {
  const [focusedDay, setFocusedDay] = useState<{ data: UsageDailyResponse; text: string } | null>(null);
  const max = Math.max(1, ...data.days.map((day) => day.total_tokens ?? 0));
  const months = [...new Set(data.days.map((day) => day.date.slice(0, 7)))];
  const byDate = new Map(data.days.map((day) => [day.date, day]));
  const today = localDate(new Date());
  return <>
    <div className="usage-months">
      {months.map((month) => {
        const [year, monthNumber] = month.split("-").map(Number);
        const first = new Date(year, monthNumber - 1, 1);
        const offset = (first.getDay() + 6) % 7;
        const count = new Date(year, monthNumber, 0).getDate();
        return <div key={month} className="usage-month"><h4>{year}{tr(" 年 ")}{monthNumber}{tr(" 月")}</h4>
          <div className="usage-heatmap" aria-label={tr("{0} Token 日历", { 0: month })}>
            {[tr("一"), tr("二"), tr("三"), tr("四"), tr("五"), tr("六"), tr("日")].map((weekday) =>
              <span key={weekday} className="usage-weekday" aria-hidden="true">{weekday}</span>)}
            {Array.from({ length: offset }, (_, index) => <span key={`blank-${index}`} aria-hidden="true" />)}
            {Array.from({ length: count }, (_, index) => {
              const date = `${month}-${String(index + 1).padStart(2, "0")}`;
              const day = byDate.get(date);
              if (!day) return <span key={date} className="usage-heatmap-cell outside" aria-hidden="true">{index + 1}</span>;
              const amount = day.total_tokens ?? 0;
              const level = amount === 0 ? 0 : Math.max(1, Math.ceil(Math.log1p(amount) / Math.log1p(max) * 5));
              const tooltip = `${date}：${day.total_tokens === null ? tr("未记录") : `${numberFormat.format(amount)} Token`}`
                + tr("；输入 {0}，输出 {1}", { 0: numberFormat.format(day.input_tokens), 1: numberFormat.format(day.output_tokens) })
                + tr("；{0} 次请求；{1}", { 0: day.request_count, 1: day.coverage === "complete" ? tr("完整") : day.coverage === "partial" ? tr("部分数据") : tr("未记录") });
              return <span key={date} className={`usage-heatmap-cell level-${level} ${day.coverage} ${date === today ? "today" : ""}`}
                title={tooltip} aria-label={tooltip} tabIndex={0}
                onFocus={() => setFocusedDay({ data, text: tooltip })}
                onMouseEnter={() => setFocusedDay({ data, text: tooltip })}>{index + 1}</span>;
            })}
          </div>
        </div>;
      })}
    </div>
    {focusedDay?.data === data && <p className="usage-day-detail" role="status">{focusedDay.text}</p>}
    <div className="usage-heatmap-legend">{tr("少 ")}<span className="usage-heatmap-cell level-0" />
      {[1, 2, 3, 4, 5].map((level) => <span key={level} className={`usage-heatmap-cell level-${level}`} />)}{tr(" 多")}<span className="usage-legend-partial">{tr("色阶按当前范围归一化 · ◩ 不完整 · 淡色未记录")}</span>
    </div>
    <details className="usage-details"><summary>{tr("查看每日总量明细")}</summary>
      <div className="usage-table-scroll"><table><thead><tr><th>{tr("日期")}</th><th>{tr("输入")}</th><th>{tr("输出")}</th><th>{tr("总量")}</th><th>{tr("请求")}</th><th>{tr("覆盖")}</th></tr></thead>
        <tbody>{data.days.map((day) => <tr key={day.date}><th>{day.date}</th>
          <td>{numberFormat.format(day.input_tokens)}</td><td>{numberFormat.format(day.output_tokens)}</td>
          <td>{day.total_tokens === null ? tr("未记录") : numberFormat.format(day.total_tokens)}</td>
          <td>{day.request_count}</td><td>{day.coverage === "complete" ? tr("完整") : day.coverage === "partial" ? tr("部分") : tr("未记录")}</td>
        </tr>)}</tbody></table></div>
    </details>
  </>;
}

function Trend({ data, storageKey }: { data: UsageDailyResponse; storageKey: string }) {
  type TooltipItem = { key: string; model: string; tokens: number; color: string };
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [dismissed, setDismissed] = useState<Record<string, number>>(() => readDismissedModels(storageKey));
  const [contextModel, setContextModel] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [tooltip, setTooltip] = useState<{
    date: string; items: TooltipItem[]; left: number; top: number; width: number;
  } | null>(null);
  useEffect(() => setTooltip(null), [data]);
  useEffect(() => {
    const counts = new Map(data.models.map((model) => [model.key, model.recorded_request_count]));
    setDismissed((current) => {
      const next = { ...current };
      let changed = false;
      for (const [key, checkpoint] of Object.entries(current)) {
        const count = counts.get(key);
        if (count !== undefined && count > checkpoint) { delete next[key]; changed = true; }
      }
      if (changed) saveDismissedModels(storageKey, next);
      return changed ? next : current;
    });
  }, [data, storageKey]);
  useEffect(() => {
    if (!contextModel) return;
    const close = () => setContextModel(null);
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", closeOnEscape); };
  }, [contextModel]);
  const showTooltip = (target: SVGCircleElement, date: string, items: TooltipItem[]) => {
    const chart = target.ownerSVGElement?.parentElement;
    const scroll = chart?.parentElement;
    if (!chart || !scroll) return;
    const pointBounds = target.getBoundingClientRect();
    const chartBounds = chart.getBoundingClientRect();
    const centerX = pointBounds.left + pointBounds.width / 2 - chartBounds.left;
    const centerY = pointBounds.top + pointBounds.height / 2 - chartBounds.top;
    const tooltipWidth = Math.min(232, Math.max(0, scroll.clientWidth - 16));
    const visibleLeft = scroll.scrollLeft + 8;
    const visibleRight = scroll.scrollLeft + scroll.clientWidth - 8;
    const preferredLeft = centerX + 14 + tooltipWidth > visibleRight
      ? centerX - tooltipWidth - 14 : centerX + 14;
    const left = Math.max(visibleLeft, Math.min(preferredLeft, visibleRight - tooltipWidth));
    const estimatedHeight = 42 + items.length * 52;
    const top = centerY >= estimatedHeight + 12 ? centerY - estimatedHeight - 10 : centerY + 16;
    setTooltip({ date, items, left, top, width: tooltipWidth });
  };
  const width = 760, height = 240, left = 52, right = 16, top = 15, bottom = 32;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const sorted = data.models.filter((model) => !(model.key in dismissed))
    .sort((a, b) => b.total_tokens - a.total_tokens || a.key.localeCompare(b.key));
  const leading = sorted.length > 8 ? sorted.slice(0, 7) : sorted;
  const extras = sorted.length > 8 ? sorted.slice(7) : [];
  const visible = [...leading, ...extras.filter((model) => expanded.has(model.key))];
  const remaining = extras.filter((model) => !expanded.has(model.key));
  const other = remaining.length ? {
    key: "__other__", model: tr("其他模型"), provider: "", model_id: "", total_tokens: remaining.reduce((sum, model) => sum + model.total_tokens, 0),
    points: data.days.map((day, index) => ({ date: day.date,
      total_tokens: day.coverage === "unavailable" ? null : remaining.reduce((sum, model) => sum + (model.points[index]?.total_tokens ?? 0), 0) })),
  } : null;
  const models = other ? [...visible, other] : visible;
  const max = Math.max(1, ...models.flatMap((model) => model.points.map((point) => point.total_tokens ?? 0)));
  const colorFor = (key: string) => {
    if (key === "__other__") return "var(--muted)";
    let hash = 0;
    for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) | 0;
    return COLORS[Math.abs(hash) % COLORS.length];
  };
  const drawnModels = models.filter((model) => !hidden.has(model.key));
  const overlappingPoints = new Map<string, TooltipItem[]>();
  for (const model of drawnModels) {
    model.points.forEach((point, dayIndex) => {
      if (point.total_tokens === null) return;
      const key = `${dayIndex}:${point.total_tokens}`;
      const items = overlappingPoints.get(key) ?? [];
      items.push({ key: model.key, model: model.model, tokens: point.total_tokens, color: colorFor(model.key) });
      overlappingPoints.set(key, items);
    });
  }
  const restoreAll = () => { setDismissed({}); saveDismissedModels(storageKey, {}); };
  const restoreModel = (key: string) => {
    const next = { ...dismissed };
    delete next[key];
    setDismissed(next);
    saveDismissedModels(storageKey, next);
  };
  const dismiss = (key: string) => {
    const model = data.models.find((item) => item.key === key);
    if (!model) return;
    const next = { ...dismissed, [key]: model.recorded_request_count ?? 0 };
    setDismissed(next);
    setHidden((current) => { const updated = new Set(current); updated.delete(key); return updated; });
    saveDismissedModels(storageKey, next);
    setContextModel(null);
    setTooltip(null);
  };
  return <>
    {data.models.length === 0 ? <p className="usage-empty">{tr("所选日期内没有已报告的模型 Token 用量。")}</p>
      : sorted.length === 0 ? <p className="usage-empty">{tr("当前范围的模型均已从趋势图移除。历史用量仍保留。")}</p> : <>
    <div className="usage-trend-scroll" onScroll={() => setTooltip(null)}>
      <div className="usage-trend-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={tr("不同模型逐日 Token 用量折线图")}>
        {[0, 1, 2, 3, 4].map((step) => {
          const y = top + step * plotHeight / 4;
          return <g key={step}><line x1={left} x2={width - right} y1={y} y2={y} className="usage-gridline" />
            <text x={left - 8} y={y + 4} textAnchor="end" className="usage-axis-text">{numberFormat.format(Math.round(max * (4 - step) / 4))}</text></g>;
        })}
        {drawnModels.map((model, index) => {
          const color = colorFor(model.key);
          const points = model.points.map((point, dayIndex) => {
            const x = left + (data.days.length === 1 ? plotWidth / 2 : dayIndex * plotWidth / (data.days.length - 1));
            const y = point.total_tokens === null ? null : top + plotHeight * (1 - point.total_tokens / max);
            return { ...point, x, y };
          });
          const segments: (typeof points)[] = [];
          let previousIndex = -2;
          for (const [dayIndex, point] of points.entries()) {
            if (point.y === null) continue;
            if (dayIndex !== previousIndex + 1) segments.push([]);
            segments[segments.length - 1].push(point);
            previousIndex = dayIndex;
          }
          return <g key={model.key}>
            {segments.map((segment, segmentIndex) => <polyline key={segmentIndex}
              points={segment.map((point) => `${point.x},${point.y}`).join(" ")}
              fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" pointerEvents="none"
              strokeDasharray={model.key === "__other__" || index % 3 === 1 ? "7 4" : undefined} />)}
            {points.length <= 31 && points.map((point, dayIndex) => {
              if (point.y === null) return null;
              const items = overlappingPoints.get(`${dayIndex}:${point.total_tokens}`) ?? [];
              if (items[0]?.key !== model.key) return null;
              const shared = items.length > 1;
              return <g key={point.date}>
                <circle cx={point.x} cy={point.y!} r={shared ? 5 : 4}
                  fill={shared ? "var(--text)" : color} pointerEvents="none" />
                {shared && <text x={point.x + 9} y={point.y - 9} className="usage-overlap-count"
                  aria-hidden="true" pointerEvents="none">{items.length}</text>}
                <circle className="usage-trend-hit" cx={point.x} cy={point.y!} r="11" fill="transparent"
                  tabIndex={0} role="img"
                  aria-label={items.map((item) => `${item.model}，${point.date}，${numberFormat.format(item.tokens)} Token`).join("；")}
                  onMouseEnter={(event) => showTooltip(event.currentTarget, point.date, items)}
                  onMouseLeave={() => setTooltip(null)}
                  onFocus={(event) => showTooltip(event.currentTarget, point.date, items)}
                  onBlur={() => setTooltip(null)} />
              </g>;
            })}
          </g>;
        })}
        <text x={left} y={height - 7} className="usage-axis-text">{data.start}</text>
        <text x={width - right} y={height - 7} textAnchor="end" className="usage-axis-text">{data.end}</text>
      </svg>
      {tooltip && <div className="usage-trend-tooltip" role="tooltip"
        style={{ left: tooltip.left, top: tooltip.top, width: tooltip.width }}>
        <span className="usage-trend-tooltip-date">{tooltip.date}</span>
        {tooltip.items.length > 1 && <span className="usage-trend-tooltip-count">{tr("同一点 · ")}{tooltip.items.length}{tr(" 个模型")}</span>}
        {tooltip.items.map((item) => <div key={item.key} className="usage-trend-tooltip-item">
          <strong><i style={{ background: item.color }} />{item.model}</strong>
          <span className="usage-trend-tooltip-value">{numberFormat.format(item.tokens)} <small>Token</small></span>
        </div>)}
      </div>}
      </div>
    </div>
    <div className="usage-model-legend">{models.map((model) =>
      <span key={model.key} className="usage-model-legend-item"><button type="button" aria-pressed={!hidden.has(model.key)}
        title={model.key === "__other__" ? tr("点击显示或隐藏曲线") : tr("点击显示或隐藏曲线；右键可从趋势图移除")}
        onContextMenu={(event) => { if (model.key !== "__other__") { event.preventDefault(); setContextModel(model.key); } }}
        onKeyDown={(event) => { if (model.key !== "__other__" && (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) { event.preventDefault(); setContextModel(model.key); } }}
        onClick={() => { setTooltip(null); setHidden((current) => { const next = new Set(current); if (next.has(model.key)) next.delete(model.key); else next.add(model.key); return next; }); }}>
        <i style={{ background: colorFor(model.key) }} />{model.model} · {numberFormat.format(model.total_tokens)}
      </button>{contextModel === model.key && <div className="usage-model-context-menu" role="menu" onPointerDown={(event) => event.stopPropagation()}>
        <button type="button" role="menuitem" onClick={() => dismiss(model.key)}>{tr("从趋势图移除")}</button>
        <small>{tr("不删除历史用量；该模型有新请求后自动恢复。")}</small>
      </div>}</span>)}</div>
    <p className="usage-model-note">{tr("点击模型切换曲线；右键可从图中移除，历史用量与总计不变。")}</p>
    {extras.length > 0 && <details className="usage-extra-models"><summary>{tr("选择更多模型（")}{extras.length}）</summary>
      {extras.map((model) => <label key={model.key}><input type="checkbox" checked={expanded.has(model.key)}
        onChange={() => setExpanded((current) => { const next = new Set(current); if (next.has(model.key)) next.delete(model.key); else next.add(model.key); return next; })} />
        {model.model} · {numberFormat.format(model.total_tokens)}</label>)}
    </details>}
    </>}
    {Object.keys(dismissed).length > 0 && <details className="usage-restore-models">
      <summary>{tr("恢复已移除模型（")}{Object.keys(dismissed).length}）</summary>
      <div className="usage-restore-panel">
        <p>{tr("选择要恢复的模型；不会修改历史用量。若所选日期内没有该模型的记录，恢复后仍不会出现在图中。")}</p>
        <ul>{Object.keys(dismissed).sort().map((key) => <li key={key}>
          <span><i style={{ background: colorFor(key) }} />{key}</span>
          <button type="button" aria-label={tr("恢复模型 {0}", { 0: key })} onClick={() => restoreModel(key)}>{tr("恢复")}</button>
        </li>)}</ul>
        <button type="button" className="usage-restore-all" onClick={restoreAll}>{tr("全部恢复")}</button>
      </div>
    </details>}
    <details className="usage-details"><summary>{tr("查看逐日明细")}</summary>
      <div className="usage-table-scroll"><table><thead><tr><th>{tr("日期")}</th>{data.models.map((model) => <th key={model.key}>{model.model}</th>)}</tr></thead>
        <tbody>{data.days.map((day, index) => <tr key={day.date}><th>{day.date}</th>
          {data.models.map((model) => <td key={model.key}>{model.points[index]?.total_tokens === null ? tr("未记录") : numberFormat.format(model.points[index]?.total_tokens ?? 0)}</td>)}
        </tr>)}</tbody></table></div>
    </details>
  </>;
}

export function UsageSettingsPanel({ connection, project, online }: {
  connection: ConnectionPreset | null; project: ProjectPreset | null; online: boolean;
}) {
  const defaults = useMemo(initialRanges, [getLocale()]);
  const [heatStart, setHeatStart] = useState(defaults.monthStart);
  const [heatEnd, setHeatEnd] = useState(defaults.end);
  const [lineStart, setLineStart] = useState(defaults.weekStart);
  const [lineEnd, setLineEnd] = useState(defaults.end);
  const [heatApplied, setHeatApplied] = useState({ start: defaults.monthStart, end: defaults.end });
  const [lineApplied, setLineApplied] = useState({ start: defaults.weekStart, end: defaults.end });
  const [heatValidation, setHeatValidation] = useState<string | null>(null);
  const [lineValidation, setLineValidation] = useState<string | null>(null);
  const [selectedHeatPreset, setSelectedHeatPreset] = useState<"month" | "previous" | "thirty" | null>("month");
  const [selectedLinePreset, setSelectedLinePreset] = useState<"seven" | "thirty" | "month" | null>("seven");
  const [scope, setScope] = useState<"global" | "project">("global");
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const api = useMemo(() => connection ? new GatewayApi(connection) : null,
    [connection?.id, connection?.base_url, connection?.credential_ref, getLocale()]);
  const cwd = project?.path;
  const heat = useUsage(api, online, heatApplied.start, heatApplied.end, zone, scope, cwd);
  const line = useUsage(api, online, lineApplied.start, lineApplied.end, zone, scope, cwd);
  const heatRetry = heat.retry, lineRetry = line.retry;
  useEffect(() => {
    if (!online) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden") { heatRetry(); lineRetry(); }
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [online, heatRetry, lineRetry]);
  const modelStorageKey = `${DISMISSED_MODELS_KEY}:${JSON.stringify([connection?.id, connection?.base_url, scope, scope === "project" ? cwd : null])}`;
  const heatDateChanged = heatStart !== heatApplied.start || heatEnd !== heatApplied.end;
  const lineDateChanged = lineStart !== lineApplied.start || lineEnd !== lineApplied.end;
  const applyHeat = () => {
    if (!heatDateChanged) return;
    const error = rangeError(heatStart, heatEnd);
    setHeatValidation(error);
    if (!error) {
      setSelectedHeatPreset(null);
      setHeatApplied({ start: heatStart, end: heatEnd });
    }
  };
  const applyLine = () => {
    if (!lineDateChanged) return;
    const error = rangeError(lineStart, lineEnd);
    setLineValidation(error);
    if (!error) {
      setSelectedLinePreset(null);
      setLineApplied({ start: lineStart, end: lineEnd });
    }
  };
  const setHeatPreset = (kind: "month" | "previous" | "thirty") => {
    const now = new Date();
    let start = new Date(now.getFullYear(), now.getMonth(), 1);
    let end = now;
    if (kind === "previous") {
      start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      end = new Date(now.getFullYear(), now.getMonth(), 0);
    } else if (kind === "thirty") {
      start = new Date(now);
      start.setDate(now.getDate() - 29);
    }
    const range = { start: localDate(start), end: localDate(end) };
    setHeatStart(range.start); setHeatEnd(range.end);
    setSelectedHeatPreset(kind);
    setHeatValidation(null);
    if (heatApplied.start === range.start && heatApplied.end === range.end) heat.retry();
    else setHeatApplied(range);
  };
  const setLinePreset = (kind: "seven" | "thirty" | "month") => {
    const now = new Date();
    const start = kind === "month" ? new Date(now.getFullYear(), now.getMonth(), 1) : new Date(now);
    if (kind !== "month") start.setDate(now.getDate() - (kind === "seven" ? 6 : 29));
    const range = { start: localDate(start), end: localDate(now) };
    setLineStart(range.start); setLineEnd(range.end);
    setSelectedLinePreset(kind);
    setLineValidation(null);
    if (lineApplied.start === range.start && lineApplied.end === range.end) line.retry();
    else setLineApplied(range);
  };
  return <section className="settings-section usage-settings" aria-labelledby="usage-settings-title">
    <div className="settings-section-heading"><div>
      <h2 id="usage-settings-title">{tr("使用情况")}</h2>
      <p>{tr("查看当前 Gateway 实际报告的 Token 用量，不包含费用估算。")}</p>
    </div></div>
    <div className="usage-scope">
      <label>{tr("统计范围 ")}<select value={scope} onChange={(event) => setScope(event.target.value as "global" | "project")}>
        <option value="global">{tr("当前 Gateway 全部项目")}</option>
        <option value="project" disabled={!project}>{tr("当前项目")}{project ? `：${project.name}` : tr("（未选择）")}</option>
      </select></label>
      <span>Gateway：{connection?.name ?? tr("未连接")}{tr(" · 时区：")}{zone}</span>
      <button type="button" onClick={() => { heat.retry(); line.retry(); }} disabled={!online}>{tr("刷新")}</button>
    </div>
    {!online && <p role="status" className="usage-notice">{tr("连接 Gateway 后才能查看使用情况。")}</p>}
    {scope === "project" && !cwd && <p role="status" className="usage-notice">{tr("请先选择项目。")}</p>}
    <div className="usage-card">
      <div className="usage-card-header"><div><h3>{tr("Token 使用热力图")}</h3><p>{tr("默认显示本月；颜色越深，用量越高。")}</p></div>
        <DateRange label={tr("热力图")} start={heatStart} end={heatEnd} onStart={setHeatStart} onEnd={setHeatEnd} /></div>
      <div className="usage-actions"><button type="button" aria-pressed={selectedHeatPreset === "month"} onClick={() => setHeatPreset("month")}>{tr("本月")}</button>
        <button type="button" aria-pressed={selectedHeatPreset === "previous"} onClick={() => setHeatPreset("previous")}>{tr("上月")}</button>
        <button type="button" aria-pressed={selectedHeatPreset === "thirty"} onClick={() => setHeatPreset("thirty")}>{tr("最近 30 天")}</button>
        <button type="button" className="primary" onClick={applyHeat} disabled={!online || !heatDateChanged}
          title={heatDateChanged ? tr("查询手动选择的日期范围") : tr("请先修改日期；刷新当前范围可使用上方的刷新按钮")}>{tr("查询所选日期")}</button></div>
      {heatValidation && <p role="alert" className="usage-error">{heatValidation}</p>}
      {heat.loading && !heat.data && <div role="status" aria-label={tr("正在加载每日用量")} className="usage-skeleton heat" />}
      {heat.error && <p role="alert" className="usage-error">{heat.error} <button type="button" onClick={heat.retry}>{tr("重试")}</button></p>}
      {heat.data && <><div className="usage-total">{heat.data.days.some((day) => day.coverage !== "unavailable")
          ? numberFormat.format(heat.data.summary.total_tokens) : "—"}
        <small>{heat.data.days.some((day) => day.coverage !== "unavailable") ? tr("已报告 Token") : tr("未记录")} · {heat.data.start}{tr(" 至 ")}{heat.data.end}</small></div>
        {heat.data.tracking_started_at ? <Heatmap data={heat.data} /> : <p className="usage-empty">{tr("尚未开始记录使用情况。完成一次模型请求后再刷新。")}</p>}
        {heat.data.tracking_started_at && heat.data.summary.request_count === 0 && <p className="usage-empty">{tr("所选日期内没有已记录的模型请求。")}</p>}
        <CoverageNote data={heat.data} /></>}
    </div>
    <div className="usage-card">
      <div className="usage-card-header"><div><h3>{tr("各模型趋势")}</h3><p>{tr("默认显示最近七天；按请求开始日期归属。")}</p></div>
        <DateRange label={tr("折线图")} start={lineStart} end={lineEnd} onStart={setLineStart} onEnd={setLineEnd} /></div>
      <div className="usage-actions"><button type="button" aria-pressed={selectedLinePreset === "seven"} onClick={() => setLinePreset("seven")}>{tr("近 7 天")}</button>
        <button type="button" aria-pressed={selectedLinePreset === "thirty"} onClick={() => setLinePreset("thirty")}>{tr("近 30 天")}</button>
        <button type="button" aria-pressed={selectedLinePreset === "month"} onClick={() => setLinePreset("month")}>{tr("本月")}</button>
        <button type="button" className="primary" onClick={applyLine} disabled={!online || !lineDateChanged}
          title={lineDateChanged ? tr("查询手动选择的日期范围") : tr("请先修改日期；刷新当前范围可使用上方的刷新按钮")}>{tr("查询所选日期")}</button></div>
      {lineValidation && <p role="alert" className="usage-error">{lineValidation}</p>}
      {line.loading && !line.data && <div role="status" aria-label={tr("正在加载模型趋势")} className="usage-skeleton trend" />}
      {line.error && <p role="alert" className="usage-error">{line.error} <button type="button" onClick={line.retry}>{tr("重试")}</button></p>}
      {line.data && <><div className="usage-total">{line.data.days.some((day) => day.coverage !== "unavailable")
          ? numberFormat.format(line.data.summary.total_tokens) : "—"}
        <small>{line.data.days.some((day) => day.coverage !== "unavailable") ? tr("已报告 Token") : tr("未记录")} · {line.data.start}{tr(" 至 ")}{line.data.end}</small></div>
        {line.data.tracking_started_at ? <Trend key={modelStorageKey} data={line.data} storageKey={modelStorageKey} /> : <p className="usage-empty">{tr("尚未开始记录使用情况。")}</p>}
        <CoverageNote data={line.data} /></>}
    </div>
  </section>;
}
