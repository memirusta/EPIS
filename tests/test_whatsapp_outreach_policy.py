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

from agentic.authorization import (
    SessionAuthorizationPolicy,
)
from agentic.capability_broker import (
    CapabilityBroker,
    default_capability_specs,
)


class FakeWhatsappProvider:
    provider_id = "whatsapp-fake"
    display_name = "Fake WhatsApp"
    priority = 1

    def available(self):
        return True

    def capabilities(self):
        return {
            "whatsapp.send_to_contact",
        }

    def execute(
        self,
        capability,
        arguments,
    ):
        return {
            "ok": True,
            "outreach_id": "fake-outreach",
        }

    def close(self):
        return None


class WhatsappOutreachPolicyTests(
    unittest.TestCase
):
    def setUp(self):
        self.policy = (
            SessionAuthorizationPolicy()
        )

    def test_explicit_named_outreach_is_authorized(
        self,
    ):
        result = self.policy.evaluate(
            "whatsapp.send_to_contact",
            {
                "contact_ref": "Ayse",
                "message": "Bunu sor.",
            },
            "Ayşe'ye WhatsApp'tan bunu sorar mısın?",
        )

        self.assertTrue(
            result.authorized
        )
        self.assertEqual(
            result.source,
            "explicit_current_turn",
        )
        self.assertEqual(
            result.category,
            "external_communication",
        )

    def test_named_ask_without_whatsapp_is_still_explicit(
        self,
    ):
        result = self.policy.evaluate(
            "whatsapp.send_to_contact",
            {
                "contact_ref": "Ayse",
                "message": "Bunu sor.",
            },
            "Ayşe'ye bunu sorar mısın?",
        )

        self.assertTrue(
            result.authorized
        )

    def test_assistant_proposed_outreach_is_blocked(
        self,
    ):
        result = self.policy.evaluate(
            "whatsapp.send_to_contact",
            {
                "contact_ref": "Ayse",
                "message": "Bunu sor.",
            },
            "Sence Ayşe bu konuda ne düşünür?",
        )

        self.assertFalse(
            result.authorized
        )
        self.assertEqual(
            result.source,
            "assistant_proposed",
        )

    def test_session_grant_cannot_auto_authorize_whatsapp(
        self,
    ):
        self.policy.grant(
            "external_communication"
        )

        result = self.policy.evaluate(
            "whatsapp.send_to_contact",
            {
                "contact_ref": "Ayse",
                "message": "Bunu sor.",
            },
            "Ayşe bu konuda ne düşünür acaba?",
        )

        self.assertFalse(
            result.authorized
        )
        self.assertNotEqual(
            result.source,
            "session_category_grant",
        )

    def test_current_turn_denial_wins(
        self,
    ):
        self.policy.grant(
            "external_communication"
        )

        result = self.policy.evaluate(
            "whatsapp.send_to_contact",
            {
                "contact_ref": "Ayse",
                "message": "Bunu sor.",
            },
            "Ayşe'ye sor ama gönderme.",
        )

        self.assertFalse(
            result.authorized
        )
        self.assertTrue(
            result.denied
        )

    def test_ask_denial_wins(
        self,
    ):
        result = self.policy.evaluate(
            "whatsapp.send_to_contact",
            {
                "contact_ref": "Ayse",
                "message": "Bunu sor.",
            },
            "Ayşe'ye bunu sorma.",
        )

        self.assertFalse(
            result.authorized
        )
        self.assertTrue(
            result.denied
        )

    def test_contract_hidden_without_provider(
        self,
    ):
        spec = next(
            item
            for item
            in default_capability_specs()
            if item.name
            == "whatsapp_send_to_contact"
        )

        self.assertEqual(
            spec.capability,
            "whatsapp.send_to_contact",
        )
        self.assertEqual(
            spec.risk_class,
            "yellow",
        )
        self.assertTrue(
            spec.confirmation_required
        )

        broker = CapabilityBroker()
        broker.register_spec(spec)

        self.assertEqual(
            broker.openai_schemas(),
            [],
        )

    def test_contract_live_only_with_provider(
        self,
    ):
        spec = next(
            item
            for item
            in default_capability_specs()
            if item.name
            == "whatsapp_send_to_contact"
        )

        broker = CapabilityBroker()
        broker.register_spec(spec)
        broker.register_provider(
            FakeWhatsappProvider()
        )

        names = {
            item["function"]["name"]
            for item
            in broker.openai_schemas()
        }

        self.assertIn(
            "whatsapp_send_to_contact",
            names,
        )


if __name__ == "__main__":
    unittest.main()
