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

from agentic.hosted_capabilities import (
    OpenAIHostedProvider,
)


class FakeResponse:
    def __init__(
        self,
        text="answer",
        dumped=None,
    ):
        self.output_text = text
        self.usage = None
        self._dumped = dumped or {}

    def model_dump(self):
        return self._dumped


class FakeResponses:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response=None):
        self.responses = FakeResponses(response)


class HostedCapabilityTests(unittest.TestCase):
    def provider(self, client, **kwargs):
        return OpenAIHostedProvider(
            api_key="test-key",
            client_factory=lambda: client,
            **kwargs,
        )

    def test_web_research_uses_hosted_web_search_and_sources(self):
        client = FakeClient(
            FakeResponse(
                "fresh",
                {
                    "output": [{
                        "action": {
                            "sources": [{
                                "url": "https://example.com/a",
                                "title": "Example",
                            }]
                        }
                    }]
                },
            )
        )
        provider = self.provider(client)
        result = provider.execute(
            "web.research",
            {
                "query": "current thing",
                "allowed_domains": [
                    "example.com",
                ],
                "context_size": "high",
            },
        )
        self.assertTrue(result["ok"])
        call = client.responses.calls[0]
        self.assertEqual(
            call["tools"][0]["type"],
            "web_search",
        )
        self.assertEqual(
            call["tools"][0]["filters"][
                "allowed_domains"
            ],
            ["example.com"],
        )
        self.assertEqual(
            result["sources"][0]["url"],
            "https://example.com/a",
        )

    def test_code_analysis_is_hosted_not_local(self):
        client = FakeClient(FakeResponse("42"))
        provider = self.provider(client)
        result = provider.execute(
            "code.analyze",
            {"task": "calculate"},
        )
        self.assertTrue(result["ok"])
        tool = client.responses.calls[0]["tools"][0]
        self.assertEqual(
            tool["type"],
            "code_interpreter",
        )
        self.assertEqual(
            result["executed_in"],
            "openai_hosted_container",
        )

    def test_vision_uses_image_input(self):
        client = FakeClient(FakeResponse("I see it"))
        provider = self.provider(client)
        result = provider.execute(
            "vision.analyze",
            {
                "image_url": "https://example.com/image.png",
                "prompt": "What is this?",
            },
        )
        self.assertTrue(result["ok"])
        content = (
            client.responses.calls[0]
            ["input"][0]["content"]
        )
        self.assertEqual(
            content[1]["type"],
            "input_image",
        )

    def test_hosted_file_search_only_live_when_configured(self):
        client = FakeClient(FakeResponse("found"))
        plain = self.provider(client)
        self.assertNotIn(
            "files.hosted_search",
            plain.capabilities(),
        )

        configured = self.provider(
            client,
            vector_store_ids=["vs_test"],
        )
        self.assertIn(
            "files.hosted_search",
            configured.capabilities(),
        )
        result = configured.execute(
            "files.hosted_search",
            {"query": "report"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            client.responses.calls[-1]["tools"][0][
                "vector_store_ids"
            ],
            ["vs_test"],
        )

    def test_invalid_web_domain_fails_before_api_call(self):
        client = FakeClient()
        provider = self.provider(client)
        result = provider.execute(
            "web.research",
            {
                "query": "x",
                "allowed_domains": [
                    "https://example.com/path",
                ],
            },
        )
        # URL form is normalized to its hostname and is safe.
        self.assertTrue(result["ok"])

        result = provider.execute(
            "web.research",
            {
                "query": "x",
                "allowed_domains": [
                    "example.com:444",
                ],
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            len(client.responses.calls),
            1,
        )


if __name__ == "__main__":
    unittest.main()
