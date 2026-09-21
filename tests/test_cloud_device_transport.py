import asyncio
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.cloud_transport import CloudDeviceTransport
from agentic.devices import Device
from server import cloud_device_agent
from server.cloud_device_agent import device_url


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.sent = asyncio.Event()

    async def send_json(self, message):
        self.messages.append(message)
        self.sent.set()


class CloudDeviceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_keeps_websocket_io_on_event_loop(self):
        websocket = FakeWebSocket()
        device = Device(
            device_id="legion",
            display_name="Legion",
            platform="windows",
            capabilities={"system.info"},
        )
        transport = CloudDeviceTransport(
            websocket,
            device,
            asyncio.get_running_loop(),
            timeout=2,
        )

        execution = asyncio.create_task(asyncio.to_thread(
            transport.execute,
            "system.info",
            {},
            request_id="task-1",
        ))
        await asyncio.wait_for(websocket.sent.wait(), timeout=1)
        command = websocket.messages[0]
        self.assertEqual(command["type"], "device.execute")
        self.assertEqual(command["id"], "task-1")
        self.assertFalse(command["confirmed"])
        self.assertTrue(transport.handle_message({
            "type": "device.result",
            "id": "task-1",
            "result": {"ok": True, "hostname": "legion"},
        }))
        self.assertEqual(await execution, {"ok": True, "hostname": "legion"})

    async def test_invalid_result_does_not_complete_pending_request(self):
        websocket = FakeWebSocket()
        transport = CloudDeviceTransport(
            websocket,
            Device("legion", "Legion", "windows", {"system.info"}),
            asyncio.get_running_loop(),
            timeout=1,
        )
        execution = asyncio.create_task(asyncio.to_thread(
            transport.execute,
            "system.info",
            {},
            request_id="task-2",
        ))
        await asyncio.wait_for(websocket.sent.wait(), timeout=1)
        self.assertFalse(transport.handle_message({
            "type": "device.result",
            "id": "task-2",
            "result": {"ok": "yes"},
        }))
        transport.close()
        result = await execution
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "unknown")

    def test_remote_server_url_maps_to_device_endpoint(self):
        self.assertEqual(
            device_url("wss://epis.example/ws"),
            "wss://epis.example/device/ws",
        )
        with self.assertRaises(ValueError):
            device_url("https://epis.example/ws")

    def test_control_plane_token_is_removed_before_tool_worker_runs(self):
        def close_coroutine(coroutine):
            coroutine.close()

        with patch.dict(os.environ, {
            "EPIS_SERVER_URL": "wss://epis.example/ws",
            "EPIS_SERVER_TOKEN": "secret-token",
        }, clear=True), patch.object(
            cloud_device_agent.asyncio,
            "run",
            side_effect=close_coroutine,
        ):
            self.assertEqual(cloud_device_agent.main(), 0)
            self.assertNotIn("EPIS_SERVER_URL", os.environ)
            self.assertNotIn("EPIS_SERVER_TOKEN", os.environ)


if __name__ == "__main__":
    unittest.main()
