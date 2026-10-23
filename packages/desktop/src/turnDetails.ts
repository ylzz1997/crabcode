import { getLocale } from "./i18n";
import { t as tr } from "./i18n";
import type { ChatItem } from "./types";

export interface TurnDetails {
  session_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  tool_call_count?: number;
  thinking_count?: number;
  request_count?: number;
  retry_count?: number | null;
  compact_count?: number | null;
  model?: string;
  provider?: string;
  reason?: string | null;
  usage?: Record<string, unknown>;
  source?: "recorded" | "history";
}

/** Keep completed rounds inspectable when an older Gateway omits metadata. */
export function withClientTurnDetails(items: ChatItem[], sessionId: string, busy = false): ChatItem[] {
  const result = [...items];
  let start = 0;
  const finish = (end: number, timing?: ChatItem) => {
    const round = result.slice(start, end);
    const assistant = [...round].reverse().find((item) => item.kind === "assistant" && item.status !== "running");
    if (!assistant || assistant.turnDetails) return;
    const lastUser = round.reduce((index, item, i) => item.kind === "user" ? i : index, -1);
    if (!timing && lastUser > round.indexOf(assistant)) return;
    const startedAt = timing?.startedAt ?? round.find((item) => item.kind === "user")?.startedAt;
    const endedAt = timing?.completedAt ?? assistant.completedAt;
    const toIso = (value: number | undefined) => value == null ? null : new Date(value).toISOString();
    const details: TurnDetails = {
      session_id: sessionId, source: "history",
      started_at: toIso(startedAt), ended_at: toIso(endedAt),
      duration_ms: startedAt == null || endedAt == null ? null : Math.max(0, endedAt - startedAt),
      tool_call_count: new Set(round.filter((item) => item.kind === "tool" && !item.agent_id).map((item) => item.tool_use_id ?? item.id)).size,
      thinking_count: round.filter((item) => item.kind === "thinking" && !item.agent_id).length,
    };
    result[start + round.indexOf(assistant)] = { ...assistant, turnDetails: details };
  };
  result.forEach((item, index) => {
    if (item.kind === "turn_duration") {
      finish(index, item);
      start = index + 1;
    }
  });
  if (!busy) finish(result.length);
  return result;
}

export function turnDetailRows(details: TurnDetails): [string, string][] {
  const count = (value: number | null | undefined) => value == null ? tr("未记录") : tr("{0} 次", { 0: value.toLocaleString(getLocale()) });
  const time = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString(getLocale(), { hour12: false }) : tr("未记录");
  const reasons: Record<string, string> = { end_turn: tr("正常结束"), stop: tr("正常结束"), interrupted: tr("已中断"), max_turns_reached: tr("达到轮数上限"), empty_response: tr("空回复"), mode_switch_requested: tr("切换模式") };
  const usage = details.usage ?? {};
  const token = (value: unknown) => typeof value === "number" ? value.toLocaleString(getLocale()) : tr("未记录");
  return [
    [tr("启动时间"), time(details.started_at)], [tr("结束时间"), time(details.ended_at)],
    [tr("耗时"), details.duration_ms == null ? tr("未记录") : tr("{0} 秒", { 0: (details.duration_ms / 1000).toLocaleString(getLocale(), { maximumFractionDigits: 2 }) })],
    [tr("工具调用次数"), count(details.tool_call_count)], [tr("思考次数"), count(details.thinking_count)],
    [tr("模型请求次数"), count(details.request_count)], [tr("重试次数"), count(details.retry_count)],
    [tr("压缩次数"), count(details.compact_count)],
    [tr("模型"), [details.provider, details.model].filter(Boolean).join(" / ") || tr("未记录")],
    [tr("结束原因"), details.reason ? reasons[details.reason] ?? details.reason : tr("未记录")],
    [tr("输入 tokens"), token(usage.total_input_tokens ?? usage.input_tokens)],
    [tr("输出 tokens"), token(usage.output_tokens)],
    [tr("缓存读取 tokens"), token(usage.cache_read_tokens)],
    [tr("缓存写入 tokens"), token(usage.cache_write_tokens)],
  ];
}
