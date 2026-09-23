import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"),
)

from agentic.file_tools import FileTools
from agentic.filesystem import FolderTools
from agentic.path_grants import PersistentPathGrantStore
from agentic.tools import ToolRegistry


class RepositoryAwarenessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "persistent path grants are Windows-scoped")
    def test_persistent_path_grant_is_recursive_and_not_sibling(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            store = PersistentPathGrantStore(str(base / "path-grants.json"))
            root = base / "EPIS"
            root.mkdir()

            granted = store.grant(
                str(root),
                ("files.create", "files.modify"),
            )
            self.assertTrue(granted)
            self.assertTrue(
                store.allows(
                    str(root / "Layer-2" / "src" / "core.py"),
                    "files.modify",
                )
            )
            self.assertFalse(
                store.allows(
                    str(base / "EPIS-old" / "core.py"),
                    "files.modify",
                )
            )

            reloaded = PersistentPathGrantStore(str(base / "path-grants.json"))
            self.assertTrue(
                reloaded.allows(
                    str(root / "tests" / "test_x.py"),
                    "files.create",
                )
            )

    def test_absolute_write_requires_observed_hash_to_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "src" / "sample.py"
            files = FileTools()

            created = files.write({
                "path": str(target),
                "content": "value = 1\n",
            })
            self.assertTrue(created["ok"])
            self.assertEqual(created["status"], "file_created")

            info = files.info({"path": str(target)})
            self.assertTrue(info["ok"])

            missing_hash = files.write({
                "path": str(target),
                "content": "value = 2\n",
            })
            self.assertFalse(missing_hash["ok"])

            replaced = files.write({
                "path": str(target),
                "content": "value = 2\n",
                "expected_sha256": info["sha256"],
            })
            self.assertTrue(replaced["ok"])
            self.assertEqual(replaced["status"], "file_replaced")

            stale = files.write({
                "path": str(target),
                "content": "value = 3\n",
                "expected_sha256": info["sha256"],
            })
            self.assertFalse(stale["ok"])

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_repository_tree_is_recursive_and_fingerprint_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "epis@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "EPIS Test"],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
            (root / "Layer-1").mkdir()
            (root / "Layer-2" / "src").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "node_modules").mkdir()
            (root / "Layer-1" / "one.py").write_text("x=1\n", encoding="utf-8")
            source = root / "Layer-2" / "src" / "two.py"
            source.write_text("y=1\n", encoding="utf-8")
            (root / "tests" / "test_two.py").write_text("pass\n", encoding="utf-8")
            (root / "node_modules" / "noise.js").write_text("noise\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)

            folders = FolderTools()
            first = folders.list({
                "path": str(root),
                "repository_tree": True,
            })
            self.assertTrue(first["ok"])
            self.assertEqual(first["scope"], "repository_tree")
            paths = {item["path"] for item in first["entries"]}
            self.assertIn("Layer-1/one.py", paths)
            self.assertIn("Layer-2/src/two.py", paths)
            self.assertIn("tests/test_two.py", paths)
            self.assertFalse(any("node_modules" in item for item in paths))
            self.assertEqual(len(first["working_tree_fingerprint"]), 64)

            source.write_text("y = 123456\n", encoding="utf-8")
            second = folders.list({
                "path": str(root),
                "repository_tree": True,
            })
            self.assertNotEqual(
                first["working_tree_fingerprint"],
                second["working_tree_fingerprint"],
            )
            self.assertTrue(
                any("Layer-2/src/two.py" in item for item in second["dirty_paths"])
            )

    def test_registry_validates_repository_tree_boolean(self):
        schema = {
            "type": "object",
            "properties": {
                "repository_tree": {"type": "boolean"},
            },
            "additionalProperties": False,
        }
        self.assertIsNone(
            ToolRegistry._validate(schema, {"repository_tree": True})
        )
        self.assertIn(
            "boolean",
            ToolRegistry._validate(schema, {"repository_tree": "yes"}),
        )

    def test_core_no_longer_has_four_model_round_repo_limit(self):
        core_path = (
            Path(__file__).resolve().parents[1]
            / "Layer-2"
            / "src"
            / "agentic"
            / "core.py"
        )
        source = core_path.read_text(encoding="utf-8")
        self.assertNotIn("model_steps < 4", source)
        self.assertIn("_BUDGET_FREE_CAPABILITIES = _SESSION_READ_CAPABILITIES", source)
        self.assertIn("Repeated tool-call cycle blocked", source)


if __name__ == "__main__":
    unittest.main()
