import { describe, expect, it } from "vitest";
import { isInsecureRemoteUrl, isLoopbackUrl, normalizeBaseUrl } from "./native";

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
