"""Workspace discovery and scoped directory creation for desktop clients."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from crabcode_gateway.schemas import (
    WorkspaceDirectoryEntry,
    WorkspaceDirectoryCreateRequest,
    WorkspaceDirectoryListing,
    WorkspaceFileEntry,
    WorkspaceInfo,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])

MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PREVIEW_BYTES = 10 * 1024 * 1024
_IMAGE_PREVIEW_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_TEXT_PREVIEW_EXTENSIONS = {
    ".bash", ".bat", ".c", ".cc", ".cfg", ".cjs", ".cmd", ".conf", ".cpp",
    ".css", ".cts", ".cxx", ".env", ".fish", ".gitattributes", ".gitignore",
    ".go", ".gql", ".graphql", ".h", ".hpp", ".htm", ".html", ".ini", ".java",
    ".js", ".json", ".jsonc", ".jsx", ".kt", ".kts", ".less", ".lock", ".md",
    ".markdown", ".mdx", ".mjs", ".mts", ".php", ".proto", ".ps1", ".py", ".pyi",
    ".rb", ".rs", ".sass", ".scss", ".sh", ".sql", ".svg", ".swift", ".toml",
    ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml", ".zsh",
}
_TEXT_PREVIEW_FILENAMES = {
    ".dockerignore", ".editorconfig", ".eslintignore", ".eslintrc", ".gitattributes",
    ".gitignore", ".npmrc", ".prettierignore", ".prettierrc", ".rgignore",
    ".stylelintignore", ".stylelintrc", "dockerfile", "gemfile", "license", "makefile",
    "procfile", "readme",
}
_MARKDOWN_PREVIEW_EXTENSIONS = {".md", ".markdown", ".mdx"}


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    normalized_path = os.path.normcase(str(path))
    for root in roots:
        normalized_root = os.path.normcase(str(root))
        try:
            if os.path.commonpath((normalized_path, normalized_root)) == normalized_root:
                return True
        except ValueError:
            # Different drives on Windows cannot share a common path.
            continue
    return False


def _resolve_directory(value: str, roots: tuple[Path, ...]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Directory not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to resolve directory") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="path must be a directory")
    if not _is_within(resolved, roots):
        raise HTTPException(status_code=403, detail="Directory is outside the allowed browse roots")
    return resolved


def _resolve_preview_file(value: str, roots: tuple[Path, ...]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to resolve file") from exc
    if not _is_within(resolved, roots):
        raise HTTPException(status_code=403, detail="File is outside the allowed browse roots")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="path must be a regular file")
    return resolved


def _preview_media_type(path: Path) -> tuple[str, int]:
    suffix = path.suffix.casefold()
    name = path.name.casefold()
    if suffix in _IMAGE_PREVIEW_EXTENSIONS:
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return media_type, MAX_IMAGE_PREVIEW_BYTES
    if (
        suffix in _TEXT_PREVIEW_EXTENSIONS
        or name in _TEXT_PREVIEW_FILENAMES
        or name.startswith(".env")
    ):
        media_type = (
            "text/markdown"
            if suffix in _MARKDOWN_PREVIEW_EXTENSIONS
            else mimetypes.guess_type(path.name)[0] or "text/plain"
        )
        if media_type == "image/svg+xml":
            media_type = "text/plain"
        return media_type, MAX_TEXT_PREVIEW_BYTES
    raise HTTPException(status_code=415, detail="File type is not supported for preview")


def _create_directory(value: str, roots: tuple[Path, ...]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    if not candidate.name or candidate.name in {".", ".."}:
        raise HTTPException(status_code=400, detail="path must name a directory")
    try:
        parent = candidate.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Parent directory not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to resolve parent directory") from exc
    if not parent.is_dir() or not _is_within(parent, roots):
        raise HTTPException(status_code=403, detail="Directory is outside the allowed browse roots")
    try:
        candidate.mkdir(exist_ok=True)
        resolved = candidate.resolve(strict=True)
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail="path exists and is not a directory") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Directory is not writable") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to create directory") from exc
    if not resolved.is_dir() or not _is_within(resolved, roots):
        raise HTTPException(status_code=403, detail="Directory is outside the allowed browse roots")
    return resolved


def _windows_drive_roots() -> list[Path]:
    """Existing drive letters, so Windows pickers can reach any disk without per-drive config."""
    if os.name != "nt":
        return []
    drives: list[Path] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:\\")
        if candidate.exists():
            drives.append(candidate)
    return drives


def build_workspace_info(startup_cwd: str, configured_roots: list[str]) -> WorkspaceInfo:
    home = Path.home().resolve()
    cwd = Path(startup_cwd).resolve()
    roots: list[Path] = []
    candidates = [home, cwd]
    candidates.extend(Path(value).expanduser() for value in configured_roots)
    candidates.extend(_windows_drive_roots())
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or resolved in roots:
            continue
        roots.append(resolved)
    return WorkspaceInfo(
        startup_cwd=str(cwd),
        home=str(home),
        browse_roots=[str(root) for root in roots],
        documents_dir=str(_recommended_documents_dir(home)),
    )


def _recommended_documents_dir(home: Path) -> Path:
    """Return a visible, cross-platform document root without creating it."""
    if os.name == "nt":
        configured = os.environ.get("USERPROFILE")
        base = Path(configured).expanduser() if configured else home
        return base / "Documents" / "CrabCode"
    return home / "Documents" / "CrabCode"


def _workspace_roots(request: Request) -> tuple[Path, ...]:
    info: WorkspaceInfo = request.app.state.workspace_info
    return tuple(Path(value) for value in info.browse_roots)


@router.get("/info", response_model=WorkspaceInfo)
async def workspace_info(request: Request) -> WorkspaceInfo:
    return request.app.state.workspace_info


@router.get("/directories", response_model=WorkspaceDirectoryListing)
async def list_directories(
    request: Request,
    path: str | None = None,
    include_hidden: bool = False,
    include_files: bool = False,
) -> WorkspaceDirectoryListing:
    info: WorkspaceInfo = request.app.state.workspace_info
    roots = _workspace_roots(request)
    current = _resolve_directory(path or info.home, roots)

    directories: list[WorkspaceDirectoryEntry] = []
    files: list[WorkspaceFileEntry] = []
    try:
        children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Directory is not readable") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to list directory") from exc

    for child in children:
        hidden = child.name.startswith(".")
        if hidden and not include_hidden:
            continue
        try:
            resolved = child.resolve(strict=True)
            if not _is_within(resolved, roots):
                continue
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir():
            directories.append(
                WorkspaceDirectoryEntry(
                    name=child.name,
                    path=str(child.absolute()),
                    hidden=hidden,
                    is_symlink=child.is_symlink(),
                )
            )
        elif include_files and resolved.is_file():
            try:
                size = resolved.stat().st_size
            except OSError:
                size = 0
            files.append(
                WorkspaceFileEntry(
                    name=child.name,
                    path=str(child.absolute()),
                    size=size,
                    hidden=hidden,
                    is_symlink=child.is_symlink(),
                )
            )

    parent = current.parent
    parent_path = str(parent) if parent != current and _is_within(parent, roots) else None
    return WorkspaceDirectoryListing(
        path=str(current),
        parent=parent_path,
        directories=directories,
        files=files,
    )


@router.get("/file", response_class=FileResponse)
async def workspace_file(path: str, request: Request) -> FileResponse:
    candidate = _resolve_preview_file(path, _workspace_roots(request))
    media_type, max_bytes = _preview_media_type(candidate)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Unable to inspect file") from exc
    if size > max_bytes:
        limit = "10 MiB" if max_bytes == MAX_IMAGE_PREVIEW_BYTES else "2 MiB"
        raise HTTPException(status_code=413, detail=f"File exceeds the {limit} preview limit")
    return FileResponse(candidate, media_type=media_type)


@router.post("/directories/create", response_model=WorkspaceDirectoryEntry)
async def create_directory(
    req: WorkspaceDirectoryCreateRequest,
    request: Request,
) -> WorkspaceDirectoryEntry:
    created = _create_directory(req.path, _workspace_roots(request))
    return WorkspaceDirectoryEntry(
        name=created.name,
        path=str(created),
        hidden=created.name.startswith("."),
        is_symlink=created.is_symlink(),
    )
