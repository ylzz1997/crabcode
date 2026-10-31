// Regenerate the checked-in macOS and Windows icons from the shared PNG master.
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

if (process.platform !== "darwin") {
  throw new Error("Regenerate the bundled icons on macOS using sips and iconutil");
}
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.join(root, "icons", "icon.png");
const destination = path.join(root, "icons", "icon.icns");
const temporary = mkdtempSync(path.join(os.tmpdir(), "crab-computer-use-icon-"));
const iconset = path.join(temporary, "icon.iconset");
try {
  mkdirSync(iconset);
  for (const size of [16, 32, 128, 256, 512]) {
    for (const scale of [1, 2]) {
      const pixels = String(size * scale);
      const name = `icon_${size}x${size}${scale === 2 ? "@2x" : ""}.png`;
      execFileSync("/usr/bin/sips", ["-z", pixels, pixels, source, "--out", path.join(iconset, name)], { stdio: "pipe" });
    }
  }
  execFileSync("/usr/bin/iconutil", ["-c", "icns", iconset, "-o", destination], { stdio: "inherit" });
  console.log(`Computer Use icon: ${destination}`);

  // The extension uses the same Computer Use artwork at a Marketplace-safe size.
  const extensionIcon = path.resolve(root, "../vscode/computer-use/logo.png");
  copyFileSync(path.join(iconset, "icon_256x256@2x.png"), extensionIcon);
  console.log(`Computer Use extension icon: ${extensionIcon}`);

  // Windows supports PNG-backed ICO entries. Keep every size in one file so
  // Explorer can select an appropriate rendition for its view and display DPI.
  const sizes = [16, 24, 32, 48, 64, 128, 256];
  const header = Buffer.alloc(6 + sizes.length * 16);
  header.writeUInt16LE(1, 2); // ICO, not CUR
  header.writeUInt16LE(sizes.length, 4);
  const images = [];
  let offset = header.length;
  for (const [index, size] of sizes.entries()) {
    const resized = path.join(temporary, `windows-${size}.png`);
    execFileSync("/usr/bin/sips", ["-z", String(size), String(size), source, "--out", resized], { stdio: "pipe" });
    const png = readFileSync(resized);
    const entry = 6 + index * 16;
    header[entry] = header[entry + 1] = size === 256 ? 0 : size;
    header.writeUInt16LE(1, entry + 4); // planes
    header.writeUInt16LE(32, entry + 6); // RGBA
    header.writeUInt32LE(png.length, entry + 8);
    header.writeUInt32LE(offset, entry + 12);
    images.push(png);
    offset += png.length;
  }
  const windowsIcon = path.join(root, "icons", "icon.ico");
  writeFileSync(windowsIcon, Buffer.concat([header, ...images]));
  console.log(`Computer Use icon: ${windowsIcon}`);
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
