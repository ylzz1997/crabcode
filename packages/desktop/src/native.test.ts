import { describe, expect, it } from "vitest";
import { isInsecureRemoteUrl, isLoopbackUrl, normalizeBaseUrl, normalizeSettings } from "./native";
import { BUILTIN_THEMES, resolveActiveTheme } from "./theme";
import type { DesktopSettings } from "./types";

describe("Gateway URL handling", () => {
  it("normalizes HTTP base URLs", () => {
    expect(normalizeBaseUrl("https://example.com:4096")).toBe("https://example.com:4096/");
    expect(normalizeBaseUrl("http://localhost:4096/base/")).toBe("http://localhost:4096/base/");
  });

  it("rejects WebSocket URLs as persisted base URLs", () => {
    expect(() => normalizeBaseUrl("ws://localhost:4096/ws")).toThrow();
  });

  it("distinguishes loopback and insecure remote URLs", () => {
    expect(isLoopbackUrl("http://127.0.0.1:4096")).toBe(true);
    expect(isLoopbackUrl("http://localhost:4096")).toBe(true);
    expect(isLoopbackUrl("http://0.0.0.0:4096")).toBe(false);
    expect(isLoopbackUrl("http://192.0.2.1:4096")).toBe(false);
    expect(isInsecureRemoteUrl("http://192.0.2.1:4096")).toBe(true);
    expect(isInsecureRemoteUrl("https://192.0.2.1:4096")).toBe(false);
  });
});

describe("desktop settings migration", () => {
  it("upgrades path-only projects to project ids and directory lists", () => {
    const legacy = {
      schema_version: 1,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [{
        id: "local",
        name: "Local",
        base_url: "http://127.0.0.1:4096",
        credential_ref: null,
        allow_insecure_remote: false,
        projects: [{ path: "/work/crab", name: "Crab", last_session_id: null }],
        last_project_path: "/work/crab",
      }],
      python_path: null,
      sidebar_width: 280,
    } as unknown as DesktopSettings;

    const migrated = normalizeSettings(legacy);

    expect(migrated.schema_version).toBe(4);
    expect(migrated.theme_mode).toBe("system");
    expect(migrated.dock_icon).toBe("dark");
    expect(migrated).toMatchObject({
      active_theme_id: "builtin.crab",
      custom_theme_presets: [],
      pointer_cursor: true,
      ui_font_size: 14,
      code_font_size: 12,
      diff_marker_style: "color",
      font_smoothing: true,
      show_turn_duration: true,
      turn_duration_format: "hms",
      composer_send_key: "enter",
      file_upload_mode: "content",
      file_upload_max_size_mb: 5,
      project_files_width: 640,
      project_files_max_tabs: 5,
      document_agent_width: 400,
      document_agent_collapsed: false,
      document_show_original_text: false,
      document_translation_concurrency: 3,
      document_translation_batch_size: 200,
    });
    expect(migrated.connections[0].projects[0]).toMatchObject({
      kind: "project",
      id: "/work/crab",
      path: "/work/crab",
      directories: ["/work/crab"],
      is_default: true,
      favorite_session_ids: [],
    });
    expect(migrated.connections[0].favorite_items).toEqual([]);
    expect(migrated.connections[0].last_model_profile).toBeNull();
    expect(migrated.connections[0].document_workspace_root).toBeNull();
    expect(migrated.connections[0].last_project_id).toBe("/work/crab");
  });

  it("restores a Windows project when the remembered path casing differs", () => {
    const migrated = normalizeSettings({
      schema_version: 4,
      connections: [{
        id: "local",
        name: "Local",
        base_url: "http://127.0.0.1:4096",
        credential_ref: null,
        allow_insecure_remote: false,
        projects: [{
          id: "project-1",
          kind: "project",
          path: "C:\\Work\\Crab",
          name: "Crab",
          directories: ["C:\\Work\\Crab"],
          last_session_id: null,
        }],
        last_project_path: "c:\\work\\crab",
        last_project_id: null,
      }],
    } as unknown as DesktopSettings);

    expect(migrated.connections[0].last_project_id).toBe("project-1");
  });

  it("normalizes the project file workspace settings", () => {
    const hidden = normalizeSettings({
      schema_version: 4,
      connections: [],
      project_files_open: true,
      project_files_width: 120,
      project_files_max_tabs: 0,
    } as unknown as DesktopSettings);
    const oversized = normalizeSettings({
      schema_version: 4,
      connections: [],
      project_files_width: 4_000,
      project_files_max_tabs: 200,
    } as unknown as DesktopSettings);

    expect(hidden.project_files_width).toBe(480);
    expect(hidden.project_files_max_tabs).toBe(1);
    expect("project_files_open" in hidden).toBe(false);
    expect(oversized.project_files_width).toBe(1_000);
    expect(oversized.project_files_max_tabs).toBe(50);
  });

  it("migrates legacy session favorites into the root of the favorite tree", () => {
    const migrated = normalizeSettings({
      schema_version: 2,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [{
        id: "local",
        name: "Local",
        base_url: "http://127.0.0.1:4096",
        credential_ref: null,
        allow_insecure_remote: false,
        projects: [{
          id: "project-1",
          path: "/work/crab",
          name: "Crab",
          directories: ["/work/crab"],
          last_session_id: null,
          favorite_session_ids: ["session-1"],
        }],
        last_project_path: "/work/crab",
        last_project_id: "project-1",
      }],
    } as unknown as DesktopSettings);

    expect(migrated.connections[0].favorite_items).toEqual([{
      id: "favorite:session:project-1:session-1",
      type: "session",
      project_id: "project-1",
      session_id: "session-1",
    }]);
  });

  it("preserves a non-empty remembered model profile", () => {
    const configured = {
      schema_version: 2,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [{
        id: "local",
        name: "Local",
        base_url: "http://127.0.0.1:4096",
        credential_ref: null,
        allow_insecure_remote: false,
        last_model_profile: "fast",
        projects: [],
        last_project_path: null,
        last_project_id: null,
      }],
    } as unknown as DesktopSettings;

    expect(normalizeSettings(configured).connections[0].last_model_profile).toBe("fast");
  });

  it("normalizes per-session conversation preferences", () => {
    const configured = {
      schema_version: 4,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [{
        id: "local",
        name: "Local",
        base_url: "http://127.0.0.1:4096",
        credential_ref: null,
        allow_insecure_remote: false,
        projects: [{
          id: "project-1",
          path: "/work/crab",
          name: "Crab",
          directories: ["/work/crab"],
          last_session_id: "session-1",
          session_preferences: {
            "session-1": {
              model_profile: "fast",
              reasoning_effort: "high",
              ultra_mode: true,
              mode: "plan",
              permission_mode: "ask",
            },
            "session-2": { reasoning_effort: "invalid", mode: "other" },
          },
        }],
        last_project_path: "/work/crab",
        last_project_id: "project-1",
      }],
    } as unknown as DesktopSettings;

    expect(normalizeSettings(configured).connections[0].projects[0].session_preferences).toEqual({
      "session-1": {
        model_profile: "fast",
        reasoning_effort: "high",
        ultra_mode: true,
        mode: "plan",
        permission_mode: "ask",
      },
    });
  });

  it("normalizes remembered document zoom and scroll positions", () => {
    const configured = {
      schema_version: 3,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [{
        id: "local",
        name: "Local",
        base_url: "http://127.0.0.1:4096",
        credential_ref: null,
        allow_insecure_remote: false,
        projects: [{
          id: "document-1",
          kind: "document",
          path: "/work/paper",
          name: "Paper",
          directories: ["/work/paper"],
          last_session_id: null,
          document_view: { zoom: 9, scroll_top: -20, scroll_left: 180 },
        }],
        last_project_path: "/work/paper",
        last_project_id: "document-1",
      }],
    } as unknown as DesktopSettings;

    expect(normalizeSettings(configured).connections[0].projects[0].document_view).toEqual({
      zoom: 2.5,
      scroll_top: 0,
      scroll_left: 180,
    });
  });

  it("preserves explicit appearance preferences", () => {
    const configured = {
      schema_version: 2,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [],
      python_path: null,
      sidebar_width: 280,
      theme_mode: "dark",
      light_theme: {
        accent_color: "#e75f4b",
        background_color: "#f5f7f6",
        foreground_color: "#172421",
        ui_font_family: "system",
        code_font_family: "system-mono",
        translucent_sidebar: false,
        contrast: 50,
      },
      dark_theme: {
        accent_color: "#33AA88",
        background_color: "#101820",
        foreground_color: "#f0f4f2",
        ui_font_family: "serif",
        code_font_family: "menlo",
        translucent_sidebar: true,
        contrast: 120,
      },
      pointer_cursor: false,
      ui_font_size: 30,
      code_font_size: 2,
      diff_marker_style: "symbols",
      font_smoothing: false,
      show_turn_duration: false,
      turn_duration_format: "seconds",
      composer_send_key: "mod_enter",
      file_upload_mode: "path",
      file_upload_max_size_mb: 250,
      project_files_max_tabs: 200,
      dock_icon: "light",
      document_show_original_text: true,
      document_translation_concurrency: 99,
      document_translation_batch_size: 2,
    } as unknown as DesktopSettings;

    const normalized = normalizeSettings(configured);
    const migratedTheme = resolveActiveTheme(normalized);

    expect(normalized.theme_mode).toBe("dark");
    expect(normalized.dock_icon).toBe("light");
    expect(normalized).toMatchObject({
      active_theme_id: "custom.migrated",
      custom_theme_presets: [{ id: "custom.migrated" }],
      pointer_cursor: false,
      ui_font_size: 18,
      code_font_size: 10,
      diff_marker_style: "symbols",
      font_smoothing: false,
      show_turn_duration: false,
      turn_duration_format: "seconds",
      composer_send_key: "mod_enter",
      file_upload_mode: "path",
      file_upload_max_size_mb: 100,
      project_files_max_tabs: 50,
      document_show_original_text: true,
      document_translation_concurrency: 8,
      document_translation_batch_size: 10,
    });
    expect(migratedTheme).toMatchObject({
      dark: {
        accent_color: "#33aa88",
        background_color: "#101820",
        foreground_color: "#f0f4f2",
        ui_font_family: "serif",
        code_font_family: "menlo",
        translucent_sidebar: true,
        contrast: 100,
      },
    });
  });

  it("maps the legacy night-mode switch to a forced light theme", () => {
    const migrated = normalizeSettings({
      schema_version: 1,
      active_connection_id: "local",
      connection_order: ["local"],
      connections: [],
      python_path: null,
      sidebar_width: 280,
      auto_night_mode: false,
    } as unknown as DesktopSettings);

    expect(migrated.theme_mode).toBe("light");
    expect(resolveActiveTheme(migrated).light.background_color).toBe("#f5f7f6");
  });

  it("drops corrupt or incompatible v4 presets and falls back to Crab default", () => {
    const futureTheme = {
      ...BUILTIN_THEMES[1],
      id: "custom.future",
      minimum_app_version: "99.0.0",
    };
    const normalized = normalizeSettings({
      ...normalizeSettings({
        schema_version: 1,
        active_connection_id: "local",
        connection_order: [],
        connections: [],
        sidebar_width: 280,
      } as unknown as DesktopSettings),
      schema_version: 4,
      active_theme_id: "custom.future",
      custom_theme_presets: [futureTheme, { id: "custom.broken" } as never],
    });

    expect(normalized.active_theme_id).toBe("builtin.crab");
    expect(normalized.custom_theme_presets).toEqual([]);
  });
});
