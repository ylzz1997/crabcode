import { setLocale, getLocale } from "../../shared/i18n/index.js";
import { t as tr, html as trHtml, localized } from "../../shared/i18n/index.js";
import { nodeComputerUseChannel } from './nodeChannel.js';
import { computerUseReauthorizationHint, initialComputerUseState } from './channel.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const labels = localized({ disabled: '已关闭', connecting: '正在连接', unavailable: '不可用', ready: '可用', busy: '正在操作', error: '错误' });

/** UI host shared by the ordinary extension and the local Remote SSH companion. */
export class VSCodeComputerUseHost {
  constructor(vscode, WebSocket, bundleDirectory) {
    this.vscode = vscode;
    setLocale(vscode.workspace?.getConfiguration("crabcode").get("language", "auto") || "auto", [vscode.env?.language || "zh-CN"]);
    this.WebSocket = WebSocket;
    this.bundleDirectory = bundleDirectory;
    this.state = initialComputerUseState('', false);
    this.render();
  }

  configure(options) {
    setLocale(options.language || "auto", [this.vscode.env?.language || "zh-CN"]);
    const identity = JSON.stringify([options.gatewayUrl, options.password, options.hostId]);
    if (!this.runtime || this.identity !== identity) {
      this.runtime?.channel.dispose();
      this.identity = identity;
      this.state = initialComputerUseState(options.hostId, options.enabled);
      this.runtime = nodeComputerUseChannel({ ...options, WebSocket: this.WebSocket, bundleDirectory: this.bundleDirectory,
        onState: state => { this.state = state; this.render(); } });
      void this.runtime.channel.connect();
    } else this.runtime.channel.setEnabled(options.enabled);
    this.render();
    return { hostId: options.hostId, enabled: options.enabled };
  }

  get statusLabel() {
    const limited = this.state.status === 'ready' && (this.state.capabilities?.input_available === false || this.state.capabilities?.capture_available === false);
    return limited ? tr("有限可用") : labels[this.state.status];
  }

  get capturePermissionHint() {
    return this.state.capabilities?.platform === 'macos' && this.state.capabilities.capture_available === false
      ? tr(computerUseReauthorizationHint(this.state.capabilities, 'screen_recording')) + tr("窗口列表和截图需要录屏权限。选择「打开录屏权限设置」后，将弹出的 Crab Computer Use.app 卡片拖到录屏列表并打开开关；也可点＋，按 ⌘⇧G 输入 ~/Library/Application Support/CrabCode/ComputerUse/Crab Computer Use.app 后添加。Crab Desktop 或 VS Code 的授权不能代替此组件的授权。完成后选择「重新检测组件与权限」。")
      : '';
  }

  get accessibilityPermissionHint() {
    return this.state.capabilities?.platform === 'macos' && this.state.capabilities.input_available === false
      ? tr(computerUseReauthorizationHint(this.state.capabilities, 'accessibility')) + tr("点击、输入和滚动需要辅助功能权限。选择「打开辅助功能权限设置」后，将弹出的 Crab Computer Use.app 卡片拖到辅助功能列表并打开开关；完成后选择「重新检测组件与权限」。")
      : '';
  }

  render() {
    if (this.statusPanel) this.renderStatus();
    if (!this.panel) return;
    const previews = this.state.previews.map(preview => {
      const frame = preview.frame;
      const safeFrame = frame && ['image/png', 'image/jpeg', 'image/webp'].includes(frame.media_type) && /^[A-Za-z0-9+/=\r\n]*$/.test(frame.data);
      return `<article><h3>${escape(preview.sessionId)} · ${escape(preview.agentId || tr("主 Agent"))}</h3>
        <p>${escape(preview.summary)} · ${preview.deliveryPolicy === 'strict_background' ? tr("严格后台") : tr("允许前台")}</p>
        ${safeFrame ? `<img alt="${escape(tr("Computer Use 监看截图"))}" src="data:${frame.media_type};base64,${frame.data}" />` : trHtml("<p>暂无监看截图</p>")}
        ${preview.observationKind?.includes('ax') ? trHtml("<p>本次模型读取 AX Tree；上图仅供监看。</p>") : ''}</article>`;
    }).join('');
    this.panel.webview.html = (trHtml("<!doctype html><html lang=\"" + getLocale() + "\"><head><meta charset=\"utf-8\">\n      <meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline';\">\n      <style>body{font-family:var(--vscode-font-family);padding:16px;color:var(--vscode-foreground)}article{border:1px solid var(--vscode-panel-border);padding:12px;margin:12px 0}img{max-width:100%;max-height:65vh;object-fit:contain}li{margin:8px 0}p{overflow-wrap:anywhere}</style></head>\n      <body><h2>Computer Use · ") + (this.statusLabel) + trHtml("</h2><p>") + (escape(this.state.error || tr("等待 Agent 开始操作电脑"))) + trHtml("</p>\n      ") + (this.accessibilityPermissionHint ? `<p>${escape(this.accessibilityPermissionHint)}</p>` : '') + trHtml("\n      ") + (this.capturePermissionHint ? `<p>${escape(this.capturePermissionHint)}</p>` : '') + trHtml("\n      ") + (previews) + trHtml("<h3>操作记录</h3><ul>") + ([...this.state.logs].reverse().map(entry => `<li>${escape(new Date(entry.time).toLocaleTimeString(getLocale()))} · ${escape(entry.summary)}</li>`).join('')) + trHtml("</ul></body></html>"));
  }

  renderStatus() {
    const capabilities = this.state.capabilities;
    const availability = value => value === true ? tr("可用") : value === false ? tr("不可用") : tr("未检测");
    const platforms = { macos: 'macOS', darwin: 'macOS', windows: 'Windows', win32: 'Windows', linux: 'Linux' };
    const rows = [
      [tr("启用状态"), this.state.enabled ? tr("已启用") : tr("已关闭")],
      [tr("当前状态"), this.statusLabel],
      [tr("正在操作电脑"), this.state.active ? tr("是") : tr("否")],
      [tr("操作系统"), platforms[capabilities?.platform] || capabilities?.platform || tr("未检测")],
      [tr("图形界面"), availability(capabilities?.gui_available)],
      [tr("键鼠输入"), availability(capabilities?.input_available)],
      [tr("屏幕截图"), availability(capabilities?.capture_available)],
      [tr("辅助功能读取（AX）"), availability(capabilities?.ax_available)],
      [tr("严格后台输入"), availability(capabilities?.strict_background_input_available)],
      [tr("显示器数量"), capabilities ? String(capabilities.displays?.length ?? 0) : tr("未检测")],
    ];
    const notes = [...new Set([this.capturePermissionHint, this.accessibilityPermissionHint, this.state.error, capabilities?.reason, capabilities?.strict_background_reason].filter(Boolean))];
    this.statusPanel.webview.html = (trHtml("<!doctype html><html lang=\"" + getLocale() + "\"><head><meta charset=\"utf-8\">\n      <meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline';\">\n      <style>body{font-family:var(--vscode-font-family);padding:16px;color:var(--vscode-foreground);max-width:720px}dl{display:grid;grid-template-columns:minmax(120px,1fr) 2fr;gap:0}dt,dd{margin:0;padding:12px 0;border-bottom:1px solid var(--vscode-panel-border);overflow-wrap:anywhere}dt{color:var(--vscode-descriptionForeground);padding-right:16px}p{line-height:1.6;overflow-wrap:anywhere}</style></head>\n      <body><h2>Computer Use 状态</h2><dl>") + (rows.map(([name, value]) => `<dt>${escape(name)}</dt><dd>${escape(value)}</dd>`).join('')) + trHtml("</dl>\n      ") + (notes.map(note => `<p>${escape(note)}</p>`).join('')) + trHtml("\n      <p>") + (!this.state.enabled && capabilities ? tr("Computer Use 已关闭，权限信息为上次检测结果。") : tr("状态随连接与权限检测结果自动更新。")) + trHtml("如需重新检查权限，请在 Computer Use 菜单选择「重新检测组件与权限」。</p></body></html>"));
  }

  async menu() {
    const permissionChoices = this.state.capabilities?.platform === 'macos'
      ? [
        { label: tr("打开录屏权限设置"), action: 'screen_recording_permissions' },
        { label: tr("打开辅助功能权限设置"), action: 'accessibility_permissions' },
      ]
      : [{ label: tr("打开系统权限设置"), action: 'permissions' }];
    const choice = await this.vscode.window.showQuickPick([
      { label: tr("Computer Use 状态查询"), action: 'status' },
      { label: this.state.enabled ? tr("关闭 Computer Use") : tr("启用 Computer Use"), action: 'toggle' },
      { label: tr("查看监看截图和操作记录"), action: 'monitor' },
      { label: tr("重新检测组件与权限"), action: 'refresh' },
      ...permissionChoices,
    ], { title: `Computer Use · ${this.statusLabel}` });
    if (!choice) return;
    try {
      if (choice.action === 'toggle') {
        const config = this.vscode.workspace.getConfiguration('crabcode');
        const target = config.inspect('computerUseEnabled')?.workspaceValue !== undefined
          ? this.vscode.ConfigurationTarget.Workspace : this.vscode.ConfigurationTarget.Global;
        await config.update('computerUseEnabled', !this.state.enabled, target);
      } else if (choice.action === 'refresh') this.runtime?.channel.refresh();
      else if (choice.action === 'screen_recording_permissions') await this.runtime?.openPermissions('screen_recording');
      else if (choice.action === 'accessibility_permissions') await this.runtime?.openPermissions('accessibility');
      else if (choice.action === 'permissions') await this.runtime?.openPermissions();
      else if (choice.action === 'status') {
        if (this.statusPanel) return this.statusPanel.reveal();
        this.statusPanel = this.vscode.window.createWebviewPanel('crabcode.computerUseStatus', tr("Computer Use 状态"), this.vscode.ViewColumn.Beside, { enableScripts: false });
        this.statusPanel.onDidDispose(() => { this.statusPanel = null; });
        this.renderStatus();
      }
      else if (choice.action === 'monitor') {
        if (this.panel) return this.panel.reveal();
        this.panel = this.vscode.window.createWebviewPanel('crabcode.computerUseMonitor', 'Computer Use', this.vscode.ViewColumn.Beside, { enableScripts: false });
        this.panel.onDidDispose(() => { this.panel = null; });
        this.render();
      }
    } catch (error) { void this.vscode.window.showErrorMessage(String(error.message || error)); }
  }

  dispose() {
    this.runtime?.channel.dispose();
    this.panel?.dispose();
    this.statusPanel?.dispose();
  }
}
