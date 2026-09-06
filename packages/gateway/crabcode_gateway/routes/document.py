"""Managed document-project import and artifact APIs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl
from pypdf import PdfReader

from crabcode_core.path_validation import validate_path_component
from crabcode_core.filesystem import replace_with_retry
from crabcode_core.subprocess_utils import (
    decode_subprocess_output,
    managed_process_command,
    subprocess_group_options,
    terminate_process_tree,
)
from crabcode_gateway.schemas import WorkspaceInfo


router = APIRouter(prefix="/document", tags=["document"])

MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_LAYOUT_BYTES = 50 * 1024 * 1024
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx",
    ".txt", ".md", ".html", ".htm",
}
OFFICE_EXTENSIONS = SUPPORTED_EXTENSIONS - {".pdf"}
MANIFEST_RELATIVE_PATH = Path(".crabcode/document/manifest.json")
_MANIFEST_LOCKS_GUARD = threading.Lock()
_MANIFEST_LOCKS: dict[str, threading.RLock] = {}

MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
}


class DocumentUrlImportRequest(BaseModel):
    workspace_path: str = Field(min_length=1)
    project_id: str = Field(min_length=1, max_length=200)
    project_name: str = Field(min_length=1, max_length=200)
    url: HttpUrl


class DocumentLayoutRequest(BaseModel):
    fingerprint: str = Field(min_length=1, max_length=500)
    page_count: int = Field(ge=1, le=100_000)
    pages: list[dict[str, Any]]


class DocumentBlogWriteRequest(BaseModel):
    revision: str | None = None
    markdown: str = Field(max_length=10_000_000)
    language: str = Field(default="", max_length=50)


class DocumentAnnotationRect(BaseModel):
    page: int = Field(ge=1, le=100_000)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class DocumentAnnotationWriteRequest(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    label: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=20_000)
    text: str = Field(min_length=1, max_length=50_000)
    rects: list[DocumentAnnotationRect] = Field(min_length=1, max_length=1_000)
    created_at: str = Field(default="", max_length=100)
    updated_at: str = Field(default="", max_length=100)


@contextmanager
def _manifest_lock(workspace: Path):
    key = os.path.normcase(str(workspace.resolve(strict=False)))
    with _MANIFEST_LOCKS_GUARD:
        lock = _MANIFEST_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _workspace_roots(request: Request) -> tuple[Path, ...]:
    info: WorkspaceInfo = request.app.state.workspace_info
    return tuple(Path(value).resolve() for value in info.browse_roots)


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    normalized = os.path.normcase(str(path))
    for root in roots:
        try:
            if os.path.commonpath((normalized, os.path.normcase(str(root)))) == os.path.normcase(str(root)):
                return True
        except ValueError:
            continue
    return False


def _validated_new_workspace(value: str, roots: tuple[Path, ...]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or not candidate.name:
        raise HTTPException(status_code=400, detail="workspace_path must name an absolute directory")
    candidate = candidate.resolve(strict=False)
    if not _is_within(candidate, roots):
        raise HTTPException(status_code=403, detail="Document workspace is outside the allowed browse roots")
    if candidate.exists():
        base = candidate
        for index in range(2, 10_000):
            candidate = base.with_name(f"{base.name}-{index}")
            if not candidate.exists():
                break
        else:
            raise HTTPException(status_code=409, detail="Unable to find an unused document workspace name")
    ancestor = candidate.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    try:
        resolved_ancestor = ancestor.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to resolve document workspace parent") from exc
    if not resolved_ancestor.is_dir() or not _is_within(resolved_ancestor, roots):
        raise HTTPException(status_code=403, detail="Document workspace parent is not allowed")
    return candidate


def _validated_workspace(value: str, roots: tuple[Path, ...]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="workspace must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Document workspace not found") from exc
    if not resolved.is_dir() or not _is_within(resolved, roots):
        raise HTTPException(status_code=403, detail="Document workspace is not allowed")
    manifest = resolved / MANIFEST_RELATIVE_PATH
    if not manifest.is_file() or manifest.is_symlink():
        raise HTTPException(status_code=404, detail="Document manifest not found")
    return resolved


def _safe_name(value: str, fallback: str = "document") -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", name).rstrip(". ")
    name = name[:180] or fallback
    try:
        validate_path_component(name, "document filename")
    except ValueError:
        name = f"_{name}"[:180]
    return name


def _file_extension(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported document format: {extension or 'unknown'}")
    return extension


def _converter() -> str | None:
    override = os.environ.get("CRABCODE_LIBREOFFICE_PATH", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if executable or os.name != "nt":
        return executable
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidate = Path(root) / "LibreOffice" / "program" / "soffice.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_write(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def _text_write(path: Path, value: str) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"))


def _read_manifest(workspace: Path) -> dict[str, Any]:
    try:
        value = json.loads((workspace / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Document manifest is invalid") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=500, detail="Document manifest is invalid")
    return value


def _validate_signature(path: Path, extension: str) -> None:
    with path.open("rb") as handle:
        prefix = handle.read(8192)
    if extension == ".pdf" and b"%PDF-" not in prefix[:1024]:
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF")
    if extension in {".docx", ".odt", ".pptx"} and not prefix.startswith(b"PK"):
        raise HTTPException(status_code=415, detail="The uploaded file does not match its format")
    if extension in {".docx", ".odt", ".pptx"}:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                valid = (
                    extension == ".docx" and "word/document.xml" in names
                    or extension == ".pptx" and "ppt/presentation.xml" in names
                    or extension == ".odt" and "content.xml" in names and "META-INF/manifest.xml" in names
                )
        except (OSError, zipfile.BadZipFile):
            valid = False
        if not valid:
            raise HTTPException(status_code=415, detail="The uploaded archive does not match its document format")
    if extension in {".doc", ".ppt"} and not prefix.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        raise HTTPException(status_code=415, detail="The uploaded file does not match its format")
    if extension == ".rtf" and not prefix.lstrip().startswith(b"{\\rtf"):
        raise HTTPException(status_code=415, detail="The uploaded file is not valid RTF")
    if extension in {".txt", ".md", ".html", ".htm"} and b"\x00" in prefix:
        raise HTTPException(status_code=415, detail="The uploaded text document contains binary data")


def _validate_mime(extension: str, content_type: str) -> None:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type or media_type == "application/octet-stream":
        return
    expected = MIME_EXTENSIONS.get(media_type)
    compatible_text = extension in {".txt", ".md", ".html", ".htm"} and media_type.startswith("text/")
    compatible_zip = extension in {".docx", ".odt", ".pptx"} and media_type in {"application/zip", "application/x-zip-compressed"}
    if expected not in {extension, ".html" if extension == ".htm" else extension} and not compatible_text and not compatible_zip:
        raise HTTPException(status_code=415, detail="Document MIME type does not match its filename")


def _pdf_page_count(path: Path) -> int:
    try:
        count = len(PdfReader(path, strict=False).pages)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The converted PDF is invalid or encrypted") from exc
    if count < 1:
        raise HTTPException(status_code=422, detail="The converted PDF has no pages")
    return count


def _plain_html(text: str) -> str:
    return (
        "<!doctype html><meta charset=\"utf-8\"><style>"
        "body{font:12pt/1.55 sans-serif;max-width:48rem;margin:2.5rem auto;}"
        "pre{white-space:pre-wrap;font:inherit}</style><pre>"
        f"{html.escape(text)}"
        "</pre>"
    )


def _prepare_conversion_input(source: Path, extension: str, internal: Path) -> Path:
    if extension not in {".txt", ".md", ".html", ".htm"}:
        return source
    text = source.read_text(encoding="utf-8", errors="replace")
    # Treat imported HTML as text. This deliberately prevents external resource
    # loads and local-file references during headless conversion.
    prepared = internal / "safe-source.html"
    prepared.write_text(_plain_html(text), encoding="utf-8")
    return prepared


async def _convert_to_pdf(source: Path, extension: str, internal: Path) -> Path:
    rendered = internal / "rendered.pdf"
    if extension == ".pdf":
        shutil.copy2(source, rendered)
        return rendered
    converter = _converter()
    if not converter:
        raise HTTPException(status_code=503, detail="LibreOffice is required to convert this document format")
    conversion_input = _prepare_conversion_input(source, extension, internal)
    output_dir = internal / "conversion"
    output_dir.mkdir()
    profile_dir = internal / "libreoffice-profile"
    profile_uri = profile_dir.resolve().as_uri()
    process = await asyncio.create_subprocess_exec(
        *managed_process_command([
            converter,
            "--headless",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(conversion_input),
        ]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_group_options(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except asyncio.TimeoutError as exc:
        await terminate_process_tree(process)
        raise HTTPException(status_code=504, detail="Document conversion timed out") from exc
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
    candidates = list(output_dir.glob("*.pdf"))
    if process.returncode != 0 or len(candidates) != 1:
        detail = decode_subprocess_output(stderr or stdout).strip()[-500:]
        raise HTTPException(status_code=422, detail=f"Document conversion failed: {detail or 'no PDF produced'}")
    shutil.move(str(candidates[0]), rendered)
    return rendered


async def _write_upload(upload: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOCUMENT_BYTES:
                raise HTTPException(status_code=413, detail="Document exceeds the 100MiB limit")
            handle.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Document is empty")
    return total


def _assert_public_host(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URL must be an unauthenticated HTTP or HTTPS address")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to resolve document URL") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise HTTPException(status_code=400, detail="Document URL resolves to a private or local address")


async def _download(url: str, destination: Path) -> tuple[str, str, int]:
    current = url
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for redirect in range(6):
            try:
                await asyncio.wait_for(asyncio.to_thread(_assert_public_host, current), timeout=10)
            except asyncio.TimeoutError as exc:
                raise HTTPException(status_code=400, detail="Document URL DNS lookup timed out") from exc
            async with client.stream("GET", current, headers={"Accept": "application/pdf,application/*;q=.9,text/*;q=.5"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect == 5:
                        raise HTTPException(status_code=400, detail="Too many or invalid document redirects")
                    current = urljoin(current, location)
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise HTTPException(status_code=400, detail=f"Document download failed ({response.status_code})")
                advertised = response.headers.get("content-length")
                if advertised and advertised.isdigit() and int(advertised) > MAX_DOCUMENT_BYTES:
                    raise HTTPException(status_code=413, detail="Document exceeds the 100MiB limit")
                total = 0
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOCUMENT_BYTES:
                            raise HTTPException(status_code=413, detail="Document exceeds the 100MiB limit")
                        handle.write(chunk)
                if total == 0:
                    raise HTTPException(status_code=400, detail="Downloaded document is empty")
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                parsed = urlsplit(str(response.url))
                disposition = response.headers.get("content-disposition", "")
                encoded_name = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
                plain_name = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
                candidate_name = unquote(encoded_name.group(1)) if encoded_name else plain_name.group(1) if plain_name else Path(parsed.path).name
                filename = _safe_name(unquote(candidate_name), "document")
                if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    inferred = MIME_EXTENSIONS.get(content_type.lower())
                    if inferred:
                        filename = f"{filename}{inferred}"
                return filename, content_type, total
    raise HTTPException(status_code=400, detail="Unable to download document")


def _redacted_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


async def _finish_import(
    *,
    request: Request,
    workspace_path: str,
    project_id: str,
    project_name: str,
    filename: str,
    content_type: str,
    origin: Literal["upload", "url"],
    source_url: str | None,
    writer,
) -> dict[str, Any]:
    if not project_name.strip() or len(project_name) > 200:
        raise HTTPException(status_code=400, detail="project_name must be between 1 and 200 characters")
    if not project_id.strip() or len(project_id) > 200:
        raise HTTPException(status_code=400, detail="project_id must be between 1 and 200 characters")
    roots = _workspace_roots(request)
    workspace = _validated_new_workspace(workspace_path, roots)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    stage = workspace.parent / f".{workspace.name}.import-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        source_dir = stage / "source"
        internal = stage / ".crabcode" / "document"
        source_dir.mkdir(parents=True)
        internal.mkdir(parents=True)
        safe_filename = _safe_name(filename)
        extension = _file_extension(safe_filename)
        _validate_mime(extension, content_type)
        source = source_dir / safe_filename
        size = await writer(source)
        _validate_signature(source, extension)
        rendered = await _convert_to_pdf(source, extension, internal)
        page_count = await asyncio.to_thread(_pdf_page_count, rendered)
        now = datetime.now(timezone.utc).isoformat()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "project_id": project_id,
            "project_name": project_name.strip(),
            "workspace": str(workspace),
            "source": {
                "origin": origin,
                "name": safe_filename,
                "path": source.relative_to(stage).as_posix(),
                "url": _redacted_url(source_url) if source_url else None,
                "content_type": content_type or mimetypes.guess_type(safe_filename)[0] or "application/octet-stream",
                "size": size,
                "sha256": _sha256(source),
            },
            "pdf": {
                "path": rendered.relative_to(stage).as_posix(),
                "sha256": _sha256(rendered),
                "page_count": page_count,
            },
            "layout": None,
            "translations": {},
            "blog": None,
            "jobs": {},
            "created_at": now,
            "updated_at": now,
        }
        _json_write(stage / MANIFEST_RELATIVE_PATH, manifest)
        replace_with_retry(stage, workspace)
        return manifest
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


@router.get("/capabilities")
async def document_capabilities(request: Request) -> dict[str, Any]:
    from crabcode_gateway.document_engine import document_engine_status

    converter = _converter()
    precise = await asyncio.to_thread(document_engine_status)
    info: WorkspaceInfo = request.app.state.workspace_info
    available = [".pdf", *sorted(OFFICE_EXTENSIONS)] if converter else [".pdf"]
    return {
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "available_extensions": available,
        "max_bytes": MAX_DOCUMENT_BYTES,
        "documents_dir": info.documents_dir,
        "libreoffice": {"available": bool(converter), "executable": converter},
        "ocr": {"available": False},
        "translation_engines": {
            "default": "precise" if precise["available"] else "legacy",
            "legacy": {"available": True, "status": "ready"},
            "precise": precise,
        },
    }


@router.post("/import/upload", status_code=201)
async def import_document_upload(
    request: Request,
    workspace_path: str = Form(...),
    project_id: str = Form(...),
    project_name: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    filename = _safe_name(file.filename or "document")

    async def writer(path: Path) -> int:
        return await _write_upload(file, path)

    try:
        return await _finish_import(
            request=request,
            workspace_path=workspace_path,
            project_id=project_id,
            project_name=project_name,
            filename=filename,
            content_type=file.content_type or "",
            origin="upload",
            source_url=None,
            writer=writer,
        )
    finally:
        await file.close()


@router.post("/import/url", status_code=201)
async def import_document_url(req: DocumentUrlImportRequest, request: Request) -> dict[str, Any]:
    # Download into the source path selected from the URL. Resolve the filename
    # before building the managed workspace so the original extension is kept.
    with tempfile.TemporaryDirectory(prefix="crabcode-document-download-") as temp_dir:
        download_path = Path(temp_dir) / "download"
        filename, content_type, size = await _download(str(req.url), download_path)

        async def copy_writer(path: Path) -> int:
            shutil.copy2(download_path, path)
            return size

        return await _finish_import(
            request=request,
            workspace_path=req.workspace_path,
            project_id=req.project_id,
            project_name=req.project_name,
            filename=filename,
            content_type=content_type,
            origin="url",
            source_url=str(req.url),
            writer=copy_writer,
        )


@router.get("/manifest")
async def document_manifest(workspace: str, request: Request) -> dict[str, Any]:
    return _read_manifest(_validated_workspace(workspace, _workspace_roots(request)))


@router.get("/asset")
async def document_asset(
    workspace: str,
    request: Request,
    kind: Literal["pdf", "source"] = "pdf",
) -> FileResponse:
    root = _validated_workspace(workspace, _workspace_roots(request))
    manifest = _read_manifest(root)
    relative = manifest.get(kind, {}).get("path")
    if not isinstance(relative, str):
        raise HTTPException(status_code=404, detail="Document asset not found")
    candidate = root / relative
    if candidate.is_symlink():
        raise HTTPException(status_code=403, detail="Document asset is invalid")
    try:
        path = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail="Document asset not found") from exc
    if not _is_within(path, (root,)) or not path.is_file():
        raise HTTPException(status_code=403, detail="Document asset is invalid")
    media_type = "application/pdf" if kind == "pdf" else manifest.get("source", {}).get("content_type")
    response = FileResponse(path, media_type=media_type, filename=path.name)
    response.headers["Accept-Ranges"] = "bytes"
    digest = manifest.get(kind, {}).get("sha256")
    response.headers["ETag"] = f'"{digest if isinstance(digest, str) and digest else _sha256(path)}"'
    return response


@router.get("/blog-asset")
async def document_blog_asset(workspace: str, path: str, request: Request) -> FileResponse:
    root = _validated_workspace(workspace, _workspace_roots(request))
    assets = root / "blog-assets"
    try:
        resolved_assets = assets.resolve(strict=True)
        candidate = (assets / path).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail="Blog asset not found") from exc
    if (
        assets.is_symlink()
        or not resolved_assets.is_dir()
        or not _is_within(resolved_assets, (root,))
        or not _is_within(candidate, (resolved_assets,))
        or not candidate.is_file()
        or candidate.is_symlink()
    ):
        raise HTTPException(status_code=403, detail="Blog asset is invalid")
    return FileResponse(candidate, media_type=mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")


@router.put("/layout")
async def save_document_layout(req: DocumentLayoutRequest, workspace: str, request: Request) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    encoded = json.dumps(req.model_dump(), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_LAYOUT_BYTES:
        raise HTTPException(status_code=413, detail="Document layout exceeds the 50MiB limit")
    try:
        internal = _managed_internal_directory(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    with _manifest_lock(root):
        return _save_document_layout_locked(root, internal, req)


def _document_annotations_path(root: Path) -> Path:
    try:
        internal = _managed_internal_directory(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    path = internal / "annotations.json"
    if path.is_symlink():
        raise HTTPException(status_code=403, detail="Document annotations path is invalid")
    return path


def _read_document_annotations_locked(root: Path) -> list[dict[str, Any]]:
    path = _document_annotations_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Document annotations are invalid") from exc
    if not isinstance(value, list):
        raise HTTPException(status_code=500, detail="Document annotations are invalid")
    try:
        return [DocumentAnnotationWriteRequest.model_validate(item).model_dump() for item in value]
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Document annotations are invalid") from exc


@router.get("/annotations")
async def read_document_annotations(workspace: str, request: Request) -> list[dict[str, Any]]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    with _manifest_lock(root):
        return _read_document_annotations_locked(root)


@router.put("/annotations")
async def write_document_annotation(
    req: DocumentAnnotationWriteRequest,
    workspace: str,
    request: Request,
) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    with _manifest_lock(root):
        annotations = _read_document_annotations_locked(root)
        now = datetime.now(timezone.utc).isoformat()
        previous = next((item for item in annotations if item["id"] == req.id), None)
        annotation = req.model_dump()
        annotation["created_at"] = previous.get("created_at") if previous else now
        annotation["updated_at"] = now
        next_annotations = [item for item in annotations if item["id"] != req.id]
        next_annotations.append(annotation)
        _json_write(_document_annotations_path(root), next_annotations)
        return annotation


@router.delete("/annotations/{annotation_id}", status_code=204)
async def delete_document_annotation(
    annotation_id: str,
    workspace: str,
    request: Request,
) -> Response:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", annotation_id):
        raise HTTPException(status_code=400, detail="Invalid annotation id")
    root = _validated_workspace(workspace, _workspace_roots(request))
    with _manifest_lock(root):
        annotations = _read_document_annotations_locked(root)
        next_annotations = [item for item in annotations if item["id"] != annotation_id]
        if len(next_annotations) == len(annotations):
            raise HTTPException(status_code=404, detail="Document annotation not found")
        _json_write(_document_annotations_path(root), next_annotations)
    return Response(status_code=204)


def _save_document_layout_locked(
    root: Path,
    internal: Path,
    req: DocumentLayoutRequest,
) -> dict[str, Any]:
    layout_path = internal / "layout.json"
    content_path = internal / "content.md"
    if content_path.is_symlink():
        raise HTTPException(status_code=403, detail="Document content path is invalid")
    lines: list[str] = []
    text_pages = 0
    for page_number, page in enumerate(req.pages, 1):
        blocks = page.get("blocks") if isinstance(page, dict) else None
        page_lines = [str(block.get("text", "")).strip() for block in (blocks or []) if isinstance(block, dict)]
        page_lines = [line for line in page_lines if line]
        if page_lines:
            text_pages += 1
            lines.extend([f"\n<!-- page:{page_number} -->\n", *page_lines])
    _json_write(layout_path, req.model_dump())
    _text_write(content_path, "\n\n".join(lines).strip() + "\n")
    manifest = _read_manifest(root)
    previous_fingerprint = (manifest.get("layout") or {}).get("fingerprint")
    if previous_fingerprint and previous_fingerprint != req.fingerprint:
        translations = internal / "translations"
        if translations.is_symlink():
            translations.unlink(missing_ok=True)
        elif translations.is_dir():
            # A browser layout change invalidates only legacy block overlays.
            # Precise PDFs are derived from the source PDF and remain valid.
            for legacy_path in translations.glob("*.json"):
                if legacy_path.is_file() and not legacy_path.is_symlink():
                    legacy_path.unlink(missing_ok=True)
        (root / "blog.md").unlink(missing_ok=True)
        current_translations = manifest.get("translations")
        manifest["translations"] = {
            locale: entry
            for locale, entry in (current_translations.items() if isinstance(current_translations, dict) else [])
            if isinstance(entry, dict) and entry.get("engine") == "precise"
        }
        manifest["blog"] = None
    manifest["layout"] = {
        "path": layout_path.relative_to(root).as_posix(),
        "fingerprint": req.fingerprint,
        "text_pages": text_pages,
        "scanned_pages": req.page_count - text_pages,
    }
    manifest.setdefault("pdf", {})["page_count"] = req.page_count
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _json_write(root / MANIFEST_RELATIVE_PATH, manifest)
    return manifest["layout"]


@router.get("/translation")
async def document_translation(workspace: str, locale: str, request: Request) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", locale):
        raise HTTPException(status_code=400, detail="Invalid translation locale")
    try:
        internal = _managed_internal_directory(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    with _manifest_lock(root):
        return _read_document_translation_locked(root, internal, locale)


@router.delete("/translation")
async def clear_document_translation(workspace: str, locale: str, request: Request) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", locale):
        raise HTTPException(status_code=400, detail="Invalid translation locale")
    try:
        internal = _managed_internal_directory(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    with _manifest_lock(root):
        return _clear_document_translation_locked(root, internal, locale)


def _read_document_translation_locked(root: Path, internal: Path, locale: str) -> dict[str, Any]:
    translations = internal / "translations"
    if translations.is_symlink():
        raise HTTPException(status_code=403, detail="Managed translations directory is invalid")
    manifest = _read_manifest(root)
    entries = manifest.get("translations")
    entry = entries.get(locale) if isinstance(entries, dict) else None
    if isinstance(entry, dict) and entry.get("engine") == "precise":
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise HTTPException(status_code=500, detail="Precise translation metadata is invalid")
        candidate = root / relative
        if candidate.is_symlink():
            raise HTTPException(status_code=403, detail="Precise translation path is invalid")
        try:
            path = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=404, detail="Translation not found") from exc
        if not _is_within(path, (root.resolve(strict=True),)) or not path.is_file():
            raise HTTPException(status_code=403, detail="Precise translation path is invalid")
        source_sha256 = str(entry.get("source_sha256") or "")
        if source_sha256 != str(manifest.get("source", {}).get("sha256") or ""):
            raise HTTPException(status_code=409, detail="Translation is stale for the current source document")
        digest = str(entry.get("pdf_sha256") or entry.get("sha256") or "")
        if digest and _sha256(path) != digest:
            raise HTTPException(status_code=500, detail="Precise translation checksum failed")
        return {
            "engine": "precise",
            "locale": locale,
            "source_sha256": source_sha256,
            "pdf_sha256": digest or _sha256(path),
            "page_count": int(entry.get("page_count") or 0),
            "engine_version": str(entry.get("engine_version") or ""),
            "warnings": entry.get("warnings") if isinstance(entry.get("warnings"), list) else [],
        }
    path = translations / f"{locale}.json"
    if path.is_symlink():
        raise HTTPException(status_code=403, detail="Translation path is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Translation not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Translation is invalid") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=500, detail="Translation is invalid")
    if value.get("source_sha256") != manifest.get("source", {}).get("sha256"):
        raise HTTPException(status_code=409, detail="Translation is stale for the current source document")
    if value.get("layout_fingerprint") != (manifest.get("layout") or {}).get("fingerprint"):
        raise HTTPException(status_code=409, detail="Translation is stale for the current document layout")
    return {**value, "engine": "legacy"}


@router.get("/translation/asset")
async def document_translation_asset(workspace: str, locale: str, request: Request) -> FileResponse:
    root = _validated_workspace(workspace, _workspace_roots(request))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", locale):
        raise HTTPException(status_code=400, detail="Invalid translation locale")
    with _manifest_lock(root):
        manifest = _read_manifest(root)
        entries = manifest.get("translations")
        entry = entries.get(locale) if isinstance(entries, dict) else None
        if not isinstance(entry, dict) or entry.get("engine") != "precise":
            raise HTTPException(status_code=404, detail="Precise translation not found")
        if entry.get("source_sha256") != manifest.get("source", {}).get("sha256"):
            raise HTTPException(status_code=409, detail="Translation is stale for the current source document")
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise HTTPException(status_code=500, detail="Precise translation metadata is invalid")
        candidate = root / relative
        if candidate.is_symlink():
            raise HTTPException(status_code=403, detail="Precise translation path is invalid")
        try:
            path = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=404, detail="Precise translation not found") from exc
        if not _is_within(path, (root.resolve(strict=True),)) or not path.is_file():
            raise HTTPException(status_code=403, detail="Precise translation path is invalid")
        actual_digest = _sha256(path)
        digest = str(entry.get("pdf_sha256") or entry.get("sha256") or "")
        if digest and digest != actual_digest:
            raise HTTPException(status_code=500, detail="Precise translation checksum failed")
        digest = digest or actual_digest
    response = FileResponse(path, media_type="application/pdf", filename=f"translated-{locale}.pdf")
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["ETag"] = f'"{digest}"'
    return response


def _clear_document_translation_locked(root: Path, internal: Path, locale: str) -> dict[str, Any]:
    manifest = _read_manifest(root)
    jobs = manifest.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    matching_job_ids = {
        operation_id
        for operation_id, job in jobs.items()
        if isinstance(operation_id, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id)
        and isinstance(job, dict)
        and job.get("action") == "translate"
        and job.get("locale") == locale
    }
    if any(
        isinstance(job, dict)
        and job.get("action") == "translate"
        and job.get("locale") == locale
        and job.get("status") == "running"
        for job in jobs.values()
    ):
        raise HTTPException(status_code=409, detail="Translation is still running")

    jobs_dir = internal / "jobs"
    if jobs_dir.is_symlink() or (jobs_dir.exists() and not jobs_dir.is_dir()):
        raise HTTPException(status_code=403, detail="Managed document jobs directory is invalid")
    if jobs_dir.is_dir():
        for job_dir in jobs_dir.iterdir():
            if job_dir.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_dir.name):
                continue
            staged = job_dir / "translation.json"
            precise_cache = job_dir / "precise-cache.json"
            for candidate in (staged, precise_cache):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                value = _read_json_file(candidate)
                if isinstance(value, dict) and value.get("locale") == locale:
                    matching_job_ids.add(job_dir.name)
                    break

    translations_dir = internal / "translations"
    if translations_dir.is_symlink() or (translations_dir.exists() and not translations_dir.is_dir()):
        raise HTTPException(status_code=403, detail="Managed translations directory is invalid")
    translation_path = translations_dir / f"{locale}.json"
    if translation_path.is_symlink() or (translation_path.exists() and not translation_path.is_file()):
        raise HTTPException(status_code=403, detail="Translation path is invalid")
    precise_path = translations_dir / locale
    if precise_path.is_symlink() or (precise_path.exists() and not precise_path.is_dir()):
        raise HTTPException(status_code=403, detail="Precise translation directory is invalid")
    removed_translation = translation_path.is_file() or precise_path.is_dir()
    translation_path.unlink(missing_ok=True)
    if precise_path.is_dir():
        shutil.rmtree(precise_path)

    for operation_id in matching_job_ids:
        _cleanup_document_job(root, operation_id)

    translations = manifest.get("translations")
    if not isinstance(translations, dict):
        translations = {}
    translations.pop(locale, None)
    manifest["translations"] = translations
    manifest["jobs"] = {
        operation_id: job
        for operation_id, job in jobs.items()
        if operation_id not in matching_job_ids
    }
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _json_write(root / MANIFEST_RELATIVE_PATH, manifest)
    return {
        "locale": locale,
        "removed_translation": removed_translation,
        "removed_jobs": len(matching_job_ids),
    }


def _blog_revision(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _managed_internal_directory(workspace: Path) -> Path:
    try:
        resolved_workspace = workspace.resolve(strict=True)
    except OSError as exc:
        raise ValueError("document workspace is unavailable") from exc
    crabcode = workspace / ".crabcode"
    internal = crabcode / "document"
    if crabcode.is_symlink() or internal.is_symlink():
        raise ValueError("managed document directories cannot be symlinks")
    try:
        resolved_internal = internal.resolve(strict=True)
    except OSError as exc:
        raise ValueError("managed document directory is unavailable") from exc
    if not resolved_internal.is_dir() or not _is_within(resolved_internal, (resolved_workspace,)):
        raise ValueError("managed document directory is outside the workspace")
    return internal


def _document_job_directory(workspace: Path, operation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
        raise ValueError("invalid document operation id")
    internal = _managed_internal_directory(workspace)
    jobs = internal / "jobs"
    if jobs.is_symlink() or not _is_within(jobs.resolve(strict=False), (workspace.resolve(strict=True),)):
        raise ValueError("document jobs directory is invalid")
    return jobs / operation_id


def _document_job_total(workspace: Path, action: str, engine: str = "legacy") -> int:
    if action != "translate":
        return 1
    if engine == "precise":
        return 100
    return sum(len(page) for page in _translation_source_pages(workspace))


def _document_pdf_path(workspace: Path) -> Path:
    manifest = _read_manifest(workspace)
    relative = manifest.get("pdf", {}).get("path")
    if not isinstance(relative, str):
        raise ValueError("document PDF is unavailable")
    candidate = workspace / relative
    if candidate.is_symlink():
        raise ValueError("document PDF path is invalid")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("document PDF is unavailable") from exc
    if not path.is_file() or not _is_within(path, (workspace.resolve(strict=True),)):
        raise ValueError("document PDF path is invalid")
    return path


def _translation_source_pages(workspace: Path) -> list[list[tuple[str, str]]]:
    """Return exact non-empty layout blocks grouped by page."""
    try:
        internal = _managed_internal_directory(workspace)
        layout_path = internal / "layout.json"
        if layout_path.is_symlink():
            raise ValueError("document layout is invalid")
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        raw_pages = layout.get("pages")
        if not isinstance(raw_pages, list):
            raise ValueError("document layout is not ready")
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ValueError("document layout is not ready") from exc

    pages: list[list[tuple[str, str]]] = []
    seen: set[str] = set()
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict) or not isinstance(raw_page.get("blocks", []), list):
            raise ValueError("document layout contains invalid pages")
        page: list[tuple[str, str]] = []
        for block in raw_page.get("blocks", []):
            if not isinstance(block, dict):
                raise ValueError("document layout contains invalid blocks")
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id or block_id in seen:
                raise ValueError("document layout contains invalid or duplicate block ids")
            seen.add(block_id)
            page.append((block_id, text))
        pages.append(page)
    return pages


def _translation_preserved_blocks(workspace: Path) -> dict[str, str]:
    """Return fixed-layout blocks that must bypass model translation."""
    try:
        internal = _managed_internal_directory(workspace)
        layout_path = internal / "layout.json"
        if layout_path.is_symlink():
            raise ValueError("document layout is invalid")
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        raw_pages = layout.get("pages")
        if not isinstance(raw_pages, list):
            raise ValueError("document layout is not ready")
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ValueError("document layout is not ready") from exc

    preserved: dict[str, str] = {}
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict) or not isinstance(raw_page.get("blocks", []), list):
            raise ValueError("document layout contains invalid pages")
        for block in raw_page.get("blocks", []):
            if not isinstance(block, dict) or block.get("kind") not in {"formula", "graphic"}:
                continue
            block_id = block.get("id")
            text = block.get("text")
            if not isinstance(block_id, str) or not block_id or not isinstance(text, str) or not text.strip():
                raise ValueError("document layout contains invalid preserved blocks")
            preserved[block_id] = text
    return preserved


def _validated_translation_file(
    workspace: Path,
    path: Path,
    locale: str | None,
) -> dict[str, str]:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("translation job file is invalid")
    try:
        staged = json.loads(path.read_text(encoding="utf-8"))
        manifest = _read_manifest(workspace)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("translation job file is invalid") from exc
    if not isinstance(staged, dict):
        raise ValueError("translation job file is invalid")
    staged_locale = staged.get("locale")
    if locale is not None and staged_locale is not None and staged_locale != locale:
        raise ValueError("translation job locale does not match")
    if staged.get("source_sha256") != manifest.get("source", {}).get("sha256"):
        raise ValueError("translation was generated for a different source document")
    if staged.get("layout_fingerprint") != (manifest.get("layout") or {}).get("fingerprint"):
        raise ValueError("translation was generated for a different document layout")
    blocks = staged.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("translation JSON is missing blocks")

    expected_ids = {
        block_id
        for page in _translation_source_pages(workspace)
        for block_id, _ in page
    }
    translated: dict[str, str] = {}
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("translation blocks must contain id and translated_text")
        block_id = block.get("id")
        translated_text = block.get("translated_text")
        if not isinstance(block_id, str) or not isinstance(translated_text, str):
            raise ValueError("translation blocks must contain id and translated_text")
        if not translated_text.strip():
            raise ValueError(f"translation is empty for block id: {block_id}")
        if block_id in translated:
            raise ValueError(f"translation contains duplicate block id: {block_id}")
        if block_id not in expected_ids:
            raise ValueError(f"translation contains unknown block id: {block_id}")
        translated[block_id] = translated_text
    return translated


def _write_translation_mapping(
    workspace: Path,
    operation_id: str,
    locale: str,
    translated: dict[str, str],
) -> int:
    pages = _translation_source_pages(workspace)
    expected_ids = [block_id for page in pages for block_id, _ in page]
    expected = set(expected_ids)
    if not set(translated).issubset(expected):
        raise ValueError("translation contains unknown block ids")
    if any(not isinstance(text, str) or not text.strip() for text in translated.values()):
        raise ValueError("translation contains empty text")
    manifest = _read_manifest(workspace)
    target = _document_job_directory(workspace, operation_id) / "translation.json"
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("translation job file is invalid")
    _json_write(target, {
        "locale": locale,
        "source_sha256": manifest.get("source", {}).get("sha256"),
        "layout_fingerprint": (manifest.get("layout") or {}).get("fingerprint"),
        "blocks": [
            {"id": block_id, "translated_text": translated[block_id]}
            for block_id in expected_ids
            if block_id in translated
        ],
    })
    return len(translated)


def _translation_job_blocks(
    workspace: Path,
    operation_id: str,
    locale: str,
) -> dict[str, str]:
    target = _document_job_directory(workspace, operation_id) / "translation.json"
    if not target.is_file():
        return {}
    return _validated_translation_file(workspace, target, locale)


def _store_translation_batch(
    workspace: Path,
    operation_id: str,
    locale: str,
    batch: dict[str, str],
) -> int:
    """Validate and atomically merge one model-produced translation batch."""
    translated = _translation_job_blocks(workspace, operation_id, locale)
    translated.update(batch)
    return _write_translation_mapping(workspace, operation_id, locale, translated)


def _cleanup_document_job(workspace: Path, operation_id: str) -> None:
    try:
        job = _document_job_directory(workspace, operation_id)
    except ValueError:
        return
    if job.is_symlink():
        job.unlink(missing_ok=True)
    else:
        shutil.rmtree(job, ignore_errors=True)


def _translation_job_progress(workspace: Path, operation_id: str) -> int:
    path = _document_job_directory(workspace, operation_id) / "translation.json"
    if not path.is_file() or path.is_symlink():
        return 0
    try:
        return len(_validated_translation_file(workspace, path, None))
    except ValueError:
        return 0


def _recover_translation_job(
    workspace: Path,
    operation_id: str,
    locale: str,
    engine: str = "legacy",
) -> int:
    """Seed a new translation operation from the newest valid partial batch."""
    internal = _managed_internal_directory(workspace)
    jobs = internal / "jobs"
    if jobs.is_symlink():
        raise ValueError("document jobs directory is invalid")
    target = _document_job_directory(workspace, operation_id)
    target.mkdir(parents=True, exist_ok=True)
    if engine == "precise":
        manifest = _read_manifest(workspace)
        source_sha256 = str(manifest.get("source", {}).get("sha256") or "")
        candidates_with_time: list[tuple[float, Path]] = []
        for path in jobs.glob("*/precise-cache.json"):
            if path.parent.name == operation_id or path.is_symlink() or path.parent.is_symlink():
                continue
            try:
                candidates_with_time.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for _, candidate in sorted(candidates_with_time, reverse=True):
            value = _read_json_file(candidate)
            if (
                isinstance(value, dict)
                and value.get("locale") == locale
                and value.get("source_sha256") == source_sha256
                and value.get("engine_version") == "0.6.4"
                and isinstance(value.get("entries"), dict)
            ):
                shutil.copy2(candidate, target / "precise-cache.json")
                # Cached paragraph count is not a stable percentage because
                # BabelDOC has not parsed this run yet.
                return 0
        return 0
    _translation_source_pages(workspace)
    candidates_with_time: list[tuple[float, Path]] = []
    for path in jobs.glob("*/translation.json"):
        if path.parent.name == operation_id or path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            candidates_with_time.append((path.stat().st_mtime, path))
        except OSError:
            continue
    candidates = [path for _, path in sorted(candidates_with_time, reverse=True)]
    for candidate in candidates:
        try:
            translated = _validated_translation_file(workspace, candidate, locale)
        except ValueError:
            continue
        if translated:
            return _write_translation_mapping(workspace, operation_id, locale, translated)
    return 0


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _update_document_job_status(
    workspace: Path,
    operation_id: str,
    *,
    action: str,
    status: str,
    locale: str,
    source: str,
    current: int,
    total: int,
    message: str,
    language: str = "",
    engine: str = "legacy",
) -> None:
    with _manifest_lock(workspace):
        _update_document_job_status_unlocked(
            workspace,
            operation_id,
            action=action,
            status=status,
            locale=locale,
            source=source,
            current=current,
            total=total,
            message=message,
            language=language,
            engine=engine,
        )


def _update_document_job_status_unlocked(
    workspace: Path,
    operation_id: str,
    *,
    action: str,
    status: str,
    locale: str,
    source: str,
    current: int,
    total: int,
    message: str,
    language: str = "",
    engine: str = "legacy",
) -> None:
    _managed_internal_directory(workspace)
    manifest = _read_manifest(workspace)
    jobs = manifest.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    jobs[operation_id] = {
        "action": action,
        "status": status,
        "locale": locale,
        "language": language,
        "source": source,
        "engine": engine,
        "current": current,
        "total": total,
        "message": message[:500],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if len(jobs) > 50:
        jobs = dict(sorted(jobs.items(), key=lambda item: item[1].get("updated_at", ""), reverse=True)[:50])
    manifest["jobs"] = jobs
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _json_write(workspace / MANIFEST_RELATIVE_PATH, manifest)


def _document_action_hash(
    workspace: Path,
    source: str,
    locale: str,
    engine: str = "legacy",
) -> str:
    with _manifest_lock(workspace):
        return _document_action_hash_unlocked(workspace, source, locale, engine)


def _document_action_hash_unlocked(
    workspace: Path,
    source: str,
    locale: str,
    engine: str = "legacy",
) -> str:
    internal = _managed_internal_directory(workspace)
    manifest = _read_manifest(workspace)
    source_hash = str(manifest.get("source", {}).get("sha256", ""))
    layout_hash = str((manifest.get("layout") or {}).get("fingerprint", ""))
    if not source_hash or (engine == "legacy" and not layout_hash):
        raise ValueError("document layout is not ready" if engine == "legacy" else "document source is not ready")
    selected_hash = source_hash
    if source == "translation":
        entries = manifest.get("translations")
        entry = entries.get(locale) if isinstance(entries, dict) else None
        relative = entry.get("path") if isinstance(entry, dict) else None
        translation = workspace / relative if isinstance(relative, str) else internal / "translations" / f"{locale}.json"
        if not translation.is_file() or translation.is_symlink() or not _is_within(
            translation.resolve(strict=True),
            (workspace.resolve(strict=True),),
        ):
            raise ValueError("selected translation is not available")
        selected_hash = _sha256(translation)
    engine_version = ""
    if engine == "precise":
        from crabcode_gateway.document_engine import BABELDOC_VERSION

        engine_version = BABELDOC_VERSION
    return hashlib.sha256(
        f"{source_hash}:{layout_hash if engine == 'legacy' else ''}:{source}:{selected_hash}:{engine}:{engine_version}".encode("utf-8")
    ).hexdigest()


def _finalize_precise_translation_unlocked(
    workspace: Path,
    internal: Path,
    job: Path,
    manifest: dict[str, Any],
    operation_id: str,
    locale: str,
    now: str,
) -> tuple[int, int]:
    from crabcode_gateway.document_engine import BABELDOC_VERSION

    staged_pdf = job / "translated.pdf"
    staged_content = job / "content.json"
    staged_diagnostics = job / "diagnostics.json"
    for path in (staged_pdf, staged_content, staged_diagnostics):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"precise translation is missing {path.name}")
    if staged_pdf.stat().st_size <= 8 or staged_pdf.stat().st_size > MAX_DOCUMENT_BYTES * 4:
        raise ValueError("precise translated PDF has an invalid size")
    if staged_content.stat().st_size > 50 * 1024 * 1024 or staged_diagnostics.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("precise translation metadata is too large")
    with staged_pdf.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise ValueError("precise translation did not produce a valid PDF")
    diagnostics = _read_json_file(staged_diagnostics)
    if diagnostics is None:
        raise ValueError("precise translation diagnostics are invalid")
    content = _read_json_file(staged_content)
    content_pages = content.get("pages") if isinstance(content, dict) else None
    if (
        content is None
        or content.get("schema_version") != 1
        or content.get("engine") != "precise"
        or not isinstance(content_pages, list)
    ):
        raise ValueError("precise translation content is invalid")
    source_sha256 = str(manifest.get("source", {}).get("sha256") or "")
    if diagnostics.get("source_sha256") != source_sha256:
        raise ValueError("precise translation was generated for a different source document")
    if diagnostics.get("engine_version") != BABELDOC_VERSION:
        raise ValueError("precise translation engine version does not match")

    source_pdf = _document_pdf_path(workspace)
    try:
        source_reader = PdfReader(str(source_pdf))
        translated_reader = PdfReader(str(staged_pdf))
    except Exception as exc:
        raise ValueError("precise translated PDF cannot be opened") from exc
    if len(source_reader.pages) != len(translated_reader.pages):
        raise ValueError("precise translated PDF page count changed")
    if len(content_pages) != len(translated_reader.pages):
        raise ValueError("precise translation content page count changed")
    source_has_text = any((page.extract_text() or "").strip() for page in source_reader.pages)
    translated_has_text = any((page.extract_text() or "").strip() for page in translated_reader.pages)
    if source_has_text and not translated_has_text:
        raise ValueError("precise translated PDF has no selectable text")
    for source_page, translated_page in zip(source_reader.pages, translated_reader.pages, strict=True):
        for source_box, translated_box in (
            (source_page.mediabox, translated_page.mediabox),
            (source_page.cropbox, translated_page.cropbox),
        ):
            source_size = (float(source_box.width), float(source_box.height))
            translated_size = (float(translated_box.width), float(translated_box.height))
            if any(abs(left - right) > 0.5 for left, right in zip(source_size, translated_size, strict=True)):
                raise ValueError("precise translated PDF page dimensions changed")
        if int(source_page.get("/Rotate", 0) or 0) % 360 != int(translated_page.get("/Rotate", 0) or 0) % 360:
            raise ValueError("precise translated PDF page rotation changed")

    translations_dir = internal / "translations"
    if translations_dir.is_symlink():
        raise ValueError("managed translations directory is invalid")
    translations_dir.mkdir(parents=True, exist_ok=True)
    target = translations_dir / locale
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("precise translation output directory is invalid")
    stage = translations_dir / f".{locale}-stage-{uuid.uuid4().hex}"
    backup = translations_dir / f".{locale}-backup-{uuid.uuid4().hex}"
    stage.mkdir()
    replaced = False
    try:
        shutil.copy2(staged_pdf, stage / "translated.pdf")
        shutil.copy2(staged_content, stage / "content.json")
        shutil.copy2(staged_diagnostics, stage / "diagnostics.json")
        digest = _sha256(stage / "translated.pdf")
        warnings = diagnostics.get("warnings") if isinstance(diagnostics.get("warnings"), list) else []
        metadata = {
            "schema_version": 1,
            "engine": "precise",
            "locale": locale,
            "source_sha256": source_sha256,
            "pdf_sha256": digest,
            "page_count": len(translated_reader.pages),
            "engine_version": BABELDOC_VERSION,
            "warnings": [str(item)[:500] for item in warnings],
            "updated_at": now,
        }
        _json_write(stage / "metadata.json", metadata)
        if target.exists():
            replace_with_retry(target, backup)
            replaced = True
        replace_with_retry(stage, target)
        translations = manifest.get("translations")
        if not isinstance(translations, dict):
            translations = {}
            manifest["translations"] = translations
        translations[locale] = {
            "engine": "precise",
            "path": (target / "translated.pdf").relative_to(workspace).as_posix(),
            "content_path": (target / "content.json").relative_to(workspace).as_posix(),
            "diagnostics_path": (target / "diagnostics.json").relative_to(workspace).as_posix(),
            "source_sha256": source_sha256,
            "pdf_sha256": digest,
            "page_count": len(translated_reader.pages),
            "engine_version": BABELDOC_VERSION,
            "warnings": metadata["warnings"],
            "updated_at": now,
        }
        manifest["updated_at"] = now
        try:
            _json_write(workspace / MANIFEST_RELATIVE_PATH, manifest)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            if replaced and backup.exists():
                replace_with_retry(backup, target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if replaced and backup.exists() and not target.exists():
            replace_with_retry(backup, target)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    _cleanup_document_job(workspace, operation_id)
    return 100, 100


def _finalize_document_job_unlocked(
    workspace: Path,
    operation_id: str,
    action: str,
    locale: str,
    source: str,
    expected_document_hash: str = "",
    language: str = "",
    engine: str = "legacy",
) -> tuple[int, int]:
    """Validate an Agent's staged artifact and publish it atomically."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", locale):
        raise ValueError("invalid document locale")
    if language and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", language):
        raise ValueError("invalid Blog language")
    if engine not in {"legacy", "precise"}:
        raise ValueError("invalid document translation engine")
    if expected_document_hash and _document_action_hash(workspace, source, locale, engine) != expected_document_hash:
        raise ValueError("source document changed while the operation was running")
    internal = _managed_internal_directory(workspace)
    manifest = _read_manifest(workspace)
    now = datetime.now(timezone.utc).isoformat()
    if action == "generate_blog":
        # Finalization may be retried after the first call has atomically moved
        # blog.md and removed the staging directory. Treat that exact publish
        # as idempotently complete instead of reporting a missing artifact.
        published = manifest.get("blog")
        expected_language = locale if language == "source" and source == "translation" else language
        expected_language = expected_language or (locale if source == "translation" else "source")
        output = workspace / "blog.md"
        if (
            isinstance(published, dict)
            and published.get("operation_id") == operation_id
            and published.get("source") == source
            and output.is_file()
            and not output.is_symlink()
        ):
            if published.get("language") != expected_language:
                published["language"] = expected_language
                manifest["updated_at"] = now
                _json_write(workspace / MANIFEST_RELATIVE_PATH, manifest)
            return 1, 1
    job = _document_job_directory(workspace, operation_id)
    if job.is_symlink():
        raise ValueError("document job directory cannot be a symlink")
    if action == "translate" and engine == "precise":
        return _finalize_precise_translation_unlocked(
            workspace,
            internal,
            job,
            manifest,
            operation_id,
            locale,
            now,
        )
    blog_backup: bytes | None = None
    blog_existed = False
    translation_output: Path | None = None
    translation_backup: bytes | None = None
    translation_existed = False
    if action == "translate":
        total = _document_job_total(workspace, action, engine)
        try:
            layout_path = internal / "layout.json"
            staged_path = job / "translation.json"
            if layout_path.is_symlink() or staged_path.is_symlink():
                raise ValueError("document translation inputs cannot be symlinks")
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Agent did not produce valid translation JSON") from exc
        expected_ids = [
            block_id
            for page in _translation_source_pages(workspace)
            for block_id, _ in page
        ]
        blocks = staged.get("blocks") if isinstance(staged, dict) else None
        if not isinstance(blocks, list):
            raise ValueError("translation JSON is missing blocks")
        if staged.get("locale") not in {None, locale}:
            raise ValueError("translation job locale does not match")
        translated: dict[str, str] = {}
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("id"), str) or not isinstance(block.get("translated_text"), str):
                raise ValueError("translation blocks must contain id and translated_text")
            block_id = block["id"]
            if not block["translated_text"].strip():
                raise ValueError(f"translation is empty for block id: {block_id}")
            if block_id in translated:
                raise ValueError(f"translation contains duplicate block id: {block_id}")
            translated[block_id] = block["translated_text"]
        if len(expected_ids) != total or set(translated) != set(expected_ids):
            missing = len(set(expected_ids) - set(translated))
            extra = len(set(translated) - set(expected_ids))
            raise ValueError(f"translation block ids do not match layout (missing={missing}, extra={extra})")
        source_sha256 = str(manifest.get("source", {}).get("sha256", ""))
        if staged.get("source_sha256") != source_sha256:
            raise ValueError("translation was generated for a different source document")
        layout_fingerprint = (manifest.get("layout") or {}).get("fingerprint", "")
        if staged.get("layout_fingerprint") != layout_fingerprint:
            raise ValueError("translation was generated for a different document layout")
        canonical = {
            "engine": "legacy",
            "locale": locale,
            "source_sha256": source_sha256,
            "layout_fingerprint": layout_fingerprint,
            "blocks": [{"id": block_id, "translated_text": translated[block_id]} for block_id in expected_ids],
        }
        translations_dir = internal / "translations"
        if translations_dir.is_symlink():
            raise ValueError("managed translations directory is invalid")
        output = translations_dir / f"{locale}.json"
        if output.is_symlink():
            raise ValueError("translation output path is invalid")
        translation_output = output
        if output.exists():
            translation_backup = output.read_bytes()
            translation_existed = True
        _json_write(output, canonical)
        translations = manifest.get("translations")
        if not isinstance(translations, dict):
            translations = {}
            manifest["translations"] = translations
        translations[locale] = {
            "engine": "legacy",
            "path": output.relative_to(workspace).as_posix(),
            "source_sha256": source_sha256,
            "blocks": total,
            "updated_at": now,
        }
        current = total
    elif action == "generate_blog":
        staged_blog = job / "blog.md"
        try:
            if staged_blog.is_symlink() or staged_blog.stat().st_size > 10_000_000:
                raise ValueError("generated Blog is invalid or too large")
            markdown = staged_blog.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("Agent did not produce blog.md") from exc
        if not markdown.strip():
            raise ValueError("generated Blog is empty")
        output = workspace / "blog.md"
        if output.is_symlink():
            raise ValueError("Blog path is invalid")
        if output.exists():
            blog_backup = output.read_bytes()
            blog_existed = True
        replace_with_retry(staged_blog, output)
        revision = _blog_revision(markdown)
        blog_language = locale if language == "source" and source == "translation" else language
        manifest["blog"] = {
            "path": "blog.md",
            "revision": revision,
            "language": blog_language or (locale if source == "translation" else "source"),
            "source": source,
            "operation_id": operation_id,
            "updated_at": now,
        }
        current = total = 1
    else:
        raise ValueError("unknown document action")
    manifest["updated_at"] = now
    try:
        _json_write(workspace / MANIFEST_RELATIVE_PATH, manifest)
    except Exception:
        if action == "generate_blog":
            if blog_existed and blog_backup is not None:
                _atomic_write_bytes(workspace / "blog.md", blog_backup)
            else:
                (workspace / "blog.md").unlink(missing_ok=True)
        if action == "translate" and translation_output is not None:
            if translation_existed and translation_backup is not None:
                _atomic_write_bytes(translation_output, translation_backup)
            else:
                translation_output.unlink(missing_ok=True)
        raise
    _cleanup_document_job(workspace, operation_id)
    return current, total


def _finalize_document_job(
    workspace: Path,
    operation_id: str,
    action: str,
    locale: str,
    source: str,
    expected_document_hash: str = "",
    language: str = "",
    engine: str = "legacy",
) -> tuple[int, int]:
    with _manifest_lock(workspace):
        return _finalize_document_job_unlocked(
            workspace,
            operation_id,
            action,
            locale,
            source,
            expected_document_hash,
            language,
            engine,
        )


@router.get("/blog")
async def read_document_blog(workspace: str, request: Request, response: Response) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    with _manifest_lock(root):
        path = root / "blog.md"
        if path.is_symlink():
            raise HTTPException(status_code=403, detail="Blog path is invalid")
        if path.exists() and not path.is_file():
            raise HTTPException(status_code=403, detail="Blog path is invalid")
        try:
            markdown = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Blog not found") from exc
        revision = _blog_revision(markdown)
        response.headers["ETag"] = f'"{revision}"'
        manifest = _read_manifest(root)
        return {"markdown": markdown, "revision": revision, "language": (manifest.get("blog") or {}).get("language", "")}


@router.put("/blog")
async def write_document_blog(req: DocumentBlogWriteRequest, workspace: str, request: Request) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    with _manifest_lock(root):
        path = root / "blog.md"
        if path.is_symlink():
            raise HTTPException(status_code=403, detail="Blog path is invalid")
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        current_revision = _blog_revision(current) if path.exists() else None
        if req.revision != current_revision:
            raise HTTPException(status_code=409, detail="Blog changed since it was opened")
        temporary = root / f".blog-{uuid.uuid4().hex}.md"
        try:
            temporary.write_text(req.markdown, encoding="utf-8")
            replace_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        revision = _blog_revision(req.markdown)
        manifest = _read_manifest(root)
        manifest["blog"] = {"path": "blog.md", "revision": revision, "language": req.language}
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        _json_write(root / MANIFEST_RELATIVE_PATH, manifest)
        return {"markdown": req.markdown, "revision": revision, "language": req.language}


@router.delete("/blog")
async def delete_document_blog(
    workspace: str,
    request: Request,
) -> dict[str, Any]:
    root = _validated_workspace(workspace, _workspace_roots(request))
    with _manifest_lock(root):
        path = root / "blog.md"
        if path.is_symlink():
            raise HTTPException(status_code=403, detail="Blog path is invalid")
        if path.exists() and not path.is_file():
            raise HTTPException(status_code=403, detail="Blog path is invalid")
        manifest = _read_manifest(root)
        entry = manifest.get("blog")
        blog_entry = entry if isinstance(entry, dict) else {}
        assets = root / "blog-assets"
        if assets.is_symlink() or (assets.exists() and not assets.is_dir()):
            raise HTTPException(status_code=403, detail="Blog assets path is invalid")
        removed = path.exists()
        if removed:
            path.unlink()
        removed_assets = assets.is_dir()
        if removed_assets:
            shutil.rmtree(assets)
        if entry is not None or removed or removed_assets:
            manifest["blog"] = None
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            _json_write(root / MANIFEST_RELATIVE_PATH, manifest)
        return {
            "removed": removed or removed_assets,
            "removed_assets": removed_assets,
            "language": blog_entry.get("language", ""),
        }
