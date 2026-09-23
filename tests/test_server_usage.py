import sys
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from fastapi.testclient import TestClient
import server.app as server_app
from agentic.devices import DeviceRegistry, UnavailableDeviceAgent
from agentic.tools import build_local_registry


class FakeCloudCore:
    def __init__(self):
        self.registry = build_local_registry()
        self.devices = DeviceRegistry()
        self.local_agent = UnavailableDeviceAgent(self.devices)
        self.transports = {self.local_agent.device.device_id: self.local_agent}

    def attach_transport(self, transport):
        self.devices.register(transport.device)
        self.transports[transport.device.device_id] = transport

    def detach_transport(self, device_id, transport=None):
        current = self.transports.get(device_id)
        if current is None or (transport is not None and current is not transport):
            return False
        self.transports.pop(device_id)
        current.close()
        return True


class ServerUsageTests(unittest.TestCase):
    def test_websocket_subprotocol_requires_the_exact_server_token(self):
        self.assertEqual(
            server_app.authorized_subprotocol("epis, private-token", "private-token"),
            "epis",
        )
        self.assertIsNone(
            server_app.authorized_subprotocol("epis, wrong-token", "private-token")
        )
        self.assertIsNone(
            server_app.authorized_subprotocol("private-token", "private-token")
        )
        self.assertEqual(
            server_app.authorized_subprotocol(
                "epis-device, private-token",
                "private-token",
                "epis-device",
            ),
            "epis-device",
        )

    def test_device_hello_allows_only_known_windows_capabilities(self):
        device = server_app.device_from_hello(
            {
                "type": "device.hello",
                "version": 1,
                "device": {
                    "device_id": "legion-1",
                    "display_name": "Legion",
                    "platform": "windows",
                    "capabilities": ["system.info"],
                },
            },
            {"system.info", "app.open"},
        )
        self.assertTrue(device.online)
        self.assertEqual(device.capabilities, {"system.info"})

        with self.assertRaises(ValueError):
            server_app.device_from_hello(
                {
                    "type": "device.hello",
                    "version": 1,
                    "device": {
                        "device_id": "legion-1",
                        "display_name": "Legion",
                        "platform": "windows",
                        "capabilities": ["unknown.root"],
                    },
                },
                {"system.info"},
            )


    def test_device_hello_enforces_platform_specific_android_capabilities(self):
        allowed = {
            "windows": {"system.info"},
            "android": {"phone.call"},
        }
        device = server_app.device_from_hello(
            {
                "type": "device.hello",
                "version": 1,
                "device": {
                    "device_id": "android-1",
                    "display_name": "Galaxy",
                    "platform": "android",
                    "capabilities": ["phone.call"],
                },
            },
            allowed,
        )
        self.assertEqual(device.platform, "android")
        self.assertEqual(device.capabilities, {"phone.call"})

        with self.assertRaises(ValueError):
            server_app.device_from_hello(
                {
                    "type": "device.hello",
                    "version": 1,
                    "device": {
                        "device_id": "android-1",
                        "display_name": "Galaxy",
                        "platform": "android",
                        "capabilities": ["system.info"],
                    },
                },
                allowed,
            )

    def test_usage_get_returns_a_snapshot_over_the_existing_websocket(self):
        expected = {
            "window": {"hours": 24, "from": "start", "to": "end"},
            "totals": {"requests": 0},
            "by_model": [],
            "recent_calls": [],
        }
        previous_core = server_app._core
        server_app._core = SimpleNamespace(
            usage_repository=SimpleNamespace(
                snapshot=lambda *, hours, limit: expected,
            ),
        )
        self.addCleanup(setattr, server_app, "_core", previous_core)

        with TestClient(server_app.app) as client:
            with client.websocket_connect("/ws") as websocket:
                self.assertEqual(websocket.receive_json()["type"], "connected")
                websocket.send_json({"type": "usage.get", "hours": 24, "limit": 20})
                response = websocket.receive_json()

        self.assertEqual(response["type"], "usage.snapshot")
        self.assertEqual(response["data"], expected)

    def test_device_websocket_round_trip(self):
        previous_core = server_app._core
        previous_token = server_app.SERVER_TOKEN
        core = FakeCloudCore()
        server_app._core = core
        server_app.SERVER_TOKEN = "private-token"
        self.addCleanup(setattr, server_app, "_core", previous_core)
        self.addCleanup(setattr, server_app, "SERVER_TOKEN", previous_token)

        with TestClient(server_app.app) as client:
            with client.websocket_connect(
                "/device/ws",
                subprotocols=["epis-device", "private-token"],
            ) as websocket:
                websocket.send_json({
                    "type": "device.hello",
                    "version": 1,
                    "device": {
                        "device_id": "legion-test",
                        "display_name": "Legion Test",
                        "platform": "windows",
                        "capabilities": ["system.info"],
                    },
                })
                connected = websocket.receive_json()
                self.assertEqual(connected["type"], "device.connected")
                transport = core.transports["legion-test"]
                results = queue.Queue()
                worker = threading.Thread(
                    target=lambda: results.put(transport.execute(
                        "system.info",
                        {},
                        request_id="task-123",
                    )),
                )
                worker.start()
                command = websocket.receive_json()
                self.assertEqual(command["type"], "device.execute")
                self.assertEqual(command["id"], "task-123")
                websocket.send_json({
                    "type": "device.result",
                    "id": "task-123",
                    "result": {"ok": True, "hostname": "legion-test"},
                })
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results.get_nowait()["hostname"], "legion-test")


if __name__ == "__main__":
    unittest.main()
