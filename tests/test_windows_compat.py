from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "packages" / "cli"))

from crabcode_core.config.manager import ConfigManager
from crabcode_core.file_lock import file_lock
from crabcode_core.lsp.client import _uri_to_path
from crabcode_core.path_validation import validate_path_component
from crabcode_core.peer.runtime import PeerMessage, PeerRuntime
from crabcode_core.prompts.system import get_system_prompt
from crabcode_core.session import storage
from crabcode_core.subprocess_utils import (
    decode_subprocess_output,
    is_process_running,
    resolve_executable_command,
    shell_command,
    subprocess_group_options,
    terminate_process_tree,
)
from crabcode_core.text_io import read_utf8_text
from crabcode_core.tools.file_edit import FileEditTool
from crabcode_core.tools.file_write import FileWriteTool
from crabcode_core.tools.grep import GrepTool
from crabcode_core.team.inbox import InboxStorage
from crabcode_core.types.tool import ToolContext


class WindowsCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_lsp_file_uri_round_trips_drive_and_unc_paths(self) -> None:
        self.assertEqual(_uri_to_path("file:///C:/Users/demo/a.py"), r"C:\Users\demo\a.py")
        self.assertEqual(_uri_to_path("file://server/share/a.py"), r"\\server\share\a.py")
        self.assertEqual(
            _uri_to_path("file:///C:/Users/demo/a%2520b.py"),
            r"C:\Users\demo\a%20b.py",
        )

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_shell_is_explicit_and_output_decoder_handles_active_code_page(self) -> None:
        command = shell_command("echo ok")
        self.assertIn(
            Path(command[0]).name.lower(),
            {"pwsh.exe", "powershell.exe", "cmd.exe"},
        )
        completed = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(decode_subprocess_output(completed.stdout).strip(), "ok")
        with patch(
            "crabcode_core.subprocess_utils.locale.getpreferredencoding",
            return_value="cp936",
        ):
            self.assertEqual(decode_subprocess_output("中文".encode("cp936")), "中文")

    def test_unix_shell_is_non_login(self) -> None:
        with (
            patch("crabcode_core.subprocess_utils.os.name", "posix"),
            patch("crabcode_core.subprocess_utils.shutil.which", return_value="/bin/bash"),
        ):
            self.assertEqual(shell_command("echo ok"), ["/bin/bash", "-c", "echo ok"])

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_windows_command_shim_is_resolved_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "crabcode-audit-tool.cmd"
            shim.write_text("@echo off\r\necho shim-ok\r\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"PATH": f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"},
            ):
                command = resolve_executable_command(
                    ["crabcode-audit-tool", "ignored"]
                )
                self.assertEqual(Path(command[0]), shim)
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                decode_subprocess_output(completed.stdout).strip(),
                "shim-ok",
            )

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_background_subprocesses_do_not_open_console_windows(self) -> None:
        options = subprocess_group_options()
        self.assertIn("creationflags", options)
        self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_system_prompt_reports_the_actual_windows_shell(self) -> None:
        prompt = "\n".join(get_system_prompt([], "test-model"))
        self.assertNotIn("Shell: unknown", prompt)
        self.assertRegex(prompt.lower(), r"shell: (?:pwsh|powershell|cmd)(?:\.exe)?")

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_session_project_key_is_case_insensitive_on_windows(self) -> None:
        self.assertEqual(
            storage.get_project_dir(r"C:\Work\CrabCode"),
            storage.get_project_dir(r"c:\work\crabcode"),
        )

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_cli_stdio_is_utf8_even_when_windows_defaults_are_not(self) -> None:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "0"
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(ROOT / "packages" / "cli"),
                str(ROOT / "packages" / "core"),
                env.get("PYTHONPATH", ""),
            ]
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from crabcode_cli.app import _configure_utf8_stdio; "
                "_configure_utf8_stdio(); print(chr(0x1f980))",
            ],
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "🦀\r\n".encode("utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_pid_probe_does_not_signal_the_process(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            self.assertTrue(is_process_running(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.assertFalse(is_process_running(process.pid))

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_peer_runtime_uses_loopback_tcp_on_windows(self) -> None:
        async def run(registry_root: Path) -> None:
            received: list[str] = []

            async def receive(message: PeerMessage) -> bool:
                received.append(message.text)
                return True

            def make_runtime() -> PeerRuntime:
                return PeerRuntime(
                    session_id=str(uuid.uuid4()),
                    cwd=str(registry_root),
                    on_message=receive,
                    on_hold=None,
                    permission_class_provider=lambda: "prompting",
                    registry_root=registry_root,
                )

            first = make_runtime()
            second = make_runtime()
            try:
                await first.start()
                await second.start()
                assert second.record is not None
                self.assertTrue(second.record.socket_path.startswith("tcp://127.0.0.1:"))
                delivery = await first.send(second.session_id, "hello")
                self.assertEqual(delivery.status, "delivered")
                self.assertEqual(received, ["hello"])
            finally:
                await first.close()
                await second.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(Path(directory)))

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_transcript_lock_coordinates_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            child_code = (
                "import sys,time;from pathlib import Path;"
                "from crabcode_core.session.storage import _transcript_file_lock;"
                "lock=_transcript_file_lock(Path(sys.argv[1]),exclusive=True);"
                "lock.__enter__();print('locked',flush=True);time.sleep(.5);lock.__exit__(None,None,None)"
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [str(ROOT / "packages" / "core"), env.get("PYTHONPATH")])
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child_code, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "locked")
                started = time.monotonic()
                with storage._transcript_file_lock(path, exclusive=True):
                    pass
                self.assertGreaterEqual(time.monotonic() - started, 0.3)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    @unittest.skipUnless(os.name == "nt", "Windows compatibility test")
    def test_process_tree_termination_reaps_descendant(self) -> None:
        if not shutil.which("taskkill"):
            self.skipTest("taskkill is unavailable")

        async def run() -> None:
            parent_code = (
                "import subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(30)']);"
                "print(child.pid,flush=True);time.sleep(30)"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                parent_code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_group_options(),
            )
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            child_pid = int((await asyncio.wait_for(process.stdout.readline(), timeout=5)).strip())
            try:
                self.assertTrue(is_process_running(child_pid))
                await terminate_process_tree(process)
                for _ in range(20):
                    if not is_process_running(child_pid):
                        break
                    await asyncio.sleep(0.05)
                self.assertFalse(is_process_running(child_pid))
            finally:
                if is_process_running(child_pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )

        asyncio.run(run())

    def test_file_edit_preserves_utf8_bom_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "example.txt"
            path.write_bytes(b"\xef\xbb\xbf" + "甲\r\n乙\r\n".encode("utf-8"))

            result = asyncio.run(
                FileEditTool().call(
                    {
                        "file_path": str(path),
                        "old_string": "甲\n乙",
                        "new_string": "甲\n丙",
                    },
                    ToolContext(cwd=str(root)),
                )
            )

            self.assertFalse(result.is_error)
            self.assertEqual(
                path.read_bytes(),
                b"\xef\xbb\xbf" + "甲\r\n丙\r\n".encode("utf-8"),
            )

    def test_file_edit_can_target_lf_region_in_mixed_newline_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "mixed.txt"
            path.write_bytes(b"first\r\ntarget\nend\r\n")

            result = asyncio.run(
                FileEditTool().call(
                    {
                        "file_path": str(path),
                        "old_string": "target\nend",
                        "new_string": "changed\nend",
                    },
                    ToolContext(cwd=str(root)),
                )
            )

            self.assertFalse(result.is_error)
            self.assertEqual(path.read_bytes(), b"first\r\nchanged\nend\r\n")

    def test_file_edit_counts_mixed_newline_matches_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "mixed.txt"
            path.write_bytes(b"same\r\nline\r\nsame\nline\n")
            context = ToolContext(cwd=str(root))

            ambiguous = asyncio.run(
                FileEditTool().call(
                    {
                        "file_path": str(path),
                        "old_string": "same\nline",
                        "new_string": "changed\nline",
                    },
                    context,
                )
            )
            self.assertTrue(ambiguous.is_error)

            replaced = asyncio.run(
                FileEditTool().call(
                    {
                        "file_path": str(path),
                        "old_string": "same\nline",
                        "new_string": "changed\nline",
                        "replace_all": True,
                    },
                    context,
                )
            )
            self.assertFalse(replaced.is_error)
            self.assertEqual(path.read_bytes(), b"changed\r\nline\r\nchanged\nline\n")

    def test_file_write_preserves_existing_encoding_and_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "example.txt"
            path.write_bytes(b"\xef\xbb\xbf" + "旧\r\n内容\r\n".encode("utf-8"))

            result = asyncio.run(
                FileWriteTool().call(
                    {"file_path": str(path), "content": "新\n内容\n"},
                    ToolContext(cwd=str(root)),
                )
            )

            self.assertFalse(result.is_error)
            self.assertEqual(
                path.read_bytes(),
                b"\xef\xbb\xbf" + "新\r\n内容\r\n".encode("utf-8"),
            )

    def test_config_update_preserves_utf8_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".crabcode" / "settings.json"
            path.parent.mkdir()
            path.write_bytes('{\r\n  "description": "中文"\r\n}\r\n'.encode("utf-8"))

            ConfigManager(cwd=str(root)).update_settings(
                "projectSettings",
                {"model": "example"},
            )

            raw = path.read_bytes()
            self.assertIn("中文", raw.decode("utf-8"))
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_short_session_path_keeps_previous_hashed_directory_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = "C:\\" + "deep-folder\\" * 30
            session_id = str(uuid.uuid4())

            with patch.object(storage, "get_projects_dir", return_value=root):
                self.assertLessEqual(len(storage.get_project_dir(cwd).name), 86)
                previous = storage._previous_hashed_project_dir(cwd)
                previous.mkdir()
                transcript = previous / f"{session_id}.jsonl"
                transcript.write_text(
                    '{"type":"session_meta","cwd":"C:\\\\legacy"}\n',
                    encoding="utf-8",
                )

                try:
                    self.assertEqual(storage.get_transcript_path(cwd, session_id), transcript)
                finally:
                    # ``TemporaryDirectory`` cannot traverse this deliberately
                    # over-MAX_PATH directory without the extended-path prefix.
                    for child in previous.iterdir():
                        child.unlink()
                    previous.rmdir()

    def test_filesystem_components_reject_windows_special_names(self) -> None:
        invalid = [
            "bad:name",
            "bad*name",
            "trailing.",
            "trailing ",
            "CON",
            "con.jsonl",
            "LPT9.txt",
            "control\x7f",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_path_component(value, "test component")
                with self.assertRaises(ValueError):
                    InboxStorage._validate_component(value, "test component")

    def test_grep_has_a_python_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "example.py"
            source.write_text("# 中文 marker\n", encoding="utf-8")
            with patch("crabcode_core.tools.grep.shutil.which", return_value=None):
                result = asyncio.run(
                    GrepTool().call(
                        {"pattern": "中文", "glob": "*.py"},
                        ToolContext(cwd=str(root)),
                    )
                )
            self.assertFalse(result.is_error)
            self.assertIn("example.py:1:# 中文 marker", result.result_for_model)

    def test_shared_file_lock_coordinates_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "shared.lock"
            child_code = (
                "import pathlib,sys,time;"
                "from crabcode_core.file_lock import file_lock;"
                "cm=file_lock(pathlib.Path(sys.argv[1]));"
                "cm.__enter__();print('locked',flush=True);time.sleep(0.75);"
                "cm.__exit__(None,None,None)"
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [str(ROOT / "packages" / "core"), env.get("PYTHONPATH", "")]
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(lock_path)],
                env=env,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                started = time.perf_counter()
                with file_lock(lock_path):
                    elapsed = time.perf_counter() - started
                self.assertGreaterEqual(elapsed, 0.4)
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)

    def test_utf8_reader_rejects_invalid_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.txt"
            path.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                read_utf8_text(path)


if __name__ == "__main__":
    unittest.main()
