from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.hot_memory import HotConversationStore


class HotConversationStoreTests(unittest.TestCase):
    def test_latest_session_is_not_trimmed_by_old_window_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "hot.jsonl")
            store = HotConversationStore(
                path,
                hours=0.001,
                max_messages=2,
                max_chars=8,
            )

            old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            store._append({"type": "message", "role": "user", "content": "first", "ts": old})
            store._append({"type": "message", "role": "assistant", "content": "second", "ts": old})
            store._append({"type": "message", "role": "user", "content": "third", "ts": old})

            self.assertEqual(
                store.load_latest_session(),
                [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "second"},
                    {"role": "user", "content": "third"},
                ],
            )

    def test_new_conversation_boundary_selects_only_latest_session(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HotConversationStore(Path(folder, "hot.jsonl"))
            store.append_turn("old user", "old assistant")
            store.mark_new_conversation()
            store.append_turn("new user", "new assistant")

            self.assertEqual(
                store.load_latest_session(),
                [
                    {"role": "user", "content": "new user"},
                    {"role": "assistant", "content": "new assistant"},
                ],
            )

    def test_load_recent_remains_compatible_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HotConversationStore(Path(folder, "hot.jsonl"))
            store.append_turn("hello", "hey")
            self.assertEqual(store.load_recent(), store.load_latest_session())


if __name__ == "__main__":
    unittest.main()
