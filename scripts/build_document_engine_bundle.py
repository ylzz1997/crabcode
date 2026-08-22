"""Build a hash-pinned, platform-specific offline BabelDOC engine bundle.

The assets directory must already contain the BabelDOC 0.6.4 model, fonts,
CMaps, and tiktoken cache.  The script never discovers assets at runtime; it
only packages and hashes the provided directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path


BABELDOC_VERSION = "0.6.4"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def platform_tag() -> str:
    raw = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    machine = platform.machine().lower().replace("-", "_")
    if machine and machine not in raw:
        raw = f"{raw}_{machine}"
    return "".join(character if character.isalnum() or character == "_" else "_" for character in raw).strip("_").lower()


def file_entries(root: Path, *, exclude: set[str] | None = None) -> list[dict[str, object]]:
    ignored = exclude or set()
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in ignored
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--crabcode-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-version", default=BABELDOC_VERSION)
    args = parser.parse_args()
    assets = args.assets.resolve(strict=True)
    crabcode_wheel = args.crabcode_wheel.resolve(strict=True)
    if not assets.is_dir() or not crabcode_wheel.is_file():
        raise SystemExit("--assets must be a directory and --crabcode-wheel must be a wheel")
    if not crabcode_wheel.name.startswith("crabcode-"):
        raise SystemExit("--crabcode-wheel must be the monolithic CrabCode wheel")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="crabcode-engine-bundle-") as temp_name:
        root = Path(temp_name) / "bundle"
        wheelhouse = root / "wheelhouse"
        bundled_assets = root / "assets"
        wheelhouse.mkdir(parents=True)
        shutil.copytree(assets, bundled_assets)
        shutil.copy2(crabcode_wheel, wheelhouse / crabcode_wheel.name)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(wheelhouse),
                f"BabelDOC=={BABELDOC_VERSION}",
            ],
            check=True,
        )
        asset_manifest = {
            "schema_version": 1,
            "engine_version": BABELDOC_VERSION,
            "asset_version": args.asset_version,
            "files": file_entries(bundled_assets, exclude={"asset-manifest.json"}),
        }
        (bundled_assets / "asset-manifest.json").write_text(
            json.dumps(asset_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 1,
            "engine_version": BABELDOC_VERSION,
            "asset_version": args.asset_version,
            "python_tag": f"cp{sys.version_info.major}{sys.version_info.minor}",
            "platform_tag": platform_tag(),
            "files": file_entries(root, exclude={"engine-manifest.json"}),
        }
        (root / "engine-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())


if __name__ == "__main__":
    main()
