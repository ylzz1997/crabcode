import { t as tr } from "./i18n";
import { useCallback, useRef, useState } from "react";
import { getLumeInstallStatus, installLume, type LumeInstallProgress, type LumeInstallStatus } from "./native";

// Owned by App so navigating away from settings does not lose install progress.
export function useLumeInstaller(onInstalled?: () => void) {
  const [status, setStatus] = useState<LumeInstallStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [progress, setProgress] = useState<LumeInstallProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const installing = useRef(false);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    if (installing.current) return;
    const current = ++generation.current;
    setChecking(true);
    try {
      const result = await getLumeInstallStatus();
      if (current === generation.current) { setStatus(result); setError(null); }
    } catch (reason) {
      if (current === generation.current) setError(String(reason));
    } finally {
      if (current === generation.current) setChecking(false);
    }
  }, []);

  async function install() {
    if (installing.current) return;
    installing.current = true;
    generation.current++;
    setChecking(false); setBusy(true); setError(null);
    setProgress({ operationId: "pending", stage: "preparing", detail: tr("正在准备安装 Lume"), percent: 3 });
    try {
      const result = await installLume(next => setProgress({ ...next, percent: Math.max(0, Math.min(100, next.percent)) }));
      if (!result.available) throw new Error(result.reason || tr("Lume 安装后验证失败"));
      setStatus(result);
      onInstalled?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      installing.current = false;
      setBusy(false); setProgress(null);
    }
  }

  return { status, busy, checking, progress, error, refresh, install };
}

export type LumeInstallerState = ReturnType<typeof useLumeInstaller>;
