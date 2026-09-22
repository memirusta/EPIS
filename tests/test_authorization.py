import sys
from pathlib import Path
import unittest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"),
)

from agentic.authorization import SessionAuthorizationPolicy


class SessionAuthorizationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SessionAuthorizationPolicy()

    def test_explicit_send_is_authorized_without_second_prompt(self):
        result = self.policy.evaluate(
            "computer.execute",
            {"goal": "Send the currently drafted message and verify it was sent."},
            "Mesajı da gönderir misin?",
        )
        self.assertTrue(result.authorized)
        self.assertEqual(result.category, "external_communication")
        self.assertEqual(result.source, "explicit_current_turn")

    def test_assistant_proposed_send_still_requires_confirmation(self):
        result = self.policy.evaluate(
            "computer.execute",
            {"goal": "Send a message to the project owner."},
            "Bu bugı incele.",
        )
        self.assertFalse(result.authorized)
        self.assertEqual(result.source, "assistant_proposed")

    def test_session_grant_applies_only_when_category_is_relevant(self):
        self.assertTrue(self.policy.grant("external_communication"))

        relevant = self.policy.evaluate(
            "computer.execute",
            {"goal": "Send the drafted message."},
            "Mesaj tarafında devam et.",
        )
        self.assertTrue(relevant.authorized)
        self.assertEqual(relevant.source, "session_category_grant")

        unrelated = self.policy.evaluate(
            "computer.execute",
            {"goal": "Send the drafted message."},
            "Bugı biraz daha incele.",
        )
        self.assertFalse(unrelated.authorized)

    def test_explicit_denial_beats_session_grant(self):
        self.policy.grant("external_communication")
        result = self.policy.evaluate(
            "computer.execute",
            {"goal": "Send the drafted message."},
            "Mesajı düzenle ama gönderme.",
        )
        self.assertFalse(result.authorized)
        self.assertTrue(result.denied)
        self.assertEqual(result.source, "user_denied")

    def test_critical_categories_always_require_confirmation(self):
        result = self.policy.evaluate(
            "computer.execute",
            {"goal": "Purchase the item and pay now."},
            "Bunu satın al.",
        )
        self.assertFalse(result.authorized)
        self.assertTrue(result.always_confirm)
        self.assertEqual(result.category, "financial_transaction")

    def test_reset_clears_session_grants(self):
        self.policy.grant("computer_control")
        self.assertIn("computer_control", self.policy.grants())
        self.policy.reset()
        self.assertEqual(self.policy.grants(), set())


if __name__ == "__main__":
    unittest.main()
