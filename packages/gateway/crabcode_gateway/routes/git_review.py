"""Read-only Git review scopes for local and remote workspaces."""

from __future__ import annotations

import difflib
import os
import subprocess
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from crabcode_gateway.routes.workspace import _is_within, _resolve_directory, _workspace_roots

router = APIRouter(prefix="/workspace/git", tags=["workspace"])
GitScope = Literal["uncommitted", "unstaged", "staged", "commit", "branch"]
MAX_FILES = 200
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


class GitCommit(BaseModel):
    id: str
    subject: str


class GitReviewInfo(BaseModel):
    available: bool
    reason: str | None = None
    root: str | None = None
    head: str | None = None
    branch: str | None = None
    default_base: str | None = None
    branches: list[str] = Field(default_factory=list)
    commits: list[GitCommit] = Field(default_factory=list)


class GitReviewFile(BaseModel):
    path: str
    action: Literal["create", "modify", "delete"]
    added: int = 0
    removed: int = 0
    diff: str | None = None
    note: str | None = None


class GitReviewDiff(BaseModel):
    files: list[GitReviewFile]
    added: int
    removed: int
    total_files: int
    truncated: bool
    base: str | None = None
    target: str | None = None


def _git(root: Path, *args: str, optional: bool = False) -> bytes | None:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    try:
        result = subprocess.run(
            ["git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
             "-c", "core.quotePath=false", "-C", str(root), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=15, check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, "Gateway 未安装 Git") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "读取 Git 超时，请缩小变更范围后重试") from exc
    if result.returncode:
        if optional:
            return None
        raise HTTPException(400, "无法读取 Git 变更：" + result.stderr.decode("utf-8", "replace").strip()[:500])
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        raise HTTPException(413, "Git 输出过大，请选择更小的变更范围")
    return result.stdout


def _repository(request: Request, path: str) -> Path | None:
    roots = _workspace_roots(request)
    directory = _resolve_directory(path, roots)
    value = _git(directory, "rev-parse", "--show-toplevel", optional=True)
    if value is None:
        return None
    root = Path(os.fsdecode(value.rstrip(b"\n"))).resolve()
    if not _is_within(root, roots):
        raise HTTPException(403, "Git 仓库不在允许浏览的目录内")
    return root


def _revision(root: Path, ref: str, *, optional: bool = False) -> str | None:
    value = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", optional=optional)
    return value.decode().strip() if value is not None else None


@router.get("/info", response_model=GitReviewInfo)
def git_info(request: Request, path: str) -> GitReviewInfo:
    try:
        root = _repository(request, path)
    except HTTPException as exc:
        if exc.status_code == 503:
            return GitReviewInfo(available=False, reason=str(exc.detail))
        raise
    if root is None:
        return GitReviewInfo(available=False, reason="当前目录不在 Git 仓库中")
    head = _revision(root, "HEAD", optional=True)
    branch_value = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True)
    branch = branch_value.decode("utf-8", "replace").strip() if branch_value else None
    refs = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes") or b""
    branches = [ref for ref in refs.decode("utf-8", "replace").splitlines() if not ref.endswith("/HEAD")]
    remote_head = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", optional=True)
    defaults = [remote_head.decode().strip() if remote_head else "", "origin/main", "main", "origin/master", "master"]
    default_base = next((ref for ref in defaults + branches if ref in branches and ref != branch), None)
    default_base = default_base or (branches[0] if branches else None)
    commits: list[GitCommit] = []
    if head:
        log = _git(root, "log", "-100", "--format=%H%x00%s%x00", head, "--") or b""
        fields = log.decode("utf-8", "replace").split("\0")
        commits = [GitCommit(id=fields[i].strip(), subject=fields[i + 1]) for i in range(0, len(fields) - 1, 2)]
    return GitReviewInfo(available=True, root=str(root), head=head, branch=branch,
                         default_base=default_base, branches=branches, commits=commits)


def _new_file(root: Path, name: str) -> GitReviewFile | None:
    path = root / name
    if not _is_within(path.parent.resolve(), (root,)):
        raise HTTPException(403, "文件不在 Git 工作区内")
    try:
        if path.is_symlink():
            raw = os.fsencode(os.readlink(path))
        else:
            if not path.is_file():
                return None
            with path.open("rb") as handle:
                raw = handle.read(MAX_PATCH_BYTES + 1)
    except FileNotFoundError:
        return None  # The working tree can change while the review is loading.
    if len(raw) > MAX_PATCH_BYTES:
        return GitReviewFile(path=name, action="create", note="文件超过 2 MiB，未加载 diff")
    if b"\0" in raw:
        return GitReviewFile(path=name, action="create", note="二进制文件，无法显示文本 diff")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return GitReviewFile(path=name, action="create", note="非 UTF-8 文件，无法显示文本 diff")
    lines = text.splitlines(keepends=True)
    patch = "".join(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{name}"))
    return GitReviewFile(path=name, action="create", added=len(lines), diff=patch,
                         note="空文件" if not lines else None)


@router.get("/diff", response_model=GitReviewDiff)
def git_diff(request: Request, path: str, scope: GitScope, ref: str | None = None) -> GitReviewDiff:
    root = _repository(request, path)
    if root is None:
        raise HTTPException(400, "当前目录不在 Git 仓库中")
    head = _revision(root, "HEAD", optional=True)
    base: str | None = None
    target: str | None = None
    unborn_worktree = scope == "uncommitted" and head is None
    if scope == "commit":
        target = _revision(root, ref or head or "HEAD")
        base = _revision(root, f"{target}^", optional=True)
        command = ["diff", base, target] if base else ["diff-tree", "--root", "--no-commit-id", "-r", target]
    elif scope == "branch":
        if not head or not ref:
            raise HTTPException(400, "请选择比较分支，并确保当前分支已有提交")
        reference = _revision(root, ref)
        common = _git(root, "merge-base", reference, head, optional=True)
        if common is None:
            raise HTTPException(400, "所选分支与当前分支没有共同祖先")
        base, target = common.decode().strip(), head
        command = ["diff", base, target]
    elif scope == "staged":
        base = head
        command = ["diff", "--cached"]
    elif scope == "uncommitted":
        base = head
        command = ["diff", head] if head else ["diff", "--cached"]
    else:
        command = ["diff"]
    # Treat renames as deletion + addition, preserving both paths without
    # ambiguous quoted-path parsing. Git never runs external diff drivers.
    options = ["--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=none", "--no-color"]
    stats = _git(root, *command, *options, "--numstat", "-z", "--") or b""
    entries: dict[str, tuple[int, int, bool]] = {}
    for record in stats.split(b"\0"):
        if not record:
            continue
        added, removed, name = record.split(b"\t", 2)
        binary = added == b"-" or removed == b"-"
        entries[os.fsdecode(name)] = (0 if binary else int(added), 0 if binary else int(removed), binary)
    untracked: set[str] = set()
    if scope in ("uncommitted", "unstaged"):
        raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z") or b""
        untracked = {os.fsdecode(name) for name in raw.split(b"\0") if name}
        for name in sorted(untracked):
            entries.setdefault(name, (0, 0, False))
    files: list[GitReviewFile] = []
    remaining_bytes = MAX_OUTPUT_BYTES
    for name, (added, removed, binary) in list(entries.items())[:MAX_FILES]:
        if name in untracked or unborn_worktree:
            file = _new_file(root, name)
            if file:
                patch_bytes = len((file.diff or "").encode("utf-8"))
                if patch_bytes > min(MAX_PATCH_BYTES, remaining_bytes):
                    file.diff = None
                    file.note = "diff 过大，仅显示行数"
                else:
                    remaining_bytes -= patch_bytes
                files.append(file)
            continue
        patch = _git(root, *command, *options, "--patch", "--", name) or b""
        text = patch.decode("utf-8", "replace")
        action = "create" if "\nnew file mode " in text else "delete" if "\ndeleted file mode " in text else "modify"
        note = None
        if binary:
            note = "二进制文件，无法显示文本 diff"
        elif len(patch) > min(MAX_PATCH_BYTES, remaining_bytes):
            note = "diff 过大，仅显示行数"
        elif "\n@@ " not in text:
            note = "文件属性或子模块变更，无文本差异"
        if note is None:
            remaining_bytes -= len(patch)
        files.append(GitReviewFile(path=name, action=action, added=added, removed=removed,
                                   diff=None if note else text, note=note))
    return GitReviewDiff(files=files, added=sum(file.added for file in files),
                         removed=sum(file.removed for file in files), total_files=len(entries),
                         truncated=len(entries) > MAX_FILES, base=base, target=target)
