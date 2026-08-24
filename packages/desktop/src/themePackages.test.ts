import { strToU8, unzipSync, zipSync } from "fflate";
import { describe, expect, it } from "vitest";
import { BUILTIN_THEMES, THEME_DOCUMENT_SCHEMA, cloneThemePreset } from "./theme";
import {
  compareVersions,
  parseSkinPackage,
  parseThemeDocument,
  serializeSkinPackage,
  serializeThemeDocument,
} from "./themePackages";

const PIXEL_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

function portableTheme() {
  return {
    ...cloneThemePreset(BUILTIN_THEMES[2]),
    id: "com.example.deep-sea",
    name: "Example Deep Sea",
    author: "Example",
  };
}

describe(".crabtheme.json", () => {
  it("round-trips a strict data-only theme", () => {
    const theme = portableTheme();
    const parsed = parseThemeDocument(serializeThemeDocument(theme));

    expect(parsed).toEqual(theme);
    expect(parsed.visuals).toBeUndefined();
  });

  it("exports a built-in under a non-reserved id so it can be imported", () => {
    const parsed = parseThemeDocument(serializeThemeDocument(BUILTIN_THEMES[0]));

    expect(parsed.id).toBe("exported.crab");
    expect(parsed.name).toBe("Crab 默认");
  });

  it("rejects unknown fields and future app requirements", () => {
    const base = JSON.parse(serializeThemeDocument(portableTheme())) as Record<string, unknown>;
    expect(() => parseThemeDocument(JSON.stringify({ ...base, surprise: true }))).toThrow("不支持的字段");

    const future = JSON.parse(serializeThemeDocument(portableTheme())) as {
      schema: string;
      theme: { minimum_app_version: string };
    };
    future.theme.minimum_app_version = "99.0.0";
    expect(() => parseThemeDocument(JSON.stringify(future))).toThrow("需要 Crab Desktop 99.0.0");
  });

  it("rejects executable or malformed theme shapes", () => {
    const document = JSON.parse(serializeThemeDocument(portableTheme())) as {
      schema: string;
      theme: Record<string, unknown>;
    };
    document.theme.script = "alert(1)";

    expect(document.schema).toBe(THEME_DOCUMENT_SCHEMA);
    expect(() => parseThemeDocument(JSON.stringify(document))).toThrow("theme 包含不支持的字段：script");
  });

  it("compares semantic versions numerically", () => {
    expect(compareVersions("0.10.0", "0.9.9")).toBeGreaterThan(0);
    expect(compareVersions("1.2.3", "1.2.3")).toBe(0);
  });
});

describe(".crabskin", () => {
  it("round-trips declared preview and decoration assets", () => {
    const theme = {
      ...portableTheme(),
      preview: { light: PIXEL_PNG, dark: PIXEL_PNG },
      visuals: {
        workspace_background: {
          data_url: PIXEL_PNG,
          opacity: 0.45,
          fit: "cover" as const,
          position: "center" as const,
        },
        composer_frame: {
          data_url: PIXEL_PNG,
          opacity: 1,
          fit: "fill" as const,
          position: "center" as const,
        },
      },
    };

    const packed = serializeSkinPackage(theme);
    const parsed = parseSkinPackage(packed);

    expect(parsed).toEqual(theme);
    expect(Object.keys(unzipSync(packed)).sort()).toEqual([
      "assets/composer_frame.png",
      "assets/workspace_background.png",
      "manifest.json",
      "preview/dark.png",
      "preview/light.png",
    ]);
  });

  it("rejects path traversal before decompressing files", () => {
    const packed = zipSync({
      "manifest.json": strToU8("{}"),
      "../evil.png": strToU8("evil"),
    });

    expect(() => parseSkinPackage(packed)).toThrow("不安全路径");
  });

  it("rejects files that are not declared by the manifest", () => {
    const theme = {
      ...portableTheme(),
      visuals: {
        app_background: {
          data_url: PIXEL_PNG,
          opacity: 1,
          fit: "cover" as const,
          position: "center" as const,
        },
      },
    };
    const files = unzipSync(serializeSkinPackage(theme));
    files["assets/unreferenced.png"] = strToU8("not an image");

    expect(() => parseSkinPackage(zipSync(files))).toThrow("未声明文件");
  });
});
