import {
  LOCAL_SETTINGS,
  SECTIONS,
  type LocalScope,
  type Section,
} from "./catalog";
import type {
  Resource,
  SettingsEvent,
  SettingsRequest,
  SettingsSnapshot,
} from "./protocol";
import type {
  ModelSettingsResponse,
  ModelSettingsSource,
  PromptSettingsResponse,
  RuntimeSettingsResponse,
  UsageDailyResponse,
} from "../client/types";
import "./webview.css";

declare function acquireVsCodeApi(): {
  postMessage(message: unknown): void;
  getState(): { section?: Section } | undefined;
  setState(state: unknown): void;
};
const vscode = acquireVsCodeApi();
const app = document.getElementById("app")!;
let snapshot: SettingsSnapshot;
let serial = 0;
let active: Section = vscode.getState()?.section ?? "general";
let scope: LocalScope = "user";
const pending = new Map<
  number,
  { resolve: (data: any) => void; reject: (error: Error) => void }
>();
const pages = new Map<Section, HTMLElement>();
const data: Partial<Record<Resource, any>> = {};
const loading = new Set<Resource>();
const dirty = new Set<Section>();
const promptDrafts = new Map<
  string,
  { name: string; sections: Record<string, string> }
>();
let userPromptDraft = "";
const sources: Partial<Record<Resource, string>> = {};
const PATHS: Record<string, string> = {
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M9 3l-1 3-3 1-2 3 2 2-1 3 3 2 3-1 2 2 3-1 1-3 3-1 1-3-2-2 1-3-3-2-3 1z",
  model: "M8 3h8v4H8zM5 7h14v13H5zM9 11v3m6-3v3m-6 3h6M2 10v7m20-7v7",
  shield: "M12 3l8 4v5c0 5-8 9-8 9s-8-4-8-9V7zM8 12l3 3 5-6",
  context: "M4 4h16v16H4zM8 8l4 4-4 4m8-8l-4 4 4 4",
  prompt: "M5 3h10l4 4v14H5zM14 3v5h5M8 12h8m-8 4h6",
  chart: "M4 3v17h17M8 16v-5m5 5V7m5 9V4",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6",
  arrow: "M7 17 17 7M7 7h10v10",
};
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls = "",
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name: string): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", PATHS[name] ?? PATHS.settings);
  svg.append(path);
  return svg;
}
function rpc(
  action: SettingsRequest["action"],
  params: Partial<SettingsRequest> = {},
): Promise<any> {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    vscode.postMessage({
      ...params,
      type: "request",
      action,
      id,
      generation: snapshot?.generation ?? 0,
    });
  });
}
const status = el("div", "status");
status.setAttribute("role", "status");
status.setAttribute("aria-live", "polite");
function notice(message: string, error = false): void {
  status.textContent = message;
  status.classList.toggle("error", error);
}
function button(
  text: string,
  action: () => unknown | Promise<unknown>,
  cls = "button",
): HTMLButtonElement {
  const node = el("button", cls, text);
  node.type = "button";
  node.addEventListener("click", () => {
    node.disabled = true;
    Promise.resolve()
      .then(action)
      .catch((error) => notice(error.message, true))
      .finally(() => {
        node.disabled = false;
      });
  });
  return node;
}
function select(
  options: readonly (readonly [string, string])[],
  value: string,
  label: string,
): HTMLSelectElement {
  const node = el("select");
  node.setAttribute("aria-label", label);
  for (const [key, title] of options) {
    const option = el("option", "", title);
    option.value = key;
    node.append(option);
  }
  node.value = value;
  return node;
}
function input(value: string, label: string, type = "text"): HTMLInputElement {
  const node = el("input");
  node.type = type;
  node.value = value;
  node.setAttribute("aria-label", label);
  return node;
}
function row(
  title: string,
  description: string,
  control: HTMLElement,
  id?: string,
): HTMLElement {
  const node = el("div", "setting-row");
  const copy = el("div", "setting-copy");
  copy.append(el("strong", "", title));
  if (description) copy.append(el("p", "", description));
  const controls = el("div", "setting-controls");
  controls.append(control);
  node.append(copy, controls);
  node.dataset.search = `${title} ${description}`;
  if (id) {
    node.id = id;
    node.tabIndex = -1;
  }
  return node;
}
function group(
  page: HTMLElement,
  title?: string,
  description?: string,
): HTMLElement {
  if (title) page.append(el("h2", "group-title", title));
  if (description) page.append(el("p", "group-description", description));
  const node = el("div", "card");
  page.append(node);
  return node;
}
function actions(...nodes: HTMLElement[]): HTMLElement {
  const node = el("div", "actions");
  node.append(...nodes);
  return node;
}
function panelFor(section: Section): HTMLElement {
  let page = pages.get(section);
  if (!page) {
    page = el("section", "settings-page");
    page.dataset.section = section;
    page.hidden = true;
    pages.set(section, page);
    content.append(page);
  }
  return page;
}
const rail = el("aside", "rail");
const brand = el("div", "brand");
brand.append(icon("settings"), el("span", "", "CrabCode设置"));
rail.append(brand);
const searchBox = el("div", "search-box");
const search = input("", "搜索设置", "search");
search.placeholder = "搜索设置";
searchBox.append(icon("search"), search);
rail.append(searchBox);
const nav = el("nav", "navigation");
nav.setAttribute("aria-label", "设置栏目");
rail.append(nav);
for (const section of SECTIONS) {
  const item = button(section.title, () => navigate(section.id), "nav-item");
  item.prepend(icon(section.icon));
  item.dataset.section = section.id;
  nav.append(item);
}
const extensionLink = button(
  "扩展设置",
  () => rpc("extension"),
  "extension-link",
);
extensionLink.append(icon("arrow"));
rail.append(extensionLink);
const main = el("main", "main");
const heading = el("header", "heading");
const title = el("h1");
const subtitle = el("p");
const headingText = el("div");
headingText.append(title, subtitle);
const refresh = button("刷新", async () => {
  if (
    dirty.has(active) &&
    !(await rpc("confirm", {
      message: "刷新会放弃当前栏目的未保存修改，继续？",
    }))
  )
    return;
  dirty.delete(active);
  if (active === "prompts") {
    promptDrafts.clear();
    userPromptDraft = "";
  }
  const resource = resourceFor(active);
  if (resource) await load(resource, true);
  else {
    panelFor(active)
      .querySelectorAll<HTMLElement>("[data-dirty]")
      .forEach((node) => {
        delete node.dataset.dirty;
      });
    acceptSnapshot(await rpc("initialize"));
  }
});
heading.append(headingText, refresh);
const context = el("div", "context-bar");
const content = el("div", "content");
const searchResults = el("section", "search-results");
searchResults.hidden = true;
main.append(heading, context, searchResults, content);
app.append(rail, main, status);

function acceptSnapshot(next: SettingsSnapshot): void {
  const changed = snapshot && next.generation !== snapshot.generation;
  snapshot = next;
  context.textContent = `${next.gateway}  ·  ${next.cwd ?? "Gateway 默认目录"}`;
  context.title = context.textContent;
  if (changed) {
    pending.forEach((request) =>
      request.reject(new Error("连接或工作区已切换；请重试")),
    );
    pending.clear();
    for (const key of Object.keys(data)) delete data[key as Resource];
    loading.clear();
    pages.forEach((page) => page.remove());
    pages.clear();
    dirty.clear();
    promptDrafts.clear();
    userPromptDraft = "";
    selectedTemplate = "";
    for (const resource of Object.keys(sources))
      delete sources[resource as Resource];
    notice("已切换连接或工作区，重新读取设置。");
    renderLocalPages();
    void navigate(active);
  } else syncLocalControls();
}
function resourceFor(section: Section): Resource | undefined {
  return section === "context" || section === "tools"
    ? "runtime"
    : section === "general"
      ? undefined
      : section;
}
async function navigate(section: Section): Promise<void> {
  if (!SECTIONS.some((item) => item.id === section)) section = "general";
  active = section;
  vscode.setState({ section });
  const definition = SECTIONS.find((item) => item.id === section)!;
  title.textContent = definition.title;
  subtitle.textContent = definition.description;
  search.value = "";
  searchResults.hidden = true;
  content.hidden = false;
  nav.querySelectorAll<HTMLButtonElement>("button").forEach((node) => {
    node.classList.toggle("active", node.dataset.section === section);
    node.setAttribute(
      "aria-current",
      node.dataset.section === section ? "page" : "false",
    );
  });
  panelFor(section);
  pages.forEach((page, id) => {
    page.hidden = id !== section;
  });
  const resource = resourceFor(section);
  if (resource && !data[resource]) await load(resource);
}
async function load(resource: Resource, force = false): Promise<void> {
  if (loading.has(resource) || (!force && data[resource])) return;
  loading.add(resource);
  const generation = snapshot.generation;
  const page = panelFor(
    resource === "runtime"
      ? active === "context"
        ? "context"
        : "tools"
      : resource,
  );
  const busy = el("p", "load-state", "正在读取…");
  page.append(busy);
  try {
    const result = await rpc("read", {
      resource,
      ...(resource === "usage" ? { query: usageQuery } : {}),
    });
    if (snapshot.generation !== generation) return;
    data[resource] = result;
    renderResource(resource);
    notice("");
  } catch (error) {
    if (snapshot.generation === generation) {
      notice((error as Error).message, true);
      const failure = el("div", "load-state error");
      failure.append(
        el("p", "", (error as Error).message),
        button("重试", () => {
          failure.remove();
          return load(resource, true);
        }),
      );
      page.append(failure);
    }
  } finally {
    if (snapshot.generation === generation) loading.delete(resource);
    busy.remove();
  }
}
function renderResource(resource: Resource): void {
  if (resource === "runtime") {
    renderRuntime("tools");
    renderRuntime("context");
  }
  if (resource === "models" && !dirty.has("models")) renderModels();
  if (resource === "prompts" && !dirty.has("prompts")) renderPrompts();
  if (resource === "usage") renderUsage();
}
async function mutate(
  resource: Resource,
  mutation: Record<string, unknown>,
): Promise<any> {
  notice("正在保存…");
  const sections: Section[] =
    resource === "runtime" ? ["tools", "context"] : [resource];
  const result = await lockEditor(sections.map(panelFor), () =>
    rpc("mutate", {
      resource,
      mutation: { source: sources[resource], ...mutation },
    }),
  );
  data[resource] = result;
  notice(
    resource === "models"
      ? "已保存。模型参数在重新选择模型后生效。"
      : resource === "runtime"
        ? "已保存。压缩和操作选项从下一轮生效；快照和额外工具在新建或恢复会话后生效。"
        : "已保存，从下一轮对话生效。",
  );
  return result;
}
function sourcePicker(page: HTMLElement, resource: Resource): boolean {
  const state = data[resource];
  const writable = (state?.editable_sources ?? []).filter(
    (item: ModelSettingsSource) => item.writable,
  ) as ModelSettingsSource[];
  const allWarnings = state?.warnings ?? [];
  for (const warning of allWarnings)
    page.append(el("p", "inline-note", warning));
  if (!writable.length) {
    page.append(el("p", "inline-note", "当前配置层不可写，设置仅供查看。"));
    return false;
  }
  if (!writable.some((item) => item.id === sources[resource]))
    sources[resource] =
      writable.find((item) => item.id === "projectSettings")?.id ??
      writable[0].id;
  const picker = select(
    writable.map((item) => [item.id, item.label]),
    sources[resource]!,
    "保存到配置层",
  );
  picker.dataset.sourceResource = resource;
  picker.addEventListener("change", () => {
    sources[resource] = picker.value;
    document
      .querySelectorAll<HTMLSelectElement>(
        `[data-source-resource="${resource}"]`,
      )
      .forEach((item) => {
        item.value = picker.value;
      });
  });
  const bar = el("div", "source-bar");
  bar.append(el("span", "", "保存到"), picker);
  page.append(bar);
  return true;
}
function localScope(page: HTMLElement): void {
  const picker = select(
    [
      ["user", "VS Code 用户"],
      ...(snapshot.hasWorkspace ? [["workspace", "当前工作区"] as const] : []),
    ],
    scope,
    "聊天偏好保存范围",
  );
  picker.dataset.localScope = "true";
  picker.addEventListener("change", () => {
    scope = picker.value as LocalScope;
    document
      .querySelectorAll<HTMLSelectElement>("[data-local-scope]")
      .forEach((item) => {
        item.value = scope;
      });
    syncLocalControls();
  });
  const bar = el("div", "source-bar");
  bar.append(el("span", "", "聊天偏好保存到"), picker);
  page.append(bar);
}
function localRows(page: HTMLElement, section: Section): void {
  localScope(page);
  const card = group(
    page,
    section === "tools"
      ? "VS Code 会话偏好"
      : section === "models"
        ? "聊天选择"
        : "聊天与输入",
  );
  for (const spec of LOCAL_SETTINGS.filter(
    (item) => item.section === section,
  )) {
    if (spec.key === "computerUseMode" && !snapshot.local[spec.key]?.value)
      continue;
    const current = snapshot.local[spec.key]?.value ?? spec.default;
    const control = spec.options
      ? select(spec.options, String(current), spec.title)
      : spec.array
        ? el("textarea")
        : input(
            String(current),
            spec.title,
            spec.min !== undefined ? "number" : "text",
          );
    if (control instanceof HTMLTextAreaElement) {
      control.rows = 3;
      control.value = (current as string[]).join("\n");
      control.setAttribute("aria-label", spec.title);
    }
    if (control instanceof HTMLInputElement && spec.min !== undefined) {
      control.min = String(spec.min);
      control.max = String(spec.max);
      control.step = "1";
    }
    control.dataset.localKey = spec.key;
    control.addEventListener("input", () => {
      control.dataset.dirty = "true";
      dirty.add(section);
    });
    control.addEventListener("change", async () => {
      const value = spec.array
        ? [
            ...new Set(
              control.value
                .split("\n")
                .map((item) => item.trim())
                .filter(Boolean),
            ),
          ]
        : spec.min !== undefined
          ? control.value.trim()
            ? Number(control.value)
            : NaN
          : control.value;
      control.disabled = true;
      try {
        const next = await rpc("saveLocal", { key: spec.key, value, scope });
        delete control.dataset.dirty;
        updateDirty(section);
        acceptSnapshot(next);
        notice("已保存");
      } catch (error) {
        notice((error as Error).message, true);
      } finally {
        control.disabled = false;
      }
    });
    const reset = button(
      "恢复继承",
      async () => {
        const next = await rpc("saveLocal", {
          key: spec.key,
          scope,
          reset: true,
        });
        delete control.dataset.dirty;
        updateDirty(section);
        acceptSnapshot(next);
        notice("已移除所选范围的覆盖");
      },
      "text-button",
    );
    const origin = el("small", "origin");
    origin.dataset.originKey = spec.key;
    const wrap = el("div", "local-control");
    wrap.append(control, actions(origin, reset));
    card.append(row(spec.title, spec.description, wrap, spec.key));
  }
}
function syncLocalControls(): void {
  if (!snapshot) return;
  for (const control of document.querySelectorAll<
    HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
  >("[data-local-key]")) {
    const key = control.dataset.localKey!;
    const value = snapshot.local[key]?.value;
    if (!control.dataset.dirty)
      control.value = Array.isArray(value)
        ? value.join("\n")
        : String(value ?? "");
  }
  for (const note of document.querySelectorAll<HTMLElement>(
    "[data-origin-key]",
  )) {
    const info = snapshot.local[note.dataset.originKey!];
    note.textContent =
      info?.workspace !== undefined
        ? scope === "user"
          ? "当前由工作区覆盖"
          : "生效来源：工作区"
        : info?.user !== undefined
          ? "生效来源：用户"
          : "使用默认 / 继承配置";
  }
}
function updateDirty(section: Section): void {
  if (!panelFor(section).querySelector('[data-dirty="true"], .model-editor'))
    dirty.delete(section);
}
function renderLocalPages(): void {
  for (const section of ["general", "models", "tools"] as const) {
    const page = panelFor(section);
    page.replaceChildren();
    localRows(page, section);
  }
  syncLocalControls();
}
async function lockEditor<T>(
  editor: HTMLElement | HTMLElement[],
  action: () => Promise<T>,
): Promise<T> {
  const controls = (Array.isArray(editor) ? editor : [editor]).flatMap(
    (container) => [
      ...container.querySelectorAll<
        | HTMLInputElement
        | HTMLSelectElement
        | HTMLTextAreaElement
        | HTMLButtonElement
      >("input,select,textarea,button"),
    ],
  );
  const disabled = controls.map((node) => node.disabled);
  controls.forEach((node) => {
    node.disabled = true;
  });
  try {
    return await action();
  } finally {
    controls.forEach((node, index) => {
      node.disabled = disabled[index];
    });
  }
}

function runtimeControl(
  card: HTMLElement,
  section: Section,
  title: string,
  description: string,
  key: keyof RuntimeSettingsResponse,
  action: string,
  options?: readonly (readonly [string, string])[],
  nullable = false,
): void {
  const state = data.runtime as RuntimeSettingsResponse;
  const current =
    state[key] ??
    (key === "auto_compact_enabled" || key === "snapshot_enabled"
      ? true
      : key === "compact_buffer_tokens"
        ? 20000
        : key === "snapshot_max_size_mb"
          ? 10
          : key === "computer_use_target_scope"
            ? "app_window"
            : key === "computer_use_delivery_policy"
              ? "allow_foreground"
              : null);
  const control =
    typeof current === "boolean"
      ? input("", title, "checkbox")
      : options
        ? select(options, String(current ?? ""), title)
        : input(current == null ? "" : String(current), title, "number");
  if (control instanceof HTMLInputElement && control.type === "checkbox") {
    control.checked = Boolean(current);
    control.className = "switch";
    control.setAttribute("role", "switch");
  }
  if (control instanceof HTMLInputElement && control.type === "number") {
    control.min = key === "compact_buffer_tokens" ? "0" : "1";
    control.step = "1";
    if (nullable) control.placeholder = "自动";
  }
  control.disabled = !(state.editable_sources ?? []).some(
    (item) => item.writable,
  );
  control.dataset.runtimeKey = String(key);
  control.addEventListener("input", () => {
    control.dataset.dirty = "true";
    dirty.add(section);
  });
  control.addEventListener("change", async () => {
    const checkbox =
      control instanceof HTMLInputElement && control.type === "checkbox";
    const value = checkbox
      ? (control as HTMLInputElement).checked
      : options
        ? control.value
        : nullable && !control.value.trim()
          ? null
          : Number(control.value);
    if (
      !checkbox &&
      !options &&
      value !== null &&
      (!control.value.trim() ||
        !Number.isSafeInteger(value) ||
        Number(value) < (key === "compact_buffer_tokens" ? 0 : 1))
    ) {
      notice("请输入有效整数；自动阈值可以留空。", true);
      return;
    }
    control.disabled = true;
    try {
      await mutate("runtime", { action, [key]: value });
      delete control.dataset.dirty;
      updateDirty(section);
      // Reflect the effective value returned by Gateway, including higher-priority overrides.
      const effective = data.runtime[key];
      if (checkbox) (control as HTMLInputElement).checked = Boolean(effective);
      else control.value = effective == null ? "" : String(effective);
      if (effective !== value)
        notice("已保存，但当前生效值由其他配置层覆盖。请检查保存范围。");
    } catch (error) {
      notice((error as Error).message, true);
    } finally {
      control.disabled = false;
    }
  });
  card.append(row(title, description, control, String(key)));
}
function renderRuntime(section: "tools" | "context"): void {
  if (dirty.has(section)) return;
  const page = panelFor(section);
  page.replaceChildren();
  if (section === "tools") localRows(page, section);
  const canEdit = sourcePicker(page, "runtime");
  const runtime = data.runtime as RuntimeSettingsResponse;
  if (section === "context") {
    const card = group(
      page,
      "自动压缩",
      "保存后从下一轮对话生效，正在运行的一轮沿用原设置。",
    );
    runtimeControl(
      card,
      section,
      "自动压缩",
      "接近上下文上限时，整理历史内容并继续当前任务。",
      "auto_compact_enabled",
      "set_compaction",
    );
    runtimeControl(
      card,
      section,
      "压缩预留 token",
      "默认 20,000。实际至少预留模型最大输出额度；设为 0 仍保留输出空间。",
      "compact_buffer_tokens",
      "set_compaction",
    );
    runtimeControl(
      card,
      section,
      "提前触发阈值",
      "已用 token 超过此值时提前压缩；留空使用自动阈值，设置值不能推迟安全阈值。",
      "max_context_length",
      "set_compaction",
      undefined,
      true,
    );
    page.append(
      el(
        "p",
        "inline-note",
        "自动阈值 = 上下文容量 − max（压缩预留 token，模型最大输出 token）",
      ),
    );
    page.append(
      button("编辑压缩提示词 ↗", () => navigate("prompts"), "text-button"),
    );
  } else {
    const computer = group(
      page,
      "Computer Use · Gateway 默认值",
      "实际可用能力由执行端和操作系统决定；上方 VS Code 覆盖优先于这里的默认值。",
    );
    runtimeControl(
      computer,
      section,
      "操作目标",
      "应用窗口需要执行端支持；整个桌面需要允许前台。",
      "computer_use_target_scope",
      "set_computer_use_options",
      [
        ["app_window", "应用窗口"],
        ["desktop", "整个桌面"],
      ],
    );
    runtimeControl(
      computer,
      section,
      "前台权限",
      "控制是否允许操作切换到前台。",
      "computer_use_delivery_policy",
      "set_computer_use_options",
      [
        ["strict_background", "严格后台"],
        ["allow_foreground", "允许前台"],
      ],
    );
    const snapshots = group(
      page,
      "文件快照",
      "快照设置在新建或恢复会话后生效。",
    );
    runtimeControl(
      snapshots,
      section,
      "启用文件快照",
      "记录文件修改，供回退和变更审查使用。",
      "snapshot_enabled",
      "set_snapshot",
    );
    runtimeControl(
      snapshots,
      section,
      "快照最大大小",
      "允许保存快照的单文件大小上限（MB）。",
      "snapshot_max_size_mb",
      "set_snapshot",
    );
    const tools = group(
      page,
      "额外工具",
      "添加 Python 导入路径；新建或恢复会话后加载。",
    );
    for (const tool of runtime.extra_tools ?? []) {
      const remove = button(
        "移除",
        async () => {
          const target = [...(runtime.editable_sources ?? [])]
            .reverse()
            .find(
              (item) =>
                item.writable &&
                runtime.extra_tools_by_source?.[item.id]?.includes(tool),
            );
          if (!target) throw new Error("工具所在配置层不可写");
          if (
            !(await rpc("confirm", {
              message: `从${target.label}移除额外工具「${tool}」？`,
            }))
          )
            return;
          await mutate("runtime", {
            action: "remove_extra_tool",
            source: target.id,
            tool_path: tool,
          });
          renderRuntime("tools");
        },
        "text-button danger",
      );
      remove.disabled = !canEdit;
      tools.append(row(tool, "", remove));
    }
    const toolInput = input("", "额外工具导入路径");
    toolInput.placeholder = "package.module:Tool";
    const add = button("添加", async () => {
      if (!toolInput.value.trim()) throw new Error("请填写工具导入路径");
      await mutate("runtime", {
        action: "add_extra_tool",
        tool_path: toolInput.value.trim(),
      });
      renderRuntime("tools");
    });
    add.disabled = !canEdit;
    toolInput.disabled = !canEdit;
    tools.append(row("添加工具", "", actions(toolInput, add)));
  }
  syncLocalControls();
}

const MODEL_FIELDS: Array<
  [string, string, "text" | "number" | "boolean" | "json", string[]?]
> = [
  ["provider", "Provider", "text"],
  ["model", "模型 ID", "text"],
  ["base_url", "Base URL", "text"],
  ["group", "配置组", "text"],
  ["format", "API 格式", "text", ["anthropic", "openai", "codex"]],
  ["api_key_env", "API Key 环境变量", "text"],
  [
    "reasoning_effort",
    "推理强度",
    "text",
    ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  ],
  ["thinking_enabled", "Thinking", "boolean"],
  ["thinking_budget", "Thinking Budget", "number"],
  ["max_tokens", "最大输出 Token", "number"],
  ["context_window", "上下文窗口", "number"],
  ["timeout", "超时（秒）", "number"],
  ["network_mode", "网络策略", "text", ["inherit", "direct", "proxy"]],
  ["proxy_url", "代理地址", "text"],
  ["request_max_retries", "请求建立重试", "number"],
  ["max_retries", "流中断重连", "number"],
  ["unbounded_connection_retries", "连接失败持续重连", "boolean"],
  ["pass_reasoning_content", "传递推理内容", "boolean"],
  [
    "anthropic_stream_transport",
    "Anthropic 流传输",
    "text",
    ["auto", "sdk", "httpx"],
  ],
  ["prompt_cache_key", "Prompt Cache Key", "text"],
  ["prompt_cache_retention", "Prompt Cache 保留", "text", ["in_memory", "24h"]],
  ["codex_auth_path", "Codex 认证文件", "text"],
  ["azure_endpoint", "Azure Endpoint", "text"],
  ["azure_api_version", "Azure API 版本", "text"],
  ["azure_deployment", "Azure Deployment", "text"],
  ["http_headers", "HTTP Headers", "json"],
  ["extra_body", "Extra Body", "json"],
];
async function removeModel(
  kind: "model" | "group",
  name: string,
  origins: string[],
): Promise<void> {
  if (
    dirty.has("models") &&
    !(await rpc("confirm", {
      message: "删除会刷新模型列表，放弃未保存的模型修改？",
    }))
  )
    return;
  const modelData = data.models as ModelSettingsResponse;
  const target = (modelData.editable_sources ?? []).find(
    (item) =>
      item.id === sources.models &&
      item.writable &&
      origins.includes(item.path),
  );
  if (!target)
    throw new Error("此条目不在当前保存层中，请先选择它所在的配置层");
  if (
    !(await rpc("confirm", {
      message: `从${target.label}删除${kind === "model" ? "模型" : "配置组"}「${name}」？其他配置层可能仍保留同名配置。`,
    }))
  )
    return;
  await mutate("models", {
    action: kind === "model" ? "delete_model" : "delete_group",
    name,
  });
  dirty.delete("models");
  renderModels();
}
async function modelEditor(
  kind: "model" | "group",
  name = "",
  config: Record<string, unknown> = {},
): Promise<void> {
  if (
    dirty.has("models") &&
    !(await rpc("confirm", { message: "放弃当前未保存的模型修改？" }))
  )
    return;
  const page = panelFor("models");
  page.querySelector(".model-editor")?.remove();
  const editor = el("div", "card editor model-editor");
  page.append(editor);
  dirty.add("models");
  editor.append(
    el(
      "h2",
      "",
      `${name ? "编辑" : "新增"}${kind === "model" ? "模型" : "配置组"}`,
    ),
  );
  const targetSource = sources.models;
  editor.append(
    el(
      "p",
      "inline-note",
      `保存到：${(data.models.editable_sources as ModelSettingsSource[]).find((item) => item.id === targetSource)?.label ?? targetSource}。留空的字段恢复继承；高级参数保留原值。`,
    ),
  );
  const nameInput = input(name, "名称");
  nameInput.readOnly = Boolean(name);
  editor.append(row("名称", "", nameInput));
  const fields = new Map<
    string,
    HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
  >();
  const advanced = el("details", "advanced");
  advanced.append(el("summary", "", "高级参数"));
  for (const [key, label, type, choices] of MODEL_FIELDS) {
    if (key === "group" && kind === "group") continue;
    const initial = config[key];
    const control =
      type === "boolean"
        ? select(
            [
              ["", "继承"],
              ["true", "开启"],
              ["false", "关闭"],
            ],
            initial == null ? "" : String(initial),
            label,
          )
        : choices
          ? select(
              [
                ["", "继承"],
                ...choices.map((value) => [value, value] as const),
              ],
              String(initial ?? ""),
              label,
            )
          : type === "json"
            ? el("textarea")
            : input(
                String(initial ?? ""),
                label,
                type === "number" ? "number" : "text",
              );
    if (control instanceof HTMLTextAreaElement) {
      control.value = initial == null ? "" : JSON.stringify(initial, null, 2);
      control.rows = 4;
      control.setAttribute("aria-label", label);
    }
    if (control instanceof HTMLInputElement && type === "number")
      control.step = key === "timeout" ? "any" : "1";
    fields.set(key, control);
    (MODEL_FIELDS.findIndex((item) => item[0] === key) < 11
      ? editor
      : advanced
    ).append(row(label, "", control));
  }
  editor.append(advanced);
  editor.append(
    actions(
      button(
        "保存",
        async () => {
          if (!nameInput.value.trim()) throw new Error("请填写名称");
          const patch: Record<string, unknown> = {};
          const removed: string[] = [];
          for (const [key, , type] of MODEL_FIELDS) {
            const control = fields.get(key);
            if (!control) continue;
            const value = control.value.trim();
            if (!value) {
              if (Object.hasOwn(config, key)) removed.push(key);
              continue;
            }
            const parsed =
              type === "boolean"
                ? value === "true"
                : type === "number"
                  ? Number(value)
                  : type === "json"
                    ? JSON.parse(value)
                    : value;
            if (
              type === "number" &&
              (!Number.isFinite(parsed) || Number(parsed) < 0)
            )
              throw new Error(`${key} 需要非负数`);
            if (
              type === "json" &&
              (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
            )
              throw new Error(`${key} 必须是 JSON 对象`);
            // Only write changed fields: inherited values must not become accidental overrides.
            if (JSON.stringify(parsed) !== JSON.stringify(config[key]))
              patch[key] = withoutRedacted(parsed);
          }
          await lockEditor(editor, () =>
            mutate("models", {
              source: targetSource,
              action: kind === "model" ? "upsert_model" : "upsert_group",
              name: nameInput.value.trim(),
              config: patch,
              remove_fields: removed,
            }),
          );
          dirty.delete("models");
          renderModels();
        },
        "button primary",
      ),
      button(
        "取消",
        () => {
          dirty.delete("models");
          editor.remove();
        },
        "button",
      ),
    ),
  );
  editor.scrollIntoView({ block: "start", behavior: "smooth" });
  nameInput.focus();
}
function withoutRedacted(value: unknown): unknown {
  if (Array.isArray(value))
    return value.filter((item) => item !== "[redacted]").map(withoutRedacted);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .filter(([, item]) => item !== "[redacted]")
        .map(([key, item]) => [key, withoutRedacted(item)]),
    );
  return value;
}
function renderModels(): void {
  const page = panelFor("models");
  page.replaceChildren();
  localRows(page, "models");
  const canEdit = sourcePicker(page, "models");
  const state = data.models as ModelSettingsResponse;
  const catalog = group(
    page,
    "Gateway 模型",
    "修改已有模型参数后，请在聊天中重新选择该模型。",
  );
  const createModel = button("新增模型", () => modelEditor("model"));
  const createGroup = button("新增配置组", () => modelEditor("group"));
  createModel.disabled = createGroup.disabled = !canEdit;
  catalog.append(actions(createModel, createGroup));
  for (const model of state.models ?? []) {
    const controls = actions(
      button(
        "编辑",
        () => modelEditor("model", model.name, model.configured),
        "text-button",
      ),
      button(
        "删除",
        () => removeModel("model", model.name, model.sources ?? []),
        "text-button danger",
      ),
    );
    controls.querySelectorAll("button").forEach((node) => {
      node.disabled = !canEdit;
    });
    catalog.append(
      row(
        model.name + (model.is_default ? " · 默认" : ""),
        [
          model.effective?.provider,
          model.effective?.model,
          model.group ? `配置组：${model.group}` : "",
        ]
          .filter(Boolean)
          .join(" · "),
        controls,
      ),
    );
    const details = el("details", "effective");
    details.append(el("summary", "", "查看最终生效参数与来源"));
    const table = el("table");
    const head = el("tr");
    for (const text of ["参数", "生效值", "来源"])
      head.append(el("th", "", text));
    table.append(head);
    for (const [key, value] of Object.entries(model.effective ?? {})) {
      const tr = el("tr");
      tr.append(
        el("td", "", MODEL_FIELDS.find((item) => item[0] === key)?.[1] ?? key),
        el(
          "td",
          "",
          typeof value === "object" ? JSON.stringify(value) : String(value),
        ),
        el(
          "td",
          "",
          Object.hasOwn(model.configured ?? {}, key)
            ? "模型配置"
            : model.group &&
                Object.hasOwn(state.groups?.[model.group] ?? {}, key)
              ? `继承 ${model.group}`
              : "默认",
        ),
      );
      table.append(tr);
    }
    details.append(
      table,
      el("p", "inline-note", (model.sources ?? []).join("\n")),
    );
    catalog.append(details);
  }
  if (!state.models?.length)
    catalog.append(el("p", "empty", "还没有模型，添加一个开始使用。"));
  const defaultModel = select(
    [
      ["", "不指定"],
      ...(state.models ?? []).map((item) => [item.name, item.name] as const),
    ],
    state.default_model ?? "",
    "Gateway 默认模型",
  );
  defaultModel.disabled = !canEdit;
  defaultModel.addEventListener("change", async () => {
    defaultModel.disabled = true;
    try {
      await mutate("models", {
        action: defaultModel.value
          ? "set_default_model"
          : "clear_default_model",
        name: defaultModel.value || undefined,
      });
      defaultModel.value = data.models.default_model ?? "";
    } catch (error) {
      notice((error as Error).message, true);
    } finally {
      defaultModel.disabled = !canEdit;
    }
  });
  catalog.append(
    row(
      "Gateway 默认模型",
      "用于 Gateway 默认模型配置；上方聊天偏好可单独指定 VS Code 默认选择。",
      defaultModel,
    ),
  );
  const groups = group(page, "配置组");
  for (const [name, config] of Object.entries(state.groups ?? {})) {
    const controls = actions(
      button("编辑", () => modelEditor("group", name, config), "text-button"),
      button(
        "删除",
        () => removeModel("group", name, state.group_sources?.[name] ?? []),
        "text-button danger",
      ),
    );
    controls.querySelectorAll("button").forEach((node) => {
      node.disabled = !canEdit;
    });
    groups.append(
      row(name, `${Object.keys(config).length} 个共享参数`, controls),
    );
  }
  if (!Object.keys(state.groups ?? {}).length)
    groups.append(el("p", "empty", "配置组可以让多个模型共享连接与参数。"));
  syncLocalControls();
}

let selectedTemplate = "";
function renderPrompts(): void {
  const page = panelFor("prompts");
  page.replaceChildren();
  const canEdit = sourcePicker(page, "prompts");
  const state = data.prompts as PromptSettingsResponse;
  const templates = state.templates ?? [];
  const card = group(
    page,
    "提示词模板",
    "系统与压缩提示词的空白字段沿用默认内容，保存模板后会自动启用。",
  );
  const activeTemplate = select(
    [
      ["", "默认模板"],
      ...templates.map((item) => [item.id, item.name] as const),
    ],
    state.active_template_id ?? "",
    "当前启用模板",
  );
  activeTemplate.disabled = !canEdit;
  activeTemplate.addEventListener("change", async () => {
    activeTemplate.disabled = true;
    try {
      await mutate("prompts", {
        action: "set_active_template",
        template_id: activeTemplate.value || null,
      });
      activeTemplate.value = data.prompts.active_template_id ?? "";
    } catch (error) {
      notice((error as Error).message, true);
    } finally {
      activeTemplate.disabled = !canEdit;
    }
  });
  card.append(row("当前启用", "更改从下一轮对话生效。", activeTemplate));
  const edit = async (id: string) => {
    if (
      dirty.has("prompts") &&
      !(await rpc("confirm", { message: "放弃当前模板的未保存修改？" }))
    ) {
      picker.value = selectedTemplate;
      return;
    }
    promptDrafts.delete(selectedTemplate);
    dirty.delete("prompts");
    selectedTemplate = id;
    renderPrompts();
  };
  const picker = select(
    [
      ["", "新建模板"],
      ...templates.map((item) => [item.id, item.name] as const),
    ],
    selectedTemplate,
    "编辑模板",
  );
  picker.addEventListener("change", () => {
    void edit(picker.value).catch((error) => notice(error.message, true));
  });
  card.append(row("编辑模板", "选择已有模板，或从默认内容新建。", picker));
  const chosen = templates.find((item) => item.id === selectedTemplate);
  const savedDraft = promptDrafts.get(selectedTemplate);
  const name = input(savedDraft?.name ?? chosen?.name ?? "", "模板名称");
  name.placeholder = "给模板起个名字";
  name.addEventListener("input", () => dirty.add("prompts"));
  const editor = el("fieldset", "prompt-editor");
  editor.disabled = !canEdit;
  editor.append(row("模板名称", "", name));
  const fields = new Map<string, HTMLTextAreaElement>();
  for (const section of state.sections ?? []) {
    const details = el("details", "prompt-section");
    details.open = section.key === "extra";
    details.append(el("summary", "", section.label));
    if (section.description)
      details.append(el("p", "group-description", section.description));
    const area = el("textarea", "prompt-text");
    area.value =
      savedDraft?.sections[section.key] ??
      chosen?.sections?.[section.key] ??
      "";
    area.placeholder = section.default_text ?? "留空沿用默认";
    area.rows = 9;
    area.setAttribute("aria-label", section.label);
    area.addEventListener("input", () => dirty.add("prompts"));
    fields.set(section.key, area);
    details.append(area);
    if (section.default_text) {
      const defaults = el("details", "default-prompt");
      defaults.append(
        el("summary", "", "查看默认内容"),
        el("pre", "", section.default_text),
      );
      details.append(defaults);
    }
    editor.append(details);
  }
  const draft = () => ({
    ...(chosen ? { id: chosen.id } : {}),
    name: name.value.trim(),
    sections: {
      ...chosen?.sections,
      ...Object.fromEntries(
        [...fields].map(([key, area]) => [key, area.value]),
      ),
    },
  });
  editor.addEventListener("input", () => {
    promptDrafts.set(selectedTemplate, draft());
    dirty.add("prompts");
  });
  editor.append(
    actions(
      button(
        "保存并启用",
        async () => {
          const value = draft();
          if (!value.name) throw new Error("请填写模板名称");
          await lockEditor(card, () =>
            mutate("prompts", {
              action: "save_template",
              template_id: value.id,
              template_name: value.name,
              sections: value.sections,
            }),
          );
          promptDrafts.delete(selectedTemplate);
          selectedTemplate = data.prompts.active_template_id ?? "";
          dirty.delete("prompts");
          renderPrompts();
        },
        "button primary",
      ),
      button("取消修改", () => {
        promptDrafts.delete(selectedTemplate);
        dirty.delete("prompts");
        renderPrompts();
      }),
      button(
        "导出当前",
        async () => {
          const result = await rpc("export", { templates: [draft()] });
          if (!result.cancelled) notice("模板已导出");
        },
        "text-button",
      ),
    ),
  );
  card.append(editor);
  const transfer = actions(
    button("导入模板", async () => {
      if (
        dirty.has("prompts") &&
        !(await rpc("confirm", {
          message: "导入将刷新模板列表，放弃未保存的修改？",
        }))
      )
        return;
      const result = await rpc("import", { source: sources.prompts });
      if (!result.cancelled) {
        promptDrafts.clear();
        dirty.delete("prompts");
        await load("prompts", true);
        notice(`已导入 ${result.imported} 个模板`);
      }
    }),
    button(
      "导出全部",
      async () => {
        if (!templates.length) throw new Error("还没有可导出的模板");
        const result = await rpc("export", { templates });
        if (!result.cancelled) notice("模板已导出");
      },
      "text-button",
    ),
  );
  (transfer.firstElementChild as HTMLButtonElement).disabled = !canEdit;
  if (chosen) {
    const remove = button(
      "删除模板",
      async () => {
        if (
          !(await rpc("confirm", { message: `删除模板「${chosen.name}」？` }))
        )
          return;
        await mutate("prompts", {
          action: "delete_template",
          template_id: chosen.id,
        });
        promptDrafts.delete(selectedTemplate);
        selectedTemplate = "";
        dirty.delete("prompts");
        renderPrompts();
      },
      "text-button danger",
    );
    remove.disabled = !canEdit;
    transfer.append(remove);
  }
  card.append(transfer);
  page.append(
    button("调整自动压缩阈值 ↗", () => navigate("context"), "text-button"),
  );
  const user = group(page, "追加到用户输入", "为每次输入附加已启用的提示。");
  for (const prompt of state.user_prompts ?? []) {
    const enabled = input("", "启用用户提示", "checkbox");
    enabled.className = "switch";
    enabled.checked = prompt.enabled !== false;
    enabled.disabled = !canEdit;
    enabled.addEventListener("change", async () => {
      enabled.disabled = true;
      try {
        await mutate("prompts", {
          action: "set_user_prompt_enabled",
          source: prompt.source,
          prompt_id: prompt.id,
          enabled: enabled.checked,
        });
      } catch (error) {
        notice((error as Error).message, true);
      } finally {
        enabled.disabled = !canEdit;
      }
    });
    const remove = button(
      "删除",
      async () => {
        if (!(await rpc("confirm", { message: "删除这条用户提示？" }))) return;
        await mutate("prompts", {
          action: "delete_user_prompt",
          source: prompt.source,
          prompt_id: prompt.id,
        });
        renderPrompts();
      },
      "text-button danger",
    );
    remove.disabled = !canEdit;
    user.append(row(prompt.text, "", actions(enabled, remove)));
  }
  const promptInput = el("textarea");
  promptInput.rows = 3;
  promptInput.value = userPromptDraft;
  promptInput.placeholder = "例如：回答使用中文，代码注释使用英文。";
  promptInput.setAttribute("aria-label", "新增用户提示");
  promptInput.disabled = !canEdit;
  promptInput.addEventListener("input", () => {
    userPromptDraft = promptInput.value;
  });
  const add = button("添加提示", async () => {
    if (!promptInput.value.trim()) throw new Error("请填写提示内容");
    await mutate("prompts", {
      action: "add_user_prompt",
      prompt_text: promptInput.value.trim(),
    });
    userPromptDraft = "";
    renderPrompts();
  });
  add.disabled = !canEdit;
  user.append(row("新增提示", "", actions(promptInput, add)));
}

function dateString(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
const today = new Date();
const usageQuery = {
  start_date: dateString(new Date(today.getFullYear(), today.getMonth(), 1)),
  end_date: dateString(today),
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  scope: "global",
};
const number = (value: number | null | undefined) =>
  value == null ? "未记录" : value.toLocaleString("zh-CN");
function renderUsage(): void {
  const page = panelFor("usage");
  page.replaceChildren();
  const usage = data.usage as UsageDailyResponse;
  const filters = el("div", "usage-filters");
  const start = input(usageQuery.start_date, "开始日期", "date");
  const end = input(usageQuery.end_date, "结束日期", "date");
  start.max = end.max = dateString(new Date());
  const range = select(
    [
      ["global", "Gateway 全部项目"],
      ...(snapshot.hasWorkspace ? [["project", "当前项目"] as const] : []),
    ],
    usageQuery.scope,
    "统计范围",
  );
  filters.append(
    start,
    el("span", "", "至"),
    end,
    range,
    button("查询", async () => {
      const days = (Date.parse(end.value) - Date.parse(start.value)) / 86400000;
      if (
        !Number.isFinite(days) ||
        days < 0 ||
        days >= 366 ||
        end.value > dateString(new Date())
      )
        throw new Error("请选择不晚于今天的 1–366 天日期范围");
      Object.assign(usageQuery, {
        start_date: start.value,
        end_date: end.value,
        scope: range.value,
      });
      await load("usage", true);
    }),
  );
  page.append(filters);
  const summary = el("div", "usage-summary");
  for (const [label, value] of [
    ["Token 总量", usage.summary.total_tokens],
    ["输入 Token", usage.summary.input_tokens],
    ["输出 Token", usage.summary.output_tokens],
    ["请求数", usage.summary.request_count],
  ] as const) {
    const metric = el("div", "metric");
    metric.append(el("span", "", label), el("strong", "", number(value)));
    summary.append(metric);
  }
  page.append(summary);
  if (
    usage.summary.unknown_requests ||
    usage.summary.missing_requests ||
    usage.summary.partial_requests
  )
    page.append(
      el(
        "p",
        "inline-note",
        `记录可能不完整：未知 ${usage.summary.unknown_requests}，缺失 ${usage.summary.missing_requests}，部分 ${usage.summary.partial_requests}。未记录的用量不会按 0 推断。`,
      ),
    );
  const trend = group(page, "每日用量");
  if (!usage.days.length)
    trend.append(el("p", "empty", "该范围暂无用量记录。"));
  const max = Math.max(1, ...usage.days.map((day) => day.total_tokens ?? 0));
  for (const day of usage.days) {
    const meter = el("progress");
    meter.max = max;
    meter.value = day.total_tokens ?? 0;
    meter.setAttribute(
      "aria-label",
      `${day.date} ${number(day.total_tokens)} Token`,
    );
    const line = el("div", "usage-day");
    line.append(
      el("span", "", day.date),
      meter,
      el("strong", "", number(day.total_tokens)),
    );
    trend.append(line);
  }
  const models = group(page, "按模型统计");
  for (const model of usage.models) {
    const detail = el("details", "model-usage");
    detail.append(
      el("summary", "", `${model.model} · ${number(model.total_tokens)} Token`),
    );
    detail.append(
      el(
        "p",
        "inline-note",
        `${model.provider} / ${model.model_id} · ${number(model.recorded_request_count)} 次已记录请求`,
      ),
    );
    for (const point of model.points)
      detail.append(
        row(
          point.date,
          "",
          el("span", "", `${number(point.total_tokens)} Token`),
        ),
      );
    models.append(detail);
  }
  if (!usage.models.length) models.append(el("p", "empty", "暂无模型用量。"));
  page.append(
    el(
      "p",
      "inline-note",
      `统计时区：${usage.timezone} · 数据来自当前 Gateway，不代表账号账单。`,
    ),
  );
}

const SEARCH_ITEMS: Array<[Section, string, string, string]> = [
  ...LOCAL_SETTINGS.map(
    (item) =>
      [item.section, item.title, item.description, item.key] as [
        Section,
        string,
        string,
        string,
      ],
  ),
  ["context", "自动压缩", "上下文 压缩 compact", "auto_compact_enabled"],
  ["context", "压缩预留 token", "buffer 上下文", "compact_buffer_tokens"],
  ["context", "提前触发阈值", "上下文 上限", "max_context_length"],
  ["tools", "文件快照", "检查点 snapshot 快照大小", "snapshot_enabled"],
  ["tools", "额外工具", "Python 工具 导入路径", ""],
  ["prompts", "提示词模板", "系统 压缩 prompt 模板 导入 导出", ""],
  ["prompts", "追加到用户输入", "用户提示", ""],
  [
    "models",
    "模型与配置组",
    "Provider API 参数 继承 上下文 思考 推理 thinking",
    "",
  ],
  ["usage", "Token 用量", "使用情况 日期 模型 统计", ""],
];
search.addEventListener("input", () => {
  const query = search.value.trim().toLocaleLowerCase();
  searchResults.replaceChildren();
  if (!query) {
    content.hidden = false;
    searchResults.hidden = true;
    return;
  }
  content.hidden = true;
  searchResults.hidden = false;
  for (const [section, label, description, target] of SEARCH_ITEMS.filter(
    (item) => item.join(" ").toLocaleLowerCase().includes(query),
  )) {
    const result = button(
      "",
      async () => {
        await navigate(section);
        const node = target ? document.getElementById(target) : undefined;
        node?.scrollIntoView({ block: "center" });
        node?.focus();
      },
      "search-result",
    );
    result.append(
      el("strong", "", label),
      el(
        "span",
        "",
        `${SECTIONS.find((item) => item.id === section)!.title} · ${description}`,
      ),
    );
    searchResults.append(result);
  }
  if (!searchResults.childElementCount)
    searchResults.append(el("p", "empty", "没有找到匹配的设置。"));
});
window.addEventListener(
  "message",
  ({ data: event }: MessageEvent<SettingsEvent>) => {
    if (event.type === "snapshot") {
      acceptSnapshot(event.data);
      return;
    }
    if (event.type === "navigate") {
      if (snapshot) void navigate(event.section);
      return;
    }
    if (event.type !== "response") return;
    const request = pending.get(event.id);
    if (!request) return;
    pending.delete(event.id);
    if (snapshot && event.generation !== snapshot.generation)
      request.reject(new Error("设置上下文已变化，请重试"));
    else if (event.error) request.reject(new Error(event.error));
    else request.resolve(event.data);
  },
);
void rpc("initialize")
  .then((initial) => {
    acceptSnapshot(initial);
    renderLocalPages();
    return navigate(active);
  })
  .catch((error) => notice(error.message, true));
