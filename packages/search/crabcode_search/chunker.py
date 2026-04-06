"""Code chunker — splits source files into semantic chunks.

Uses tree-sitter AST parsing when available (pip install crabcode-search[ast]),
falling back to regex-based boundary detection otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from tree_sitter_language_pack import get_parser as _ts_get_parser

    _HAS_TREE_SITTER = True
except ImportError:
    _HAS_TREE_SITTER = False

MAX_CHUNK_LINES = 80
MIN_CHUNK_LINES = 3


@dataclass
class ChunkMeta:
    file_path: str
    start_line: int
    end_line: int
    signature: str
    content: str

    @property
    def display(self) -> str:
        return f"{self.file_path}:{self.start_line}-{self.end_line}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk_file(file_path: str, content: str) -> list[ChunkMeta]:
    """Split a single file into semantic chunks.

    Tries tree-sitter AST parsing first; falls back to regex if unavailable.
    """
    if not content.strip():
        return []

    if _HAS_TREE_SITTER:
        lang = _ext_to_ts_language(file_path)
        if lang:
            try:
                return _chunk_file_treesitter(file_path, content, lang)
            except Exception:
                pass

    return _chunk_file_regex(file_path, content)


# ---------------------------------------------------------------------------
# Tree-sitter AST chunking
# ---------------------------------------------------------------------------

_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".lua": "lua",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
    ".zig": "zig",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".jl": "julia",
    ".dart": "dart",
    ".r": "r",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
}

_DEFINITION_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({
        "function_definition", "class_definition", "decorated_definition",
    }),
    "javascript": frozenset({
        "function_declaration", "class_declaration", "export_statement",
        "lexical_declaration", "variable_declaration",
    }),
    "typescript": frozenset({
        "function_declaration", "class_declaration", "export_statement",
        "lexical_declaration", "interface_declaration",
        "type_alias_declaration", "enum_declaration",
    }),
    "tsx": frozenset({
        "function_declaration", "class_declaration", "export_statement",
        "lexical_declaration", "interface_declaration",
        "type_alias_declaration", "enum_declaration",
    }),
    "rust": frozenset({
        "function_item", "impl_item", "struct_item", "enum_item",
        "trait_item", "mod_item", "const_item", "static_item",
        "type_item", "macro_definition",
    }),
    "go": frozenset({
        "function_declaration", "method_declaration", "type_declaration",
    }),
    "java": frozenset({
        "class_declaration", "interface_declaration", "method_declaration",
        "enum_declaration", "record_declaration",
    }),
    "c": frozenset({
        "function_definition", "struct_specifier", "enum_specifier",
        "type_definition",
    }),
    "cpp": frozenset({
        "function_definition", "class_specifier", "struct_specifier",
        "enum_specifier", "namespace_definition", "template_declaration",
    }),
    "c_sharp": frozenset({
        "class_declaration", "interface_declaration", "method_declaration",
        "enum_declaration", "struct_declaration", "namespace_declaration",
    }),
    "ruby": frozenset({
        "method", "class", "module", "singleton_method",
    }),
    "swift": frozenset({
        "function_declaration", "class_declaration", "struct_declaration",
        "enum_declaration", "protocol_declaration",
    }),
    "kotlin": frozenset({
        "function_declaration", "class_declaration", "object_declaration",
    }),
    "scala": frozenset({
        "function_definition", "class_definition", "object_definition",
        "trait_definition",
    }),
    "php": frozenset({
        "function_definition", "class_declaration", "method_declaration",
    }),
    "lua": frozenset({
        "function_declaration", "local_function",
    }),
    "elixir": frozenset({
        "call",
    }),
    "haskell": frozenset({
        "function", "type_class_declaration", "data_declaration",
    }),
}

_CONTAINER_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"class_definition", "decorated_definition"}),
    "javascript": frozenset({"class_declaration", "class_body"}),
    "typescript": frozenset({"class_declaration", "class_body"}),
    "tsx": frozenset({"class_declaration", "class_body"}),
    "rust": frozenset({"impl_item", "trait_item", "mod_item"}),
    "go": frozenset(),
    "java": frozenset({"class_declaration", "class_body", "interface_declaration"}),
    "c": frozenset(),
    "cpp": frozenset({"class_specifier", "struct_specifier", "namespace_definition"}),
    "c_sharp": frozenset({"class_declaration", "struct_declaration", "namespace_declaration"}),
    "ruby": frozenset({"class", "module"}),
    "swift": frozenset({"class_declaration", "struct_declaration"}),
    "kotlin": frozenset({"class_declaration", "object_declaration"}),
    "scala": frozenset({"class_definition", "object_definition", "trait_definition"}),
    "php": frozenset({"class_declaration"}),
}

_METHOD_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"function_definition", "decorated_definition"}),
    "javascript": frozenset({"method_definition"}),
    "typescript": frozenset({"method_definition", "public_field_definition"}),
    "tsx": frozenset({"method_definition", "public_field_definition"}),
    "rust": frozenset({"function_item"}),
    "java": frozenset({"method_declaration", "constructor_declaration"}),
    "cpp": frozenset({"function_definition"}),
    "c_sharp": frozenset({"method_declaration", "constructor_declaration"}),
    "ruby": frozenset({"method", "singleton_method"}),
    "swift": frozenset({"function_declaration"}),
    "kotlin": frozenset({"function_declaration"}),
    "scala": frozenset({"function_definition"}),
    "php": frozenset({"method_declaration"}),
}


def _ext_to_ts_language(file_path: str) -> str | None:
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANGUAGE.get(ext)


def _chunk_file_treesitter(
    file_path: str, content: str, language: str
) -> list[ChunkMeta]:
    parser = _ts_get_parser(language)
    tree = parser.parse(content.encode("utf-8", errors="replace"))
    root = tree.root_node

    lines = content.splitlines(keepends=True)
    definition_types = _DEFINITION_TYPES.get(language, frozenset())
    container_types = _CONTAINER_TYPES.get(language, frozenset())
    method_types = _METHOD_TYPES.get(language, frozenset())

    chunks: list[ChunkMeta] = []
    header_nodes: list[Any] = []

    for child in root.children:
        if child.type in definition_types:
            if header_nodes:
                _flush_header(file_path, lines, header_nodes, chunks)
                header_nodes = []

            node_lines = child.end_point[0] - child.start_point[0] + 1

            if (
                node_lines > MAX_CHUNK_LINES
                and child.type in container_types
                and method_types
            ):
                _chunk_container_node(
                    file_path, lines, child, language,
                    container_types, method_types, chunks,
                )
            else:
                _add_node_chunk(file_path, lines, child, chunks)
        else:
            header_nodes.append(child)

    if header_nodes:
        _flush_header(file_path, lines, header_nodes, chunks)

    return chunks


def _add_node_chunk(
    file_path: str,
    lines: list[str],
    node: Any,
    chunks: list[ChunkMeta],
) -> None:
    start = node.start_point[0]
    end = min(node.end_point[0] + 1, len(lines))

    if end - start > MAX_CHUNK_LINES:
        end = start + MAX_CHUNK_LINES

    chunk_lines = lines[start:end]
    text = "".join(chunk_lines).strip()
    if not text or len(chunk_lines) < MIN_CHUNK_LINES:
        return

    sig = _node_signature(node, lines)
    chunks.append(ChunkMeta(
        file_path=file_path,
        start_line=start + 1,
        end_line=end,
        signature=sig,
        content=text,
    ))


_BODY_BLOCK_TYPES = frozenset({
    "block", "body", "class_body", "enum_body", "impl_body",
    "declaration_list", "field_declaration_list", "statement_block",
    "compound_statement",
})


def _chunk_container_node(
    file_path: str,
    lines: list[str],
    node: Any,
    language: str,
    container_types: frozenset[str],
    method_types: frozenset[str],
    chunks: list[ChunkMeta],
) -> None:
    """Split a large class/impl/trait into header + individual methods."""
    children_defs: list[Any] = []

    def _find_methods(n: Any) -> None:
        for child in n.children:
            if child.type in method_types:
                children_defs.append(child)
            elif (
                child.type in container_types
                or child.type in _BODY_BLOCK_TYPES
                or child.type.endswith("_body")
            ):
                _find_methods(child)

    _find_methods(node)

    if not children_defs:
        _add_node_chunk(file_path, lines, node, chunks)
        return

    header_start = node.start_point[0]
    first_method_start = children_defs[0].start_point[0]

    if first_method_start > header_start:
        header_end = min(first_method_start, header_start + MAX_CHUNK_LINES)
        header_text = "".join(lines[header_start:header_end]).strip()
        if header_text and (header_end - header_start) >= MIN_CHUNK_LINES:
            sig = _node_signature(node, lines)
            chunks.append(ChunkMeta(
                file_path=file_path,
                start_line=header_start + 1,
                end_line=header_end,
                signature=sig,
                content=header_text,
            ))

    for method_node in children_defs:
        _add_node_chunk(file_path, lines, method_node, chunks)


def _flush_header(
    file_path: str,
    lines: list[str],
    nodes: list[Any],
    chunks: list[ChunkMeta],
) -> None:
    if not nodes:
        return
    start = nodes[0].start_point[0]
    end = nodes[-1].end_point[0] + 1
    if end - start < MIN_CHUNK_LINES:
        return

    text = "".join(lines[start:end]).strip()
    if not text:
        return

    if end - start > MAX_CHUNK_LINES:
        end = start + MAX_CHUNK_LINES
        text = "".join(lines[start:end]).strip()

    chunks.append(ChunkMeta(
        file_path=file_path,
        start_line=start + 1,
        end_line=end,
        signature="(module header)",
        content=text,
    ))


def _node_signature(node: Any, lines: list[str]) -> str:
    first_line = lines[node.start_point[0]].strip() if node.start_point[0] < len(lines) else ""
    if len(first_line) > 120:
        first_line = first_line[:117] + "..."
    return first_line


# ---------------------------------------------------------------------------
# Regex fallback chunking
# ---------------------------------------------------------------------------

_FUNC_CLASS_RE = re.compile(
    r"^(?:"
    r"(?:export\s+)?(?:async\s+)?(?:def|class|function|fn|func|pub\s+fn|pub\s+async\s+fn)"
    r"|(?:export\s+)?(?:const|let|var)\s+\w+\s*="
    r"|(?:public|private|protected|static|\s)*(?:class|interface|enum|record)\s"
    r"|(?:public|private|protected|static|\s)*\w[\w<>\[\],\s]*\s+\w+\s*\("
    r")\s*",
    re.MULTILINE,
)


def _chunk_file_regex(file_path: str, content: str) -> list[ChunkMeta]:
    """Regex-based chunking — used when tree-sitter is not available."""
    lines = content.splitlines(keepends=True)
    if not lines:
        return []

    boundaries: list[int] = []
    for i, line in enumerate(lines):
        if _FUNC_CLASS_RE.match(line):
            boundaries.append(i)

    if boundaries:
        return _chunk_by_boundaries(file_path, lines, boundaries)
    return _chunk_by_paragraphs(file_path, lines)


def _chunk_by_boundaries(
    file_path: str,
    lines: list[str],
    boundaries: list[int],
) -> list[ChunkMeta]:
    chunks: list[ChunkMeta] = []

    if boundaries[0] > 0:
        header_lines = lines[: boundaries[0]]
        header_text = "".join(header_lines).strip()
        if header_text and len(header_lines) >= MIN_CHUNK_LINES:
            chunks.append(ChunkMeta(
                file_path=file_path,
                start_line=1,
                end_line=boundaries[0],
                signature="(module header)",
                content=header_text,
            ))

    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)

        if end - start > MAX_CHUNK_LINES:
            end = start + MAX_CHUNK_LINES

        chunk_lines = lines[start:end]
        text = "".join(chunk_lines).strip()
        if not text or len(chunk_lines) < MIN_CHUNK_LINES:
            continue

        sig = lines[start].strip()
        if len(sig) > 120:
            sig = sig[:117] + "..."

        chunks.append(ChunkMeta(
            file_path=file_path,
            start_line=start + 1,
            end_line=end,
            signature=sig,
            content=text,
        ))

    return chunks


def _chunk_by_paragraphs(
    file_path: str,
    lines: list[str],
) -> list[ChunkMeta]:
    """Fallback: split by blank-line paragraphs, capped at MAX_CHUNK_LINES."""
    chunks: list[ChunkMeta] = []
    current_start = 0
    current_lines: list[str] = []

    def _flush() -> None:
        if not current_lines:
            return
        text = "".join(current_lines).strip()
        if text and len(current_lines) >= MIN_CHUNK_LINES:
            sig = current_lines[0].strip()
            if len(sig) > 120:
                sig = sig[:117] + "..."
            chunks.append(ChunkMeta(
                file_path=file_path,
                start_line=current_start + 1,
                end_line=current_start + len(current_lines),
                signature=sig,
                content=text,
            ))

    for i, line in enumerate(lines):
        if line.strip() == "" and len(current_lines) >= MIN_CHUNK_LINES:
            _flush()
            current_start = i + 1
            current_lines = []
        else:
            current_lines.append(line)
            if len(current_lines) >= MAX_CHUNK_LINES:
                _flush()
                current_start = i + 1
                current_lines = []

    _flush()
    return chunks


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go",
    ".java", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".cs", ".rb", ".swift", ".kt", ".kts", ".scala", ".lua",
    ".php", ".sh", ".bash", ".zsh", ".fish", ".r", ".m", ".mm",
    ".toml", ".yaml", ".yml", ".json", ".xml", ".html",
    ".css", ".scss", ".less", ".sql", ".graphql", ".proto",
    ".md", ".txt", ".rst", ".tex", ".el", ".vim",
    ".zig", ".nim", ".ex", ".exs", ".erl", ".hrl",
    ".clj", ".cljs", ".hs", ".ml", ".mli", ".f90",
    ".jl", ".dart", ".v", ".sv",
})


def is_indexable(path: Path) -> bool:
    """Check if a file should be indexed."""
    return path.suffix.lower() in _TEXT_EXTENSIONS
