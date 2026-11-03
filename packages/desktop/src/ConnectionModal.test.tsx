/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionModal } from "./App";
import { setLocale } from "./i18n";
import { normalizeSettings } from "./native";
import type { DesktopSettings } from "./types";

function changeInput(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("Gateway connection editor", () => {
  let container: HTMLDivElement;
  let root: Root;
  const settings = normalizeSettings({
    active_connection_id: "local",
    connection_order: ["local", "remote"],
    connections: [{
      id: "local", name: "Local", base_url: "http://127.0.0.1:4096",
      credential_ref: "local-secret", projects: [{ path: "/work/local", name: "Local project" }],
    }, {
      id: "remote", name: "Build", base_url: "https://build.example.com:4096",
      credential_ref: "remote-secret", projects: [{ path: "/work/remote", name: "Remote project" }],
    }],
  } as unknown as DesktopSettings);

  beforeEach(() => {
    setLocale("zh-CN");
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(initialEditingId: string | null = null) {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onActivate = vi.fn();
    act(() => root.render(<ConnectionModal settings={settings} activeConnectionId="local"
      initialEditingId={initialEditingId} onClose={vi.fn()} onActivate={onActivate} onSave={onSave} />));
    return { onSave, onActivate };
  }

  it("keeps Local selectable without an edit action and ignores attempts to open its editor", () => {
    const { onActivate } = render("local");
    const rows = container.querySelectorAll(".saved-connection-row");
    expect(rows[0].querySelector('[title="编辑连接"]')).toBeNull();
    expect(rows[1].querySelector('[title="编辑连接"]')).not.toBeNull();
    expect(container.querySelector(".modal-divider")?.textContent).toBe("新建远程连接");
    const fields = container.querySelectorAll<HTMLInputElement>("form input");
    expect(fields[0].value).toBe("");
    expect(fields[1].value).toBe("https://");
    act(() => rows[0].querySelector<HTMLButtonElement>("button")!.click());
    expect(onActivate).toHaveBeenCalledWith("local");
  });

  it("creates a separate remote connection with a labeled HTTP opt-in and no Local metadata", async () => {
    const { onSave } = render("local");
    const fields = container.querySelectorAll<HTMLInputElement>("form input");
    act(() => {
      changeInput(fields[0], "1111");
      changeInput(fields[1], "http://192.0.2.60:4096");
    });
    const warning = container.querySelector<HTMLLabelElement>(".warning-check")!;
    expect(warning.textContent).toBe("允许通过未加密连接传输凭据");
    const checkbox = warning.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(checkbox.checked).toBe(false);
    act(() => checkbox.click());
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(onSave).toHaveBeenCalledOnce();
    const [saved] = onSave.mock.calls[0];
    expect(saved.id).not.toBe("local");
    expect(saved).toMatchObject({ name: "1111", base_url: "http://192.0.2.60:4096",
      allow_insecure_remote: true, credential_ref: null, projects: [], favorite_items: [] });
    expect(settings.connections[0].name).toBe("Local");
    expect(settings.connections[0].base_url).toBe("http://127.0.0.1:4096");
  });

  it("still edits remote connections while preserving their projects and stored credentials", async () => {
    const { onSave } = render("remote");
    const name = container.querySelector<HTMLInputElement>("form input")!;
    act(() => changeInput(name, "Updated build server"));
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      id: "remote", name: "Updated build server", credential_ref: "remote-secret",
      projects: settings.connections[1].projects,
    }), "");
  });
});
