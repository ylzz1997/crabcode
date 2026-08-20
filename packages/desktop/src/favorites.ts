import type {
  ConnectionPreset,
  FavoriteEntry,
  FavoriteFolder,
  GatewayViewState,
  ProjectPreset,
  SessionInfo,
} from "./types";

export type FavoriteViewEntry =
  | { kind: "folder"; entry: FavoriteFolder; children: FavoriteViewEntry[] }
  | { kind: "project"; entry: Extract<FavoriteEntry, { type: "project" }>; project: ProjectPreset }
  | { kind: "session"; entry: Extract<FavoriteEntry, { type: "session" }>; project: ProjectPreset; session: SessionInfo };

export type FavoriteFolderOption = { id: string | null; name: string; depth: number };

function stableFavoriteId(kind: "project" | "session", projectId: string, sessionId = ""): string {
  return ["favorite", kind, projectId, sessionId].filter(Boolean).join(":");
}

export function legacyFavoriteEntries(connection: Pick<ConnectionPreset, "projects">): FavoriteEntry[] {
  return connection.projects.flatMap((project) => (
    (project.favorite_session_ids ?? []).map((sessionId) => ({
      id: stableFavoriteId("session", project.id, sessionId),
      type: "session" as const,
      project_id: project.id,
      session_id: sessionId,
    }))
  ));
}

export function favoriteEntries(connection: ConnectionPreset | null): FavoriteEntry[] {
  if (!connection) return [];
  return Array.isArray(connection.favorite_items)
    ? connection.favorite_items
    : legacyFavoriteEntries(connection);
}

export function normalizeFavoriteEntries(value: unknown): FavoriteEntry[] {
  if (!Array.isArray(value)) return [];
  const seenIds = new Set<string>();
  const normalize = (raw: unknown): FavoriteEntry | null => {
    if (!raw || typeof raw !== "object") return null;
    const item = raw as Record<string, unknown>;
    const id = typeof item.id === "string" && item.id.trim() ? item.id : crypto.randomUUID();
    if (seenIds.has(id)) return null;
    if (item.type === "folder") {
      if (typeof item.name !== "string" || !item.name.trim()) return null;
      seenIds.add(id);
      return {
        id,
        type: "folder",
        name: item.name.trim(),
        children: normalizeFavoriteEntriesWithSeen(item.children, seenIds),
      };
    }
    if (item.type === "project" && typeof item.project_id === "string" && item.project_id) {
      seenIds.add(id);
      return { id, type: "project", project_id: item.project_id };
    }
    if (item.type === "session"
      && typeof item.project_id === "string" && item.project_id
      && typeof item.session_id === "string" && item.session_id) {
      seenIds.add(id);
      return { id, type: "session", project_id: item.project_id, session_id: item.session_id };
    }
    return null;
  };
  return value.flatMap((item) => {
    const normalized = normalize(item);
    return normalized ? [normalized] : [];
  });
}

function normalizeFavoriteEntriesWithSeen(value: unknown, seenIds: Set<string>): FavoriteEntry[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw): FavoriteEntry[] => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Record<string, unknown>;
    const id = typeof item.id === "string" && item.id.trim() ? item.id : crypto.randomUUID();
    if (seenIds.has(id)) return [];
    if (item.type === "folder" && typeof item.name === "string" && item.name.trim()) {
      seenIds.add(id);
      return [{
        id,
        type: "folder",
        name: item.name.trim(),
        children: normalizeFavoriteEntriesWithSeen(item.children, seenIds),
      }];
    }
    if (item.type === "project" && typeof item.project_id === "string" && item.project_id) {
      seenIds.add(id);
      return [{ id, type: "project", project_id: item.project_id }];
    }
    if (item.type === "session"
      && typeof item.project_id === "string" && item.project_id
      && typeof item.session_id === "string" && item.session_id) {
      seenIds.add(id);
      return [{ id, type: "session", project_id: item.project_id, session_id: item.session_id }];
    }
    return [];
  });
}

export function resolveFavoriteEntries(
  connection: ConnectionPreset | null,
  gateway: GatewayViewState | null,
): FavoriteViewEntry[] {
  if (!connection || !gateway) return [];
  const resolve = (entries: FavoriteEntry[]): FavoriteViewEntry[] => entries.flatMap((entry): FavoriteViewEntry[] => {
    if (entry.type === "folder") return [{ kind: "folder", entry, children: resolve(entry.children) }];
    const project = connection.projects.find((item) => item.id === entry.project_id);
    if (!project) return [];
    if (entry.type === "project") return [{ kind: "project", entry, project }];
    const session = (gateway.sessionsByProject[project.path] ?? [])
      .find((item) => item.session_id === entry.session_id);
    return session ? [{ kind: "session", entry, project, session }] : [];
  });
  return resolve(favoriteEntries(connection));
}

export function hasFavoriteProject(entries: FavoriteEntry[], projectId: string): boolean {
  return entries.some((entry) => entry.type === "folder"
    ? hasFavoriteProject(entry.children, projectId)
    : entry.type === "project" && entry.project_id === projectId);
}

export function hasFavoriteSession(entries: FavoriteEntry[], projectId: string, sessionId: string): boolean {
  return entries.some((entry) => entry.type === "folder"
    ? hasFavoriteSession(entry.children, projectId, sessionId)
    : entry.type === "session" && entry.project_id === projectId && entry.session_id === sessionId);
}

export function addFavoriteEntry(
  entries: FavoriteEntry[],
  parentId: string | null,
  entry: FavoriteEntry,
): FavoriteEntry[] {
  if (parentId === null) return [...entries, entry];
  let added = false;
  const visit = (items: FavoriteEntry[]): FavoriteEntry[] => items.map((item) => {
    if (item.type !== "folder") return item;
    if (item.id === parentId) {
      added = true;
      return { ...item, children: [...item.children, entry] };
    }
    const children = visit(item.children);
    return children === item.children ? item : { ...item, children };
  });
  const next = visit(entries);
  return added ? next : entries;
}

export function removeFavoriteEntries(
  entries: FavoriteEntry[],
  predicate: (entry: FavoriteEntry) => boolean,
): FavoriteEntry[] {
  let changed = false;
  const next = entries.flatMap((entry): FavoriteEntry[] => {
    if (predicate(entry)) {
      changed = true;
      return [];
    }
    if (entry.type !== "folder") return [entry];
    const children = removeFavoriteEntries(entry.children, predicate);
    if (children !== entry.children) {
      changed = true;
      return [{ ...entry, children }];
    }
    return [entry];
  });
  return changed ? next : entries;
}

export type FavoriteFolderDeleteMode = "recursive" | "promote" | "root";

export function deleteFavoriteFolder(
  entries: FavoriteEntry[],
  folderId: string,
  mode: FavoriteFolderDeleteMode,
): FavoriteEntry[] {
  const folder = findFavoriteEntry(entries, folderId);
  if (!folder || folder.type !== "folder") return entries;
  if (mode === "recursive") {
    return removeFavoriteEntries(entries, (entry) => entry.id === folderId);
  }
  if (mode === "root") {
    const withoutFolder = removeFavoriteEntries(entries, (entry) => entry.id === folderId);
    return [...withoutFolder, ...folder.children];
  }

  let changed = false;
  const promote = (items: FavoriteEntry[]): FavoriteEntry[] => items.flatMap((entry): FavoriteEntry[] => {
    if (entry.id === folderId && entry.type === "folder") {
      changed = true;
      return entry.children;
    }
    if (entry.type !== "folder") return [entry];
    const children = promote(entry.children);
    return children === entry.children ? [entry] : [{ ...entry, children }];
  });
  const next = promote(entries);
  return changed ? next : entries;
}

export function renameFavoriteFolder(entries: FavoriteEntry[], folderId: string, name: string): FavoriteEntry[] {
  return entries.map((entry) => {
    if (entry.type !== "folder") return entry;
    if (entry.id === folderId) return { ...entry, name };
    const children = renameFavoriteFolder(entry.children, folderId, name);
    return children === entry.children ? entry : { ...entry, children };
  });
}

function findFavoriteEntry(entries: FavoriteEntry[], entryId: string): FavoriteEntry | null {
  for (const entry of entries) {
    if (entry.id === entryId) return entry;
    if (entry.type === "folder") {
      const child = findFavoriteEntry(entry.children, entryId);
      if (child) return child;
    }
  }
  return null;
}

function containsEntry(entry: FavoriteEntry, entryId: string): boolean {
  return entry.id === entryId
    || (entry.type === "folder" && entry.children.some((child) => containsEntry(child, entryId)));
}

export function moveFavoriteEntry(
  entries: FavoriteEntry[],
  entryId: string,
  parentId: string | null,
): FavoriteEntry[] {
  const entry = findFavoriteEntry(entries, entryId);
  if (!entry || entry.id === parentId || (parentId && containsEntry(entry, parentId))) return entries;
  if (parentId) {
    const parent = findFavoriteEntry(entries, parentId);
    if (!parent || parent.type !== "folder") return entries;
  }
  const without = removeFavoriteEntries(entries, (item) => item.id === entryId);
  return addFavoriteEntry(without, parentId, entry);
}

export function favoriteFolderOptions(
  entries: FavoriteEntry[],
  movingEntryId?: string,
): FavoriteFolderOption[] {
  const options: FavoriteFolderOption[] = [{ id: null, name: "收藏根目录", depth: 0 }];
  const moving = movingEntryId ? findFavoriteEntry(entries, movingEntryId) : null;
  const visit = (items: FavoriteEntry[], depth: number) => {
    items.forEach((entry) => {
      if (entry.type !== "folder" || (moving && containsEntry(moving, entry.id))) return;
      options.push({ id: entry.id, name: entry.name, depth });
      visit(entry.children, depth + 1);
    });
  };
  visit(entries, 0);
  return options;
}

export function favoriteParentId(entries: FavoriteEntry[], entryId: string, parentId: string | null = null): string | null {
  for (const entry of entries) {
    if (entry.id === entryId) return parentId;
    if (entry.type === "folder") {
      const found = favoriteParentId(entry.children, entryId, entry.id);
      if (found !== null || entry.children.some((child) => child.id === entryId)) return found;
    }
  }
  return null;
}

export function countFavoriteItems(entries: FavoriteEntry[]): number {
  return entries.reduce((count, entry) => (
    count + (entry.type === "folder" ? countFavoriteItems(entry.children) : 1)
  ), 0);
}

export function favoriteSessionIdsForProject(entries: FavoriteEntry[], projectId: string): string[] {
  const ids: string[] = [];
  const visit = (items: FavoriteEntry[]) => items.forEach((entry) => {
    if (entry.type === "folder") visit(entry.children);
    else if (entry.type === "session" && entry.project_id === projectId) ids.push(entry.session_id);
  });
  visit(entries);
  return [...new Set(ids)];
}
