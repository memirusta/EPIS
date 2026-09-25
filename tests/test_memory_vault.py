from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from memory_vault import LocalMemoryVault


class FakeCipher:
    def encrypt_str(self, value: str) -> str:
        return "enc:" + value[::-1]

    def decrypt_str(self, value: str) -> str:
        if not value:
            return ""
        if not value.startswith("enc:"):
            raise ValueError("unexpected plaintext")
        return value[4:][::-1]


class MemoryVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "vault.sqlite3"
        self.vault = LocalMemoryVault(self.path, cipher=FakeCipher())

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_contains_memory_v1_tables(self):
        with closing(sqlite3.connect(self.path)) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for required in {
            "memories",
            "people",
            "relationships",
            "episodes",
            "habits",
            "memory_evidence",
            "nightly_runs",
            "memory_changes",
        }:
            self.assertIn(required, names)

    def test_nightly_commit_is_idempotent_and_field_encrypted(self):
        messages = [
            {"message_id": "m1", "role": "user", "text": "Ben mor ejderhaları seviyorum"},
            {"message_id": "m2", "role": "assistant", "text": "Not ettim"},
        ]
        analysis = {
            "key_events": ["Mor ejderha testi yapıldı"],
            "behavioral_insights": ["Kullanıcı kısa ve doğrudan yanıt seviyor"],
            "epis_learnings": [],
            "tomorrow_context": "Projeye devam edilecek",
            "habits": {"confirmed": ["Sabah erken kalkmak"], "new_detected": [], "changed": [], "broken": []},
        }
        result = self.vault.apply_nightly_result(
            day_id="2026-09-23",
            input_hash="a" * 64,
            run_id="run-1",
            summary="Günün özeti",
            analysis=analysis,
            transcript_messages=messages,
        )
        self.assertFalse(result["already_committed"])
        again = self.vault.apply_nightly_result(
            day_id="2026-09-23",
            input_hash="a" * 64,
            run_id="run-2",
            summary="başka özet",
            analysis=analysis,
            transcript_messages=messages,
        )
        self.assertTrue(again["already_committed"])
        self.assertTrue(self.vault.is_run_committed("2026-09-23", "a" * 64))

        with closing(sqlite3.connect(self.path)) as conn:
            blob = "\n".join(str(row[0]) for row in conn.execute("SELECT content_enc FROM memories"))
            count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        self.assertNotIn("Mor ejderha", blob)
        self.assertEqual(count, 1)

    def test_relevant_search_returns_distilled_not_raw_transcript(self):
        self.vault.apply_nightly_result(
            day_id="2026-09-23",
            input_hash="b" * 64,
            run_id="run",
            summary="Özet",
            analysis={
                "key_events": ["Nebula tarayıcı performans testi tamamlandı"],
                "behavioral_insights": ["Satranç turnuvasına ilgi var"],
                "epis_learnings": [],
                "tomorrow_context": "",
                "habits": {},
            },
            transcript_messages=[
                {"message_id": "raw1", "role": "user", "text": "çok gizli ham mesaj"},
            ],
        )
        rows = self.vault.search_relevant("Nebula performans", limit=4)
        self.assertTrue(rows)
        self.assertIn("Nebula", rows[0]["content"])
        self.assertTrue(all("çok gizli ham mesaj" not in row["content"] for row in rows))

    def test_identity_self_is_not_a_vault_write_target(self):
        with closing(sqlite3.connect(self.path)) as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
        self.assertIn("user_authoritative", columns)
        # There is deliberately no identity_self table: that file remains user-owned.
        with closing(sqlite3.connect(self.path)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("identity_self", names)


if __name__ == "__main__":
    unittest.main()
