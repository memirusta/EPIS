import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = (
    Path(__file__)
    .resolve()
    .parents[1]
)

SRC = (
    ROOT
    / "Layer-2"
    / "src"
)

sys.path.insert(
    0,
    str(SRC),
)

from agentic.whatsapp_outreach import (
    LocalWhatsappBridge,
    load_whatsapp_bridge_config,
)


class FakeResponse:
    def __init__(
        self,
        status_code,
        payload,
    ):
        self.status_code = (
            status_code
        )
        self.payload = payload

    def json(
        self,
    ):
        return self.payload


class WhatsappLocalBridgeTests(
    unittest.TestCase
):
    def test_remote_bridge_url_is_rejected(
        self,
    ):
        with self.assertRaises(
            ValueError
        ):
            LocalWhatsappBridge(
                "http://10.0.0.5:8766",
                "x" * 48,
            )

        with self.assertRaises(
            ValueError
        ):
            LocalWhatsappBridge(
                "https://127.0.0.1:8766",
                "x" * 48,
            )

    def test_short_bridge_token_is_rejected(
        self,
    ):
        with self.assertRaises(
            ValueError
        ):
            LocalWhatsappBridge(
                "http://127.0.0.1:8766",
                "short",
            )

    def test_health_requires_connected_whatsapp(
        self,
    ):
        bridge = LocalWhatsappBridge(
            "http://127.0.0.1:8766",
            "x" * 48,
        )

        with patch(
            "agentic.whatsapp_outreach.requests.request",
            return_value=FakeResponse(
                200,
                {
                    "ok": True,
                    "whatsapp_connected":
                        False,
                },
            ),
        ):
            self.assertFalse(
                bridge.available()
            )

        with patch(
            "agentic.whatsapp_outreach.requests.request",
            return_value=FakeResponse(
                200,
                {
                    "ok": True,
                    "whatsapp_connected":
                        True,
                },
            ),
        ):
            self.assertTrue(
                bridge.available()
            )

    def test_send_returns_only_safe_bridge_result(
        self,
    ):
        bridge = LocalWhatsappBridge(
            "http://127.0.0.1:8766",
            "x" * 48,
        )

        with patch(
            "agentic.whatsapp_outreach.requests.request",
            return_value=FakeResponse(
                200,
                {
                    "ok": True,
                    "status": "sent",
                    "provider_message_ref":
                        "MSG-123",
                    "jid":
                        "MUST-NOT-ESCAPE",
                },
            ),
        ):
            result = bridge.send(
                "contact:ayse",
                "Merhaba",
                outreach_id=
                    "a" * 32,
            )

        self.assertEqual(
            result,
            {
                "ok": True,
                "status": "sent",
                "provider_message_ref":
                    "MSG-123",
            },
        )

        self.assertNotIn(
            "MUST-NOT-ESCAPE",
            repr(result),
        )

    def test_private_config_loader_whitelists_python_keys(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            path = (
                Path(temp)
                / "bridge.env"
            )

            path.write_text(
                "\n".join([
                    (
                        "EPIS_WHATSAPP_BRIDGE_URL="
                        "http://127.0.0.1:8766"
                    ),
                    (
                        "EPIS_WHATSAPP_BRIDGE_TOKEN="
                        + "z" * 48
                    ),
                    (
                        "EPIS_INTERNAL_EVENT_TOKEN="
                        "MUST_NOT_LOAD"
                    ),
                    (
                        "OPENAI_API_KEY="
                        "MUST_NOT_LOAD"
                    ),
                ]),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "EPIS_WHATSAPP_BRIDGE_CONFIG":
                        str(path),
                },
                clear=False,
            ):
                config = (
                    load_whatsapp_bridge_config()
                )

        self.assertEqual(
            set(config),
            {
                "EPIS_WHATSAPP_BRIDGE_URL",
                "EPIS_WHATSAPP_BRIDGE_TOKEN",
            },
        )

        self.assertNotIn(
            "EPIS_INTERNAL_EVENT_TOKEN",
            config,
        )

        self.assertNotIn(
            "OPENAI_API_KEY",
            config,
        )


if __name__ == "__main__":
    unittest.main()
