import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import (
    AsyncMock,
    patch,
)

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

os.environ.setdefault(
    "EPIS_DEPLOYMENT",
    "local",
)

server_app = importlib.import_module(
    "server.app"
)

from fastapi import HTTPException

from agentic.whatsapp_outreach import (
    WHATSAPP_DEVICE_REPLY_CAPABILITY,
)


class FakeDevice:
    def __init__(
        self,
        capabilities,
        *,
        online=True,
    ):
        self.device_id = (
            "trusted-windows"
        )

        self.display_name = (
            "Trusted Windows"
        )

        self.platform = (
            "windows"
        )

        self.capabilities = set(
            capabilities
        )

        self.online = online


class FakeTransport:
    def __init__(
        self,
        *,
        result=None,
        capabilities=None,
    ):
        self.device = FakeDevice(
            capabilities
            or {
                WHATSAPP_DEVICE_REPLY_CAPABILITY,
            }
        )

        self.result = (
            result
            if result is not None
            else {
                "ok": True,
                "status":
                    "accepted",
                "outreach_id":
                    "outreach-safe",
                "person_id":
                    "PRIVATE_PERSON",
                "memory_id":
                    "PRIVATE_MEMORY",
            }
        )

        self.calls = []

    def refresh(
        self,
    ):
        return (
            self.device.online
        )

    def execute(
        self,
        capability,
        arguments,
        *,
        confirmed=False,
        request_id=None,
    ):
        self.calls.append({
            "capability":
                capability,

            "arguments":
                dict(arguments),

            "confirmed":
                confirmed,

            "request_id":
                request_id,
        })

        return dict(
            self.result
        )


class FakeCore:
    def __init__(
        self,
        transports,
    ):
        self.transports = {
            str(index):
                transport

            for index, transport
            in enumerate(
                transports
            )
        }

        self.local_agent = object()


class FakeRequest:
    def __init__(
        self,
        payload,
    ):
        self.payload = payload

    async def json(
        self,
    ):
        return self.payload


class BrokenJsonRequest:
    async def json(
        self,
    ):
        raise ValueError(
            "broken"
        )


class WhatsappOutreachIngressTests(
    unittest.TestCase
):
    def payload(
        self,
    ):
        return {
            "provider_message_ref":
                "msg-private-123",

            "incoming_message_ref":
                "incoming-private-789",

            "provider_contact_ref":
                "contact-private-456",

            "content":
                "Üçüncü kişinin cevabı.",

            "confidence":
                0.9,
        }

    def test_routes_reply_to_private_device_capability(
        self,
    ):
        transport = (
            FakeTransport()
        )

        core = FakeCore([
            transport
        ])

        with patch.object(
            server_app,
            "get_core",
            return_value=core,
        ):
            result = asyncio.run(
                server_app
                ._route_whatsapp_outreach_reply(
                    self.payload()
                )
            )

        self.assertEqual(
            result,
            {
                "ok": True,
                "status":
                    "accepted",
                "outreach_id":
                    "outreach-safe",
            },
        )

        self.assertEqual(
            len(transport.calls),
            1,
        )

        call = (
            transport.calls[0]
        )

        self.assertEqual(
            call["capability"],
            WHATSAPP_DEVICE_REPLY_CAPABILITY,
        )

        self.assertTrue(
            call["confirmed"]
        )

        self.assertRegex(
            call["request_id"],
            r"^wa-reply-[0-9a-f]{40}$",
        )

    def test_safe_response_never_echoes_private_reply_material(
        self,
    ):
        transport = (
            FakeTransport()
        )

        core = FakeCore([
            transport
        ])

        payload = self.payload()

        with patch.object(
            server_app,
            "get_core",
            return_value=core,
        ):
            result = asyncio.run(
                server_app
                ._route_whatsapp_outreach_reply(
                    payload
                )
            )

        rendered = repr(
            result
        )

        self.assertNotIn(
            payload[
                "provider_message_ref"
            ],
            rendered,
        )

        self.assertNotIn(
            payload[
                "provider_contact_ref"
            ],
            rendered,
        )

        self.assertNotIn(
            payload["content"],
            rendered,
        )

        self.assertNotIn(
            "PRIVATE_PERSON",
            rendered,
        )

        self.assertNotIn(
            "PRIVATE_MEMORY",
            rendered,
        )

    def test_retry_uses_same_device_command_id(
        self,
    ):
        transport = (
            FakeTransport()
        )

        core = FakeCore([
            transport
        ])

        payload = self.payload()

        with patch.object(
            server_app,
            "get_core",
            return_value=core,
        ):
            asyncio.run(
                server_app
                ._route_whatsapp_outreach_reply(
                    payload
                )
            )

            asyncio.run(
                server_app
                ._route_whatsapp_outreach_reply(
                    payload
                )
            )

        self.assertEqual(
            transport.calls[0][
                "request_id"
            ],
            transport.calls[1][
                "request_id"
            ],
        )

    def test_no_device_returns_503(
        self,
    ):
        core = FakeCore([])

        with patch.object(
            server_app,
            "get_core",
            return_value=core,
        ):
            with self.assertRaises(
                HTTPException
            ) as context:
                asyncio.run(
                    server_app
                    ._route_whatsapp_outreach_reply(
                        self.payload()
                    )
                )

        self.assertEqual(
            context.exception.status_code,
            503,
        )

    def test_multiple_devices_fail_closed(
        self,
    ):
        core = FakeCore([
            FakeTransport(),
            FakeTransport(),
        ])

        with patch.object(
            server_app,
            "get_core",
            return_value=core,
        ):
            with self.assertRaises(
                HTTPException
            ) as context:
                asyncio.run(
                    server_app
                    ._route_whatsapp_outreach_reply(
                        self.payload()
                    )
                )

        self.assertEqual(
            context.exception.status_code,
            409,
        )

    def test_invalid_payload_never_reaches_device(
        self,
    ):
        transport = (
            FakeTransport()
        )

        core = FakeCore([
            transport
        ])

        payload = self.payload()
        payload["content"] = ""

        with patch.object(
            server_app,
            "get_core",
            return_value=core,
        ):
            with self.assertRaises(
                HTTPException
            ) as context:
                asyncio.run(
                    server_app
                    ._route_whatsapp_outreach_reply(
                        payload
                    )
                )

        self.assertEqual(
            context.exception.status_code,
            400,
        )

        self.assertEqual(
            transport.calls,
            [],
        )

    def test_endpoint_uses_internal_auth_and_not_normal_whatsapp_turn(
        self,
    ):
        transport = (
            FakeTransport()
        )

        core = FakeCore([
            transport
        ])

        normal_turn = AsyncMock(
            side_effect=AssertionError(
                "normal whatsapp turn must not run"
            )
        )

        with (
            patch.object(
                server_app,
                "get_core",
                return_value=core,
            ),
            patch.object(
                server_app,
                "_require_internal_event",
            ) as require_auth,
            patch.object(
                server_app,
                "_whatsapp_turn",
                normal_turn,
            ),
        ):
            response = asyncio.run(
                server_app
                .whatsapp_outreach_reply(
                    FakeRequest(
                        self.payload()
                    )
                )
            )

        require_auth.assert_called_once()

        normal_turn.assert_not_called()

        body = json.loads(
            response.body.decode(
                "utf-8"
            )
        )

        self.assertEqual(
            body["status"],
            "accepted",
        )

    def test_invalid_json_is_400(
        self,
    ):
        with patch.object(
            server_app,
            "_require_internal_event",
        ):
            with self.assertRaises(
                HTTPException
            ) as context:
                asyncio.run(
                    server_app
                    .whatsapp_outreach_reply(
                        BrokenJsonRequest()
                    )
                )

        self.assertEqual(
            context.exception.status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
