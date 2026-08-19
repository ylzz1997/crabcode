import {
  AlertTriangle,
  Archive,
  Bot,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  FileDiff,
  Folder,
  FolderOpen,
  History,
  ImagePlus,
  LoaderCircle,
  MessageSquarePlus,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  Server,
  Settings,
  ShieldAlert,
  Square,
  Trash2,
  WifiOff,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { applyGatewayEvent } from "./events";
import { GatewayApi, SessionChannel } from "./gateway";
import {
  deleteCredential,
  ensureLocalGateway,
  isDesktopShell,
  isInsecureRemoteUrl,
  isLoopbackUrl,
  loadSettings,
  normalizeBaseUrl,
  saveSettings,
  storeCredential,
} from "./native";
import type {
  ChatItem,
  ConnectionPreset,
  DesktopSettings,
  GatewayEvent,
  GatewayViewState,
  ProjectPreset,
  SessionInfo,
  SessionViewState,
  WorkspaceDirectoryListing,
} from "./types";

type GatewayMap = Record<string, GatewayViewState>;
type SessionMap = Record<string, SessionViewState>;

const EMPTY_GATEWAY: GatewayViewState = {
  status: "connecting",
  error: null,
  token: null,
  tokenExpiresAt: 0,
  workspace: null,
  sessionsByProject: {},
  models: [],
  runningCount: 0,
  pendingCount: 0,
};

function sessionKey(connectionId: string, sessionId: string): string {
  return `${connectionId}:${sessionId}`;
}

function basename(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).at(-1) || path;
}

function formatDate(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function textFromUnknown(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function readImage(file: File): Promise<{ name: string; media_type: string; data: string; dataUrl: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("无法读取图片"));
    reader.onload = () => {
      const dataUrl = String(reader.result ?? "");
      const separator = dataUrl.indexOf(",");
      if (separator < 0) {
        reject(new Error("图片格式无效"));
        return;
      }
      resolve({
        name: file.name,
        media_type: file.type || "image/png",
        data: dataUrl.slice(separator + 1),
        dataUrl,
      });
    };
    reader.readAsDataURL(file);
  });
}

function App() {
  const [settings, setSettings] = useState<DesktopSettings | null>(null);
  const [gateways, setGateways] = useState<GatewayMap>({});
  const [sessions, setSessions] = useState<SessionMap>({});
  const [activeSessions, setActiveSessions] = useState<Record<string, string | null>>({});
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [search, setSearch] = useState("");
  const [composer, setComposer] = useState("");
  const [pendingImages, setPendingImages] = useState<Array<{
    name: string;
    media_type: string;
    data: string;
    dataUrl: string;
  }>>([]);
  const [connectionModal, setConnectionModal] = useState(false);
  const [directoryModal, setDirectoryModal] = useState(false);
  const [settingsModal, setSettingsModal] = useState(false);
  const [checkpointModal, setCheckpointModal] = useState(false);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const apiRef = useRef(new Map<string, GatewayApi>());
  const channelRef = useRef(new Map<string, SessionChannel>());
  const connectedRef = useRef(new Set<string>());
  const messageEndRef = useRef<HTMLDivElement | null>(null);

  const activeConnection = useMemo(() => {
    if (!settings) return null;
    return settings.connections.find((item) => item.id === settings.active_connection_id)
      ?? settings.connections[0]
      ?? null;
  }, [settings]);
  const activeGateway = activeConnection ? gateways[activeConnection.id] : null;
  const activeProject = activeConnection?.projects.find(
    (item) => item.path === activeConnection.last_project_path,
  ) ?? activeConnection?.projects[0] ?? null;
  const activeSessionKey = activeConnection ? activeSessions[activeConnection.id] : null;
  const activeSession = activeSessionKey ? sessions[activeSessionKey] : null;
  const activeChannel = activeSessionKey ? channelRef.current.get(activeSessionKey) : null;

  const commitSettings = useCallback((update: (current: DesktopSettings) => DesktopSettings) => {
    setSettings((current) => {
      if (!current) return current;
      const next = update(current);
      void saveSettings(next).catch((error) => setGlobalError(String(error)));
      return next;
    });
  }, []);

  const updateConnection = useCallback((
    connectionId: string,
    update: (connection: ConnectionPreset) => ConnectionPreset,
  ) => {
    commitSettings((current) => ({
      ...current,
      connections: current.connections.map((connection) => (
        connection.id === connectionId ? update(connection) : connection
      )),
    }));
  }, [commitSettings]);

  const refreshProjectSessions = useCallback(async (connectionId: string, cwd: string) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    const list = await api.sessions(cwd);
    setGateways((current) => ({
      ...current,
      [connectionId]: {
        ...(current[connectionId] ?? EMPTY_GATEWAY),
        sessionsByProject: {
          ...(current[connectionId]?.sessionsByProject ?? {}),
          [cwd]: list,
        },
      },
    }));
  }, []);

  const updateSessionStatus = useCallback(async (connectionId: string, key: string, id: string) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    try {
      const status = await api.sessionStatus(id);
      setSessions((current) => current[key]
        ? { ...current, [key]: { ...current[key], status } }
        : current);
    } catch {
      // The history still works if the optional status request races session setup.
    }
  }, []);

  const openSession = useCallback((
    connection: ConnectionPreset,
    project: ProjectPreset,
    info?: SessionInfo,
  ) => {
    const api = apiRef.current.get(connection.id);
    if (!api) {
      setGlobalError("Gateway 尚未连接");
      return;
    }
    let key = sessionKey(connection.id, info?.session_id ?? `new-${crypto.randomUUID()}`);
    if (channelRef.current.has(key)) {
      setActiveSessions((current) => ({ ...current, [connection.id]: key }));
      return;
    }
    const initial: SessionViewState = {
      id: info?.session_id ?? key.split(":").slice(1).join(":"),
      cwd: project.path,
      title: info?.title || "新会话",
      items: [],
      busy: false,
      connected: false,
      operationId: null,
      status: null,
      error: null,
    };
    setSessions((current) => ({ ...current, [key]: initial }));
    setActiveSessions((current) => ({ ...current, [connection.id]: key }));

    const channel = new SessionChannel(api, {
      sessionId: info?.session_id,
      cwd: project.path,
      onEvent: (event: GatewayEvent) => {
        setSessions((current) => {
          const state = current[key];
          return state ? { ...current, [key]: applyGatewayEvent(state, event) } : current;
        });
        if (event.type === "server.connected") {
          const id = event.properties?.session_id;
          if (typeof id === "string") void updateSessionStatus(connection.id, key, id);
        }
      },
      onReady: (id) => {
        const nextKey = sessionKey(connection.id, id);
        if (nextKey !== key) {
          const previousKey = key;
          const existingChannel = channelRef.current.get(previousKey);
          channelRef.current.delete(previousKey);
          if (existingChannel) channelRef.current.set(nextKey, existingChannel);
          setSessions((current) => {
            const old = current[previousKey];
            if (!old) return current;
            const { [previousKey]: _, ...rest } = current;
            return { ...rest, [nextKey]: { ...old, id } };
          });
          setActiveSessions((current) => ({
            ...current,
            [connection.id]: current[connection.id] === previousKey ? nextKey : current[connection.id],
          }));
          key = nextKey;
        }
        updateConnection(connection.id, (current) => ({
          ...current,
          last_project_path: project.path,
          projects: current.projects.map((item) => item.path === project.path
            ? { ...item, last_session_id: id }
            : item),
        }));
        void refreshProjectSessions(connection.id, project.path);
        void updateSessionStatus(connection.id, key, id);
      },
      onState: (connected, error) => {
        setSessions((current) => current[key]
          ? { ...current, [key]: { ...current[key], connected, error: error ?? null } }
          : current);
      },
    });
    channelRef.current.set(key, channel);
    void channel.connect();
  }, [refreshProjectSessions, updateConnection, updateSessionStatus]);

  const connectGateway = useCallback(async (connection: ConnectionPreset, pythonPath: string | null) => {
    setGateways((current) => ({
      ...current,
      [connection.id]: { ...(current[connection.id] ?? EMPTY_GATEWAY), status: "connecting", error: null },
    }));
    try {
      if (isLoopbackUrl(connection.base_url)) {
        await ensureLocalGateway(
          connection.id,
          connection.base_url,
          pythonPath,
          connection.credential_ref,
        );
      }
      const api = new GatewayApi(connection);
      apiRef.current.set(connection.id, api);
      await api.authenticate();
      const [workspace, models] = await Promise.all([api.workspaceInfo(), api.models()]);
      const projects = connection.projects.length > 0
        ? connection.projects
        : [{ path: workspace.startup_cwd, name: basename(workspace.startup_cwd), last_session_id: null }];
      const sessionEntries = await Promise.all(
        projects.map(async (project) => [project.path, await api.sessions(project.path)] as const),
      );
      setGateways((current) => ({
        ...current,
        [connection.id]: {
          status: "online",
          error: null,
          token: api.accessToken,
          tokenExpiresAt: api.tokenExpiresAt,
          workspace,
          models,
          sessionsByProject: Object.fromEntries(sessionEntries),
          runningCount: 0,
          pendingCount: 0,
        },
      }));
      if (connection.projects.length === 0) {
        updateConnection(connection.id, (current) => ({
          ...current,
          projects,
          last_project_path: workspace.startup_cwd,
        }));
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const message = isLoopbackUrl(connection.base_url) && !isDesktopShell()
        ? `浏览器版不会自动启动本地 Gateway。请先运行 crabcode gateway。${detail}`
        : detail;
      setGateways((current) => ({
        ...current,
        [connection.id]: {
          ...(current[connection.id] ?? EMPTY_GATEWAY),
          status: "error",
          error: message,
        },
      }));
    }
  }, [updateConnection]);

  useEffect(() => {
    void loadSettings()
      .then(setSettings)
      .catch((error) => setGlobalError(String(error)));
    return () => {
      channelRef.current.forEach((channel) => channel.dispose());
    };
  }, []);

  useEffect(() => {
    if (!settings) return;
    for (const connection of settings.connections) {
      if (connectedRef.current.has(connection.id)) continue;
      connectedRef.current.add(connection.id);
      void connectGateway(connection, settings.python_path);
    }
  }, [connectGateway, settings]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ block: "end" });
  }, [activeSession?.items.length, activeSession?.items.at(-1)?.text]);

  const switchConnection = (connectionId: string) => {
    commitSettings((current) => ({ ...current, active_connection_id: connectionId }));
  };

  const switchProject = (project: ProjectPreset) => {
    if (!activeConnection) return;
    updateConnection(activeConnection.id, (connection) => ({
      ...connection,
      last_project_path: project.path,
    }));
    const lastId = project.last_session_id;
    if (lastId) {
      const info = activeGateway?.sessionsByProject[project.path]?.find((item) => item.session_id === lastId);
      if (info) openSession(activeConnection, project, info);
    }
  };

  const sendMessage = async () => {
    const text = composer.trim();
    if ((!text && pendingImages.length === 0) || !activeChannel || !activeSessionKey || !activeSession) return;
    try {
      if (activeSession.busy && activeSession.operationId) {
        activeChannel.steer(
          text,
          activeSession.operationId,
          pendingImages.map(({ media_type, data }) => ({ media_type, data })),
        );
        const attachmentLine = pendingImages.length > 0
          ? `${pendingImages.map((image) => `[图片：${image.name}]`).join(" ")}${text ? "\n\n" : ""}`
          : "";
        setSessions((current) => ({
          ...current,
          [activeSessionKey]: {
            ...current[activeSessionKey],
            items: [
              ...current[activeSessionKey].items,
              { id: crypto.randomUUID(), kind: "user", text: `引导：${attachmentLine}${text}`, status: "complete" },
            ],
          },
        }));
      } else {
        const operationId = activeChannel.sendMessage(
          text,
          pendingImages.map(({ media_type, data }) => ({ media_type, data })),
        );
        const attachmentLine = pendingImages.length > 0
          ? `${pendingImages.map((image) => `[图片：${image.name}]`).join(" ")}${text ? "\n\n" : ""}`
          : "";
        setSessions((current) => ({
          ...current,
          [activeSessionKey]: {
            ...current[activeSessionKey],
            busy: true,
            operationId,
            items: [
              ...current[activeSessionKey].items,
              { id: crypto.randomUUID(), kind: "user", text: `${attachmentLine}${text}`, status: "complete" },
            ],
          },
        }));
      }
      setComposer("");
      setPendingImages([]);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const resolvePermission = (item: ChatItem, allowed: boolean, always = false) => {
    if (!activeChannel || !item.tool_use_id) return;
    let feedback: string | undefined;
    if (!allowed) feedback = window.prompt("拒绝原因（可选）") ?? undefined;
    activeChannel.permission(item.tool_use_id, allowed, always, feedback);
  };

  const toggleChoice = (item: ChatItem, option: string) => {
    if (!activeSessionKey) return;
    setSessions((current) => {
      const session = current[activeSessionKey];
      if (!session) return current;
      return {
        ...current,
        [activeSessionKey]: {
          ...session,
          items: session.items.map((candidate) => {
            if (candidate.id !== item.id) return candidate;
            const selected = candidate.multiple
              ? candidate.selected?.includes(option)
                ? candidate.selected.filter((value) => value !== option)
                : [...(candidate.selected ?? []), option]
              : [option];
            return { ...candidate, selected };
          }),
        },
      };
    });
  };

  const submitChoice = (item: ChatItem) => {
    if (!activeChannel || !item.tool_use_id) return;
    activeChannel.choice(item.tool_use_id, item.selected ?? []);
  };

  const selectMode = (mode: "agent" | "plan") => {
    activeChannel?.switchMode(mode);
    if (activeSessionKey) {
      setSessions((current) => {
        const value = current[activeSessionKey];
        if (!value?.status) return current;
        return { ...current, [activeSessionKey]: { ...value, status: { ...value.status, mode } } };
      });
    }
  };

  const activeList = activeProject
    ? activeGateway?.sessionsByProject[activeProject.path] ?? []
    : [];
  const filteredSessions = activeList.filter((item) => {
    const needle = search.trim().toLowerCase();
    return !needle || item.title.toLowerCase().includes(needle) || item.preview.toLowerCase().includes(needle);
  });

  if (!settings) {
    return <div className="boot"><LoaderCircle className="spin" />正在加载 Crab Desktop</div>;
  }

  return (
    <div className="app-shell">
      <header className="gateway-tabs">
        <button
          className="icon-button sidebar-toggle"
          title={sidebarOpen ? "隐藏侧栏" : "显示侧栏"}
          onClick={() => setSidebarOpen((value) => !value)}
        >
          {sidebarOpen ? <PanelLeftClose /> : <PanelLeftOpen />}
        </button>
        <div className="tab-strip">
          {settings.connection_order.map((id) => {
            const connection = settings.connections.find((item) => item.id === id);
            if (!connection) return null;
            const gateway = gateways[id];
            const connectionSessions = Object.entries(sessions)
              .filter(([key]) => key.startsWith(`${id}:`))
              .map(([, value]) => value);
            const running = connectionSessions.filter((item) => item.busy).length;
            const pending = connectionSessions.flatMap((item) => item.items)
              .filter((item) => item.status === "pending" && (item.kind === "permission" || item.kind === "choice"))
              .length;
            return (
              <button
                key={id}
                className={`gateway-tab ${activeConnection?.id === id ? "active" : ""}`}
                onClick={() => switchConnection(id)}
                onDoubleClick={() => {
                  const name = window.prompt("连接名称", connection.name)?.trim();
                  if (name) updateConnection(id, (current) => ({ ...current, name }));
                }}
                title={`${connection.name}\n${connection.base_url}`}
              >
                <ConnectionDot status={gateway?.status ?? "connecting"} />
                <span>{connection.name}</span>
                {running > 0 && <span className="tab-count running">{running}</span>}
                {pending > 0 && <span className="tab-count pending">{pending}</span>}
              </button>
            );
          })}
        </div>
        <button className="icon-button" title="连接 Gateway" onClick={() => setConnectionModal(true)}>
          <Plus />
        </button>
        <button className="icon-button" title="Desktop 设置" onClick={() => setSettingsModal(true)}>
          <Settings />
        </button>
      </header>

      <div className="workbench">
        {sidebarOpen && (
          <aside className="sidebar" style={{ width: settings.sidebar_width }}>
            <div className="sidebar-heading">
              <div className="connection-name">
                <Server />
                <span>{activeConnection?.name ?? "Gateway"}</span>
              </div>
              <button
                className="icon-button small"
                title="重新连接"
                onClick={() => {
                  if (!activeConnection) return;
                  connectedRef.current.add(activeConnection.id);
                  void connectGateway(activeConnection, settings.python_path);
                }}
              >
                <RefreshCw />
              </button>
            </div>

            {activeGateway?.status === "error" && (
              <button className="connection-error" onClick={() => setConnectionModal(true)}>
                <WifiOff />
                <span>{activeGateway.error}</span>
              </button>
            )}

            <button
              className="primary-action"
              disabled={!activeConnection || !activeProject || activeGateway?.status !== "online"}
              onClick={() => activeConnection && activeProject && openSession(activeConnection, activeProject)}
            >
              <MessageSquarePlus />
              新会话
            </button>

            <section className="sidebar-section projects-section">
              <div className="section-label">
                <span>项目</span>
                <button
                  className="icon-button tiny"
                  title="添加项目"
                  disabled={activeGateway?.status !== "online"}
                  onClick={() => setDirectoryModal(true)}
                >
                  <Plus />
                </button>
              </div>
              <div className="project-list">
                {activeConnection?.projects.map((project) => (
                  <button
                    key={project.path}
                    className={`project-item ${activeProject?.path === project.path ? "active" : ""}`}
                    title={project.path}
                    onClick={() => switchProject(project)}
                    onDoubleClick={() => {
                      if (!activeConnection) return;
                      const name = window.prompt("项目名称", project.name)?.trim();
                      if (!name) return;
                      updateConnection(activeConnection.id, (connection) => ({
                        ...connection,
                        projects: connection.projects.map((item) => item.path === project.path
                          ? { ...item, name }
                          : item),
                      }));
                    }}
                  >
                    {activeProject?.path === project.path ? <FolderOpen /> : <Folder />}
                    <span>{project.name}</span>
                  </button>
                ))}
              </div>
            </section>

            <section className="sidebar-section sessions-section">
              <div className="section-label"><span>会话</span><span>{filteredSessions.length}</span></div>
              <label className="session-search">
                <Search />
                <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索会话" />
              </label>
              <div className="session-list">
                {filteredSessions.map((info) => {
                  const key = sessionKey(activeConnection!.id, info.session_id);
                  const view = sessions[key];
                  const isActive = activeSessionKey === key;
                  const pending = view?.items.some((item) => item.status === "pending" && (
                    item.kind === "permission" || item.kind === "choice"
                  ));
                  return (
                    <button
                      key={info.session_id}
                      className={`session-item ${isActive ? "active" : ""}`}
                      onClick={() => activeConnection && activeProject && openSession(activeConnection, activeProject, info)}
                    >
                      <span className="session-title-row">
                        <span className="session-title">{info.title || "未命名会话"}</span>
                        {view?.busy && <LoaderCircle className="spin" />}
                        {pending && <ShieldAlert className="pending-icon" />}
                      </span>
                      <span className="session-preview">{info.preview || formatDate(info.created_at)}</span>
                    </button>
                  );
                })}
                {activeGateway?.status === "online" && filteredSessions.length === 0 && (
                  <div className="empty-sidebar">暂无会话</div>
                )}
              </div>
            </section>
          </aside>
        )}

        <main className="main-panel">
          {!activeSession ? (
            <EmptyWorkspace
              connection={activeConnection}
              project={activeProject}
              gateway={activeGateway}
              onNew={() => activeConnection && activeProject && openSession(activeConnection, activeProject)}
              onConnect={() => setConnectionModal(true)}
            />
          ) : (
            <>
              <div className="conversation-header">
                <div className="conversation-title">
                  <h1>{activeSession.title}</h1>
                  <span>{activeSession.cwd}</span>
                </div>
                <div className="conversation-actions">
                  <button className="icon-button" title="检查点" onClick={() => setCheckpointModal(true)}>
                    <History />
                  </button>
                  <button
                    className="icon-button"
                    title="归档会话"
                    onClick={async () => {
                      if (!activeConnection || !activeProject || !activeSession.id) return;
                      if (!window.confirm("归档这个会话？")) return;
                      await apiRef.current.get(activeConnection.id)?.archive(activeSession.id);
                      channelRef.current.get(activeSessionKey!)?.dispose();
                      channelRef.current.delete(activeSessionKey!);
                      setSessions((current) => {
                        const { [activeSessionKey!]: _, ...rest } = current;
                        return rest;
                      });
                      setActiveSessions((current) => ({ ...current, [activeConnection.id]: null }));
                      await refreshProjectSessions(activeConnection.id, activeProject.path);
                    }}
                  >
                    <Archive />
                  </button>
                </div>
              </div>
              <div className="messages">
                {activeSession.items.length === 0 && (
                  <div className="conversation-empty">
                    <Bot />
                    <h2>在 {activeProject?.name ?? "项目"} 中构建什么？</h2>
                  </div>
                )}
                {activeSession.items.map((item) => (
                  <ChatItemView
                    key={item.id}
                    item={item}
                    onPermission={resolvePermission}
                    onToggleChoice={toggleChoice}
                    onSubmitChoice={submitChoice}
                    onPlan={(action) => activeChannel?.planAction(
                      action,
                      item.detail && typeof item.detail === "object" ? item.detail as Record<string, unknown> : undefined,
                    )}
                  />
                ))}
                <div ref={messageEndRef} />
              </div>
              <div className="composer-wrap">
                <div className="composer-context">
                  <span><Folder />{activeProject?.name}</span>
                  <span><Server />{activeConnection?.name}</span>
                  {!activeSession.connected && <span className="danger"><WifiOff />连接中断</span>}
                </div>
                <div className="composer-box">
                  {pendingImages.length > 0 && (
                    <div className="attachment-strip">
                      {pendingImages.map((image, index) => (
                        <div className="attachment-thumb" key={`${image.name}-${index}`}>
                          <img src={image.dataUrl} alt={image.name} />
                          <button
                            className="icon-button tiny"
                            title="移除图片"
                            onClick={() => setPendingImages((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                          ><X /></button>
                        </div>
                      ))}
                    </div>
                  )}
                  <textarea
                    value={composer}
                    onChange={(event) => setComposer(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        void sendMessage();
                      }
                    }}
                    placeholder={activeSession.busy ? "输入内容以引导当前任务" : "输入任务"}
                    rows={3}
                  />
                  <div className="composer-toolbar">
                    <div className="toolbar-left">
                      <label className="icon-button small" title="添加图片或文本文件">
                        <ImagePlus />
                        <input
                          type="file"
                          multiple
                          hidden
                          accept="image/*,.txt,.md,.json,.py,.ts,.tsx,.js,.jsx,.rs,.go,.java,.css,.html,.yaml,.yml,.toml"
                          onChange={async (event) => {
                            const files = Array.from(event.target.files ?? []);
                            const chunks: string[] = [];
                            const images: Array<{ name: string; media_type: string; data: string; dataUrl: string }> = [];
                            for (const file of files) {
                              if (file.type.startsWith("image/")) {
                                if (file.size > 20 * 1024 * 1024) {
                                  setGlobalError(`${file.name} 超过 20MB`);
                                  continue;
                                }
                                images.push(await readImage(file));
                              } else {
                                chunks.push(`\n\n<file name="${file.name}">\n${await file.text()}\n</file>`);
                              }
                            }
                            if (images.length > 0) setPendingImages((current) => [...current, ...images]);
                            setComposer((value) => value + chunks.join(""));
                            event.target.value = "";
                          }}
                        />
                      </label>
                      <select
                        aria-label="模型"
                        value={activeSession.status?.model_profile ?? ""}
                        onChange={(event) => activeChannel?.switchModel(event.target.value)}
                      >
                        <option value="">{activeSession.status?.model || "默认模型"}</option>
                        {activeGateway?.models.map((model) => (
                          <option key={model.name} value={model.name}>{model.name}</option>
                        ))}
                      </select>
                      <div className="segmented-control" aria-label="运行模式">
                        <button
                          className={activeSession.status?.mode !== "plan" ? "active" : ""}
                          onClick={() => selectMode("agent")}
                        >Agent</button>
                        <button
                          className={activeSession.status?.mode === "plan" ? "active" : ""}
                          onClick={() => selectMode("plan")}
                        >Plan</button>
                      </div>
                      <select
                        aria-label="权限模式"
                        value={activeSession.status?.permission_mode ?? "default"}
                        onChange={(event) => activeChannel?.setPermissionMode(event.target.value)}
                      >
                        <option value="default">跟随设置</option>
                        <option value="ask">每次询问</option>
                        <option value="ai_review">AI 审查</option>
                        <option value="run_everything">完全访问</option>
                      </select>
                    </div>
                    <div className="toolbar-right">
                      {activeSession.status?.context_window_tokens ? (
                        <span className="context-usage" title="上下文用量">
                          {Math.round(activeSession.status.context_used_percent)}%
                        </span>
                      ) : null}
                      {activeSession.busy ? (
                        <button
                          className="round-action stop"
                          title="中断任务"
                          onClick={() => activeChannel?.interrupt(activeSession.operationId)}
                        >
                          <Square />
                        </button>
                      ) : (
                        <button
                          className="round-action send"
                          title="发送"
                          disabled={(!composer.trim() && pendingImages.length === 0) || !activeSession.connected}
                          onClick={() => void sendMessage()}
                        >
                          <Send />
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}
        </main>
      </div>

      {connectionModal && (
        <ConnectionModal
          settings={settings}
          activeConnectionId={activeConnection?.id ?? ""}
          onClose={() => setConnectionModal(false)}
          onActivate={(id) => {
            switchConnection(id);
            setConnectionModal(false);
          }}
          onSave={async (connection, password) => {
            if (isInsecureRemoteUrl(connection.base_url) && !connection.allow_insecure_remote) {
              throw new Error("远程 HTTP 会明文传输密码，请确认不安全连接后再保存");
            }
            connection.base_url = normalizeBaseUrl(connection.base_url).replace(/\/$/, "");
            if (password) {
              connection.credential_ref = connection.credential_ref ?? `gateway-${connection.id}`;
              await storeCredential(connection.credential_ref, password);
            }
            commitSettings((current) => {
              const exists = current.connections.some((item) => item.id === connection.id);
              return {
                ...current,
                active_connection_id: connection.id,
                connections: exists
                  ? current.connections.map((item) => item.id === connection.id ? connection : item)
                  : [...current.connections, connection],
                connection_order: exists
                  ? current.connection_order
                  : [...current.connection_order, connection.id],
              };
            });
            connectedRef.current.add(connection.id);
            await connectGateway(connection, settings.python_path);
            setConnectionModal(false);
          }}
        />
      )}

      {directoryModal && activeConnection && activeGateway?.workspace && (
        <DirectoryModal
          api={apiRef.current.get(activeConnection.id)!}
          home={activeGateway.workspace.home}
          roots={activeGateway.workspace.browse_roots}
          onClose={() => setDirectoryModal(false)}
          onSelect={(path) => {
            const project: ProjectPreset = { path, name: basename(path), last_session_id: null };
            updateConnection(activeConnection.id, (connection) => ({
              ...connection,
              projects: connection.projects.some((item) => item.path === path)
                ? connection.projects
                : [...connection.projects, project],
              last_project_path: path,
            }));
            void refreshProjectSessions(activeConnection.id, path);
            setDirectoryModal(false);
          }}
        />
      )}

      {settingsModal && (
        <SettingsModal
          settings={settings}
          onClose={() => setSettingsModal(false)}
          onSave={(pythonPath) => {
            commitSettings((current) => ({ ...current, python_path: pythonPath || null }));
            setSettingsModal(false);
          }}
          onDelete={async (id) => {
            const connection = settings.connections.find((item) => item.id === id);
            if (!connection || id === "local") return;
            channelRef.current.forEach((channel, key) => {
              if (key.startsWith(`${id}:`)) channel.dispose();
            });
            if (connection.credential_ref) await deleteCredential(connection.credential_ref);
            commitSettings((current) => ({
              ...current,
              active_connection_id: current.active_connection_id === id ? "local" : current.active_connection_id,
              connections: current.connections.filter((item) => item.id !== id),
              connection_order: current.connection_order.filter((value) => value !== id),
            }));
          }}
        />
      )}

      {checkpointModal && activeConnection && activeSession && (
        <CheckpointModal
          api={apiRef.current.get(activeConnection.id)!}
          sessionId={activeSession.id}
          onClose={() => setCheckpointModal(false)}
          onRestored={() => {
            setCheckpointModal(false);
            channelRef.current.get(activeSessionKey!)?.dispose();
            channelRef.current.delete(activeSessionKey!);
            setSessions((current) => ({
              ...current,
              [activeSessionKey!]: { ...current[activeSessionKey!], items: [], connected: false },
            }));
            const info = activeList.find((item) => item.session_id === activeSession.id);
            if (activeProject && info) openSession(activeConnection, activeProject, info);
          }}
        />
      )}

      {globalError && (
        <div className="toast error-toast" role="alert">
          <AlertTriangle />
          <span>{globalError}</span>
          <button className="icon-button tiny" onClick={() => setGlobalError(null)}><X /></button>
        </div>
      )}
    </div>
  );
}

function ConnectionDot({ status }: { status: GatewayViewState["status"] }) {
  return status === "connecting"
    ? <LoaderCircle className="connection-dot spin" />
    : <Circle className={`connection-dot ${status}`} fill="currentColor" />;
}

function EmptyWorkspace({
  connection,
  project,
  gateway,
  onNew,
  onConnect,
}: {
  connection: ConnectionPreset | null;
  project: ProjectPreset | null;
  gateway: GatewayViewState | null | undefined;
  onNew: () => void;
  onConnect: () => void;
}) {
  if (!connection || gateway?.status === "error") {
    return (
      <div className="empty-workspace">
        <WifiOff />
        <h1>{connection ? "无法连接 Gateway" : "连接 CrabCode Gateway"}</h1>
        <p>{gateway?.error ?? "添加本地或远程 Gateway 连接"}</p>
        <button className="command-button" onClick={onConnect}><Server />管理连接</button>
      </div>
    );
  }
  if (gateway?.status !== "online") {
    return <div className="empty-workspace"><LoaderCircle className="spin" /><h1>正在连接 {connection.name}</h1></div>;
  }
  return (
    <div className="empty-workspace">
      <Bot />
      <h1>{project ? `在 ${project.name} 中开始` : "选择一个项目"}</h1>
      <p>{project?.path}</p>
      {project && <button className="command-button" onClick={onNew}><MessageSquarePlus />新会话</button>}
    </div>
  );
}

function ChatItemView({ item, onPermission, onToggleChoice, onSubmitChoice, onPlan }: {
  item: ChatItem;
  onPermission: (item: ChatItem, allowed: boolean, always?: boolean) => void;
  onToggleChoice: (item: ChatItem, option: string) => void;
  onSubmitChoice: (item: ChatItem) => void;
  onPlan: (action: "execute" | "revise" | "cancel") => void;
}) {
  const [collapsed, setCollapsed] = useState(item.collapsed ?? false);
  if (item.kind === "user") {
    return <article className="message user-message"><ReactMarkdown remarkPlugins={[remarkGfm]}>{item.text ?? ""}</ReactMarkdown></article>;
  }
  if (item.kind === "assistant") {
    return <article className="message assistant-message"><ReactMarkdown remarkPlugins={[remarkGfm]}>{item.text ?? ""}</ReactMarkdown></article>;
  }
  if (item.kind === "system") return <div className="system-line">{item.text}</div>;
  if (item.kind === "error") return <div className="error-line"><AlertTriangle />{item.text}</div>;
  if (item.kind === "thinking") {
    return (
      <article className="activity-card thinking-card">
        <button className="activity-header" onClick={() => setCollapsed((value) => !value)}>
          {item.status === "running" ? <LoaderCircle className="spin" /> : <Bot />}
          <span>{item.title}</span>
          {collapsed ? <ChevronRight /> : <ChevronDown />}
        </button>
        {!collapsed && <div className="activity-body prose-muted">{item.text}</div>}
      </article>
    );
  }
  if (item.kind === "tool") {
    return (
      <article className="activity-card tool-card">
        <button className="activity-header" onClick={() => setCollapsed((value) => !value)}>
          {item.status === "running" ? <LoaderCircle className="spin" /> : <Circle fill="currentColor" />}
          <span>{item.title}</span>
          <code>{typeof item.detail === "object" && item.detail && "path" in item.detail ? String((item.detail as Record<string, unknown>).path) : ""}</code>
          {collapsed ? <ChevronRight /> : <ChevronDown />}
        </button>
        {!collapsed && <pre className="activity-body">{textFromUnknown(item.detail)}</pre>}
      </article>
    );
  }
  if (item.kind === "file_change") {
    return (
      <article className="activity-card diff-card">
        <button className="activity-header" onClick={() => setCollapsed((value) => !value)}>
          <FileDiff />
          <span>{item.action} {item.title}</span>
          {collapsed ? <ChevronRight /> : <ChevronDown />}
        </button>
        {!collapsed && <pre className="activity-body diff-view">{item.diff || "此事件没有附带 diff"}</pre>}
      </article>
    );
  }
  if (item.kind === "permission") {
    return (
      <article className="request-card">
        <div className="request-title"><ShieldAlert /><strong>{item.title}</strong></div>
        {item.text && <p>{item.text}</p>}
        <pre>{textFromUnknown(item.detail)}</pre>
        {item.status === "pending" ? (
          <div className="request-actions">
            <button onClick={() => onPermission(item, false)}>拒绝</button>
            <button onClick={() => onPermission(item, true, true)}>始终允许</button>
            <button className="primary" onClick={() => onPermission(item, true)}>允许</button>
          </div>
        ) : <span className={`request-result ${item.status}`}>{item.status === "allowed" ? "已允许" : "已拒绝"}</span>}
      </article>
    );
  }
  if (item.kind === "choice") {
    return (
      <article className="request-card">
        <div className="request-title"><MoreHorizontal /><strong>{item.title}</strong></div>
        <div className="choice-list">
          {item.options?.map((option) => (
            <label key={option}>
              <input
                type={item.multiple ? "checkbox" : "radio"}
                checked={item.selected?.includes(option) ?? false}
                disabled={item.status !== "pending"}
                onChange={() => onToggleChoice(item, option)}
              />
              <span>{option}</span>
            </label>
          ))}
        </div>
        {item.status === "pending" && (
          <div className="request-actions">
            <button className="primary" disabled={!item.selected?.length} onClick={() => onSubmitChoice(item)}>确认</button>
          </div>
        )}
      </article>
    );
  }
  if (item.kind === "plan") {
    return (
      <article className="request-card plan-card">
        <div className="request-title"><Bot /><strong>{item.title}</strong></div>
        <pre>{textFromUnknown(item.detail)}</pre>
        <div className="request-actions">
          <button onClick={() => onPlan("cancel")}>取消</button>
          <button onClick={() => onPlan("revise")}>修改</button>
          <button className="primary" onClick={() => onPlan("execute")}>执行计划</button>
        </div>
      </article>
    );
  }
  return null;
}

function ConnectionModal({ settings, activeConnectionId, onClose, onActivate, onSave }: {
  settings: DesktopSettings;
  activeConnectionId: string;
  onClose: () => void;
  onActivate: (id: string) => void;
  onSave: (connection: ConnectionPreset, password: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://");
  const [password, setPassword] = useState("");
  const [allowInsecure, setAllowInsecure] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editingConnection = settings.connections.find((connection) => connection.id === editingId) ?? null;
  const editConnection = (connection: ConnectionPreset) => {
    setEditingId(connection.id);
    setName(connection.name);
    setBaseUrl(connection.base_url);
    setPassword("");
    setAllowInsecure(connection.allow_insecure_remote);
    setError(null);
  };
  const createConnection = () => {
    setEditingId(null);
    setName("");
    setBaseUrl("https://");
    setPassword("");
    setAllowInsecure(false);
    setError(null);
  };
  return (
    <Modal title="Gateway 连接" onClose={onClose}>
      <div className="saved-connections">
        {settings.connections.map((connection) => (
          <div className="saved-connection-row" key={connection.id}>
            <button
              className={connection.id === activeConnectionId ? "active" : ""}
              onClick={() => onActivate(connection.id)}
            >
              <Server />
              <span><strong>{connection.name}</strong><small>{connection.base_url}</small></span>
              <ChevronRight />
            </button>
            <button className="icon-button" title="编辑连接" onClick={() => editConnection(connection)}>
              <Settings />
            </button>
          </div>
        ))}
      </div>
      <div className="modal-divider">
        <span>{editingConnection ? `编辑 ${editingConnection.name}` : "新建远程连接"}</span>
      </div>
      <form
        className="form-grid"
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          let fallbackName: string;
          try {
            fallbackName = new URL(baseUrl).host;
          } catch {
            setError("Gateway 地址无效");
            setBusy(false);
            return;
          }
          const id = editingConnection?.id ?? crypto.randomUUID();
          void onSave({
            id,
            name: name.trim() || fallbackName,
            base_url: baseUrl,
            credential_ref: password
              ? editingConnection?.credential_ref ?? `gateway-${id}`
              : editingConnection?.credential_ref ?? null,
            allow_insecure_remote: allowInsecure,
            projects: editingConnection?.projects ?? [],
            last_project_path: editingConnection?.last_project_path ?? null,
          }, password).catch((reason) => {
            setError(reason instanceof Error ? reason.message : String(reason));
            setBusy(false);
          });
        }}
      >
        <label><span>名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="构建服务器" /></label>
        <label><span>Gateway 地址</span><input required value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://host:4096" /></label>
        <label>
          <span>{editingConnection?.credential_ref ? "密码（留空则保留；浏览器重开后需重新输入）" : "密码"}</span>
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" />
        </label>
        {isInsecureRemoteUrl(baseUrl) && (
          <label className="warning-check">
            <input type="checkbox" checked={allowInsecure} onChange={(event) => setAllowInsecure(event.target.checked)} />
            <ShieldAlert /><span>允许通过未加密连接传输凭据</span>
          </label>
        )}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
        <div className="modal-actions">
          {editingConnection && <button type="button" onClick={createConnection}>新建连接</button>}
          <button type="button" onClick={onClose}>取消</button>
          <button className="primary" disabled={busy}>{busy ? <LoaderCircle className="spin" /> : editingConnection ? <Settings /> : <Plus />}保存并连接</button>
        </div>
      </form>
    </Modal>
  );
}

function DirectoryModal({ api, home, roots, onClose, onSelect }: {
  api: GatewayApi;
  home: string;
  roots: string[];
  onClose: () => void;
  onSelect: (path: string) => void;
}) {
  const [path, setPath] = useState(home);
  const [listing, setListing] = useState<WorkspaceDirectoryListing | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setListing(null);
    setError(null);
    void api.directories(path, showHidden)
      .then(setListing)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [api, path, showHidden]);
  return (
    <Modal title="选择项目目录" onClose={onClose} wide>
      <div className="directory-roots">
        {roots.map((root) => <button key={root} onClick={() => setPath(root)}><Folder />{root}</button>)}
      </div>
      <div className="directory-toolbar">
        <button className="icon-button" title="上一级" disabled={!listing?.parent} onClick={() => listing?.parent && setPath(listing.parent)}><ChevronLeft /></button>
        <input value={path} onChange={(event) => setPath(event.target.value)} onKeyDown={(event) => event.key === "Enter" && setPath(event.currentTarget.value)} />
        <label><input type="checkbox" checked={showHidden} onChange={(event) => setShowHidden(event.target.checked)} />显示隐藏目录</label>
      </div>
      <div className="directory-list">
        {!listing && !error && <div className="directory-loading"><LoaderCircle className="spin" /></div>}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
        {listing?.directories.map((directory) => (
          <button key={directory.path} onDoubleClick={() => setPath(directory.path)} onClick={() => setPath(directory.path)}>
            <Folder />
            <span>{directory.name}</span>
            {directory.is_symlink && <small>链接</small>}
            <ChevronRight />
          </button>
        ))}
      </div>
      <div className="modal-actions">
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={!listing} onClick={() => listing && onSelect(listing.path)}><FolderOpen />使用此目录</button>
      </div>
    </Modal>
  );
}

function SettingsModal({ settings, onClose, onSave, onDelete }: {
  settings: DesktopSettings;
  onClose: () => void;
  onSave: (pythonPath: string) => void;
  onDelete: (id: string) => Promise<void>;
}) {
  const [pythonPath, setPythonPath] = useState(settings.python_path ?? "");
  return (
    <Modal title="Desktop 设置" onClose={onClose}>
      <div className="form-grid">
        {isDesktopShell() ? (
          <label><span>Python 路径</span><input value={pythonPath} onChange={(event) => setPythonPath(event.target.value)} placeholder="自动检测 python3 / python" /></label>
        ) : (
          <div className="runtime-note">浏览器版连接已经运行的 Gateway；本地自动安装、启动和系统凭据库仅在 Tauri 桌面版可用。</div>
        )}
      </div>
      <div className="settings-connections">
        <h3>已保存连接</h3>
        {settings.connections.map((connection) => (
          <div key={connection.id}>
            <Server /><span><strong>{connection.name}</strong><small>{connection.base_url}</small></span>
            {connection.id !== "local" && (
              <button className="icon-button" title="删除连接" onClick={() => void onDelete(connection.id)}><Trash2 /></button>
            )}
          </div>
        ))}
      </div>
      <div className="modal-actions">
        <button onClick={onClose}>取消</button>
        <button className="primary" onClick={() => onSave(pythonPath)}><Settings />保存</button>
      </div>
    </Modal>
  );
}

function CheckpointModal({ api, sessionId, onClose, onRestored }: {
  api: GatewayApi;
  sessionId: string;
  onClose: () => void;
  onRestored: () => void;
}) {
  const [items, setItems] = useState<Array<{ id: string; label?: string; timestamp?: string; files?: string[] }> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void api.checkpoints(sessionId).then(setItems).catch((reason) => setError(String(reason)));
  }, [api, sessionId]);
  const restore = async (id?: string) => {
    if (!window.confirm(id ? "恢复到这个检查点？文件和会话都将回退。" : "撤销最近一次文件和会话改动？")) return;
    setBusy(true);
    try {
      if (id) await api.revert(sessionId, id);
      else await api.undo(sessionId);
      onRestored();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  };
  return (
    <Modal title="检查点与恢复" onClose={onClose}>
      <button className="undo-latest" disabled={busy} onClick={() => void restore()}><RotateCcw />撤销最近检查点</button>
      <div className="checkpoint-list">
        {!items && !error && <LoaderCircle className="spin" />}
        {items?.map((item) => (
          <button key={item.id} disabled={busy} onClick={() => void restore(item.id)}>
            <History />
            <span><strong>{item.label || item.id.slice(0, 8)}</strong><small>{item.timestamp || item.files?.join(", ")}</small></span>
            <RotateCcw />
          </button>
        ))}
        {items?.length === 0 && <div className="empty-sidebar">暂无检查点</div>}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
      </div>
    </Modal>
  );
}

function Modal({ title, onClose, wide = false, children }: {
  title: string;
  onClose: () => void;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header><h2>{title}</h2><button className="icon-button" title="关闭" onClick={onClose}><X /></button></header>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

export default App;
