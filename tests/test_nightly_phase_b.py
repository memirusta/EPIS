from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Layer-2" / "src"
sys.path.insert(0, str(SRC))

from memory_vault import LocalMemoryVault


class FakeCipher:
    def encrypt_str(self, value: str) -> str:
        return "enc:" + value[::-1]

    def decrypt_str(self, value: str) -> str:
        if not value:
            return ""
        return value[4:][::-1]


class FakeTranscriptClient:
    def __init__(self, frozen, *, ack_fails=False):
        self.frozen = frozen
        self.ack_fails = ack_fails
        self.freeze_calls = 0
        self.acks = []

    def freeze(self, **kwargs):
        self.freeze_calls += 1
        return dict(self.frozen) if self.frozen else None

    def acknowledge_and_purge(self, *, day_id, input_hash):
        self.acks.append((day_id, input_hash))
        if self.ack_fails:
            raise RuntimeError("network_down")
        return {"ok": True, "purged": len(self.frozen.get("messages") or [])}


class FakePrivacy:
    def anonymize(self, text):
        return text, {}

    def deanonymize(self, text, mapping):
        return text


class FakeRouter:
    privacy = FakePrivacy()
    routing_table = {}


def load_nightly_module():
    """Load NC with isolated minimal router/crypto stubs for orchestration tests."""
    router_stub = types.ModuleType("router")
    router_stub.EpisRouter = FakeRouter
    crypto_stub = types.ModuleType("crypto_layer")
    crypto_stub.get_cipher = lambda: FakeCipher()
    crypto_stub.load_json_file = lambda path: {}

    old_router = sys.modules.get("router")
    old_crypto = sys.modules.get("crypto_layer")
    sys.modules["router"] = router_stub
    sys.modules["crypto_layer"] = crypto_stub
    try:
        spec = importlib.util.spec_from_file_location(
            "nightly_recalculation_phase_b_test",
            SRC / "nightly_recalculation.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if old_router is None:
            sys.modules.pop("router", None)
        else:
            sys.modules["router"] = old_router
        if old_crypto is None:
            sys.modules.pop("crypto_layer", None)
        else:
            sys.modules["crypto_layer"] = old_crypto


class NightlyPhaseBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = LocalMemoryVault(Path(self.temp.name) / "vault.sqlite3", cipher=FakeCipher())
        self.frozen = {
            "day_id": "2026-09-23",
            "status": "frozen",
            "input_hash": "c" * 64,
            "messages": [
                {"message_id": "m1", "role": "user", "text": "Nebula testini bitirdim", "created_at": "2026-09-23T10:00:00+00:00"},
                {"message_id": "m2", "role": "assistant", "text": "Tamam", "created_at": "2026-09-23T10:00:02+00:00"},
            ],
        }
        self.nr = load_nightly_module()

    def tearDown(self):
        self.temp.cleanup()

    def _engine(self, client):
        engine = self.nr.NightlyRecalculation(transcript_client=client, vault=self.vault)
        engine._stage1_summarize = lambda data: (
            "Günün özeti yeterince uzun; aktivite, etkileşim, enerji ve odak ayrıntıları var. " * 5,
            {"turns": 1},
        )
        analysis = {
            "date": "2026-09-23",
            "mood_estimate": "pozitif",
            "energy_level": "orta",
            "focus_quality": "orta",
            "stress_signal": "yok",
            "key_events": ["Nebula testi tamamlandı"],
            "habits": {"confirmed": [], "new_detected": [], "changed": [], "broken": []},
            "behavioral_insights": ["Teknik projede ilerleme var"],
            "anomalies": [],
            "epis_learnings": [],
            "tomorrow_context": "Nebula üzerinde devam",
            "weekly_contribution": "",
        }
        engine._stage2_analyze = lambda summary: (json.dumps(analysis, ensure_ascii=False), {"turns": 1})
        engine._stage3_epis_voice = lambda summary, analysis: ("İyi ilerleme var.", {"turns": 1, "chars": 17})
        engine._update_memory = lambda *args, **kwargs: None
        engine._update_habits = lambda *args, **kwargs: None
        engine._run_drift_check = lambda: "OK"
        engine._prepare_vault_sync = lambda: None
        engine._append_pending = lambda **kwargs: None
        engine._finalize = lambda: engine.report
        engine._handle_failure = lambda error: None
        return engine

    def test_cloud_purge_happens_only_after_local_commit(self):
        client = FakeTranscriptClient(self.frozen)
        engine = self._engine(client)
        report = engine.run()
        self.assertEqual(report["status"], "success")
        self.assertTrue(self.vault.is_run_committed("2026-09-23", "c" * 64))
        self.assertEqual(client.acks, [("2026-09-23", "c" * 64)])
        self.assertEqual(self.vault.run_state("2026-09-23", "c" * 64)["status"], "cloud_acked")

    def test_ack_failure_preserves_committed_run_for_retry(self):
        first_client = FakeTranscriptClient(self.frozen, ack_fails=True)
        first = self._engine(first_client)
        first_report = first.run()
        self.assertEqual(first_report["status"], "partial")
        self.assertEqual(self.vault.run_state("2026-09-23", "c" * 64)["status"], "committed")

        second_client = FakeTranscriptClient(self.frozen)
        second = self._engine(second_client)
        second._stage1_summarize = lambda data: (_ for _ in ()).throw(AssertionError("model rerun"))
        second_report = second.run()
        self.assertEqual(second_report["status"], "success")
        self.assertEqual(second_client.acks, [("2026-09-23", "c" * 64)])
        self.assertEqual(self.vault.run_state("2026-09-23", "c" * 64)["status"], "cloud_acked")

    def test_failure_before_vault_commit_never_acks_cloud(self):
        client = FakeTranscriptClient(self.frozen)
        engine = self._engine(client)
        engine._stage1_summarize = lambda data: (_ for _ in ()).throw(RuntimeError("model_boom"))
        report = engine.run()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(client.acks, [])
        self.assertFalse(self.vault.is_run_committed("2026-09-23", "c" * 64))

    def test_backlog_runner_drains_oldest_completed_days_in_one_wake(self):
        class BacklogClient:
            def pending_days(self, **kwargs):
                return [
                    {"day_id": "2026-09-20"},
                    {"day_id": "2026-09-21"},
                    {"day_id": "2026-09-22"},
                ]

        client = BacklogClient()
        engine = self.nr.NightlyRecalculation(transcript_client=client, vault=self.vault)
        seen = []

        class Worker:
            def __init__(self, day_id):
                self.day_id = day_id

            def run(self, sensor_data=None):
                seen.append((self.day_id, sensor_data))
                return {"date": self.day_id, "status": "success", "errors": []}

        engine._new_backlog_worker = lambda day_id, _client: Worker(day_id)
        report = engine.run_backlog(sensor_data={"biometrics": {"today": True}}, max_days=7)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["processed_days"], ["2026-09-20", "2026-09-21", "2026-09-22"])
        self.assertEqual(seen, [
            ("2026-09-20", None),
            ("2026-09-21", None),
            ("2026-09-22", None),
        ])

    def test_backlog_runner_stops_on_partial_ack_failure(self):
        class BacklogClient:
            def pending_days(self, **kwargs):
                return [{"day_id": "2026-09-20"}, {"day_id": "2026-09-21"}]

        client = BacklogClient()
        engine = self.nr.NightlyRecalculation(transcript_client=client, vault=self.vault)
        seen = []

        class Worker:
            def __init__(self, day_id):
                self.day_id = day_id

            def run(self, sensor_data=None):
                seen.append(self.day_id)
                return {
                    "date": self.day_id,
                    "status": "partial" if self.day_id == "2026-09-20" else "success",
                    "errors": ["network_down"] if self.day_id == "2026-09-20" else [],
                }

        engine._new_backlog_worker = lambda day_id, _client: Worker(day_id)
        report = engine.run_backlog(max_days=7)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(seen, ["2026-09-20"])
        self.assertEqual(report["processed_days"], [])


    def test_stage3_accepts_plain_text_luna_response_in_one_turn(self):
        class VoiceClient:
            def __init__(self):
                self.calls = []

            def infer(self, *, stage, prompt):
                self.calls.append({
                    "stage": stage,
                    "prompt": prompt,
                })
                return (
                    "Bugun teknik tarafta belirgin ilerleme var. "
                    "Yarin ayni odagi koruyup tek bir ana isi bitirmeye calis."
                )

        client = VoiceClient()
        engine = self.nr.NightlyRecalculation(
            transcript_client=client,
            vault=self.vault,
        )

        analysis = {
            "mood_estimate": "pozitif",
            "energy_level": "orta",
            "stress_signal": "dusuk",
            "behavioral_insights": [
                "Teknik projede istikrarli ilerleme var",
            ],
            "tomorrow_context": "Projeye devam",
            "epis_learnings": [],
        }

        message, log = engine._stage3_epis_voice(
            "Bugun proje uzerinde ilerleme kaydedildi.",
            analysis,
        )

        self.assertTrue(message.startswith("Bugun teknik tarafta"))
        self.assertEqual(log["turns"], 1)
        self.assertEqual(log["chars"], len(message))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["stage"], "voice")
        self.assertIn(
            "Yalnizca kullaniciya gidecek mesajin kendisini dondur",
            client.calls[0]["prompt"],
        )
        self.assertNotIn(
            '{"type":"direct"',
            client.calls[0]["prompt"],
        )

    def test_report_uses_provider_neutral_stage_names(self):
        client = FakeTranscriptClient(self.frozen)
        engine = self._engine(client)

        report = engine.run()
        stages = report["stages"]

        self.assertIn("stage1_summary", stages)
        self.assertIn("stage2_analysis", stages)
        self.assertIn("stage3_voice", stages)

        self.assertNotIn("stage1_gemini", stages)
        self.assertNotIn("stage2_claude", stages)
        self.assertNotIn("stage3_epis", stages)


if __name__ == "__main__":
    unittest.main()
