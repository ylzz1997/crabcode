/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PromptSettingsPanel } from "./PromptSettingsPanel";
import type { GatewayViewState, PromptSettingsMutation, PromptSettingsResponse } from "./types";

const { savePromptExport } = vi.hoisted(() => ({
  savePromptExport: vi.fn(async (_filename: string, _bytes: Uint8Array) => "/tmp/客服.json"),
}));

vi.mock("./native", () => ({
  savePromptExport,
}));

const gateway: GatewayViewState = {
  status: "online",
  error: null,
  token: null,
  tokenExpiresAt: 0,
  workspace: null,
  sessionsByProject: {},
  models: [],
  runningCount: 0,
  pendingCount: 0,
};

const data: PromptSettingsResponse = {
  cwd: "/work/crabcode",
  active_template_id: null,
  templates: [{
    id: "care",
    name: "客服",
    sections: { intro: "custom identity" },
    source: "userSettings",
  }],
  user_prompts: [{
    id: "p1",
    text: "用中文回答",
    enabled: false,
    source: "userSettings",
  }],
  sections: [
    { key: "intro", label: "介绍", default_text: "You are CrabCode." },
    { key: "compact_prompt", label: "上下文压缩提示词", default_text: "Create a durable checkpoint.", description: "用于手动和自动压缩，随模版生效。" },
    { key: "extra", label: "额外段落" },
  ],
  warnings: [],
  editable_sources: [{
    id: "userSettings",
    label: "用户配置",
    path: "/home/settings.json",
    exists: true,
    writable: true,
  }],
};

function setValue(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = Object.getPrototypeOf(element) as object;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set
    ?? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("PromptSettingsPanel", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    savePromptExport.mockClear();
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("starts on the default template and saves a named template", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={data}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });

    const options = Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="使用的提示词模版"] [role="option"]'));
    expect(options[0]?.textContent).toContain("默认");
    expect(options[0]?.getAttribute("aria-selected")).toBe("true");
    expect(options.map((option) => option.textContent ?? "")).toEqual(expect.arrayContaining([expect.stringContaining("客服")]));
    expect(container.querySelector<HTMLTextAreaElement>('[aria-label="介绍"]')?.placeholder).toBe("You are CrabCode.");
    expect(container.querySelector<HTMLTextAreaElement>('[aria-label="介绍"]')?.value).toBe("");
    expect(container.querySelector<HTMLTextAreaElement>('[aria-label="额外段落"]')?.placeholder).toBe("留空则不追加额外段落");
    expect(container.textContent).not.toContain("填入内置默认");
    expect(container.textContent).not.toContain("恢复默认");

    const name = container.querySelector<HTMLInputElement>('[aria-label="模版名称"]')!;
    const intro = container.querySelector<HTMLTextAreaElement>('[aria-label="介绍"]')!;
    await act(async () => {
      setValue(name, "客服副本");
      setValue(intro, "先给出结论");
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prompt-settings-actions .primary")!.click();
    });

    expect(onMutate).toHaveBeenCalledWith(expect.objectContaining({
      action: "save_template",
      source: "userSettings",
      template_name: "客服副本",
      sections: expect.objectContaining({ intro: "先给出结论" }),
    }));
    expect(onMutate.mock.calls.map((call) => call[0].template_id)).toEqual([undefined]);
  });

  it("checks a saved user prompt to append it", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={data}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });

    const checkbox = container.querySelector<HTMLInputElement>('[aria-label="追加提示：用中文回答"]')!;
    expect(checkbox.checked).toBe(false);
    await act(async () => checkbox.click());

    expect(onMutate).toHaveBeenCalledWith(expect.objectContaining({
      action: "set_user_prompt_enabled",
      source: "userSettings",
      prompt_id: "p1",
      enabled: true,
    }));
  });

  it("loads, edits, exports and restores the compaction prompt in the selected template", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={{ ...data, active_template_id: "care", templates: [{
            ...data.templates[0], sections: { intro: "custom identity", compact_prompt: "Previous checkpoint rules" },
          }] }}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });
    const area = container.querySelector<HTMLTextAreaElement>('[aria-label="上下文压缩提示词"]')!;
    expect(area.value).toBe("Previous checkpoint rules");
    expect(container.textContent).toContain("用于手动和自动压缩，随模版生效。");

    await act(async () => setValue(area, "保留目标、决策和未完成工作。"));
    await act(async () => container.querySelector<HTMLButtonElement>(".prompt-settings-actions .primary")!.click());
    expect(onMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      action: "save_template", template_id: "care",
      sections: expect.objectContaining({ compact_prompt: "保留目标、决策和未完成工作。" }),
    }));

    const exportButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "导出 JSON")!;
    await act(async () => exportButton.click());
    expect(JSON.parse(new TextDecoder().decode(savePromptExport.mock.calls[0][1])).sections.compact_prompt)
      .toBe("保留目标、决策和未完成工作。");

    await act(async () => setValue(area, ""));
    expect(area.value).toBe("");
    expect(area.placeholder).toBe("Create a durable checkpoint.");
    await act(async () => container.querySelector<HTMLButtonElement>(".prompt-settings-actions .primary")!.click());
    expect(onMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      action: "save_template", template_id: "care", sections: expect.objectContaining({ compact_prompt: "" }),
    }));
  });

  it("deletes a template and a user prompt from the list", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={data}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });

    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="删除模版 客服"]')!.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="移除提示：用中文回答"]')!.click();
    });

    expect(onMutate).toHaveBeenNthCalledWith(1, expect.objectContaining({
      action: "delete_template",
      source: "userSettings",
      template_id: "care",
    }));
    expect(onMutate).toHaveBeenNthCalledWith(2, expect.objectContaining({
      action: "delete_user_prompt",
      source: "userSettings",
      prompt_id: "p1",
    }));
  });

  it("filters templates from the search box", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={data}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });

    const search = container.querySelector<HTMLInputElement>('[aria-label="搜索提示词模版"]')!;
    await act(async () => setValue(search, "客服"));
    const options = Array.from(container.querySelectorAll('[aria-label="使用的提示词模版"] [role="option"]'));
    expect(options.map((option) => option.textContent ?? "")).toEqual([expect.stringContaining("客服")]);
  });

  it("exports the current template and every saved template as JSON", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={data}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });

    const click = async (label: string) => {
      const button = Array.from(container.querySelectorAll("button")).find((item) => item.textContent?.includes(label));
      await act(async () => button?.click());
    };
    await click("客服");
    await click("导出 JSON");

    const current = JSON.parse(new TextDecoder().decode(savePromptExport.mock.calls[0][1])) as {
      id: string;
      name: string;
      sections: Record<string, string>;
    };
    expect(savePromptExport.mock.calls[0][0]).toBe("客服.json");
    expect(current).toEqual({ id: "care", name: "客服", sections: { intro: "custom identity" } });
    expect(container.textContent).toContain("已导出到 /tmp/客服.json");

    await click("导出全部");
    const all = JSON.parse(new TextDecoder().decode(savePromptExport.mock.calls[1][1])) as {
      templates: Array<{ id: string; name: string }>;
    };
    expect(savePromptExport.mock.calls[1][0]).toBe("prompt-templates.json");
    expect(all.templates).toEqual([{ id: "care", name: "客服", sections: { intro: "custom identity" } }]);
  });

  it("imports a template object and a template list from JSON", async () => {
    const onMutate = vi.fn(async (_mutation: PromptSettingsMutation) => undefined);
    await act(async () => {
      root.render(
        <PromptSettingsPanel
          activeConnection={null}
          activeProject={null}
          gateway={gateway}
          data={data}
          loading={false}
          error={null}
          onRefresh={() => undefined}
          onMutate={onMutate}
        />,
      );
    });

    const input = container.querySelector<HTMLInputElement>('[aria-label="导入提示词模版 JSON"]')!;
    const choose = async (contents: string, filename: string) => {
      const file = new File([contents], filename, { type: "application/json" });
      await act(async () => {
        Object.defineProperty(input, "files", { configurable: true, value: [file] });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    };

    await choose(JSON.stringify({ name: "值班", sections: { intro: "先结论", extra: "  " } }), "值班.json");
    expect(onMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      action: "save_template",
      source: "userSettings",
      template_name: "值班",
      sections: { intro: "先结论" },
    }));
    expect(onMutate.mock.calls.at(-1)?.[0].template_id).toBeUndefined();
    expect(container.textContent).toContain("已导入 值班");

    await choose(JSON.stringify({
      templates: [
        { id: "care", name: "客服", sections: { intro: "更新" } },
        { name: "夜班", sections: { system: "简短" } },
      ],
    }), "prompt-templates.json");
    expect(onMutate).toHaveBeenCalledWith(expect.objectContaining({
      action: "save_template",
      template_id: "care",
      template_name: "客服",
      sections: { intro: "更新" },
    }));
    expect(onMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      action: "save_template",
      template_name: "夜班",
      sections: { system: "简短" },
    }));
    expect(container.textContent).toContain("已导入 2 个模版");
  });
});
