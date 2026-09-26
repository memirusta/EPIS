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

from agentic.whatsapp_outreach import (
    FakeWhatsappBridge,
    VaultContactResolver,
    VaultOutreachState,
    WhatsappOutreachProvider,
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


class WhatsappOutreachTests(
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

    def add_person(
        self,
        name,
        *,
        aliases=None,
        allowlisted=True,
        provider_ref=None,
    ):
        return self.vault.upsert_person(
            name,
            aliases=aliases or [],
            metadata={
                "whatsapp": {
                    "allowlisted":
                        allowlisted,

                    "contact_ref":
                        (
                            provider_ref
                            or
                            "contact:"
                            + name.casefold()
                        ),
                }
            },
        )

    def provider(self):
        bridge = (
            FakeWhatsappBridge()
        )

        provider = (
            WhatsappOutreachProvider(
                VaultContactResolver(
                    self.vault
                ),
                bridge,
                VaultOutreachState(
                    self.vault
                ),
            )
        )

        return (
            provider,
            bridge,
        )

    def test_resolves_canonical_name_and_alias(
        self,
    ):
        person_id = self.add_person(
            "Ayşe",
            aliases=[
                "Ayse",
                "Ays",
            ],
            provider_ref=
                "contact:ayse-01",
        )

        resolved = (
            self.vault
            .resolve_contact_ref(
                "Ayse"
            )
        )

        self.assertEqual(
            resolved["status"],
            "resolved",
        )

        self.assertEqual(
            resolved["person_id"],
            person_id,
        )

        self.assertEqual(
            resolved[
                "provider_contact_ref"
            ],
            "contact:ayse-01",
        )

    def test_ambiguous_alias_fails_closed(
        self,
    ):
        self.add_person(
            "Mert A",
            aliases=["Mert"],
            provider_ref=
                "contact:mert-a",
        )

        self.add_person(
            "Mert B",
            aliases=["Mert"],
            provider_ref=
                "contact:mert-b",
        )

        provider, bridge = (
            self.provider()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Mert",

                "message":
                    "Selam",
            },
        )

        self.assertFalse(
            result["ok"]
        )

        self.assertEqual(
            result["error"],
            "contact_ambiguous",
        )

        self.assertEqual(
            bridge.calls,
            [],
        )

    def test_non_allowlisted_contact_is_blocked(
        self,
    ):
        self.add_person(
            "Deniz",
            allowlisted=False,
            provider_ref=
                "contact:deniz",
        )

        provider, bridge = (
            self.provider()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Deniz",

                "message":
                    "Selam",
            },
        )

        self.assertFalse(
            result["ok"]
        )

        self.assertEqual(
            result["error"],
            "contact_not_allowlisted",
        )

        self.assertEqual(
            bridge.calls,
            [],
        )

    def test_provider_hides_transport_identity(
        self,
    ):
        self.add_person(
            "Ayşe",
            aliases=["Ayse"],
            provider_ref=
                "contact:private-opaque-ref",
        )

        provider, bridge = (
            self.provider()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Ayse",

                "message":
                    "Bugün müsait misin?",
            },
        )

        self.assertTrue(
            result["ok"]
        )

        self.assertTrue(
            result["outreach_id"]
        )

        self.assertEqual(
            len(bridge.calls),
            1,
        )

        self.assertEqual(
            bridge.calls[0][
                "provider_contact_ref"
            ],
            "contact:private-opaque-ref",
        )

        # Internal transport ref must never flow back toward Luna.
        self.assertNotIn(
            "private-opaque-ref",
            repr(result),
        )

    def test_send_creates_durable_correlation_state(
        self,
    ):
        self.add_person(
            "Ayşe",
            aliases=["Ayse"],
            provider_ref=
                "contact:private-ayse",
        )

        provider, bridge = (
            self.provider()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
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

        self.assertTrue(
            result[
                "reply_tracking"
            ]
        )

        self.assertNotIn(
            "provider_message_ref",
            result,
        )

        provider_message_ref = (
            bridge.calls[0][
                "outreach_id"
            ]
        )

        # Fake bridge convention is deterministic for the test,
        # while the outward provider result stays opaque.
        reply_ref = (
            "fake-message:"
            + provider_message_ref
        )

        state = VaultOutreachState(
            self.vault
        )

        accepted = state.accept_reply(
            provider_message_ref=
                reply_ref,

            provider_contact_ref=
                "contact:private-ayse",

            content=
                "İyiyim, teşekkürler.",
        )

        self.assertEqual(
            accepted["status"],
            "accepted",
        )

    def test_matched_reply_becomes_external_perspective(
        self,
    ):
        self.add_person(
            "Ayşe",
            aliases=["Ayse"],
            provider_ref=
                "contact:ayse-reply",
        )

        provider, bridge = (
            self.provider()
        )

        sent = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Ayse",
                "message":
                    "Emir hakkında ne düşünüyorsun?",
            },
        )

        self.assertTrue(
            sent["ok"]
        )

        reply_ref = (
            "fake-message:"
            + sent["outreach_id"]
        )

        state = VaultOutreachState(
            self.vault
        )

        accepted = state.accept_reply(
            provider_message_ref=
                reply_ref,

            provider_contact_ref=
                "contact:ayse-reply",

            content=
                "Emir bazen çok detaycı davranıyor.",

            confidence=
                0.95,
        )

        self.assertEqual(
            accepted["status"],
            "accepted",
        )

        # Still capped by the Vault's third-party evidence rule.
        self.assertEqual(
            accepted[
                "confidence"
            ],
            0.75,
        )

        memory = next(
            item
            for item
            in self.vault
            .recent_memories(
                limit=20
            )
            if item["id"]
            == accepted[
                "memory_id"
            ]
        )

        self.assertEqual(
            memory["kind"],
            "external_perspective",
        )

        self.assertEqual(
            memory["provenance"],
            "external_perspective",
        )

        self.assertFalse(
            memory[
                "user_authoritative"
            ]
        )

    def test_reply_sender_must_match_original_contact(
        self,
    ):
        self.add_person(
            "Ayşe",
            aliases=["Ayse"],
            provider_ref=
                "contact:ayse-safe",
        )

        provider, _ = (
            self.provider()
        )

        sent = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Ayse",
                "message":
                    "Bir şey soracağım.",
            },
        )

        state = VaultOutreachState(
            self.vault
        )

        rejected = state.accept_reply(
            provider_message_ref=(
                "fake-message:"
                + sent[
                    "outreach_id"
                ]
            ),

            provider_contact_ref=
                "contact:someone-else",

            content=
                "Sahte cevap",
        )

        self.assertEqual(
            rejected["status"],
            "sender_mismatch",
        )

        memories = [
            item
            for item
            in self.vault
            .recent_memories(
                limit=20
            )
            if item["kind"]
            == "external_perspective"
        ]

        self.assertEqual(
            memories,
            [],
        )

    def test_duplicate_reply_is_idempotent(
        self,
    ):
        self.add_person(
            "Ayşe",
            aliases=["Ayse"],
            provider_ref=
                "contact:ayse-idempotent",
        )

        provider, _ = (
            self.provider()
        )

        sent = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Ayse",
                "message":
                    "Bunu cevaplar mısın?",
            },
        )

        state = VaultOutreachState(
            self.vault
        )

        kwargs = {
            "provider_message_ref":
                (
                    "fake-message:"
                    + sent[
                        "outreach_id"
                    ]
                ),

            "provider_contact_ref":
                "contact:ayse-idempotent",

            "content":
                "Aynı webhook cevabı.",
        }

        first = state.accept_reply(
            **kwargs
        )

        second = state.accept_reply(
            **kwargs
        )

        self.assertEqual(
            first["status"],
            "accepted",
        )

        self.assertEqual(
            second["status"],
            "duplicate",
        )

        self.assertEqual(
            first["memory_id"],
            second["memory_id"],
        )

    def test_unknown_reply_reference_is_not_ingested(
        self,
    ):
        state = VaultOutreachState(
            self.vault
        )

        result = state.accept_reply(
            provider_message_ref=
                "unknown-message",

            provider_contact_ref=
                "contact:anyone",

            content=
                "Bu kaydedilmemeli.",
        )

        self.assertEqual(
            result["status"],
            "unmatched",
        )

        self.assertEqual(
            [
                item
                for item
                in self.vault
                .recent_memories(
                    limit=20
                )
                if item["kind"]
                == "external_perspective"
            ],
            [],
        )

    def test_unquoted_reply_uses_single_recent_pending_outreach(
        self,
    ):
        self.add_person(
            "Merve",
            provider_ref=
                "contact:merve",
        )

        provider, _ = (
            self.provider()
        )

        sent = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Merve",
                "message":
                    "Bir şey soracağım.",
            },
        )

        state = VaultOutreachState(
            self.vault
        )

        result = (
            state.accept_reply_by_contact(
                incoming_message_ref=
                    "incoming-1",
                provider_contact_ref=
                    "contact:merve",
                content=
                    "Bence gayet iyi.",
            )
        )

        self.assertEqual(
            result["status"],
            "accepted",
        )

        self.assertEqual(
            result["outreach_id"],
            sent["outreach_id"],
        )

    def test_unquoted_reply_is_ambiguous_with_two_pending_outreaches(
        self,
    ):
        self.add_person(
            "Merve",
            provider_ref=
                "contact:merve-amb",
        )

        provider, _ = (
            self.provider()
        )

        provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Merve",
                "message":
                    "Birinci soru",
            },
        )

        provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Merve",
                "message":
                    "İkinci soru",
            },
        )

        state = VaultOutreachState(
            self.vault
        )

        result = (
            state.accept_reply_by_contact(
                incoming_message_ref=
                    "incoming-amb",
                provider_contact_ref=
                    "contact:merve-amb",
                content=
                    "Evet.",
            )
        )

        self.assertEqual(
            result["status"],
            "ambiguous",
        )

        self.assertEqual(
            result["candidate_count"],
            2,
        )

    def test_unquoted_reply_is_idempotent(
        self,
    ):
        self.add_person(
            "Merve",
            provider_ref=
                "contact:merve-idem",
        )

        provider, _ = (
            self.provider()
        )

        provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Merve",
                "message":
                    "Nasılsın?",
            },
        )

        state = VaultOutreachState(
            self.vault
        )

        kwargs = {
            "incoming_message_ref":
                "incoming-same",
            "provider_contact_ref":
                "contact:merve-idem",
            "content":
                "İyiyim.",
        }

        first = (
            state.accept_reply_by_contact(
                **kwargs
            )
        )

        second = (
            state.accept_reply_by_contact(
                **kwargs
            )
        )

        self.assertEqual(
            first["status"],
            "accepted",
        )

        self.assertEqual(
            second["status"],
            "duplicate",
        )

        self.assertEqual(
            first["memory_id"],
            second["memory_id"],
        )

    def test_external_perspective_confidence_is_capped(
        self,
    ):
        person_id = (
            self.add_person(
                "Ayşe",
            )
        )

        stored = (
            self.vault
            .record_external_perspective(
                person_id=
                    person_id,

                content=
                    "Emir bazen çok detaycı davranıyor.",

                confidence=
                    0.99,

                outreach_id=
                    "outreach-test",
            )
        )

        self.assertEqual(
            stored["kind"],
            "external_perspective",
        )

        self.assertEqual(
            stored["confidence"],
            0.75,
        )

        self.assertEqual(
            stored["provenance"],
            "external_perspective",
        )

        self.assertFalse(
            stored[
                "user_authoritative"
            ]
        )

        memory = next(
            item
            for item
            in self.vault
            .recent_memories(
                limit=10
            )
            if item["id"]
            == stored["memory_id"]
        )

        self.assertEqual(
            memory["kind"],
            "external_perspective",
        )

        self.assertFalse(
            memory[
                "user_authoritative"
            ]
        )

        self.assertNotEqual(
            memory["subject"],
            "identity_self",
        )


if __name__ == "__main__":
    unittest.main()
