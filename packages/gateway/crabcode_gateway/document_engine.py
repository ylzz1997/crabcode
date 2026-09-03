"""Optional local BabelDOC engine discovery and installation.

The document engine deliberately lives outside the Gateway environment.  The
default installer gets BabelDOC from its official Python package and asks
BabelDOC to download and verify its own runtime assets.  An offline release
bundle remains supported for air-gapped environments.  After installation PDF
processing never contacts BabelDOC, Hugging Face, or ModelScope.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx

from crabcode_core.subprocess_utils import subprocess_group_options
from crabcode_gateway import __version__ as CRABCODE_VERSION


BABELDOC_VERSION = "0.6.4"
ENGINE_PROTOCOL_VERSION = 1
ENGINE_MANIFEST_NAME = "engine.json"
ENGINE_BUNDLE_MANIFEST_NAME = "engine-manifest.json"
ENGINE_WORKER_NAME = "document-worker.py"
MAX_ENGINE_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
ESTIMATED_ENGINE_BUNDLE_BYTES = 550 * 1024 * 1024
ProgressCallback = Callable[[str], None]


def _run_text_command(
    command: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run an engine helper with deterministic UTF-8 and no Windows popup."""
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        **subprocess_group_options(),
        **kwargs,
    )


def document_engine_root() -> Path:
    override = os.environ.get("CRABCODE_DOCUMENT_ENGINE_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return Path.home() / ".crabcode" / "engines" / "babeldoc" / BABELDOC_VERSION


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "venv" / "Scripts" / "python.exe"
    return root / "venv" / "bin" / "python"


def _worker_path(root: Path) -> Path:
    return root / ENGINE_WORKER_NAME


def _platform_tag() -> str:
    raw = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    machine = platform.machine().lower().replace("-", "_")
    if machine and machine not in raw:
        raw = f"{raw}_{machine}"
    return re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()


def _python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _verify_asset_manifest(root: Path) -> str | None:
    manifest_path = root / "assets" / "asset-manifest.json"
    manifest = _read_json(manifest_path)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        return "高精度 PDF 引擎资源清单缺失或损坏"
    assets = manifest_path.parent.resolve(strict=True)
    for item in files:
        if not isinstance(item, dict):
            return "高精度 PDF 引擎资源清单损坏"
        relative = item.get("path")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            return "高精度 PDF 引擎资源清单损坏"
        try:
            candidate = assets / _safe_bundle_member(relative)
        except ValueError:
            return "高精度 PDF 引擎资源清单损坏"
        try:
            path = candidate.resolve(strict=True)
        except OSError:
            return f"高精度 PDF 引擎资源缺失：{relative}"
        if assets not in path.parents or not path.is_file() or _sha256(path) != digest:
            return f"高精度 PDF 引擎资源校验失败：{relative}"
    return None


def _verify_engine_runtime(root: Path) -> str | None:
    try:
        check = _run_text_command(
            [
                str(_venv_python(root)),
                "-c",
                (
                    "import importlib.metadata, importlib.util; "
                    "print(importlib.metadata.version('BabelDOC')); "
                    f"assert {str(_worker_path(root).is_file())} or "
                    "importlib.util.find_spec('crabcode_gateway.document_worker')"
                ),
            ],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "高精度 PDF 引擎运行环境损坏"
    if check.returncode != 0 or check.stdout.strip().splitlines()[-1:] != [BABELDOC_VERSION]:
        return "高精度 PDF 引擎运行环境版本不匹配"
    return None


def document_engine_status() -> dict[str, Any]:
    root = document_engine_root()
    install_state = _read_json(root.parent / ".install-state.json")
    manifest = _read_json(root / ENGINE_MANIFEST_NAME)
    status = "not_installed"
    detail = "高精度 PDF 引擎尚未安装"
    ready = False
    installed_version: str | None = None
    asset_version: str | None = None
    if root.exists() and manifest is None:
        status = "broken"
        detail = "高精度 PDF 引擎清单缺失或损坏"
    elif manifest is not None:
        installed_version = str(manifest.get("engine_version") or "") or None
        asset_version = str(manifest.get("asset_version") or "") or None
        if manifest.get("protocol_version") != ENGINE_PROTOCOL_VERSION:
            status = "upgrade_required"
            detail = "高精度 PDF 引擎协议版本不兼容"
        elif installed_version != BABELDOC_VERSION:
            status = "upgrade_required"
            detail = "高精度 PDF 引擎需要升级"
        elif not _venv_python(root).is_file():
            status = "broken"
            detail = "高精度 PDF 引擎的 Python 环境不完整"
        elif not (root / "assets" / "asset-manifest.json").is_file():
            status = "broken"
            detail = "高精度 PDF 引擎资源不完整"
        else:
            runtime_error = _verify_engine_runtime(root)
            asset_error = _verify_asset_manifest(root) if runtime_error is None else None
            if runtime_error:
                status = "broken"
                detail = runtime_error
            elif asset_error:
                status = "broken"
                detail = asset_error
            else:
                status = "ready"
                detail = "高精度 PDF 引擎可以使用"
                ready = True
    result = {
        "status": status,
        "available": ready,
        "engine": "precise",
        "version": installed_version or BABELDOC_VERSION,
        "installed_version": installed_version,
        "asset_version": asset_version,
        "protocol_version": ENGINE_PROTOCOL_VERSION,
        "python_supported": (3, 10) <= sys.version_info[:2] < (3, 14),
        "managed": True,
        "install_root": str(root),
        "download_bytes": (
            manifest.get("download_bytes") or manifest.get("bundle_bytes")
            if manifest
            else ESTIMATED_ENGINE_BUNDLE_BYTES
        ),
        "download_estimated": manifest is None,
        "install_source": manifest.get("install_source") if manifest else "official",
        "install_command": "crabcode document-engine install",
        "remove_command": "crabcode document-engine remove --yes",
        "detail": detail,
    }
    if isinstance(install_state, dict) and install_state.get("status") in {"downloading", "verifying", "installing", "broken"}:
        result["status"] = install_state["status"]
        result["available"] = False
        result["detail"] = str(install_state.get("detail") or detail)
    return result


def document_engine_python() -> Path:
    status = document_engine_status()
    if not status["available"]:
        raise RuntimeError(str(status["detail"]))
    return _venv_python(document_engine_root())


def document_engine_worker_command() -> list[str]:
    """Return the isolated worker command, including old bundle compatibility."""
    python = document_engine_python()
    # Keep BabelDOC and its assets in the managed virtual environment, but run
    # the worker shipped with the current Gateway.  Worker-only fixes then take
    # effect after an app update without downloading the engine again.
    current_worker = Path(__file__).with_name("document_worker.py")
    if current_worker.is_file():
        return [str(python), str(current_worker)]
    installed_worker = _worker_path(document_engine_root())
    if installed_worker.is_file():
        return [str(python), str(installed_worker)]
    return [str(python), "-m", "crabcode_gateway.document_worker"]


def select_document_translation_engine(
    requested: str,
    precise_status: dict[str, Any] | None = None,
) -> str:
    if requested not in {"auto", "legacy", "precise"}:
        raise ValueError("invalid document translation engine")
    if requested == "legacy":
        return "legacy"
    status = precise_status or document_engine_status()
    if requested == "precise" and not status.get("available"):
        raise RuntimeError(str(status.get("detail") or "high-fidelity PDF engine is unavailable"))
    return "precise" if status.get("available") else "legacy"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_bundle_member(name: str) -> Path:
    normalized = Path(name.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        raise ValueError(f"unsafe engine bundle member: {name}")
    return normalized


def _download_bundle(source: str, destination: Path, progress: ProgressCallback) -> None:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        progress("正在下载高精度 PDF 引擎")
        size = 0
        with httpx.stream("GET", source, follow_redirects=True, timeout=120) as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if response.status_code == 404:
                    raise RuntimeError(
                        "当前 CrabCode 版本尚未发布适用于本机平台与 Python 版本的高精度 PDF 资源包；"
                        "请发布对应离线包，或使用 --bundle 指定本地资源包"
                    ) from exc
                raise
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ENGINE_BUNDLE_BYTES:
                        raise ValueError("document engine bundle exceeds 2 GiB")
                    handle.write(chunk)
        return
    path = Path(source).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_ENGINE_BUNDLE_BYTES:
        raise ValueError("document engine bundle is invalid")
    shutil.copy2(path, destination)


def _extract_and_verify_bundle(bundle: Path, destination: Path) -> dict[str, Any]:
    with zipfile.ZipFile(bundle) as archive:
        try:
            raw_manifest = archive.read(ENGINE_BUNDLE_MANIFEST_NAME)
            manifest = json.loads(raw_manifest)
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError("document engine bundle manifest is invalid") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise ValueError("document engine bundle manifest is invalid")
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported document engine bundle schema")
        if manifest.get("engine_version") != BABELDOC_VERSION:
            raise ValueError("document engine bundle version does not match CrabCode")
        if manifest.get("python_tag") not in {None, _python_tag()}:
            raise ValueError("document engine bundle targets a different Python version")
        if manifest.get("platform_tag") not in {None, _platform_tag()}:
            raise ValueError("document engine bundle targets a different platform")

        expected: dict[str, str] = {}
        for item in manifest["files"]:
            if not isinstance(item, dict):
                raise ValueError("document engine bundle file entry is invalid")
            name = item.get("path")
            digest = item.get("sha256")
            if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                raise ValueError("document engine bundle file entry is invalid")
            member = _safe_bundle_member(name)
            if member.as_posix() in expected:
                raise ValueError("document engine bundle contains duplicate files")
            expected[member.as_posix()] = str(digest)

        extracted_bytes = 0
        for name, digest in expected.items():
            try:
                info = archive.getinfo(name)
            except KeyError as exc:
                raise ValueError(f"document engine bundle is missing {name}") from exc
            if info.is_dir() or info.file_size > MAX_ENGINE_BUNDLE_BYTES:
                raise ValueError(f"document engine bundle entry is invalid: {name}")
            extracted_bytes += info.file_size
            if extracted_bytes > MAX_ENGINE_BUNDLE_BYTES:
                raise ValueError("document engine bundle expands beyond 2 GiB")
            target = destination / _safe_bundle_member(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if _sha256(target) != digest:
                raise ValueError(f"document engine bundle checksum failed: {name}")
    return manifest


def _create_engine_venv(stage: Path, notify: ProgressCallback) -> Path:
    notify("正在创建独立 Python 环境")
    created = _run_text_command(
        [sys.executable, "-m", "venv", str(stage / "venv")],
        capture_output=True,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or "unable to create document engine environment")
    return _venv_python(stage)


def _copy_engine_worker(stage: Path) -> None:
    source = Path(__file__).with_name("document_worker.py")
    if not source.is_file():
        raise RuntimeError("CrabCode document worker source is missing")
    shutil.copy2(source, _worker_path(stage))


def _asset_entries(assets: Path) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    total = 0
    for path in sorted(assets.rglob("*")):
        if not path.is_file() or path.name == "asset-manifest.json":
            continue
        size = path.stat().st_size
        total += size
        entries.append({
            "path": path.relative_to(assets).as_posix(),
            "sha256": _sha256(path),
            "size": size,
        })
    if not entries:
        raise RuntimeError("BabelDOC did not download any runtime assets")
    return entries, total


def _write_asset_manifest(assets: Path) -> int:
    entries, total = _asset_entries(assets)
    manifest = {
        "schema_version": 1,
        "engine_version": BABELDOC_VERSION,
        "asset_version": BABELDOC_VERSION,
        "files": entries,
    }
    (assets / "asset-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return total


def _publish_engine_stage(stage: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}-backup")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _install_document_engine_from_official_source(
    target: Path,
    parent: Path,
    notify: ProgressCallback,
) -> None:
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-install-", dir=parent))
    try:
        engine_python = _create_engine_venv(stage, notify)
        notify("正在从 BabelDOC 官方源安装程序与依赖")
        install = _run_text_command(
            [
                str(engine_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                f"BabelDOC=={BABELDOC_VERSION}",
            ],
            capture_output=True,
        )
        if install.returncode != 0:
            raise RuntimeError(install.stderr.strip() or "BabelDOC installation failed")

        assets = stage / "assets"
        assets.mkdir()
        notify("正在下载并校验 BabelDOC 官方模型与字体")
        warmup = _run_text_command(
            [
                str(engine_python),
                "-c",
                (
                    "import os, sys; from pathlib import Path; "
                    "assets=Path(sys.argv[1]); "
                    "import babeldoc.const as const; "
                    "const.CACHE_FOLDER=assets; "
                    "const.TIKTOKEN_CACHE_FOLDER=assets/'tiktoken'; "
                    "const.TIKTOKEN_CACHE_FOLDER.mkdir(parents=True, exist_ok=True); "
                    "os.environ['TIKTOKEN_CACHE_DIR']=str(const.TIKTOKEN_CACHE_FOLDER); "
                    "from babeldoc.assets.assets import warmup; warmup()"
                ),
                str(assets),
            ],
            capture_output=True,
        )
        if warmup.returncode != 0:
            raise RuntimeError(warmup.stderr.strip() or warmup.stdout.strip() or "BabelDOC asset download failed")

        notify("正在校验高精度 PDF 引擎")
        asset_bytes = _write_asset_manifest(assets)
        _copy_engine_worker(stage)
        manifest = {
            "schema_version": 1,
            "engine": "precise",
            "engine_version": BABELDOC_VERSION,
            "asset_version": BABELDOC_VERSION,
            "protocol_version": ENGINE_PROTOCOL_VERSION,
            "python_tag": _python_tag(),
            "platform_tag": _platform_tag(),
            "install_source": "official",
            "download_bytes": asset_bytes,
        }
        (stage / ENGINE_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        notify("正在启用高精度 PDF 引擎")
        _publish_engine_stage(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _install_document_engine(
    bundle_source: str | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    notify = progress or (lambda _message: None)
    if not ((3, 10) <= sys.version_info[:2] < (3, 14)):
        raise RuntimeError("BabelDOC requires Python 3.10 through 3.13")
    target = document_engine_root()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    source = bundle_source or os.environ.get("CRABCODE_DOCUMENT_ENGINE_BUNDLE_URL", "").strip()
    if not source:
        _install_document_engine_from_official_source(target, parent, notify)
        return {"installed": True}
    with tempfile.TemporaryDirectory(prefix="crabcode-document-engine-") as temp_name:
        temp = Path(temp_name)
        bundle = temp / "engine.zip"
        _download_bundle(source, bundle, notify)
        extracted = temp / "bundle"
        extracted.mkdir()
        notify("正在校验高精度 PDF 引擎")
        bundle_manifest = _extract_and_verify_bundle(bundle, extracted)
        wheelhouse = extracted / "wheelhouse"
        assets = extracted / "assets"
        if not wheelhouse.is_dir() or not assets.is_dir():
            raise ValueError("document engine bundle is missing wheelhouse or assets")

        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-install-", dir=parent))
        try:
            engine_python = _create_engine_venv(stage, notify)
            notify("正在安装高精度 PDF 引擎")
            install = _run_text_command(
                [
                    str(engine_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheelhouse),
                    f"BabelDOC=={BABELDOC_VERSION}",
                ],
                capture_output=True,
            )
            if install.returncode != 0:
                raise RuntimeError(install.stderr.strip() or "offline engine installation failed")
            install_crabcode = _run_text_command(
                [
                    str(engine_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--find-links",
                    str(wheelhouse),
                    f"crabcode=={CRABCODE_VERSION}",
                ],
                capture_output=True,
            )
            if install_crabcode.returncode != 0:
                raise RuntimeError(install_crabcode.stderr.strip() or "offline CrabCode worker installation failed")
            shutil.copytree(assets, stage / "assets")
            _copy_engine_worker(stage)
            _json = {
                "schema_version": 1,
                "engine": "precise",
                "engine_version": BABELDOC_VERSION,
                "asset_version": bundle_manifest.get("asset_version") or BABELDOC_VERSION,
                "protocol_version": ENGINE_PROTOCOL_VERSION,
                "python_tag": _python_tag(),
                "platform_tag": _platform_tag(),
                "bundle_sha256": _sha256(bundle),
                "bundle_bytes": bundle.stat().st_size,
                "install_source": "offline_bundle",
            }
            (stage / ENGINE_MANIFEST_NAME).write_text(
                json.dumps(_json, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            notify("正在启用高精度 PDF 引擎")
            _publish_engine_stage(stage, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    return {"installed": True}


def install_document_engine(
    bundle_source: str | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    target = document_engine_root()
    target.parent.mkdir(parents=True, exist_ok=True)
    state_path = target.parent / ".install-state.json"
    notify = progress or (lambda _message: None)

    def tracked(message: str) -> None:
        status = (
            "downloading"
            if "下载" in message
            else "verifying"
            if "校验" in message
            else "installing"
        )
        state_path.write_text(
            json.dumps({"status": status, "detail": message}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        notify(message)

    tracked("正在下载高精度 PDF 引擎")
    try:
        _install_document_engine(bundle_source, progress=tracked)
    except Exception as exc:
        state_path.unlink(missing_ok=True)
        current = document_engine_status()
        if current["status"] != "ready":
            state_path.write_text(
                json.dumps({"status": "broken", "detail": str(exc)}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        raise
    state_path.unlink(missing_ok=True)
    return document_engine_status()


def remove_document_engine() -> dict[str, Any]:
    target = document_engine_root()
    expected_parent = target.parent
    if target.name != BABELDOC_VERSION or expected_parent.name != "babeldoc":
        raise RuntimeError("refusing to remove an unexpected document engine path")
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    (target.parent / ".install-state.json").unlink(missing_ok=True)
    return document_engine_status()
