import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))
from agentic.filesystem import FolderTools, parts
from agentic.media import MediaController, spotify_search
from agentic.system_tools import audio_status, mute, open_settings, brightness, SETTINGS
from agentic.tools import build_local_registry
from agentic.permissions import PermissionEngine
from agentic.device_worker import DeviceWorker


class FakeMedia:
    def __init__(self):
        self.items = [SimpleNamespace(source_app_user_model_id="SyntheticPlayer.exe", status="playing")]
        self.calls = []
        self.accept = True
        self.change = True

    async def sessions(self):
        return self.items

    def state(self, session):
        return session.status

    async def command(self, session, action):
        self.calls.append(action)
        if self.accept and self.change:
            session.status = {"play": "playing", "pause": "paused"}.get(action, session.status)
        return self.accept


class MediaTests(unittest.TestCase):
    def setup_media(self):
        backend = FakeMedia()
        control = MediaController(backend)
        selection = control.run("list", {})["sessions"][0]
        arguments = {k: selection[k] for k in ("session_id", "app_id")}
        return backend, control, arguments

    def test_targeted_pause_is_not_toggle_and_already_paused_is_noop(self):
        backend, control, args = self.setup_media()
        result = control.run("control", {**args, "action": "pause"})
        self.assertTrue(result["state_verified"])
        self.assertEqual(backend.calls, ["pause"])
        self.assertTrue(control.run("control", {**args, "action": "pause"})["ok"])
        self.assertEqual(backend.calls, ["pause"])

    def test_list_has_no_track_data(self):
        control = MediaController(FakeMedia())
        self.assertEqual(set(control.run("list", {})["sessions"][0]), {"session_id", "app_id", "playback_state"})
        self.assertEqual(control.run("list", {"app_name": "missing"})["sessions"], [])

    def test_unknown_expired_or_changed_media_tokens_fail(self):
        backend, control, args = self.setup_media()
        self.assertFalse(control.run("control", {**args, "app_id": "other", "action": "pause"})["ok"])
        with patch("agentic.media.time.monotonic", return_value=10**15):
            self.assertFalse(control.run("control", {**args, "action": "pause"})["ok"])
        control.run("list", {})
        self.assertFalse(control.run("control", {**args, "action": "pause"})["ok"])
        self.assertEqual(backend.calls, [])

    def test_disappeared_or_ambiguous_session_not_controlled(self):
        for duplicate in (False, True):
            backend, control, args = self.setup_media()
            backend.items = backend.items * 2 if duplicate else []
            self.assertFalse(control.run("control", {**args, "action": "pause"})["ok"])
            self.assertEqual(backend.calls, [])

    def test_rejected_media_command_has_no_fallback(self):
        backend, control, args = self.setup_media()
        backend.accept = False
        self.assertFalse(control.run("control", {**args, "action": "pause"})["ok"])
        self.assertEqual(backend.calls, ["pause"])

    def test_accepted_does_not_mean_verified_state_or_track(self):
        backend, control, args = self.setup_media()
        backend.change = False
        result = control.run("control", {**args, "action": "pause"})
        self.assertTrue(result["accepted"])
        self.assertFalse(result["state_verified"])
        self.assertFalse(control.run("control", {**args, "action": "next"})["state_verified"])

    def test_timeout_blocks_replay(self):
        backend, control, args = self.setup_media()
        async def timeout(*args):
            raise TimeoutError
        backend.command = timeout
        result = control.run("control", {**args, "action": "pause"})
        self.assertEqual(result["outcome"], "unknown")

    def test_spotify_search_escapes_query_without_playback(self):
        with patch("os.startfile") as start:
            result = spotify_search({"query": "Manifest / Snap?x=#"})
            self.assertTrue(result["ok"])
            self.assertEqual(start.call_args.args[0], "https://open.spotify.com/search/Manifest%20%2F%20Snap%3Fx%3D%23")
            self.assertIn("no track selected", result["message"])
            self.assertFalse(spotify_search({"query": "bad\ntext"})["ok"])


class FolderTests(unittest.TestCase):
    def test_path_rejects_escape_devices_streams_and_protected_names(self):
        for path in ("../secret", "a/../b", "C:\\Windows", "\\server\\share", "a:secret", ".env", "a//b", "CON.txt", "folder.", "trailing ", "a\nb", "a/" ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parts(path)
        self.assertEqual(parts("Proje/Deneme"), ("Proje", "Deneme"))

    def test_metadata_only_listing_and_open_folder_not_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "note.txt").write_text("private text")
            (root / ".env").write_text("fake secret")
            tools = FolderTools(lambda name: root)
            result = tools.list({"root": "workspace"})
            self.assertEqual(result["entries"], [{"name": "note.txt", "kind": "file"}])
            self.assertNotIn("private text", str(result))
            with patch("os.startfile") as start:
                self.assertTrue(tools.open({"root": "workspace"})["ok"])
                with self.assertRaises(ValueError):
                    tools.open({"root": "workspace", "relative_path": "note.txt"})
                start.assert_called_once()

    def test_create_only_one_directory_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = FolderTools(lambda name: Path(folder))
            self.assertTrue(tools.create({"name": "Test"})["ok"])
            self.assertFalse(tools.create({"name": "Test"})["ok"])
            self.assertFalse(tools.create({"name": "More/Nested"})["ok"])
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["Test"])

    def test_listing_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            for i in range(105):
                Path(folder, f"{i}.txt").touch()
            result = FolderTools(lambda name: Path(folder)).list({"root": "workspace"})
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["entries"]), 100)

    def test_reparse_ancestor_rejected(self):
        import stat
        with tempfile.TemporaryDirectory() as folder:
            tools = FolderTools(lambda name: Path(folder))
            real = os.lstat
            def attributes(path, *args, **kwargs):
                if Path(path) == Path(folder).parent:
                    return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return real(path, *args, **kwargs)
            with patch("agentic.filesystem.os.lstat", side_effect=attributes):
                with self.assertRaises(ValueError):
                    tools.directory("workspace")

    def test_listing_fits_transport_even_with_long_unicode_names(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            for i in range(80):
                Path(folder, str(i) + "界" * 140).touch()
            result = FolderTools(lambda name: Path(folder)).list({"root": "workspace"})
            self.assertTrue(result["truncated"])
            self.assertLess(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 25000)


class SystemTests(unittest.TestCase):
    def test_cli_help_and_workspace_do_not_call_model(self):
        from agentic.cli import interact
        core = Mock()
        with patch("builtins.input", side_effect=["/help", "/workspace", "quit"]), patch("builtins.print"):
            self.assertEqual(interact(core), 0)
        core.handle.assert_not_called()

    def test_brightness_uses_typed_inputs_and_handles_void_driver_result(self):
        service, method, parameters = Mock(), Mock(), Mock()
        monitor = SimpleNamespace(InstanceName="test-display", CurrentBrightness=50)
        method.InstanceName = "test-display"
        method.Methods_.return_value.InParameters.SpawnInstance_.return_value = parameters
        properties = {name: SimpleNamespace(Value=None) for name in ("Timeout", "Brightness")}
        parameters.Properties_.side_effect = properties.__getitem__
        service.ExecQuery.side_effect = [[monitor], [method], [monitor]]
        method.ExecMethod_.return_value = None
        with patch("win32com.client.GetObject", return_value=service), patch("pythoncom.CoInitialize"), patch("pythoncom.CoUninitialize"):
            self.assertTrue(brightness({"level": 50})["ok"])
        self.assertEqual(properties["Timeout"].Value, 0)
        self.assertEqual(properties["Brightness"].Value, 50)
        method.ExecMethod_.assert_called_once_with("WmiSetBrightness", parameters)

    def test_brightness_multiple_monitors_is_not_guessed(self):
        service = Mock()
        service.ExecQuery.return_value = [Mock(), Mock()]
        with patch("win32com.client.GetObject", return_value=service), patch("pythoncom.CoInitialize"), patch("pythoncom.CoUninitialize"):
            self.assertFalse(brightness({"level": 50})["ok"])

    def test_mute_readback_and_status(self):
        endpoint = Mock()
        endpoint.GetMasterVolumeLevelScalar.return_value = .2
        endpoint.GetMute.return_value = 1
        with patch("agentic.system_tools.audio_endpoint", return_value=endpoint):
            self.assertEqual(audio_status({}), {"ok": True, "level": 20, "muted": True})
            self.assertTrue(mute({"state": "muted"})["ok"])
            self.assertFalse(mute({"state": "unmuted"})["ok"])

    def test_settings_only_fixed_uris(self):
        with patch("os.startfile") as start:
            self.assertTrue(open_settings({"page": "sound"})["ok"])
            start.assert_called_once_with(SETTINGS["sound"])
            self.assertFalse(build_local_registry().dispatch("open_settings", {"page": "cmd.exe"})["ok"])

    def test_new_risks_and_schema_bounds(self):
        registry = build_local_registry()
        for name in ("list_media_sessions", "search_spotify", "list_folder", "open_folder", "create_workspace_folder", "open_settings", "lock_screen"):
            self.assertTrue(PermissionEngine().decide(registry.get(name)[0], {}).requires_confirmation)
        for name, args in (("set_brightness", {"level": 0}), ("set_brightness", {"level": True}),
                           ("set_mute", {"state": "toggle"}), ("create_workspace_folder", {"name": "x", "root": "desktop"}),
                           ("list_folder", {"root": "C:\\Windows"})):
            self.assertFalse(registry.dispatch(name, args)["ok"])

    def test_worker_denies_new_yellow_actions_without_confirmation(self):
        worker = DeviceWorker()
        import time
        for capability, args in (("system.lock", {}), ("files.list", {"root": "documents"}),
                                 ("media.sessions", {}), ("files.create_folder", {"name": "NeverCreated"})):
            with patch.object(worker.registry, "dispatch") as dispatch:
                result = worker.handle({"version": 1, "operation": "execute", "id": "deny",
                    "device_id": worker.local.device.device_id, "capability": capability,
                    "arguments": args, "confirmed": False, "deadline": time.time()+10})
                self.assertFalse(result["ok"])
                dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
