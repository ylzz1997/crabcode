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
  const count = (value: number | null | undefined) => value == null ? "未记录" : `${value.toLocaleString("zh-CN")} 次`;
  const time = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "未记录";
  const reasons: Record<string, string> = { end_turn: "正常结束", stop: "正常结束", interrupted: "已中断", max_turns_reached: "达到轮数上限", empty_response: "空回复", mode_switch_requested: "切换模式" };
  const usage = details.usage ?? {};
  const token = (value: unknown) => typeof value === "number" ? value.toLocaleString("zh-CN") : "未记录";
  return [
    ["启动时间", time(details.started_at)], ["结束时间", time(details.ended_at)],
    ["耗时", details.duration_ms == null ? "未记录" : `${(details.duration_ms / 1000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 秒`],
    ["工具调用次数", count(details.tool_call_count)], ["思考次数", count(details.thinking_count)],
    ["模型请求次数", count(details.request_count)], ["重试次数", count(details.retry_count)],
    ["压缩次数", count(details.compact_count)],
    ["模型", [details.provider, details.model].filter(Boolean).join(" / ") || "未记录"],
    ["结束原因", details.reason ? reasons[details.reason] ?? details.reason : "未记录"],
    ["输入 tokens", token(usage.total_input_tokens ?? usage.input_tokens)],
    ["输出 tokens", token(usage.output_tokens)],
    ["缓存读取 tokens", token(usage.cache_read_tokens)],
    ["缓存写入 tokens", token(usage.cache_write_tokens)],
  ];
}
