"""Local folder tools.

Supports:
- Legacy known roots: desktop / documents / downloads / workspace
- Explicit absolute local Windows paths such as D:\\Projects\\Nebula-Browser

Folder listing never reads file contents.
Network/UNC paths are not supported.
Creating folders remains restricted to the EPIS workspace.
"""

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PureWindowsPath


ROOT_NAMES = ("desktop", "documents", "downloads", "workspace")

RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *[f"COM{i}" for i in range(10)],
    *[f"LPT{i}" for i in range(10)],
}

# Bunları repo listelerinde göstermenin pek faydası yok.
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".epis",
    ".gradle",
    ".idea",
    ".next",
    "build",
    "dist",
    "target",
    "coverage",
}

# Generated/backup artifacts can otherwise dominate repository inventories and
# cause Luna/Sol to inspect stale source copies. These are filtered even when
# they are untracked and not covered by the repository's own .gitignore.
IGNORED_FILE_SUFFIXES = (
    ".pyc", ".pyo", ".class", ".o", ".obj", ".tmp", ".temp",
    ".bak", ".orig", ".rej", ".swp", ".swo",
)


def repository_path_is_ignored(relative: str) -> bool:
    normalized = str(relative or "").strip().replace("\\", "/")
    if not normalized:
        return True
    components = normalized.split("/")
    folded = [part.casefold() for part in components]
    if any(part in IGNORED_DIRECTORY_NAMES for part in folded[:-1]):
        return True
    name = folded[-1]
    if protected_metadata_name(name):
        return True
    if name.endswith("~") or name.endswith(IGNORED_FILE_SUFFIXES):
        return True
    if ".bak-" in name or ".before-" in name or name.startswith(".#"):
        return True
    return False


PROTECTED_METADATA_NAMES = frozenset({
    ".env",
    "keys.env",
    "credentials.json",
    "credential.json",
    "secrets.json",
    "passwords.txt",
    "id_rsa",
    "id_ed25519",
    "id_dsa",
    "id_ecdsa",
})


def protected_metadata_name(value: str) -> bool:
    name = str(value or "").casefold()

    return (
        name in PROTECTED_METADATA_NAMES
        or name.startswith(".env.")
        or "private_key" in name
    )


def parts(relative):
    """Validate a relative path used with one of the legacy known roots."""
    if not relative:
        return ()

    path = PureWindowsPath(relative)

    if path.is_absolute() or path.drive or path.root:
        raise ValueError(
            "Only relative paths are allowed when using a known root"
        )

    raw = relative.replace("\\", "/").split("/")

    if len(raw) > 12:
        raise ValueError("Path too deep")

    for name in raw:
        if (
            not name
            or name in (".", "..")
            or name.endswith((".", " "))
            or len(name) > 100
            or any(ord(c) < 32 or c in '<>:"|?*' for c in name)
            or name.split(".")[0].upper() in RESERVED
            or protected_metadata_name(name)
        ):
            raise ValueError("Invalid or protected path component")

    return tuple(raw)


def known_root(name):
    """Resolve one of the legacy known EPIS roots."""
    if name == "workspace":
        return (
            Path(__file__).resolve().parents[3]
            / ".epis-runtime"
            / "files"
        )

    from win32com.shell import shell

    folder_id = {
        "desktop": shell.FOLDERID_Desktop,
        "documents": shell.FOLDERID_Documents,
        "downloads": shell.FOLDERID_Downloads,
    }[name]

    return Path(
        shell.SHGetKnownFolderPath(
            folder_id,
            0,
            None,
        )
    )


def absolute_local_directory(value: str) -> Path:
    """Resolve an explicitly supplied absolute local Windows folder."""
    if not value or not isinstance(value, str):
        raise ValueError("Missing folder path")

    target = Path(value).expanduser()

    if not target.is_absolute():
        raise ValueError("Absolute Windows path required")

    raw = str(target)

    # Şimdilik ağ yollarını açmıyoruz.
    if raw.startswith(("\\\\", "//")):
        raise ValueError("Network/UNC folders are not supported")

    try:
        target = target.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("Folder does not exist") from exc

    if not target.is_dir():
        raise ValueError("Folder does not exist")

    return target


class FolderTools:
    def __init__(self, resolver=known_root):
        self.resolver = resolver

    def directory(
        self,
        root=None,
        relative="",
        absolute_path=None,
    ):
        """
        Resolve either:

        1. An explicit absolute path:
           D:\\Projects\\Nebula-Browser

        2. A legacy known root + relative path:
           documents + Projects/foo
        """

        if absolute_path:
            return absolute_local_directory(absolute_path)

        if root not in ROOT_NAMES:
            raise ValueError(
                "Provide either an absolute path or a known root"
            )

        base = self.resolver(root)

        if (
            not base.is_absolute()
            or str(base).startswith(("\\\\", "//"))
        ):
            raise ValueError("Remote folders not supported")

        # Legacy known-root yolunda önceki güvenlik kontrolünü koru.
        for item in reversed((base, *base.parents)):
            if not item.exists():
                continue

            info = os.lstat(item)

            if (
                info.st_file_attributes
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError(
                    "Reparse folders not supported"
                )

        if root == "workspace" and not base.exists():
            base.mkdir(
                parents=True,
                exist_ok=False,
            )

        target = base

        for name in parts(relative):
            target /= name

            if not target.exists():
                raise ValueError("Folder does not exist")

            attrs = os.lstat(
                target
            ).st_file_attributes

            if attrs & (
                stat.FILE_ATTRIBUTE_REPARSE_POINT
                | stat.FILE_ATTRIBUTE_HIDDEN
                | stat.FILE_ATTRIBUTE_SYSTEM
            ):
                raise ValueError(
                    "Protected folders not supported"
                )

        if not target.is_dir():
            raise ValueError("Folder does not exist")

        return target

    @staticmethod
    def _git_text(target: Path, *arguments: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(target), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.rstrip("\r\n")

    def _repository_tree(self, target: Path):
        raw_files = self._git_text(
            target,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        )
        if raw_files is None:
            return None

        repo_root = self._git_text(
            target, "rev-parse", "--show-toplevel"
        )
        head = self._git_text(
            target, "rev-parse", "HEAD"
        )
        branch = self._git_text(
            target, "rev-parse", "--abbrev-ref", "HEAD"
        )
        status = self._git_text(
            target, "status", "--porcelain=v1"
        )

        entries = []
        truncated = False
        encoded_size = 0
        for raw_path in raw_files.splitlines():
            relative = raw_path.strip().replace("\\", "/")
            if not relative:
                continue
            if repository_path_is_ignored(relative):
                continue
            item = {"path": relative, "kind": "file"}
            encoded_size += len(
                json.dumps(item, ensure_ascii=False).encode("utf-8")
            ) + 2
            if len(entries) >= 10000 or encoded_size > 250000:
                truncated = True
                break
            entries.append(item)

        dirty_paths = []
        for line in (status or "").splitlines():
            if len(line) < 4:
                continue
            candidate_name = line[3:].strip().split(" -> ")[-1].strip().strip('"')
            candidate_name = candidate_name.replace("\\", "/")
            if candidate_name and not repository_path_is_ignored(candidate_name):
                dirty_paths.append(candidate_name)

        actual_root = Path(repo_root or target)
        fingerprint_parts = [head or ""]
        dirty_content_sha256 = {}
        for candidate_name in dirty_paths:
            candidate = actual_root / candidate_name
            try:
                if candidate.is_file():
                    digest = hashlib.sha256()
                    with candidate.open("rb") as handle:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                    content_hash = digest.hexdigest()
                else:
                    content_hash = "missing"
            except OSError:
                content_hash = "unreadable"
            dirty_content_sha256[candidate_name] = content_hash
            fingerprint_parts.append(f"{candidate_name}|{content_hash}")

        working_tree_fingerprint = hashlib.sha256(
            "\n".join(fingerprint_parts).encode("utf-8", errors="replace")
        ).hexdigest()

        return {
            "ok": True,
            "path": str(target),
            "repo_root": repo_root or str(target),
            "git_head": head,
            "git_branch": branch,
            "working_tree_fingerprint": working_tree_fingerprint,
            "dirty_paths": dirty_paths,
            "dirty_content_sha256": dirty_content_sha256,
            "entries": entries,
            "truncated": truncated,
            "scope": "repository_tree",
            "file_contents_read": False,
            "hidden_entries_included": False,
            "system_entries_included": False,
            "reparse_entries_included": False,
            "dependency_directories_included": False,
        }

    def list(self, args):
        target = self.directory(
            root=args.get("root"),
            relative=args.get("relative_path", ""),
            absolute_path=args.get("path"),
        )

        if args.get("repository_tree"):
            repository = self._repository_tree(target)
            if repository is not None:
                return repository

        items = []
        truncated = False
        encoded_size = 0

        with os.scandir(target) as entries:
            for index, entry in enumerate(entries):
                if index >= 500 or len(items) >= 100:
                    truncated = True
                    break

                try:
                    attrs = entry.stat(
                        follow_symlinks=False
                    ).st_file_attributes

                    if attrs & (
                        stat.FILE_ATTRIBUTE_REPARSE_POINT
                        | stat.FILE_ATTRIBUTE_HIDDEN
                        | stat.FILE_ATTRIBUTE_SYSTEM
                    ):
                        continue

                    if protected_metadata_name(entry.name):
                        continue

                    if (
                        entry.is_dir(follow_symlinks=False)
                        and entry.name.casefold()
                        in IGNORED_DIRECTORY_NAMES
                    ):
                        continue

                    item = {
                        "name": entry.name,
                        "kind": (
                            "folder"
                            if entry.is_dir(
                                follow_symlinks=False
                            )
                            else "file"
                        ),
                    }

                    encoded_size += (
                        len(
                            json.dumps(
                                item,
                                ensure_ascii=False,
                            ).encode("utf-8")
                        )
                        + 2
                    )

                    if encoded_size > 24000:
                        truncated = True
                        break

                    items.append(item)

                except OSError:
                    continue

        items.sort(
            key=lambda entry: entry["name"].casefold()
        )

        return {
            "ok": True,
            "path": str(target),
            "entries": items,
            "truncated": truncated,
            "scope": "single_directory",
            "file_contents_read": False,
            "hidden_entries_included": False,
            "system_entries_included": False,
            "reparse_entries_included": False,
            "dependency_directories_included": False,
        }

    def open(self, args):
        target = self.directory(
            root=args.get("root"),
            relative=args.get("relative_path", ""),
            absolute_path=args.get("path"),
        )

        os.startfile(str(target))

        return {
            "ok": True,
            "status": "open_requested",
            "path": str(target),
            "visible_window_verified": False,
        }

    def create(self, args):
        """
        Folder creation intentionally stays restricted
        to the dedicated EPIS workspace.
        """
        names = parts(args["name"])

        if len(names) != 1:
            return {
                "ok": False,
                "error": "Use one folder name",
            }

        parent = self.directory(
            root="workspace",
            relative=args.get(
                "relative_path",
                "",
            ),
        )

        target = parent / names[0]

        if target.exists():
            return {
                "ok": False,
                "error": (
                    "Destination already exists; "
                    "nothing overwritten"
                ),
            }

        target.mkdir(exist_ok=False)

        return {
            "ok": True,
            "status": "folder_created",
            "name": names[0],
            "path": str(target),
        }


def register_folder_tools(registry):
    from .tools import ToolSpec

    folders = FolderTools()

    device = {
        "device_id": {
            "type": "string",
        }
    }

    location = {
        "root": {
            "type": "string",
            "enum": list(ROOT_NAMES),
            "description": (
                "Optional legacy root: "
                "desktop/documents/downloads/workspace."
            ),
        },
        "relative_path": {
            "type": "string",
            "maxLength": 500,
            "description": (
                "Relative folder path when root is used."
            ),
        },
        "path": {
            "type": "string",
            "maxLength": 2000,
            "description": (
                "Absolute local Windows folder path. "
                r"Example: D:\Projects\Nebula-Browser"
            ),
        },
        "repository_tree": {
            "type": "boolean",
            "description": (
                "When true, return a recursive Git-aware repository tree "
                "using tracked and non-ignored files, plus HEAD/branch/dirty paths. "
                "Prefer this as the first inspection call for a repository root."
            ),
        },
    }

    def schema(properties, required):
        return {
            "type": "object",
            "properties": {
                **properties,
                **device,
            },
            "required": required,
            "additionalProperties": False,
        }

    registry.register(
        ToolSpec(
            "list_folder",
            (
                "List one local directory. Accepts either an "
                "absolute local Windows folder path or the legacy "
                "desktop/documents/downloads/workspace roots. "
                "If the user explicitly provides a project or "
                "repository path, inspect it directly; do not ask "
                "them to copy it into the EPIS workspace. "
                "This tool lists names only and does not read "
                "file contents. For repository review, call it once on the "
                "repository root with repository_tree=true before walking "
                "individual folders."
            ),
            schema(
                location,
                [],
            ),
            "files.list",
            "yellow",
            True,
            confirmation_notice=(
                "Bu klasördeki görünür dosya/klasör adları "
                "model API'sine gönderilecek. "
                "Dosya içerikleri okunmaz."
            ),
        ),
        folders.list,
    )

    registry.register(
        ToolSpec(
            "open_folder",
            (
                "Open a local folder in Explorer. Accepts either "
                "an absolute local Windows folder path or a "
                "legacy known root. Does not open files or "
                "execute anything."
            ),
            schema(
                location,
                [],
            ),
            "files.open_folder",
            "yellow",
            True,
        ),
        folders.open,
    )

    registry.register(
        ToolSpec(
            "create_workspace_folder",
            (
                "Create one new folder ONLY inside the dedicated "
                "EPIS workspace. No overwrite, deletion or file "
                "writes. Absolute paths are not supported for "
                "folder creation."
            ),
            schema(
                {
                    "name": {
                        "type": "string",
                        "maxLength": 100,
                    },
                    "relative_path": {
                        "type": "string",
                        "maxLength": 500,
                        "description": (
                            "Optional relative parent folder "
                            "inside the EPIS workspace."
                        ),
                    },
                },
                ["name"],
            ),
            "files.create_folder",
            "yellow",
            True,
        ),
        folders.create,
    )