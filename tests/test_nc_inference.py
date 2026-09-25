from pathlib import Path
from types import SimpleNamespace
import os
import sys
import unittest

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Layer-2" / "src"
sys.path.insert(0, str(SRC))

# Make server import deterministic for this isolated regression file.
os.environ.setdefault("EPIS_DEPLOYMENT", "local")
os.environ.setdefault("EPIS_SERVER_TOKEN", "nc-test-server-token")
os.environ.setdefault("EPIS_INTERNAL_EVENT_TOKEN", "nc-test-internal-token")

from agentic.luna import OpenAILunaClient
from nc_transcript_client import CloudTranscriptClient
import server.app as server_app


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="ok",
                        tool_calls=None,
                    )
                )
            ],
        )


class FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=FakeCompletions()
        )


class FakeLuna:
    model = "test-luna"

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
            }
        )
        return SimpleNamespace(
            text='{"type":"direct","message":"ok"}',
            tool_calls=[],
        )


class FakeSol:
    model = "test-sol"

    def __init__(self):
        self.calls = []

    def analyze(self, task, context=None):
        self.calls.append(
            {
                "task": task,
                "context": context,
            }
        )
        return {
            "ok": True,
            "model_used": self.model,
            "result": '{"mood_estimate":"pozitif"}',
        }


class NcInferenceTests(unittest.TestCase):

    def test_luna_omits_tool_fields_when_tools_are_empty(self):
        fake_openai = FakeOpenAI()

        luna = OpenAILunaClient(
            model="test-luna",
            api_key="test-key",
        )
        luna._client = lambda: fake_openai

        reply = luna.complete(
            [{"role": "user", "content": "hello"}],
            [],
        )

        self.assertEqual(reply.text, "ok")
        self.assertEqual(
            len(fake_openai.chat.completions.calls),
            1,
        )

        request = fake_openai.chat.completions.calls[0]

        self.assertNotIn("tools", request)
        self.assertNotIn("tool_choice", request)
        self.assertNotIn("parallel_tool_calls", request)

    def test_cloud_client_infer_uses_internal_nc_endpoint(self):
        client = CloudTranscriptClient(
            base_url="https://example.invalid",
            token="test-token",
        )

        calls = []

        def fake_json(method, path, *, body=None, timeout=None):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "body": body,
                    "timeout": timeout,
                }
            )
            return {
                "ok": True,
                "text": "summary-result",
            }

        client._json = fake_json

        result = client.infer(
            stage="summary",
            prompt="summarize this",
        )

        self.assertEqual(result, "summary-result")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(
            calls[0]["path"],
            "/internal/nc/infer",
        )
        self.assertEqual(
            calls[0]["body"],
            {
                "stage": "summary",
                "prompt": "summarize this",
            },
        )
        self.assertGreaterEqual(
            calls[0]["timeout"],
            120.0,
        )

    def test_internal_nc_summary_is_stateless_and_tool_free(self):
        luna = FakeLuna()
        sol = FakeSol()
        fake_core = SimpleNamespace(
            luna=luna,
            sol=sol,
        )

        old_get_core = server_app.get_core
        old_token = server_app.INTERNAL_EVENT_TOKEN

        server_app.get_core = lambda: fake_core
        server_app.INTERNAL_EVENT_TOKEN = "nc-test-token"

        try:
            with TestClient(server_app.app) as client:
                response = client.post(
                    "/internal/nc/infer",
                    headers={
                        "Authorization": "Bearer nc-test-token",
                    },
                    json={
                        "stage": "summary",
                        "prompt": "Summarize safely.",
                    },
                )

            self.assertEqual(
                response.status_code,
                200,
                response.text,
            )

            payload = response.json()

            self.assertTrue(payload["ok"])
            self.assertEqual(
                payload["stage"],
                "summary",
            )
            self.assertEqual(
                payload["model"],
                "test-luna",
            )

            self.assertEqual(len(luna.calls), 1)
            self.assertEqual(
                luna.calls[0]["tools"],
                [],
            )
            self.assertEqual(sol.calls, [])

            messages = luna.calls[0]["messages"]
            self.assertEqual(
                messages[0]["role"],
                "system",
            )
            self.assertEqual(
                messages[1]["role"],
                "user",
            )
            self.assertEqual(
                messages[1]["content"],
                "Summarize safely.",
            )

        finally:
            server_app.get_core = old_get_core
            server_app.INTERNAL_EVENT_TOKEN = old_token

    def test_internal_nc_analysis_routes_to_sol(self):
        luna = FakeLuna()
        sol = FakeSol()
        fake_core = SimpleNamespace(
            luna=luna,
            sol=sol,
        )

        old_get_core = server_app.get_core
        old_token = server_app.INTERNAL_EVENT_TOKEN

        server_app.get_core = lambda: fake_core
        server_app.INTERNAL_EVENT_TOKEN = "nc-test-token"

        try:
            with TestClient(server_app.app) as client:
                response = client.post(
                    "/internal/nc/infer",
                    headers={
                        "Authorization": "Bearer nc-test-token",
                    },
                    json={
                        "stage": "analysis",
                        "prompt": "Return structured analysis.",
                    },
                )

            self.assertEqual(
                response.status_code,
                200,
                response.text,
            )

            payload = response.json()

            self.assertEqual(
                payload["text"],
                '{"mood_estimate":"pozitif"}',
            )
            self.assertEqual(
                payload["model"],
                "test-sol",
            )

            self.assertEqual(luna.calls, [])
            self.assertEqual(len(sol.calls), 1)
            self.assertEqual(
                sol.calls[0]["task"],
                "Return structured analysis.",
            )
            self.assertEqual(
                sol.calls[0]["context"],
                {"reason": "nightly_analysis"},
            )

        finally:
            server_app.get_core = old_get_core
            server_app.INTERNAL_EVENT_TOKEN = old_token

    def test_internal_nc_rejects_invalid_stage_before_model_call(self):
        luna = FakeLuna()
        sol = FakeSol()

        fake_core = SimpleNamespace(
            luna=luna,
            sol=sol,
        )

        old_get_core = server_app.get_core
        old_token = server_app.INTERNAL_EVENT_TOKEN

        server_app.get_core = lambda: fake_core
        server_app.INTERNAL_EVENT_TOKEN = "nc-test-token"

        try:
            with TestClient(server_app.app) as client:
                response = client.post(
                    "/internal/nc/infer",
                    headers={
                        "Authorization": "Bearer nc-test-token",
                    },
                    json={
                        "stage": "arbitrary",
                        "prompt": "hello",
                    },
                )

            self.assertEqual(
                response.status_code,
                400,
            )
            self.assertEqual(luna.calls, [])
            self.assertEqual(sol.calls, [])

        finally:
            server_app.get_core = old_get_core
            server_app.INTERNAL_EVENT_TOKEN = old_token


if __name__ == "__main__":
    unittest.main()
