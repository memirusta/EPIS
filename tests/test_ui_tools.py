import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.ui_tools import UIController


class FakeControl:
    def __init__(
        self,
        name,
        role="ButtonControl",
        automation_id="id",
        enabled=True,
        password=False,
    ):
        self.name = name
        self.role = role
        self.automation_id = automation_id
        self.enabled = enabled
        self.password = password
        self.clicked = 0
        self.value = None


class FakeBackend:
    def __init__(self, controls, process_name="app.exe"):
        self.controls = controls
        self.process_name = process_name
        self.hwnd = 42
        self.keys = []
        self.navigated = []

    def inspect_foreground(self, max_depth):
        return self.hwnd, self.process_name, list(self.controls)

    def foreground_handle(self):
        return self.hwnd

    def foreground_process(self):
        return self.process_name

    def meta(self, control):
        return {
            "name": control.name,
            "role": control.role,
            "automation_id": control.automation_id,
            "enabled": control.enabled,
            "password": control.password,
        }

    def click(self, control):
        control.clicked += 1

    def set_value(self, control, text):
        control.value = text

    def send_keys(self, keys):
        self.keys.append(keys)

    def navigate_https(self, url):
        self.navigated.append(url)


class UIToolTests(unittest.TestCase):
    def test_sensitive_send_button_is_blocked_by_routine_click(self):
        send = FakeControl("Send")
        controller = UIController(FakeBackend([send]))
        inspected = controller.inspect({})
        element = inspected["elements"][0]
        self.assertEqual(element["interaction"], "sensitive")

        routine = controller.click(
            {"element_ref": element["element_ref"]},
            approved_sensitive=False,
        )
        self.assertFalse(routine["ok"])
        self.assertEqual(send.clicked, 0)

        approved = controller.click(
            {"element_ref": element["element_ref"]},
            approved_sensitive=True,
        )
        self.assertTrue(approved["ok"])
        self.assertEqual(send.clicked, 1)

    def test_project_editor_text_is_sensitive(self):
        edit = FakeControl("Editor", role="EditControl")
        controller = UIController(
            FakeBackend([edit], process_name="Code.exe")
        )
        inspected = controller.inspect({})
        element = inspected["elements"][0]
        self.assertEqual(element["interaction"], "sensitive")
        denied = controller.type_text(
            {
                "element_ref": element["element_ref"],
                "text": "change",
            },
            approved_sensitive=False,
        )
        self.assertFalse(denied["ok"])
        self.assertIsNone(edit.value)

    def test_routine_text_field_can_be_set_without_sensitive_path(self):
        edit = FakeControl("Search", role="EditControl")
        controller = UIController(FakeBackend([edit]))
        element = controller.inspect({})["elements"][0]
        result = controller.type_text(
            {
                "element_ref": element["element_ref"],
                "text": "hello",
            },
            approved_sensitive=False,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(edit.value, "hello")

    def test_foreground_change_invalidates_refs(self):
        button = FakeControl("Open")
        backend = FakeBackend([button])
        controller = UIController(backend)
        element = controller.inspect({})["elements"][0]
        backend.hwnd = 99
        result = controller.click(
            {"element_ref": element["element_ref"]},
            approved_sensitive=False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(button.clicked, 0)

    def test_https_navigation_is_routine_only_for_supported_foreground_browser(self):
        backend = FakeBackend([], process_name="Nebula.exe")
        controller = UIController(backend)
        result = controller.navigate_https({"url": "https://www.youtube.com/"})
        self.assertTrue(result["ok"])
        self.assertEqual(
            backend.navigated,
            ["https://www.youtube.com/"],
        )

        backend.process_name = "Code.exe"
        denied = controller.navigate_https({"url": "https://www.youtube.com/"})
        self.assertFalse(denied["ok"])

        backend.process_name = "Nebula.exe"
        unsafe = controller.navigate_https({"url": "http://example.com/"})
        self.assertFalse(unsafe["ok"])


if __name__ == "__main__":
    unittest.main()
