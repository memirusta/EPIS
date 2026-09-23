"""Persistent recursive local-path grants for EPIS file editing.

The model never writes this store directly. Core records a grant only after the
user confirms an approval card. A grant applies to its root and descendants,
but never to an entire drive root. Delete/execute permissions are intentionally
outside this store.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path


DEFAULT_PERMISSIONS = ("files.create", "files.modify")


def _normalize(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing path")
    raw = value.strip()
    if raw.startswith(("\\\\", "//")):
        raise ValueError("Network/UNC paths are not grantable")
    normalized = os.path.normcase(
        os.path.abspath(os.path.normpath(raw))
    )
    drive, _ = os.path.splitdrive(normalized)
    if not drive:
        raise ValueError("Absolute local path required")
    if normalized.rstrip("/\\") == drive.rstrip("/\\"):
        raise ValueError("Drive-root grants are not allowed")
    return normalized


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except (ValueError, OSError):
        return False


class PersistentPathGrantStore:
    """Small trusted JSON store under LocalAppData, outside repository roots."""

    def __init__(self, path: str | None = None):
        configured = path
        if configured is None:
            allow_override = os.getenv(
                "EPIS_ALLOW_SECURITY_STORE_OVERRIDE",
                "",
            ).strip().lower() in {"1", "true", "yes"}
            if allow_override:
                configured = os.getenv("EPIS_PATH_GRANTS_FILE")

        if configured:
            self.path = Path(configured)
        else:
            base = Path(os.getenv("LOCALAPPDATA") or Path.home())
            self.path = base / "EPIS" / "security" / "path-grants.json"
        self._data = self._load()

    def _load(self) -> dict:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("grants"), list):
                    return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {"version": 1, "grants": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def allows(self, path: str, permission: str) -> bool:
        try:
            normalized = _normalize(path)
        except ValueError:
            return False
        for grant in self._data.get("grants", []):
            try:
                root = _normalize(grant.get("root", ""))
            except ValueError:
                continue
            permissions = set(grant.get("permissions") or [])
            if (
                grant.get("recursive", True)
                and permission in permissions
                and _inside(normalized, root)
            ):
                return True
        return False

    def grant(
        self,
        root: str,
        permissions=DEFAULT_PERMISSIONS,
    ) -> str:
        normalized = _normalize(root)
        wanted = sorted(set(permissions))
        grants = self._data.setdefault("grants", [])
        for grant in grants:
            try:
                current = _normalize(grant.get("root", ""))
            except ValueError:
                continue
            if current == normalized:
                merged = sorted(set(grant.get("permissions") or []) | set(wanted))
                grant.update({
                    "root": normalized,
                    "permissions": merged,
                    "recursive": True,
                    "granted_at": datetime.now(timezone.utc).isoformat(),
                })
                self._save()
                return normalized
        grants.append({
            "root": normalized,
            "permissions": wanted,
            "recursive": True,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        })
        self._save()
        return normalized

    def roots(self) -> list[dict]:
        return [dict(item) for item in self._data.get("grants", [])]
