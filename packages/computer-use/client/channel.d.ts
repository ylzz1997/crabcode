/** Environment adapters are the only client-specific part of the host channel. */
export interface ComputerUseVmConfig {
    name: string;
    storage: string;
    user: string;
    shared_directory: string;
    shared_read_only: boolean;
    forwarded_ports: number[];
}
export interface ComputerUseSocket {
    readyState: number;
    addEventListener(type: string, listener: (event: any) => void): void;
    send(data: string): void;
    close(): void;
}
export interface ComputerUseApi {
    authenticate(): Promise<void>;
    computerUseWebSocketUrl(): string;
}
export interface ComputerUsePlatform {
    isNative(): boolean;
    platform: string;
    randomUuid(): string;
    invoke<T = unknown>(command: string, args?: Record<string, unknown>): Promise<T>;
    createSocket(url: string): ComputerUseSocket;
    setTimeout(callback: () => void, delay: number): number;
    clearTimeout(timer: number): void;
}
export type ComputerUseStatus = "disabled" | "unavailable" | "connecting" | "ready" | "busy" | "error";
export declare const COMPUTER_USE_RELEASE_RETENTION_MS = 15000;
export type ComputerUsePermissionKind = "screen_recording" | "accessibility";
export interface ComputerUseCapabilities {
    environment?: "host" | "local_vm";
    environment_id?: string;
    environment_name?: string;
    instance_id?: string;
    paused?: boolean;
    gui_available: boolean;
    input_available: boolean;
    capture_available?: boolean;
    ax_available?: boolean;
    reauthorization_required?: ComputerUsePermissionKind[];
    ax_protocol_version?: number | null;
    window_observation_version?: number | null;
    platform: string;
    displays: Array<{
        id: string;
        name: string;
        x: number;
        y: number;
        width: number;
        height: number;
        primary: boolean;
    }>;
    supported_modes: Array<"background_app" | "foreground_desktop">;
    delivery_policy_version?: number;
    strict_background_input_available?: boolean;
    strict_background_reason?: string;
    reason?: string | null;
}
export declare function computerUseReauthorizationHint(capabilities: ComputerUseCapabilities | null | undefined, kind: ComputerUsePermissionKind): string;
export interface ComputerUseFrame {
    data: string;
    media_type: string;
    width: number;
    height: number;
    origin_x: number;
    origin_y: number;
    frame_id: string;
}
export interface ComputerUseCursor {
    x: number;
    y: number;
}
export interface ComputerUsePreview {
    key: string;
    sessionId?: string;
    agentId?: string;
    mode: "background_app" | "foreground_desktop";
    deliveryPolicy?: "strict_background" | "allow_foreground";
    status: "busy" | "ready" | "error";
    action: string;
    summary: string;
    frame: ComputerUseFrame | null;
    frameUpdatedAt?: number;
    observationKind?: "ax" | "screenshot" | "ax_and_screenshot";
    axElementCount?: number;
    cursor: ComputerUseCursor | null;
    updatedAt: number;
}
export interface ComputerUseLogEntry {
    id: string;
    time: number;
    action: string;
    summary: string;
    ok: boolean;
    sessionId?: string;
    agentId?: string;
}
export interface ComputerUseState {
    hostId: string;
    enabled: boolean;
    active: boolean;
    mode: "background_app" | "foreground_desktop";
    deliveryPolicy?: "strict_background" | "allow_foreground";
    status: ComputerUseStatus;
    capabilities: ComputerUseCapabilities | null;
    previews: ComputerUsePreview[];
    logs: ComputerUseLogEntry[];
    error: string | null;
}
export declare function initialComputerUseState(hostId: string, enabled: boolean): ComputerUseState;
export declare class ComputerUseChannel {
    private api;
    private hostId;
    private onState;
    private platform;
    private vm;
    private socket;
    private reconnectTimer;
    private heartbeatTimer;
    private attempts;
    private capabilityGeneration;
    private releaseTimers;
    private releaseDeadlines;
    private latestRequestIds;
    private disposed;
    private enabled;
    private capabilities;
    private axConnectionId;
    private state;
    constructor(api: ComputerUseApi, hostId: string, enabled: boolean, onState: (state: ComputerUseState) => void, platform: ComputerUsePlatform, vm?: ComputerUseVmConfig | null);
    private publish;
    connect(): Promise<void>;
    private refreshCapabilities;
    setEnabled(enabled: boolean): void;
    refresh(): void;
    dispose(): void;
    private scheduleHeartbeat;
    private releaseVm;
    private releaseAx;
    private sendRegistration;
    private send;
    private scheduleReconnect;
    private previewKey;
    private cancelRelease;
    private cancelAllReleases;
    private scheduleRelease;
    private restorePreviews;
    private handleMessage;
}
