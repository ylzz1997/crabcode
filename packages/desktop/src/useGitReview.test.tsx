/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { useGitReview } from "./useGitReview";
import type { GatewayApi } from "./gateway";
import type { GitReviewDiff, GitReviewInfo, GitReviewScope } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const info: GitReviewInfo = { available: true, reason: null, root: "/repo", head: "abc", branch: "main", default_base: "main", branches: ["main"], commits: [] };
const diff = (path: string): GitReviewDiff => ({ files: [{ path, action: "modify", added: 1, removed: 0, diff: "+new", note: null }], added: 1, removed: 0, total_files: 1, truncated: false, base: null, target: null });

it("aborts stale scopes, hides old content, and discards late results", async () => {
  const requests: { resolve: (data: GitReviewDiff) => void; signal: AbortSignal }[] = [];
  const api = { gitReviewInfo: vi.fn().mockResolvedValue(info), gitReviewDiff: vi.fn((_path, _scope, _ref, signal) =>
    new Promise<GitReviewDiff>((resolve) => requests.push({ resolve, signal }))) } as unknown as GatewayApi;
  const container = document.createElement("div"); const root = createRoot(container);
  let value!: ReturnType<typeof useGitReview>;
  function Probe({ scope }: { scope: GitReviewScope }) {
    value = useGitReview(api, "/repo", { source: "git", scope, path: null, showAll: true }, true);
    return null;
  }
  await act(async () => root.render(<Probe scope="staged" />));
  expect(requests).toHaveLength(1);
  await act(async () => root.render(<Probe scope="unstaged" />));
  expect(requests[0].signal.aborted).toBe(true);
  expect(value.data).toBeNull(); expect(value.loading).toBe(true);
  await act(async () => requests[1].resolve(diff("working.txt")));
  expect(value.data?.files[0].path).toBe("working.txt");
  await act(async () => requests[0].resolve(diff("stale.txt")));
  expect(value.data?.files[0].path).toBe("working.txt");
  act(() => root.unmount());
});

it("clears existing diffs when a refreshed folder is no longer a repository", async () => {
  const gitReviewInfo = vi.fn().mockResolvedValue(info);
  const api = { gitReviewInfo, gitReviewDiff: vi.fn().mockResolvedValue(diff("old.txt")) } as unknown as GatewayApi;
  const container = document.createElement("div"); const root = createRoot(container);
  let value!: ReturnType<typeof useGitReview>;
  function Probe() { value = useGitReview(api, "/repo", { source: "git", scope: "uncommitted", path: null, showAll: true }, true); return null; }
  await act(async () => root.render(<Probe />));
  expect(value.data?.files[0].path).toBe("old.txt");
  gitReviewInfo.mockResolvedValue({ ...info, available: false, reason: "当前目录不在 Git 仓库中" });
  await act(async () => value.refresh());
  expect(value.data).toBeNull(); expect(value.loading).toBe(false);
  expect(value.error).toBe("当前目录不在 Git 仓库中");
  act(() => root.unmount());
});
