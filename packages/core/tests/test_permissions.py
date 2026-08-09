from __future__ import annotations

import unittest

from crabcode_core.events import CoreSession
from crabcode_core.permissions.manager import PermissionManager, PermissionMode
from crabcode_core.types.config import CrabCodeSettings, PermissionsSettings


class PermissionConfigurationTests(unittest.TestCase):
    def test_omitted_default_mode_remains_distinguishable_from_ask(self) -> None:
        settings = PermissionsSettings()

        self.assertIsNone(settings.default_mode)
        self.assertEqual(PermissionManager(settings).mode, PermissionMode.DEFAULT)

    def test_run_everything_config_selects_bypass_mode(self) -> None:
        settings = PermissionsSettings(default_mode="run_everything")

        self.assertEqual(PermissionManager(settings).mode, PermissionMode.BYPASS)

    def test_client_default_restores_loaded_permission_mode(self) -> None:
        settings = CrabCodeSettings(
            permissions=PermissionsSettings(default_mode="run_everything"),
        )
        session = CoreSession(settings=settings)
        session._permission_manager = PermissionManager(settings.permissions)

        self.assertTrue(session.set_client_permission_mode("ask"))
        self.assertEqual(session._permission_manager.mode, PermissionMode.DEFAULT)

        session.switch_mode("plan")
        self.assertTrue(session.set_client_permission_mode("default"))
        session.switch_mode("agent")
        self.assertEqual(session._permission_manager.mode, PermissionMode.BYPASS)


if __name__ == "__main__":
    unittest.main()
