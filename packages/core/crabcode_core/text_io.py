"""UTF-8 text I/O that preserves existing file newline conventions."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Utf8Text:
    text: str
    newline: str | None
    has_bom: bool


def read_utf8_text(path: Path) -> Utf8Text:
    raw = path.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    if has_bom:
        raw = raw[len(codecs.BOM_UTF8):]
    text = raw.decode("utf-8")
    return Utf8Text(text=text, newline=_detect_newline(text), has_bom=has_bom)


def write_utf8_text(
    path: Path,
    text: str,
    *,
    newline: str | None = None,
    has_bom: bool = False,
) -> int:
    if newline is not None:
        text = normalize_newlines(text, newline)
    raw = text.encode("utf-8")
    if has_bom:
        raw = codecs.BOM_UTF8 + raw
    return path.write_bytes(raw)


def normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


def _detect_newline(text: str) -> str | None:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    counts = [(crlf, "\r\n"), (lf, "\n"), (cr, "\r")]
    count, newline = max(counts, key=lambda item: item[0])
    return newline if count else None
