import json
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

from memory_vault import (
    LocalMemoryVault,
)

from agentic.whatsapp_outreach import (
    LocalWhatsappBridge,
)


class FakeCipher:
    def encrypt_str(
        self,
        value,
    ):
        if isinstance(
            value,
            str,
        ):
            raw = value
        else:
            raw = json.dumps(
                value,
                ensure_ascii=False,
            )

        return (
            "enc:"
            + raw[::-1]
        )

    def decrypt_str(
        self,
        value,
    ):
        if not value:
            return ""

        return value[4:][::-1]


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


class WhatsappContactEnrollmentTests(
    unittest.TestCase
):
    def setUp(
        self,
    ):
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

    def tearDown(
        self,
    ):
        self.temp.cleanup()

    def test_vault_binding_preserves_existing_metadata_and_aliases(
        self,
    ):
        self.vault.upsert_person(
            "Merve",
            aliases=[
                "Merv",
            ],
            metadata={
                "legacy_relation":
                    "friend",
                "custom":
                    {
                        "keep":
                            True,
                    },
            },
        )

        result = (
            self.vault
            .configure_person_whatsapp(
                "Merve",
                aliases=[
                    "Merve Hanım",
                ],
                contact_ref=
                    "contact:abc123",
                allowlisted=True,
            )
        )

        self.assertTrue(
            result[
                "allowlisted"
            ]
        )

        with (
            self.vault._lock,
            self.vault._connection()
            as conn,
        ):
            row = conn.execute(
                """
                SELECT
                    aliases_enc,
                    metadata_json
                FROM people
                WHERE id=?
                """,
                (
                    result[
                        "person_id"
                    ],
                ),
            ).fetchone()

        aliases = json.loads(
            self.vault._dec(
                row["aliases_enc"]
            )
        )

        metadata = json.loads(
            row["metadata_json"]
        )

        self.assertIn(
            "Merv",
            aliases,
        )

        self.assertIn(
            "Merve Hanım",
            aliases,
        )

        self.assertEqual(
            metadata[
                "legacy_relation"
            ],
            "friend",
        )

        self.assertTrue(
            metadata[
                "custom"
            ][
                "keep"
            ]
        )

        self.assertEqual(
            metadata[
                "whatsapp"
            ][
                "contact_ref"
            ],
            "contact:abc123",
        )

        self.assertTrue(
            metadata[
                "whatsapp"
            ][
                "allowlisted"
            ]
        )

    def test_enroll_contact_never_returns_jid(
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
                    "status":
                        "enrolled",
                    "contact_ref":
                        "contact:test",
                    "jid":
                        "905551112233@s.whatsapp.net",
                },
            ),
        ):
            result = (
                bridge.enroll_contact(
                    "contact:test",
                    "+90 555 111 22 33",
                )
            )

        self.assertEqual(
            result,
            {
                "ok": True,
                "status":
                    "enrolled",
                "contact_ref":
                    "contact:test",
            },
        )

        self.assertNotIn(
            "@s.whatsapp.net",
            repr(result),
        )

        self.assertNotIn(
            "905551112233",
            repr(result),
        )

    def test_remove_contact_is_safe_and_idempotent(
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
                    "status":
                        "already_absent",
                },
            ),
        ):
            result = (
                bridge.remove_contact(
                    "contact:test"
                )
            )

        self.assertTrue(
            result["ok"]
        )

        self.assertEqual(
            result["status"],
            "already_absent",
        )


if __name__ == "__main__":
    unittest.main()
