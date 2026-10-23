import { t as tr } from "./i18n";
import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  AlertTriangle, ArrowUpRight, Check, ChevronDown, Download, FolderOpen,
  KeyRound, LoaderCircle, Monitor, MoreHorizontal, Pause, Play, Plus,
  Power, RefreshCw, Save, Server, Settings2, ShieldCheck,
} from "lucide-react";
import { isDesktopShell } from "./native";
import { isWindowsPlatform } from "./platform";
import type { DesktopSettings } from "./types";
import type { ComputerUseCapabilities } from "./computerUse";
import type { LumeInstallerState } from "./lumeInstaller";
import { listVirtualMachines, manageVirtualMachine, normalizeVmConfig, type LocalVmConfig, type VmInfo } from "./virtualMachine";

export type VmSettingsUpdate = Pick<DesktopSettings, "computer_use_environment" | "computer_use_vm">;
interface Props {
  settings: DesktopSettings;
  taskBusy?: boolean;
  onChange?: (changes: VmSettingsUpdate) => void;
  onRefresh?: () => void;
  lumeInstaller?: LumeInstallerState;
}
export function VirtualMachineSettings({ settings, taskBusy = false, onChange, onRefresh, lumeInstaller }: Props) {
  const [config, setConfig] = useState(() => normalizeVmConfig(settings.computer_use_vm));
  const [ports, setPorts] = useState(() => normalizeVmConfig(settings.computer_use_vm).forwarded_ports.join(", "));
  const [machines, setMachines] = useState<VmInfo[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [resources, setResources] = useState({ cpus: 4, memory_gb: 8, disk_gb: 80, ipsw: "latest" });
  const moreActionsRef = useRef<HTMLDetailsElement>(null);
  const native = isDesktopShell();
  const windows = isWindowsPlatform();
  const selected = settings.computer_use_environment === "local_vm";
  const locked = !native || busy !== null || taskBusy || Boolean(lumeInstaller?.busy);
  const engineReady = lumeInstaller?.status?.available;
  const vmLocked = locked || lumeInstaller?.status?.available === false;
  const refreshLume = lumeInstaller?.refresh;
  const needsLumeCheck = !lumeInstaller?.status && !lumeInstaller?.error;
  useEffect(() => { if (native && selected && needsLumeCheck) void refreshLume?.(); }, [native, selected, needsLumeCheck, refreshLume]);
  useEffect(() => { setConfig(normalizeVmConfig(settings.computer_use_vm)); setPorts(normalizeVmConfig(settings.computer_use_vm).forwarded_ports.join(", ")); }, [settings.computer_use_vm]);

  async function run(operation: string) {
    if (moreActionsRef.current) moreActionsRef.current.open = false;
    setBusy(operation); setError(null); setMessage(null);
    try {
      if (operation === "list") {
        setMachines(await listVirtualMachines(config.storage));
      } else if (operation === "check") {
        const state = await invoke<ComputerUseCapabilities>("computer_use_vm_capabilities", { config });
        if (!state.gui_available || !state.input_available) throw new Error(state.reason || tr("请在虚拟机内授权录屏和辅助功能"));
        setMessage(tr("已连接 {0}：截图和输入可用。", { 0: config.name }));
        onRefresh?.();
      } else {
        const result = await manageVirtualMachine(config, operation, { password: operation === "install" ? password : undefined, create: operation === "create" ? resources : undefined });
        if (result.ok === false) throw new Error(result.error || tr("虚拟机操作失败"));
        const labels: Record<string, string> = {
          start: tr("虚拟机已后台启动；等待 macOS 登录后安装执行器或检查连接。"),
          stop: tr("虚拟机已关闭。"), create: tr("虚拟机已创建，可以后台启动。"),
          install: tr("执行器已安装。请打开虚拟机，授权 Crab Computer Use 的录屏和辅助功能，然后检查连接。"),
          permissions: tr("已在虚拟机内请求权限；请打开虚拟机完成授权。"),
          takeover: tr("已暂停虚拟机自动操作，可以打开桌面接管。"),
          resume: tr("已恢复自动操作；检查连接后重新观察桌面。"),
          view: tr("已打开虚拟机桌面。"),
        };
        setMessage(result.summary || labels[operation] || tr("操作完成"));
        onRefresh?.();
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setPassword(""); setBusy(null); }
  }
  function saveConfig() {
    const parsed = ports.trim() ? ports.split(/[,，\s]+/).map(Number) : [];
    if (parsed.length > 8 || new Set(parsed).size !== parsed.length || parsed.some(p => !Number.isInteger(p) || p < 1 || p > 65535)) {
      setError(tr("请输入最多 8 个不同的端口号（1–65535），用逗号分隔。")); return;
    }
    const next = { ...config, forwarded_ports: parsed }; setConfig(next); setError(null);
    onChange?.({ computer_use_environment: "local_vm", computer_use_vm: next });
    setMessage(tr("虚拟机配置已保存，新建或重新打开会话后生效。"));
  }
  function field(key: keyof LocalVmConfig, value: string | boolean) { setConfig(current => ({ ...current, [key]: value })); }
  return <div className="vm-settings" role="group" aria-label={tr("Computer Use 执行环境")}>
    <div className="vm-environment-row">
      <div className="vm-copy">
        <strong>{tr("执行环境")}</strong>
        <span>{selected ? tr("使用独立的 macOS 桌面，操作不打扰本机。") : tr("使用本机上的应用窗口或桌面。")}</span>
      </div>
      <div className="settings-segmented" aria-label={tr("Computer Use 执行环境选择")}>
        <button type="button" disabled={locked || !onChange} aria-pressed={!selected} className={!selected ? "active" : ""} onClick={() => onChange?.({ computer_use_environment: "host", computer_use_vm: normalizeVmConfig(settings.computer_use_vm) })}>{tr("本机")}</button>
        <button type="button" disabled={locked || !onChange || windows} title={windows ? tr("Windows 暂不支持本地虚拟机") : undefined} aria-pressed={selected} className={selected ? "active" : ""} onClick={() => { if (!windows) onChange?.({ computer_use_environment: "local_vm", computer_use_vm: config }); }}>{tr("本地虚拟机")}</button>
      </div>
    </div>
    {!native && <p className="vm-feedback">{tr("请在 Apple Silicon Mac 上使用 Crab Desktop 配置本地虚拟机。")}</p>}
    {native && windows && <p className="vm-feedback">{tr("本地虚拟机目前不支持Windows平台")}</p>}
    {taskBusy && <p className="vm-feedback"><ShieldCheck />{tr("任务运行中，环境配置已锁定；可在“更多操作”中暂停。")}</p>}
    {selected && !windows && <div className="vm-workspace">
      <div className="vm-engine-install">
        <div className="vm-engine-row">
          <span className="vm-engine-icon" aria-hidden="true"><Server /></span>
          <div className="vm-engine-copy">
            <div className="vm-engine-title">
              <strong>Lume</strong>
              <span className={`vm-engine-badge ${engineReady ? "ready" : ""}`} role="status">
                {engineReady ? <Check /> : <span className="vm-status-dot" />}
                {lumeInstaller?.busy ? tr("安装中") : lumeInstaller?.checking ? tr("检测中") : engineReady ? tr("已安装") : lumeInstaller?.status?.supported && !lumeInstaller.status.path ? tr("未安装") : tr("未就绪")}
              </span>
            </div>
            <span className="vm-caption">{engineReady ? tr("已就绪 · {0}", { 0: lumeInstaller?.status?.version }) : tr("本地虚拟机引擎 · 无需管理员密码")}</span>
          </div>
          <div className="vm-engine-actions">
            <button className="vm-button icon quiet" type="button" aria-label={tr("重新检测 Lume")} title={tr("重新检测 Lume")}
              disabled={!native || busy !== null || lumeInstaller?.busy || lumeInstaller?.checking || !lumeInstaller}
              onClick={() => void lumeInstaller?.refresh()}><RefreshCw className={lumeInstaller?.checking ? "spin" : ""} /></button>
            {!engineReady && <button className="vm-button primary" type="button"
              disabled={locked || !lumeInstaller || lumeInstaller.checking || !lumeInstaller.status?.supported}
              onClick={() => void lumeInstaller?.install()}>
              {lumeInstaller?.busy ? <LoaderCircle className="spin" /> : <Download />}
              {lumeInstaller?.busy ? tr("正在安装…") : lumeInstaller?.error ? tr("重试安装 Lume") : lumeInstaller?.status?.path ? tr("升级 / 修复 Lume") : tr("一键安装 Lume")}
            </button>}
            <a className="vm-help-link" href="https://cua.ai/docs/how-to-guides/lume/install-lume" target="_blank" rel="noreferrer" title={tr("Lume 官方安装说明")}>{tr("安装说明")}<ArrowUpRight /></a>
          </div>
        </div>
        {!engineReady && lumeInstaller?.status?.reason && lumeInstaller.status.reason !== tr("尚未安装 Lume") && <p className="vm-engine-reason">{lumeInstaller.status.reason}</p>}
        {lumeInstaller?.busy && lumeInstaller.progress && <div className="document-engine-progress vm-install-progress" role="progressbar" aria-label={tr("Lume 安装进度")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={lumeInstaller.progress.percent} aria-valuetext={lumeInstaller.progress.detail}>
          <div className="document-engine-progress-copy"><span>{lumeInstaller.progress.detail}</span><strong>{lumeInstaller.progress.percent}%</strong></div>
          <div className="document-engine-progress-track"><i style={{ width: `${lumeInstaller.progress.percent}%` }} /></div>
        </div>}
        {lumeInstaller?.error && <p className="vm-install-error" role="alert">{lumeInstaller.error}</p>}
      </div>

      <div className="vm-machine-section">
        <div className="vm-section-heading">
          <h4>{tr("虚拟机")}</h4>
          <button className="vm-button quiet" type="button" disabled={vmLocked} aria-expanded={createOpen} aria-controls="vm-create-form" onClick={() => setCreateOpen(!createOpen)}><Plus />{createOpen ? tr("收起新建") : tr("新建虚拟机")}</button>
        </div>
        <div className="vm-fields vm-identity-fields">
          <label>{tr("名称")}<div className="vm-input-with-action">
              <input aria-label={tr("虚拟机名称")} placeholder={tr("选择或输入名称")} list="crab-local-vms" value={config.name} disabled={locked} onChange={e => field("name", e.target.value)} />
              <button className="vm-button icon quiet" type="button" disabled={vmLocked} title={tr("读取 VM 列表")} aria-label={tr("读取 VM 列表")} onClick={() => void run("list")}><RefreshCw className={busy === "list" ? "spin" : ""} /></button>
            </div>
          </label>
          <datalist id="crab-local-vms">{machines.map(vm => <option key={vm.name} value={vm.name}>{vm.status}</option>)}</datalist>
          <label>{tr("登录用户")}<input aria-label={tr("虚拟机用户名")} value={config.user} disabled={locked} onChange={e => field("user", e.target.value)} /></label>
        </div>
        {createOpen && <div className="vm-create" id="vm-create-form">
          <div className="vm-section-heading"><h4>{tr("新建 macOS 虚拟机")}</h4><span className="vm-caption">{tr("使用上方名称")}</span></div>
          <div className="vm-fields vm-resource-fields">
            {(["cpus", "memory_gb", "disk_gb"] as const).map((key, i) => <label key={key}>{[tr("CPU 核数"), tr("内存 · GiB"), tr("磁盘 · GiB")][i]}<input type="number" aria-label={[tr("虚拟机 CPU 核数"), tr("虚拟机内存 GiB"), tr("虚拟机磁盘 GiB")][i]} min={[2, 4, 50][i]} max={[32, 128, 2048][i]} value={resources[key]} disabled={locked} onChange={e => setResources(r => ({ ...r, [key]: Number(e.target.value) }))} /></label>)}
          </div>
          <label className="vm-field">{tr("macOS Tahoe 安装镜像")}<input aria-label={tr("macOS 安装镜像")} placeholder={tr("IPSW 路径或 latest")} value={resources.ipsw} disabled={locked} onChange={e => setResources(r => ({ ...r, ipsw: e.target.value }))} /></label>
          <p className="vm-caption">{tr("至少预留 50 GB 空间。下载和安装需要较长时间；默认账户为 lume。")}</p>
          <button className="vm-button primary" type="button" disabled={vmLocked} onClick={() => void run("create")}><Download />{tr("下载并创建")}</button>
        </div>}
        <div className="vm-control-bar">
          <button className="vm-button primary" type="button" disabled={vmLocked} onClick={() => void run("start")}><Play />{tr("后台启动")}</button>
          <button className="vm-button" type="button" disabled={!native || busy !== null || lumeInstaller?.status?.available === false} onClick={() => void run("view")}><Monitor />{tr("打开虚拟机桌面")}</button>
          <button className="vm-button quiet" type="button" disabled={vmLocked} onClick={() => void run("check")}><ShieldCheck />{tr("检查连接")}</button>
          <details className="vm-more-actions" ref={moreActionsRef}
            onKeyDown={e => { if (e.key === "Escape") { e.currentTarget.open = false; e.currentTarget.querySelector("summary")?.focus(); } }}
            onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) e.currentTarget.open = false; }}>
            <summary className="vm-button quiet" aria-label={tr("更多操作")} title={tr("更多操作")}><MoreHorizontal /><span>{tr("更多")}</span></summary>
            {/* Safari does not focus buttons on click; retain summary focus until
                the action runs so blur cannot close the menu before click. */}
            <div className="vm-action-menu" onMouseDown={e => e.preventDefault()}>
              <button type="button" disabled={!native || busy !== null} onClick={() => void run("takeover")}><Pause />{tr("暂停自动操作")}</button>
              <button type="button" disabled={vmLocked} onClick={() => void run("resume")}><Play />{tr("恢复自动操作")}</button>
              <button className="vm-stop" type="button" disabled={vmLocked} onClick={() => void run("stop")}><Power />{tr("关闭 VM")}</button>
            </div>
          </details>
        </div>
      </div>

      <div className="vm-options">
        <details className="vm-disclosure">
          <summary><KeyRound /><span className="vm-disclosure-copy"><strong>{tr("安装与权限")}</strong><span>{tr("首次连接时配置")}</span></span><ChevronDown className="vm-chevron" /></summary>
          <div className="vm-disclosure-body">
            <p className="vm-caption">{tr("启动虚拟机并登录桌面，开启“远程登录（SSH）”。安装执行器后，在虚拟机内授予录屏与辅助功能权限。")}</p>
            <label className="vm-field">{tr("虚拟机账户密码")}<input type="password" autoComplete="off" placeholder={tr("已配置 SSH 密钥时可留空")} aria-label={tr("虚拟机账户密码")} value={password} disabled={locked} onChange={e => setPassword(e.target.value)} /></label>
            <p className="vm-caption">{tr("仅用于首次配置专用 SSH 密钥，不会保存。权限变更后可重启执行器。")}</p>
            <div className="vm-inline-actions">
              <button className="vm-button" type="button" disabled={vmLocked} onClick={() => void run("install")}><Download />{tr("安装／重启执行器")}</button>
              <button className="vm-button quiet" type="button" disabled={vmLocked} onClick={() => void run("permissions")}><ShieldCheck />{tr("在虚拟机内请求权限")}</button>
            </div>
          </div>
        </details>
        <details className="vm-disclosure">
          <summary><Settings2 /><span className="vm-disclosure-copy"><strong>{tr("共享与高级设置")}</strong><span>{tr("共享目录、端口转发、存储位置")}</span></span><ChevronDown className="vm-chevron" /></summary>
          <div className="vm-disclosure-body">
            <label className="vm-field">{tr("共享目录")}<input aria-label={tr("虚拟机共享目录")} placeholder={tr("/Users/you/Projects（可留空）")} value={config.shared_directory} disabled={locked} onChange={e => field("shared_directory", e.target.value)} /></label>
            <label className="vm-checkbox"><input type="checkbox" checked={config.shared_read_only} disabled={locked} onChange={e => field("shared_read_only", e.target.checked)} />{tr("只读共享")}<span>{tr("保护本机文件")}</span></label>
            <p className="vm-caption">{tr("下次启动时挂载。关闭只读后，虚拟机可直接修改此目录中的本机文件。")}</p>
            <div className="vm-fields vm-advanced-fields">
              <label>{tr("本机 TCP 端口")}<input aria-label={tr("转发本机 TCP 端口")} placeholder={tr("例如 3000, 5173")} value={ports} disabled={locked} onChange={e => setPorts(e.target.value)} /></label>
              <label>{tr("Lume 存储位置")}<input aria-label={tr("Lume 存储名称")} value={config.storage} disabled={locked} onChange={e => field("storage", e.target.value)} /></label>
            </div>
            <p className="vm-caption">{tr("保存并检查连接后，可在虚拟机中通过 localhost 的相同端口访问本机服务；退出 Desktop 后转发关闭。")}</p>
            <div className="vm-scope-note"><FolderOpen /><span>{tr("Computer Use 操作虚拟机；Bash、Read、Edit 仍在 Gateway 所在电脑执行。应用与登录状态需在虚拟机中单独配置。")}</span></div>
          </div>
        </details>
      </div>
      <div className="vm-save-row">
        <span className="vm-caption">{tr("配置保存在本机，下次打开会话生效")}</span>
        <button className="vm-button" type="button" disabled={locked || !onChange} onClick={saveConfig}><Save />{tr("保存虚拟机配置")}</button>
      </div>
    </div>}
    {busy && <p className="vm-feedback"><LoaderCircle className="spin" />{busy === "create" ? tr("正在下载／安装 macOS，请保持 Crab Desktop 打开…") : tr("正在处理虚拟机…")}</p>}
    {error && <p role="alert" className="vm-feedback error"><AlertTriangle />{error}</p>}
    {message && <p role="status" className="vm-feedback success"><Check />{message}</p>}
  </div>;
}
