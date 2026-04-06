"""Embedding backends — API and local model support."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any

import numpy as np


class Embedder(ABC):
    """Abstract embedding backend."""

    @abstractmethod
    async def embed(self, texts: list[str], task_type: str | None = None) -> np.ndarray:
        """Embed a batch of texts. Returns (N, dimension) float32 array, L2-normalized.

        Args:
            texts: List of texts to embed.
            task_type: Optional hint for the embedding model (e.g. "RETRIEVAL_DOCUMENT",
                "CODE_RETRIEVAL_QUERY"). Non-Gemini backends ignore this.
        """
        ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    async def preload(self) -> None:
        """Pre-load the model. Override for backends with deferred initialization."""

    def _normalize(self, vecs: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        return (vecs / norms).astype(np.float32)


class OllamaEmbedder(Embedder):
    """Ollama local HTTP API — no extra Python deps."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        dim: int | None = None,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._dim = dim
        self._resolved_dim: int | None = None

    @property
    def dimension(self) -> int:
        if self._dim:
            return self._dim
        if self._resolved_dim:
            return self._resolved_dim
        return 768

    async def embed(self, texts: list[str], task_type: str | None = None) -> np.ndarray:
        import httpx

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()

        embeddings = data["embeddings"]
        vecs = np.array(embeddings, dtype=np.float32)
        if self._resolved_dim is None:
            self._resolved_dim = vecs.shape[1]
        return self._normalize(vecs)


class GeminiEmbedder(Embedder):
    """Google Gemini Embedding API — zero extra deps (pure httpx).

    Uses batchEmbedContents for efficiency (one HTTP round-trip per batch).
    Supports task_type for domain-optimised embeddings:
      - "RETRIEVAL_DOCUMENT" — for code chunks being indexed
      - "CODE_RETRIEVAL_QUERY" — for natural-language search queries
    """

    BATCH_SIZE = 100  # max items per batchEmbedContents call
    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        model: str = "gemini-embedding-2-preview",
        dim: int = 768,
        api_key: str | None = None,
        default_task_type: str = "RETRIEVAL_DOCUMENT",
    ):
        # Strip leading "models/" if user mistakenly includes it
        self._model = model.removeprefix("models/")
        self._dim = dim
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._default_task_type = default_task_type

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str], task_type: str | None = None) -> np.ndarray:
        import httpx

        effective_task_type = task_type or self._default_task_type
        url = f"{self._BASE_URL}/models/{self._model}:batchEmbedContents"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }

        all_vecs: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for i in range(0, len(texts), self.BATCH_SIZE):
                batch = texts[i : i + self.BATCH_SIZE]
                requests: list[dict[str, Any]] = []
                for text in batch:
                    req: dict[str, Any] = {
                        "model": f"models/{self._model}",
                        "content": {"parts": [{"text": text}]},
                        "taskType": effective_task_type,
                    }
                    if self._dim:
                        req["outputDimensionality"] = self._dim
                    requests.append(req)

                resp = await client.post(
                    url, headers=headers, json={"requests": requests}
                )
                resp.raise_for_status()
                data = resp.json()
                for emb in data["embeddings"]:
                    all_vecs.append(emb["values"])

        vecs = np.array(all_vecs, dtype=np.float32)
        return self._normalize(vecs)


class OpenAIEmbedder(Embedder):
    """OpenAI-compatible embedding API."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        dim: int = 1536,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str], task_type: str | None = None) -> np.ndarray:
        import httpx

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        body: dict[str, Any] = {
            "model": self._model,
            "input": texts,
        }
        if self._dim:
            body["dimensions"] = self._dim

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        embeddings = [item["embedding"] for item in data["data"]]
        vecs = np.array(embeddings, dtype=np.float32)
        return self._normalize(vecs)


def _detect_best_device() -> str:
    """Auto-detect the best available compute device: cuda > mps > cpu."""
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


_GPU_PER_SAMPLE_MB = 80.0
_CPU_PER_SAMPLE_MB = 50.0
_MAX_BATCH = {"cuda": 64, "mps": 32, "cpu": 8}


def _auto_batch_size(device: str = "cpu") -> int:
    """Compute a safe encode batch_size from available device memory.

    Transformer attention memory scales quadratically with sequence length,
    so per-sample cost on GPU is much higher than the raw embedding size.
    We use ~80 MB/sample for GPU/MPS and ~8 MB/sample for CPU.

    For CUDA, uses free VRAM (40% budget).  For MPS and CPU, uses available
    system RAM — on Apple Silicon the two share unified memory.
    """
    gpu = device in ("cuda", "mps")
    per_sample = _GPU_PER_SAMPLE_MB if gpu else _CPU_PER_SAMPLE_MB
    cap = _MAX_BATCH.get(device, 32)

    try:
        if device == "cuda":
            import torch

            free, _ = torch.cuda.mem_get_info()
            available_mb = free / (1024**2)
            budget_ratio = 0.4
        else:
            import psutil

            available_mb = psutil.virtual_memory().available / (1024**2)
            budget_ratio = 0.2 if gpu else 0.3

        size = max(1, int(available_mb * budget_ratio / per_sample))
        return min(size, cap)
    except Exception:
        return 4 if gpu else 8


def _is_oom_error(exc: Exception) -> bool:
    """Detect GPU/MPS out-of-memory errors (including MPS buffer limits)."""
    try:
        import torch

        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
    except (ImportError, AttributeError):
        pass
    msg = str(exc).lower()
    return (
        "out of memory" in msg
        or "oom" in msg
        or "invalid buffer size" in msg
    )


class HuggingFaceEmbedder(Embedder):
    """Local embedding via sentence-transformers (HuggingFace models).

    Model loading is deferred to a background thread on first use to avoid
    blocking the event loop (loading a 0.6B model can take 10+ seconds).

    device: ``None`` (default) auto-detects the best device (CUDA > MPS > CPU).
    Pass ``"cpu"`` / ``"cuda"`` / ``"mps"`` to force a specific device.

    batch_size: ``None`` (default) computes dynamically from available memory
    (VRAM for CUDA, unified memory for MPS/CPU).

    If an OOM error occurs on GPU/MPS, the model is moved to CPU
    transparently and the batch is retried.
    """

    def __init__(
        self,
        model: str = "BAAI/bge-small-en-v1.5",
        batch_size: int | None = None,
        device: str | None = None,
        sync: bool = False,
    ):
        try:
            import sentence_transformers  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "HuggingFace backend requires sentence-transformers: "
                "pip install crabcode-search[huggingface]"
            ) from exc
        self._model_name = model
        self._batch_size = batch_size
        self._requested_device = device
        self._device: str | None = None
        self._sync_mode = sync
        self._model: Any = None
        self._dim: int | None = None

    async def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        if self._requested_device is not None:
            self._device = self._requested_device
        else:
            self._device = _detect_best_device()

        import sys
        print(f"[embedder] using device: {self._device}", file=sys.stderr)

        if self._sync_mode:
            self._model = SentenceTransformer(self._model_name, device=self._device)
        else:
            self._model = await asyncio.to_thread(
                SentenceTransformer, self._model_name, device=self._device,
            )
        self._dim = self._model.get_sentence_embedding_dimension()

    @property
    def dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        return 1024

    async def preload(self) -> None:
        await self._ensure_loaded()

    async def embed(self, texts: list[str], task_type: str | None = None) -> np.ndarray:
        await self._ensure_loaded()
        assert self._device is not None
        bs = self._batch_size if self._batch_size is not None else _auto_batch_size(self._device)

        def _encode() -> np.ndarray:
            return self._model.encode(texts, normalize_embeddings=True, batch_size=bs)

        try:
            if self._sync_mode:
                vecs = _encode()
            else:
                vecs = await asyncio.to_thread(_encode)
        except RuntimeError as exc:
            if not _is_oom_error(exc):
                raise
            vecs = await self._oom_retry(texts, bs, exc)
        return np.asarray(vecs, dtype=np.float32)

    async def _oom_retry(
        self, texts: list[str], failed_bs: int, original_exc: Exception,
    ) -> np.ndarray:
        """Progressive OOM recovery: halve batch on same device, then CPU."""
        import sys

        if self._device != "cpu" and failed_bs > 1:
            new_bs = max(1, failed_bs // 2)
            print(
                f"[embedder] OOM on {self._device} (batch={failed_bs}), "
                f"retrying with batch={new_bs}",
                file=sys.stderr,
            )
            try:
                vecs = await asyncio.to_thread(
                    self._model.encode, texts, normalize_embeddings=True,
                    batch_size=new_bs,
                )
                self._batch_size = new_bs
                return vecs
            except RuntimeError as exc2:
                if not _is_oom_error(exc2):
                    raise

        if self._device == "cpu":
            raise original_exc

        print(
            f"[embedder] still OOM on {self._device}, falling back to CPU",
            file=sys.stderr,
        )
        self._model = self._model.to("cpu")
        self._device = "cpu"
        bs = _auto_batch_size("cpu")
        self._batch_size = bs
        return await asyncio.to_thread(
            self._model.encode, texts, normalize_embeddings=True, batch_size=bs,
        )


class ModelScopeEmbedder(Embedder):
    """Local embedding via ModelScope SDK."""

    def __init__(self, model: str = "iic/nlp_gte_sentence-embedding_chinese-base"):
        try:
            from modelscope.models import Model
            from modelscope.pipelines import pipeline
        except ImportError as exc:
            raise ImportError(
                "ModelScope backend requires modelscope: "
                "pip install crabcode-search[modelscope]"
            ) from exc
        self._pipeline = pipeline("sentence-embedding", model=model)
        self._dim: int = 0

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str], task_type: str | None = None) -> np.ndarray:
        result = await asyncio.to_thread(
            self._pipeline, {"source_sentence": texts}
        )
        vecs = np.array(result["text_embedding"], dtype=np.float32)
        if self._dim == 0:
            self._dim = vecs.shape[1]
        return self._normalize(vecs)


_EMBEDDER_REGISTRY: dict[str, type[Embedder]] = {
    "ollama": OllamaEmbedder,
    "gemini": GeminiEmbedder,
    "openai": OpenAIEmbedder,
    "huggingface": HuggingFaceEmbedder,
    "modelscope": ModelScopeEmbedder,
}


def create_embedder(
    backend: str = "ollama",
    model: str | None = None,
    dimension: int | None = None,
    **kwargs: Any,
) -> Embedder:
    """Factory: create an embedder from config."""
    cls = _EMBEDDER_REGISTRY.get(backend)
    if cls is None:
        raise ValueError(
            f"Unknown embedder backend: {backend!r}. "
            f"Available: {', '.join(_EMBEDDER_REGISTRY)}"
        )

    init_kwargs: dict[str, Any] = {k: v for k, v in kwargs.items() if v is not None}
    if model:
        init_kwargs["model"] = model
    if dimension is not None:
        init_kwargs["dim"] = dimension

    return cls(**init_kwargs)
