import sys
from pathlib import Path
import unittest


sys.path.insert(
    0,
    str(
        Path(__file__)
        .resolve()
        .parents[1]
        / "Layer-2"
        / "src"
    ),
)


from agentic.whatsapp_outreach import (
    LocalWhatsappBridge,
    WhatsappOutreachProvider,
)


class FakeResolver:
    def resolve_contact(
        self,
        contact_ref,
    ):
        return {
            "status":
                "resolved",

            "person_id":
                "person-merve",

            "canonical_name":
                "Merve",

            "provider_contact_ref":
                "contact:opaque",
        }


class FakeTracker:
    def __init__(
        self,
    ):
        self.begun = []
        self.failed = []

    def begin(
        self,
        **kwargs,
    ):
        self.begun.append(
            kwargs
        )

    def mark_failed(
        self,
        **kwargs,
    ):
        self.failed.append(
            kwargs
        )

    def mark_sent(
        self,
        **kwargs,
    ):
        raise AssertionError(
            "mark_sent must not run "
            "for rejected disclosure"
        )


class FakeDisclosureBridge:
    def send(
        self,
        provider_contact_ref,
        message,
        *,
        outreach_id,
    ):
        return {
            "ok": False,
            "error":
                "recipient_identity_context_missing",

            "retryable":
                True,

            "required": [
                "ai_identity",
                "emir_context",
            ],

            # Must never leak through provider.
            "jid":
                "SHOULD_NOT_LEAK",

            "phone":
                "SHOULD_NOT_LEAK",
        }


class WhatsappDisclosureTransportTests(
    unittest.TestCase
):
    def bridge_with_response(
        self,
        status,
        payload,
    ):
        bridge = object.__new__(
            LocalWhatsappBridge
        )

        bridge._json_request = (
            lambda *args, **kwargs:
                (
                    status,
                    dict(payload),
                )
        )

        return bridge

    def test_local_bridge_preserves_soft_guard_result(
        self,
    ):
        bridge = (
            self.bridge_with_response(
                409,
                {
                    "ok": False,
                    "error":
                        "recipient_identity_context_missing",

                    "retryable":
                        True,

                    "required": [
                        "ai_identity",
                        "emir_context",
                    ],

                    "jid":
                        "SECRET",

                    "phone":
                        "SECRET",
                },
            )
        )

        result = bridge.send(
            "contact:opaque",
            "Selam.",
            outreach_id=
                "outreach-test",
        )

        self.assertEqual(
            result,
            {
                "ok": False,
                "error":
                    "recipient_identity_context_missing",

                "retryable":
                    True,

                "required": [
                    "ai_identity",
                    "emir_context",
                ],
            },
        )

    def test_local_bridge_keeps_unknown_http_errors_opaque(
        self,
    ):
        bridge = (
            self.bridge_with_response(
                500,
                {
                    "ok": False,
                    "error":
                        "private_internal_failure",

                    "secret":
                        "do-not-leak",
                },
            )
        )

        result = bridge.send(
            "contact:opaque",
            "hello",
            outreach_id=
                "outreach-test",
        )

        self.assertEqual(
            result,
            {
                "ok": False,
                "error":
                    "whatsapp_bridge_http_500",
            },
        )

    def test_hard_deception_is_non_retryable(
        self,
    ):
        bridge = (
            self.bridge_with_response(
                422,
                {
                    "ok": False,
                    "error":
                        "recipient_identity_deception",

                    "retryable":
                        False,
                },
            )
        )

        result = bridge.send(
            "contact:opaque",
            "Ben Emir'im.",
            outreach_id=
                "outreach-test",
        )

        self.assertEqual(
            result,
            {
                "ok": False,
                "error":
                    "recipient_identity_deception",

                "retryable":
                    False,
            },
        )

    def test_provider_preserves_retry_contract_only(
        self,
    ):
        provider = object.__new__(
            WhatsappOutreachProvider
        )

        tracker = FakeTracker()

        provider.resolver = (
            FakeResolver()
        )

        provider.tracker = (
            tracker
        )

        provider.bridge = (
            FakeDisclosureBridge()
        )

        result = provider.execute(
            "whatsapp.send_to_contact",
            {
                "contact_ref":
                    "Merve",

                "message":
                    "Selam.",
            },
        )

        self.assertEqual(
            result["ok"],
            False,
        )

        self.assertEqual(
            result["error"],
            "recipient_identity_context_missing",
        )

        self.assertEqual(
            result["retryable"],
            True,
        )

        self.assertEqual(
            result["required"],
            [
                "ai_identity",
                "emir_context",
            ],
        )

        self.assertNotIn(
            "jid",
            result,
        )

        self.assertNotIn(
            "phone",
            result,
        )

        self.assertEqual(
            len(
                tracker.begun
            ),
            1,
        )

        self.assertEqual(
            len(
                tracker.failed
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
