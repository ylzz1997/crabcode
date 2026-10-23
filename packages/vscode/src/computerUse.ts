import { t as tr, getLocale } from "./i18n";
import * as vscode from 'vscode';
import { randomUUID } from 'node:crypto';
import type { ComputerUseBinding, ComputerUseHostOptions } from '../../computer-use/client/vscodeHost.js';
import type { CrabCodeConnection } from './connection';

export class ComputerUseController implements vscode.Disposable {
  private disposed = false;
  private revision = 0;
  private readonly hostId: string;

  constructor(_context: vscode.ExtensionContext, private readonly connection: CrabCodeConnection, private readonly output: vscode.OutputChannel) {
    // workspaceState is shared by windows on the same workspace. A window-local
    // identity prevents their host registrations from replacing each other.
    this.hostId = `vscode-${randomUUID()}`;
  }

  async configure(): Promise<void> {
    const revision = ++this.revision;
    const config = vscode.workspace.getConfiguration('crabcode');
    const enabled = config.get<boolean>('computerUseEnabled', true);
    // Bind immediately even while capability detection is pending, just as Desktop
    // does. Gateway availability gates tool exposure independently.
    this.connection.setComputerUseBinding({ hostId: this.hostId, enabled });
    try {
      if (!enabled) {
        await vscode.commands.executeCommand('crabcode.computerUse.disposeLocal', this.hostId).then(undefined, () => {});
        return;
      }
      let gatewayUrl = config.get<string>('serverUrl', 'ws://localhost:4096/ws');
      if (vscode.env.remoteName) {
        const remote = new URL(gatewayUrl);
        const query = remote.search;
        remote.search = '';
        remote.protocol = remote.protocol === 'wss:' ? 'https:' : 'http:';
        const forwarded = await vscode.env.asExternalUri(vscode.Uri.parse(remote.toString()));
        const localUrl = new URL(forwarded.toString());
        localUrl.protocol = localUrl.protocol === 'https:' ? 'wss:' : 'ws:';
        for (const [name, value] of new URLSearchParams(query)) localUrl.searchParams.set(name, value);
        gatewayUrl = localUrl.toString();
      }
      if (this.disposed || revision !== this.revision) return;
      const options: ComputerUseHostOptions = { gatewayUrl, password: config.get<string>('password', ''), hostId: this.hostId, enabled, language: getLocale() };
      const binding = await vscode.commands.executeCommand<ComputerUseBinding>('crabcode.computerUse.configureLocal', options);
      if (!this.disposed && revision === this.revision && binding) this.connection.setComputerUseBinding(binding);
    } catch (error) {
      if (this.disposed || revision !== this.revision) return;
      this.connection.setComputerUseBinding({ hostId: this.hostId, enabled: false });
      this.output.appendLine(`Computer Use: ${error instanceof Error ? error.message : String(error)}`);
      if (enabled) void vscode.window.showErrorMessage(tr("请在本机安装或更新 CrabCode Computer Use 配套扩展，然后重新检测。Computer Use 操作的是本机桌面。"));
    }
  }

  async showMenu(): Promise<void> {
    try {
      await vscode.commands.executeCommand('crabcode.computerUse.local');
    } catch (error) {
      this.output.appendLine(`Computer Use: ${error instanceof Error ? error.message : String(error)}`);
      void vscode.window.showErrorMessage(tr("请在本机安装或更新 CrabCode Computer Use 配套扩展，然后重试。"));
    }
  }

  dispose(): void {
    this.disposed = true;
    this.revision++;
    void vscode.commands.executeCommand('crabcode.computerUse.disposeLocal', this.hostId).then(undefined, () => {});
  }
}
