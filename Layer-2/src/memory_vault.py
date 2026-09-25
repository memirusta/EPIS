"""Trusted local long-term memory vault for EPIS.

The vault is the authoritative durable store for distilled personal memory.
Raw same-day conversation remains on the shared server only until a verified
Nightly Recalculation (NC) commit.  The vault itself is local to the trusted
user device and is never created on the cloud control plane.

Sensitive free-text fields are encrypted with EPIS' existing local cipher.
SQLite page-level encryption is intentionally not claimed here; the database
schema/metadata remains visible while personal text values are field-encrypted.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable
import uuid


_SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"[\wçğıöşüÇĞİÖŞÜ-]{2,}", re.UNICODE)
_STOPWORDS = {
    "acaba", "ama", "artık", "bana", "ben", "beni", "benim", "bir", "biri",
    "biz", "bu", "bunu", "da", "de", "daha", "diye", "en", "gibi", "için",
    "ile", "mi", "mı", "mu", "mü", "nasıl", "ne", "neden", "o", "olan", "olarak",
    "onu", "sen", "şey", "şu", "ve", "veya", "ya", "yani",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_vault_path() -> Path:
    explicit = (os.getenv("EPIS_MEMORY_VAULT_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    if os.name == "nt":
        base = (os.getenv("LOCALAPPDATA") or "").strip()
        if base:
            return Path(base) / "EPIS" / "memory-vault.sqlite3"

    xdg = (os.getenv("XDG_DATA_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "EPIS" / "memory-vault.sqlite3"
    return Path.home() / ".local" / "share" / "EPIS" / "memory-vault.sqlite3"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_fingerprint(kind: str, subject: str, content: str) -> str:
    blob = f"{kind.strip().casefold()}\n{subject.strip().casefold()}\n{content.strip().casefold()}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(value or "")
        if token.casefold() not in _STOPWORDS
    }


class LocalMemoryVault:
    """SQLite-backed, local-only long-term memory store.

    ``cipher`` must expose ``encrypt_str`` and ``decrypt_str``.  If omitted,
    EPIS' existing ``crypto_layer.get_cipher`` is loaded lazily.  Lazy loading
    keeps unit tests independent from the host key store while production stays
    fail-closed on its normal crypto implementation.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        cipher=None,
    ):
        self.path = Path(path or default_vault_path()).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if cipher is None:
            from crypto_layer import get_cipher
            cipher = get_cipher()
        self.cipher = cipher
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vault_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT 'user',
                    content_fingerprint TEXT NOT NULL,
                    content_enc TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_day TEXT,
                    source_input_hash TEXT,
                    provenance TEXT NOT NULL DEFAULT 'nightly',
                    valid_from TEXT,
                    valid_to TEXT,
                    superseded_by TEXT REFERENCES memories(id),
                    user_authoritative INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, subject, content_fingerprint, source_day)
                );

                CREATE TABLE IF NOT EXISTS people (
                    id TEXT PRIMARY KEY,
                    canonical_name_enc TEXT NOT NULL,
                    name_fingerprint TEXT NOT NULL UNIQUE,
                    aliases_enc TEXT NOT NULL DEFAULT '',
                    notes_enc TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_day TEXT,
                    source_input_hash TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL REFERENCES people(id),
                    relation_enc TEXT NOT NULL,
                    notes_enc TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_day TEXT,
                    source_input_hash TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    superseded_by TEXT REFERENCES relationships(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    day_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    summary_enc TEXT NOT NULL,
                    analysis_enc TEXT NOT NULL,
                    epis_voice_enc TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(day_id, input_hash)
                );

                CREATE TABLE IF NOT EXISTS habits (
                    id TEXT PRIMARY KEY,
                    name_fingerprint TEXT NOT NULL,
                    name_enc TEXT NOT NULL,
                    state TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_day TEXT,
                    source_input_hash TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    valid_from TEXT,
                    valid_to TEXT,
                    superseded_by TEXT REFERENCES habits(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(name_fingerprint, state, source_day)
                );

                CREATE TABLE IF NOT EXISTS memory_evidence (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    episode_id TEXT REFERENCES episodes(id) ON DELETE CASCADE,
                    day_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    message_ids_json TEXT NOT NULL DEFAULT '[]',
                    evidence_enc TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nightly_runs (
                    day_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    started_at TEXT NOT NULL,
                    committed_at TEXT,
                    cloud_ack_at TEXT,
                    purge_count INTEGER,
                    error_enc TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(day_id, input_hash)
                );

                CREATE TABLE IF NOT EXISTS memory_changes (
                    id TEXT PRIMARY KEY,
                    day_id TEXT,
                    input_hash TEXT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memories_source_day
                    ON memories(source_day, updated_at);
                CREATE INDEX IF NOT EXISTS idx_memories_kind
                    ON memories(kind, updated_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_day
                    ON memory_evidence(day_id, input_hash);
                CREATE INDEX IF NOT EXISTS idx_habits_source_day
                    ON habits(source_day, updated_at);
                """
            )
            conn.execute(
                """
                INSERT INTO vault_meta(key, value, updated_at)
                VALUES('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(_SCHEMA_VERSION), _utc_now()),
            )

    @property
    def schema_version(self) -> int:
        return _SCHEMA_VERSION

    def _enc(self, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = _json(value)
        return self.cipher.encrypt_str(value)

    def _dec(self, value: str | None) -> str:
        return self.cipher.decrypt_str(value or "")

    def run_state(self, day_id: str, input_hash: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM nightly_runs WHERE day_id=? AND input_hash=?",
                (day_id, input_hash),
            ).fetchone()
        return dict(row) if row else None

    def is_run_committed(self, day_id: str, input_hash: str) -> bool:
        state = self.run_state(day_id, input_hash)
        return bool(state and state.get("status") in {"committed", "cloud_acked"})

    def note_run_started(self, day_id: str, input_hash: str, run_id: str) -> None:
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO nightly_runs(day_id, input_hash, status, run_id, started_at)
                VALUES(?, ?, 'processing', ?, ?)
                ON CONFLICT(day_id, input_hash) DO UPDATE SET
                    run_id=excluded.run_id,
                    started_at=CASE
                        WHEN nightly_runs.status IN ('committed','cloud_acked')
                        THEN nightly_runs.started_at ELSE excluded.started_at END,
                    status=CASE
                        WHEN nightly_runs.status IN ('committed','cloud_acked')
                        THEN nightly_runs.status ELSE 'processing' END,
                    error_enc=''
                """,
                (day_id, input_hash, run_id, now),
            )

    def note_run_failed(self, day_id: str, input_hash: str, error: str) -> None:
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO nightly_runs(day_id, input_hash, status, started_at, error_enc)
                VALUES(?, ?, 'failed', ?, ?)
                ON CONFLICT(day_id, input_hash) DO UPDATE SET
                    status=CASE WHEN nightly_runs.status IN ('committed','cloud_acked')
                                THEN nightly_runs.status ELSE 'failed' END,
                    error_enc=CASE WHEN nightly_runs.status IN ('committed','cloud_acked')
                                   THEN nightly_runs.error_enc ELSE excluded.error_enc END
                """,
                (day_id, input_hash, now, self._enc(error[:4000])),
            )

    def mark_cloud_acked(self, day_id: str, input_hash: str, purge_count: int) -> None:
        with self._lock, self._connection() as conn:
            changed = conn.execute(
                """
                UPDATE nightly_runs
                SET status='cloud_acked', cloud_ack_at=?, purge_count=?
                WHERE day_id=? AND input_hash=? AND status IN ('committed','cloud_acked')
                """,
                (_utc_now(), int(purge_count), day_id, input_hash),
            ).rowcount
            if changed != 1:
                raise RuntimeError("nightly_run_not_committed")

    def apply_nightly_result(
        self,
        *,
        day_id: str,
        input_hash: str,
        run_id: str,
        summary: str,
        analysis: dict[str, Any],
        epis_voice: str = "",
        transcript_messages: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Atomically commit distilled NC output.

        This is the local safety boundary: the caller may acknowledge/purge the
        cloud transcript only after this transaction returns successfully.
        """
        message_ids = [
            str(item.get("message_id") or "")
            for item in transcript_messages
            if str(item.get("message_id") or "")
        ]
        evidence_text = "\n".join(
            f"{item.get('role')}: {str(item.get('text') or '').strip()}"
            for item in transcript_messages
            if str(item.get("text") or "").strip()
        )[:24000]
        now = _utc_now()

        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT status FROM nightly_runs WHERE day_id=? AND input_hash=?",
                (day_id, input_hash),
            ).fetchone()
            if state and state[0] in {"committed", "cloud_acked"}:
                return {"already_committed": True, "day_id": day_id, "input_hash": input_hash}

            conn.execute(
                """
                INSERT INTO nightly_runs(day_id, input_hash, status, run_id, started_at)
                VALUES(?, ?, 'processing', ?, ?)
                ON CONFLICT(day_id, input_hash) DO UPDATE SET
                    status='processing', run_id=excluded.run_id, error_enc=''
                """,
                (day_id, input_hash, run_id, now),
            )

            episode_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT OR IGNORE INTO episodes
                    (id, day_id, input_hash, summary_enc, analysis_enc, epis_voice_enc, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    day_id,
                    input_hash,
                    self._enc(summary[:12000]),
                    self._enc(analysis),
                    self._enc(epis_voice[:4000]),
                    now,
                ),
            )
            existing_episode = conn.execute(
                "SELECT id FROM episodes WHERE day_id=? AND input_hash=?",
                (day_id, input_hash),
            ).fetchone()
            episode_id = str(existing_episode[0])

            candidates: list[dict[str, Any]] = []

            # Explicit durable candidates produced by NC are preferred.  They
            # carry per-message evidence ids and can represent stable facts,
            # preferences, projects, goals or commitments without pretending
            # calculated observations are user-authored facts.
            for item in analysis.get("memory_candidates") or []:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                kind = str(item.get("kind") or "fact").strip().lower()[:40] or "fact"
                subject = str(item.get("subject") or "user").strip()[:160] or "user"
                try:
                    confidence = float(item.get("confidence", 0.75))
                except (TypeError, ValueError):
                    confidence = 0.75
                evidence_ids = [
                    str(value) for value in (item.get("evidence_message_ids") or [])
                    if str(value or "")
                ][:24]
                candidates.append({
                    "kind": kind,
                    "subject": subject,
                    "content": content,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "provenance": "nightly:memory_candidate",
                    "evidence_ids": evidence_ids,
                })

            for item in analysis.get("key_events") or []:
                if isinstance(item, str) and item.strip():
                    candidates.append({
                        "kind": "event",
                        "subject": "user",
                        "content": item.strip(),
                        "confidence": 0.72,
                        "provenance": "nightly:key_event",
                        "evidence_ids": [],
                    })

            # Keep a few calculated observations separately.  They are useful
            # context but intentionally lower-confidence than explicit facts.
            for item in analysis.get("behavioral_insights") or []:
                if isinstance(item, str) and item.strip():
                    candidates.append({
                        "kind": "identity_calculated",
                        "subject": "user",
                        "content": item.strip(),
                        "confidence": 0.58,
                        "provenance": "nightly:calculated",
                        "evidence_ids": [],
                    })
            tomorrow = analysis.get("tomorrow_context")
            if isinstance(tomorrow, str) and tomorrow.strip():
                candidates.append({
                    "kind": "commitment_context",
                    "subject": "user",
                    "content": tomorrow.strip(),
                    "confidence": 0.70,
                    "provenance": "nightly:tomorrow",
                    "evidence_ids": [],
                })

            message_by_id = {
                str(item.get("message_id") or ""): item
                for item in transcript_messages
                if str(item.get("message_id") or "")
            }
            memory_evidence_rows: list[tuple[str, list[str], str]] = []
            memory_ids: list[str] = []
            for candidate in candidates[:30]:
                kind = candidate["kind"]
                subject = candidate["subject"]
                content = candidate["content"]
                confidence = candidate["confidence"]
                provenance = candidate["provenance"]
                fingerprint = _content_fingerprint(kind, subject, content)
                memory_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memories
                        (id, kind, subject, content_fingerprint, content_enc,
                         confidence, source_day, source_input_hash, provenance,
                         valid_from, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        kind,
                        subject,
                        fingerprint,
                        self._enc(content),
                        max(0.0, min(1.0, float(confidence))),
                        day_id,
                        input_hash,
                        provenance,
                        day_id,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT id FROM memories
                    WHERE kind=? AND subject=? AND content_fingerprint=? AND source_day=?
                    """,
                    (kind, subject, fingerprint, day_id),
                ).fetchone()
                stored_id = str(row[0])
                memory_ids.append(stored_id)
                candidate_ids = [
                    mid for mid in candidate.get("evidence_ids", []) if mid in message_by_id
                ]
                if candidate_ids:
                    candidate_evidence = "\n".join(
                        f"{message_by_id[mid].get('role')}: {str(message_by_id[mid].get('text') or '').strip()}"
                        for mid in candidate_ids
                    )[:12000]
                else:
                    candidate_ids = list(message_ids)
                    candidate_evidence = evidence_text
                memory_evidence_rows.append((stored_id, candidate_ids, candidate_evidence))
                conn.execute(
                    """
                    INSERT INTO memory_changes
                        (id, day_id, input_hash, entity_type, entity_id, action, after_json, created_at)
                    VALUES(?, ?, ?, 'memory', ?, 'upsert', ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        day_id,
                        input_hash,
                        stored_id,
                        _json({"kind": kind, "subject": subject, "confidence": confidence}),
                        now,
                    ),
                )

            habits = analysis.get("habits") or {}
            for state_name in ("confirmed", "new_detected", "changed", "broken"):
                for item in habits.get(state_name) or []:
                    if not isinstance(item, str) or not item.strip():
                        continue
                    name = item.strip()
                    fingerprint = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO habits
                            (id, name_fingerprint, name_enc, state, confidence,
                             source_day, source_input_hash, valid_from, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            fingerprint,
                            self._enc(name),
                            state_name,
                            0.70,
                            day_id,
                            input_hash,
                            day_id,
                            now,
                            now,
                        ),
                    )

            conn.execute(
                """
                INSERT INTO memory_evidence
                    (id, episode_id, day_id, input_hash, message_ids_json, evidence_enc, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    episode_id,
                    day_id,
                    input_hash,
                    _json(message_ids),
                    self._enc(evidence_text),
                    now,
                ),
            )
            seen_evidence: set[str] = set()
            for memory_id, candidate_ids, candidate_evidence in memory_evidence_rows:
                if memory_id in seen_evidence:
                    continue
                seen_evidence.add(memory_id)
                conn.execute(
                    """
                    INSERT INTO memory_evidence
                        (id, memory_id, day_id, input_hash, message_ids_json, evidence_enc, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        memory_id,
                        day_id,
                        input_hash,
                        _json(candidate_ids),
                        self._enc(candidate_evidence),
                        now,
                    ),
                )

            conn.execute(
                """
                UPDATE nightly_runs
                SET status='committed', committed_at=?, error_enc=''
                WHERE day_id=? AND input_hash=?
                """,
                (now, day_id, input_hash),
            )

        return {
            "already_committed": False,
            "day_id": day_id,
            "input_hash": input_hash,
            "episode_id": episode_id,
            "memory_count": len(set(memory_ids)),
        }

    def _memory_rows(self, limit: int = 160) -> list[sqlite3.Row]:
        with self._lock, self._connection() as conn:
            return conn.execute(
                """
                SELECT id, kind, subject, content_enc, confidence, source_day,
                       provenance, valid_from, valid_to, superseded_by,
                       user_authoritative, metadata_json, updated_at
                FROM memories
                WHERE superseded_by IS NULL AND valid_to IS NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()

    def search_relevant(self, query: str, *, limit: int = 8, scan: int = 180) -> list[dict[str, Any]]:
        q_tokens = _tokens(query)
        rows = self._memory_rows(scan)
        scored: list[tuple[float, dict[str, Any]]] = []
        total = max(1, len(rows))
        for idx, row in enumerate(rows):
            content = self._dec(row["content_enc"])
            c_tokens = _tokens(content)
            overlap = len(q_tokens & c_tokens)
            if q_tokens and overlap == 0:
                continue
            lexical = overlap / max(1, len(q_tokens)) if q_tokens else 0.0
            recency = 1.0 - (idx / total)
            confidence = float(row["confidence"] or 0.0)
            score = lexical * 0.72 + confidence * 0.18 + recency * 0.10
            scored.append(
                (
                    score,
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "subject": row["subject"],
                        "content": content,
                        "confidence": confidence,
                        "source_day": row["source_day"],
                        "provenance": row["provenance"],
                        "user_authoritative": bool(row["user_authoritative"]),
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[: max(1, min(int(limit), 30))]]

    def recent_memories(self, *, limit: int = 5) -> list[dict[str, Any]]:
        result = []
        for row in self._memory_rows(limit):
            result.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "subject": row["subject"],
                    "content": self._dec(row["content_enc"]),
                    "confidence": float(row["confidence"] or 0.0),
                    "source_day": row["source_day"],
                    "provenance": row["provenance"],
                    "user_authoritative": bool(row["user_authoritative"]),
                }
            )
        return result

    def upsert_person(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        notes: str = "",
        confidence: float = 1.0,
        source_day: str | None = None,
        source_input_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("person_name_required")
        fingerprint = hashlib.sha256(clean_name.casefold().encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT id FROM people WHERE name_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            person_id = str(row[0]) if row else uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO people
                    (id, canonical_name_enc, name_fingerprint, aliases_enc, notes_enc,
                     confidence, source_day, source_input_hash, metadata_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name_fingerprint) DO UPDATE SET
                    canonical_name_enc=excluded.canonical_name_enc,
                    aliases_enc=excluded.aliases_enc,
                    notes_enc=CASE WHEN excluded.notes_enc != '' THEN excluded.notes_enc ELSE people.notes_enc END,
                    confidence=MAX(people.confidence, excluded.confidence),
                    source_day=COALESCE(excluded.source_day, people.source_day),
                    source_input_hash=COALESCE(excluded.source_input_hash, people.source_input_hash),
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    person_id,
                    self._enc(clean_name),
                    fingerprint,
                    self._enc(aliases or []),
                    self._enc(notes),
                    max(0.0, min(1.0, float(confidence))),
                    source_day,
                    source_input_hash,
                    _json(metadata or {}),
                    now,
                    now,
                ),
            )
        return person_id

    def list_people(self, *, limit: int = 200) -> dict[str, Any]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT canonical_name_enc, aliases_enc, notes_enc, confidence, metadata_json
                FROM people ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        people: dict[str, Any] = {}
        for row in rows:
            name = self._dec(row["canonical_name_enc"])
            if not name:
                continue
            try:
                aliases = json.loads(self._dec(row["aliases_enc"]) or "[]")
            except json.JSONDecodeError:
                aliases = []
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            people[name] = {
                "aliases": aliases if isinstance(aliases, list) else [],
                "notes": self._dec(row["notes_enc"]),
                "relation": str(metadata.get("legacy_relation") or ""),
                "confidence": float(row["confidence"] or 0.0),
            }
        return {"people": people}

    def migrate_legacy_once(self, memory_dir: str | os.PathLike[str], identity_dir: str | os.PathLike[str]) -> dict[str, int]:
        marker = "legacy_migration_v1"
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT value FROM vault_meta WHERE key=?", (marker,)).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except Exception:
                    return {"already_migrated": 1}

        memory_dir = Path(memory_dir)
        identity_dir = Path(identity_dir)
        stats = {"people": 0, "episodes": 0, "calculated": 0, "habits": 0}

        # The existing encrypted people.json is read through crypto_layer's helper.
        try:
            from crypto_layer import load_json_file
            people_data = load_json_file(str(memory_dir / "people.json")) or {}
        except Exception:
            people_data = {}
        for name, info in (people_data.get("people") or {}).items():
            if not isinstance(info, dict):
                info = {"notes": str(info)}
            self.upsert_person(
                name,
                aliases=list(info.get("aliases") or []),
                notes=str(info.get("notes") or ""),
                confidence=1.0,
                metadata={"legacy_relation": info.get("relation")},
            )
            stats["people"] += 1

        weekly_path = memory_dir / "weekly.json"
        if weekly_path.is_file():
            try:
                weekly = json.loads(weekly_path.read_text(encoding="utf-8"))
            except Exception:
                weekly = {}
            for day in weekly.get("days") or []:
                day_id = str(day.get("date") or "").strip()
                summary = str(day.get("summary") or "").strip()
                if not day_id or not summary:
                    continue
                analysis = day.get("analysis") if isinstance(day.get("analysis"), dict) else {}
                legacy_hash = "legacy:" + hashlib.sha256(day_id.encode("utf-8")).hexdigest()
                now = _utc_now()
                with self._lock, self._connection() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO episodes
                            (id, day_id, input_hash, summary_enc, analysis_enc, epis_voice_enc, created_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            day_id,
                            legacy_hash,
                            self._enc(summary),
                            self._enc(analysis),
                            self._enc(str(day.get("epis_voice") or "")),
                            now,
                        ),
                    )
                stats["episodes"] += 1

        calc_path = identity_dir / "identity_calculated.json"
        if calc_path.is_file():
            try:
                calc = json.loads(calc_path.read_text(encoding="utf-8"))
            except Exception:
                calc = {}
            for row in calc.get("trait_history") or []:
                if not isinstance(row, dict):
                    continue
                content = _json(row)
                day_id = str(row.get("date") or "") or None
                self._insert_legacy_memory(
                    "identity_calculated",
                    content,
                    source_day=day_id,
                    confidence=0.65,
                )
                stats["calculated"] += 1

        habits_path = identity_dir.parent / "habits" / "habit_log.json"
        if habits_path.is_file():
            try:
                habit_log = json.loads(habits_path.read_text(encoding="utf-8"))
            except Exception:
                habit_log = {}
            for entry in habit_log.get("entries") or []:
                day_id = str(entry.get("date") or "") or None
                for state_name in ("confirmed", "new_detected", "changed", "broken"):
                    for item in entry.get(state_name) or []:
                        if isinstance(item, str) and item.strip():
                            self._insert_legacy_habit(item.strip(), state_name, day_id)
                            stats["habits"] += 1

        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vault_meta(key, value, updated_at) VALUES(?, ?, ?)",
                (marker, _json(stats), _utc_now()),
            )
        return stats

    def _insert_legacy_memory(self, kind: str, content: str, *, source_day: str | None, confidence: float) -> None:
        fingerprint = _content_fingerprint(kind, "user", content)
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO memories
                    (id, kind, subject, content_fingerprint, content_enc, confidence,
                     source_day, provenance, valid_from, created_at, updated_at)
                VALUES(?, ?, 'user', ?, ?, ?, ?, 'legacy_migration', ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    kind,
                    fingerprint,
                    self._enc(content),
                    confidence,
                    source_day,
                    source_day,
                    now,
                    now,
                ),
            )

    def _insert_legacy_habit(self, name: str, state: str, source_day: str | None) -> None:
        fingerprint = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO habits
                    (id, name_fingerprint, name_enc, state, confidence,
                     source_day, valid_from, created_at, updated_at)
                VALUES(?, ?, ?, ?, 0.65, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, fingerprint, self._enc(name), state, source_day, source_day, now, now),
            )
