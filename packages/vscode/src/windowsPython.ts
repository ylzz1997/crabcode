import { execFile } from "child_process";
import { readdirSync, statSync } from "fs";
import { homedir } from "os";
import { win32 as path } from "path";

// Read PEP 514 registrations as UTF-8 JSON so spaces and non-ASCII paths are
// preserved. No registry values are evaluated as commands or PowerShell code.
const REGISTRY_SCRIPT = String.raw`
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$paths = @(foreach ($root in @('Registry::HKEY_CURRENT_USER\Software\Python', 'Registry::HKEY_LOCAL_MACHINE\Software\Python', 'Registry::HKEY_LOCAL_MACHINE\Software\WOW6432Node\Python')) {
  foreach ($company in Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue) {
    if ($company.PSChildName -eq 'PyLauncher') { continue }
    foreach ($tag in Get-ChildItem -LiteralPath $company.PSPath -ErrorAction SilentlyContinue) {
      $install = Get-Item -LiteralPath ($tag.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
      if ($null -eq $install) { continue }
      try {
        if (-not [string]::IsNullOrWhiteSpace([string]$install.GetValue('ExecutableArguments'))) { continue }
        $executable = [string]$install.GetValue('ExecutablePath')
        if (-not $executable -and $company.PSChildName -eq 'PythonCore') {
          $directory = [string]$install.GetValue('')
          if ($directory) { $executable = Join-Path $directory 'python.exe' }
        }
        if ($executable) { $executable }
      } finally { $install.Close() }
    }
  }
})
ConvertTo-Json -InputObject $paths -Compress
`;

function registryCandidates(env: NodeJS.ProcessEnv): Promise<string[]> {
  const powershell = path.join(env.SystemRoot || "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
  return new Promise((resolve) => {
    execFile(powershell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", REGISTRY_SCRIPT], {
      windowsHide: true,
      timeout: 5_000,
      maxBuffer: 1024 * 1024,
      encoding: "utf8",
    }, (error, stdout) => {
      if (error) { resolve([]); return; }
      try {
        const parsed: unknown = JSON.parse(stdout.trim().replace(/^\uFEFF/, ""));
        resolve(Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : []);
      } catch { resolve([]); }
    });
  });
}

/** Extra Windows candidates; the caller still probes version and executability. */
export async function windowsPythonCandidates(env: NodeJS.ProcessEnv = process.env): Promise<string[]> {
  const candidates: string[] = [];
  const seen = new Set<string>();
  const add = (value: string) => {
    if (!path.isAbsolute(value)) return;
    const normalized = path.normalize(value);
    try {
      if (!seen.has(normalized.toLowerCase()) && statSync(normalized).isFile()) {
        seen.add(normalized.toLowerCase());
        candidates.push(normalized);
      }
    } catch { /* Removed or inaccessible installations are optional. */ }
  };
  const scan = (root: string | undefined, prefix = "") => {
    if (!root || !path.isAbsolute(root)) return;
    try {
      const entries = readdirSync(root).filter((name) => name.toLowerCase().startsWith(prefix));
      entries.sort((a, b) => b.localeCompare(a, "en", { numeric: true }));
      for (const entry of entries) add(path.join(root, entry, "python.exe"));
    } catch { /* Missing installation roots are normal. */ }
  };

  for (const candidate of await registryCandidates(env)) add(candidate);
  scan(env.UV_PYTHON_INSTALL_DIR);
  const home = env.USERPROFILE || homedir();
  const roaming = env.APPDATA || path.join(home, "AppData", "Roaming");
  const local = env.LOCALAPPDATA || path.join(home, "AppData", "Local");
  for (const relative of ["uv/python", "uv/data/python"]) scan(path.join(roaming, relative));
  for (const relative of ["Programs/Python", "Python", "uv/python"]) scan(path.join(local, relative));
  for (const root of [env.ProgramW6432, env.ProgramFiles, env["ProgramFiles(x86)"], env.SystemDrive ? `${env.SystemDrive}\\` : undefined]) {
    scan(root, "python");
  }
  for (const root of [home, env.PROGRAMDATA]) {
    if (!root) continue;
    for (const distribution of ["anaconda3", "miniconda3", "miniforge3", "mambaforge"]) {
      add(path.join(root, distribution, "python.exe"));
    }
  }
  return candidates;
}
