"""Canonical same-day conversation transcript storage.

The transcript is intentionally short-lived raw conversation data.  It exists so
all EPIS chat clients can render the same day transcript and so Nightly
Recalculation can later freeze + consume one deterministic input.  Long-term
personal memory belongs in the trusted local memory vault, not here.

Cloud deployments should use PostgreSQL through ``DATABASE_URL`` (or the more
specific ``EPIS_DAILY_TRANSCRIPT_DATABASE_URL``).  SQLite is used for local/dev
and tests.  A cloud SQLite fallback is deliberately marked non-durable so the
server can stay bootable while configuration is being rolled out without
pretending dyno-local storage is persistent.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any, Iterable
import uuid
from zoneinfo import ZoneInfo


_SCHEMA_VERSION = 1
_DEFAULT_TIMEZONE = "Europe/Istanbul"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_timezone(name: str | None) -> ZoneInfo:
    value = (name or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE
    try:
        return ZoneInfo(value)
    except Exception:
        return ZoneInfo("UTC")


def day_id_for(
    value: datetime | None = None,
    *,
    timezone_name: str | None = None,
) -> str:
    """Return the EPIS civil-day id used for daily transcript boundaries."""

    zone = _safe_timezone(
        timezone_name or os.getenv("EPIS_DAY_TIMEZONE") or _DEFAULT_TIMEZONE
    )
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(zone).date().isoformat()


def _normalize_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        mime_type = str(item.get("mime_type") or "application/octet-stream").strip()
        try:
            size_bytes = int(item.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        if not name:
            continue
        result.append(
            {
                "name": name[:300],
                "mime_type": mime_type[:160],
                "size_bytes": max(0, size_bytes),
            }
        )
    return result


def _message_payload(
    *,
    seq: int,
    message_id: str,
    day_id: str,
    role: str,
    text: str,
    request_id: str | None,
    client_id: str | None,
    origin_device_id: str | None,
    attachments_json: str | None,
    created_at: str,
    source: str,
) -> dict[str, Any]:
    try:
        attachments = json.loads(attachments_json or "[]")
    except json.JSONDecodeError:
        attachments = []
    return {
        "seq": int(seq),
        "message_id": message_id,
        "day_id": day_id,
        "role": role,
        "text": text,
        "request_id": request_id,
        "client_id": client_id,
        "origin_device_id": origin_device_id,
        "attachments": _normalize_attachments(attachments),
        "created_at": created_at,
        "source": source,
    }


def transcript_input_hash(messages: Iterable[dict[str, Any]]) -> str:
    """Stable digest used by the future NC idempotency boundary."""

    compact = [
        {
            "seq": int(item.get("seq") or 0),
            "message_id": str(item.get("message_id") or ""),
            "role": str(item.get("role") or ""),
            "text": str(item.get("text") or ""),
            "attachments": _normalize_attachments(item.get("attachments")),
            "created_at": str(item.get("created_at") or ""),
        }
        for item in messages
    ]
    encoded = json.dumps(
        compact,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TranscriptStoreStatus:
    backend: str
    durable: bool
    schema_version: int = _SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "durable": self.durable,
            "schema_version": self.schema_version,
        }


class SqliteDailyTranscriptStore:
    backend = "sqlite"

    def __init__(self, path: str | os.PathLike[str], *, durable: bool = True):
        self.path = str(path)
        self.durable = bool(durable)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(
                parents=True,
                exist_ok=True,
            )
        self._initialize()

    @property
    def status(self) -> TranscriptStoreStatus:
        return TranscriptStoreStatus(self.backend, self.durable)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def _connection(self):
        """Yield a SQLite connection and always close the OS file handle.

        ``sqlite3.Connection`` as a context manager commits/rolls back but does
        not close the connection.  That is easy to miss on POSIX, where an open
        database can still be unlinked, but it leaks a file handle on Windows
        and prevents TemporaryDirectory cleanup.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_transcript_days (
                    day_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active',
                    input_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    frozen_at TEXT,
                    processed_at TEXT,
                    context_floor_seq INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS daily_transcript_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    day_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    request_id TEXT,
                    client_id TEXT,
                    origin_device_id TEXT,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'chat',
                    FOREIGN KEY(day_id) REFERENCES daily_transcript_days(day_id)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_transcript_day_seq
                    ON daily_transcript_messages(day_id, seq);
                CREATE INDEX IF NOT EXISTS idx_daily_transcript_request
                    ON daily_transcript_messages(request_id);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(daily_transcript_days)"
                ).fetchall()
            }
            if "context_floor_seq" not in columns:
                connection.execute(
                    "ALTER TABLE daily_transcript_days "
                    "ADD COLUMN context_floor_seq INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _ensure_day(connection: sqlite3.Connection, day_id: str) -> None:
        now = _utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO daily_transcript_days
                (day_id, status, created_at, updated_at)
            VALUES (?, 'active', ?, ?)
            """,
            (day_id, now, now),
        )

    def append_message(
        self,
        *,
        role: str,
        text: str,
        day_id: str | None = None,
        request_id: str | None = None,
        client_id: str | None = None,
        origin_device_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        created_at: str | None = None,
        source: str = "chat",
        message_id: str | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("invalid_transcript_role")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("empty_transcript_message")
        target_day = day_id or day_id_for()
        created = created_at or _utc_now()
        msg_id = message_id or uuid.uuid4().hex
        attachments_json = json.dumps(
            _normalize_attachments(attachments or []),
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_day(connection, target_day)
            state = connection.execute(
                "SELECT status FROM daily_transcript_days WHERE day_id = ?",
                (target_day,),
            ).fetchone()
            if state is None or state["status"] != "active":
                raise RuntimeError("daily_transcript_not_active")
            cursor = connection.execute(
                """
                INSERT INTO daily_transcript_messages
                    (message_id, day_id, role, text, request_id, client_id,
                     origin_device_id, attachments_json, created_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    target_day,
                    role,
                    clean_text,
                    request_id,
                    client_id,
                    origin_device_id,
                    attachments_json,
                    created,
                    source,
                ),
            )
            connection.execute(
                "UPDATE daily_transcript_days SET updated_at = ? WHERE day_id = ?",
                (_utc_now(), target_day),
            )
            seq = int(cursor.lastrowid)
            connection.commit()

        return _message_payload(
            seq=seq,
            message_id=msg_id,
            day_id=target_day,
            role=role,
            text=clean_text,
            request_id=request_id,
            client_id=client_id,
            origin_device_id=origin_device_id,
            attachments_json=attachments_json,
            created_at=created,
            source=source,
        )

    def list_messages(self, day_id: str | None = None) -> list[dict[str, Any]]:
        target_day = day_id or day_id_for()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT seq, message_id, day_id, role, text, request_id,
                       client_id, origin_device_id, attachments_json,
                       created_at, source
                FROM daily_transcript_messages
                WHERE day_id = ?
                ORDER BY seq ASC
                """,
                (target_day,),
            ).fetchall()
        return [
            _message_payload(**dict(row))
            for row in rows
        ]

    def list_pending_days(
        self,
        *,
        before_day_id: str | None = None,
        limit: int = 31,
    ) -> list[dict[str, Any]]:
        """Return active/frozen days that still require trusted-local NC."""
        params: list[Any] = []
        where = "d.status IN ('active','frozen')"
        if before_day_id:
            where += " AND d.day_id < ?"
            params.append(before_day_id)
        params.append(max(1, min(int(limit), 366)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT d.day_id, d.status, d.input_hash, d.created_at, d.updated_at,
                       d.frozen_at, COUNT(m.seq) AS message_count
                FROM daily_transcript_days d
                LEFT JOIN daily_transcript_messages m ON m.day_id=d.day_id
                WHERE {where}
                GROUP BY d.day_id, d.status, d.input_hash, d.created_at, d.updated_at, d.frozen_at
                HAVING COUNT(m.seq) > 0
                ORDER BY d.day_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def import_if_empty(
        self,
        messages: list[dict[str, Any]],
        *,
        day_id: str | None = None,
        source: str = "desktop_local_cache",
    ) -> list[dict[str, Any]]:
        target_day = day_id or day_id_for()
        sanitized: list[dict[str, Any]] = []
        for item in messages[:100]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            text = str(item.get("text") or "").strip()
            if role not in {"user", "assistant"} or not text:
                continue
            sanitized.append(
                {
                    "role": role,
                    "text": text[:24000],
                    "attachments": _normalize_attachments(item.get("attachments")),
                }
            )

        if not sanitized:
            return []

        imported: list[dict[str, Any]] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_day(connection, target_day)
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM daily_transcript_messages WHERE day_id = ?",
                (target_day,),
            ).fetchone()["count"]
            if int(count) != 0:
                connection.rollback()
                return []
            state = connection.execute(
                "SELECT status FROM daily_transcript_days WHERE day_id = ?",
                (target_day,),
            ).fetchone()
            if state is None or state["status"] != "active":
                connection.rollback()
                return []

            for item in sanitized:
                msg_id = uuid.uuid4().hex
                created = _utc_now()
                attachments_json = json.dumps(
                    item["attachments"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO daily_transcript_messages
                        (message_id, day_id, role, text, attachments_json,
                         created_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg_id,
                        target_day,
                        item["role"],
                        item["text"],
                        attachments_json,
                        created,
                        source,
                    ),
                )
                imported.append(
                    _message_payload(
                        seq=int(cursor.lastrowid),
                        message_id=msg_id,
                        day_id=target_day,
                        role=item["role"],
                        text=item["text"],
                        request_id=None,
                        client_id=None,
                        origin_device_id=None,
                        attachments_json=attachments_json,
                        created_at=created,
                        source=source,
                    )
                )
            connection.execute(
                "UPDATE daily_transcript_days SET updated_at = ? WHERE day_id = ?",
                (_utc_now(), target_day),
            )
            connection.commit()
        return imported

    def list_context_messages(
        self,
        day_id: str | None = None,
    ) -> list[dict[str, Any]]:
        target_day = day_id or day_id_for()
        with self._lock, self._connection() as connection:
            state = connection.execute(
                "SELECT context_floor_seq FROM daily_transcript_days WHERE day_id = ?",
                (target_day,),
            ).fetchone()
            floor = int(state["context_floor_seq"]) if state is not None else 0
            rows = connection.execute(
                """
                SELECT seq, message_id, day_id, role, text, request_id,
                       client_id, origin_device_id, attachments_json,
                       created_at, source
                FROM daily_transcript_messages
                WHERE day_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (target_day, floor),
            ).fetchall()
        return [_message_payload(**dict(row)) for row in rows]

    def mark_context_boundary(self, day_id: str | None = None) -> int:
        target_day = day_id or day_id_for()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_day(connection, target_day)
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq "
                "FROM daily_transcript_messages WHERE day_id = ?",
                (target_day,),
            ).fetchone()
            floor = int(row["max_seq"])
            connection.execute(
                """
                UPDATE daily_transcript_days
                SET context_floor_seq = ?, updated_at = ?
                WHERE day_id = ?
                """,
                (floor, _utc_now(), target_day),
            )
            connection.commit()
        return floor

    def freeze_day(self, day_id: str | None = None) -> dict[str, Any]:
        target_day = day_id or day_id_for()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_day(connection, target_day)
            state = connection.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = ?",
                (target_day,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT seq, message_id, day_id, role, text, request_id,
                       client_id, origin_device_id, attachments_json,
                       created_at, source
                FROM daily_transcript_messages
                WHERE day_id = ? ORDER BY seq ASC
                """,
                (target_day,),
            ).fetchall()
            messages = [_message_payload(**dict(row)) for row in rows]

            if state["status"] == "processed":
                # A successful NC run may already have purged the raw rows.  The
                # stored hash is therefore the authoritative idempotency key.
                connection.commit()
                return {
                    "day_id": target_day,
                    "status": "processed",
                    "input_hash": state["input_hash"],
                    "messages": messages,
                    "already_processed": True,
                }

            digest = transcript_input_hash(messages)
            now = _utc_now()
            if state["status"] == "active":
                connection.execute(
                    """
                    UPDATE daily_transcript_days
                    SET status = 'frozen', input_hash = ?, frozen_at = ?, updated_at = ?
                    WHERE day_id = ?
                    """,
                    (digest, now, now, target_day),
                )
            elif state["status"] == "frozen":
                if state["input_hash"] != digest:
                    connection.rollback()
                    raise RuntimeError("frozen_transcript_changed")
            else:
                connection.rollback()
                raise RuntimeError("invalid_transcript_state")
            connection.commit()
        return {
            "day_id": target_day,
            "status": "frozen",
            "input_hash": digest,
            "messages": messages,
            "already_processed": False,
        }

    def mark_processed(self, day_id: str, input_hash: str) -> bool:
        with self._lock, self._connection() as connection:
            state = connection.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = ?",
                (day_id,),
            ).fetchone()
            if (
                state is not None
                and state["status"] == "processed"
                and state["input_hash"] == input_hash
            ):
                return True
            cursor = connection.execute(
                """
                UPDATE daily_transcript_days
                SET status = 'processed', processed_at = ?, updated_at = ?
                WHERE day_id = ? AND status = 'frozen' AND input_hash = ?
                """,
                (_utc_now(), _utc_now(), day_id, input_hash),
            )
            connection.commit()
            return cursor.rowcount == 1

    def purge_processed(self, day_id: str, input_hash: str) -> int:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = ?",
                (day_id,),
            ).fetchone()
            if (
                state is None
                or state["status"] != "processed"
                or state["input_hash"] != input_hash
            ):
                connection.rollback()
                raise RuntimeError("transcript_not_safely_processed")
            cursor = connection.execute(
                "DELETE FROM daily_transcript_messages WHERE day_id = ?",
                (day_id,),
            )
            connection.commit()
            return int(cursor.rowcount)

    def acknowledge_processed(
        self,
        day_id: str,
        input_hash: str,
        *,
        purge: bool = True,
    ) -> dict[str, Any]:
        """Atomically verify the frozen receipt, mark processed, and purge raw rows."""
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = ?",
                (day_id,),
            ).fetchone()
            if state is None or state["input_hash"] != input_hash:
                connection.rollback()
                raise RuntimeError("transcript_receipt_mismatch")
            if state["status"] == "frozen":
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE daily_transcript_days
                    SET status='processed', processed_at=?, updated_at=?
                    WHERE day_id=? AND status='frozen' AND input_hash=?
                    """,
                    (now, now, day_id, input_hash),
                )
            elif state["status"] != "processed":
                connection.rollback()
                raise RuntimeError("transcript_receipt_mismatch")

            purged = 0
            if purge:
                cursor = connection.execute(
                    "DELETE FROM daily_transcript_messages WHERE day_id = ?",
                    (day_id,),
                )
                purged = int(cursor.rowcount)
            connection.commit()
            return {"marked": True, "purged": purged}


class PostgresDailyTranscriptStore:
    backend = "postgres"
    durable = True

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._lock = threading.RLock()
        self._initialize()

    @property
    def status(self) -> TranscriptStoreStatus:
        return TranscriptStoreStatus(self.backend, True)

    @staticmethod
    def _psycopg():
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL transcript storage requires psycopg[binary]"
            ) from exc
        return psycopg

    def _connect(self):
        return self._psycopg().connect(self.database_url)

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_transcript_days (
                    day_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active',
                    input_hash TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    frozen_at TIMESTAMPTZ,
                    processed_at TIMESTAMPTZ,
                    context_floor_seq BIGINT NOT NULL DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE daily_transcript_days
                ADD COLUMN IF NOT EXISTS context_floor_seq BIGINT NOT NULL DEFAULT 0
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_transcript_messages (
                    seq BIGSERIAL PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    day_id TEXT NOT NULL REFERENCES daily_transcript_days(day_id),
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    request_id TEXT,
                    client_id TEXT,
                    origin_device_id TEXT,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMPTZ NOT NULL,
                    source TEXT NOT NULL DEFAULT 'chat'
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_daily_transcript_day_seq
                ON daily_transcript_messages(day_id, seq)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_daily_transcript_request
                ON daily_transcript_messages(request_id)
                """
            )
            connection.commit()

    @staticmethod
    def _ensure_day(cursor, day_id: str) -> None:
        now = _utc_now()
        cursor.execute(
            """
            INSERT INTO daily_transcript_days
                (day_id, status, created_at, updated_at)
            VALUES (%s, 'active', %s, %s)
            ON CONFLICT (day_id) DO NOTHING
            """,
            (day_id, now, now),
        )

    @staticmethod
    def _row_payload(row: tuple[Any, ...]) -> dict[str, Any]:
        (
            seq,
            message_id,
            day_id,
            role,
            text,
            request_id,
            client_id,
            origin_device_id,
            attachments_json,
            created_at,
            source,
        ) = row
        return _message_payload(
            seq=int(seq),
            message_id=str(message_id),
            day_id=str(day_id),
            role=str(role),
            text=str(text),
            request_id=request_id,
            client_id=client_id,
            origin_device_id=origin_device_id,
            attachments_json=attachments_json,
            created_at=(
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at)
            ),
            source=str(source),
        )

    def append_message(self, **kwargs) -> dict[str, Any]:
        role = kwargs.get("role")
        text = str(kwargs.get("text") or "").strip()
        if role not in {"user", "assistant"}:
            raise ValueError("invalid_transcript_role")
        if not text:
            raise ValueError("empty_transcript_message")
        target_day = kwargs.get("day_id") or day_id_for()
        created = kwargs.get("created_at") or _utc_now()
        msg_id = kwargs.get("message_id") or uuid.uuid4().hex
        attachments_json = json.dumps(
            _normalize_attachments(kwargs.get("attachments") or []),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            self._ensure_day(cursor, target_day)
            cursor.execute(
                "SELECT status FROM daily_transcript_days WHERE day_id = %s FOR UPDATE",
                (target_day,),
            )
            state = cursor.fetchone()
            if state is None or state[0] != "active":
                raise RuntimeError("daily_transcript_not_active")
            cursor.execute(
                """
                INSERT INTO daily_transcript_messages
                    (message_id, day_id, role, text, request_id, client_id,
                     origin_device_id, attachments_json, created_at, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING seq
                """,
                (
                    msg_id,
                    target_day,
                    role,
                    text,
                    kwargs.get("request_id"),
                    kwargs.get("client_id"),
                    kwargs.get("origin_device_id"),
                    attachments_json,
                    created,
                    kwargs.get("source") or "chat",
                ),
            )
            seq = int(cursor.fetchone()[0])
            cursor.execute(
                "UPDATE daily_transcript_days SET updated_at = %s WHERE day_id = %s",
                (_utc_now(), target_day),
            )
            connection.commit()
        return _message_payload(
            seq=seq,
            message_id=msg_id,
            day_id=target_day,
            role=role,
            text=text,
            request_id=kwargs.get("request_id"),
            client_id=kwargs.get("client_id"),
            origin_device_id=kwargs.get("origin_device_id"),
            attachments_json=attachments_json,
            created_at=created,
            source=kwargs.get("source") or "chat",
        )

    def list_messages(self, day_id: str | None = None) -> list[dict[str, Any]]:
        target_day = day_id or day_id_for()
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT seq, message_id, day_id, role, text, request_id,
                       client_id, origin_device_id, attachments_json,
                       created_at, source
                FROM daily_transcript_messages
                WHERE day_id = %s ORDER BY seq ASC
                """,
                (target_day,),
            )
            rows = cursor.fetchall()
        return [self._row_payload(row) for row in rows]

    def list_pending_days(
        self,
        *,
        before_day_id: str | None = None,
        limit: int = 31,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "d.status IN ('active','frozen')"
        if before_day_id:
            where += " AND d.day_id < %s"
            params.append(before_day_id)
        params.append(max(1, min(int(limit), 366)))
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT d.day_id, d.status, d.input_hash, d.created_at, d.updated_at,
                       d.frozen_at, COUNT(m.seq) AS message_count
                FROM daily_transcript_days d
                LEFT JOIN daily_transcript_messages m ON m.day_id=d.day_id
                WHERE {where}
                GROUP BY d.day_id, d.status, d.input_hash, d.created_at, d.updated_at, d.frozen_at
                HAVING COUNT(m.seq) > 0
                ORDER BY d.day_id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cursor.fetchall()
        return [
            {
                "day_id": row[0],
                "status": row[1],
                "input_hash": row[2],
                "created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
                "updated_at": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]),
                "frozen_at": (row[5].isoformat() if row[5] is not None and hasattr(row[5], "isoformat") else (str(row[5]) if row[5] is not None else None)),
                "message_count": int(row[6] or 0),
            }
            for row in rows
        ]

    def import_if_empty(
        self,
        messages: list[dict[str, Any]],
        *,
        day_id: str | None = None,
        source: str = "desktop_local_cache",
    ) -> list[dict[str, Any]]:
        target_day = day_id or day_id_for()
        sanitized = [
            {
                "role": item.get("role"),
                "text": str(item.get("text") or "").strip()[:24000],
                "attachments": _normalize_attachments(item.get("attachments")),
            }
            for item in messages[:100]
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and str(item.get("text") or "").strip()
        ]
        if not sanitized:
            return []
        imported: list[dict[str, Any]] = []
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            self._ensure_day(cursor, target_day)
            cursor.execute(
                "SELECT status FROM daily_transcript_days WHERE day_id = %s FOR UPDATE",
                (target_day,),
            )
            state = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) FROM daily_transcript_messages WHERE day_id = %s",
                (target_day,),
            )
            if state is None or state[0] != "active" or int(cursor.fetchone()[0]) != 0:
                connection.rollback()
                return []
            for item in sanitized:
                msg_id = uuid.uuid4().hex
                created = _utc_now()
                attachments_json = json.dumps(
                    item["attachments"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cursor.execute(
                    """
                    INSERT INTO daily_transcript_messages
                        (message_id, day_id, role, text, attachments_json,
                         created_at, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING seq
                    """,
                    (
                        msg_id,
                        target_day,
                        item["role"],
                        item["text"],
                        attachments_json,
                        created,
                        source,
                    ),
                )
                imported.append(
                    _message_payload(
                        seq=int(cursor.fetchone()[0]),
                        message_id=msg_id,
                        day_id=target_day,
                        role=item["role"],
                        text=item["text"],
                        request_id=None,
                        client_id=None,
                        origin_device_id=None,
                        attachments_json=attachments_json,
                        created_at=created,
                        source=source,
                    )
                )
            cursor.execute(
                "UPDATE daily_transcript_days SET updated_at = %s WHERE day_id = %s",
                (_utc_now(), target_day),
            )
            connection.commit()
        return imported

    def list_context_messages(
        self,
        day_id: str | None = None,
    ) -> list[dict[str, Any]]:
        target_day = day_id or day_id_for()
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT context_floor_seq FROM daily_transcript_days WHERE day_id = %s",
                (target_day,),
            )
            state = cursor.fetchone()
            floor = int(state[0]) if state is not None else 0
            cursor.execute(
                """
                SELECT seq, message_id, day_id, role, text, request_id,
                       client_id, origin_device_id, attachments_json,
                       created_at, source
                FROM daily_transcript_messages
                WHERE day_id = %s AND seq > %s
                ORDER BY seq ASC
                """,
                (target_day, floor),
            )
            rows = cursor.fetchall()
        return [self._row_payload(row) for row in rows]

    def mark_context_boundary(self, day_id: str | None = None) -> int:
        target_day = day_id or day_id_for()
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            self._ensure_day(cursor, target_day)
            cursor.execute(
                "SELECT status FROM daily_transcript_days WHERE day_id = %s FOR UPDATE",
                (target_day,),
            )
            cursor.fetchone()
            cursor.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM daily_transcript_messages WHERE day_id = %s",
                (target_day,),
            )
            floor = int(cursor.fetchone()[0])
            cursor.execute(
                """
                UPDATE daily_transcript_days
                SET context_floor_seq = %s, updated_at = %s
                WHERE day_id = %s
                """,
                (floor, _utc_now(), target_day),
            )
            connection.commit()
        return floor

    def freeze_day(self, day_id: str | None = None) -> dict[str, Any]:
        target_day = day_id or day_id_for()
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            self._ensure_day(cursor, target_day)
            cursor.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = %s FOR UPDATE",
                (target_day,),
            )
            state = cursor.fetchone()
            cursor.execute(
                """
                SELECT seq, message_id, day_id, role, text, request_id,
                       client_id, origin_device_id, attachments_json,
                       created_at, source
                FROM daily_transcript_messages
                WHERE day_id = %s ORDER BY seq ASC
                """,
                (target_day,),
            )
            messages = [self._row_payload(row) for row in cursor.fetchall()]

            if state[0] == "processed":
                connection.commit()
                return {
                    "day_id": target_day,
                    "status": "processed",
                    "input_hash": state[1],
                    "messages": messages,
                    "already_processed": True,
                }

            digest = transcript_input_hash(messages)
            now = _utc_now()
            if state[0] == "active":
                cursor.execute(
                    """
                    UPDATE daily_transcript_days
                    SET status = 'frozen', input_hash = %s, frozen_at = %s, updated_at = %s
                    WHERE day_id = %s
                    """,
                    (digest, now, now, target_day),
                )
            elif state[0] == "frozen":
                if state[1] != digest:
                    connection.rollback()
                    raise RuntimeError("frozen_transcript_changed")
            else:
                connection.rollback()
                raise RuntimeError("invalid_transcript_state")
            connection.commit()
        return {
            "day_id": target_day,
            "status": "frozen",
            "input_hash": digest,
            "messages": messages,
            "already_processed": False,
        }

    def mark_processed(self, day_id: str, input_hash: str) -> bool:
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = %s",
                (day_id,),
            )
            state = cursor.fetchone()
            if state is not None and state[0] == "processed" and state[1] == input_hash:
                return True
            cursor.execute(
                """
                UPDATE daily_transcript_days
                SET status = 'processed', processed_at = %s, updated_at = %s
                WHERE day_id = %s AND status = 'frozen' AND input_hash = %s
                """,
                (_utc_now(), _utc_now(), day_id, input_hash),
            )
            changed = cursor.rowcount == 1
            connection.commit()
            return changed

    def purge_processed(self, day_id: str, input_hash: str) -> int:
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = %s FOR UPDATE",
                (day_id,),
            )
            state = cursor.fetchone()
            if state is None or state[0] != "processed" or state[1] != input_hash:
                connection.rollback()
                raise RuntimeError("transcript_not_safely_processed")
            cursor.execute(
                "DELETE FROM daily_transcript_messages WHERE day_id = %s",
                (day_id,),
            )
            count = int(cursor.rowcount)
            connection.commit()
            return count

    def acknowledge_processed(
        self,
        day_id: str,
        input_hash: str,
        *,
        purge: bool = True,
    ) -> dict[str, Any]:
        """Atomically verify the frozen receipt, mark processed, and purge raw rows."""
        with self._lock, self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, input_hash FROM daily_transcript_days WHERE day_id = %s FOR UPDATE",
                (day_id,),
            )
            state = cursor.fetchone()
            if state is None or state[1] != input_hash:
                connection.rollback()
                raise RuntimeError("transcript_receipt_mismatch")
            if state[0] == "frozen":
                now = _utc_now()
                cursor.execute(
                    """
                    UPDATE daily_transcript_days
                    SET status='processed', processed_at=%s, updated_at=%s
                    WHERE day_id=%s AND status='frozen' AND input_hash=%s
                    """,
                    (now, now, day_id, input_hash),
                )
            elif state[0] != "processed":
                connection.rollback()
                raise RuntimeError("transcript_receipt_mismatch")

            purged = 0
            if purge:
                cursor.execute(
                    "DELETE FROM daily_transcript_messages WHERE day_id = %s",
                    (day_id,),
                )
                purged = int(cursor.rowcount)
            connection.commit()
            return {"marked": True, "purged": purged}


def build_daily_transcript_store(
    *,
    deployment_mode: str | None = None,
):
    """Build the transcript backend without hard-coding a machine path."""

    mode = (deployment_mode or os.getenv("EPIS_DEPLOYMENT") or "local").strip().lower()
    database_url = (
        os.getenv("EPIS_DAILY_TRANSCRIPT_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()

    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresDailyTranscriptStore(database_url)

    if database_url.startswith("sqlite:///"):
        path = database_url[len("sqlite:///") :]
        return SqliteDailyTranscriptStore(path, durable=True)

    explicit_path = (os.getenv("EPIS_DAILY_TRANSCRIPT_SQLITE_PATH") or "").strip()
    if explicit_path:
        return SqliteDailyTranscriptStore(explicit_path, durable=mode != "cloud")

    base = Path(
        os.getenv("EPIS_CLOUD_RUNTIME_DIR")
        or (Path(tempfile.gettempdir()) / "epis-cloud-runtime")
    )
    return SqliteDailyTranscriptStore(
        base / "daily-transcript.sqlite3",
        durable=mode != "cloud",
    )
