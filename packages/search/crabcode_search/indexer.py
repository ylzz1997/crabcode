"""Codebase indexer — orchestrates scanning, chunking, embedding, and storage."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator

from crabcode_search.chunker import ChunkMeta, chunk_file, is_indexable
from crabcode_search.embedder import Embedder
from crabcode_search.store import VectorStore

BATCH_SIZE = 5
INDEX_DIR_NAME = ".crabcode/search"


@dataclass
class IndexProgress:
    done: int
    total: int


class CodebaseIndexer:
    """Coordinates file scanning, chunking, embedding, and vector storage."""

    def __init__(self, cwd: str, embedder: Embedder):
        self.cwd = Path(cwd).resolve()
        self.embedder = embedder
        self._index_dir = self.cwd / INDEX_DIR_NAME
        self.store = VectorStore(self._index_dir, dimension=embedder.dimension)
        self._file_index: dict[str, float] = {}
        self._file_index_path = self._index_dir / "file_index.json"
        self.total_files: int = 0

        self._load_file_index()
        self.store.load()

    @property
    def progress(self) -> IndexProgress:
        indexed = len(self._file_index)
        return IndexProgress(done=indexed, total=max(self.total_files, indexed))

    async def build_or_update(self) -> AsyncGenerator[IndexProgress, None]:
        """Full/incremental index build, yielding progress after each batch."""
        all_files = self._scan_files()
        self.total_files = len(all_files)
        changed = self._detect_changes(all_files)
        deleted = self._detect_deleted(all_files)

        await self._purge_deleted(deleted)

        if not changed:
            yield IndexProgress(done=self.total_files, total=self.total_files)
            return

        total_changed = len(changed)
        done = 0

        for batch in _batched(changed, BATCH_SIZE):
            all_chunks: list[ChunkMeta] = []
            for file_path in batch:
                rel = str(file_path.relative_to(self.cwd))
                await self.store.remove_by_file(rel)
                try:
                    content = file_path.read_text(errors="replace")
                except OSError:
                    continue
                chunks = chunk_file(rel, content)
                all_chunks.extend(chunks)

            if all_chunks:
                texts = [c.content for c in all_chunks]
                vectors = await self.embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT")
                await self.store.add(vectors, all_chunks)

            for file_path in batch:
                rel = str(file_path.relative_to(self.cwd))
                try:
                    self._file_index[rel] = file_path.stat().st_mtime
                except OSError:
                    pass

            done += len(batch)
            yield IndexProgress(done=done, total=total_changed)

        self._save_file_index()
        self.store.save()

    async def incremental_update(self) -> None:
        """Quick mtime-based update for changed files (typically < 1s)."""
        all_files = self._scan_files()
        changed = self._detect_changes(all_files)
        deleted = self._detect_deleted(all_files)

        await self._purge_deleted(deleted)

        if not changed:
            if deleted:
                self._save_file_index()
                self.store.save()
            return

        for file_path in changed:
            rel = str(file_path.relative_to(self.cwd))
            await self.store.remove_by_file(rel)

        all_chunks: list[ChunkMeta] = []
        for file_path in changed:
            rel = str(file_path.relative_to(self.cwd))
            try:
                content = file_path.read_text(errors="replace")
            except OSError:
                continue
            chunks = chunk_file(rel, content)
            all_chunks.extend(chunks)

        if all_chunks:
            texts = [c.content for c in all_chunks]
            vectors = await self.embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT")
            await self.store.add(vectors, all_chunks)

        for file_path in changed:
            rel = str(file_path.relative_to(self.cwd))
            try:
                self._file_index[rel] = file_path.stat().st_mtime
            except OSError:
                pass

        self._save_file_index()
        self.store.save()

    def _scan_files(self) -> list[Path]:
        """Scan the codebase for indexable files, respecting .gitignore."""
        tracked = self._git_tracked_files()
        if tracked is not None:
            return [f for f in tracked if is_indexable(f)]

        files: list[Path] = []
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv",
                     ".tox", ".mypy_cache", ".ruff_cache", "dist", "build",
                     ".crabcode", ".cursor"}
        for item in self.cwd.rglob("*"):
            if any(part in skip_dirs for part in item.parts):
                continue
            if item.is_file() and is_indexable(item):
                files.append(item)
        return files

    def _git_tracked_files(self) -> list[Path] | None:
        """Use git ls-files to get tracked files (respects .gitignore)."""
        try:
            result = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            files: list[Path] = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    files.append(self.cwd / line)
            return files
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _detect_changes(self, files: list[Path]) -> list[Path]:
        """Return files whose mtime changed since last index."""
        changed: list[Path] = []
        for f in files:
            rel = str(f.relative_to(self.cwd))
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if rel not in self._file_index or self._file_index[rel] != mtime:
                changed.append(f)
        return changed

    def _detect_deleted(self, current_files: list[Path]) -> list[str]:
        """Return relative paths that are in file_index but no longer on disk."""
        current_rels = {str(f.relative_to(self.cwd)) for f in current_files}
        return [rel for rel in self._file_index if rel not in current_rels]

    async def _purge_deleted(self, deleted: list[str]) -> None:
        """Remove vectors and file_index entries for deleted files."""
        for rel in deleted:
            await self.store.remove_by_file(rel)
            del self._file_index[rel]

    def _load_file_index(self) -> None:
        if self._file_index_path.exists():
            try:
                with open(self._file_index_path) as f:
                    self._file_index = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._file_index = {}

    def _save_file_index(self) -> None:
        self._index_dir.mkdir(parents=True, exist_ok=True)
        with open(self._file_index_path, "w") as f:
            json.dump(self._file_index, f)


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
