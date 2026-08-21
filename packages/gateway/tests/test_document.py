from __future__ import annotations

import json
from io import BytesIO
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from crabcode_gateway.routes.document import (
    MANIFEST_RELATIVE_PATH,
    _assert_public_host,
    _blog_revision,
    _file_extension,
    _finalize_document_job,
    _redacted_url,
    _recover_translation_job,
    _safe_name,
    _validate_signature,
    _validate_mime,
    _translation_job_progress,
    _validated_new_workspace,
    _validated_workspace,
    router,
)
from crabcode_gateway.schemas import WorkspaceInfo
from crabcode_gateway.schemas import core_event_to_payload
from crabcode_core.types.event import DocumentJobEvent


def _one_page_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def test_document_names_and_urls_are_sanitized() -> None:
    assert _safe_name("../A:B?.pdf") == "A-B-.pdf"
    assert _file_extension("paper.PDF") == ".pdf"
    assert _redacted_url("https://example.com/paper.pdf?token=secret#page=2") == "https://example.com/paper.pdf"


def test_document_signature_rejects_disguised_pdf(tmp_path: Path) -> None:
    candidate = tmp_path / "paper.pdf"
    candidate.write_bytes(b"not a pdf")
    with pytest.raises(HTTPException) as caught:
        _validate_signature(candidate, ".pdf")
    assert caught.value.status_code == 415


def test_document_mime_and_openxml_structure_are_validated(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as caught:
        _validate_mime(".pdf", "image/png")
    assert caught.value.status_code == 415

    candidate = tmp_path / "paper.docx"
    with zipfile.ZipFile(candidate, "w") as archive:
        archive.writestr("unrelated.txt", "not a Word document")
    with pytest.raises(HTTPException) as caught:
        _validate_signature(candidate, ".docx")
    assert caught.value.status_code == 415


def test_workspace_creation_stays_inside_allowed_root_and_disambiguates(tmp_path: Path) -> None:
    existing = tmp_path / "Paper"
    existing.mkdir()
    resolved = _validated_new_workspace(str(existing), (tmp_path.resolve(),))
    assert resolved == tmp_path / "Paper-2"

    with pytest.raises(HTTPException) as caught:
        _validated_new_workspace(str(tmp_path.parent / "outside"), (tmp_path.resolve(),))
    assert caught.value.status_code == 403


def test_existing_workspace_requires_document_manifest(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as caught:
        _validated_workspace(str(tmp_path), (tmp_path.resolve(),))
    assert caught.value.status_code == 404

    manifest = tmp_path / MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert _validated_workspace(str(tmp_path), (tmp_path.resolve(),)) == tmp_path.resolve()


def test_blog_revision_is_stable() -> None:
    assert _blog_revision("hello") == _blog_revision("hello")
    assert _blog_revision("hello") != _blog_revision("hello\n")


def test_document_job_event_serializes_for_websocket_clients() -> None:
    payload = core_event_to_payload(DocumentJobEvent(
        action="translate",
        status="running",
        locale="zh-CN",
        current=4,
        total=10,
    ))
    assert payload.model_dump() == {
        "type": "document_job",
        "action": "translate",
        "status": "running",
        "locale": "zh-CN",
        "source": "",
        "current": 4,
        "total": 10,
        "message": "",
    }


def test_private_document_url_is_rejected() -> None:
    with pytest.raises(HTTPException) as caught:
        _assert_public_host("http://127.0.0.1/document.pdf")
    assert caught.value.status_code == 400


def test_translation_job_requires_an_exact_block_mapping(tmp_path: Path) -> None:
    internal = tmp_path / ".crabcode" / "document"
    job = internal / "jobs" / "operation-1"
    job.mkdir(parents=True)
    (internal / "manifest.json").write_text(json.dumps({
        "source": {"sha256": "source-hash"},
        "layout": {"fingerprint": "layout-hash"},
        "translations": {},
    }))
    (internal / "layout.json").write_text(json.dumps({
        "pages": [{"blocks": [
            {"id": "p1-b0", "text": "Hello"},
            {"id": "p1-b1", "text": "World"},
        ]}],
    }))
    (job / "translation.json").write_text(json.dumps({
        "source_sha256": "source-hash",
        "layout_fingerprint": "layout-hash",
        "blocks": [
            {"id": "p1-b0", "translated_text": "你好"},
            {"id": "p1-b0", "translated_text": "重复"},
        ],
    }))
    with pytest.raises(ValueError, match="duplicate"):
        _finalize_document_job(tmp_path, "operation-1", "translate", "zh-CN", "original")
    assert not (internal / "translations" / "zh-CN.json").exists()

    (job / "translation.json").write_text(json.dumps({
        "source_sha256": "source-hash",
        "layout_fingerprint": "layout-hash",
        "blocks": [
            {"id": "p1-b1", "translated_text": "世界"},
            {"id": "p1-b0", "translated_text": "你好"},
        ],
    }))
    assert _finalize_document_job(tmp_path, "operation-1", "translate", "zh-CN", "original") == (2, 2)
    published = json.loads((internal / "translations" / "zh-CN.json").read_text())
    assert [block["id"] for block in published["blocks"]] == ["p1-b0", "p1-b1"]
    assert not job.exists()

    old_job = internal / "jobs" / "interrupted" / "translation.json"
    old_job.parent.mkdir(parents=True)
    old_job.write_text(json.dumps({
        "locale": "zh-CN",
        "source_sha256": "source-hash",
        "layout_fingerprint": "layout-hash",
        "blocks": [{"id": "p1-b0", "translated_text": "你好"}],
    }))
    assert _recover_translation_job(tmp_path, "operation-2", "zh-CN") == 1
    assert _translation_job_progress(tmp_path, "operation-2") == 1


def test_document_job_rejects_symlinked_internal_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    internal = workspace / ".crabcode" / "document"
    outside = tmp_path / "outside"
    internal.mkdir(parents=True)
    outside.mkdir()
    (internal / "manifest.json").write_text(json.dumps({"source": {"sha256": "hash"}}))
    (internal / "layout.json").write_text(json.dumps({"pages": []}))
    (internal / "jobs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="jobs directory"):
        _recover_translation_job(workspace, "operation-1", "zh-CN")


def test_pdf_upload_layout_asset_and_blog_round_trip(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.workspace_info = WorkspaceInfo(
        startup_cwd=str(tmp_path),
        home=str(tmp_path),
        browse_roots=[str(tmp_path)],
        documents_dir=str(tmp_path / "Documents" / "CrabCode"),
    )
    app.include_router(router)
    workspace = tmp_path / "Documents" / "CrabCode" / "Paper"

    with TestClient(app) as client:
        broken_workspace = workspace.with_name("Broken")
        broken = client.post(
            "/document/import/upload",
            data={
                "workspace_path": str(broken_workspace),
                "project_id": "broken-project",
                "project_name": "Broken",
            },
            files={"file": ("broken.pdf", b"%PDF-1.4\nnot-a-pdf", "application/pdf")},
        )
        assert broken.status_code == 422
        assert not broken_workspace.exists()
        assert not list(workspace.parent.glob(".Broken.import-*"))

        imported = client.post(
            "/document/import/upload",
            data={
                "workspace_path": str(workspace),
                "project_id": "project-1",
                "project_name": "Paper",
            },
            files={"file": ("paper.pdf", _one_page_pdf(), "application/pdf")},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["workspace"] == str(workspace)
        assert imported.json()["pdf"]["page_count"] == 1
        assert imported.json()["jobs"] == {}
        assert (workspace / "source" / "paper.pdf").is_file()
        assert (workspace / ".crabcode" / "document" / "rendered.pdf").is_file()

        layout = client.put(
            "/document/layout",
            params={"workspace": str(workspace)},
            json={
                "fingerprint": "pdf-fingerprint",
                "page_count": 1,
                "pages": [{
                    "width": 612,
                    "height": 792,
                    "blocks": [{"id": "p1-b0", "text": "Hello", "x": 0.1, "y": 0.2}],
                }],
            },
        )
        assert layout.status_code == 200, layout.text
        assert layout.json()["text_pages"] == 1
        assert "Hello" in (workspace / ".crabcode" / "document" / "content.md").read_text()

        asset = client.get(
            "/document/asset",
            params={"workspace": str(workspace)},
            headers={"Range": "bytes=0-7"},
        )
        assert asset.status_code == 206
        assert len(asset.content) == 8
        assert asset.content.startswith(b"%PDF-1.")
        assert asset.headers["accept-ranges"] == "bytes"

        assets = workspace / "blog-assets"
        assets.mkdir()
        (assets / "chart.png").write_bytes(b"image-bytes")
        (workspace / "secret.png").write_bytes(b"not-an-asset")
        image = client.get(
            "/document/blog-asset",
            params={"workspace": str(workspace), "path": "chart.png"},
        )
        assert image.status_code == 200
        assert image.content == b"image-bytes"
        traversal = client.get(
            "/document/blog-asset",
            params={"workspace": str(workspace), "path": "../secret.png"},
        )
        assert traversal.status_code == 403

        saved = client.put(
            "/document/blog",
            params={"workspace": str(workspace)},
            json={"revision": None, "markdown": "# Paper\n", "language": "en"},
        )
        assert saved.status_code == 200, saved.text
        revision = saved.json()["revision"]
        assert revision
        assert client.get("/document/blog", params={"workspace": str(workspace)}).json() == {
            "markdown": "# Paper\n",
            "revision": revision,
            "language": "en",
        }
        conflict = client.put(
            "/document/blog",
            params={"workspace": str(workspace)},
            json={"revision": None, "markdown": "stale", "language": "en"},
        )
        assert conflict.status_code == 409
