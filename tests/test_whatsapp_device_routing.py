import sys
from pathlib import Path
import tempfile
import unittest

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

from memory_vault import (
    LocalMemoryVault,
)

from agentic.tools import (
    ToolRegistry,
)

from agentic.whatsapp_outreach import (
    CoreWhatsappOutreachProvider,
    FakeWhatsappBridge,
    WHATSAPP_DEVICE_REPLY_CAPABILITY,
    WHATSAPP_DEVICE_SEND_CAPABILITY,
    WhatsappDeviceController,
    register_whatsapp_device_tools,
)


class FakeCipher:
    def encrypt_str(
        self,
        value: str,
    ) -> str:
        return (
            "enc:"
            + value[::-1]
        )

    def decrypt_str(
        self,
        value: str,
    ) -> str:
        if not value:
            return ""

        return value[4:][::-1]


class FakeDevice:
    def __init__(
        self,
        capabilities,
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

        self.online = True


class FakeTransport:
    def __init__(
        self,
        capabilities,
        result=None,
    ):
        self.device = FakeDevice(
            capabilities
        )

        self.result = (
            result
            if result is not None
            else {
                "ok": True,
                "status": "sent",
                "outreach_id":
                    "safe-outreach",
                "person_id":
                    "person-1",
                "contact_name":
                    "Ayşe",
                "reply_tracking":
                    True,
                "provider_message_ref":
                    "MUST_NOT_ESCAPE",
            }
        )

        self.calls = []

    def refresh(self):
        return self.device.online

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

        # CoreWhatsappOutreachProvider intentionally excludes this
        # in-process transport.
        self.local_agent = object()


class WhatsappDeviceRoutingTests(
    unittest.TestCase
):
    def setUp(self):
        self.temp = (
            tempfile
            .TemporaryDirectory()
        )

        self.vault = (
            LocalMemoryVault(
                Path(
                    self.temp.name
                )
                / "vault.sqlite3",
                cipher=FakeCipher(),
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_device_contracts_are_hidden_from_model(
        self,
    ):
        registry = ToolRegistry()

        controller = (
            WhatsappDeviceController(
                self.vault,
                FakeWhatsappBridge(),
            )
        )

        register_whatsapp_device_tools(
            registry,
            controller,
        )

        capabilities = (
            registry.capabilities()
        )

        self.assertIn(
            WHATSAPP_DEVICE_SEND_CAPABILITY,
            capabilities,
        )

        self.assertIn(
            WHATSAPP_DEVICE_REPLY_CAPABILITY,
            capabilities,
        )

        names = {
            item["function"]["name"]
            for item
            in registry.openai_schemas()
        }

        self.assertNotIn(
            "whatsapp_outreach_device_send",
            names,
        )

        self.assertNotIn(
            "whatsapp_outreach_device_accept_reply",
            names,
        )

    def test_device_send_uses_private_vault_and_fake_bridge(
        self,
    ):
        self.vault.upsert_person(
            "Ayşe",
            aliases=["Ayse"],
            metadata={
                "whatsapp": {
                    "allowlisted": True,
                    "contact_ref":
                        "contact:opaque-ayse",
                }
            },
        )

        registry = ToolRegistry()

        bridge = (
            FakeWhatsappBridge()
        )

        register_whatsapp_device_tools(
            registry,
            WhatsappDeviceController(
                self.vault,
                bridge,
            ),
        )

        entry = registry.for_capability(
            WHATSAPP_DEVICE_SEND_CAPABILITY
        )

        self.assertIsNotNone(
            entry
        )

        spec, _ = entry

        result = registry.dispatch(
            spec.name,
            {
                "contact_ref":
                    "Ayse",
                "message":
                    "Nasılsın?",
            },
        )

        self.assertTrue(
            result["ok"]
        )

        self.assertNotIn(
            "provider_message_ref",
            result,
        )

        self.assertNotIn(
            "opaque-ayse",
            repr(result),
        )

        self.assertEqual(
            len(bridge.calls),
            1,
        )

    def test_cloud_provider_routes_only_to_one_trusted_device(
        self,
    ):
        transport = FakeTransport({
            WHATSAPP_DEVICE_SEND_CAPABILITY,
        })

        provider = (
            CoreWhatsappOutreachProvider(
                FakeCore([
                    transport
                ])
            )
        )

        self.assertTrue(
            provider.available()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Ayse",
                "message":
                    "Selam",
            },
        )

        self.assertTrue(
            result["ok"]
        )

        self.assertEqual(
            transport.calls[0][
                "capability"
            ],
            WHATSAPP_DEVICE_SEND_CAPABILITY,
        )

        self.assertTrue(
            transport.calls[0][
                "confirmed"
            ]
        )

        self.assertNotIn(
            "provider_message_ref",
            result,
        )

        self.assertNotIn(
            "MUST_NOT_ESCAPE",
            repr(result),
        )

    def test_multiple_whatsapp_devices_fail_closed(
        self,
    ):
        first = FakeTransport({
            WHATSAPP_DEVICE_SEND_CAPABILITY,
        })

        second = FakeTransport({
            WHATSAPP_DEVICE_SEND_CAPABILITY,
        })

        provider = (
            CoreWhatsappOutreachProvider(
                FakeCore([
                    first,
                    second,
                ])
            )
        )

        self.assertFalse(
            provider.available()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Ayse",
                "message":
                    "Selam",
            },
        )

        self.assertFalse(
            result["ok"]
        )

        self.assertEqual(
            result["error"],
            "whatsapp_device_ambiguous",
        )

        self.assertEqual(
            first.calls,
            [],
        )

        self.assertEqual(
            second.calls,
            [],
        )


if __name__ == "__main__":
    unittest.main()
