import type { SlashCommandAction } from "../../shared/slashCommands.js";
import type { GatewayApi } from "./gateway";

type Fields = Record<string, unknown>;

export interface DesktopSlashContext {
  api: GatewayApi;
  sessionId: string;
  show: (text: string) => void;
  card: (title: string, text: string) => void;
  local: (message: SlashCommandAction) => Promise<boolean>;
}

function renderResult(value: unknown): string {
  if (value == null) return "操作完成";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.length ? value.map(renderResult).join("\n\n") : "暂无记录";
  const data = value as Fields;
  if (Array.isArray(data.lines)) return data.lines.join("\n") || "暂无输出";
  return JSON.stringify(value, null, 2);
}

/** Execute commands absent from the original Desktop handler; retain its existing UI actions. */
export async function executeAdditionalDesktopAction(message: SlashCommandAction, context: DesktopSlashContext): Promise<boolean> {
  if (await context.local(message)) return true;
  const { api, sessionId } = context;
  const id = (key: string) => encodeURIComponent(String(message[key] ?? ""));
  const get = (path: string, fields: Fields = {}) => {
    const query = new URLSearchParams({ session_id: sessionId });
    for (const [key, value] of Object.entries(fields)) {
      if (value != null) query.set(key, String(value));
    }
    return api.request(`${path}?${query}`);
  };
  const post = (path: string, fields: Fields = {}) => api.request(path, {
    method: "POST", body: JSON.stringify({ ...fields, session_id: sessionId }),
  });
  let result: unknown;
  let title: string;
  switch (message.type) {
    case "fetchPlanStatus": title = "计划状态"; result = await get("/config/plan-status"); break;
    case "fetchAgents": title = "Agent 列表"; result = await get("/agent/list"); break;
    case "fetchAgent": title = "Agent 详情"; result = await get(`/agent/${id("agentId")}`); break;
    case "fetchAgentLog": title = "Agent 输出"; result = await get(`/agent/${id("agentId")}/transcript`, { lines: message.lines }); break;
    case "sendAgentInput": title = "Agent 追加输入"; result = await post(`/agent/${id("agentId")}/input`, { prompt: message.prompt, interrupt: message.interrupt }); break;
    case "waitAgent": title = "等待 Agent"; result = await post("/agent/wait", { agent_id: message.agentIds ?? message.agentId, timeout_ms: message.timeoutMs }); break;
    case "cancelAgent":
      title = "取消 Agent";
      result = await api.request(`/agent/${id("agentId")}/cancel?${new URLSearchParams({ session_id: sessionId })}`, { method: "POST" });
      break;
    case "spawnAgent": title = "启动 Agent"; result = await post("/agent/spawn", { prompt: message.prompt, subagent_type: message.subagentType, name: message.name, model_profile: message.modelProfile, callback: message.callback }); break;
    case "fetchPeers": title = "其他会话"; result = await get("/peer/list"); break;
    case "sendPeerMessage": title = "会话消息"; result = await post("/peer/send", { to: message.to, text: message.text }); break;
    case "fetchGoal": title = "Goal 状态"; result = await get("/config/goal"); break;
    case "manageGoal":
      title = "管理 Goal";
      result = await post("/config/goal", { action: message.action, objective: message.objective,
        ...(message.budgetWasSet ? { token_budget: message.tokenBudget } : {}) });
      break;
    case "fetchTask": title = "后台任务详情"; result = await api.backgroundTask(String(message.taskId)); break;
    case "fetchTaskOutput": title = "后台任务输出"; result = await api.backgroundTaskOutput(String(message.taskId), Number(message.lines)); break;
    case "fetchSchedules":
      title = "定时任务";
      result = await get("/schedule/list", { status: message.status, schedule_type: message.scheduleType, enabled: message.enabled, limit: message.limit });
      break;
    case "fetchSchedule": title = "定时任务详情"; result = await get(`/schedule/${id("jobId")}`); break;
    case "fetchScheduleRuns": title = "定时任务执行历史"; result = await get(`/schedule/${id("jobId")}/runs`, { status: message.status, limit: message.limit }); break;
    case "createSchedule": title = "创建定时任务"; result = await post("/schedule/create", message.request as Fields); break;
    case "mutateSchedule": title = "管理定时任务"; result = await post(`/schedule/${message.action === "run" ? "trigger" : message.action}`, { job_id: message.jobId }); break;
    case "fetchLogs": title = "后台日志"; result = await get("/logs", { name: message.name, lines: message.tail ?? message.lines, clear: message.clear }); break;
    case "fetchTeams": title = "团队列表"; result = await get("/team/list"); break;
    case "createTeam": title = "创建团队"; result = await post("/team/create", { name: message.name, max_teammates: message.maxTeammates }); break;
    case "fetchTeamStatus": title = "团队状态"; result = await get(`/team/${id("teamId")}/status`); break;
    case "fetchTeamMessages": title = "团队消息"; result = await get(`/team/${id("teamId")}/messages`, { agent_id: message.agentId, unread: message.unread }); break;
    case "fetchTeamTasks": title = "团队任务板"; result = await get(`/team/${id("teamId")}/tasks`); break;
    case "spawnTeamMember": title = "添加团队成员"; result = await post("/team/spawn", { team_id: message.teamId, prompt: message.prompt, role: message.role, name: message.name, model_profile: message.modelProfile }); break;
    case "removeTeamMember": title = "移除团队成员"; result = await post("/team/remove", { team_id: message.teamId, agent_id: message.agentId }); break;
    case "sendTeamMessage": title = "发送团队消息"; result = await post("/team/message", { team_id: message.teamId, to: message.to, text: message.text, from_agent: message.fromAgent ?? "" }); break;
    case "broadcastTeamMessage": title = "广播团队消息"; result = await post("/team/broadcast", { team_id: message.teamId, text: message.text, from_agent: message.fromAgent ?? "" }); break;
    case "markTeamMessagesRead": title = "标记团队消息已读"; result = await post("/team/messages/read", { team_id: message.teamId, agent_id: message.agentId, message_ids: message.messageIds }); break;
    case "addTeamTask": title = "添加团队任务"; result = await post("/team/task/add", { team_id: message.teamId, description: message.description }); break;
    case "claimTeamTask": title = "认领团队任务"; result = await post("/team/task/claim", { team_id: message.teamId, task_id: message.taskId, agent_id: message.agentId }); break;
    case "completeTeamTask": title = "完成团队任务"; result = await post("/team/task/complete", { team_id: message.teamId, task_id: message.taskId, result: message.result, agent_id: message.agentId }); break;
    case "failTeamTask": title = "团队任务失败"; result = await post("/team/task/fail", { team_id: message.teamId, task_id: message.taskId, reason: message.reason, agent_id: message.agentId }); break;
    case "getTeamBridge": title = "跨团队连接"; result = await get(`/team/${id("teamA")}/bridge/${id("teamB")}`); break;
    case "registerTeamBridge": title = "设置跨团队连接"; result = await post("/team/bridge", { team_a: message.teamA, team_b: message.teamB, policy: message.policy }); break;
    case "sendCrossTeamMessage": title = "跨团队消息"; result = await post("/team/cross-message", { from_team: message.fromTeam, to_team: message.toTeam, text: message.text, from_agent: message.fromAgent ?? "", to_agent: message.toAgent ?? "" }); break;
    case "shutdownTeam": title = "关闭团队"; result = await post("/team/shutdown", { team_id: message.teamId }); break;
    default: return false;
  }
  context.card(title, renderResult(result));
  return true;
}

export async function followDesktopLog(api: GatewayApi, sessionId: string, name: string, signal: AbortSignal, onLines: (text: string) => void): Promise<void> {
  const response = await api.response(`/logs/follow?${new URLSearchParams({ session_id: sessionId, name })}`, { signal });
  if (!response.body) throw new Error("Gateway 未返回日志流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      const lines = frames.flatMap((frame) => frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => JSON.parse(line.slice(5)) as string));
      if (lines.length) onLines(lines.join("\n"));
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
