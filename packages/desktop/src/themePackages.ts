import { strFromU8, strToU8, unzipSync, zipSync } from "fflate";
import desktopPackage from "../package.json";
import {
  SKIN_DOCUMENT_SCHEMA,
  THEME_DOCUMENT_SCHEMA,
  THEME_VISUAL_SLOTS,
  compareThemeVersions,
  parseThemePreset,
} from "./theme";
import type {
  ThemePreset,
  ThemeVisualAsset,
  ThemeVisualFit,
  ThemeVisualPosition,
  ThemeVisualSlot,
} from "./types";

const MAX_THEME_BYTES = 256 * 1024;
const MAX_SKIN_ARCHIVE_BYTES = 12 * 1024 * 1024;
const MAX_SKIN_UNPACKED_BYTES = 20 * 1024 * 1024;
const MAX_SKIN_FILE_BYTES = 8 * 1024 * 1024;
const MAX_SKIN_FILES = 24;

interface SkinManifestVisual {
  asset: string;
  opacity: number;
  fit: ThemeVisualFit;
  position: ThemeVisualPosition;
}

interface SkinManifestTheme extends Omit<ThemePreset, "preview" | "visuals"> {
  preview?: { light?: string; dark?: string };
  visuals?: Partial<Record<ThemeVisualSlot, SkinManifestVisual>>;
}

interface SkinManifest {
  schema: typeof SKIN_DOCUMENT_SCHEMA;
  theme: SkinManifestTheme;
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function exactKeys(value: Record<string, unknown>, allowed: readonly string[], label: string): void {
  const allowedSet = new Set(allowed);
  const unexpected = Object.keys(value).find((key) => !allowedSet.has(key));
  if (unexpected) throw new Error(`${label} 包含不支持的字段：${unexpected}`);
}

export function compareVersions(left: string, right: string): number {
  return compareThemeVersions(left, right);
}

function ensureCompatible(theme: ThemePreset): void {
  if (compareVersions(theme.minimum_app_version, desktopPackage.version) > 0) {
    throw new Error(`此主题需要 Crab Desktop ${theme.minimum_app_version} 或更高版本；当前版本是 ${desktopPackage.version}`);
  }
}

export function serializeThemeDocument(theme: ThemePreset): string {
  const { preview: _preview, visuals: _visuals, ...portableTheme } = theme;
  if (portableTheme.id.startsWith("builtin.")) portableTheme.id = `exported.${portableTheme.id.slice("builtin.".length)}`;
  return `${JSON.stringify({ schema: THEME_DOCUMENT_SCHEMA, theme: portableTheme }, null, 2)}\n`;
}

export function parseThemeDocument(text: string): ThemePreset {
  if (new TextEncoder().encode(text).byteLength > MAX_THEME_BYTES) throw new Error("主题文件不能超过 256 KiB");
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("主题文件不是有效的 JSON");
  }
  const document = object(parsed);
  if (!document) throw new Error("主题文件根节点必须是对象");
  exactKeys(document, ["schema", "theme"], "主题文件");
  if (document.schema !== THEME_DOCUMENT_SCHEMA) throw new Error(`不支持的主题格式：${String(document.schema)}`);
  const theme = parseThemePreset(document.theme);
  if (theme.preview || theme.visuals) throw new Error(".crabtheme.json 不能携带图片；请使用 .crabskin");
  ensureCompatible(theme);
  return theme;
}

function safeArchivePath(path: string): boolean {
  return path.length > 0
    && path.length <= 180
    && !path.startsWith("/")
    && !path.includes("\\")
    && !path.includes("\0")
    && !path.split("/").includes("..")
    && !path.split("/").includes("");
}

function readU16(data: Uint8Array, offset: number): number {
  return data[offset] | (data[offset + 1] << 8);
}

function readU32(data: Uint8Array, offset: number): number {
  return (data[offset]
    | (data[offset + 1] << 8)
    | (data[offset + 2] << 16)
    | (data[offset + 3] << 24)) >>> 0;
}

/** Inspect the central directory before decompression to reject oversized or unsafe archives. */
function inspectZip(data: Uint8Array): string[] {
  if (data.byteLength > MAX_SKIN_ARCHIVE_BYTES) throw new Error("皮肤包不能超过 12 MiB");
  const minimumEocd = 22;
  if (data.byteLength < minimumEocd) throw new Error("皮肤包不是有效的 ZIP 文件");
  let eocd = -1;
  for (let offset = data.length - minimumEocd; offset >= Math.max(0, data.length - 65_557); offset -= 1) {
    if (readU32(data, offset) === 0x06054b50) {
      eocd = offset;
      break;
    }
  }
  if (eocd < 0) throw new Error("皮肤包不是有效的 ZIP 文件");
  const entries = readU16(data, eocd + 10);
  const centralSize = readU32(data, eocd + 12);
  const centralOffset = readU32(data, eocd + 16);
  if (entries < 1 || entries > MAX_SKIN_FILES) throw new Error(`皮肤包文件数必须在 1–${MAX_SKIN_FILES} 之间`);
  if (centralOffset + centralSize > eocd || centralOffset >= data.length) throw new Error("皮肤包中央目录无效");
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const names: string[] = [];
  let total = 0;
  let cursor = centralOffset;
  for (let index = 0; index < entries; index += 1) {
    if (cursor + 46 > data.length || readU32(data, cursor) !== 0x02014b50) throw new Error("皮肤包中央目录损坏");
    const unpacked = readU32(data, cursor + 24);
    const nameLength = readU16(data, cursor + 28);
    const extraLength = readU16(data, cursor + 30);
    const commentLength = readU16(data, cursor + 32);
    if (unpacked > MAX_SKIN_FILE_BYTES) throw new Error("皮肤包中存在超过 8 MiB 的文件");
    total += unpacked;
    if (total > MAX_SKIN_UNPACKED_BYTES) throw new Error("皮肤包解压后不能超过 20 MiB");
    const nameStart = cursor + 46;
    const nameEnd = nameStart + nameLength;
    if (nameEnd > data.length) throw new Error("皮肤包文件名损坏");
    let name: string;
    try {
      name = decoder.decode(data.subarray(nameStart, nameEnd));
    } catch {
      throw new Error("皮肤包文件名必须是 UTF-8");
    }
    if (!safeArchivePath(name)) throw new Error(`皮肤包包含不安全路径：${name}`);
    if (names.includes(name)) throw new Error(`皮肤包包含重复文件：${name}`);
    names.push(name);
    cursor = nameEnd + extraLength + commentLength;
  }
  return names;
}

function imageMime(bytes: Uint8Array): "image/png" | "image/jpeg" | "image/webp" | "image/gif" | null {
  if (bytes.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return "image/png";
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  if (bytes.length >= 12 && strFromU8(bytes.subarray(0, 4)) === "RIFF" && strFromU8(bytes.subarray(8, 12)) === "WEBP") return "image/webp";
  if (bytes.length >= 6 && (strFromU8(bytes.subarray(0, 6)) === "GIF87a" || strFromU8(bytes.subarray(0, 6)) === "GIF89a")) return "image/gif";
  return null;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function imageDataUrl(bytes: Uint8Array, label: string): string {
  const mime = imageMime(bytes);
  if (!mime) throw new Error(`${label} 不是受支持的 PNG、JPEG、WebP 或 GIF 图片`);
  return `data:${mime};base64,${bytesToBase64(bytes)}`;
}

function readAsset(files: Record<string, Uint8Array>, path: unknown, label: string, used: Set<string>): string {
  if (typeof path !== "string" || !safeArchivePath(path) || path === "manifest.json") throw new Error(`${label} 路径无效`);
  const bytes = files[path];
  if (!bytes) throw new Error(`${label} 引用的文件不存在：${path}`);
  used.add(path);
  return imageDataUrl(bytes, label);
}

function manifestThemeToPreset(value: unknown, files: Record<string, Uint8Array>, used: Set<string>): ThemePreset {
  const source = object(value);
  if (!source) throw new Error("manifest.theme 必须是对象");
  const portable = { ...source };
  if (source.preview !== undefined) {
    const previewSource = object(source.preview);
    if (!previewSource) throw new Error("manifest.theme.preview 必须是对象");
    exactKeys(previewSource, ["light", "dark"], "manifest.theme.preview");
    portable.preview = {
      ...(previewSource.light === undefined ? {} : { light: readAsset(files, previewSource.light, "浅色预览", used) }),
      ...(previewSource.dark === undefined ? {} : { dark: readAsset(files, previewSource.dark, "深色预览", used) }),
    };
  }
  if (source.visuals !== undefined) {
    const visualsSource = object(source.visuals);
    if (!visualsSource) throw new Error("manifest.theme.visuals 必须是对象");
    exactKeys(visualsSource, [...THEME_VISUAL_SLOTS], "manifest.theme.visuals");
    const visuals: Record<string, ThemeVisualAsset> = {};
    for (const [slot, rawVisual] of Object.entries(visualsSource)) {
      const visual = object(rawVisual);
      if (!visual) throw new Error(`manifest.theme.visuals.${slot} 必须是对象`);
      exactKeys(visual, ["asset", "opacity", "fit", "position"], `manifest.theme.visuals.${slot}`);
      visuals[slot] = {
        data_url: readAsset(files, visual.asset, `装饰资源 ${slot}`, used),
        opacity: visual.opacity as number,
        fit: visual.fit as ThemeVisualFit,
        position: visual.position as ThemeVisualPosition,
      };
    }
    portable.visuals = visuals;
  }
  return parseThemePreset(portable);
}

export function parseSkinPackage(data: Uint8Array): ThemePreset {
  const names = inspectZip(data);
  if (!names.includes("manifest.json")) throw new Error("皮肤包缺少 manifest.json");
  let files: Record<string, Uint8Array>;
  try {
    files = unzipSync(data);
  } catch {
    throw new Error("皮肤包解压失败");
  }
  let unpackedBytes = 0;
  for (const [name, bytes] of Object.entries(files)) {
    if (!names.includes(name) || bytes.byteLength > MAX_SKIN_FILE_BYTES) throw new Error(`皮肤包文件异常：${name}`);
    unpackedBytes += bytes.byteLength;
    if (unpackedBytes > MAX_SKIN_UNPACKED_BYTES) throw new Error("皮肤包解压后不能超过 20 MiB");
  }
  const manifestBytes = files["manifest.json"];
  if (!manifestBytes || manifestBytes.byteLength > MAX_THEME_BYTES) throw new Error("manifest.json 缺失或过大");
  let parsed: unknown;
  try {
    parsed = JSON.parse(strFromU8(manifestBytes));
  } catch {
    throw new Error("manifest.json 不是有效的 JSON");
  }
  const manifest = object(parsed);
  if (!manifest) throw new Error("manifest.json 根节点必须是对象");
  exactKeys(manifest, ["schema", "theme"], "manifest.json");
  if (manifest.schema !== SKIN_DOCUMENT_SCHEMA) throw new Error(`不支持的皮肤格式：${String(manifest.schema)}`);
  const used = new Set(["manifest.json"]);
  const theme = manifestThemeToPreset(manifest.theme, files, used);
  const unused = names.find((name) => !used.has(name));
  if (unused) throw new Error(`皮肤包包含未声明文件：${unused}`);
  ensureCompatible(theme);
  return theme;
}

function dataUrlParts(dataUrl: string): { mime: string; bytes: Uint8Array } {
  const match = /^data:(image\/(?:png|jpeg|webp|gif));base64,([a-z0-9+/]+=*)$/i.exec(dataUrl);
  if (!match) throw new Error("皮肤包含无效的内嵌图片");
  return { mime: match[1].toLowerCase(), bytes: base64ToBytes(match[2]) };
}

function extensionForMime(mime: string): string {
  if (mime === "image/jpeg") return "jpg";
  return mime.slice("image/".length);
}

export function serializeSkinPackage(theme: ThemePreset): Uint8Array {
  if (!theme.visuals && !theme.preview) throw new Error("当前预设不包含皮肤图片资源");
  const files: Record<string, Uint8Array> = {};
  const { preview, visuals, ...portable } = theme;
  if (portable.id.startsWith("builtin.")) portable.id = `exported.${portable.id.slice("builtin.".length)}`;
  const manifestTheme: SkinManifestTheme = { ...portable };
  if (preview) {
    manifestTheme.preview = {};
    for (const scheme of ["light", "dark"] as const) {
      const dataUrl = preview[scheme];
      if (!dataUrl) continue;
      const { mime, bytes } = dataUrlParts(dataUrl);
      const path = `preview/${scheme}.${extensionForMime(mime)}`;
      files[path] = bytes;
      manifestTheme.preview[scheme] = path;
    }
  }
  if (visuals) {
    manifestTheme.visuals = {};
    for (const slot of THEME_VISUAL_SLOTS) {
      const visual = visuals[slot];
      if (!visual) continue;
      const { mime, bytes } = dataUrlParts(visual.data_url);
      const path = `assets/${slot}.${extensionForMime(mime)}`;
      files[path] = bytes;
      manifestTheme.visuals[slot] = {
        asset: path,
        opacity: visual.opacity,
        fit: visual.fit,
        position: visual.position,
      };
    }
  }
  const manifest: SkinManifest = { schema: SKIN_DOCUMENT_SCHEMA, theme: manifestTheme };
  files["manifest.json"] = strToU8(`${JSON.stringify(manifest, null, 2)}\n`);
  const zipped = zipSync(files, { level: 6 });
  if (zipped.byteLength > MAX_SKIN_ARCHIVE_BYTES) throw new Error("导出的皮肤包超过 12 MiB");
  return zipped;
}

export function safeThemeFilename(name: string): string {
  const normalized = name
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9\u4e00-\u9fff._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return normalized || "crab-theme";
}
