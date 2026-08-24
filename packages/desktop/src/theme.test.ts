import { describe, expect, it } from "vitest";
import { normalizeSettings } from "./native";
import {
  BUILTIN_THEMES,
  DEFAULT_THEME_ID,
  ThemeRegistry,
  deleteCustomTheme,
  duplicateTheme,
  normalizeStoredThemePreset,
  resolveActiveTheme,
  resolveThemeTokens,
  updateActiveThemeProfile,
} from "./theme";
import type { DesktopSettings, ThemePreset } from "./types";

function settings(): DesktopSettings {
  return normalizeSettings({
    schema_version: 1,
    active_connection_id: "local",
    connection_order: [],
    connections: [],
    sidebar_width: 280,
  } as unknown as DesktopSettings);
}

describe("ThemeRegistry", () => {
  it("ships multiple dual-mode presets and falls back to Crab default", () => {
    const registry = new ThemeRegistry();

    expect(registry.list().map((theme) => theme.id)).toEqual([
      "builtin.crab",
      "builtin.graphite",
      "builtin.deep-sea",
    ]);
    expect(registry.resolve("missing.theme")).toMatchObject({
      fellBack: true,
      theme: { id: DEFAULT_THEME_ID, light: {}, dark: {} },
    });
  });

  it("does not allow a custom preset to replace a built-in id", () => {
    const spoof = { ...BUILTIN_THEMES[1], id: DEFAULT_THEME_ID, name: "Spoof" };
    const registry = new ThemeRegistry([spoof]);

    expect(registry.resolve(DEFAULT_THEME_ID).theme.name).toBe("Crab 默认");
  });

  it("rejects corrupt and version-incompatible stored themes", () => {
    const corrupt = { ...BUILTIN_THEMES[1], id: "custom.corrupt", light: { nope: true } };
    const incompatible = {
      ...BUILTIN_THEMES[1],
      id: "custom.future",
      minimum_app_version: "99.0.0",
    };

    expect(normalizeStoredThemePreset(corrupt)).toBeNull();
    expect(normalizeStoredThemePreset(incompatible)).toBeNull();
  });
});

describe("theme preset editing", () => {
  it("duplicates a built-in before editing it", () => {
    const edited = updateActiveThemeProfile(settings(), "dark", { accent_color: "#123456" });
    const theme = resolveActiveTheme(edited);

    expect(edited.active_theme_id).toMatch(/^custom\.theme\./);
    expect(edited.custom_theme_presets).toHaveLength(1);
    expect(theme.name).toBe("Crab 默认 副本");
    expect(theme.dark.accent_color).toBe("#123456");
    expect(BUILTIN_THEMES[0].dark.accent_color).toBe("#ff765f");
  });

  it("deletes a selected custom theme and restores the default", () => {
    const duplicated = duplicateTheme(settings());
    const deleted = deleteCustomTheme(duplicated, duplicated.active_theme_id);

    expect(deleted.active_theme_id).toBe(DEFAULT_THEME_ID);
    expect(deleted.custom_theme_presets).toEqual([]);
  });

  it("resolves semantic overrides and shape tokens", () => {
    const source = BUILTIN_THEMES[0].dark;
    const tokens = resolveThemeTokens({
      ...source,
      radius_scale: 1.5,
      shadow_strength: 0,
      token_overrides: { panel: "#123456" },
    }, true);

    expect(tokens.panel).toBe("#123456");
    expect(tokens.radius_md).toBe("12px");
    expect(tokens.shadow).toContain("0.080");
  });

  it("clones returned presets so callers cannot mutate the registry", () => {
    const registry = new ThemeRegistry();
    const first = registry.get(DEFAULT_THEME_ID) as ThemePreset;
    first.dark.token_overrides.panel = "#000000";

    expect(registry.get(DEFAULT_THEME_ID)?.dark.token_overrides.panel).toBeUndefined();
  });
});
