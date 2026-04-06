"""CodebaseSearchTool — semantic code search as a CrabCode extra tool."""

from __future__ import annotations

import os
from pathlib import Path
import json

# Must be set before ANY native library (FAISS / PyTorch) loads libomp.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import asyncio
from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult

from crabcode_search.background import STATUS_FILE_NAME
from crabcode_search.embedder import create_embedder
from crabcode_search.indexer import CodebaseIndexer


class CodebaseSearchTool(Tool):
    """Semantic codebase search powered by embeddings and FAISS."""

    name = "CodebaseSearch"
    description = "Search the codebase using natural language queries."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query about the codebase.",
            },
            "target_directory": {
                "type": "string",
                "description": "Optional subdirectory to limit search scope.",
            },
            "num_results": {
                "type": "integer",
                "description": "Number of results to return (default 10).",
                "default": 10,
            },
        },
        "required": ["query"],
    }

    def __init__(self) -> None:
        self._indexer: CodebaseIndexer | None = None
        self._tool_config: dict[str, Any] = {}
        self._init_lock = asyncio.Lock()
        self._status_path: Path | None = None

    async def setup(self, context: ToolContext) -> None:
        await super().setup(context)
        self._tool_config = context.tool_config
        self._status_path = Path(context.cwd) / ".crabcode" / "search" / STATUS_FILE_NAME

        status = self._read_background_status()
        if status:
            state = status.get("state")
            if state == "ready":
                chunks = status.get("chunks")
                message = "Background index ready"
                if chunks is not None:
                    message += f" ({chunks} chunks)"
                self.emit_event("ready", {"message": message})
                return
            if state in {"starting", "preloading", "loading_index", "indexing"}:
                done = status.get("done")
                total = status.get("total")
                percent = None
                if isinstance(done, int) and isinstance(total, int) and total > 0:
                    percent = done / total
                self.emit_event(
                    "progress",
                    {
                        "message": f"Background {state}...",
                        "percent": percent,
                    },
                )
                return

        await self._ensure_initialized()

    async def _ensure_initialized(self) -> None:
        if self._indexer is not None:
            return

        async with self._init_lock:
            if self._indexer is not None:
                return

            context = self._setup_context
            if context is None:
                return

            config = self._tool_config
            embedder = create_embedder(
            backend=config.get("embedder", "ollama"),
            model=config.get("model"),
            dimension=config.get("dimension"),
            batch_size=config.get("batch_size"),
            device=config.get("device"),
            )
            await embedder.preload()
            self._indexer = CodebaseIndexer(context.cwd, embedder=embedder)

            threads = config.get("threads")
            if threads is not None:
                threads = int(threads)
                os.environ["OMP_NUM_THREADS"] = str(threads)
                os.environ["MKL_NUM_THREADS"] = str(threads)
                try:
                    import torch
                    torch.set_num_threads(threads)
                except Exception:
                    pass

            all_files = self._indexer._scan_files()
            self._indexer.total_files = len(all_files)
            if self._indexer.store.count > 0 and not self._indexer._detect_changes(
                all_files
            ):
                self.emit_event("ready", {
                    "message": f"Index loaded ({self._indexer.store.count} chunks)",
                })
                return

            self.emit_event("progress", {"message": "Starting indexing...", "percent": 0})
            self._background_task = asyncio.create_task(self._build_index())

    def _read_background_status(self) -> dict[str, Any] | None:
        if self._status_path is None or not self._status_path.exists():
            return None
        try:
            data = json.loads(self._status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def _build_index(self) -> None:
        assert self._indexer is not None
        async for progress in self._indexer.build_or_update():
            if progress.total > 0:
                pct = progress.done / progress.total
            else:
                pct = 1.0
            self.emit_event("progress", {
                "message": f"Indexing: {progress.done}/{progress.total} files",
                "percent": pct,
            })
        self.emit_event("ready", {
            "message": f"Index ready ({self._indexer.store.count} chunks, {self._indexer.total_files} files)",
        })

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Search the codebase using natural language queries. "
            "Returns semantically relevant code snippets with file paths and line numbers. "
            "Use this when you need to find code by meaning rather than exact text. "
            "For exact text/regex matching, use Grep instead."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if self._indexer is None:
            await self._ensure_initialized()
        if self._indexer is None:
            return ToolResult(
                result_for_model="CodebaseSearch is not initialized. Use Grep or Glob instead.",
                is_error=True,
            )

        query = tool_input.get("query", "")
        if not query:
            return ToolResult(result_for_model="query is required.", is_error=True)

        num_results = tool_input.get("num_results", 10)
        target_dir = tool_input.get("target_directory")

        building = self._background_task and not self._background_task.done()

        if not building:
            await self._indexer.incremental_update()

        query_vec = await self._indexer.embedder.embed([query], task_type="CODE_RETRIEVAL_QUERY")
        results = await self._indexer.store.search(query_vec[0], top_k=num_results * 2)

        if target_dir:
            results = [r for r in results if r.chunk.file_path.startswith(target_dir)]

        results = results[:num_results]

        if not results:
            msg = "No results found."
            if building:
                progress = self._indexer.progress
                msg += (
                    f"\n\n[Note: Index still building ({progress.done}/{progress.total} files). "
                    "Results may be incomplete. Use Grep for precise searches.]"
                )
            return ToolResult(result_for_model=msg)

        output_parts: list[str] = []
        for i, r in enumerate(results, 1):
            output_parts.append(
                f"[{i}] {r.chunk.file_path}:{r.chunk.start_line}-{r.chunk.end_line} "
                f"(score: {r.score:.3f})\n"
                f"    {r.chunk.signature}\n"
                f"```\n{r.chunk.content}\n```"
            )

        output = "\n\n".join(output_parts)

        if building:
            progress = self._indexer.progress
            output += (
                f"\n\n[Note: Index still building ({progress.done}/{progress.total} files). "
                "Results may be incomplete. Use Grep for precise searches.]"
            )

        return ToolResult(result_for_model=output)
