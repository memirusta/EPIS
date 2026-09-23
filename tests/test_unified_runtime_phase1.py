from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAYER2_SRC = REPO_ROOT / "Layer-2" / "src"

if str(LAYER2_SRC) not in sys.path:
    sys.path.insert(0, str(LAYER2_SRC))

from agentic.authorization import SessionAuthorizationPolicy
from agentic.file_tools import FileTools


class MutationIntentTests(unittest.TestCase):
    def setUp(self):
        self.policy = SessionAuthorizationPolicy()

    def test_inspection_does_not_authorize_file_mutation(self):
        decision = self.policy.evaluate(
            "files.patch_text", {}, "D:\\EPIS reposunu incele ve sorunları söyle"
        )
        self.assertEqual(decision.category, "file_mutation")
        self.assertFalse(decision.authorized)

    def test_explicit_fix_authorizes_file_mutation(self):
        decision = self.policy.evaluate(
            "files.patch_text", {}, "core.py içindeki bu bugı düzelt"
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.source, "explicit_current_turn")

    def test_explicit_read_only_denial_wins(self):
        decision = self.policy.evaluate(
            "files.write_text", {}, "sadece incele, hiçbir şeyi değiştirme"
        )
        self.assertTrue(decision.denied)
        self.assertFalse(decision.authorized)


class PatchTextTests(unittest.TestCase):
    def test_patch_requires_unique_context_and_current_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "sample.py"
            target.write_text("value = 1\n", encoding="utf-8")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            files = FileTools()

            result = files.patch({
                "path": str(target),
                "expected_sha256": digest,
                "old_text": "value = 1",
                "new_text": "value = 2",
            })
            self.assertTrue(result["ok"], result)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

            stale = files.patch({
                "path": str(target),
                "expected_sha256": digest,
                "old_text": "value = 2",
                "new_text": "value = 3",
            })
            self.assertFalse(stale["ok"])
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_patch_rejects_zero_or_multiple_matches_without_write(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "sample.py"
            target.write_text("x = 1\nx = 1\n", encoding="utf-8")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            before = target.read_bytes()
            files = FileTools()

            result = files.patch({
                "path": str(target),
                "expected_sha256": digest,
                "old_text": "x = 1",
                "new_text": "x = 2",
            })
            self.assertFalse(result["ok"])
            self.assertEqual(result.get("error"), "patch_context_mismatch")
            self.assertEqual(result.get("match_count"), 2)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
