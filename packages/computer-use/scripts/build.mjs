import { execFileSync } from "node:child_process";
import { mkdirSync, cpSync, writeFileSync, rmSync, readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const signingConfigPath = path.resolve(root, "../../.crabcode/computer-use-signing.json");
function signingIdentity() {
  const override = process.env.CRABCODE_SIGNING_IDENTITY?.trim();
  if (override) return override;
  if (!existsSync(signingConfigPath)) return "-";
  const config = JSON.parse(readFileSync(signingConfigPath, "utf8"));
  if (typeof config.identity !== "string" || !config.identity.trim()) {
    throw new Error(`Set a nonempty identity in ${signingConfigPath}; refusing to fall back to ad-hoc signing`);
  }
  return config.identity.trim();
}
const identity = process.platform === "darwin" ? signingIdentity() : null;
const version = readFileSync(path.join(root, "host/Cargo.toml"), "utf8").match(/^version\s*=\s*"([^"]+)"/m)?.[1];
if (!version) throw new Error("Computer Use component version is missing");
const debug = process.argv.includes("--debug");
const destinationArg = process.argv.indexOf("--out");
const destination = destinationArg < 0 ? path.join(root, "dist", `${process.platform}-${process.arch}`) : path.resolve(process.argv[destinationArg + 1]);
const executable = `crabcode-computer-use-host${process.platform === "win32" ? ".exe" : ""}`;
execFileSync("cargo", ["build", "--locked", "--manifest-path", path.join(root, "Cargo.toml"), "-p", "crabcode-computer-use-host", ...(debug ? [] : ["--release"])], { stdio: "inherit" });
const target = process.env.CARGO_TARGET_DIR || path.join(root, "target");
const binary = path.join(target, debug ? "debug" : "release", executable);
mkdirSync(destination, { recursive: true });
if (process.platform === "darwin") {
  const bundle = path.join(destination, "Crab Computer Use.app");
  rmSync(bundle, { recursive: true, force: true });
  mkdirSync(path.join(bundle, "Contents", "MacOS"), { recursive: true });
  mkdirSync(path.join(bundle, "Contents", "Resources"), { recursive: true });
  cpSync(binary, path.join(bundle, "Contents", "MacOS", executable));
  cpSync(path.join(root, "icons", "icon.icns"), path.join(bundle, "Contents", "Resources", "icon.icns"));
  writeFileSync(path.join(bundle, "Contents", "Info.plist"), `<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.crabcode.computer-use</string>
<key>CFBundleName</key><string>Crab Computer Use</string>
<key>CFBundleExecutable</key><string>${executable}</string>
<key>CFBundleIconFile</key><string>icon.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>${version}</string>
<key>CFBundleShortVersionString</key><string>${version}</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>`);
  // Local builds can pin an Apple Development identity without committing
  // personal certificate details. Release CI supplies its Developer ID via env.
  execFileSync("/usr/bin/codesign", ["--force", "--sign", identity, ...(identity === "-" ? [] : ["--timestamp", "--options", "runtime"]), bundle], { stdio: "inherit" });
  execFileSync("/usr/bin/codesign", ["--verify", "--deep", "--strict", bundle], { stdio: "inherit" });
} else {
  cpSync(binary, path.join(destination, executable));
}
writeFileSync(path.join(destination, "runtime.json"), JSON.stringify({ protocol_version: 1, version, platform: process.platform, arch: process.arch }, null, 2) + "\n");
console.log(`Computer Use component: ${destination}`);

if (process.argv.includes("--sync-local-clients")) {
  const clientDestinations = [
    path.resolve(root, "../desktop/src-tauri/resources/computer-use"),
    path.resolve(root, "../vscode/resources/computer-use"),
    path.resolve(root, "../vscode/computer-use/resources/computer-use"),
  ];
  if (!clientDestinations.includes(destination)) {
    throw new Error("--sync-local-clients requires a Desktop or VS Code resource destination");
  }
  for (const mirror of clientDestinations) {
    if (mirror === destination) continue;
    rmSync(mirror, { recursive: true, force: true });
    cpSync(destination, mirror, { recursive: true, preserveTimestamps: true });
    console.log(`Computer Use component mirrored: ${mirror}`);
  }
}
