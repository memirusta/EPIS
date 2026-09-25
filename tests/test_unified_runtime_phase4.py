import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.authorization import SessionAuthorizationPolicy
from agentic.filesystem import FolderTools, repository_path_is_ignored
from agentic.repository_context import RepositoryBindingStore, RepositoryInspector
from agentic.tools import ToolRegistry, build_local_registry


def make_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "epis@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "EPIS Test"], cwd=root, check=True)


def commit_all(root: Path, message: str = "base") -> None:
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)


@unittest.skipUnless(shutil.which("git"), "git is required")
class RepositoryRuntimePhase4Tests(unittest.TestCase):
    def inspector(self, root: Path) -> RepositoryInspector:
        return RepositoryInspector(
            RepositoryBindingStore(root / "device-security" / "binding.json")
        )

    def test_large_repository_inventory_is_batched_without_losing_full_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            make_repo(root)
            src = root / "src"
            src.mkdir()
            for index in range(6005):
                (src / f"f{index:04d}.py").write_text("x=1\n", encoding="utf-8")
            commit_all(root)

            inspector = self.inspector(root)
            first = inspector.snapshot(str(root), cursor=0, batch_size=500)
            self.assertEqual(first["inventory_count"], 6005)
            self.assertEqual(len(first["entries"]), 500)
            self.assertEqual(first["next_cursor"], 500)
            self.assertFalse(first["index_complete"])

            last = inspector.snapshot(str(root), cursor=6000, batch_size=500)
            self.assertEqual(len(last["entries"]), 5)
            self.assertIsNone(last["next_cursor"])
            self.assertTrue(last["index_complete"])

    def test_backup_generated_and_epis_metadata_are_filtered(self):
        self.assertTrue(repository_path_is_ignored("src/core.py.bak"))
        self.assertTrue(repository_path_is_ignored("src/core.py.before-fix-123.bak"))
        self.assertTrue(repository_path_is_ignored("src/__pycache__/core.pyc"))
        self.assertTrue(repository_path_is_ignored(".epis/repo-context.md"))
        self.assertFalse(repository_path_is_ignored("src/core.py"))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            make_repo(root)
            (root / "src").mkdir()
            (root / "src" / "core.py").write_text("x=1\n", encoding="utf-8")
            commit_all(root)
            (root / "src" / "core.py.bak").write_text("stale\n", encoding="utf-8")
            (root / "src" / "thing.py.before-test-1.bak").write_text("stale\n", encoding="utf-8")

            snap = self.inspector(root).snapshot(str(root))
            self.assertIn("src/core.py", snap["entries"])
            self.assertNotIn("src/core.py.bak", snap["entries"])
            self.assertFalse(any("before-test" in value for value in snap["entries"]))

    def test_dirty_fingerprint_uses_contents_not_only_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            make_repo(root)
            target = root / "sample.py"
            target.write_text("aaaa\n", encoding="utf-8")
            commit_all(root)

            target.write_text("bbbb\n", encoding="utf-8")
            stat_before = target.stat()
            folders = FolderTools()
            first = folders.list({"path": str(root), "repository_tree": True})

            target.write_text("cccc\n", encoding="utf-8")
            os.utime(
                target,
                ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns),
            )
            second = folders.list({"path": str(root), "repository_tree": True})

            self.assertNotEqual(
                first["working_tree_fingerprint"],
                second["working_tree_fingerprint"],
            )
            self.assertNotEqual(
                first["dirty_content_sha256"]["sample.py"],
                second["dirty_content_sha256"]["sample.py"],
            )

    def test_binding_is_validated_and_can_drive_pathless_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            root.mkdir()
            make_repo(root)
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            commit_all(root)

            inspector = self.inspector(base)
            bound = inspector.bind(str(root))
            self.assertEqual(Path(bound["repo_root"]), root.resolve())
            snap = inspector.snapshot()
            self.assertEqual(Path(snap["repo_root"]), root.resolve())

            not_repo = base / "not-repo"
            not_repo.mkdir()
            with self.assertRaises(ValueError):
                inspector.bind(str(not_repo))

    def test_saved_state_and_semantic_context_become_stale_on_delta(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            make_repo(root)
            source = root / "src.py"
            source.write_text("value = 1\n", encoding="utf-8")
            commit_all(root)

            inspector = self.inspector(root)
            saved = inspector.save_context(
                str(root),
                context_markdown="# Architecture\n- `src.py`: sample module",
                observed_paths=["src.py"],
            )
            self.assertTrue(Path(saved["inspection_state"]).is_file())
            self.assertTrue(Path(saved["repo_context"]).is_file())

            state = json.loads(
                (root / ".epis" / "inspection-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(state["known_file_sha256"]["src.py"]), 64)

            current = inspector.snapshot(str(root), include_context=True)
            self.assertEqual(current["state_status"], "current")
            self.assertIn("Architecture", current["repo_context"])

            source.write_text("value = 2\n", encoding="utf-8")
            stale = inspector.snapshot(str(root))
            self.assertEqual(stale["state_status"], "stale")
            self.assertIn("src.py", stale["delta_paths"])

    def test_repository_binding_requires_explicit_current_turn(self):
        policy = SessionAuthorizationPolicy()
        yes = policy.evaluate(
            "repository.bind",
            {"path": "D:/Example"},
            "Repo artık burada, bunu ana repo yap.",
        )
        self.assertTrue(yes.authorized)
        no = policy.evaluate(
            "repository.bind",
            {"path": "D:/Example"},
            "Bu repoyu incele.",
        )
        self.assertFalse(no.authorized)
        self.assertEqual(no.source, "explicit_repository_binding_required")

        save = policy.evaluate(
            "repository.context_write",
            {"path": "D:/Example"},
            "Repo contextini kaydet.",
        )
        self.assertTrue(save.authorized)

    def test_registry_and_array_validation_expose_phase4_tools(self):
        registry = build_local_registry()
        names = {spec.name for spec in registry.specs()}
        self.assertIn("bind_repository", names)
        self.assertIn("repository_snapshot", names)
        self.assertIn("save_repository_context", names)
        self.assertEqual(len(registry.specs()), 65)

        schema = {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {"type": "string", "maxLength": 5},
                }
            },
            "additionalProperties": False,
        }
        self.assertIsNone(ToolRegistry._validate(schema, {"paths": ["a", "bb"]}))
        self.assertIn("too many", ToolRegistry._validate(schema, {"paths": ["a", "b", "c"]}))
        self.assertIn("must be a string", ToolRegistry._validate(schema, {"paths": [1]}))


if __name__ == "__main__":
    unittest.main()
