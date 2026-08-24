import json
from pathlib import Path

from crabcode_gateway.routes.document import _finalize_document_job
from crabcode_gateway.routes.event import _blog_language_instruction


def _document_workspace(tmp_path: Path) -> Path:
    internal = tmp_path / ".crabcode" / "document"
    (internal / "jobs" / "operation-1").mkdir(parents=True)
    (internal / "translations").mkdir()
    (internal / "layout.json").write_text(
        json.dumps({"fingerprint": "layout-1", "pages": [{"blocks": []}]}),
        encoding="utf-8",
    )
    (internal / "manifest.json").write_text(
        json.dumps(
            {
                "source": {"sha256": "source-1"},
                "layout": {"fingerprint": "layout-1"},
                "translations": {},
                "blog": None,
                "jobs": {},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_blog_finalize_is_idempotent_after_atomic_publish(tmp_path: Path) -> None:
    workspace = _document_workspace(tmp_path)
    staged = workspace / ".crabcode" / "document" / "jobs" / "operation-1" / "blog.md"
    staged.write_text("# 中文标题\n\n这是中文正文。", encoding="utf-8")

    assert _finalize_document_job(
        workspace, "operation-1", "generate_blog", "zh-CN", "original"
    ) == (1, 1)
    assert _finalize_document_job(
        workspace, "operation-1", "generate_blog", "zh-CN", "original", language="zh-CN"
    ) == (1, 1)
    assert (workspace / "blog.md").is_file()
    assert not staged.parent.exists()
    manifest = json.loads((workspace / ".crabcode" / "document" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["blog"]["language"] == "zh-CN"


def test_blog_language_instruction_expands_locale_code() -> None:
    instruction = _blog_language_instruction("zh-CN")
    assert "简体中文" in instruction
    assert "不要因为原文是英文而输出英文" in instruction
