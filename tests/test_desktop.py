import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import result
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))
from agentic.desktop import AppCatalog, WindowController, Win32Windows, app_entry
from agentic.tools import build_local_registry
from agentic.device_worker import DeviceWorker
from agentic.permissions import PermissionEngine


class DesktopTests(unittest.TestCase):
    def test_catalog_matches_extended_app_name_to_start_app(self):
        catalog = AppCatalog(
            sources=(),
            start_sources=(
                lambda: [
                    ("Brave", "Brave"),
                    ("Windows Terminal", "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"),
                    ],
                ),
            )
        

        result = catalog.discover(
            {"query": "Brave Browser"}
    )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["apps"][0]["app_name"],
            "Brave",
        )
        self.assertNotIn(
            "Brave",
            result["apps"][0]["app_id"],
        )
    
    def test_catalog_never_returns_paths_and_launch_binds_id_and_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "sample.exe")
            path.write_bytes(b"synthetic test, never executed")
            catalog = AppCatalog([lambda: [("Sample", str(path)), ("Duplicate", str(path))]])
            results = catalog.discover({"query": "sample"})
            self.assertEqual(len(results["apps"]), 1)
            self.assertNotIn("path", results["apps"][0])
            with patch("os.startfile") as launch:
                self.assertTrue(catalog.launch(results["apps"][0])["ok"])
                launch.assert_called_once_with(str(path))
                self.assertFalse(catalog.launch({**results["apps"][0], "app_name": "Other"})["ok"])

    def test_changed_binary_invalidates_discovery_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "sample.exe")
            path.write_bytes(b"version1")
            catalog = AppCatalog([lambda: [("Sample", str(path))]])
            app = catalog.discover({})["apps"][0]
            path.write_bytes(b"changed version 2")
            with patch("os.startfile") as launch:
                self.assertFalse(catalog.launch(app)["ok"])
                launch.assert_not_called()

    def test_catalog_allows_argument_free_interactive_shells_but_blocks_runtimes(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe"):
                path = Path(folder, name)
                path.write_bytes(b"test")
                self.assertIsNotNone(app_entry(name, str(path)))
            for name in ("python.exe", "node.exe", "wscript.exe", "file.bat", "file.lnk"):
                path = Path(folder, name)
                path.write_bytes(b"test")
                self.assertIsNone(app_entry("bad", str(path)))
            self.assertIsNone(app_entry("bad", r"\\server\share\app.exe"))
            self.assertIsNone(app_entry("bad", "relative.exe"))
            self.assertIsNone(app_entry("bad", '"C:\\app.exe" --command test'))

    def windows(self):
        backend = Mock()
        backend.enumerate.return_value = [{"hwnd": 42, "pid": 5, "created": 123.0,
                                          "process": "sample.exe", "class": "Sample", "minimized": False}]
        backend.apply.return_value = {"ok": True}
        return WindowController(backend), backend

    def test_windows_return_no_titles_handles_or_content(self):
        control, backend = self.windows()
        window = control.list_windows({"app_name": "sample"})["windows"][0]
        self.assertEqual(set(window), {"window_id", "app_name", "minimized"})
        self.assertNotEqual(window["window_id"], "42")
        self.assertTrue(control.act(window, "minimize")["ok"])
        backend.apply.assert_called_once()

    def test_window_identity_and_expiry_fail_closed(self):
        for field, changed in (("pid", 7), ("created", 999), ("class", "Different"), ("process", "other.exe")):
            control, backend = self.windows()
            selection = control.list_windows({})["windows"][0]
            backend.enumerate.return_value = [{**backend.enumerate.return_value[0], field: changed}]
            self.assertFalse(control.act(selection, "close")["ok"])
            backend.apply.assert_not_called()
        control, backend = self.windows()
        selection = control.list_windows({})["windows"][0]
        with patch("agentic.desktop.time.monotonic", return_value=10**15):
            self.assertFalse(control.act(selection, "close")["ok"])
        self.assertFalse(control.act({**selection, "app_name": "different"}, "focus")["ok"])
        backend.apply.assert_not_called()

    def test_old_snapshot_tokens_are_not_reused(self):
        control, _ = self.windows()
        old = control.list_windows({})["windows"][0]
        control.list_windows({})
        self.assertFalse(control.act(old, "focus")["ok"])

    def test_same_app_multiple_windows_cannot_be_guessed_by_model(self):
        control, backend = self.windows()
        selection = control.list_windows({})["windows"][0]
        backend.enumerate.return_value.append({**backend.enumerate.return_value[0], "hwnd": 43})
        self.assertFalse(control.act(selection, "close")["ok"])
        backend.apply.assert_not_called()

    def test_native_restore_verifies_normal_state(self):
        with patch("win32gui.ShowWindow"), patch("win32gui.GetWindowPlacement", return_value=(0, 1, (), (), ())):
            self.assertTrue(Win32Windows().apply({"hwnd": 42}, "restore")["ok"])

    def test_native_focus_does_not_lie_when_windows_rejects_it(self):
        with patch("win32gui.IsIconic", return_value=False), \
             patch("win32gui.SetForegroundWindow", side_effect=OSError), \
             patch("win32gui.GetForegroundWindow", return_value=99):
            self.assertFalse(Win32Windows().apply({"hwnd": 42}, "focus")["ok"])

    def test_desktop_routine_discovery_launch_and_window_actions_do_not_prompt(self):
        registry = build_local_registry()
        for name in ("discover_apps", "launch_discovered_app", "list_windows", "close_window"):
            self.assertFalse(PermissionEngine().decide(registry.get(name)[0], {}).requires_confirmation)
        self.assertFalse(registry.dispatch("launch_discovered_app", {"app_id": "x", "app_name": "x", "command": "whoami"})["ok"])

    def test_core_discovery_then_launch_runs_without_approval(self):
        from agentic.core import AgentCore
        from agentic.devices import DeviceRegistry, LocalDeviceAgent
        from agentic.luna import LunaReply, ToolCall
        from test_agentic_core import FakeLuna, FakeContext, FakeMemory

        selection = {"app_id": "a" * 24, "app_name": "Synthetic app"}
        with patch.object(AppCatalog, "discover", return_value={"ok": True, "apps": [selection]}) as discover, \
             patch.object(AppCatalog, "launch", return_value={"ok": True, "message": "Launch requested"}) as launch:
            registry = build_local_registry()
            devices = DeviceRegistry()
            local = LocalDeviceAgent(devices, registry.dispatch_capability, registry.capabilities())
            luna = FakeLuna([
                LunaReply(tool_calls=[ToolCall("discover", "discover_apps", {"query": "Synthetic"})]),
                LunaReply(tool_calls=[ToolCall("launch", "launch_discovered_app", selection)]),
                LunaReply(text="Başlatma isteğini gönderdim."),
            ])
            core = AgentCore(luna, "EPIS", FakeContext(), FakeMemory(), registry, devices, local)
            try:
                final = core.handle("Synthetic app aç")
                self.assertFalse(final.confirmation_required)
                self.assertEqual(final.message, "Başlatma isteğini gönderdim.")
                discover.assert_called_once()
                launch.assert_called_once_with(selection)
            finally:
                core.close()


if __name__ == "__main__":
    unittest.main()
