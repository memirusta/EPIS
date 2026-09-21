import sys
from pathlib import Path
import unittest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "Layer-2"
        / "src"
    ),
)

from agentic.computer_device import (
    ComputerDeviceController,
)
from agentic.tools import build_local_registry


class FakeWindows:
    def __init__(self):
        self.row = {
            "hwnd": 42,
            "pid": 7,
            "created": 1.0,
            "process": "app.exe",
            "class": "App",
            "minimized": False,
        }
        self.calls = []

    def resolve_selection(
        self,
        arguments,
        *,
        refresh_ttl=False,
    ):
        self.calls.append(
            (dict(arguments), refresh_ttl)
        )
        if (
            arguments.get("window_id") != "window"
            or arguments.get("app_name")
            != "app.exe"
        ):
            return None, {
                "ok": False,
                "error": "bad window",
            }
        return dict(self.row), None


class FakeBackend:
    def __init__(self):
        self.events = []
        self.foreground_pid = 7
        self.password = False
        self.rect = (100, 200, 1100, 700)

    def capture_window(self, row):
        return {
            "bytes": b"jpeg-bytes",
            "mime_type": "image/jpeg",
            "width": 500,
            "height": 250,
            "window_width": 1000,
            "window_height": 500,
            "rect": self.rect,
        }

    def window_rect(self, row):
        return self.rect

    def foreground_info(self):
        return {
            "hwnd": 42,
            "pid": self.foreground_pid,
            "process": "app.exe",
        }

    def password_field_focused(self):
        return self.password

    def click(
        self,
        x,
        y,
        button,
        *,
        double=False,
        modifiers=None,
    ):
        self.events.append(
            (
                "click",
                x,
                y,
                button,
                double,
                tuple(modifiers or []),
            )
        )

    def move(self, x, y):
        self.events.append(
            ("move", x, y)
        )

    def scroll(
        self,
        x,
        y,
        sx,
        sy,
        *,
        modifiers=None,
    ):
        self.events.append(
            (
                "scroll",
                x,
                y,
                sx,
                sy,
                tuple(modifiers or []),
            )
        )

    def drag(
        self,
        path,
        *,
        modifiers=None,
    ):
        self.events.append(
            (
                "drag",
                tuple(path),
                tuple(modifiers or []),
            )
        )

    def keypress(self, keys):
        self.events.append(
            ("keypress", tuple(keys))
        )

    def type_text(self, text):
        self.events.append(
            ("type", text)
        )


class ComputerDeviceTests(unittest.TestCase):
    def controller(self):
        windows = FakeWindows()
        backend = FakeBackend()
        controller = ComputerDeviceController(
            windows,
            backend,
        )
        return controller, windows, backend

    def test_capture_binds_opaque_frame_to_window(self):
        controller, windows, _ = self.controller()
        result = controller.capture({
            "window_id": "window",
            "app_name": "app.exe",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["mime_type"],
            "image/jpeg",
        )
        self.assertEqual(
            len(result["frame_id"]),
            32,
        )
        self.assertTrue(
            windows.calls[-1][1]
        )

    def test_frame_coordinates_map_only_inside_captured_window(self):
        controller, _, backend = self.controller()
        frame = controller.capture({
            "window_id": "window",
            "app_name": "app.exe",
        })
        result = controller.apply_actions({
            "window_id": "window",
            "app_name": "app.exe",
            "frame_id": frame["frame_id"],
            "actions": [{
                "type": "click",
                "button": "left",
                "x": 250,
                "y": 125,
            }],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(
            backend.events[0][:4],
            (
                "click",
                600,
                450,
                "left",
            ),
        )

        denied = controller.apply_actions({
            "window_id": "window",
            "app_name": "app.exe",
            "frame_id": frame["frame_id"],
            "actions": [{
                "type": "click",
                "x": 9999,
                "y": 1,
            }],
        })
        self.assertFalse(denied["ok"])

    def test_unexpected_app_switch_and_password_typing_fail_closed(self):
        controller, _, backend = self.controller()
        frame = controller.capture({
            "window_id": "window",
            "app_name": "app.exe",
        })

        backend.foreground_pid = 99
        lost = controller.apply_actions({
            "window_id": "window",
            "app_name": "app.exe",
            "frame_id": frame["frame_id"],
            "actions": [{
                "type": "type",
                "text": "hello",
            }],
        })
        self.assertFalse(lost["ok"])
        self.assertEqual(
            backend.events,
            [],
        )

        backend.foreground_pid = 7
        backend.password = True
        blocked = controller.apply_actions({
            "window_id": "window",
            "app_name": "app.exe",
            "frame_id": frame["frame_id"],
            "actions": [{
                "type": "type",
                "text": "secret",
            }],
        })
        self.assertFalse(blocked["ok"])
        self.assertEqual(
            backend.events,
            [],
        )

    def test_low_level_computer_tools_are_hidden_from_luna(self):
        registry = build_local_registry()
        visible = {
            item["function"]["name"]
            for item in registry.openai_schemas(
                registry.capabilities("windows")
            )
        }
        self.assertNotIn(
            "computer_capture_screen",
            visible,
        )
        self.assertNotIn(
            "computer_apply_actions",
            visible,
        )
        self.assertIn(
            "computer.capture",
            registry.capabilities("windows"),
        )
        self.assertIn(
            "computer.actions",
            registry.capabilities("windows"),
        )


if __name__ == "__main__":
    unittest.main()
