function vmEnvironmentId(vm) { return `lume:${vm.storage}:${vm.name}`; }
export const COMPUTER_USE_RELEASE_RETENTION_MS = 15_000;
export function computerUseReauthorizationHint(capabilities, kind) {
    if (capabilities?.platform !== "macos" || capabilities.environment === "local_vm"
        || !capabilities.reauthorization_required?.includes(kind)
        || (kind === "screen_recording" ? capabilities.capture_available : capabilities.ax_available) !== false)
        return "";
    return "检测到旧版 Crab Computer Use 已被替换。若权限列表中已有同名条目，请先移除旧条目，再将当前应用卡片拖入列表并开启开关，完成后重新检测。";
}
export function initialComputerUseState(hostId, enabled) {
    return {
        hostId,
        enabled,
        active: false,
        mode: "background_app",
        deliveryPolicy: "allow_foreground",
        status: enabled ? "connecting" : "disabled",
        capabilities: null,
        previews: [],
        logs: [],
        error: null,
    };
}
export class ComputerUseChannel {
    api;
    hostId;
    onState;
    platform;
    vm;
    socket = null;
    reconnectTimer = null;
    heartbeatTimer = null;
    attempts = 0;
    capabilityGeneration = 0;
    releaseTimers = new Map();
    releaseDeadlines = new Map();
    latestRequestIds = new Map();
    disposed = false;
    enabled;
    capabilities = null;
    axConnectionId;
    state;
    constructor(api, hostId, enabled, onState, platform, vm = null) {
        this.api = api;
        this.hostId = hostId;
        this.onState = onState;
        this.platform = platform;
        this.vm = vm;
        this.axConnectionId = platform.randomUuid();
        this.enabled = enabled;
        this.state = initialComputerUseState(hostId, enabled);
    }
    publish(update) {
        this.state = { ...this.state, ...update, hostId: this.hostId, enabled: this.enabled };
        this.onState(this.state);
    }
    async connect() {
        if (this.disposed)
            return;
        this.publish({ status: this.enabled ? "connecting" : "disabled", error: null });
        if (this.enabled)
            await this.refreshCapabilities();
        if (this.disposed)
            return;
        try {
            await this.api.authenticate();
            if (this.disposed)
                return;
            const socket = this.platform.createSocket(this.api.computerUseWebSocketUrl());
            this.socket = socket;
            socket.addEventListener("open", () => {
                if (this.disposed || this.socket !== socket)
                    return socket.close();
                this.attempts = 0;
                this.sendRegistration();
            });
            socket.addEventListener("message", (event) => {
                if (this.disposed || this.socket !== socket)
                    return;
                void this.handleMessage(String(event.data));
            });
            socket.addEventListener("close", () => {
                if (this.socket !== socket)
                    return;
                void this.releaseAx();
                this.axConnectionId = this.platform.randomUuid();
                this.socket = null;
                if (!this.disposed) {
                    this.publish({ status: this.enabled ? "connecting" : "disabled" });
                    this.scheduleReconnect();
                }
            });
            socket.addEventListener("error", () => {
                if (this.socket === socket)
                    this.publish({ status: "error", error: "Computer Use Host connection failed" });
            });
        }
        catch (error) {
            this.publish({ status: "error", error: error instanceof Error ? error.message : String(error) });
            this.scheduleReconnect();
        }
    }
    async refreshCapabilities(background = false) {
        const generation = ++this.capabilityGeneration;
        let capabilities;
        if (!this.platform.isNative()) {
            capabilities = {
                gui_available: false,
                input_available: false,
                platform: "browser",
                displays: [],
                supported_modes: [],
                reason: "Computer Use requires the native Computer Use component",
            };
        }
        else {
            try {
                capabilities = this.vm
                    ? await this.platform.invoke("computer_use_vm_capabilities", { config: this.vm })
                    : await this.platform.invoke("computer_use_capabilities");
            }
            catch (error) {
                capabilities = {
                    gui_available: false,
                    input_available: false,
                    platform: this.platform.platform || "unknown",
                    displays: [],
                    supported_modes: [],
                    reason: error instanceof Error ? error.message : String(error),
                };
            }
        }
        if (this.disposed || !this.enabled || generation !== this.capabilityGeneration)
            return false;
        if (background && JSON.stringify(capabilities) === JSON.stringify(this.capabilities))
            return false;
        if (!this.vm && capabilities.instance_id && this.capabilities?.instance_id && capabilities.instance_id !== this.capabilities.instance_id) {
            this.axConnectionId = this.platform.randomUuid();
            this.cancelAllReleases();
            this.latestRequestIds.clear();
            this.publish({ active: false, previews: [] });
        }
        if (this.vm) {
            capabilities = { ...capabilities, environment: "local_vm", environment_id: vmEnvironmentId(this.vm), environment_name: this.vm.name };
            if (this.capabilities?.instance_id && this.capabilities.instance_id !== capabilities.instance_id) {
                this.cancelAllReleases();
                this.latestRequestIds.clear();
                this.publish({ active: false, previews: [] });
            }
        }
        this.capabilities = capabilities;
        this.scheduleHeartbeat();
        this.publish({
            capabilities,
            status: background && this.state.previews.some(preview => preview.status === "busy") ? "busy"
                : capabilities.gui_available ? (this.socket?.readyState === 1 ? "ready" : "connecting") : "unavailable",
            error: capabilities.reason ?? null,
        });
        return true;
    }
    setEnabled(enabled) {
        const changed = this.enabled !== enabled;
        if (!enabled && changed)
            void this.releaseAx();
        if (!enabled && changed && this.vm) {
            for (const preview of this.state.previews)
                void this.releaseVm(preview.sessionId, preview.agentId);
        }
        this.enabled = enabled;
        if (enabled && changed) {
            this.publish({ status: "connecting", error: null });
            void this.refreshCapabilities().then((applied) => {
                if (!applied || !this.enabled || this.disposed)
                    return;
                this.send({
                    type: "computer_use_host_state",
                    enabled: true,
                    gui_available: this.capabilities?.gui_available === true,
                    capabilities: this.capabilities,
                });
            });
            return;
        }
        this.publish({
            status: !enabled ? "disabled" : this.capabilities?.gui_available ? (this.socket?.readyState === 1 ? "ready" : "connecting") : "unavailable",
            ...(!enabled ? { active: false, previews: [] } : {}),
            error: enabled ? this.capabilities?.reason ?? null : null,
        });
        if (!enabled) {
            this.cancelAllReleases();
            if (this.heartbeatTimer !== null)
                this.platform.clearTimeout(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
        this.send({
            type: "computer_use_host_state",
            enabled,
            gui_available: this.capabilities?.gui_available === true,
            capabilities: this.capabilities,
        });
    }
    refresh() {
        if (!this.enabled || this.disposed)
            return;
        this.publish({ status: "connecting", error: null });
        void this.refreshCapabilities().then((applied) => {
            if (!applied || !this.enabled || this.disposed)
                return;
            this.send({
                type: "computer_use_host_state",
                enabled: true,
                gui_available: this.capabilities?.gui_available === true,
                capabilities: this.capabilities,
            });
        });
    }
    dispose() {
        void this.releaseAx();
        if (this.vm) {
            for (const preview of this.state.previews)
                void this.releaseVm(preview.sessionId, preview.agentId);
        }
        this.disposed = true;
        if (this.heartbeatTimer !== null)
            this.platform.clearTimeout(this.heartbeatTimer);
        this.heartbeatTimer = null;
        if (this.reconnectTimer !== null)
            this.platform.clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
        this.cancelAllReleases();
        this.latestRequestIds.clear();
        this.socket?.close();
        this.socket = null;
    }
    scheduleHeartbeat() {
        if (this.disposed || !this.enabled || this.vm || !this.platform.isNative() || this.heartbeatTimer !== null)
            return;
        this.heartbeatTimer = this.platform.setTimeout(() => {
            this.heartbeatTimer = null;
            if (this.disposed || !this.enabled)
                return;
            void (async () => {
                if (this.capabilities?.instance_id) {
                    await this.platform.invoke("computer_use_heartbeat", { hostId: this.hostId, connectionId: this.axConnectionId,
                        instanceId: this.capabilities.instance_id }).catch(() => { });
                }
                if (this.disposed || !this.enabled)
                    return;
                const applied = await this.refreshCapabilities(true);
                if (applied && this.enabled && !this.disposed) {
                    this.send({ type: "computer_use_host_state", enabled: true,
                        gui_available: this.capabilities?.gui_available === true, capabilities: this.capabilities });
                }
            })().finally(() => this.scheduleHeartbeat());
        }, 30_000);
    }
    async releaseVm(sessionId, agentId, allAgents = false) {
        if (!this.vm || !sessionId || !this.capabilities?.instance_id)
            return;
        try {
            await this.platform.invoke("computer_use_vm_release", { config: this.vm, instanceId: this.capabilities.instance_id,
                owner: `${this.hostId}:${sessionId}${allAgents ? "" : `:${agentId ?? "main"}`}`, allAgents });
        }
        catch { /* A stopped VM has already lost its lease. Never retry input. */ }
    }
    async releaseAx(sessionId, agentId, allAgents = true) {
        if (this.vm || (this.capabilities?.ax_protocol_version !== 1 && !this.capabilities?.instance_id))
            return;
        try {
            await this.platform.invoke("computer_use_release_ax", { hostId: this.hostId, connectionId: this.axConnectionId,
                sessionId: sessionId ?? null, agentId: agentId ?? null, allAgents });
        }
        catch { /* A stopped host has already discarded native references. */ }
    }
    sendRegistration() {
        this.send({
            type: "computer_use_host_register",
            host_id: this.hostId,
            enabled: this.enabled,
            gui_available: this.capabilities?.gui_available === true,
            capabilities: this.capabilities,
        });
    }
    send(payload) {
        if (this.socket?.readyState === 1)
            this.socket.send(JSON.stringify(payload));
    }
    scheduleReconnect() {
        if (this.disposed || this.reconnectTimer !== null)
            return;
        const delay = Math.min(30_000, 1000 * 2 ** Math.min(this.attempts++, 5));
        this.reconnectTimer = this.platform.setTimeout(() => {
            this.reconnectTimer = null;
            void this.connect();
        }, delay);
    }
    previewKey(sessionId, agentId) {
        return `session:${sessionId || "unknown"}:agent:${agentId || "main"}`;
    }
    cancelRelease(key) {
        const timer = this.releaseTimers.get(key);
        if (timer !== undefined)
            this.platform.clearTimeout(timer);
        this.releaseTimers.delete(key);
        this.releaseDeadlines.delete(key);
    }
    cancelAllReleases() {
        for (const timer of this.releaseTimers.values())
            this.platform.clearTimeout(timer);
        this.releaseTimers.clear();
        this.releaseDeadlines.clear();
    }
    scheduleRelease(key, deadline = Date.now() + COMPUTER_USE_RELEASE_RETENTION_MS) {
        const previousTimer = this.releaseTimers.get(key);
        if (previousTimer !== undefined)
            this.platform.clearTimeout(previousTimer);
        this.releaseDeadlines.set(key, deadline);
        const timer = this.platform.setTimeout(() => {
            this.releaseTimers.delete(key);
            if (this.disposed)
                return;
            this.releaseDeadlines.delete(key);
            const previews = this.state.previews.filter((preview) => preview.key !== key);
            const status = previews.some((preview) => preview.status === "busy")
                ? "busy"
                : previews.some((preview) => preview.status === "error")
                    ? "error"
                    : this.enabled && this.capabilities?.gui_available
                        ? "ready"
                        : this.state.status;
            this.publish({ active: previews.length > 0, previews, status });
        }, Math.max(0, deadline - Date.now()));
        this.releaseTimers.set(key, timer);
    }
    restorePreviews(raw) {
        if (!this.enabled || !Array.isArray(raw))
            return;
        const restored = [];
        const deadlines = new Map();
        for (const value of raw) {
            if (!value || typeof value !== "object")
                continue;
            const item = value;
            const sessionId = typeof item.session_id === "string" ? item.session_id : undefined;
            if (!sessionId)
                continue;
            const agentId = typeof item.agent_id === "string" ? item.agent_id : undefined;
            const key = this.previewKey(sessionId, agentId);
            const status = item.status === "busy" || item.status === "error" ? item.status : "ready";
            const deadline = typeof item.release_deadline_ms === "number"
                ? item.release_deadline_ms
                : undefined;
            if (deadline !== undefined && deadline <= Date.now())
                continue;
            restored.push({
                key,
                sessionId,
                agentId,
                mode: item.mode === "foreground_desktop" ? "foreground_desktop" : "background_app",
                deliveryPolicy: item.delivery_policy === "allow_foreground" ? "allow_foreground"
                    : item.delivery_policy === "strict_background" ? "strict_background" : undefined,
                status,
                action: String(item.action || "unknown"),
                summary: String(item.summary || (status === "busy" ? "Executing…" : item.action || "Computer Use")),
                frame: item.frame && typeof item.frame === "object" ? item.frame : null,
                frameUpdatedAt: typeof item.frame_updated_at_ms === "number" ? item.frame_updated_at_ms : undefined,
                observationKind: item.observation_kind === "ax" || item.observation_kind === "screenshot" || item.observation_kind === "ax_and_screenshot" ? item.observation_kind : undefined,
                axElementCount: typeof item.ax_element_count === "number" ? item.ax_element_count : undefined,
                cursor: item.cursor && typeof item.cursor === "object" ? item.cursor : null,
                updatedAt: typeof item.updated_at_ms === "number" ? item.updated_at_ms : Date.now(),
            });
            if (deadline !== undefined)
                deadlines.set(key, deadline);
        }
        if (restored.length === 0)
            return;
        const keys = new Set(restored.map((preview) => preview.key));
        const previews = [
            ...this.state.previews.filter((preview) => !keys.has(preview.key)),
            ...restored,
        ];
        const latest = previews.reduce((a, b) => b.updatedAt > a.updatedAt ? b : a);
        this.publish({
            active: true,
            mode: latest.mode,
            deliveryPolicy: latest.deliveryPolicy,
            previews,
            status: previews.some((preview) => preview.status === "busy")
                ? "busy"
                : previews.some((preview) => preview.status === "error")
                    ? "error"
                    : this.state.status,
        });
        for (const [key, deadline] of deadlines)
            this.scheduleRelease(key, deadline);
    }
    async handleMessage(raw) {
        let message;
        try {
            message = JSON.parse(raw);
        }
        catch {
            this.publish({ status: "error", error: "Computer Use Host received an invalid message" });
            return;
        }
        if (message.type === "computer_use_host_registered" || message.type === "computer_use_host_state_ack") {
            const available = message.available === true;
            this.publish({
                status: !this.enabled ? "disabled" : this.state.previews.some(preview => preview.status === "busy") ? "busy"
                    : available ? "ready" : "unavailable",
                error: this.capabilities?.reason ?? null,
            });
            if (message.type === "computer_use_host_registered")
                this.restorePreviews(message.previews);
            return;
        }
        if (message.type === "computer_use_error") {
            this.publish({ status: "error", error: String(message.error || "Computer Use Host error") });
            return;
        }
        if (message.type === "computer_use_release") {
            const sessionId = typeof message.session_id === "string" ? message.session_id : undefined;
            if (!sessionId)
                return;
            const agentId = typeof message.agent_id === "string" ? message.agent_id : undefined;
            await this.releaseVm(sessionId, agentId, message.all_agents === true);
            await this.releaseAx(sessionId, agentId, message.all_agents === true);
            const keys = message.all_agents === true
                ? this.state.previews
                    .filter((preview) => preview.sessionId === sessionId)
                    .map((preview) => preview.key)
                : [this.previewKey(sessionId, agentId)];
            const deadline = typeof message.release_deadline_ms === "number"
                ? message.release_deadline_ms
                : Date.now() + COMPUTER_USE_RELEASE_RETENTION_MS;
            for (const key of keys) {
                if (this.state.previews.some((preview) => preview.key === key))
                    this.scheduleRelease(key, deadline);
            }
            return;
        }
        if (message.type !== "computer_use_request")
            return;
        const requestId = String(message.request_id || "");
        const action = message.action && typeof message.action === "object"
            ? message.action
            : {};
        const actionName = String(action.action || "unknown");
        const scope = message.target_scope ?? (message.mode === "foreground_desktop" ? "desktop" : "app_window");
        const mode = scope === "desktop" ? "foreground_desktop" : "background_app";
        const policy = message.delivery_policy ?? "allow_foreground";
        const deliveryPolicy = policy === "strict_background" ? "strict_background" : "allow_foreground";
        const logId = requestId || this.platform.randomUuid();
        const sessionId = typeof message.session_id === "string" ? message.session_id : undefined;
        const agentId = typeof message.agent_id === "string" ? message.agent_id : undefined;
        const previewKey = this.previewKey(sessionId, agentId);
        const requestToken = requestId || logId;
        this.latestRequestIds.set(previewKey, requestToken);
        this.cancelRelease(previewKey);
        const previousPreview = this.state.previews.find((preview) => preview.key === previewKey);
        const busyPreview = {
            key: previewKey,
            sessionId,
            agentId,
            mode,
            deliveryPolicy,
            status: "busy",
            action: actionName,
            summary: "Executing…",
            frame: previousPreview?.frame ?? null,
            frameUpdatedAt: previousPreview?.frameUpdatedAt,
            observationKind: previousPreview?.observationKind,
            axElementCount: previousPreview?.axElementCount,
            cursor: previousPreview?.cursor ?? null,
            updatedAt: Date.now(),
        };
        this.publish({
            status: "busy",
            active: true,
            mode,
            deliveryPolicy,
            error: null,
            previews: [...this.state.previews.filter((preview) => preview.key !== previewKey), busyPreview],
            logs: [...this.state.logs, {
                    id: logId,
                    time: Date.now(),
                    action: actionName,
                    summary: "Executing…",
                    ok: true,
                    sessionId,
                    agentId,
                }].slice(-100),
        });
        let result;
        let invoked = false;
        try {
            if (!this.enabled || !this.capabilities?.gui_available)
                throw new Error("Computer Use is disabled or a graphical desktop is unavailable");
            if (scope !== "app_window" && scope !== "desktop")
                throw new Error("Invalid target scope");
            if (policy !== "strict_background" && policy !== "allow_foreground")
                throw new Error("Invalid delivery policy");
            if (scope === "desktop" && policy === "strict_background")
                throw new Error("Desktop scope requires allow_foreground");
            if (message.mode !== undefined && message.mode !== mode)
                throw new Error("Conflicting target scope and mode");
            if (!["observe", "list_windows", "list_displays", "wait"].includes(actionName)
                && this.capabilities.delivery_policy_version !== 1)
                throw new Error("The host must be upgraded before it can enforce the foreground-delivery policy");
            const request = { mode, target_scope: scope, delivery_policy: policy, action };
            const axOperation = ["press", "set_value", "perform_action"].includes(actionName)
                || ["observation", "snapshot_id", "element_id", "ax_action"].some((key) => key in action);
            if (axOperation && (this.vm || scope !== "app_window" || this.capabilities.ax_protocol_version !== 1 || !this.capabilities.ax_available)) {
                throw new Error("AX operations require an AX-capable macOS window host");
            }
            if (this.vm) {
                if (message.environment_id !== vmEnvironmentId(this.vm) || !this.capabilities.instance_id
                    || message.instance_id !== this.capabilities.instance_id)
                    throw new Error("VM binding changed; refresh and observe again");
                invoked = true;
                result = await this.platform.invoke("computer_use_vm_execute", {
                    config: this.vm, instanceId: this.capabilities.instance_id, owner: `${this.hostId}:${sessionId ?? "unknown"}:${agentId ?? "main"}`, request,
                });
            }
            else {
                if (message.environment_id)
                    throw new Error("A VM request cannot execute on the host desktop");
                if (this.capabilities.ax_protocol_version === 1) {
                    if (!sessionId)
                        throw new Error("AX-capable host requests require a session identity");
                    invoked = true;
                    result = await this.platform.invoke("computer_use_execute", { ...(this.capabilities.instance_id ? { instanceId: this.capabilities.instance_id, lease: { host_id: this.hostId, connection_id: this.axConnectionId, session_id: sessionId, agent_id: agentId ?? null } } : {}), request: { ...request,
                            owner: { host_id: this.hostId, connection_id: this.axConnectionId, session_id: sessionId, agent_id: agentId ?? null },
                        } });
                }
                else {
                    invoked = true;
                    result = await this.platform.invoke("computer_use_execute", { request, ...(this.capabilities.instance_id ? { instanceId: this.capabilities.instance_id, lease: { host_id: this.hostId, connection_id: this.axConnectionId, session_id: sessionId, agent_id: agentId ?? null } } : {}) });
                }
            }
        }
        catch (error) {
            result = { ok: false, action: actionName, error: error instanceof Error ? error.message : String(error),
                action_dispatched: invoked ? null : false, retry_safe: !invoked, focus_isolation: "unavailable",
                target_scope: scope, delivery_policy: policy };
        }
        const entry = {
            id: logId,
            time: Date.now(),
            action: String(result.action || actionName),
            summary: String(result.summary || result.error || actionName),
            ok: result.ok !== false,
            sessionId,
            agentId,
        };
        const requestIsCurrent = this.latestRequestIds.get(previewKey) === requestToken;
        if (requestIsCurrent)
            this.latestRequestIds.delete(previewKey);
        const currentPreview = this.state.previews.find((preview) => preview.key === previewKey);
        const shouldPublishCompletion = requestIsCurrent && currentPreview !== undefined;
        const hasAx = result.accessibility !== undefined;
        const keepObservation = !hasAx && !result.screenshot;
        const frame = result.screenshot ?? result.preview_screenshot;
        const completedPreview = {
            key: previewKey,
            sessionId,
            agentId,
            mode,
            deliveryPolicy,
            status: result.ok === false ? "error" : "ready",
            action: String(result.action || actionName),
            summary: String(result.summary || result.error || actionName),
            frame: frame ?? currentPreview?.frame ?? null,
            frameUpdatedAt: frame ? Date.now() : currentPreview?.frameUpdatedAt,
            observationKind: result.observation_kind ?? (keepObservation ? currentPreview?.observationKind
                : hasAx ? result.screenshot ? "ax_and_screenshot" : "ax" : "screenshot"),
            axElementCount: hasAx ? result.accessibility?.element_count ?? result.accessibility?.elements?.length
                : keepObservation ? currentPreview?.axElementCount : undefined,
            cursor: result.cursor ?? currentPreview?.cursor ?? null,
            updatedAt: Date.now(),
        };
        const previews = !this.enabled
            ? []
            : shouldPublishCompletion
                ? [...this.state.previews.filter((preview) => preview.key !== previewKey), completedPreview]
                : this.state.previews;
        const anyPreviewIsBusy = previews.some((preview) => preview.status === "busy");
        this.publish({
            status: !this.enabled
                ? "disabled"
                : anyPreviewIsBusy
                    ? "busy"
                    : shouldPublishCompletion && result.ok === false
                        ? "error"
                        : this.capabilities?.gui_available ? "ready" : "unavailable",
            active: previews.length > 0,
            previews,
            logs: [...this.state.logs.filter((item) => item.id !== logId), entry].slice(-100),
            error: shouldPublishCompletion && result.ok === false
                ? String(result.error || "Computer Use action failed")
                : this.capabilities?.reason ?? null,
        });
        const releaseDeadline = this.releaseDeadlines.get(previewKey);
        if (releaseDeadline !== undefined && !this.releaseTimers.has(previewKey)) {
            this.scheduleRelease(previewKey, releaseDeadline);
        }
        if (!this.enabled)
            this.cancelAllReleases();
        // Keep monitor pixels outside the tool result even with an older Gateway
        // that does not yet know to remove transport-only fields.
        const { preview_screenshot, preview_screenshot_error: _previewError, ...toolResult } = result;
        this.send({ type: "computer_use_result", request_id: requestId, result: toolResult, preview_screenshot });
    }
}
