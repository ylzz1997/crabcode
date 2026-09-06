"""Regression tests for the Windows compatibility audit, without model downloads."""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
for package in ("core", "cli", "gateway", "debugger"):
    sys.path.insert(0, str(ROOT / "packages" / package))

from crabcode_core.command_line import split_command_arguments
from crabcode_core.filesystem import replace_with_retry
from crabcode_core.hooks.manager import HookManager
from crabcode_core.lsp.client import LSPClient, _diagnostic_uri_key
from crabcode_core.snapshot.tracker import _save_file_backup, _restore_file_backups
from crabcode_core.subprocess_utils import (
    is_process_running, managed_process_command, resolve_executable_command,
    subprocess_group_options,
)
from crabcode_core.tools.file_write import FileWriteTool
from crabcode_core.tools.lint import _run_linter
from crabcode_core.types.tool import ToolContext, ToolResult
from crabcode_core.tools._input_helpers import first_non_empty_str, latest_user_text_for_agent_fallback
from crabcode_gateway.document_engine import (
    BABELDOC_VERSION, ENGINE_BUNDLE_MANIFEST_NAME, _extract_and_verify_bundle,
    _publish_engine_stage, _safe_bundle_member,
)
from crabcode_gateway.routes.document import _converter


def search_method(filename: str, name: str):
    # Search's model dependencies are optional. Execute the actual method AST,
    # not a reimplementation, so these filesystem tests need no ML downloads.
    source = ROOT / "packages/search/crabcode_search" / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    namespace = {"Path": Path, "subprocess": subprocess, "subprocess_group_options": subprocess_group_options,
                 "os": os, "ToolResult": ToolResult, "first_non_empty_str": first_non_empty_str,
                 "latest_user_text_for_agent_fallback": latest_user_text_for_agent_fallback}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


class AuditRegressionTests(unittest.TestCase):
    def test_archive_rejects_windows_and_posix_escape_paths(self):
        invalid = ["/escape", "C:escape", "C:/escape", r"\escape", r"\\server\share\escape",
                   "../escape", "a/../escape", "a//b", "a/./b", "a:b", "CON.txt", "a.", "a ", ""]
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(ValueError):
                _safe_bundle_member(name)
        self.assertEqual(_safe_bundle_member("assets/中文 file.bin").as_posix(), "assets/中文 file.bin")

    def test_archive_extracts_verified_portable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "engine.zip"
            data = b"test engine asset"
            manifest = {"schema_version": 1, "engine_version": BABELDOC_VERSION,
                        "files": [{"path": "assets/test.bin", "sha256": hashlib.sha256(data).hexdigest()}]}
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(ENGINE_BUNDLE_MANIFEST_NAME, json.dumps(manifest))
                archive.writestr("assets/test.bin", data)
            _extract_and_verify_bundle(bundle, root / "output")
            self.assertEqual((root / "output/assets/test.bin").read_bytes(), data)

    def test_snapshot_restores_exact_original_bytes(self):
        samples = [b"one\ntwo\n", b"one\r\ntwo\n", b"\xef\xbb\xbfhello\r\n", "中文".encode("gbk")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 file.txt"
            for index, raw in enumerate(samples):
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    _save_file_backup(directory, "session", str(index), path.name, "normalized text")
                    path.write_bytes(b"changed")
                    self.assertEqual(_restore_file_backups(directory, str(index)), [str(path)])
                    self.assertEqual(path.read_bytes(), raw)

    def test_write_refuses_to_destroy_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.txt"
            original = "中文".encode("gbk")
            path.write_bytes(original)
            result = asyncio.run(FileWriteTool().call(
                {"file_path": str(path), "content": "replacement"},
                ToolContext(cwd=directory, session_id="audit"),
            ))
            self.assertTrue(result.is_error)
            self.assertEqual(path.read_bytes(), original)

    def test_git_index_preserves_unicode_and_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", directory], check=True, capture_output=True)
            names = ["中文.py", " leading space.py", "a b.py"]
            for name in names:
                (root / name).write_bytes(b"pass\n")
            files = search_method("indexer.py", "_git_tracked_files")(SimpleNamespace(cwd=root))
            self.assertEqual(set(files), {root / name for name in names})

    def test_search_directory_filter_accepts_existing_windows_index_paths(self):
        def result(filename):
            return SimpleNamespace(chunk=SimpleNamespace(file_path=filename, start_line=1, end_line=1,
                                   signature="", content="pass"), score=1.0)
        rows = [result(r"src\nested\a.py"), result("src/nested/b.py"), result("src/nested-other/c.py")]
        instance = SimpleNamespace(
            _indexer=SimpleNamespace(incremental_update=AsyncMock(),
                embedder=SimpleNamespace(embed=AsyncMock(return_value=[[0]])),
                store=SimpleNamespace(search=AsyncMock(return_value=rows))),
            _background_task=None, _normalize_target_directory=lambda value, cwd: value,
        )
        output = asyncio.run(search_method("tool.py", "call")(
            instance, {"query": "test", "target_directory": "src/nested"}, ToolContext(cwd=os.getcwd())))
        self.assertIn("a.py", output.result_for_model)
        self.assertIn("b.py", output.result_for_model)
        self.assertNotIn("c.py", output.result_for_model)

    def test_engine_publish_restores_previous_install_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, stage = root / "engine", root / "stage"
            old.mkdir()
            stage.mkdir()
            (old / "old").write_bytes(b"old")
            original = os.replace
            def fail_stage(source, target):
                if source == stage:
                    raise PermissionError("engine in use")
                original(source, target)
            with patch("crabcode_gateway.document_engine.replace_with_retry", side_effect=fail_stage):
                with self.assertRaises(PermissionError):
                    _publish_engine_stage(stage, old)
            self.assertEqual((old / "old").read_bytes(), b"old")
            self.assertTrue(stage.exists())

    def test_libreoffice_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "office tool.exe"
            executable.touch()
            with patch.dict(os.environ, {"CRABCODE_LIBREOFFICE_PATH": str(executable)}):
                self.assertEqual(_converter(), str(executable.resolve()))

    def test_posix_argument_parsing_unchanged(self):
        with patch("crabcode_core.command_line.os.name", "posix"):
            self.assertEqual(split_command_arguments(r'add "a b" c\ d'), ["add", "a b", "c d"])


@unittest.skipUnless(os.name == "nt", "requires real Windows process and filesystem APIs")
class WindowsAuditRegressionTests(unittest.TestCase):
    def test_batch_metacharacters_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "tool.cmd"
            shim.write_bytes(b"@echo off\r\necho %*\r\n")
            for argument in ["a&echo injected", "%PATH%", "a|b", 'a"b', "a!b", "a^b", "a(b)", "a\nb"]:
                with self.subTest(argument=argument), self.assertRaises(ValueError):
                    resolve_executable_command([str(shim), argument])
            command = resolve_executable_command([str(shim), "space argument"])
            result = subprocess.run(command, capture_output=True, check=True)
            self.assertIn(b"space argument", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_npm_shim_passes_metacharacters_as_literal_argv(self):
        with tempfile.TemporaryDirectory(prefix="audit space ") as directory:
            root = Path(directory)
            script = root / "cli.js"
            script.write_text("process.stdout.write(JSON.stringify(process.argv.slice(2)))", encoding="utf-8")
            shim = root / "tool.cmd"
            shim.write_text(
                '@ECHO off\nGOTO start\n:find_dp0\nSET dp0=%~dp0\nEXIT /b\n:start\nSETLOCAL\nCALL :find_dp0\n\n'
                'IF EXIST "%dp0%\\node.exe" (\n  SET "_prog=%dp0%\\node.exe"\n) ELSE (\n'
                '  SET "_prog=node"\n  SET PATHEXT=%PATHEXT:;.JS;=;%\n)\n\n'
                'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\cli.js" %*\n',
                encoding="utf-8",
            )
            arguments = ["a&b", "a b", "%PATH%", 'a"b', "中文"]
            command = resolve_executable_command([str(shim), *arguments])
            self.assertEqual(Path(command[0]).name.lower(), "node.exe")
            result = subprocess.run(command, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), arguments)

    def test_executable_discovery_uses_child_path_pathext_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            shim = root / "bin/tool.cmd"
            shim.write_bytes(b"@echo ok")
            environment = {"Path": "bin", "PATHEXT": ".CMD"}
            command = resolve_executable_command(["tool", "argument"], env=environment, cwd=directory)
            self.assertEqual(Path(command[0]), shim)
            command = resolve_executable_command([r"bin\tool.cmd"], env=environment, cwd=directory)
            self.assertEqual(Path(command[0]), shim)

    def test_missing_child_executable_does_not_fall_back_to_parent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                resolve_executable_command(["python"], env={"PATH": directory, "PATHEXT": ".EXE"})

    def test_lsp_receives_diagnostics_for_equivalent_windows_uri(self):
        async def run():
            client = LSPClient([sys.executable], "file:///C:/Work")
            await client._handle_notification({"method": "textDocument/publishDiagnostics",
                "params": {"uri": "file:///c%3A/work/a.py", "diagnostics": [{"message": "test"}]}})
            diagnostics = await client.wait_for_diagnostics(r"C:\Work\A.py", timeout=0.1, debounce=0)
            self.assertEqual(diagnostics, [{"message": "test"}])
        asyncio.run(run())

    def test_parent_path_and_configured_path_have_one_windows_identity(self):
        from crabcode_debugger.dap import DAPClient
        for client, attribute in [(LSPClient(["tool"], "file:///C:/Work", env={"Path": "custom"}), "_env"),
                                  (DAPClient(["tool"], cwd=os.getcwd(), env={"Path": "custom"}), "env")]:
            environment = getattr(client, attribute)
            self.assertEqual(environment["PATH"], "custom")
            self.assertNotIn("Path", environment)

    def test_npm_cmd_runs_under_restricted_powershell_policy(self):
        powershell = shutil.which("powershell")
        if not powershell:
            self.skipTest("Windows PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "npm.ps1").write_text("Write-Output ps1", encoding="utf-8")
            (root / "npm.cmd").write_bytes(b"@echo off\r\necho cmd-ok\r\n")
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted",
                "-Command", "& npm.cmd --version"], capture_output=True,
                env={**os.environ, "PATH": directory + os.pathsep + os.environ.get("PATH", "")}, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"cmd-ok", result.stdout)

    def test_lsp_uri_keys_match_drive_case_escaping_and_unc(self):
        self.assertEqual(_diagnostic_uri_key("file:///C:/Work/A%20B.py"),
                         _diagnostic_uri_key("file:///c%3A/work/a%20b.py"))
        self.assertEqual(_diagnostic_uri_key("file://Server/Share/A.py"),
                         _diagnostic_uri_key("file://server/share/a.py"))
        self.assertNotEqual(_diagnostic_uri_key("file:///C:/a%2520b.py"),
                            _diagnostic_uri_key("file:///C:/a%20b.py"))

    def test_cli_preserves_quoted_and_unquoted_windows_paths(self):
        self.assertEqual(split_command_arguments(r'add --cwd C:\Work\project "C:\Work Dir\"'),
                         ["add", "--cwd", "C:\\Work\\project", "C:\\Work Dir\\"])
        self.assertEqual(split_command_arguments(r"add '\\server\share\folder'"),
                         ["add", r"\\server\share\folder"])

    def test_linter_decodes_legacy_windows_output(self):
        config = {"cmd": [sys.executable, "-c", "import sys;sys.stdout.buffer.write(bytes.fromhex('d6d0cec4'))"]}
        with patch("crabcode_core.subprocess_utils.locale.getpreferredencoding", return_value="cp936"):
            output, error, code = asyncio.run(_run_linter(config, [], os.getcwd()))
        self.assertEqual((output, error, code), ("中文", "", 0))

    def test_supervisor_preserves_stdio_environment_and_exit_status(self):
        code = "import os,sys;print(os.environ['AUDIT_VALUE']);print(sys.stdin.read());sys.exit(42)"
        result = subprocess.run(managed_process_command([sys.executable, "-c", code]),
                                input=b"input", capture_output=True, env={**os.environ, "AUDIT_VALUE": "value"})
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(result.stdout, b"value\r\ninput\r\n")

    def test_supervisor_reaps_descendant_after_launcher_exits(self):
        code = ("import subprocess,sys;child=subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(30)']);print(child.pid,flush=True)")
        result = subprocess.run(managed_process_command([sys.executable, "-c", code]),
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(is_process_running(int(result.stdout.strip())))

    def test_killing_supervisor_reaps_descendants_without_taskkill(self):
        code = ("import subprocess,sys,time;child=subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(30)']);print(child.pid,flush=True);time.sleep(30)")
        process = subprocess.Popen(managed_process_command([sys.executable, "-c", code]),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child_pid = int(process.stdout.readline().strip())
            self.assertTrue(is_process_running(child_pid))
            process.kill()
            process.communicate(timeout=10)
            for _ in range(40):
                if not is_process_running(child_pid):
                    break
                time.sleep(0.025)
            self.assertFalse(is_process_running(child_pid))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_hook_cancellation_cleans_up_the_started_process(self):
        async def run():
            started = asyncio.Event()
            processes = []
            spawn = asyncio.create_subprocess_exec
            async def capture(*args, **kwargs):
                process = await spawn(*args, **kwargs)
                processes.append(process)
                started.set()
                return process
            with patch("crabcode_core.hooks.manager.asyncio.create_subprocess_exec", side_effect=capture):
                task = asyncio.create_task(HookManager()._run_command(
                    "Start-Sleep -Seconds 30", payload={}, event="audit", cwd=os.getcwd(), env=dict(os.environ), timeout=60))
                await asyncio.wait_for(started.wait(), 10)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertIsNotNone(processes[0].returncode)
        asyncio.run(run())

    def test_libreoffice_standard_windows_install_without_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LibreOffice/program/soffice.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.dict(os.environ, {"CRABCODE_LIBREOFFICE_PATH": "", "ProgramFiles": directory}), \
                 patch("crabcode_gateway.routes.document.shutil.which", return_value=None):
                self.assertEqual(_converter(), str(executable))

    def test_atomic_replace_waits_for_file_handle_release(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as directory:
            target, source = Path(directory) / "target", Path(directory) / "stage"
            target.write_bytes(b"old")
            source.write_bytes(b"new")
            handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            release = threading.Timer(0.12, lambda: kernel.CloseHandle(handle))
            release.start()
            try:
                replace_with_retry(source, target)
            finally:
                release.join()
            self.assertEqual(target.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
