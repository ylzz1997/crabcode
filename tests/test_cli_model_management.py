"""CLI model/group add-delete menus and persistent catalog mutations."""

from __future__ import annotations

import asyncio
import codecs
import json
from collections import deque
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from crabcode_cli import repl
from crabcode_cli.model_manager import (
    MenuOption,
    SearchableMenu,
    manage_model_catalog,
)
from crabcode_core.api.model_discovery import ModelDiscoveryError
from crabcode_core.config.manager import ConfigManager
from crabcode_core.config.model_catalog import (
    CatalogImportResult,
    CatalogMutationResult,
    ModelCatalogError,
    add_catalog_entry,
    delete_catalog_entry,
    import_group_models,
)
from crabcode_core.events import CoreSession
from crabcode_core.types.config import CrabCodeSettings


class TerminalOutput(DummyOutput):
    def get_size(self):
        return Size(rows=20, columns=100)


class ScriptedPrompter:
    def __init__(self, *, selects=(), texts=(), confirms=(), multiselects=()):
        self.selects = deque(selects)
        self.texts = deque(texts)
        self.confirms = deque(confirms)
        self.multiselects = deque(multiselects)
        self.multiselect_calls = []
        self.select_calls = []
        self.notes = []
        self.errors = []

    async def select(self, message, options, **kwargs):
        self.select_calls.append((message, list(options), kwargs))
        return self.selects.popleft()

    async def text(self, message, *, initial=""):
        return self.texts.popleft()

    async def multiselect(self, message, options):
        self.multiselect_calls.append((message, list(options)))
        return self.multiselects.popleft()

    async def confirm(self, message, *, default=False):
        return self.confirms.popleft()

    def note(self, title, body):
        self.notes.append((title, body))

    def error(self, message):
        self.errors.append(message)


class SearchableMenuTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 3)

    @asynccontextmanager
    async def menu(self, *, searchable=True, multiple=False, options=None):
        options = options or (
            MenuOption("model", "Model", "named profile"),
            MenuOption("group", "Group", "shared provider settings"),
        )
        with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=TerminalOutput()
        ):
            menu = SearchableMenu(
                "Manage",
                "Choose an entry",
                options,
                searchable=searchable,
                initial="model",
                multiple=multiple,
            )
            task = asyncio.create_task(menu.run())
            try:
                await self.wait_for(lambda: menu.app.is_running)
                yield menu, pipe, task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_searches_labels_and_hints_then_selects(self):
        async with self.menu() as (menu, pipe, task):
            pipe.send_text("provider")
            await self.wait_for(lambda: len(menu.matches) == 1)
            self.assertEqual(menu.matches[0].value, "group")
            pipe.send_text("\r")
            self.assertEqual(await task, "group")

    async def test_non_search_menu_uses_arrow_navigation(self):
        async with self.menu(searchable=False) as (_menu, pipe, task):
            pipe.send_text("\x1b[B\r")
            self.assertEqual(await task, "group")

    async def test_multiselect_keeps_choices_across_search_and_skips_existing(self):
        options = (
            MenuOption("old", "Old", disabled=True),
            MenuOption("first", "First", "context_window: 64000"),
            MenuOption("second", "Second"),
        )
        async with self.menu(multiple=True, options=options) as (menu, pipe, task):
            pipe.send_text(" \r")
            await self.wait_for(lambda: menu.app.is_running)
            self.assertFalse(task.done())
            self.assertFalse(menu.checked)
            pipe.send_text("64000\t")
            await self.wait_for(lambda: menu.checked == {"first"})
            pipe.send_text("\x15second\x01")
            await self.wait_for(lambda: menu.checked == {"first", "second"})
            pipe.send_text("\x01")
            await self.wait_for(lambda: menu.checked == {"first"})
            pipe.send_text("\x15\x01")
            await self.wait_for(lambda: menu.checked == {"first", "second"})
            pipe.send_text("\r")
            self.assertEqual(await task, ["first", "second"])

    async def test_multiselect_limit_and_cancel_do_not_return_partial_choices(self):
        options = [MenuOption(str(i), f"model-{i:04}") for i in range(1001)]
        async with self.menu(multiple=True, options=options) as (menu, pipe, task):
            pipe.send_text("\x01")
            await self.wait_for(lambda: bool(menu.selection_error))
            self.assertFalse(menu.checked)
            pipe.send_text("model-0000 ")
            await self.wait_for(lambda: menu.checked == {"0"})
            pipe.send_text("\x15\x01")
            await self.wait_for(lambda: bool(menu.selection_error) and len(menu.matches) == 1001)
            self.assertEqual(menu.checked, {"0"})
            pipe.send_text("\x1b")
            self.assertIsNone(await task)


class ModelCatalogMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.cwd = self.root / "project"
        self.home.mkdir()
        self.cwd.mkdir()
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def write_settings(self, source, value):
        path = Path(ConfigManager(cwd=str(self.cwd)).settings_file_paths[source])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def test_add_preserves_unrelated_settings_and_rejects_cross_layer_duplicate(self):
        project_path = self.write_settings(
            "projectSettings",
            {"language": "zh-CN", "models": {"existing": {"model": "old"}}},
        )
        self.write_settings("userSettings", {"groups": {"shared": {"provider": "openai"}}})

        result = add_catalog_entry(
            str(self.cwd),
            "projectSettings",
            "model",
            "new-model",
            {"group": "shared", "model": "gpt-new"},
        )

        self.assertEqual(result.path, project_path)
        saved = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["language"], "zh-CN")
        self.assertEqual(saved["models"]["existing"]["model"], "old")
        self.assertEqual(
            saved["models"]["new-model"],
            {"group": "shared", "model": "gpt-new"},
        )
        with self.assertRaisesRegex(ModelCatalogError, "already exists"):
            add_catalog_entry(
                str(self.cwd), "localSettings", "group", "shared", {"provider": "router"}
            )

    def test_delete_is_layer_exact_and_clears_same_layer_default(self):
        self.write_settings("userSettings", {"models": {"same": {"model": "user"}}})
        project_path = self.write_settings(
            "projectSettings",
            {
                "default_model": "same",
                "models": {"same": {"model": "project"}},
                "unrelated": {"keep": True},
            },
        )

        result = delete_catalog_entry(
            str(self.cwd), "projectSettings", "model", "same"
        )

        self.assertTrue(result.cleared_default)
        saved = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertNotIn("models", saved)
        self.assertIsNone(saved["default_model"])
        self.assertEqual(saved["unrelated"], {"keep": True})
        self.assertEqual(ConfigManager(cwd=str(self.cwd)).load().models["same"].model, "user")

    def test_atomic_write_preserves_bom_crlf_and_file_mode(self):
        path = Path(ConfigManager(cwd=str(self.cwd)).settings_file_paths["projectSettings"])
        path.parent.mkdir(parents=True)
        path.write_bytes(codecs.BOM_UTF8 + b'{\r\n  "language": "zh-CN"\r\n}\r\n')
        path.chmod(0o640)

        add_catalog_entry(
            str(self.cwd), "projectSettings", "group", "router", {"provider": "router"}
        )

        raw = path.read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8))
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_batch_import_preserves_layers_resolves_collisions_and_is_idempotent(self):
        user_path = self.write_settings("userSettings", {
            "groups": {"shared": {"provider": "openai", "base_url": "https://example.test/v1", "max_tokens": 4096}},
            "models": {"existing": {"group": "shared", "model": "old", "temperature": 0.25},
                       "org-new": {"model": "other"}, "shared - org-new": {"model": "another"}},
            "default_model": "existing",
        })
        project_path = self.write_settings("projectSettings", {"unrelated": {"keep": True}})
        user_before = user_path.read_bytes()
        metadata = {"org/new": {"context_window": 128000, "max_tokens": 8192, "thinking_enabled": False}}
        result = import_group_models(str(self.cwd), "projectSettings", "shared", ["old", "org/new", "org/new", "fresh"], metadata)
        self.assertEqual(result.names, ("shared - org-new (2)", "fresh"))
        saved = json.loads(project_path.read_text())
        self.assertEqual(saved, {"unrelated": {"keep": True}, "models": {
            "shared - org-new (2)": {"group": "shared", "model": "org/new", "context_window": 128000, "max_tokens": 4096, "thinking_enabled": False},
            "fresh": {"group": "shared", "model": "fresh"},
        }})
        self.assertEqual(user_path.read_bytes(), user_before)
        settings = ConfigManager(cwd=str(self.cwd)).load()
        self.assertEqual(settings.default_model, "existing")
        self.assertEqual(settings.models["fresh"].base_url, "https://example.test/v1")
        with patch("crabcode_core.config.model_catalog._atomic_write_settings") as write:
            result = import_group_models(str(self.cwd), "projectSettings", "shared", ["org/new", "fresh"], metadata)
        self.assertEqual(result.names, ())
        write.assert_not_called()

    def test_batch_import_validates_before_writing_and_failed_write_is_atomic(self):
        path = self.write_settings("projectSettings", {"groups": {"shared": {"provider": "openai"}}})
        original = path.read_bytes()
        for group, ids in (("missing", ["valid"]), ("shared", ["valid", "\n"]), ("shared", []), ("shared", ["valid"] * 1001)):
            with self.subTest(group=group, count=len(ids)), self.assertRaises(ModelCatalogError):
                import_group_models(str(self.cwd), "projectSettings", group, ids, {})
            self.assertEqual(path.read_bytes(), original)
        with patch("crabcode_core.config.model_catalog.replace_with_retry", side_effect=PermissionError("test")):
            with self.assertRaisesRegex(ModelCatalogError, "not writable"):
                import_group_models(str(self.cwd), "projectSettings", "shared", ["one", "two"], {})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])


class ModelManagementWizardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.cwd = self.root / "project"
        self.home.mkdir()
        self.cwd.mkdir()
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def write_project(self, value):
        path = self.cwd / ".crabcode" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def make_session(self):
        settings = ConfigManager(cwd=str(self.cwd)).load()
        settings._crabcode_explicit_settings = CrabCodeSettings()
        return CoreSession(cwd=str(self.cwd), settings=settings)

    async def test_no_arg_add_chooses_group_and_builds_router_config(self):
        session = self.make_session()
        prompter = ScriptedPrompter(
            selects=("group", "projectSettings", "router", "openai"),
            texts=("my-router", "https://router.example/v1", "ROUTER_KEY", ""),
            confirms=(True,),
        )

        result = await manage_model_catalog(session, "add", prompter=prompter)

        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "group")
        saved = json.loads((self.cwd / ".crabcode" / "settings.json").read_text())
        self.assertEqual(
            saved["groups"]["my-router"],
            {
                "provider": "router",
                "base_url": "https://router.example/v1",
                "api_key_env": "ROUTER_KEY",
                "format": "openai",
            },
        )
        self.assertIn("my-router", session.settings.groups)

    async def test_add_model_inherits_group_and_accepts_advanced_json(self):
        self.write_project({"groups": {"shared": {"provider": "openai"}}})
        session = self.make_session()
        prompter = ScriptedPrompter(
            selects=("projectSettings", "shared", "__inherit__"),
            texts=(
                "fast",
                "gpt-fast",
                '{"reasoning_effort":"low","http_headers":{"Authorization":"Bearer test"}}',
            ),
            confirms=(True,),
        )

        result = await manage_model_catalog(
            session, "add", "model", prompter=prompter
        )

        self.assertEqual(result.name, "fast")
        raw = ConfigManager(cwd=str(self.cwd)).get_settings_for_source("projectSettings")
        self.assertEqual(
            raw["models"]["fast"],
            {
                "model": "gpt-fast",
                "reasoning_effort": "low",
                "http_headers": {"Authorization": "Bearer test"},
                "group": "shared",
            },
        )
        self.assertIn("[redacted]", prompter.notes[-1][1])
        self.assertNotIn("Bearer test", prompter.notes[-1][1])
        self.assertEqual(session.settings.get_api_config("fast").provider, "openai")
        self.assertIn("fast", session.list_models())

    async def test_discover_from_add_menu_imports_metadata_and_refreshes_catalog(self):
        self.write_project({
            "groups": {"shared": {"provider": "router", "format": "openai", "base_url": "https://example.test/v1", "api_key_env": "TEST_KEY", "network_mode": "direct"}},
            "env": {"TEST_KEY": "test-secret"},
            "models": {"active": {"group": "shared", "model": "old"}}, "default_model": "active",
        })
        session = self.make_session()
        session._current_model_name = "active"
        metadata = {"context_window": 128000, "max_tokens": 8192, "thinking_enabled": False}
        discover = AsyncMock(return_value=[{"id": "old"}, {"id": "new", "name": "New model", "config": metadata}])
        prompter = ScriptedPrompter(selects=("discover", "shared", "localSettings"), multiselects=(["new"],), confirms=(True,))
        with patch("crabcode_cli.model_manager.discover_models", discover):
            result = await manage_model_catalog(session, "add", prompter=prompter)
        self.assertEqual(result.names, ("new",))
        config, env = discover.await_args.args
        self.assertEqual(config.base_url, "https://example.test/v1")
        self.assertEqual(config.network_mode, "direct")
        self.assertEqual(env, {"TEST_KEY": "test-secret"})
        options = prompter.multiselect_calls[0][1]
        self.assertTrue(options[0].disabled)
        self.assertFalse(options[1].disabled)
        self.assertIn("128000", options[1].hint)
        self.assertEqual(json.loads(result.path.read_text())["models"], {"new": {"group": "shared", "model": "new", **metadata}})
        self.assertIn("new", session.list_models())
        self.assertEqual(session._current_model_name, "active")
        self.assertEqual(session.settings.default_model, "active")
        self.assertNotIn("test-secret", str(prompter.notes))

    async def test_cancel_discovery_at_each_step_never_writes_or_reloads(self):
        path = self.write_project({"groups": {"shared": {"provider": "openai"}}})
        original = path.read_bytes()
        for prompter in (
            ScriptedPrompter(selects=(None,)),
            ScriptedPrompter(selects=("shared",), multiselects=(None,)),
            ScriptedPrompter(selects=("shared", None), multiselects=(["new"],)),
            ScriptedPrompter(selects=("shared", "projectSettings"), multiselects=(["new"],), confirms=(False,)),
        ):
            session = self.make_session()
            with patch("crabcode_cli.model_manager.discover_models", AsyncMock(return_value=[{"id": "new"}])), patch.object(session, "reload_model_catalog") as reload:
                self.assertIsNone(await manage_model_catalog(session, "add", "discover", prompter=prompter))
                reload.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    async def test_empty_already_added_and_failed_discovery_do_not_write(self):
        path = self.write_project({"groups": {"shared": {"provider": "openai"}}, "models": {"old": {"group": "shared", "model": "old"}}})
        original = path.read_bytes()
        for value in ([], [{"id": "old"}], ModelDiscoveryError("HTTP 401: authentication failed")):
            prompter = ScriptedPrompter(selects=("shared",))
            discover = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
            with patch("crabcode_cli.model_manager.discover_models", discover):
                if isinstance(value, Exception):
                    with self.assertRaisesRegex(ModelCatalogError, "HTTP 401"):
                        await manage_model_catalog(self.make_session(), "add", "discover", prompter=prompter)
                else:
                    self.assertIsNone(await manage_model_catalog(self.make_session(), "add", "discover", prompter=prompter))
            self.assertFalse(prompter.multiselect_calls)
            self.assertEqual(path.read_bytes(), original)

    async def test_discovery_without_groups_explains_how_to_add_one(self):
        with self.assertRaisesRegex(ModelCatalogError, "/add group"):
            await manage_model_catalog(self.make_session(), "add", "discover", prompter=ScriptedPrompter())

    async def test_delete_group_warns_about_references_and_removes_exact_definition(self):
        self.write_project(
            {
                "groups": {"shared": {"provider": "openai"}},
                "models": {"fast": {"group": "shared", "model": "gpt-fast"}},
            }
        )
        session = self.make_session()
        prompter = ScriptedPrompter(selects=("0",), confirms=(True,))

        result = await manage_model_catalog(
            session, "delete", "group", prompter=prompter
        )

        self.assertEqual(result.name, "shared")
        self.assertIn("fast", prompter.notes[-1][1])
        raw = ConfigManager(cwd=str(self.cwd)).get_settings_for_source("projectSettings")
        self.assertNotIn("groups", raw)
        self.assertEqual(raw["models"]["fast"]["group"], "shared")

    async def test_active_model_and_its_group_are_protected(self):
        self.write_project(
            {
                "groups": {"shared": {"provider": "openai"}},
                "models": {"fast": {"group": "shared", "model": "gpt-fast"}},
            }
        )
        session = self.make_session()
        session._current_model_name = "fast"

        with self.assertRaisesRegex(ModelCatalogError, "active model"):
            await manage_model_catalog(
                session,
                "delete",
                "model",
                prompter=ScriptedPrompter(selects=("0",)),
            )
        with self.assertRaisesRegex(ModelCatalogError, "while active model"):
            await manage_model_catalog(
                session,
                "delete",
                "group",
                prompter=ScriptedPrompter(selects=("0",)),
            )


class ModelManagementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_dispatch_and_summary(self):
        output = StringIO()
        result = CatalogImportResult("group [/literal]", ("one", "two"), "projectSettings", Path("/tmp/settings.json"))
        manager = AsyncMock(return_value=result)
        with patch("crabcode_cli.model_manager.manage_model_catalog", manager), patch.object(repl, "console", Console(file=output, width=160)):
            self.assertTrue(await repl._handle_command("/add discover", SimpleNamespace(skills=[]), None, []))
            self.assertTrue(await repl._handle_command("/del discover", SimpleNamespace(skills=[]), None, []))
        self.assertEqual(manager.await_count, 1)
        self.assertEqual(manager.await_args.args[1:], ("add", "discover"))
        self.assertIn("Imported 2 models", output.getvalue())
        self.assertIn("group [/literal]", output.getvalue())
        self.assertIn("/model", output.getvalue())
        self.assertIn("discover", repl._SLASH_COMMANDS["/add"])

    async def test_add_and_del_dispatch_kind_to_guided_manager(self):
        output = StringIO()
        session = SimpleNamespace(skills=[])
        added = CatalogMutationResult(
            "add",
            "model",
            "fast [/literal]",
            "projectSettings",
            Path("/tmp/settings.json"),
        )
        deleted = CatalogMutationResult(
            "delete",
            "group",
            "shared",
            "localSettings",
            Path("/tmp/settings.local.json"),
        )
        manager = AsyncMock(side_effect=(added, deleted))
        with (
            patch("crabcode_cli.model_manager.manage_model_catalog", manager),
            patch.object(repl, "console", Console(file=output, width=160)),
        ):
            self.assertTrue(await repl._handle_command("/add model", session, None, []))
            self.assertTrue(await repl._handle_command("/del group", session, None, []))

        self.assertEqual(manager.await_args_list[0].args[1:], ("add", "model"))
        self.assertEqual(manager.await_args_list[1].args[1:], ("delete", "group"))
        self.assertIn("fast [/literal]", output.getvalue())
        self.assertIn("shared", output.getvalue())
        self.assertIn("/add", repl._SLASH_COMMANDS)
        self.assertEqual(repl._SLASH_COMMANDS["/del"], ["model", "group"])

    async def test_invalid_subcommand_does_not_open_manager(self):
        manager = AsyncMock()
        with patch("crabcode_cli.model_manager.manage_model_catalog", manager):
            self.assertTrue(
                await repl._handle_command(
                    "/add model extra", SimpleNamespace(skills=[]), None, []
                )
            )
        manager.assert_not_awaited()
