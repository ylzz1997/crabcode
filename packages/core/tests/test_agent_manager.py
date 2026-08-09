from __future__ import annotations

import asyncio
import unittest
from typing import Any

from crabcode_core.agent_manager import AgentManager
from crabcode_core.types.config import AgentSettings, ApiConfig, CrabCodeSettings
from crabcode_core.types.event import CoreEvent


class AgentManagerModelProfileTests(unittest.TestCase):
    def _manager(self, settings: CrabCodeSettings) -> AgentManager:
        async def event_sink(_event: CoreEvent) -> None:
            pass

        def adapter_provider(_model_profile: str | None) -> Any:
            raise AssertionError("adapter must not be created for an invalid model profile")

        return AgentManager(
            settings=settings,
            agent_settings=AgentSettings(),
            tools_provider=lambda: [],
            adapter_provider=adapter_provider,
            event_sink=event_sink,
            permission_manager=None,
            prompt_profile=None,
            cwd=".",
            env={},
            session_id="test-session",
            current_model_name="gpt",
        )

    def test_rejects_unknown_explicit_model_profile_before_queueing(self) -> None:
        settings = CrabCodeSettings(
            default_model="gpt",
            models={
                "gpt": ApiConfig(provider="codex", model="gpt-5.6-sol"),
                "gemini": ApiConfig(provider="gemini", model="gemini-2.5-pro"),
            },
        )
        manager = self._manager(settings)

        with self.assertRaisesRegex(
            ValueError,
            "Unknown model profile 'claude-sonnet-4-6'.*Available profiles: gemini, gpt",
        ):
            asyncio.run(
                manager.spawn_agent(
                    prompt="Investigate callbacks",
                    model_profile="claude-sonnet-4-6",
                )
            )

        self.assertEqual(manager.list_agents(), [])

    def test_rejects_unknown_profile_when_none_are_configured(self) -> None:
        manager = self._manager(CrabCodeSettings())

        with self.assertRaisesRegex(ValueError, "Available profiles: \\(none configured\\)"):
            asyncio.run(manager.spawn_agent(prompt="Investigate", model_profile="claude"))

        self.assertEqual(manager.list_agents(), [])


if __name__ == "__main__":
    unittest.main()
