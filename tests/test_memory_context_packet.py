from pathlib import Path
import importlib.util
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Layer-2" / "src"


def load_context_builder():
    stub = types.ModuleType("memory_manager")
    stub._STALE_STATE_KEYS = frozenset()
    stub.MemoryManager = object
    previous = sys.modules.get("memory_manager")
    sys.modules["memory_manager"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "context_builder_memory_v1_test",
            SRC / "context_builder.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module.ContextBuilder
    finally:
        if previous is None:
            sys.modules.pop("memory_manager", None)
        else:
            sys.modules["memory_manager"] = previous


class FakeMemory:
    def get_current_state(self):
        return {}

    def get_relevant_memories(self, query, limit=8):
        return [
            {
                "kind": "project",
                "content": "Nebula Browser Tauri tabanlı bir proje",
                "confidence": 0.94,
                "source_day": "2026-09-23",
            }
        ]

    def get_people(self):
        return {"people": {}}

    # These are legacy raw/archive paths. Trusted packet must never touch them.
    def get_recent_interactions(self, *args, **kwargs):
        raise AssertionError("raw lifetime archive accessed")

    def search_interactions(self, *args, **kwargs):
        raise AssertionError("raw lifetime archive accessed")

    def search_sessions(self, *args, **kwargs):
        raise AssertionError("raw session archive accessed")

    def search_thinking_log(self, *args, **kwargs):
        raise AssertionError("thinking log accessed")

    def get_episodic_context(self):
        raise AssertionError("legacy episodic JSON accessed")


class TrustedPacketTests(unittest.TestCase):
    def test_packet_uses_only_relevant_distilled_memory(self):
        ContextBuilder = load_context_builder()
        builder = ContextBuilder(FakeMemory())
        packet = builder.build_trusted_packet("Nebula hakkında ne hatırlıyorsun?")
        self.assertIn("İLGİLİ UZUN SÜRELİ HAFIZA", packet)
        self.assertIn("Nebula Browser", packet)
        self.assertNotIn("thinking_log", packet)
        self.assertNotIn("BU OTURUM", packet)


if __name__ == "__main__":
    unittest.main()
