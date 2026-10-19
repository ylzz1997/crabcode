#!/usr/bin/env python3
"""Build downloadable client packages from prebuilt Computer Use bundles.

This script never publishes packages or changes release/version metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("darwin-arm64", "darwin-x64", "win32-x64")


def run(*args: str, cwd: Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_versions() -> str:
    version = re.search(
        r'^VERSION = "([^"]+)"',
        (ROOT / "packages/core/crabcode_core/_version.py").read_text(),
        re.MULTILINE,
    ).group(1)
    for relative in (
        "packages/vscode/package.json",
        "packages/vscode/computer-use/package.json",
        "packages/desktop/package.json",
        "packages/desktop/src-tauri/tauri.conf.json",
    ):
        if read_json(ROOT / relative)["version"] != version:
            raise ValueError(f"Version mismatch: {relative}; expected {version}")
    return version


def executable_relative(platform: str) -> Path:
    if platform.startswith("darwin-"):
        return Path("Crab Computer Use.app/Contents/MacOS/crabcode-computer-use-host")
    return Path("crabcode-computer-use-host.exe")


def check_binary(path: Path, platform: str) -> None:
    with path.open("rb") as binary:
        header = binary.read(64)
        if platform.startswith("darwin-"):
            expected_cpu = 0x0100000C if platform.endswith("arm64") else 0x01000007
            if len(header) < 8 or struct.unpack("<II", header[:8]) != (0xFEEDFACF, expected_cpu):
                raise ValueError(f"Incorrect Mach-O architecture: {path} ({platform})")
            if os.name != "nt" and not path.stat().st_mode & 0o111:
                raise ValueError(f"Executable permission missing: {path}")
        else:
            if len(header) < 64 or header[:2] != b"MZ":
                raise ValueError(f"Invalid PE executable: {path}")
            binary.seek(struct.unpack_from("<I", header, 60)[0])
            if binary.read(6) != b"PE\0\0\x64\x86":
                raise ValueError(f"Incorrect PE architecture: {path} ({platform})")


def check_bundle(directory: Path, platform: str, version: str) -> dict:
    manifest = read_json(directory / "runtime.json")
    target_os, arch = platform.split("-", 1)
    for key, expected in (("platform", target_os), ("arch", arch), ("version", version), ("protocol_version", 1)):
        if manifest.get(key) != expected:
            raise ValueError(f"{directory}/runtime.json: {key} must be {expected!r}")
    check_binary(directory / executable_relative(platform), platform)
    if target_os == "darwin":
        contents = directory / "Crab Computer Use.app/Contents"
        plist = plistlib.loads((contents / "Info.plist").read_bytes())
        if plist.get("CFBundleIdentifier") != "io.crabcode.computer-use" or plist.get("CFBundleVersion") != version:
            raise ValueError(f"Incorrect app identity/version: {contents}")
        if not (contents / "Resources" / plist["CFBundleIconFile"]).read_bytes().startswith(b"icns"):
            raise ValueError(f"Missing macOS icon: {contents}")
        if not (contents / "_CodeSignature/CodeResources").is_file():
            raise ValueError(f"Missing macOS signature: {contents}")
        if sys.platform == "darwin":
            run("codesign", "--verify", "--deep", "--strict", str(contents.parent))
    return manifest


def copy_extension(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in ("dist", "resources", "logo.png", "README.md", "CHANGELOG.md", "LICENSE"):
        item = source / name
        if item.is_dir():
            shutil.copytree(item, destination / name, ignore=shutil.ignore_patterns("computer-use", ".DS_Store", "*.map"))
        elif item.is_file():
            shutil.copy2(item, destination / name)
    manifest = read_json(source / "package.json")
    # Native payloads have already been built on their respective runners.
    # A prepublish hook in this temporary directory would rebuild/replace them.
    manifest.get("scripts", {}).pop("vscode:prepublish", None)
    (destination / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (destination / ".vscodeignore").write_text("**/.DS_Store\n**/*.map\n", encoding="utf-8")


def check_vsix(archive_path: Path, bundles: dict[str, Path], version: str, target: str | None = None) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("extension/package.json"))
        expected_name = "crabcode-computer-use" if bundles else "crabcode"
        if manifest["version"] != version or manifest["name"] != expected_name:
            raise ValueError(f"Incorrect extension identity/version: {archive_path}")
        identity = ET.fromstring(archive.read("extension.vsixmanifest")).find(".//{*}Identity")
        if identity is None or identity.get("TargetPlatform") != target:
            raise ValueError(f"Incorrect VSIX platform: {archive_path}")
        prefix = "extension/resources/computer-use/"
        actual = {name for name in archive.namelist() if name.startswith(prefix) and not name.endswith("/")}
        expected = set()
        for platform, directory in bundles.items():
            for file in directory.rglob("*"):
                if file.is_file():
                    name = prefix + platform + "/" + file.relative_to(directory).as_posix()
                    expected.add(name)
                    if archive.read(name) != file.read_bytes():
                        raise ValueError(f"Native payload changed during packaging: {name}")
            if platform.startswith("darwin-"):
                name = prefix + platform + "/" + executable_relative(platform).as_posix()
                if not archive.getinfo(name).external_attr >> 16 & 0o111:
                    raise ValueError(f"VSIX lost executable permissions: {name}")
        if actual != expected:
            raise ValueError(f"Unexpected or missing native payloads: {archive_path}")


def write_checksums(output: Path) -> None:
    files = sorted(p for p in output.iterdir() if p.suffix in (".vsix", ".dmg", ".msi"))
    if not files:
        raise ValueError(f"No client packages in {output}")
    lines = []
    for path in files:
        with path.open("rb") as file:
            lines.append(f"{hashlib.file_digest(file, 'sha256').hexdigest()}  {path.name}\n")
    (output / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


def vsce_links(source: Path) -> list[str]:
    repository = read_json(source / "package.json")["repository"]
    url = repository["url"].removesuffix(".git")
    ref = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    directory = source.relative_to(ROOT).as_posix()
    return ["--baseContentUrl", f"{url}/blob/{ref}/{directory}/",
            "--baseImagesUrl", f"{url}/raw/{ref}/{directory}/"]


def check_dmg(installer: Path, platform: str, version: str) -> None:
    run("hdiutil", "verify", str(installer))
    # Tauri removes the intermediate .app when only a DMG is requested. Inspect
    # the application inside the actual read-only installer image instead.
    with tempfile.TemporaryDirectory(prefix="crabcode-dmg-") as temporary:
        mount = Path(temporary) / "volume"
        mount.mkdir()
        run("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(installer))
        try:
            app = mount / "Crab Desktop.app"
            run("codesign", "--verify", "--deep", "--strict", str(app))
            check_binary(app / "Contents/MacOS/crabcode-desktop", platform)
            check_bundle(app / "Contents/Resources/resources/computer-use", platform, version)
        finally:
            run("hdiutil", "detach", str(mount))


def package_vsix(bundle_root: Path, output: Path, version: str) -> None:
    bundles = {platform: bundle_root / platform for platform in PLATFORMS}
    for platform, directory in bundles.items():
        check_bundle(directory, platform, version)
    for relative in ("packages/vscode", "packages/vscode/computer-use"):
        run("npm", "run", "build", cwd=ROOT / relative)
    vsce = ROOT / "packages/vscode/node_modules/@vscode/vsce/vsce"
    with tempfile.TemporaryDirectory(prefix="crabcode-vsix-") as temporary:
        staging = Path(temporary)
        main = staging / "main"
        copy_extension(ROOT / "packages/vscode", main)
        package = output / f"crabcode-{version}.vsix"
        run("node", str(vsce), "package", "--no-dependencies", *vsce_links(ROOT / "packages/vscode"),
            "--out", str(package), cwd=main)
        check_vsix(package, {}, version)
        for target in (*PLATFORMS, "universal"):
            selected = bundles if target == "universal" else {target: bundles[target]}
            extension = staging / target
            copy_extension(ROOT / "packages/vscode/computer-use", extension)
            for platform, directory in selected.items():
                shutil.copytree(directory, extension / "resources/computer-use" / platform)
            package = output / f"crabcode-computer-use-{version}-{target}.vsix"
            args = [] if target == "universal" else ["--target", target]
            run("node", str(vsce), "package", "--no-dependencies", *args,
                *vsce_links(ROOT / "packages/vscode/computer-use"), "--out", str(package), cwd=extension)
            check_vsix(package, selected, version, None if target == "universal" else target)
    write_checksums(output)


def package_desktop(bundle_root: Path, output: Path, platform: str, version: str) -> None:
    if platform.split("-", 1)[0] != sys.platform:
        raise ValueError("Desktop installers must be built on their target OS")
    source = bundle_root / platform
    check_bundle(source, platform, version)
    desktop = ROOT / "packages/desktop"
    resources = desktop / "src-tauri/resources/computer-use"
    shutil.rmtree(resources, ignore_errors=True)
    shutil.copytree(source, resources)
    config = {"build": {"beforeBuildCommand": "npm run build"}}
    is_mac = platform.startswith("darwin-")
    if is_mac:
        config["bundle"] = {"macOS": {
            "signingIdentity": os.environ.get("CRABCODE_SIGNING_IDENTITY") or "-",
            "minimumSystemVersion": "11.0",
        }}
    kind = "dmg" if is_mac else "msi"
    # Pass a file rather than inline JSON: Windows npm/cmd quoting can strip the
    # JSON quotes before Tauri sees them. Invoke its Node entry point directly.
    with tempfile.TemporaryDirectory(prefix="crabcode-tauri-config-") as temporary:
        config_file = Path(temporary) / "config.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        run("node", str(desktop / "node_modules/@tauri-apps/cli/tauri.js"), "build", "--ci", "--bundles", kind,
            "--config", str(config_file), "--", "--locked", cwd=desktop)
    target = desktop / "src-tauri/target/release"
    check_binary(target / ("crabcode-desktop" if is_mac else "crabcode-desktop.exe"), platform)
    # Cargo caches/local build trees can contain installers from older versions.
    installers = list((target / "bundle" / kind).glob(f"*_{version}_*.{kind}"))
    if len(installers) != 1:
        raise ValueError(f"Expected exactly one {kind.upper()} installer; found {installers}")
    installer = installers[0]
    if is_mac:
        check_dmg(installer, platform, version)
    else:
        # Administrative extraction verifies that the MSI actually carries the
        # helper, without installing or starting Desktop on the build runner.
        with tempfile.TemporaryDirectory(prefix="crabcode-msi-") as temporary:
            result = subprocess.run(["msiexec.exe", "/a", str(installer), "/qn", f"TARGETDIR={temporary}"])
            if result.returncode not in (0, 3010):
                raise ValueError(f"MSI extraction failed: {result.returncode}")
            payloads = [p.parent for p in Path(temporary).rglob("runtime.json")
                        if (p.parent / executable_relative(platform)).is_file()]
            if len(payloads) != 1:
                raise ValueError("MSI must include exactly one Computer Use bundle")
            check_bundle(payloads[0], platform, version)
    shutil.copy2(installer, output / f"crab-desktop-{version}-{platform}.{kind}")
    write_checksums(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "native", "vsix", "desktop"))
    parser.add_argument("--bundle-root", type=Path, default=ROOT / "packages/computer-use/dist")
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/clients")
    args = parser.parse_args()
    version = check_versions()
    if args.command == "check":
        print(f"Client versions match {version}")
        return
    if args.command in ("native", "desktop") and not args.platform:
        parser.error("--platform is required for native/desktop")
    if args.command == "native":
        check_bundle(args.bundle_root.resolve() / args.platform, args.platform, version)
        print(f"Verified Computer Use: {args.platform} {version}")
        return
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.command == "vsix":
        package_vsix(args.bundle_root.resolve(), args.output, version)
    else:
        package_desktop(args.bundle_root.resolve(), args.output, args.platform, version)


if __name__ == "__main__":
    main()
