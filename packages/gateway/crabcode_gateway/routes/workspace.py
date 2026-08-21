"""Workspace discovery and scoped directory creation for desktop clients."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    WorkspaceDirectoryEntry,
    WorkspaceDirectoryCreateRequest,
    WorkspaceDirectoryListing,
    WorkspaceInfo,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])


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


def build_workspace_info(startup_cwd: str, configured_roots: list[str]) -> WorkspaceInfo:
    home = Path.home().resolve()
    cwd = Path(startup_cwd).resolve()
    roots: list[Path] = []
    candidates = [home, cwd]
    candidates.extend(Path(value).expanduser() for value in configured_roots)
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
) -> WorkspaceDirectoryListing:
    info: WorkspaceInfo = request.app.state.workspace_info
    roots = _workspace_roots(request)
    current = _resolve_directory(path or info.home, roots)

    directories: list[WorkspaceDirectoryEntry] = []
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
            if not resolved.is_dir() or not _is_within(resolved, roots):
                continue
        except (OSError, RuntimeError):
            continue
        directories.append(
            WorkspaceDirectoryEntry(
                name=child.name,
                path=str(child.absolute()),
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
    )


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
