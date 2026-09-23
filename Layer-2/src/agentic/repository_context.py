"""Deterministic repository indexing, binding and EPIS repository memory.

The model never reads the local filesystem through this module directly.  These
helpers run inside the trusted device agent and return bounded evidence to Luna.
Semantic repository notes may be supplied by Sol, but deterministic Git state is
always recomputed locally immediately before it is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import threading
import uuid
from typing import Any

from .filesystem import protected_metadata_name, repository_path_is_ignored


STATE_VERSION = 1
DEFAULT_BATCH_SIZE = 250
MAX_BATCH_SIZE = 500
MAX_CONTEXT_CHARS = 60000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.epis-tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    head: str
    branch: str


class RepositoryBindingStore:
    """Device-local canonical repository binding.

    The default store lives below LOCALAPPDATA/EPIS/security and is intentionally
    outside model-writable repository paths.  Tests may inject an explicit path.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None):
        if path is None:
            root = Path(os.getenv("LOCALAPPDATA") or Path.home())
            path = root / "EPIS" / "security" / "repository-binding.json"
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> str | None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
        root = raw.get("repo_root") if isinstance(raw, dict) else None
        return root if isinstance(root, str) and root.strip() else None

    def bind(self, repo_root: Path) -> str:
        payload = {
            "version": STATE_VERSION,
            "repo_root": str(repo_root),
            "bound_at": _utc_now(),
        }
        with self._lock:
            _atomic_write_json(self.path, payload)
        return str(repo_root)


class RepositoryInspector:
    def __init__(self, binding_store: RepositoryBindingStore | None = None):
        self.binding_store = binding_store or RepositoryBindingStore()

    @staticmethod
    def _git(target: Path, *args: str, timeout: int = 30) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(target), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("git_unavailable") from exc
        if completed.returncode != 0:
            raise ValueError("not_a_git_repository")
        return completed.stdout.rstrip("\r\n")

    def identity(self, path: str | None = None) -> RepositoryIdentity:
        raw = (path or self.binding_store.load() or "").strip()
        if not raw:
            raise ValueError("repository_path_required")
        if raw.startswith(("\\\\", "//")):
            raise ValueError("network_repository_not_supported")
        target = Path(raw).expanduser()
        if not target.is_absolute():
            raise ValueError("absolute_repository_path_required")
        try:
            target = target.resolve(strict=True)
        except OSError as exc:
            raise ValueError("repository_path_not_found") from exc
        if not target.is_dir():
            raise ValueError("repository_path_not_found")

        root_text = self._git(target, "rev-parse", "--show-toplevel")
        root = Path(root_text).resolve(strict=True)
        head = self._git(root, "rev-parse", "HEAD")
        branch = self._git(root, "rev-parse", "--abbrev-ref", "HEAD")
        return RepositoryIdentity(root=root, head=head, branch=branch)

    def bind(self, path: str) -> dict[str, Any]:
        identity = self.identity(path)
        bound = self.binding_store.bind(identity.root)
        return {
            "ok": True,
            "status": "repository_bound",
            "repo_root": bound,
            "git_head": identity.head,
            "git_branch": identity.branch,
        }

    def inventory(self, root: Path) -> list[str]:
        raw = self._git(
            root,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        )
        entries: list[str] = []
        for value in raw.split("\0"):
            relative = value.strip().replace("\\", "/")
            if not relative or repository_path_is_ignored(relative):
                continue
            entries.append(relative)
        entries.sort(key=str.casefold)
        return entries

    def dirty_paths(self, root: Path) -> list[str]:
        commands = (
            ("diff", "--name-only", "-z"),
            ("diff", "--cached", "--name-only", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        )
        paths: set[str] = set()
        for command in commands:
            raw = self._git(root, *command)
            for value in raw.split("\0"):
                relative = value.strip().replace("\\", "/")
                if relative and not repository_path_is_ignored(relative):
                    paths.add(relative)
        return sorted(paths, key=str.casefold)

    @staticmethod
    def _safe_repo_file(root: Path, relative: str) -> Path | None:
        normalized = str(relative or "").strip().replace("\\", "/")
        if not normalized or repository_path_is_ignored(normalized):
            return None
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts:
            return None
        if protected_metadata_name(pure.name):
            return None
        candidate = (root / Path(*pure.parts)).resolve(strict=False)
        try:
            if os.path.commonpath([str(candidate), str(root)]) != str(root):
                return None
        except ValueError:
            return None
        return candidate

    def working_tree_fingerprint(
        self,
        identity: RepositoryIdentity,
        dirty_paths: list[str],
    ) -> tuple[str, dict[str, str]]:
        content_hashes: dict[str, str] = {}
        parts = [identity.head]
        for relative in dirty_paths:
            candidate = self._safe_repo_file(identity.root, relative)
            if candidate is None:
                continue
            if candidate.is_file():
                try:
                    digest = _sha256_file(candidate)
                except OSError:
                    digest = "unreadable"
            else:
                digest = "missing"
            content_hashes[relative] = digest
            parts.append(f"{relative}|{digest}")
        return (
            hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest(),
            content_hashes,
        )

    @staticmethod
    def index_fingerprint(entries: list[str]) -> str:
        return hashlib.sha256("\0".join(entries).encode("utf-8")).hexdigest()

    @staticmethod
    def _state_path(root: Path) -> Path:
        return root / ".epis" / "inspection-state.json"

    @staticmethod
    def _context_path(root: Path) -> Path:
        return root / ".epis" / "repo-context.md"

    def load_state(self, root: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(self._state_path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _head_delta(self, root: Path, old_head: str, new_head: str) -> list[str]:
        if not old_head or old_head == new_head:
            return []
        try:
            raw = self._git(root, "diff", "--name-only", "-z", old_head, new_head)
        except ValueError:
            return []
        return sorted(
            {
                value.strip().replace("\\", "/")
                for value in raw.split("\0")
                if value.strip()
                and not repository_path_is_ignored(value.strip().replace("\\", "/"))
            },
            key=str.casefold,
        )

    def snapshot(
        self,
        path: str | None = None,
        *,
        cursor: int = 0,
        batch_size: int = DEFAULT_BATCH_SIZE,
        include_context: bool = False,
    ) -> dict[str, Any]:
        if cursor < 0:
            raise ValueError("cursor_must_be_non_negative")
        if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
            raise ValueError("invalid_batch_size")

        identity = self.identity(path)
        entries = self.inventory(identity.root)
        dirty = self.dirty_paths(identity.root)
        fingerprint, dirty_hashes = self.working_tree_fingerprint(identity, dirty)
        index_fingerprint = self.index_fingerprint(entries)
        previous = self.load_state(identity.root)

        previous_head = str((previous or {}).get("git_head") or "")
        previous_fingerprint = str(
            (previous or {}).get("working_tree_fingerprint") or ""
        )
        delta = set(dirty)
        delta.update(self._head_delta(identity.root, previous_head, identity.head))

        if previous is None:
            state_status = "missing"
            requires_full_inspection = True
        elif (
            previous_head == identity.head
            and previous_fingerprint == fingerprint
            and (previous or {}).get("index_fingerprint") == index_fingerprint
        ):
            state_status = "current"
            requires_full_inspection = False
            delta.clear()
        else:
            state_status = "stale"
            requires_full_inspection = False

        start = min(cursor, len(entries))
        end = min(start + batch_size, len(entries))
        next_cursor = end if end < len(entries) else None

        context_path = self._context_path(identity.root)
        context_exists = context_path.is_file()
        context_text = ""
        context_truncated = False
        if include_context and context_exists:
            try:
                context_text = context_path.read_text(encoding="utf-8")
            except OSError:
                context_text = ""
            if len(context_text) > MAX_CONTEXT_CHARS:
                context_text = context_text[:MAX_CONTEXT_CHARS]
                context_truncated = True

        return {
            "ok": True,
            "scope": "repository_snapshot",
            "repo_root": str(identity.root),
            "git_head": identity.head,
            "git_branch": identity.branch,
            "working_tree_fingerprint": fingerprint,
            "index_fingerprint": index_fingerprint,
            "dirty_paths": dirty,
            "dirty_content_sha256": dirty_hashes,
            "delta_paths": sorted(delta, key=str.casefold),
            "state_status": state_status,
            "requires_full_inspection": requires_full_inspection,
            "inventory_count": len(entries),
            "cursor": start,
            "batch_size": batch_size,
            "entries": entries[start:end],
            "next_cursor": next_cursor,
            "index_complete": next_cursor is None,
            "repo_context_exists": context_exists,
            "repo_context": context_text if include_context else None,
            "repo_context_truncated": context_truncated,
            "file_contents_read": bool(include_context and context_text),
        }

    def save_context(
        self,
        path: str | None = None,
        *,
        context_markdown: str = "",
        observed_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        identity = self.identity(path)
        entries = self.inventory(identity.root)
        dirty = self.dirty_paths(identity.root)
        fingerprint, dirty_hashes = self.working_tree_fingerprint(identity, dirty)
        index_fingerprint = self.index_fingerprint(entries)

        known_hashes: dict[str, str] = {}
        for relative in observed_paths or []:
            candidate = self._safe_repo_file(identity.root, relative)
            if candidate is None or not candidate.is_file():
                continue
            try:
                known_hashes[relative.replace("\\", "/")] = _sha256_file(candidate)
            except OSError:
                continue

        previous = self.load_state(identity.root) or {}
        previous_known = previous.get("known_file_sha256")
        if isinstance(previous_known, dict):
            for key, value in previous_known.items():
                if isinstance(key, str) and isinstance(value, str):
                    known_hashes.setdefault(key, value)
        # Dirty paths override any stale previously observed hashes.
        for key, value in dirty_hashes.items():
            known_hashes[key] = value

        state = {
            "version": STATE_VERSION,
            "repo_root": str(identity.root),
            "git_branch": identity.branch,
            "git_head": identity.head,
            "working_tree_fingerprint": fingerprint,
            "index_fingerprint": index_fingerprint,
            "dirty_paths": dirty,
            "inventory_count": len(entries),
            "known_file_sha256": dict(sorted(known_hashes.items())),
            "updated_at": _utc_now(),
        }
        _atomic_write_json(self._state_path(identity.root), state)

        context_written = False
        if context_markdown.strip():
            cleaned = context_markdown.strip()
            if len(cleaned) > MAX_CONTEXT_CHARS:
                raise ValueError("repository_context_too_large")
            header = (
                "<!-- EPIS repository context: semantic cache, not source evidence. "
                f"HEAD={identity.head} fingerprint={fingerprint} -->\n"
            )
            _atomic_write_text(
                self._context_path(identity.root),
                header + cleaned + "\n",
            )
            context_written = True

        return {
            "ok": True,
            "status": "repository_context_saved",
            "repo_root": str(identity.root),
            "inspection_state": str(self._state_path(identity.root)),
            "repo_context": (
                str(self._context_path(identity.root)) if context_written else None
            ),
            "working_tree_fingerprint": fingerprint,
            "git_head": identity.head,
            "known_file_sha256_count": len(known_hashes),
        }


def register_repository_tools(registry) -> None:
    from .tools import ToolSpec

    inspector = RepositoryInspector()
    device = {"device_id": {"type": "string"}}

    registry.register(
        ToolSpec(
            "bind_repository",
            (
                "Set the canonical local Git repository after the user explicitly says "
                "this repository/location should become EPIS's current canonical repo. "
                "The path is validated with Git and stored device-locally; no source "
                "files are changed."
            ),
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 2000},
                    **device,
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "repository.bind",
            effects=("configuration",),
        ),
        lambda args: inspector.bind(args["path"]),
    )

    registry.register(
        ToolSpec(
            "repository_snapshot",
            (
                "Inspect a Git repository deterministically. Returns HEAD/branch, a "
                "content-based dirty fingerprint, delta paths versus saved .epis state, "
                "and one bounded batch of the COMPLETE Git-aware file index. Continue "
                "with next_cursor for large repositories. If path is omitted, use the "
                "device-local canonical repository binding. repo-context.md is semantic "
                "cache only and never replaces fresh evidence."
            ),
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 2000},
                    "cursor": {"type": "integer", "minimum": 0},
                    "batch_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_BATCH_SIZE,
                    },
                    "include_context": {"type": "boolean"},
                    **device,
                },
                "additionalProperties": False,
            },
            "repository.inspect",
            "yellow",
            True,
            confirmation_notice=(
                "Repository dosya adları, Git durumu ve seçilirse EPIS repo-context "
                "önbelleği model API'sine gönderilecek. Kaynak dosya içerikleri bu "
                "araç tarafından okunmaz."
            ),
            effects=("read",),
        ),
        lambda args: inspector.snapshot(
            args.get("path"),
            cursor=args.get("cursor", 0),
            batch_size=args.get("batch_size", DEFAULT_BATCH_SIZE),
            include_context=bool(args.get("include_context", False)),
        ),
    )

    registry.register(
        ToolSpec(
            "save_repository_context",
            (
                "Persist deterministic repository inspection state to "
                ".epis/inspection-state.json and optional Sol-generated semantic notes "
                "to .epis/repo-context.md. This is repository metadata mutation and is "
                "allowed only when the CURRENT user message explicitly authorizes a "
                "change/update/save. observed_paths must contain only files actually "
                "read during inspection; Core hashes them locally before saving."
            ),
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 2000},
                    "context_markdown": {
                        "type": "string",
                        "maxLength": MAX_CONTEXT_CHARS,
                    },
                    "observed_paths": {
                        "type": "array",
                        "maxItems": 2000,
                        "items": {"type": "string", "maxLength": 2000},
                    },
                    **device,
                },
                "additionalProperties": False,
            },
            "repository.context_write",
            effects=("write",),
        ),
        lambda args: inspector.save_context(
            args.get("path"),
            context_markdown=args.get("context_markdown", ""),
            observed_paths=args.get("observed_paths") or [],
        ),
    )
