from __future__ import annotations

import unittest

import httpx

from crabcode_core.query.loop import (
    _format_exception_message,
    _is_recoverable_api_exception,
    _trim_tool_results_to_fit,
)
from crabcode_core.types.message import (
    ToolResultBlock,
    create_tool_result_message,
    create_user_message,
)


class FormatExceptionMessageTests(unittest.TestCase):
    def test_empty_exception_message_falls_back_to_exception_type(self) -> None:
        error = httpx.ConnectError("")

        self.assertEqual(
            _format_exception_message(error),
            "ConnectError: no additional details",
        )

    def test_exception_type_is_kept_with_a_detailed_message(self) -> None:
        error = httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

        self.assertEqual(
            _format_exception_message(error),
            (
                "RemoteProtocolError: peer closed connection without sending "
                "complete message body"
            ),
        )

    def test_http_transport_errors_are_recoverable(self) -> None:
        self.assertTrue(_is_recoverable_api_exception(httpx.ConnectError("")))
        self.assertTrue(
            _is_recoverable_api_exception(httpx.RemoteProtocolError("truncated"))
        )
        self.assertFalse(_is_recoverable_api_exception(ValueError("invalid request")))


class ToolResultPreflightTests(unittest.TestCase):
    def test_oversized_tool_result_is_pruned_before_remote_compaction(self) -> None:
        messages = [
            create_user_message(content="original request"),
            create_tool_result_message(
                tool_use_id="tool-1",
                result="x" * 500_000,
            ),
            create_user_message(content="continue"),
            create_user_message(content="latest context"),
        ]

        changed = _trim_tool_results_to_fit(messages, 10_000)

        self.assertTrue(changed)
        block = messages[1].content[0]
        self.assertIsInstance(block, ToolResultBlock)
        self.assertLessEqual(len(block.content), 2_000)
        self.assertIn("old tool output pruned", block.content)


if __name__ == "__main__":
    unittest.main()
