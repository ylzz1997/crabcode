import type {
  DesktopSettings,
  ThemePreset,
  ThemeProfile,
  ThemeSemanticColors,
  ThemeVisualAsset,
  ThemeVisualFit,
  ThemeVisualPosition,
  ThemeVisualSlot,
  ThemeVisuals,
} from "./types";
import desktopPackage from "../package.json";

export const DEFAULT_THEME_ID = "builtin.crab";
export const THEME_DOCUMENT_SCHEMA = "io.crabcode.theme/v1";
export const SKIN_DOCUMENT_SCHEMA = "io.crabcode.skin/v1";

export const THEME_VISUAL_SLOTS = [
  "app_background",
  "workspace_background",
  "sidebar_overlay",
  "welcome_character_left",
  "welcome_character_right",
  "composer_frame",
  "top_trim",
  "bottom_trim",
] as const satisfies readonly ThemeVisualSlot[];

const THEME_VISUAL_FITS = ["cover", "contain", "fill", "none"] as const satisfies readonly ThemeVisualFit[];
const THEME_VISUAL_POSITIONS = [
  "center",
  "top",
  "bottom",
  "left",
  "right",
  "left top",
  "left bottom",
  "right top",
  "right bottom",
] as const satisfies readonly ThemeVisualPosition[];

export const THEME_SEMANTIC_COLOR_KEYS = [
  "bg",
  "panel",
  "panel_strong",
  "surface",
  "surface_hover",
  "surface_active",
  "border",
  "border_soft",
  "text",
  "muted",
  "subtle",
  "accent",
  "accent_strong",
  "accent_soft",
  "green",
  "green_soft",
  "orange",
  "orange_soft",
  "red",
  "red_soft",
  "code_bg",
] as const satisfies readonly (keyof ThemeSemanticColors)[];

const PROFILE_KEYS = new Set([
  "accent_color",
  "background_color",
  "foreground_color",
  "ui_font_family",
  "code_font_family",
  "translucent_sidebar",
  "contrast",
  "radius_scale",
  "shadow_strength",
  "token_overrides",
]);
const PRESET_KEYS = new Set([
  "id",
  "name",
  "author",
  "version",
  "description",
  "minimum_app_version",
  "light",
  "dark",
  "preview",
  "visuals",
]);
const VISUAL_KEYS = new Set(["data_url", "opacity", "fit", "position"]);
const MAX_EMBEDDED_IMAGE_BYTES = 8 * 1024 * 1024;
const MAX_EMBEDDED_THEME_BYTES = 20 * 1024 * 1024;

function profile(
  accent: string,
  background: string,
  foreground: string,
  overrides: Partial<ThemeSemanticColors> = {},
): ThemeProfile {
  return {
    accent_color: accent,
    background_color: background,
    foreground_color: foreground,
    ui_font_family: "system",
    code_font_family: "system-mono",
    translucent_sidebar: false,
    contrast: 50,
    radius_scale: 1,
    shadow_strength: 50,
    token_overrides: overrides,
  };
}

export const DEFAULT_LIGHT_THEME: ThemeProfile = profile("#e75f4b", "#f5f7f6", "#172421");
export const DEFAULT_DARK_THEME: ThemeProfile = profile("#ff765f", "#0d1517", "#edf4ef");

export const BUILTIN_THEMES: readonly ThemePreset[] = [
  {
    id: DEFAULT_THEME_ID,
    name: "Crab 默认",
    author: "CrabCode",
    version: "1.0.0",
    description: "Crab Desktop 的珊瑚强调色与青灰工作台。",
    minimum_app_version: "0.1.4",
    light: DEFAULT_LIGHT_THEME,
    dark: DEFAULT_DARK_THEME,
  },
  {
    id: "builtin.graphite",
    name: "石墨",
    author: "CrabCode",
    version: "1.0.0",
    description: "克制的中性灰层级，适合长时间编码。",
    minimum_app_version: "0.1.4",
    light: profile("#56606f", "#f4f4f3", "#202226", {
      panel: "#e9e9e7",
      panel_strong: "#ddddda",
      border: "#c9cac6",
    }),
    dark: profile("#aeb9ca", "#111315", "#edf0f3", {
      panel: "#1a1d20",
      panel_strong: "#24282d",
      surface: "#202429",
      code_bg: "#0d0f11",
    }),
  },
  {
    id: "builtin.deep-sea",
    name: "深海",
    author: "CrabCode",
    version: "1.0.0",
    description: "蓝绿海水色与电光强调色，不含第三方角色素材。",
    minimum_app_version: "0.1.4",
    light: profile("#087f8c", "#eef7f7", "#12363b", {
      panel: "#dceeee",
      panel_strong: "#cce5e6",
      surface: "#f9fdfd",
      border: "#b7d6d8",
      accent_soft: "#d1eff1",
    }),
    dark: profile("#45d4df", "#07191d", "#e6f7f7", {
      panel: "#0c252b",
      panel_strong: "#12323a",
      surface: "#102b32",
      surface_hover: "#173840",
      border: "#28505a",
      code_bg: "#061317",
      accent_soft: "#123f48",
    }),
  },
];

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function exactKeys(value: Record<string, unknown>, allowed: Set<string>, label: string): void {
  const unexpected = Object.keys(value).find((key) => !allowed.has(key));
  if (unexpected) throw new Error(`${label} 包含不支持的字段：${unexpected}`);
}

export function isThemeColor(value: unknown): value is string {
  return typeof value === "string" && /^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/i.test(value);
}

function requiredString(
  value: unknown,
  label: string,
  maximum: number,
  pattern?: RegExp,
): string {
  if (typeof value !== "string" || value.trim() === "" || value.length > maximum || (pattern && !pattern.test(value))) {
    throw new Error(`${label} 无效`);
  }
  return value.trim();
}

function numberInRange(value: unknown, label: string, minimum: number, maximum: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error(`${label} 必须在 ${minimum}–${maximum} 之间`);
  }
  return value;
}

function integerInRange(value: unknown, label: string, minimum: number, maximum: number): number {
  const number = numberInRange(value, label, minimum, maximum);
  if (!Number.isInteger(number)) throw new Error(`${label} 必须是整数`);
  return number;
}

function enumValue<T extends string>(value: unknown, values: readonly T[], label: string): T {
  if (typeof value !== "string" || !values.includes(value as T)) throw new Error(`${label} 无效`);
  return value as T;
}

function parseProfile(value: unknown, label: string): ThemeProfile {
  const source = object(value);
  if (!source) throw new Error(`${label} 必须是对象`);
  exactKeys(source, PROFILE_KEYS, label);
  const overridesSource = object(source.token_overrides);
  if (!overridesSource) throw new Error(`${label}.token_overrides 必须是对象`);
  const allowedTokenKeys = new Set<string>(THEME_SEMANTIC_COLOR_KEYS);
  exactKeys(overridesSource, allowedTokenKeys, `${label}.token_overrides`);
  const tokenOverrides: Partial<ThemeSemanticColors> = {};
  for (const [key, color] of Object.entries(overridesSource)) {
    if (!isThemeColor(color)) throw new Error(`${label}.token_overrides.${key} 必须是十六进制颜色`);
    tokenOverrides[key as keyof ThemeSemanticColors] = color.toLowerCase();
  }
  if (!isThemeColor(source.accent_color)) throw new Error(`${label}.accent_color 无效`);
  if (!isThemeColor(source.background_color)) throw new Error(`${label}.background_color 无效`);
  if (!isThemeColor(source.foreground_color)) throw new Error(`${label}.foreground_color 无效`);
  if (typeof source.translucent_sidebar !== "boolean") throw new Error(`${label}.translucent_sidebar 必须是布尔值`);
  return {
    accent_color: source.accent_color.toLowerCase(),
    background_color: source.background_color.toLowerCase(),
    foreground_color: source.foreground_color.toLowerCase(),
    ui_font_family: enumValue(source.ui_font_family, ["system", "inter", "serif"] as const, `${label}.ui_font_family`),
    code_font_family: enumValue(source.code_font_family, ["system-mono", "menlo", "monaco"] as const, `${label}.code_font_family`),
    translucent_sidebar: source.translucent_sidebar,
    contrast: integerInRange(source.contrast, `${label}.contrast`, 0, 100),
    radius_scale: numberInRange(source.radius_scale, `${label}.radius_scale`, 0.5, 1.75),
    shadow_strength: integerInRange(source.shadow_strength, `${label}.shadow_strength`, 0, 100),
    token_overrides: tokenOverrides,
  };
}

function dataUrlBytes(dataUrl: string): number {
  const comma = dataUrl.indexOf(",");
  if (comma < 0) return Number.POSITIVE_INFINITY;
  const encoded = dataUrl.slice(comma + 1).replace(/=+$/, "");
  return Math.floor(encoded.length * 3 / 4);
}

export function isSafeImageDataUrl(value: unknown, maximumBytes = MAX_EMBEDDED_IMAGE_BYTES): value is string {
  return typeof value === "string"
    && /^data:image\/(?:png|jpeg|webp|gif);base64,[a-z0-9+/]+=*$/i.test(value)
    && dataUrlBytes(value) <= maximumBytes;
}

function parseVisual(value: unknown, label: string): ThemeVisualAsset {
  const source = object(value);
  if (!source) throw new Error(`${label} 必须是对象`);
  exactKeys(source, VISUAL_KEYS, label);
  if (!isSafeImageDataUrl(source.data_url)) throw new Error(`${label}.data_url 不是受支持的图片或超过 8 MiB`);
  return {
    data_url: source.data_url,
    opacity: numberInRange(source.opacity, `${label}.opacity`, 0, 1),
    fit: enumValue(source.fit, THEME_VISUAL_FITS, `${label}.fit`),
    position: enumValue(source.position, THEME_VISUAL_POSITIONS, `${label}.position`),
  };
}

function parseVisuals(value: unknown): ThemeVisuals | undefined {
  if (value === undefined) return undefined;
  const source = object(value);
  if (!source) throw new Error("visuals 必须是对象");
  const allowedSlots = new Set<string>(THEME_VISUAL_SLOTS);
  exactKeys(source, allowedSlots, "visuals");
  const visuals: ThemeVisuals = {};
  for (const [slot, visual] of Object.entries(source)) {
    visuals[slot as ThemeVisualSlot] = parseVisual(visual, `visuals.${slot}`);
  }
  return Object.keys(visuals).length ? visuals : undefined;
}

function parsePreview(value: unknown): ThemePreset["preview"] {
  if (value === undefined) return undefined;
  const source = object(value);
  if (!source) throw new Error("preview 必须是对象");
  exactKeys(source, new Set(["light", "dark"]), "preview");
  const preview: NonNullable<ThemePreset["preview"]> = {};
  if (source.light !== undefined) {
    if (!isSafeImageDataUrl(source.light, 2 * 1024 * 1024)) throw new Error("preview.light 无效或超过 2 MiB");
    preview.light = source.light;
  }
  if (source.dark !== undefined) {
    if (!isSafeImageDataUrl(source.dark, 2 * 1024 * 1024)) throw new Error("preview.dark 无效或超过 2 MiB");
    preview.dark = source.dark;
  }
  return Object.keys(preview).length ? preview : undefined;
}

export function parseThemePreset(value: unknown, options: { allowBuiltinId?: boolean } = {}): ThemePreset {
  const source = object(value);
  if (!source) throw new Error("theme 必须是对象");
  exactKeys(source, PRESET_KEYS, "theme");
  const id = requiredString(source.id, "theme.id", 64, /^[a-z0-9][a-z0-9._-]*$/);
  if (!options.allowBuiltinId && id.startsWith("builtin.")) throw new Error("导入主题不能使用 builtin.* ID");
  const preset: ThemePreset = {
    id,
    name: requiredString(source.name, "theme.name", 80),
    author: requiredString(source.author, "theme.author", 80),
    version: requiredString(source.version, "theme.version", 32, /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/),
    description: typeof source.description === "string" && source.description.length <= 240 ? source.description.trim() : (() => { throw new Error("theme.description 无效"); })(),
    minimum_app_version: requiredString(source.minimum_app_version, "theme.minimum_app_version", 32, /^\d+\.\d+\.\d+$/),
    light: parseProfile(source.light, "theme.light"),
    dark: parseProfile(source.dark, "theme.dark"),
    preview: parsePreview(source.preview),
    visuals: parseVisuals(source.visuals),
  };
  const embeddedBytes = [
    preset.preview?.light,
    preset.preview?.dark,
    ...Object.values(preset.visuals ?? {}).map((visual) => visual.data_url),
  ].filter((item): item is string => typeof item === "string")
    .reduce((total, item) => total + dataUrlBytes(item), 0);
  if (embeddedBytes > MAX_EMBEDDED_THEME_BYTES) throw new Error("主题内嵌图片总计不能超过 20 MiB");
  return preset;
}

export function compareThemeVersions(left: string, right: string): number {
  const parse = (version: string): [number, number, number] => {
    const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version);
    return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : [0, 0, 0];
  };
  const leftParts = parse(left);
  const rightParts = parse(right);
  for (let index = 0; index < 3; index += 1) {
    if (leftParts[index] !== rightParts[index]) return leftParts[index] - rightParts[index];
  }
  return 0;
}

export function isThemeCompatible(theme: ThemePreset, appVersion = desktopPackage.version): boolean {
  return compareThemeVersions(theme.minimum_app_version, appVersion) <= 0;
}

export function normalizeStoredThemePreset(value: unknown): ThemePreset | null {
  try {
    const theme = parseThemePreset(value);
    return isThemeCompatible(theme) ? theme : null;
  } catch {
    return null;
  }
}

function cloneProfile(value: ThemeProfile): ThemeProfile {
  return { ...value, token_overrides: { ...value.token_overrides } };
}

export function cloneThemePreset(value: ThemePreset): ThemePreset {
  return {
    ...value,
    light: cloneProfile(value.light),
    dark: cloneProfile(value.dark),
    preview: value.preview ? { ...value.preview } : undefined,
    visuals: value.visuals
      ? Object.fromEntries(Object.entries(value.visuals).map(([slot, visual]) => [slot, { ...visual }])) as ThemeVisuals
      : undefined,
  };
}

export class ThemeRegistry {
  private readonly themes = new Map<string, ThemePreset>();

  constructor(customThemes: readonly ThemePreset[] = []) {
    for (const preset of BUILTIN_THEMES) this.themes.set(preset.id, preset);
    for (const candidate of customThemes) {
      const preset = normalizeStoredThemePreset(candidate);
      if (preset && !this.themes.has(preset.id)) this.themes.set(preset.id, preset);
    }
  }

  list(): ThemePreset[] {
    return [...this.themes.values()].map(cloneThemePreset);
  }

  get(id: string): ThemePreset | null {
    const theme = this.themes.get(id);
    return theme ? cloneThemePreset(theme) : null;
  }

  isBuiltin(id: string): boolean {
    return BUILTIN_THEMES.some((theme) => theme.id === id);
  }

  resolve(id: string): { theme: ThemePreset; fellBack: boolean } {
    const selected = this.themes.get(id);
    return selected
      ? { theme: cloneThemePreset(selected), fellBack: false }
      : { theme: cloneThemePreset(BUILTIN_THEMES[0]), fellBack: true };
  }
}

export function resolveActiveTheme(settings: Pick<DesktopSettings, "active_theme_id" | "custom_theme_presets">): ThemePreset {
  return new ThemeRegistry(settings.custom_theme_presets).resolve(settings.active_theme_id).theme;
}

function mix(color: string, amount: number, other: string): string {
  return `color-mix(in srgb, ${color} ${amount}%, ${other})`;
}

export interface ResolvedThemeTokens extends ThemeSemanticColors {
  shadow: string;
  radius_sm: string;
  radius_md: string;
  radius_lg: string;
  radius_xl: string;
}

export function resolveThemeTokens(profileValue: ThemeProfile, dark: boolean): ResolvedThemeTokens {
  const background = profileValue.background_color;
  const foreground = profileValue.foreground_color;
  const accent = profileValue.accent_color;
  const derived: ThemeSemanticColors = {
    bg: background,
    panel: mix(background, 94, dark ? "white" : "black"),
    panel_strong: mix(background, 88, dark ? "white" : "black"),
    surface: dark ? mix(background, 90, "white") : mix(background, 35, "white"),
    surface_hover: mix(background, 84, dark ? "white" : "black"),
    surface_active: mix(background, 78, dark ? "white" : "black"),
    border: mix(background, 72, dark ? "white" : "black"),
    border_soft: mix(background, 82, dark ? "white" : "black"),
    text: foreground,
    muted: mix(foreground, 72, background),
    subtle: mix(foreground, 56, background),
    accent,
    accent_strong: mix(accent, dark ? 72 : 78, dark ? "white" : "black"),
    accent_soft: mix(accent, 22, "transparent"),
    green: dark ? "#49c981" : "#14804a",
    green_soft: dark ? "#193d2b" : "#e4f5ea",
    orange: dark ? "#ff9a5f" : "#b04c15",
    orange_soft: dark ? "#4c2d1e" : "#fff0e7",
    red: dark ? "#ff7777" : "#c43333",
    red_soft: dark ? "#4a2525" : "#fdeaea",
    code_bg: mix(background, 78, "black"),
  };
  const radius = profileValue.radius_scale;
  const shadowAlpha = (0.08 + profileValue.shadow_strength * 0.003).toFixed(3);
  return {
    ...derived,
    ...profileValue.token_overrides,
    shadow: `0 18px 50px rgba(0, 0, 0, ${shadowAlpha})`,
    radius_sm: `${Math.round(5 * radius * 10) / 10}px`,
    radius_md: `${Math.round(8 * radius * 10) / 10}px`,
    radius_lg: `${Math.round(12 * radius * 10) / 10}px`,
    radius_xl: `${Math.round(18 * radius * 10) / 10}px`,
  };
}

export function themeProfilesEqual(left: ThemeProfile, right: ThemeProfile): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function legacyThemePreset(light: ThemeProfile, dark: ThemeProfile): ThemePreset {
  return {
    id: "custom.migrated",
    name: "迁移的外观",
    author: "Crab Desktop",
    version: "1.0.0",
    description: "从 Crab Desktop schema v3 的外观设置自动迁移。",
    minimum_app_version: "0.1.4",
    light,
    dark,
  };
}

function customId(prefix = "custom.theme"): string {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}.${suffix.toLowerCase()}`;
}

export function duplicateTheme(
  settings: DesktopSettings,
  sourceId = settings.active_theme_id,
): DesktopSettings {
  const registry = new ThemeRegistry(settings.custom_theme_presets);
  const source = registry.resolve(sourceId).theme;
  const copy: ThemePreset = {
    ...cloneThemePreset(source),
    id: customId(),
    name: `${source.name} 副本`,
    author: "本地用户",
    version: "1.0.0",
    description: `基于 ${source.name} 创建的本地预设。`,
    minimum_app_version: "0.1.4",
  };
  return {
    ...settings,
    active_theme_id: copy.id,
    custom_theme_presets: [...settings.custom_theme_presets, copy],
  };
}

export function updateActiveThemeProfile(
  settings: DesktopSettings,
  scheme: "light" | "dark",
  changes: Partial<ThemeProfile>,
): DesktopSettings {
  let current = settings;
  const registry = new ThemeRegistry(settings.custom_theme_presets);
  if (registry.isBuiltin(settings.active_theme_id)) current = duplicateTheme(settings);
  return {
    ...current,
    custom_theme_presets: current.custom_theme_presets.map((theme) => theme.id === current.active_theme_id
      ? {
          ...theme,
          [scheme]: {
            ...theme[scheme],
            ...changes,
            token_overrides: changes.token_overrides
              ? { ...theme[scheme].token_overrides, ...changes.token_overrides }
              : theme[scheme].token_overrides,
          },
        }
      : theme),
  };
}

export function renameCustomTheme(settings: DesktopSettings, id: string, name: string): DesktopSettings {
  const normalized = name.trim().slice(0, 80);
  if (!normalized || new ThemeRegistry().isBuiltin(id)) return settings;
  return {
    ...settings,
    custom_theme_presets: settings.custom_theme_presets.map((theme) => (
      theme.id === id ? { ...theme, name: normalized } : theme
    )),
  };
}

export function deleteCustomTheme(settings: DesktopSettings, id: string): DesktopSettings {
  if (new ThemeRegistry().isBuiltin(id)) return settings;
  return {
    ...settings,
    active_theme_id: settings.active_theme_id === id ? DEFAULT_THEME_ID : settings.active_theme_id,
    custom_theme_presets: settings.custom_theme_presets.filter((theme) => theme.id !== id),
  };
}

export function addImportedTheme(settings: DesktopSettings, imported: ThemePreset): DesktopSettings {
  const registry = new ThemeRegistry(settings.custom_theme_presets);
  const id = registry.get(imported.id) ? customId("custom.imported") : imported.id;
  const theme = { ...cloneThemePreset(imported), id };
  return {
    ...settings,
    active_theme_id: id,
    custom_theme_presets: [...settings.custom_theme_presets, theme],
  };
}
