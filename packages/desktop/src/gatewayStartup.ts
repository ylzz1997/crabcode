import { t as tr } from "./i18n";
import type { WorkspaceInfo } from "./types";

export function gatewayEnvironmentLog(workspace: WorkspaceInfo): string[] {
  const runtime = workspace.runtime;
  return [
    ...(runtime ? [
      tr("CrabCode Gateway 版本：{0}", { 0: runtime.gateway_version }),
      tr("Gateway 包路径：{0}", { 0: runtime.gateway_path }),
      tr("Gateway 使用的 Python：{0}", { 0: runtime.python_version }),
      tr("Python 解释器路径：{0}", { 0: runtime.python_executable }),
      tr("Python 环境：{0} · {1}", { 0: ({ venv: tr("虚拟环境"), conda: tr("Conda 环境"), system: tr("基础环境") })[runtime.environment_kind], 1: runtime.python_prefix }),
      tr("Gateway 运行平台：{0}", { 0: runtime.platform }),
    ] : [tr("此 Gateway 未提供详细运行环境信息（可能为旧版本服务）")]),
    tr("Gateway 启动目录：{0}", { 0: workspace.startup_cwd }),
  ];
}

export function gatewayLogAddress(baseUrl: string): string {
  const url = new URL(baseUrl);
  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  return url.toString();
}

export interface GatewayStartupProgress {
  connectionId: string;
  operationId: string;
  stage: string;
  detail: string;
}

export interface GatewayStartupState {
  stage: string;
  detail: string;
  startedAt: number;
  finishedAt: number | null;
  history: Array<{ time: number; detail: string }>;
}

export function updateGatewayStartup(
  current: GatewayStartupState | undefined,
  stage: string,
  detail: string,
  now = Date.now(),
): GatewayStartupState {
  const history = current?.history ?? [];
  return {
    ...current,
    stage,
    detail,
    startedAt: current?.startedAt ?? now,
    finishedAt: stage === "online" || stage === "error" ? now : null,
    history: history.at(-1)?.detail === detail
      ? history
      : [...history, { time: now, detail }].slice(-100),
  };
}
