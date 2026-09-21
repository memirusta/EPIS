import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.luna import LunaReply, ToolCall
from agentic.tools import ToolSpec
from test_agentic_core import build_core


class LiveCapabilityTests(unittest.TestCase):
    def test_model_tools_hide_capability_without_online_device(self):
        core = build_core([])
        core.registry.register(
            ToolSpec(
                "offline_only",
                "Unavailable test tool.",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "offline.only",
            ),
            lambda _args: {"ok": True},
        )
        names = {
            item["function"]["name"]
            for item in core._model_tools()
            if item.get("type") == "function"
        }
        self.assertIn("get_system_info", names)
        self.assertNotIn("offline_only", names)

    def test_live_manifest_is_in_system_context(self):
        core = build_core([LunaReply(text="Hazırım.")])
        core.handle("Ne yapabiliyorsun?")
        system = core.luna.calls[0][0][0]["content"]
        self.assertIn("# LIVE CAPABILITIES", system)
        self.assertIn("get_system_info", system)
        self.assertIn("system.info", system)
        self.assertNotIn("wait_for_window", system)

    def test_multi_step_goal_continues_until_final_reply(self):
        core = build_core([
            LunaReply(
                tool_calls=[
                    ToolCall("one", "get_system_info", {})
                ]
            ),
            LunaReply(
                tool_calls=[
                    ToolCall("two", "get_system_info", {})
                ]
            ),
            LunaReply(text="İki adımı da tamamladım."),
        ])
        turn = core.handle("İki kez durum kontrol et ve sonra bitir")
        self.assertEqual(len(turn.tool_results), 2)
        self.assertEqual(turn.message, "İki adımı da tamamladım.")
        self.assertEqual(len(core.luna.calls), 3)


if __name__ == "__main__":
    unittest.main()
