import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.usage import OpenAIOrganizationUsageRepository, SQLiteUsageRepository
from agentic.luna import OpenAILunaClient


class UsageRepositoryTests(unittest.TestCase):
    def test_tool_usage_is_content_free_and_cost_stays_unknown(self):
        repository = SQLiteUsageRepository(":memory:")
        self.addCleanup(repository.close)

        repository.record_tool_call(
            provider="local-device",
            operation="open_app",
            capability="app.open",
            status="succeeded",
            latency_ms=75,
            device_id="legion",
        )
        snapshot = repository.snapshot()

        self.assertEqual(snapshot["tool_usage"]["totals"]["calls"], 1)
        self.assertEqual(snapshot["tool_usage"]["totals"]["succeeded"], 1)
        self.assertEqual(snapshot["tool_usage"]["totals"]["api_requests"], 0)
        self.assertIsNone(snapshot["tool_usage"]["totals"]["estimated_cost"])
        event = snapshot["tool_usage"]["recent_calls"][0]
        self.assertEqual(event["operation"], "open_app")
        self.assertEqual(event["device_id"], "legion")
        self.assertNotIn("arguments", event)
        self.assertNotIn("result", event)

    def test_snapshot_contains_only_usage_metadata_and_aggregates_cache(self):
        with TemporaryDirectory() as folder:
            repository = SQLiteUsageRepository(Path(folder) / "usage.db")
            try:
                repository.record_openai_response(
                    request_kind="luna",
                    model="gpt-5.6-luna",
                    usage=SimpleNamespace(
                        input_tokens=100,
                        output_tokens=25,
                        input_tokens_details=SimpleNamespace(cached_tokens=60),
                        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                    ),
                    latency_ms=1234,
                )
                repository.record_openai_response(
                    request_kind="sol",
                    model="gpt-5.6-sol",
                    usage={
                        "input_tokens": 80,
                        "output_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 12},
                    },
                    latency_ms=4567,
                )

                snapshot = repository.snapshot(hours=24, limit=10)
            finally:
                repository.close()

        self.assertEqual(snapshot["totals"]["requests"], 2)
        self.assertEqual(snapshot["totals"]["input_tokens"], 180)
        self.assertEqual(snapshot["totals"]["cached_input_tokens"], 60)
        self.assertEqual(snapshot["totals"]["output_tokens"], 45)
        self.assertEqual(snapshot["totals"]["reasoning_tokens"], 12)
        self.assertEqual(snapshot["totals"]["total_tokens"], 225)
        self.assertEqual(snapshot["totals"]["cache_hit_percent"], 33.3)
        self.assertIsNone(snapshot["totals"]["estimated_cost"])
        self.assertEqual(len(snapshot["by_model"]), 2)
        self.assertEqual(len(snapshot["recent_calls"]), 2)
        self.assertNotIn("prompt", repr(snapshot).lower())
        self.assertNotIn("response", repr(snapshot).lower())

    def test_missing_usage_still_records_a_completed_request_without_fake_tokens(self):
        repository = SQLiteUsageRepository(":memory:")
        self.addCleanup(repository.close)

        repository.record_openai_response(
            request_kind="luna",
            model="gpt-5.6-luna",
            usage=None,
            latency_ms=20,
        )

        call = repository.snapshot()["recent_calls"][0]
        self.assertEqual(call["latency_ms"], 20)
        self.assertIsNone(call["input_tokens"])
        self.assertIsNone(call["output_tokens"])

    def test_luna_adapter_records_the_sdk_usage_after_a_real_response_shape(self):
        repository = SQLiteUsageRepository(":memory:")
        self.addCleanup(repository.close)
        client = OpenAILunaClient(
            api_key="test",
            usage_repository=repository,
        )
        sdk = Mock()
        sdk.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=40,
                output_tokens=10,
                input_tokens_details=SimpleNamespace(cached_tokens=8),
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Merhaba", tool_calls=[]),
                ),
            ],
        )

        with patch.object(client, "_client", return_value=sdk):
            reply = client.complete([], [])

        self.assertEqual(reply.text, "Merhaba")
        call = repository.snapshot()["recent_calls"][0]
        self.assertEqual(call["request_kind"], "luna")
        self.assertEqual(call["input_tokens"], 40)
        self.assertEqual(call["cached_input_tokens"], 8)
        self.assertEqual(call["output_tokens"], 10)
        self.assertIsInstance(call["latency_ms"], int)

    def test_luna_adapter_records_chat_completions_usage_field_names(self):
        repository = SQLiteUsageRepository(":memory:")
        self.addCleanup(repository.close)
        client = OpenAILunaClient(
            api_key="test",
            usage_repository=repository,
        )
        sdk = Mock()
        sdk.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=44,
                completion_tokens=11,
                prompt_tokens_details=SimpleNamespace(cached_tokens=9),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Merhaba", tool_calls=[]),
                ),
            ],
        )

        with patch.object(client, "_client", return_value=sdk):
            client.complete([], [])

        call = repository.snapshot()["recent_calls"][0]
        self.assertEqual(call["input_tokens"], 44)
        self.assertEqual(call["cached_input_tokens"], 9)
        self.assertEqual(call["output_tokens"], 11)
        self.assertEqual(call["reasoning_tokens"], 2)

    def test_openai_snapshot_uses_real_org_totals_and_keeps_local_call_details(self):
        local = SQLiteUsageRepository(":memory:")
        self.addCleanup(local.close)
        local.record_openai_response(
            request_kind="sol",
            model="gpt-5.6-sol",
            usage={
                "input_tokens": 80,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 40},
                "output_tokens_details": {"reasoning_tokens": 12},
            },
            latency_ms=1800,
        )

        calls = []

        def fake_get(path, params):
            calls.append((path, dict(params)))
            if path == "/organization/costs":
                return {
                    "data": [
                        {
                            "start_time": 1_758_316_800,
                            "end_time": 1_758_403_200,
                            "results": [
                                {"amount": {"value": 0.08, "currency": "usd"}}
                            ],
                        }
                    ],
                    "has_more": False,
                }
            return {
                "data": [
                    {
                        "start_time": 1_758_366_000,
                        "end_time": 1_758_369_600,
                        "results": [
                            {
                                "model": "gpt-5.6-sol",
                                "api_key_id": "key-123",
                                "num_model_requests": 3,
                                "input_tokens": 120,
                                "input_cached_tokens": 60,
                                "output_tokens": 30,
                            }
                        ],
                    }
                ],
                "has_more": False,
            }

        now = datetime.now(timezone.utc)
        repository = OpenAIOrganizationUsageRepository(
            local,
            admin_api_key="admin-secret",
            http_get=fake_get,
            now_factory=lambda: now,
        )
        snapshot = repository.snapshot(hours=24, limit=20)

        self.assertEqual(snapshot["source"], "openai")
        self.assertEqual(snapshot["totals"]["requests"], 3)
        self.assertEqual(snapshot["totals"]["total_tokens"], 150)
        self.assertEqual(snapshot["totals"]["cached_input_tokens"], 60)
        self.assertEqual(snapshot["totals"]["reasoning_tokens"], 12)
        self.assertEqual(snapshot["totals"]["estimated_cost"], 0.08)
        self.assertEqual(snapshot["totals"]["cost_currency"], "usd")
        self.assertEqual(snapshot["scope"]["cost"], "api_keys")
        self.assertEqual(snapshot["hourly"][0]["total_tokens"], 150)
        self.assertEqual(snapshot["recent_calls"][0]["latency_ms"], 1800)
        self.assertNotIn("admin-secret", repr(snapshot))

        cost_call = next(call for call in calls if call[0] == "/organization/costs")
        self.assertEqual(cost_call[1]["api_key_ids"], ["key-123"])


if __name__ == "__main__":
    unittest.main()
