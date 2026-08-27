"""ImageTool - attach a local image as an inline tool-result image."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolContext, ToolResult


class ImageTool(Tool):
    """Emit a local image as a separate ImageContent block.

    Codex tools send image bytes through ``emitImage`` instead of putting a
    filesystem path in Markdown.  This tool provides the same explicit
    boundary to the model after another tool creates an image file.
    """

    name = "Image"
    is_read_only = True
    is_concurrency_safe = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute or session-relative path to the image file.",
            },
            "mime_type": {
                "type": "string",
                "description": "Optional image MIME type override, for files without a known extension.",
            },
        },
        "required": ["path"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Attach a local image to the conversation as an inline image result. "
            "Use this after an image is generated or saved to disk. The image "
            "bytes are sent as a separate ImageContent block, so do not put a "
            "file:// or local filesystem path in Markdown. Pass the image path "
            "and, only when needed, an image MIME type override."
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionResult:
        return PermissionResult(behavior=PermissionBehavior.ALLOW)

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        raw_path = str(tool_input.get("path", "") or tool_input.get("file_path", "")).strip()
        if not raw_path:
            return ToolResult(result_for_model="Error: path is required", is_error=True)

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path(context.cwd) / path
        try:
            path = path.resolve()
        except OSError:
            # Keep the original path for a useful error if resolution fails.
            pass

        if not path.exists():
            return ToolResult(result_for_model=f"Error: image not found: {path}", is_error=True)
        if not path.is_file():
            return ToolResult(result_for_model=f"Error: not a file: {path}", is_error=True)

        mime_type = str(
            tool_input.get("mime_type", "")
            or tool_input.get("mimeType", "")
            or ""
        ).strip()
        if not mime_type:
            mime_type = mimetypes.guess_type(path.name)[0] or ""
        if not mime_type.lower().startswith("image/"):
            return ToolResult(
                result_for_model=(
                    f"Error: cannot determine an image MIME type for {path}; "
                    "pass mime_type explicitly"
                ),
                is_error=True,
            )

        try:
            data = path.read_bytes()
            context.emit_image(data, mime_type)
        except (OSError, TypeError, ValueError) as exc:
            return ToolResult(result_for_model=f"Error attaching image {path}: {exc}", is_error=True)

        message = f"Image attached: {path} ({mime_type}, {len(data)} bytes)"
        return ToolResult(
            data={"path": str(path), "media_type": mime_type, "bytes": len(data)},
            result_for_model=message,
            result_for_display=message,
            images=[context.emitted_images[-1]],
        )
