"""Reject incomplete/wrong-platform native payloads before distributing clients."""

import json
from pathlib import Path
import plistlib
import shutil
import struct
import tempfile
import unittest
from unittest import mock
import zipfile

from scripts import package_clients as packaging, sync_version


class ClientPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.version = packaging.check_versions()
        # Fixtures are structural headers, not runnable/signed applications.
        patch = mock.patch.object(packaging, "run")
        patch.start()
        self.addCleanup(patch.stop)

    def bundle(self, platform):
        directory = self.root / platform
        binary = directory / packaging.executable_relative(platform)
        binary.parent.mkdir(parents=True)
        target_os, arch = platform.split("-")
        (directory / "runtime.json").write_text(json.dumps({
            "platform": target_os, "arch": arch, "version": self.version, "protocol_version": 1,
        }))
        if target_os == "darwin":
            binary.write_bytes(struct.pack("<II", 0xFEEDFACF, 0x0100000C if arch == "arm64" else 0x01000007))
            contents = binary.parent.parent
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "io.crabcode.computer-use", "CFBundleVersion": self.version,
                "CFBundleIconFile": "icon.icns",
            }))
            (contents / "Resources").mkdir()
            (contents / "Resources/icon.icns").write_bytes(b"icns fixture")
            (contents / "_CodeSignature").mkdir()
            (contents / "_CodeSignature/CodeResources").write_bytes(b"signature fixture")
        else:
            data = bytearray(70)
            data[:2] = b"MZ"
            struct.pack_into("<I", data, 60, 64)
            data[64:] = b"PE\0\0\x64\x86"
            binary.write_bytes(data)
        binary.chmod(0o755)
        return directory

    def archive(self, bundles, target=None, omit=None, tamper=None, icon=None,
                manifest_overrides=None, language_packs=None):
        path = self.root / "extension.vsix"
        with zipfile.ZipFile(path, "w") as archive:
            manifest = {
                "name": "crabcode-computer-use" if bundles else "crabcode", "version": self.version,
            }
            manifest.update(manifest_overrides or {})
            for name, translations in (language_packs or {}).items():
                archive.writestr("extension/" + name, json.dumps(translations))
            if icon is not None:
                manifest["icon"] = "logo.png"
                archive.writestr("extension/logo.png", icon)
            archive.writestr("extension/package.json", json.dumps(manifest))
            attribute = f' TargetPlatform="{target}"' if target else ""
            archive.writestr("extension.vsixmanifest", f"<PackageManifest><Metadata><Identity{attribute}/></Metadata></PackageManifest>")
            for platform, directory in bundles.items():
                for file in directory.rglob("*"):
                    if not file.is_file():
                        continue
                    name = "extension/resources/computer-use/" + platform + "/" + file.relative_to(directory).as_posix()
                    if omit and name.endswith(omit):
                        continue
                    if tamper and name.endswith(tamper):
                        archive.writestr(name, b"modified payload")
                    else:
                        info = zipfile.ZipInfo.from_file(file, name)
                        if file.name == "crabcode-computer-use-host":
                            info.external_attr |= 0o111 << 16
                        archive.writestr(info, file.read_bytes())
        return path

    def test_all_supported_binary_architectures(self):
        for platform in packaging.PLATFORMS:
            with self.subTest(platform=platform):
                packaging.check_bundle(self.bundle(platform), platform, self.version)

    def test_metadata_cannot_disguise_another_cpu(self):
        directory = self.bundle("darwin-arm64")
        binary = directory / packaging.executable_relative("darwin-arm64")
        binary.write_bytes(struct.pack("<II", 0xFEEDFACF, 0x01000007))
        with self.assertRaisesRegex(ValueError, "architecture"):
            packaging.check_bundle(directory, "darwin-arm64", self.version)

    def test_old_component_version_is_rejected(self):
        directory = self.bundle("win32-x64")
        manifest = packaging.read_json(directory / "runtime.json")
        manifest["version"] = "0.0.0"
        (directory / "runtime.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "version"):
            packaging.check_bundle(directory, "win32-x64", self.version)

    def test_universal_vsix_preserves_all_native_files_and_permissions(self):
        bundles = {platform: self.bundle(platform) for platform in packaging.PLATFORMS}
        packaging.check_vsix(self.archive(bundles), bundles, self.version)

    def test_platform_vsix_has_correct_marketplace_target(self):
        bundles = {"win32-x64": self.bundle("win32-x64")}
        archive = self.archive(bundles, "win32-x64")
        packaging.check_vsix(archive, bundles, self.version, "win32-x64")
        with self.assertRaisesRegex(ValueError, "VSIX platform"):
            packaging.check_vsix(archive, bundles, self.version, "darwin-arm64")

    def test_missing_mac_signature_cannot_pass_vsix_verification(self):
        bundles = {"darwin-arm64": self.bundle("darwin-arm64")}
        with self.assertRaises(KeyError):
            packaging.check_vsix(self.archive(bundles, omit="CodeResources"), bundles, self.version)

    def test_packaging_cannot_silently_modify_native_files(self):
        bundles = {"darwin-x64": self.bundle("darwin-x64")}
        with self.assertRaisesRegex(ValueError, "payload changed"):
            packaging.check_vsix(self.archive(bundles, tamper="CodeResources"), bundles, self.version)

    def test_staging_preserves_every_language_pack_for_both_extensions(self):
        for relative in ("packages/vscode", "packages/vscode/computer-use"):
            with self.subTest(extension=relative):
                source = packaging.ROOT / relative
                staged = self.root / relative
                packaging.copy_extension(source, staged)
                expected = {p.name: p.read_bytes() for p in source.glob("package.nls*.json")}
                self.assertIn("package.nls.json", expected)
                self.assertGreater(len(expected), 1)
                self.assertEqual(expected, {p.name: p.read_bytes() for p in staged.glob("package.nls*.json")})

    def test_localized_manifest_requires_default_pack_and_all_referenced_keys(self):
        manifest = {"description": "%description%", "contributes": {
            "commands": [{"title": "%command.title%"}],
        }}
        english = {"description": "Description", "command.title": "Open"}
        chinese = {"description": "描述", "command.title": "打开"}
        complete = {"package.nls.json": english, "package.nls.zh-cn.json": chinese}
        packaging.check_vsix(self.archive({}, manifest_overrides=manifest, language_packs=complete), {}, self.version)
        with self.assertRaisesRegex(ValueError, "Missing default language pack"):
            packaging.check_vsix(self.archive({}, manifest_overrides=manifest), {}, self.version)
        for name in complete:
            with self.subTest(pack=name):
                broken = {**complete, name: {"description": "Description"}}
                with self.assertRaisesRegex(ValueError, "Unresolved localization keys.*command.title"):
                    packaging.check_vsix(self.archive({}, manifest_overrides=manifest, language_packs=broken), {}, self.version)

    def test_verification_rejects_dropped_or_changed_locales(self):
        source = self.root / "source"
        source.mkdir()
        english = {"description": "Description"}
        chinese = {"description": "描述"}
        for name, values in (("package.nls.json", english), ("package.nls.zh-cn.json", chinese)):
            (source / name).write_text(json.dumps(values), encoding="utf-8")
        for translations in (None, {"description": "Wrong translation"}):
            with self.subTest(translations=translations):
                packs = {"package.nls.json": english}
                if translations is not None:
                    packs["package.nls.zh-cn.json"] = translations
                archive = self.archive({}, manifest_overrides={"description": "%description%"}, language_packs=packs)
                with self.assertRaisesRegex(ValueError, "Missing or changed language pack.*zh-cn"):
                    packaging.check_vsix(archive, {}, self.version, source=source)

    @staticmethod
    def png_header(width, height):
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 4

    def test_oversized_icon_is_rejected_before_staging(self):
        source = self.root / "source"
        source.mkdir()
        (source / "package.json").write_text(json.dumps({"icon": "logo.png"}))
        (source / "logo.png").write_bytes(self.png_header(1254, 1254))
        staged = self.root / "staged"
        with self.assertRaisesRegex(ValueError, "1254x1254.*1024x1024"):
            packaging.copy_extension(source, staged)
        self.assertFalse(staged.exists())

    def test_packaged_icon_must_meet_marketplace_dimension_limits(self):
        packaging.check_vsix(self.archive({}, icon=self.png_header(1024, 1024)), {}, self.version)
        for width, height in ((1254, 1254), (2048, 512), (512, 2048), (64, 64)):
            with self.subTest(width=width, height=height):
                with self.assertRaisesRegex(ValueError, "Marketplace.*1024x1024"):
                    packaging.check_vsix(self.archive({}, icon=self.png_header(width, height)), {}, self.version)


class ClientVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        version = sync_version.load_version()
        paths = {path for path, _ in sync_version.rendered_files(version)}
        paths.add(sync_version.VERSION_FILE)
        # Copy the complete client metadata even if the synchronizer regresses.
        for relative in ("packages/vscode", "packages/vscode/computer-use", "packages/desktop"):
            paths.update(sync_version.ROOT / relative / name for name in ("package.json", "package-lock.json"))
        paths.update(sync_version.ROOT / "packages/desktop/src-tauri" / name
                     for name in ("Cargo.toml", "Cargo.lock", "tauri.conf.json"))
        for path in paths:
            destination = self.root / path.relative_to(sync_version.ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        for name, value in (("ROOT", self.root), ("VERSION_FILE", self.root / "packages/core/crabcode_core/_version.py")):
            patch = mock.patch.object(sync_version, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_version_bump_updates_all_clients_and_only_root_npm_versions(self):
        version = "91.82.73"
        dependencies = {}
        for path in self.root.rglob("package-lock.json"):
            dependencies[path] = {key: value for key, value in packaging.read_json(path)["packages"].items() if key}
        sync_version.VERSION_FILE.write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        for path, rendered in sync_version.rendered_files(version):
            path.write_text(rendered, encoding="utf-8")
        self.assertEqual(packaging.check_versions(), version)
        for relative in ("packages/vscode", "packages/vscode/computer-use", "packages/desktop"):
            directory = self.root / relative
            self.assertEqual(packaging.read_json(directory / "package.json")["version"], version)
            lock = packaging.read_json(directory / "package-lock.json")
            self.assertEqual(lock["version"], version)
            self.assertEqual(lock["packages"][""]["version"], version)
            self.assertEqual({key: value for key, value in lock["packages"].items() if key}, dependencies[directory / "package-lock.json"])
        desktop = self.root / "packages/desktop/src-tauri"
        self.assertEqual(packaging.read_json(desktop / "tauri.conf.json")["version"], version)
        self.assertIn(f'version = "{version}"', (desktop / "Cargo.toml").read_text())
        self.assertIn(f'name = "crabcode-desktop"\nversion = "{version}"', (desktop / "Cargo.lock").read_text())
        for path, rendered in sync_version.rendered_files(version):
            self.assertEqual(path.read_text(encoding="utf-8"), rendered, path)

    def test_packaging_rejects_stale_desktop_crate_and_lock_metadata(self):
        version = sync_version.load_version()
        for relative in ("packages/desktop/src-tauri/Cargo.toml", "packages/desktop/src-tauri/Cargo.lock",
                         "packages/desktop/package-lock.json", "packages/vscode/computer-use/package-lock.json"):
            with self.subTest(file=relative):
                path = self.root / relative
                original = path.read_text(encoding="utf-8")
                path.write_text(original.replace(f'"{version}"', '"0.0.0"'), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, "Version mismatch"):
                        packaging.check_versions()
                finally:
                    path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
