"""USearch-backed vector store with async-safe concurrent access.

Uses USearch HNSW for all dataset sizes:
  - count < HNSW_THRESHOLD  → exact brute-force search (exact=True, O(N))
  - count >= HNSW_THRESHOLD → approximate HNSW search (exact=False, O(log N))

USearch uses SimSIMD instead of OpenMP, which avoids the libomp conflict with
PyTorch on macOS that made FAISS unusable in the same process.

Persistence:
  index.usearch  — USearch HNSW index
  hnsw_state.json — key→metadata-index mapping and next-key counter
  metadata.json  — list of chunk dicts
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from crabcode_search.chunker import ChunkMeta

logger = logging.getLogger(__name__)

HNSW_THRESHOLD = 100_000
HNSW_CONNECTIVITY = 16
HNSW_EXPANSION_ADD = 128
HNSW_EXPANSION_SEARCH = 64


@dataclass
class SearchResult:
    chunk: ChunkMeta
    score: float


class VectorStore:
    """USearch-backed vector store with metadata tracking.

    Small codebases (count < HNSW_THRESHOLD) use exact inner-product search;
    large codebases use approximate HNSW — both via the same USearch index.
    """

    def __init__(self, index_dir: Path, dimension: int = 768):
        self._index_dir = index_dir
        self._dimension = dimension

        self._index: Any = None          # usearch.index.Index (lazy init)
        self._metadata: list[dict[str, Any]] = []
        self._key_to_idx: dict[int, int] = {}   # usearch key → metadata list index
        self._next_key: int = 0
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        if self._index is None:
            return 0
        return len(self._index)

    def _get_or_create_index(self) -> Any:
        if self._index is None:
            from usearch.index import Index

            self._index = Index(
                ndim=self._dimension,
                metric="ip",
                dtype="f32",
                connectivity=HNSW_CONNECTIVITY,
                expansion_add=HNSW_EXPANSION_ADD,
                expansion_search=HNSW_EXPANSION_SEARCH,
            )
        return self._index

    async def add(self, vectors: np.ndarray, chunks: list[ChunkMeta]) -> None:
        if vectors.shape[0] == 0:
            return
        async with self._lock:
            idx = self._get_or_create_index()
            base = len(self._metadata)
            keys = np.arange(
                self._next_key, self._next_key + vectors.shape[0], dtype=np.int64
            )
            idx.add(keys, vectors)
            for i, chunk in enumerate(chunks):
                self._key_to_idx[int(keys[i])] = base + i
                self._metadata.append({
                    "file_path": chunk.file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "signature": chunk.signature,
                    "content": chunk.content,
                })
            self._next_key += vectors.shape[0]

    async def remove_by_file(self, file_path: str) -> None:
        async with self._lock:
            if not self._metadata or self._index is None:
                return

            remove_set = {
                i for i, m in enumerate(self._metadata) if m["file_path"] == file_path
            }
            if not remove_set:
                return

            keys_to_remove = [k for k, idx in self._key_to_idx.items() if idx in remove_set]
            for k in keys_to_remove:
                self._index.remove(k)
                del self._key_to_idx[k]

            keep = [i for i in range(len(self._metadata)) if i not in remove_set]
            new_meta = [self._metadata[i] for i in keep]
            old_to_new = {old: new for new, old in enumerate(keep)}
            self._key_to_idx = {
                k: old_to_new[v]
                for k, v in self._key_to_idx.items()
                if v in old_to_new
            }
            self._metadata = new_meta

    async def search(self, query_vec: np.ndarray, top_k: int = 10) -> list[SearchResult]:
        async with self._lock:
            if self._index is None or len(self._index) == 0:
                return []
            if query_vec.ndim == 1:
                query_vec = query_vec.reshape(1, -1)
            k = min(top_k, len(self._index))
            exact = len(self._index) < HNSW_THRESHOLD
            matches = self._index.search(query_vec, k, exact=exact)
            keys = matches.keys.ravel()
            distances = matches.distances.ravel()

            results: list[SearchResult] = []
            for key, dist in zip(keys, distances):
                key = int(key)
                if key not in self._key_to_idx:
                    continue
                meta_idx = self._key_to_idx[key]
                if meta_idx >= len(self._metadata):
                    continue
                meta = self._metadata[meta_idx]
                results.append(SearchResult(
                    chunk=ChunkMeta(**meta),
                    score=1.0 - float(dist),
                ))
            return results

    def save(self) -> None:
        self._index_dir.mkdir(parents=True, exist_ok=True)
        if self._index is not None:
            self._index.save(str(self._index_dir / "index.usearch"))
        state = {
            "key_to_idx": {str(k): v for k, v in self._key_to_idx.items()},
            "next_key": self._next_key,
        }
        with open(self._index_dir / "hnsw_state.json", "w") as f:
            json.dump(state, f)
        with open(self._index_dir / "metadata.json", "w") as f:
            json.dump(self._metadata, f)
        (self._index_dir / "index.npy").unlink(missing_ok=True)

    def load(self) -> bool:
        meta_path = self._index_dir / "metadata.json"
        if not meta_path.exists():
            return False
        try:
            with open(meta_path) as f:
                self._metadata = json.load(f)

            usearch_path = self._index_dir / "index.usearch"
            if usearch_path.exists():
                from usearch.index import Index

                idx = Index(
                    ndim=self._dimension,
                    metric="ip",
                    dtype="f32",
                    connectivity=HNSW_CONNECTIVITY,
                    expansion_add=HNSW_EXPANSION_ADD,
                    expansion_search=HNSW_EXPANSION_SEARCH,
                )
                idx.load(str(usearch_path))
                self._index = idx
                if len(idx) > 0:
                    self._dimension = idx.ndim

                state_path = self._index_dir / "hnsw_state.json"
                if state_path.exists():
                    with open(state_path) as f:
                        state = json.load(f)
                    self._key_to_idx = {int(k): v for k, v in state["key_to_idx"].items()}
                    self._next_key = state["next_key"]
                else:
                    n = len(idx)
                    self._key_to_idx = {k: k for k in range(n)}
                    self._next_key = n
                return True

            # Legacy: plain numpy index — migrate to usearch on next save
            npy_path = self._index_dir / "index.npy"
            if npy_path.exists():
                vectors = np.load(npy_path).astype(np.float32)
                if vectors.shape[0] > 0:
                    self._dimension = vectors.shape[1]
                    idx = self._get_or_create_index()
                    n = vectors.shape[0]
                    keys = np.arange(n, dtype=np.int64)
                    idx.add(keys, vectors)
                    self._key_to_idx = {k: k for k in range(n)}
                    self._next_key = n
                return True

            return False
        except Exception:
            logger.exception("Failed to load vector store")
            return False
