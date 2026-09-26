/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";
import { GatewayApi } from "./gateway";
import type { ConnectionPreset } from "./types";

vi.mock("./native", () => ({
  authenticateConnection: vi.fn().mockResolvedValue({ access_token: null, expires_in: 0, mode: "none" }),
  normalizeBaseUrl: (raw: string) => raw,
}));

const connection = { base_url: "http://127.0.0.1:4096" } as ConnectionPreset;

function respondWith(body: unknown, status = 422, contentType = "application/json") {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
    contentType === "application/json" ? JSON.stringify(body) : String(body),
    { status, headers: { "Content-Type": contentType } },
  )));
}

afterEach(() => vi.unstubAllGlobals());

describe("Gateway request errors", () => {
  it("renders FastAPI validation lists instead of [object Object]", async () => {
    respondWith({
      detail: [{
        type: "literal_error",
        loc: ["body", "action"],
        msg: "Input should be 'set_snapshot', 'add_extra_tool' or 'remove_extra_tool'",
        input: "set_computer_use_options",
      }],
    });
    await expect(new GatewayApi(connection).runtimeSettings("/work/crab"))
      .rejects.toThrow("body.action: Input should be 'set_snapshot', 'add_extra_tool' or 'remove_extra_tool'");
  });

  it("keeps string details and falls back to the status text", async () => {
    respondWith({ detail: "运行与工具配置无效：snapshot.max_size_mb: 超出范围" });
    await expect(new GatewayApi(connection).runtimeSettings("/work/crab"))
      .rejects.toThrow("运行与工具配置无效：snapshot.max_size_mb: 超出范围");

    respondWith("not json", 500, "text/plain");
    await expect(new GatewayApi(connection).runtimeSettings("/work/crab"))
      .rejects.toThrow(/^500\b/);
  });
});
