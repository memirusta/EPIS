from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DesktopCloudRuntimePhase3Tests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_release_runtime_has_no_implicit_localhost_fallback(self):
        server = self.read("Layer-2/desktop-ui/src-tauri/src/server.rs")
        app = self.read("Layer-2/desktop-ui/src/App.tsx")
        self.assertIn("Production EPIS server must use wss://", server)
        self.assertIn("EPIS_DESKTOP_LOCAL_SERVER", server)
        self.assertIn("cfg!(debug_assertions)", server)
        self.assertNotIn('url: "ws://127.0.0.1:8000/ws",\n          token: "",', app)
        self.assertIn("EPIS cloud configuration unavailable", app)

    def test_plaintext_token_is_migrated_to_dpapi(self):
        server = self.read("Layer-2/desktop-ui/src-tauri/src/server.rs")
        self.assertIn("ProtectedData]::Protect", server)
        self.assertIn("ProtectedData]::Unprotect", server)
        self.assertIn("server-token.dpapi", server)
        self.assertIn("strip_legacy_plaintext_token", server)

    def test_device_agent_has_watchdog_and_crash_loop_guard(self):
        server = self.read("Layer-2/desktop-ui/src-tauri/src/server.rs")
        self.assertIn("epis-device-agent-watchdog", server)
        self.assertIn("WATCHDOG_CRASH_LIMIT", server)
        self.assertIn("WATCHDOG_COOLDOWN", server)
        self.assertIn("AlreadyRunning", server)
        self.assertIn("EPIS_SPOTIFY_REDIRECT_URI", server)

    def test_release_bundle_only_contains_cloud_server_entrypoint(self):
        config = json.loads(
            self.read("Layer-2/desktop-ui/src-tauri/tauri.conf.json")
        )
        resources = config["bundle"]["resources"]
        self.assertIn(
            "../../src/server/cloud_device_agent.py",
            resources,
        )
        self.assertNotIn("../../src/server/*.py", resources)

    def test_dev_servers_have_no_canonical_drive_hardcode(self):
        local = self.read("Layer-2/src/server/local_server.py")
        dev = self.read("Layer-2/src/server/dev_server.py")
        self.assertNotIn(r"D:\EPIS", local)
        self.assertNotIn(r"D:\EPIS", dev)
        self.assertIn("LUNA_API_KEY", local)
        self.assertIn("LUNA_API_KEY", dev)

    def test_startup_state_verifies_current_executable(self):
        startup = self.read("Layer-2/desktop-ui/src-tauri/src/startup.rs")
        self.assertIn("EPIS_CURRENT_EXE", startup)
        self.assertIn("Test-Path -LiteralPath $exe -PathType Leaf", startup)
        self.assertIn("OrdinalIgnoreCase", startup)

    def test_frontend_reconnect_uses_backoff(self):
        app = self.read("Layer-2/desktop-ui/src/App.tsx")
        self.assertIn("reconnectDelayRef", app)
        self.assertIn("Math.min(delay * 2, 30000)", app)


if __name__ == "__main__":
    unittest.main()
