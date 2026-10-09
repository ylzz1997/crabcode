import { useCallback, useEffect, useState } from "react";
import type { GatewayApi } from "./gateway";
import type { ChangeReviewSelection } from "./ChangeReview";
import type { GitReviewDiff, GitReviewInfo } from "./types";

export function useGitReview(api: GatewayApi, path: string, selection: ChangeReviewSelection | null, active: boolean) {
  const [revision, setRevision] = useState(0);
  const [repository, setRepository] = useState<{ path: string; info: GitReviewInfo | null; error: string | null; loading: boolean }>({ path, info: null, error: null, loading: true });
  const [result, setResult] = useState<{ key: string; data: GitReviewDiff | null; error: string | null; loading: boolean }>({ key: "", data: null, error: null, loading: false });
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const scope = selection?.source === "git" ? selection.scope : null;
  const info = repository.path === path ? repository.info : null;
  const ref = selection?.source === "git"
    ? selection.ref ?? (scope === "branch" ? info?.default_base : scope === "commit" ? info?.head : undefined) ?? undefined
    : undefined;
  const key = JSON.stringify([path, scope, ref]);

  useEffect(() => {
    const controller = new AbortController();
    setRepository((current) => ({ path, info: current.path === path ? current.info : null, error: null, loading: true }));
    void (async () => {
      try {
        const next = await api.gitReviewInfo(path, controller.signal);
        if (!controller.signal.aborted) setRepository({ path, info: next, error: null, loading: false });
      } catch (reason) {
        if (controller.signal.aborted) return;
        const message = reason instanceof Error ? reason.message : String(reason);
        setRepository({ path, info: null, error: /404|^Not Found$/i.test(message) ? "当前 Gateway 尚不支持 Git 查看，请更新并重启 Gateway" : message, loading: false });
      }
    })();
    return () => controller.abort();
  }, [api, path, revision, active, scope]);

  useEffect(() => {
    if (!scope || !active || !info?.available) return;
    const controller = new AbortController();
    setResult({ key, data: null, error: null, loading: true });
    void (async () => {
      try {
        const data = await api.gitReviewDiff(path, scope, ref, controller.signal);
        if (!controller.signal.aborted) setResult({ key, data, error: null, loading: false });
      } catch (reason) {
        if (!controller.signal.aborted) setResult({ key, data: null, error: reason instanceof Error ? reason.message : String(reason), loading: false });
      }
    })();
    return () => controller.abort();
  }, [api, path, scope, ref, key, revision, active, info?.available]);

  useEffect(() => {
    if (!active || !scope) return;
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [active, scope, refresh]);

  return {
    info,
    infoLoading: repository.path !== path || repository.loading,
    unavailable: repository.error ?? info?.reason ?? (repository.loading ? "正在检测 Git…" : "当前目录没有 Git 仓库"),
    data: info?.available && result.key === key ? result.data : null,
    loading: Boolean(scope && (repository.path !== path || repository.loading || (info?.available && (result.key !== key || result.loading)))),
    error: scope ? repository.error ?? (info && !info.available ? info.reason ?? "当前目录没有 Git 仓库" : result.key === key ? result.error : null) : null,
    refresh,
  };
}
