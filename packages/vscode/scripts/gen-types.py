#!/usr/bin/env python3
"""Auto-generate src/client/types.ts from packages/gateway/crabcode_gateway/schemas.py.

Reads the Pydantic model definitions in schemas.py and emits matching
TypeScript interfaces / type aliases so the client stays in sync with
the gateway wire format.

Usage:
    python scripts/gen-types.py          # writes to src/client/types.ts
    python scripts/gen-types.py --check  # exits 1 if file is out-of-date
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

# ── Project root detection ────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMAS_PATH = ROOT / "packages" / "gateway" / "crabcode_gateway" / "schemas.py"
OUTPUT_PATH = ROOT / "packages" / "vscode" / "src" / "client" / "types.ts"

# ── Python → TypeScript type mapping ─────────────────────────────

PY_TO_TS: dict[str, str] = {
    "str": "string",
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "Any": "unknown",
    "dict[str, Any]": "Record<string, unknown>",
    # Imported type aliases do not have a class definition in schemas.py, so
    # keep the wire-level literal union explicit in the generated client.
    "ReasoningEffort": '"none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"',
}


def resolve_ts_type(annotation: ast.expr) -> str:
    """Convert a Python AST annotation to a TypeScript type string."""
    # Bare name  (e.g. str, int, bool)
    if isinstance(annotation, ast.Constant):
        if annotation.value is None:
            return "null"
        return PY_TO_TS.get(str(annotation.value), str(annotation.value))

    if isinstance(annotation, ast.Name):
        if annotation.id == "None":
            return "null"
        return PY_TO_TS.get(annotation.id, annotation.id)

    # Optional / union: X | None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = resolve_ts_type(annotation.left)
        right = resolve_ts_type(annotation.right)
        if right == "null":
            return f"{left} | null"
        return f"{left} | {right}"

    # Subscript: list[X], dict[K, V], Literal[...]
    if isinstance(annotation, ast.Subscript):
        value = getattr(annotation, "value", None)
        if isinstance(value, ast.Name):
            if value.id == "list":
                inner = resolve_ts_type(annotation.slice)
                return f"{inner}[]"
            if value.id == "dict":
                # dict[str, Any] → Record<string, unknown>
                if isinstance(annotation.slice, ast.Tuple):
                    key = resolve_ts_type(annotation.slice.elts[0])
                    val = resolve_ts_type(annotation.slice.elts[1])
                    return f"Record<{key}, {val}>"
                return "Record<string, unknown>"
            if value.id == "Literal":
                # Literal["a", "b"] → "a" | "b"
                members: list[str] = []
                elts = (
                    annotation.slice.elts
                    if isinstance(annotation.slice, ast.Tuple)
                    else [annotation.slice]
                )
                for elt in elts:
                    if isinstance(elt, ast.Constant):
                        members.append(json_literal(elt.value))
                return " | ".join(members)
        # Fallback
        return "unknown"

    # None constant
    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return "null"

    return "unknown"


def json_literal(val: object) -> str:
    """Format a Python value as a JSON/TS literal."""
    if isinstance(val, str):
        return f'"{val}"'
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    return "null"


# ── AST parsing helpers ──────────────────────────────────────────


@dataclass
class FieldInfo:
    name: str
    ts_type: str
    optional: bool  # has a default value OR is X | None
    default_ts: str | None  # TS representation of the default



@dataclass
class ModelInfo:
    name: str
    docstring: str
    fields: list[FieldInfo] = dc_field(default_factory=list)
    is_payload: bool = False  # has a `type` Literal discriminator


def extract_models(source: str) -> tuple[list[ModelInfo], list[str]]:
    """Parse schemas.py and return (models, event_payload_variants).

    event_payload_variants is the ordered list of class names inside the
    EventPayload Union (if present).
    """
    tree = ast.parse(source)

    models: list[ModelInfo] = []
    event_payload_variants: list[str] = []
    model_map: dict[str, ModelInfo] = {}

    for node in ast.iter_child_nodes(tree):
        # ── Class definitions ──
        if isinstance(node, ast.ClassDef):
            # Only process BaseModel subclasses
            is_basemodel = any(
                (isinstance(b, ast.Name) and b.id == "BaseModel")
                for b in node.bases
            )
            if not is_basemodel:
                continue

            docstring = ast.get_docstring(node) or ""
            mi = ModelInfo(name=node.name, docstring=docstring)

            for stmt in node.body:
                # Skip docstrings, methods, etc.
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue

                field_name = stmt.target.id
                ts_type = resolve_ts_type(stmt.annotation)

                # Determine default & optionality
                optional = False
                default_ts: str | None = None

                if stmt.value is not None:
                    # ``Field(...)`` can carry validation metadata without
                    # supplying a Pydantic default.  Such a field remains
                    # required on the wire; only ``default=`` or
                    # ``default_factory=`` makes it optional.
                    is_metadata_only_field = False
                    if isinstance(stmt.value, ast.Call):
                        func = getattr(stmt.value, "func", None)
                        if isinstance(func, ast.Name) and func.id == "Field":
                            is_metadata_only_field = not any(
                                kw.arg in {"default", "default_factory"}
                                for kw in stmt.value.keywords
                            )
                    if not is_metadata_only_field:
                        optional = True
                        default_ts = _default_to_ts(stmt.value)

                # Also optional if type includes `| null`
                if "| null" in ts_type:
                    optional = True

                # Check for Literal `type` field → payload discriminator
                if field_name == "type" and isinstance(stmt.annotation, ast.Subscript):
                    mi.is_payload = True

                mi.fields.append(
                    FieldInfo(
                        name=field_name,
                        ts_type=ts_type,
                        optional=optional,
                        default_ts=default_ts,
                    )
                )

            models.append(mi)
            model_map[node.name] = mi

        # ── EventPayload Union assignment ──
        # Handles both: EventPayload: Union[...] = Union[...] (AnnAssign)
        #           and: EventPayload = Union[...] (Assign)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "EventPayload" and isinstance(node.value, ast.Subscript):
                event_payload_variants = _extract_union_names(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "EventPayload":
                    if isinstance(node.value, ast.Subscript):
                        event_payload_variants = _extract_union_names(node.value)

    return models, event_payload_variants


def _extract_union_names(subscript: ast.Subscript) -> list[str]:
    """Extract class names from a Union[X, Y, Z] subscript."""
    names: list[str] = []
    if isinstance(subscript.slice, ast.Tuple):
        for elt in subscript.slice.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
    return names


def _default_to_ts(node: ast.expr) -> str:
    """Convert a Python AST default value to a TypeScript literal."""
    if isinstance(node, ast.Constant):
        return json_literal(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _default_to_ts(node.operand)
        return f"-{inner}"
    if isinstance(node, ast.List):
        items = ", ".join(_default_to_ts(e) for e in node.elts)
        return f"[{items}]"
    if isinstance(node, ast.Dict):
        pairs = []
        for k, v in zip(node.keys, node.values):
            k_ts = _default_to_ts(k) if k else ""
            v_ts = _default_to_ts(v)
            pairs.append(f"{k_ts}: {v_ts}")
        return "{" + ", ".join(pairs) + "}"
    if isinstance(node, ast.Name):
        if node.id == "True":
            return "true"
        if node.id == "False":
            return "false"
        if node.id == "None":
            return "null"
    # Field(default_factory=...)
    if isinstance(node, ast.Call):
        func = getattr(node, "func", None)
        if isinstance(func, ast.Attribute) and func.attr == "Field":
            # Look for default_factory= or default=
            for kw in node.keywords:
                if kw.arg == "default_factory":
                    # list → [], dict → {}
                    if isinstance(kw.value, ast.Name):
                        if kw.value.id == "list":
                            return "[]"
                        if kw.value.id == "dict":
                            return "{}"
                if kw.arg == "default":
                    return _default_to_ts(kw.value)
    return "undefined"


# ── Code generation ──────────────────────────────────────────────

HEADER = """\
/**
 * Auto-generated TypeScript types mirroring packages/gateway/crabcode_gateway/schemas.py
 *
 * ─── HOW TO REGENERATE ────────────────────────────────────────────
 *   python packages/vscode/scripts/gen-types.py
 *
 * Do NOT edit this file by hand unless you also update the generator.
 * ──────────────────────────────────────────────────────────────────
 */
"""


def _section_comment(title: str) -> str:
    underline = "─" * (65 - len(title))
    return f"\n// ── {title} {underline}\n"


def _doc_comment(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) == 1:
        return f"/** {lines[0]} */\n"
    out = "/**\n"
    for line in lines:
        out += f" * {line}\n"
    out += " */\n"
    return out


def generate_ts(models: list[ModelInfo], event_payload_variants: list[str]) -> str:
    """Produce the full TypeScript file content."""
    out = HEADER

    # ── Shared types ──
    response_keywords = ("Info", "Response")
    shared_models = [
        m
        for m in models
        if not m.is_payload
        and not m.name.endswith("Request")
        and not any(m.name.endswith(kw) for kw in response_keywords)
    ]
    if shared_models:
        out += _section_comment("Shared types")
        for m in shared_models:
            out += _emit_interface(m)

    # ── Request types ──
    request_models = [
        m for m in models if m.name.endswith("Request") and not m.is_payload
    ]
    if request_models:
        out += _section_comment("Request types")
        for m in request_models:
            out += _emit_interface(m)

    # ── Response / info types ──
    response_models = [
        m
        for m in models
        if any(m.name.endswith(kw) for kw in response_keywords) and not m.is_payload
    ]
    if response_models:
        out += _section_comment("Response / info types")
        for m in response_models:
            out += _emit_interface(m)

    # ── EventPayload variants ──
    payload_models = [m for m in models if m.is_payload]
    # Use the EventPayload Union ordering if available
    if event_payload_variants:
        name_to_model = {m.name: m for m in payload_models}
        ordered = []
        for name in event_payload_variants:
            if name in name_to_model:
                ordered.append(name_to_model[name])
        # Add any not in the union (shouldn't happen but be safe)
        for m in payload_models:
            if m not in ordered:
                ordered.append(m)
        payload_models = ordered

    if payload_models:
        out += _section_comment("EventPayload tagged union")
        # EventBus adds these fields after serializing the Pydantic payload.
        # They are intentionally kept out of the individual schemas because
        # the server.connected handshake and heartbeat do not carry a session.
        out += "/** Metadata attached by the Gateway EventBus to routed events. */\n"
        out += "export interface EventEnvelope {\n"
        out += "  session_id?: string;\n"
        out += "  operation_id?: string;\n"
        out += '  operation_scope?: "foreground" | "plan" | "background";\n'
        out += "  command?: string;\n"
        out += "  command_error?: boolean;\n"
        out += "}\n\n"
        for m in payload_models:
            out += _emit_interface(m)

    # ── Tagged union type ──
    if event_payload_variants:
        out += _section_comment("Tagged union")
        variants = " |\n    ".join(event_payload_variants)
        out += "export type EventPayload =\n"
        out += "  EventEnvelope & (\n"
        out += f"    {variants}\n"
        out += "  );\n"

    # ── Discriminator helper ──
    if event_payload_variants:
        out += _section_comment("Type discriminator helper")
        out += "export type EventPayloadType = EventPayload[\"type\"];\n"

    return out.rstrip() + "\n"


def _emit_interface(m: ModelInfo) -> str:
    """Emit a single TypeScript interface."""
    parts = ""
    if m.docstring:
        parts += _doc_comment(m.docstring)
    parts += f"export interface {m.name} {{\n"

    # EventBus appends routing metadata after serializing the Core payload.
    # Keep it on the concrete interface as well as EventEnvelope so handlers
    # that receive one payload variant (rather than EventPayload) can target
    # the originating session and operation.
    if m.is_payload:
        declared_names = {field.name for field in m.fields}
        if "session_id" not in declared_names:
            parts += "  session_id?: string;\n"
        if "operation_id" not in declared_names:
            parts += "  operation_id?: string;\n"
        if "operation_scope" not in declared_names:
            parts += '  operation_scope?: "foreground" | "plan" | "background";\n'

    # Sort: required fields first, then optional
    required = [f for f in m.fields if not f.optional]
    optional = [f for f in m.fields if f.optional]

    for f in required + optional:
        # Pydantic applies the Literal default at runtime, but `type` is the
        # discriminator on the wire and must remain required for TypeScript's
        # tagged-union narrowing to work.
        is_required_discriminator = m.is_payload and f.name == "type"
        opt_marker = "?" if f.optional and not is_required_discriminator else ""
        parts += f"  {f.name}{opt_marker}: {f.ts_type};\n"

    parts += "}\n\n"
    return parts


# ── Main ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate TypeScript types from schemas.py")
    parser.add_argument("--check", action="store_true", help="Check if generated file is up-to-date")
    args = parser.parse_args()

    source = SCHEMAS_PATH.read_text(encoding="utf-8")
    models, event_payload_variants = extract_models(source)
    generated = generate_ts(models, event_payload_variants)

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"FAIL: {OUTPUT_PATH} does not exist", file=sys.stderr)
            sys.exit(1)
        existing = OUTPUT_PATH.read_text(encoding="utf-8")
        if existing != generated:
            print(
                f"FAIL: {OUTPUT_PATH} is out-of-date. Run: python packages/vscode/scripts/gen-types.py",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"OK: {OUTPUT_PATH} is up-to-date")
        sys.exit(0)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(generated, encoding="utf-8")
    print(f"Generated {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
