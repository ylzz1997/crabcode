"""Reject incomplete/wrong-platform native payloads before distributing clients."""

import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest import mock
import zipfile

spec = importlib.util.spec_from_file_location(
    "package_clients", Path(__file__).resolve().parents[1] / "scripts/package_clients.py"
)
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


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

    def archive(self, bundles, target=None, omit=None, tamper=None):
        path = self.root / "extension.vsix"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("extension/package.json", json.dumps({
                "name": "crabcode-computer-use" if bundles else "crabcode", "version": self.version,
            }))
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


if __name__ == "__main__":
    unittest.main()
