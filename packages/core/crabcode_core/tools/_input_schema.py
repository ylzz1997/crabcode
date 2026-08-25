"""Normalize model-generated tool inputs against their JSON Schema."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_CAMEL_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)


@dataclass(frozen=True)
class ToolInputCorrection:
    path: str
    received: str
    expected: str


@dataclass(frozen=True)
class ToolInputNormalization:
    value: dict[str, Any]
    corrections: tuple[ToolInputCorrection, ...] = ()
    error: str | None = None


def _to_snake_case(key: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def _display_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _normalize_value(
    value: Any,
    schema: Any,
    path: str,
) -> tuple[Any, list[ToolInputCorrection], str | None]:
    if not isinstance(schema, dict):
        return value, [], None

    if isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return value, [], None
        normalized_items = []
        corrections: list[ToolInputCorrection] = []
        for index, item in enumerate(value):
            normalized, nested, error = _normalize_value(
                item,
                item_schema,
                f"{path}[{index}]",
            )
            if error:
                return value, corrections, error
            normalized_items.append(normalized)
            corrections.extend(nested)
        return normalized_items, corrections, None

    if not isinstance(value, dict):
        return value, [], None

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return value, [], None

    normalized: dict[str, Any] = {
        key: item for key, item in value.items() if key in properties
    }
    corrections: list[ToolInputCorrection] = []

    for received, item in value.items():
        if received in properties:
            continue
        expected = _to_snake_case(received)
        if expected == received or expected not in properties:
            normalized[received] = item
            continue

        received_path = _display_path(path, received)
        expected_path = _display_path(path, expected)
        if expected in normalized and normalized[expected] != item:
            return value, corrections, (
                f'Received "{received_path}"; did you mean "{expected_path}"? '
                f'Both "{received_path}" and "{expected_path}" were provided with '
                "different values, so the tool input could not be normalized unambiguously."
            )

        normalized.setdefault(expected, item)
        corrections.append(
            ToolInputCorrection(
                path=received_path,
                received=received,
                expected=expected,
            )
        )

    for key, item in list(normalized.items()):
        property_schema = properties.get(key)
        if not isinstance(property_schema, dict):
            continue
        normalized_item, nested, error = _normalize_value(
            item,
            property_schema,
            _display_path(path, key),
        )
        if error:
            return value, corrections, error
        normalized[key] = normalized_item
        corrections.extend(nested)

    return normalized, corrections, None


def normalize_tool_input(
    tool_input: dict[str, Any],
    input_schema: dict[str, Any],
) -> ToolInputNormalization:
    """Normalize unambiguous camelCase keys declared as snake_case by a schema."""
    value, corrections, error = _normalize_value(tool_input, input_schema, "")
    if not isinstance(value, dict):
        value = tool_input
    return ToolInputNormalization(
        value=value,
        corrections=tuple(corrections),
        error=error,
    )
