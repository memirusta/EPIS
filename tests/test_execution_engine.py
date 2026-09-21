import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.core import AgentCore
from agentic.devices import DeviceRegistry, LocalDeviceAgent
from agentic.execution import WorldState
from agentic.luna import ToolCall
from agentic.permissions import PermissionEngine
from agentic.tools import ToolRegistry, ToolSpec
from test_agentic_core import FakeContext, FakeLuna, FakeMemory


def schema(properties=None, required=()):
    return {
        "type": "object",
        "properties": {
            **(properties or {}),
            "device_id": {"type": "string"},
        },
        "required": list(required),
        "additionalProperties": False,
    }


class ExecutionEngineTests(unittest.TestCase):
    def test_world_state_expires_runtime_fact(self):
        now = [10.0]
        world = WorldState(clock=lambda: now[0])
        world.set(
            "pc",
            "window.foreground",
            {"window_id": "w"},
            source="test",
            ttl_seconds=2,
        )
        self.assertEqual(
            world.get("pc", "window.foreground")["window_id"],
            "w",
        )
        now[0] = 13.0
        self.assertIsNone(world.get("pc", "window.foreground"))

    def test_core_focuses_correlated_launch_before_navigation(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "focus_window",
                "Focus exact window.",
                schema({
                    "window_id": {"type": "string"},
                    "app_name": {"type": "string"},
                }, ("window_id", "app_name")),
                "windows.focus",
                effects=("window.foreground",),
            ),
            lambda _args: {"ok": True},
        )
        registry.register(
            ToolSpec(
                "navigate",
                "Navigate browser.",
                schema({"url": {"type": "string"}}, ("url",)),
                "browser.navigate_foreground",
                preconditions=("foreground.latest_app",),
            ),
            lambda _args: {"ok": True},
        )

        calls = []

        def dispatch(capability, arguments):
            calls.append((capability, dict(arguments)))
            if capability == "windows.focus":
                return {
                    "ok": True,
                    "status": "window_state_verified",
                    "state_verified": True,
                }
            if capability == "browser.navigate_foreground":
                return {
                    "ok": True,
                    "status": "navigation_requested",
                    "page_load_verified": False,
                }
            return {"ok": False}

        devices = DeviceRegistry()
        local = LocalDeviceAgent(
            devices,
            dispatch,
            {"windows.focus", "browser.navigate_foreground"},
        )
        core = AgentCore(
            luna=FakeLuna([]),
            system_prompt="EPIS",
            context_builder=FakeContext(),
            memory=FakeMemory(),
            registry=registry,
            devices=devices,
            local_agent=local,
            permissions=PermissionEngine(),
        )
        self.addCleanup(core.close)

        core.execution.world.set(
            local.device.device_id,
            "app.latest_window",
            {
                "window_id": "launch-window",
                "app_name": "app.exe",
            },
            source="launch",
            ttl_seconds=120,
        )

        turn = core._dispatch(
            ToolCall(
                "navigate-1",
                "navigate",
                {"url": "https://www.youtube.com/"},
            ),
            False,
        )

        self.assertEqual(
            [capability for capability, _ in calls],
            ["windows.focus", "browser.navigate_foreground"],
        )
        self.assertTrue(turn.tool_results[0]["ok"])
        self.assertEqual(
            turn.tool_results[0]["core_recovery"][0]["tool"],
            "focus_window",
        )

    def test_spotify_auth_precondition_is_core_owned_and_fail_closed(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "spotify_connection_status",
                "Read auth status.",
                schema(),
                "spotify.auth_status",
                effects=("spotify.authenticated",),
            ),
            lambda _args: {"ok": True},
        )
        registry.register(
            ToolSpec(
                "spotify_search_tracks",
                "Search.",
                schema({"query": {"type": "string"}}, ("query",)),
                "spotify.search",
                preconditions=("spotify.authenticated",),
            ),
            lambda _args: {"ok": True},
        )

        calls = []

        def dispatch(capability, arguments):
            calls.append((capability, dict(arguments)))
            if capability == "spotify.auth_status":
                return {
                    "ok": True,
                    "status": "not_connected",
                    "client_id_configured": True,
                }
            raise AssertionError("search must not run without auth")

        devices = DeviceRegistry()
        local = LocalDeviceAgent(
            devices,
            dispatch,
            {"spotify.auth_status", "spotify.search"},
        )
        core = AgentCore(
            luna=FakeLuna([]),
            system_prompt="EPIS",
            context_builder=FakeContext(),
            memory=FakeMemory(),
            registry=registry,
            devices=devices,
            local_agent=local,
            permissions=PermissionEngine(),
        )
        self.addCleanup(core.close)

        turn = core._dispatch(
            ToolCall(
                "search-1",
                "spotify_search_tracks",
                {"query": "Stateside"},
            ),
            False,
        )

        self.assertEqual(
            [capability for capability, _ in calls],
            ["spotify.auth_status"],
        )
        result = turn.tool_results[0]
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["precondition"],
            "spotify.authenticated",
        )
        self.assertEqual(
            result["recovery_tool"],
            "spotify_connect",
        )


if __name__ == "__main__":
    unittest.main()
