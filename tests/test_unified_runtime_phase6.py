import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Layer-2" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic.path_grants import PersistentPathGrantStore
from crypto_layer import EpisCipher


class UnifiedRuntimePhase6Tests(unittest.TestCase):
    def test_desktop_persistence_is_bounded_and_drops_tool_payloads(self):
        app = (ROOT / "Layer-2/desktop-ui/src/App.tsx").read_text(encoding="utf-8")
        self.assertIn('epis.desktop.chat.v2', app)
        self.assertIn('MAX_PERSISTED_MESSAGES = 80', app)
        self.assertIn('.slice(-MAX_PERSISTED_MESSAGES)', app)
        self.assertIn('.map(({ role, text }) => ({', app)
        self.assertIn('LEGACY_CHAT_STORAGE_KEYS', app)
        self.assertIn('window.localStorage.removeItem(key)', app)
        self.assertNotIn('({ role, text, toolResults }) => ({', app)

    def test_mobile_has_no_hard_coded_cloud_deployment_url(self):
        secure = (ROOT / "Layer-2/mobile/lib/src/secure_config.dart").read_text(encoding="utf-8")
        self.assertIn("const defaultEpisServerUrl = '';", secure)
        self.assertNotIn('herokuapp.com', secure.lower())

    def test_mobile_connection_and_refresh_hardening_is_present(self):
        client = (ROOT / "Layer-2/mobile/lib/src/epis_client.dart").read_text(encoding="utf-8")
        controller = (ROOT / "Layer-2/mobile/lib/src/controller.dart").read_text(encoding="utf-8")
        settings = (ROOT / "Layer-2/mobile/lib/src/screens/settings_screen.dart").read_text(encoding="utf-8")
        devices = (ROOT / "Layer-2/mobile/lib/src/screens/devices_screen.dart").read_text(encoding="utf-8")
        chat = (ROOT / "Layer-2/mobile/lib/src/screens/chat_screen.dart").read_text(encoding="utf-8")

        self.assertIn('_reconnectAttempt', client)
        self.assertIn('Duration(seconds: 30)', client)
        self.assertIn("parsed.scheme.toLowerCase() != 'wss'", controller)
        self.assertIn('parsed.userInfo.isNotEmpty', controller)
        self.assertIn('parsed.hasQuery', controller)
        self.assertIn('Future<void> refreshDevices()', controller)
        self.assertIn('completer.future.timeout', controller)
        self.assertIn('bool sendMessage(String text)', controller)
        self.assertIn('finally {', settings)
        self.assertIn('onRefresh: controller.refreshDevices', devices)
        self.assertIn('_nearBottom', chat)
        self.assertIn('_trackScrollPosition', chat)

    def test_path_grants_default_to_protected_security_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            evil = str(Path(folder) / "model-writable.json")
            env = {
                "LOCALAPPDATA": folder,
                "EPIS_PATH_GRANTS_FILE": evil,
            }
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("EPIS_ALLOW_SECURITY_STORE_OVERRIDE", None)
                store = PersistentPathGrantStore()
                self.assertEqual(
                    store.path,
                    Path(folder) / "EPIS" / "security" / "path-grants.json",
                )

                os.environ["EPIS_ALLOW_SECURITY_STORE_OVERRIDE"] = "1"
                override = PersistentPathGrantStore()
                self.assertEqual(override.path, Path(evil))

    def test_encryption_required_writes_fail_closed(self):
        disabled = EpisCipher(key=None, enabled=False)
        self.assertEqual(disabled.encrypt_str("secret"), "secret")

        required = EpisCipher(key=None, enabled=True)
        with self.assertRaises(RuntimeError):
            required.encrypt_str("secret")

    def test_agentic_prompt_matches_current_mutation_contract(self):
        core = (ROOT / "Layer-2/src/epis_core.py").read_text(encoding="utf-8")
        self.assertIn('apply_text_patch', core)
        self.assertIn('ikinci kez semantik onay isteme', core)
        self.assertNotIn('Ardından değişikliği uygulamak isteyip istemediğini sor ve o turda dosya değiştirme', core)

    def test_device_snapshots_are_server_pushed(self):
        app = (ROOT / "Layer-2/src/server/app.py").read_text(encoding="utf-8")
        self.assertIn('async def _broadcast_devices_snapshot()', app)
        self.assertGreaterEqual(app.count('await _broadcast_devices_snapshot()'), 2)

    def test_generated_repo_state_and_backup_files_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn('.epis/', ignore)
        self.assertIn('*.bak', ignore)
        self.assertIn('*.orig', ignore)
        self.assertIn('*.rej', ignore)

    def test_tracked_backup_sources_are_removed(self):
        agentic = ROOT / "Layer-2/src/agentic"
        self.assertFalse((agentic / "core.py.bak-session-read-20260920-202247").exists())
        self.assertFalse((agentic / "core.py.bak-session-read-20260920-202948").exists())
        self.assertFalse((agentic / "luna.py.bak-sol-output-20260920-203956").exists())


if __name__ == "__main__":
    unittest.main()
