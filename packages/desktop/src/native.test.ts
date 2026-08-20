import { describe, expect, it } from "vitest";
import { isInsecureRemoteUrl, isLoopbackUrl, normalizeBaseUrl, normalizeSettings } from "./native";
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

    expect(migrated.schema_version).toBe(2);
    expect(migrated.connections[0].projects[0]).toMatchObject({
      id: "/work/crab",
      path: "/work/crab",
      directories: ["/work/crab"],
    });
    expect(migrated.connections[0].last_project_id).toBe("/work/crab");
  });
});
