from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from pypdf import PdfWriter

from crabcode_gateway.document_engine import BABELDOC_VERSION
from crabcode_gateway.document_engine import document_engine_status
from crabcode_gateway.document_engine import _download_bundle
from crabcode_gateway.document_engine import _extract_and_verify_bundle
from crabcode_gateway.document_engine import _install_document_engine
from crabcode_gateway.document_engine import select_document_translation_engine
from crabcode_gateway.routes.document import MANIFEST_RELATIVE_PATH
from crabcode_gateway.routes.document import _clear_document_translation_locked
from crabcode_gateway.routes.document import _finalize_document_job
from crabcode_gateway.routes.document import _json_write
from crabcode_gateway.routes.document import _read_document_translation_locked
from crabcode_gateway.routes.event import _request_translation_batch
from crabcode_gateway import document_worker


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf(path: Path, sizes: list[tuple[float, float]]) -> None:
    writer = PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    with path.open("wb") as handle:
        writer.write(handle)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    internal = root / ".crabcode" / "document"
    translations = internal / "translations"
    translations.mkdir(parents=True)
    (internal / "jobs").mkdir()
    source_pdf = root / "source.pdf"
    root.mkdir(exist_ok=True)
    _pdf(source_pdf, [(612, 792)])
    source_sha256 = _sha256(source_pdf)
    manifest = {
        "schema_version": 1,
        "project_id": "test",
        "project_name": "Test",
        "workspace": str(root),
        "source": {"path": "source.pdf", "sha256": source_sha256},
        "pdf": {"path": "source.pdf", "sha256": source_sha256, "page_count": 1},
        "layout": {"path": ".crabcode/document/layout.json", "fingerprint": "layout-1"},
        "translations": {},
        "blog": None,
        "jobs": {},
    }
    _json_write(root / MANIFEST_RELATIVE_PATH, manifest)
    return root, internal


def _stage_precise(root: Path, operation_id: str, *, sizes: list[tuple[float, float]]) -> None:
    job = root / ".crabcode" / "document" / "jobs" / operation_id
    job.mkdir(parents=True)
    _pdf(job / "translated.pdf", sizes)
    _json_write(job / "content.json", {
        "schema_version": 1,
        "engine": "precise",
        "pages": [{"page": index, "paragraphs": [f"page {index}"]} for index in range(1, len(sizes) + 1)],
    })
    manifest = json.loads((root / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    _json_write(job / "diagnostics.json", {
        "schema_version": 1,
        "engine": "precise",
        "engine_version": BABELDOC_VERSION,
        "source_sha256": manifest["source"]["sha256"],
        "page_count": len(sizes),
        "warnings": [],
    })
    _json_write(job / "precise-cache.json", {
        "schema_version": 1,
        "locale": "zh-CN",
        "source_sha256": manifest["source"]["sha256"],
        "engine_version": BABELDOC_VERSION,
        "entries": {},
    })


def test_schema_v1_legacy_translation_remains_readable(tmp_path: Path) -> None:
    root, internal = _workspace(tmp_path)
    legacy = {
        "locale": "zh-CN",
        "source_sha256": _sha256(root / "source.pdf"),
        "layout_fingerprint": "layout-1",
        "blocks": [{"id": "p1", "translated_text": "你好"}],
    }
    _json_write(internal / "translations" / "zh-CN.json", legacy)

    value = _read_document_translation_locked(root, internal, "zh-CN")

    assert value == {**legacy, "engine": "legacy"}
    assert json.loads((root / MANIFEST_RELATIVE_PATH).read_text())["schema_version"] == 1


def test_precise_publish_is_atomic_and_keeps_legacy_fallback(tmp_path: Path) -> None:
    root, internal = _workspace(tmp_path)
    legacy_path = internal / "translations" / "zh-CN.json"
    _json_write(legacy_path, {
        "locale": "zh-CN",
        "source_sha256": _sha256(root / "source.pdf"),
        "layout_fingerprint": "layout-1",
        "blocks": [{"id": "p1", "translated_text": "旧译文"}],
    })
    _stage_precise(root, "precise-1", sizes=[(612, 792)])

    assert _finalize_document_job(
        root, "precise-1", "translate", "zh-CN", "original", "", "", "precise"
    ) == (100, 100)
    manifest = json.loads((root / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    published = root / manifest["translations"]["zh-CN"]["path"]
    published_digest = _sha256(published)
    assert manifest["schema_version"] == 1
    assert manifest["translations"]["zh-CN"]["engine"] == "precise"
    assert legacy_path.is_file()
    assert _read_document_translation_locked(root, internal, "zh-CN")["pdf_sha256"] == published_digest

    _stage_precise(root, "precise-2", sizes=[(300, 300)])
    with pytest.raises(ValueError, match="dimensions"):
        _finalize_document_job(
            root, "precise-2", "translate", "zh-CN", "original", "", "", "precise"
        )
    after = json.loads((root / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert after["translations"]["zh-CN"]["pdf_sha256"] == published_digest
    assert _sha256(published) == published_digest

    cleared = _clear_document_translation_locked(root, internal, "zh-CN")
    assert cleared["removed_translation"] is True
    assert cleared["removed_jobs"] == 1
    assert not legacy_path.exists()
    assert not (internal / "translations" / "zh-CN").exists()
    assert not (internal / "jobs" / "precise-2").exists()


def test_engine_status_detects_corrupted_managed_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "babeldoc" / BABELDOC_VERSION
    asset = root / "assets" / "models" / "layout.onnx"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"model")
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    _json_write(root / "assets" / "asset-manifest.json", {
        "schema_version": 1,
        "files": [{"path": "models/layout.onnx", "sha256": _sha256(asset)}],
    })
    _json_write(root / "engine.json", {
        "schema_version": 1,
        "engine_version": BABELDOC_VERSION,
        "protocol_version": 1,
    })
    monkeypatch.setenv("CRABCODE_DOCUMENT_ENGINE_HOME", str(root))
    monkeypatch.setattr("crabcode_gateway.document_engine._verify_engine_runtime", lambda _root: None)

    assert document_engine_status()["status"] == "ready"
    asset.write_bytes(b"corrupted")
    assert document_engine_status()["status"] == "broken"


def test_engine_bundle_rejects_hash_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "engine.zip"
    manifest = {
        "schema_version": 1,
        "engine_version": BABELDOC_VERSION,
        "files": [{"path": "assets/model.onnx", "sha256": "0" * 64}],
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("engine-manifest.json", json.dumps(manifest))
        archive.writestr("assets/model.onnx", b"corrupted")

    with pytest.raises(ValueError, match="checksum"):
        _extract_and_verify_bundle(bundle, tmp_path / "extracted")


def test_engine_download_reports_missing_release_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("GET", "https://example.test/engine.zip")
    response = httpx.Response(404, request=request)

    class _ResponseContext:
        def __enter__(self):
            return response

        def __exit__(self, *_args):
            response.close()

    monkeypatch.setattr(httpx, "stream", lambda *_args, **_kwargs: _ResponseContext())

    with pytest.raises(RuntimeError, match="尚未发布"):
        _download_bundle(request.url.__str__(), tmp_path / "engine.zip", lambda _message: None)


def test_engine_default_install_uses_official_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "babeldoc" / BABELDOC_VERSION
    calls: list[tuple[Path, Path]] = []
    monkeypatch.delenv("CRABCODE_DOCUMENT_ENGINE_BUNDLE_URL", raising=False)
    monkeypatch.setattr(
        "crabcode_gateway.document_engine.document_engine_root",
        lambda: root,
    )
    monkeypatch.setattr(
        "crabcode_gateway.document_engine._install_document_engine_from_official_source",
        lambda target, parent, _notify: calls.append((target, parent)),
    )

    assert _install_document_engine() == {"installed": True}
    assert calls == [(root, root.parent)]


def test_translation_engine_auto_selection_and_explicit_legacy() -> None:
    unavailable = {"available": False, "detail": "not installed"}
    ready = {"available": True, "detail": "ready"}
    assert select_document_translation_engine("auto", unavailable) == "legacy"
    assert select_document_translation_engine("auto", ready) == "precise"
    assert select_document_translation_engine("legacy", ready) == "legacy"
    with pytest.raises(RuntimeError, match="not installed"):
        select_document_translation_engine("precise", unavailable)


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return SimpleNamespace(type="text", text=self._text, usage={})

    async def aclose(self) -> None:
        return None


class _Adapter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def stream_message(self, **_kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return _Stream(response)


def test_precise_placeholder_validation_retries_without_accepting_mutation() -> None:
    adapter = _Adapter([
        '{"translations":[{"index":0,"translated_text":"错误 <B1>文本</b1>"}]}',
        '{"translations":[{"index":0,"translated_text":"正确 <b1>文本</b1>"}]}',
    ])
    config = SimpleNamespace(
        model="test",
        max_tokens=1024,
        timeout=10,
        context_window=4096,
        reasoning_effort=None,
    )

    translated, _usage = asyncio.run(_request_translation_batch(
        adapter,
        config,
        "zh-CN",
        ["Text <b1>formula</b1>"],
        preserve_placeholders=True,
    ))

    assert translated == ["正确 <b1>文本</b1>"]
    assert adapter.calls == 2


def test_worker_bridge_correlates_concurrent_translation_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = document_worker._Bridge()

    def respond(payload: dict[str, object]) -> None:
        request_id = str(payload["request_id"])
        source = str(payload["text"])
        with bridge._lock:
            event, _response = bridge._responses[request_id]
            bridge._responses[request_id] = (
                event,
                {"type": "translation_response", "translated_text": f"translated:{source}"},
            )
            event.set()

    monkeypatch.setattr(document_worker, "_emit", respond)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bridge.translate, ["one", "two", "three", "four"]))

    assert results == ["translated:one", "translated:two", "translated:three", "translated:four"]
    assert bridge._responses == {}


def test_worker_runtime_blocks_external_network() -> None:
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    try:
        document_worker._disable_network()
        with pytest.raises(OSError, match="network access is disabled"):
            socket.create_connection(("example.com", 443))
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create_connection
