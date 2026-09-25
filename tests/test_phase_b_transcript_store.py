from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from server.daily_transcript import SqliteDailyTranscriptStore
from nc_transcript_client import CloudTranscriptClient, _server_http_url


class PhaseBTranscriptStoreTests(unittest.TestCase):
    def test_pending_days_are_oldest_first_and_processed_is_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SqliteDailyTranscriptStore(Path(temp) / "daily.sqlite3")
            store.append_message(role="user", text="older", day_id="2026-09-20")
            frozen = store.freeze_day("2026-09-20")
            store.append_message(role="user", text="newer", day_id="2026-09-21")
            pending = store.list_pending_days(before_day_id="2026-09-22")
            self.assertEqual([row["day_id"] for row in pending], ["2026-09-20", "2026-09-21"])
            self.assertEqual([row["status"] for row in pending], ["frozen", "active"])
            store.mark_processed("2026-09-20", frozen["input_hash"])
            self.assertEqual(
                [row["day_id"] for row in store.list_pending_days(before_day_id="2026-09-22")],
                ["2026-09-21"],
            )


    def test_atomic_ack_marks_and_purges_exact_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SqliteDailyTranscriptStore(Path(temp) / "daily.sqlite3")
            store.append_message(role="user", text="keep until commit", day_id="2026-09-20")
            frozen = store.freeze_day("2026-09-20")
            with self.assertRaises(RuntimeError):
                store.acknowledge_processed("2026-09-20", "0" * 64, purge=True)
            self.assertEqual(len(store.list_messages("2026-09-20")), 1)
            receipt = store.acknowledge_processed(
                "2026-09-20", frozen["input_hash"], purge=True
            )
            self.assertEqual(receipt, {"marked": True, "purged": 1})
            self.assertEqual(store.list_messages("2026-09-20"), [])
            again = store.acknowledge_processed(
                "2026-09-20", frozen["input_hash"], purge=True
            )
            self.assertEqual(again, {"marked": True, "purged": 0})

    def test_transcript_client_does_not_treat_missing_endpoint_as_success(self):
        client = CloudTranscriptClient(base_url="https://example.test", token="token")
        response = Mock(status_code=404, text="not found")
        response.json.return_value = {"detail": "Not Found"}
        with patch("nc_transcript_client.requests.request", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "shared_transcript_http_404"):
                client.pending_days(before_day_id="2026-09-24")

    def test_runtime_websocket_url_is_safely_normalized_to_https(self):
        with patch.dict(os.environ, {"EPIS_SERVER_URL": "wss://example.test/ws"}, clear=False):
            for key in ("EPIS_SERVER_HTTP_URL", "EPIS_SHARED_RUNTIME_URL", "EPIS_RUNTIME_URL"):
                os.environ.pop(key, None)
            self.assertEqual(_server_http_url(), "https://example.test")


if __name__ == "__main__":
    unittest.main()
