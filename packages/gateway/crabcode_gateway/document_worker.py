"""Isolated BabelDOC worker using an NDJSON translation bridge.

This module runs inside the optional engine virtual environment.  It receives
only document paths and translated strings; provider credentials remain in the
Gateway process.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()


def _emit(payload: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


class _Bridge:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._responses: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}

    def read_forever(self) -> None:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "cancel":
                self.cancelled.set()
                with self._lock:
                    pending = list(self._responses.values())
                for event, _ in pending:
                    event.set()
                continue
            if message.get("type") != "translation_response":
                continue
            request_id = message.get("request_id")
            if not isinstance(request_id, str):
                continue
            with self._lock:
                pending = self._responses.get(request_id)
                if pending is not None:
                    self._responses[request_id] = (pending[0], message)
                    pending[0].set()

    def translate(self, text: str) -> str:
        if self.cancelled.is_set():
            raise asyncio.CancelledError
        request_id = uuid.uuid4().hex
        event = threading.Event()
        with self._lock:
            self._responses[request_id] = (event, None)
        _emit({"type": "translate_request", "request_id": request_id, "text": text})
        while not event.wait(0.25):
            if self.cancelled.is_set():
                raise asyncio.CancelledError
        with self._lock:
            _, response = self._responses.pop(request_id, (event, None))
        if self.cancelled.is_set():
            raise asyncio.CancelledError
        if not response or response.get("error"):
            raise RuntimeError(str((response or {}).get("error") or "translation bridge closed"))
        translated = response.get("translated_text")
        if not isinstance(translated, str) or not translated.strip():
            raise RuntimeError("translation bridge returned empty text")
        return translated


def _configure_asset_cache(engine_root: Path) -> None:
    assets = engine_root / "assets"
    if not (assets / "asset-manifest.json").is_file():
        raise RuntimeError("document engine assets are missing")
    # BabelDOC 0.6.4 fixes its cache below Path.home().  Repoint the module
    # globals before importing any asset consumers so no runtime download is
    # attempted and all managed files stay inside the engine directory.
    import babeldoc.const as babel_const

    babel_const.CACHE_FOLDER = assets
    babel_const.TIKTOKEN_CACHE_FOLDER = assets / "tiktoken"
    babel_const.TIKTOKEN_CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(babel_const.TIKTOKEN_CACHE_FOLDER)


def _disable_network() -> None:
    """Make accidental BabelDOC asset/provider HTTP calls fail immediately."""
    original_connect = socket.socket.connect

    def local_only_connect(sock: socket.socket, address: Any) -> Any:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise OSError("network access is disabled in the document worker")
        return original_connect(sock, address)

    def no_network_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("network access is disabled in the document worker")

    socket.socket.connect = local_only_connect  # type: ignore[method-assign]
    socket.create_connection = no_network_connection  # type: ignore[assignment]


def _write_content_json(pdf_path: Path, output: Path) -> int:
    import pymupdf

    pages: list[dict[str, Any]] = []
    with pymupdf.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            paragraphs = [
                str(block[4]).strip()
                for block in page.get_text("blocks", sort=True)
                if len(block) > 4 and str(block[4]).strip()
            ]
            pages.append({"page": index, "paragraphs": paragraphs})
    output.write_text(
        json.dumps(
            {"schema_version": 1, "engine": "precise", "pages": pages},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return len(pages)


def _translate_with_progress(
    config: Any,
    bridge: _Bridge,
    *,
    translate: Any,
    get_stages: Any,
    progress_monitor_type: Any,
) -> Any:
    """Run BabelDOC synchronously while forwarding its progress callbacks.

    BabelDOC 0.6.4's async wrapper waits for a separate finish event after the
    executor has returned.  That event can be lost, leaving a completed PDF at
    99% forever.  The synchronous API already returns the authoritative result,
    so run it in our worker thread and use callbacks only for progress updates.
    """

    def progress_changed(**event: Any) -> None:
        if event.get("type") not in {"progress_start", "progress_update", "progress_end"}:
            return
        _emit({
            "type": "progress",
            "stage": str(event.get("stage") or "processing"),
            "overall_progress": float(event.get("overall_progress") or 0),
        })

    # ProgressMonitor.on_finish invokes its finish callback whenever a cancel
    # event is supplied.  Translation errors and the returned result are
    # propagated directly by do_translate, so this callback is intentionally a
    # no-op rather than another completion channel.
    def finished(**_event: Any) -> None:
        return None

    with progress_monitor_type(
        get_stages(config),
        progress_change_callback=progress_changed,
        finish_callback=finished,
        cancel_event=bridge.cancelled,
        report_interval=config.report_interval,
    ) as progress_monitor:
        return translate(progress_monitor, config)


async def _run(start: dict[str, Any], bridge: _Bridge) -> None:
    engine_root = Path(str(start["engine_root"])).resolve(strict=True)
    input_path = Path(str(start["input_path"])).resolve(strict=True)
    job_dir = Path(str(start["job_dir"])).resolve(strict=True)
    if not input_path.is_file() or not job_dir.is_dir():
        raise RuntimeError("document worker input paths are invalid")
    engine_version = str(start.get("engine_version") or "")
    if not engine_version:
        raise RuntimeError("document worker engine version is missing")
    _configure_asset_cache(engine_root)
    _disable_network()

    from babeldoc.docvision.base_doclayout import DocLayoutModel
    from babeldoc.format.pdf.high_level import do_translate
    from babeldoc.format.pdf.high_level import get_translation_stage
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode
    from babeldoc.progress_monitor import ProgressMonitor
    from babeldoc.translator.translator import BaseTranslator

    class CrabCodeTranslator(BaseTranslator):
        name = "crabcode"

        def __init__(self) -> None:
            super().__init__("auto", str(start["locale"]), True)
            self.model = "gateway"

        def do_translate(self, text, rate_limit_params=None):  # noqa: ANN001, ARG002
            if not isinstance(text, str):
                raise ValueError("translation input must be text")
            return bridge.translate(text)

        def do_llm_translate(self, text, rate_limit_params=None):  # noqa: ANN001, ARG002
            raise NotImplementedError

    output_dir = job_dir / "precise-output"
    working_dir = job_dir / "precise-working"
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir.mkdir(parents=True, exist_ok=True)
    translator = CrabCodeTranslator()
    model = DocLayoutModel.load_onnx()
    config = TranslationConfig(
        translator=translator,
        input_file=input_path,
        lang_in="auto",
        lang_out=str(start["locale"]),
        doc_layout_model=model,
        output_dir=output_dir,
        working_dir=working_dir,
        no_dual=True,
        no_mono=False,
        qps=max(1, int(start.get("concurrency") or 1)),
        pool_max_workers=max(1, int(start.get("concurrency") or 1)),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        skip_scanned_detection=False,
        ocr_workaround=False,
        auto_enable_ocr_workaround=False,
        auto_extract_glossary=False,
        save_auto_extracted_glossary=False,
        report_interval=0.1,
    )
    result = await asyncio.to_thread(
        _translate_with_progress,
        config,
        bridge,
        translate=do_translate,
        get_stages=get_translation_stage,
        progress_monitor_type=ProgressMonitor,
    )
    if result is None or result.mono_pdf_path is None:
        raise RuntimeError("BabelDOC did not produce a monolingual PDF")

    translated_pdf = job_dir / "translated.pdf"
    shutil.copy2(Path(result.mono_pdf_path), translated_pdf)
    page_count = _write_content_json(translated_pdf, job_dir / "content.json")
    diagnostics = {
        "schema_version": 1,
        "engine": "precise",
        "engine_version": engine_version,
        "source_sha256": str(start.get("source_sha256") or ""),
        "page_count": page_count,
        "warnings": [],
    }
    (job_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit({
        "type": "completed",
        "page_count": page_count,
        "warnings": [],
    })


def main() -> None:
    raw = sys.stdin.readline()
    try:
        start = json.loads(raw)
    except json.JSONDecodeError:
        _emit({"type": "error", "code": "invalid_request", "message": "invalid worker start message"})
        raise SystemExit(2)
    if not isinstance(start, dict) or start.get("type") != "start":
        _emit({"type": "error", "code": "invalid_request", "message": "missing worker start message"})
        raise SystemExit(2)
    bridge = _Bridge()
    reader = threading.Thread(target=bridge.read_forever, name="document-worker-stdin", daemon=True)
    reader.start()
    try:
        asyncio.run(_run(start, bridge))
    except asyncio.CancelledError:
        _emit({"type": "error", "code": "cancelled", "message": "document translation cancelled"})
        raise SystemExit(130)
    except Exception as exc:
        code = "scanned_pdf_unsupported" if re.search(r"scanned pdf", str(exc), re.IGNORECASE) else "precise_failed"
        _emit({"type": "error", "code": code, "message": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
