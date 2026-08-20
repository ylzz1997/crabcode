import { describe, expect, it } from "vitest";
import {
  addFavoriteEntry,
  countFavoriteItems,
  deleteFavoriteFolder,
  favoriteFolderOptions,
  favoriteParentId,
  hasFavoriteProject,
  hasFavoriteSession,
  moveFavoriteEntry,
  removeFavoriteEntries,
  resolveFavoriteEntries,
} from "./favorites";
import type { ConnectionPreset, FavoriteEntry, GatewayViewState } from "./types";

const project = {
  id: "project-1",
  path: "/work/crab",
  name: "CrabCode",
  directories: ["/work/crab"],
  last_session_id: null,
  favorite_session_ids: [],
};

describe("favorite tree", () => {
  it("supports folders nested to arbitrary levels and moving entries between them", () => {
    let entries: FavoriteEntry[] = [{
      id: "clients",
      type: "folder",
      name: "客户",
      children: [],
    }];
    entries = addFavoriteEntry(entries, "clients", {
      id: "acme",
      type: "folder",
      name: "ACME",
      children: [],
    });
    entries = addFavoriteEntry(entries, null, {
      id: "project-favorite",
      type: "project",
      project_id: project.id,
    });
    entries = moveFavoriteEntry(entries, "project-favorite", "acme");

    expect(favoriteParentId(entries, "project-favorite")).toBe("acme");
    expect(hasFavoriteProject(entries, project.id)).toBe(true);
    expect(countFavoriteItems(entries)).toBe(1);
    expect(favoriteFolderOptions(entries, "clients").map((item) => item.id)).toEqual([null]);
  });

  it("removes matching session references from nested folders", () => {
    const entries: FavoriteEntry[] = [{
      id: "folder",
      type: "folder",
      name: "重要",
      children: [{
        id: "session-favorite",
        type: "session",
        project_id: project.id,
        session_id: "session-1",
      }],
    }];

    expect(hasFavoriteSession(entries, project.id, "session-1")).toBe(true);
    const next = removeFavoriteEntries(entries, (entry) => entry.id === "session-favorite");
    expect(hasFavoriteSession(next, project.id, "session-1")).toBe(false);
  });

  it("deletes a folder recursively or keeps its contents at the parent or root", () => {
    const entries: FavoriteEntry[] = [{
      id: "parent",
      type: "folder",
      name: "客户",
      children: [{
        id: "target",
        type: "folder",
        name: "ACME",
        children: [{
          id: "favorite",
          type: "session",
          project_id: project.id,
          session_id: "session-1",
        }],
      }],
    }];

    const promoted = deleteFavoriteFolder(entries, "target", "promote");
    expect(promoted[0]).toMatchObject({
      id: "parent",
      children: [{ id: "favorite" }],
    });

    const movedToRoot = deleteFavoriteFolder(entries, "target", "root");
    expect(movedToRoot).toMatchObject([
      { id: "parent", children: [] },
      { id: "favorite" },
    ]);

    const recursivelyDeleted = deleteFavoriteFolder(entries, "target", "recursive");
    expect(recursivelyDeleted).toMatchObject([{ id: "parent", children: [] }]);
    expect(hasFavoriteSession(recursivelyDeleted, project.id, "session-1")).toBe(false);
  });

  it("resolves project and session favorites while preserving folder hierarchy", () => {
    const connection = {
      id: "local",
      name: "Local",
      base_url: "http://127.0.0.1:4096",
      credential_ref: null,
      allow_insecure_remote: false,
      projects: [project],
      favorite_items: [{
        id: "folder",
        type: "folder",
        name: "发布",
        children: [
          { id: "project-favorite", type: "project", project_id: project.id },
          { id: "session-favorite", type: "session", project_id: project.id, session_id: "session-1" },
        ],
      }],
      last_project_path: project.path,
      last_project_id: project.id,
    } as ConnectionPreset;
    const gateway = {
      sessionsByProject: {
        [project.path]: [{
          session_id: "session-1",
          message_count: 2,
          model: "",
          provider: "",
          created_at: "2026-08-20T00:00:00Z",
          title: "发布检查",
          cwd: project.path,
          tokens_used: 0,
          preview: "检查状态",
        }],
      },
    } as GatewayViewState;

    const resolved = resolveFavoriteEntries(connection, gateway);
    expect(resolved[0]).toMatchObject({
      kind: "folder",
      entry: { name: "发布" },
      children: [{ kind: "project" }, { kind: "session", session: { title: "发布检查" } }],
    });
  });
});
