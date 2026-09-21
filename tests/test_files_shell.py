import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2/src"))
from agentic.device_worker import DeviceWorker
from agentic.file_tools import FileTools, MAX_BYTES
from agentic.filesystem import FolderTools
from agentic.shell_tools import ShellTools, run_windows, set_shell_enabled, shell_enabled
from agentic.tools import build_local_registry


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = FileTools(FolderTools(lambda root: self.root))
        self.args = {"root": "workspace", "relative_path": "sample.txt"}

    def write(self, content="Merhaba Türkçe"):
        return self.files.write({**self.args, "content": content})

    def test_create_read_info_and_no_overwrite(self):
        created = self.write()
        self.assertTrue(created["ok"])
        self.assertEqual(self.files.info(self.args)["sha256"], created["sha256"])
        self.assertEqual(self.files.read(self.args)["content"], "Merhaba Türkçe")
        self.assertFalse(self.write("replacement")["ok"])
        self.assertEqual((self.root / "sample.txt").read_text(encoding="utf-8"), "Merhaba Türkçe")

    def test_copy_move_hash_and_destination_collision(self):
        digest = self.write()["sha256"]
        args = {**self.args, "expected_sha256": digest, "destination_root": "workspace", "destination_path": "copy.txt"}
        self.assertTrue(self.files.transfer(args)["ok"])
        self.assertFalse(self.files.transfer(args, move=True)["ok"])
        args["destination_path"] = "renamed.txt"
        self.assertTrue(self.files.transfer(args, move=True)["ok"])
        self.assertFalse((self.root / "sample.txt").exists())
        self.assertTrue((self.root / "renamed.txt").exists())

    def test_source_changed_during_confirmation_is_rejected(self):
        digest = self.write()["sha256"]
        (self.root / "sample.txt").write_text("changed", encoding="utf-8")
        for move in (False, True):
            self.assertFalse(self.files.transfer({**self.args, "expected_sha256": digest,
                "destination_root": "workspace", "destination_path": "never.txt"}, move=move)["ok"])
        self.assertFalse((self.root / "never.txt").exists())

    def test_paths_secrets_binary_extensions_and_links_denied(self):
        for name in ("../escape.txt", "C:/outside.txt", "x:ads.txt", ".env", "keys.env", "secrets.json", "passwords.txt", "a.exe", "a.db", "a.lnk"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.files.path("workspace", name)
        self.write()
        os.link(self.root / "sample.txt", self.root / "linked.txt")
        with self.assertRaises(ValueError):
            self.files.read(self.args)

    def test_bounds_utf8_and_pagination(self):
        (self.root / "sample.txt").write_text("x" * 6500, encoding="utf-8")
        result = self.files.read(self.args)
        self.assertEqual(len(result["content"]), 6000)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(self.files.read({**self.args, "offset": 6000})["content"]), 500)
        (self.root / "sample.txt").write_bytes(b"\xff")
        self.assertFalse(self.files.read(self.args)["ok"])
        (self.root / "sample.txt").write_bytes(b"x" * (MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            self.files.info(self.args)


class ShellTests(unittest.TestCase):
    def test_disabled_never_runs_or_resolves_path(self):
        runner, folders = Mock(), Mock()
        shell = ShellTools(folders, runner, enabled=lambda: False)
        self.assertFalse(shell.run({"command": "Write-Output test", "root": "workspace"})["ok"])
        runner.assert_not_called()
        folders.directory.assert_not_called()

    def test_output_is_separate_bounded_expiring_and_not_replayed(self):
        runner = Mock(return_value={"ok": True, "output": "PRIVATE OUTPUT", "exit_code": 0})
        shell = ShellTools(Mock(), runner, enabled=lambda: True)
        result = shell.run({"command": "Write-Output test", "root": "workspace"})
        self.assertNotIn("PRIVATE OUTPUT", json.dumps(result))
        key = result["output_id"]
        self.assertEqual(shell.read({"output_id": key})["output"], "PRIVATE OUTPUT")
        with patch("agentic.shell_tools.time.time", return_value=time.time() + 121):
            self.assertFalse(shell.read({"output_id": key})["ok"])
        self.assertEqual(runner.call_count, 1)

    def test_local_opt_in_expires_and_off_revokes(self):
        with tempfile.TemporaryDirectory() as directory, patch("agentic.shell_tools.PERMIT", Path(directory) / "permit.json"):
            self.assertFalse(shell_enabled())
            set_shell_enabled(True)
            self.assertTrue(shell_enabled())
            with patch("agentic.shell_tools.time.time", return_value=time.time() + 3601):
                self.assertFalse(shell_enabled())
            set_shell_enabled(False)
            self.assertFalse(shell_enabled())

    def test_every_new_capability_requires_real_boolean_approval(self):
        worker = DeviceWorker()
        cases = [("files.info", {"root": "workspace", "relative_path": "x.txt"}),
                 ("files.read_text", {"root": "workspace", "relative_path": "x.txt"}),
                 ("files.write_text", {"root": "workspace", "relative_path": "x.txt", "content": "x"}),
                 ("shell.powershell", {"root": "workspace", "command": "Write-Output test"}),
                 ("shell.output", {"output_id": "a" * 32})]
        for cap in ("files.copy", "files.move"):
            cases.append((cap, {"root": "workspace", "relative_path": "x.txt", "destination_root": "workspace",
                                "destination_path": "y.txt", "expected_sha256": "a" * 64}))
        for capability, args in cases:
            for approval in (False, "true", 1):
                with self.subTest(capability=capability, approval=approval), patch.object(worker.registry, "dispatch") as dispatch:
                    result = worker.handle({"version": 1, "operation": "execute", "id": "deny",
                        "device_id": worker.local.device.device_id, "capability": capability,
                        "arguments": args, "confirmed": approval, "deadline": time.time() + 10})
                    self.assertFalse(result["ok"])
                    dispatch.assert_not_called()

    def test_schema_rejects_overlong_content_command_and_offsets(self):
        registry = build_local_registry()
        for name, args in (("run_powershell", {"command": "x" * 2001, "root": "workspace"}),
                           ("write_text_file", {"root": "workspace", "relative_path": "x.txt", "content": "x" * 6001}),
                           ("read_text_file", {"root": "workspace", "relative_path": "x.txt", "offset": -1})):
            self.assertFalse(registry.dispatch(name, args)["ok"])


@unittest.skipUnless(os.getenv("EPIS_NATIVE_TESTS") == "1", "Opt-in user-session native shell tests")
class NativeShellTests(unittest.TestCase):
    def test_paired_files_shell_output_and_request_receipts(self):
        import uuid
        from agentic.devices import DeviceRegistry
        from agentic.paired_transport import PairedLocalAgent
        from agentic.pairing import bootstrap_local_pair
        from agentic.filesystem import known_root
        name = "EPIS-test-" + uuid.uuid4().hex + ".txt"
        target = known_root("workspace") / name
        try:
            with tempfile.TemporaryDirectory() as directory:
                profile = Path(directory) / "pair"
                caps = {"files.write_text", "files.read_text", "shell.powershell", "shell.output"}
                bootstrap_local_pair(profile, "file-shell-fixture", caps)
                agent = PairedLocalAgent(DeviceRegistry(), profile, heartbeat_interval=0.1)
                try:
                    args = {"root": "workspace", "relative_path": name, "content": "EPIS synthetic fixture"}
                    self.assertFalse(agent.execute("files.write_text", args)["ok"])
                    first = agent.execute("files.write_text", args, confirmed=True, request_id="fixture-write")
                    self.assertTrue(first["ok"], first)
                    self.assertEqual(agent.execute("files.write_text", args, confirmed=True, request_id="fixture-write"), first)
                    self.assertEqual(agent.execute("files.read_text", {"root": "workspace", "relative_path": name}, confirmed=True)["content"], "EPIS synthetic fixture")
                    set_shell_enabled(False)
                    shell_args = {"root": "workspace", "command": "Start-Sleep -Seconds 6; Write-Output 'EPIS paired shell fixture'"}
                    self.assertFalse(agent.execute("shell.powershell", shell_args, confirmed=True)["ok"])
                    set_shell_enabled(True)
                    result = agent.execute("shell.powershell", shell_args, confirmed=True, request_id="fixture-shell")
                    self.assertTrue(result["ok"], result)
                    self.assertNotIn("EPIS paired shell fixture", json.dumps(result))
                    self.assertEqual(agent.execute("shell.powershell", shell_args, confirmed=True, request_id="fixture-shell"), result)
                    self.assertFalse(agent.execute("shell.output", {"output_id": result["output_id"]})["ok"])
                    output = agent.execute("shell.output", {"output_id": result["output_id"]}, confirmed=True)
                    self.assertIn("EPIS paired shell fixture", output["output"])
                    self.assertTrue(agent.refresh())
                finally:
                    set_shell_enabled(False)
                    agent.close()
        finally:
            target.unlink(missing_ok=True)

    def test_real_powershell_utf8_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_windows("Write-Output 'EPIS Türkçe shell fixture'", directory)
            self.assertTrue(result["ok"], result)
            self.assertIn("Türkçe", result["output"])
            self.assertEqual(run_windows("exit 7", directory)["exit_code"], 7)

    def test_timeout_and_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_windows("Start-Sleep -Seconds 10", directory, seconds=1)
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["outcome"], "unknown")
            result = run_windows("Write-Output ('x' * 100000)", directory)
            self.assertLessEqual(len(result["output"]), 16000)
            self.assertTrue(result["truncated"])

    def test_background_child_is_terminated(self):
        import psutil
        with tempfile.TemporaryDirectory() as directory:
            result = run_windows("$s = [Diagnostics.ProcessStartInfo]::new(); $s.FileName = $PSHOME + '\\powershell.exe'; $s.Arguments = '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 60'; $s.UseShellExecute = $false; $s.CreateNoWindow = $true; $p = [Diagnostics.Process]::Start($s); Write-Output $p.Id", directory)
            self.assertTrue(result["ok"], result)
            pid = int(result["output"].strip())
            try:
                psutil.Process(pid).wait(timeout=3)
            except psutil.NoSuchProcess:
                pass


if __name__ == "__main__":
    unittest.main()
