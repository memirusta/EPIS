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
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable
import uuid


_SCHEMA_VERSION = 3  # whatsapp_auto_conversation_v1
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


def _recipient_opted_out(value: str) -> bool:
    folded = str(value or "").translate(str.maketrans({
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
        "Ç": "c", "Ğ": "g", "İ": "i", "I": "i", "Ö": "o", "Ş": "s", "Ü": "u",
    })).casefold()
    folded = re.sub(r"[^\w\s']", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if folded in {"dur", "yazma", "mesaj atma", "bana yazma", "cevap verme",
                  "konusmayi birak", "istemiyorum", "mesaj gonderme", "stop",
                  "don't message me", "do not message me", "stop messaging me",
                  "don't reply", "leave me alone"}:
        return True
    return any(phrase in folded for phrase in (
        "bana yazma", "mesaj atma", "mesaj gonderme", "cevap verme",
        "konusmayi birak", "don't message me", "do not message me",
        "stop messaging me", "don't reply", "leave me alone",
    )) or bool(re.search(r"\b(?:dur|yazma|istemiyorum|stop)\b", folded))


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

                CREATE TABLE IF NOT EXISTS external_outreach (
                    outreach_id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL REFERENCES people(id),
                    provider TEXT NOT NULL,
                    provider_contact_ref_enc TEXT NOT NULL,
                    outbound_message_enc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_message_ref_enc TEXT NOT NULL DEFAULT '',
                    provider_message_fingerprint TEXT,
                    error_enc TEXT NOT NULL DEFAULT '',
                    reply_memory_id TEXT REFERENCES memories(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    replied_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_external_outreach_provider_message
                ON external_outreach(
                    provider,
                    provider_message_fingerprint
                )
                WHERE provider_message_fingerprint IS NOT NULL;

                CREATE TABLE IF NOT EXISTS external_outreach_reply_receipts (
                    provider TEXT NOT NULL,
                    inbound_message_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outreach_id TEXT,
                    memory_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(
                        provider,
                        inbound_message_fingerprint
                    )
                );

                CREATE TABLE IF NOT EXISTS whatsapp_auto_conversations (
                    session_id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL REFERENCES people(id),
                    provider_contact_ref_enc TEXT NOT NULL,
                    goal_enc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    max_auto_replies INTEGER NOT NULL,
                    auto_reply_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL, -- empty means no time limit
                    initial_outreach_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    stopped_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_whatsapp_auto_person
                    ON whatsapp_auto_conversations(person_id, status);

                CREATE TABLE IF NOT EXISTS whatsapp_auto_reply_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES whatsapp_auto_conversations(session_id),
                    inbound_fingerprint TEXT NOT NULL UNIQUE,
                    inbound_content_enc TEXT NOT NULL,
                    generated_content_enc TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    outreach_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_auto_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES whatsapp_auto_conversations(session_id),
                    direction TEXT NOT NULL,
                    content_enc TEXT NOT NULL,
                    created_at TEXT NOT NULL
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

    def configure_person_whatsapp(
        self,
        name: str,
        *,
        contact_ref: str,
        aliases: list[str] | None = None,
        allowlisted: bool = True,
    ) -> dict[str, Any]:
        """Merge private WhatsApp routing metadata into one person.

        Existing notes, confidence, provenance metadata and unrelated
        metadata keys are preserved.
        """

        clean_name = str(
            name or ""
        ).strip()

        clean_contact_ref = str(
            contact_ref or ""
        ).strip()

        if not clean_name:
            raise ValueError(
                "person_name_required"
            )

        if not clean_contact_ref:
            raise ValueError(
                "contact_ref_required"
            )

        clean_aliases = []

        seen_aliases = set()

        for raw in aliases or []:
            alias = str(
                raw or ""
            ).strip()

            key = alias.casefold()

            if (
                not alias
                or key in seen_aliases
                or key
                == clean_name.casefold()
            ):
                continue

            seen_aliases.add(
                key
            )

            clean_aliases.append(
                alias
            )

        fingerprint = hashlib.sha256(
            clean_name.casefold().encode(
                "utf-8"
            )
        ).hexdigest()

        now = _utc_now()

        previous_contact_ref = ""

        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    aliases_enc,
                    metadata_json
                FROM people
                WHERE name_fingerprint=?
                """,
                (
                    fingerprint,
                ),
            ).fetchone()

            if row is None:
                person_id = (
                    uuid.uuid4().hex
                )

                metadata = {
                    "whatsapp": {
                        "allowlisted":
                            bool(
                                allowlisted
                            ),

                        "contact_ref":
                            clean_contact_ref,
                    }
                }

                conn.execute(
                    """
                    INSERT INTO people(
                        id,
                        canonical_name_enc,
                        name_fingerprint,
                        aliases_enc,
                        notes_enc,
                        confidence,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    VALUES(
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        1.0,
                        ?,
                        ?,
                        ?
                    )
                    """,
                    (
                        person_id,
                        self._enc(
                            clean_name
                        ),
                        fingerprint,
                        self._enc(
                            clean_aliases
                        ),
                        self._enc(""),
                        _json(metadata),
                        now,
                        now,
                    ),
                )

            else:
                person_id = str(
                    row["id"]
                )

                try:
                    existing_aliases = (
                        json.loads(
                            self._dec(
                                row[
                                    "aliases_enc"
                                ]
                            )
                            or "[]"
                        )
                    )
                except Exception:
                    existing_aliases = []

                if not isinstance(
                    existing_aliases,
                    list,
                ):
                    existing_aliases = []

                merged_aliases = []
                merged_seen = set()

                for raw in [
                    *existing_aliases,
                    *clean_aliases,
                ]:
                    alias = str(
                        raw or ""
                    ).strip()

                    key = alias.casefold()

                    if (
                        not alias
                        or key
                        in merged_seen
                        or key
                        == clean_name.casefold()
                    ):
                        continue

                    merged_seen.add(
                        key
                    )

                    merged_aliases.append(
                        alias
                    )

                try:
                    metadata = json.loads(
                        row[
                            "metadata_json"
                        ]
                        or "{}"
                    )
                except Exception:
                    metadata = {}

                if not isinstance(
                    metadata,
                    dict,
                ):
                    metadata = {}

                old_whatsapp = metadata.get(
                    "whatsapp"
                )

                if isinstance(
                    old_whatsapp,
                    dict,
                ):
                    previous_contact_ref = str(
                        old_whatsapp.get(
                            "contact_ref"
                        )
                        or ""
                    ).strip()

                metadata["whatsapp"] = {
                    "allowlisted":
                        bool(
                            allowlisted
                        ),

                    "contact_ref":
                        clean_contact_ref,
                }

                conn.execute(
                    """
                    UPDATE people
                    SET
                        aliases_enc=?,
                        metadata_json=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        self._enc(
                            merged_aliases
                        ),
                        _json(metadata),
                        now,
                        person_id,
                    ),
                )

        return {
            "person_id":
                person_id,

            "contact_ref":
                clean_contact_ref,

            "previous_contact_ref":
                previous_contact_ref,

            "allowlisted":
                bool(
                    allowlisted
                ),
        }

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

    def display_name_for_person(self, person_id: str) -> str:
        """Return only a person's enrolled human-facing name.

        This is intentionally narrower than ``list_people``: trusted-device
        reply notifications may use the name, but must never need routing
        metadata, notes, aliases, or a provider identifier.
        """
        clean_person_id = str(person_id or "").strip()
        if not clean_person_id:
            return ""

        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT canonical_name_enc
                FROM people
                WHERE id=?
                """,
                (clean_person_id,),
            ).fetchone()

        if row is None:
            return ""

        return self._dec(row["canonical_name_enc"]).strip()[:256]

    def start_whatsapp_auto_conversation(
        self, *, contact_ref: str, goal: str, duration_minutes: int | None = None,
        max_auto_replies: int = 0, initial_message: str = "",
    ) -> dict[str, Any]:
        resolved = self.resolve_contact_ref(contact_ref)
        if resolved.get("status") != "resolved":
            return {"ok": False, "error": "contact_" + str(resolved.get("status") or "unavailable")}
        clean_goal = str(goal or "").strip()
        clean_initial = str(initial_message or "").strip()
        if not clean_goal or len(clean_goal) > 2000 or len(clean_initial) > 4000:
            return {"ok": False, "error": "invalid_auto_conversation_content"}
        duration = None if duration_minutes is None else max(1, min(120, int(duration_minutes)))
        quota = int(max_auto_replies)
        if quota < 0:
            raise ValueError(
                "max_auto_replies_must_be_nonnegative"
            )
        now = _utc_now()
        expires = (
            (datetime.now(timezone.utc) + timedelta(minutes=duration)).isoformat()
            if duration is not None else ""
        )
        session_id = uuid.uuid4().hex
        person_id = str(resolved["person_id"])
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE whatsapp_auto_conversations
                   SET status='stopped', stopped_at=?, updated_at=?
                   WHERE person_id=? AND status IN ('active', 'starting')""",
                (now, now, person_id),
            )
            conn.execute(
                """INSERT INTO whatsapp_auto_conversations
                   (session_id, person_id, provider_contact_ref_enc, goal_enc,
                    status, max_auto_replies, expires_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, person_id, self._enc(str(resolved["provider_contact_ref"])),
                 self._enc(clean_goal), "starting" if clean_initial else "active",
                 quota, expires, now, now),
            )
        return {"ok": True, "session_id": session_id,
                "status": "starting" if clean_initial else "active",
                "auto_conversation_active": not bool(clean_initial),
                "contact_name": str(resolved["canonical_name"])[:256]}

    def whatsapp_auto_conversation_state(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """SELECT status, auto_reply_count, max_auto_replies, expires_at,
                          initial_outreach_id FROM whatsapp_auto_conversations
                   WHERE session_id=?""", (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def stop_whatsapp_auto_on_opt_out(self, *, provider_contact_ref: str, content: str) -> bool:
        if not provider_contact_ref or not _recipient_opted_out(content):
            return False
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT session_id, provider_contact_ref_enc
                   FROM whatsapp_auto_conversations WHERE status='active'"""
            ).fetchall()
            for row in rows:
                if self._dec(row["provider_contact_ref_enc"]) == provider_contact_ref:
                    conn.execute(
                        """UPDATE whatsapp_auto_conversations
                           SET status='stopped', stopped_at=?, updated_at=? WHERE session_id=?""",
                        (now, now, row["session_id"]),
                    )
                    return True
        return False

    def activate_whatsapp_auto_conversation(self, session_id: str, outreach_id: str) -> bool:
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT outbound_message_enc, person_id FROM external_outreach
                   WHERE outreach_id=? AND status='sent'""", (outreach_id,),
            ).fetchone()
            if row is None:
                return False
            changed = conn.execute(
                """UPDATE whatsapp_auto_conversations
                   SET status='active', initial_outreach_id=?, updated_at=?
                   WHERE session_id=? AND person_id=? AND status='starting'
                     AND (expires_at='' OR expires_at>?)""",
                (outreach_id, now, session_id, row["person_id"], now),
            ).rowcount
            if changed:
                conn.execute(
                    """INSERT INTO whatsapp_auto_messages(session_id, direction, content_enc, created_at)
                       VALUES (?, 'outbound', ?, ?)""",
                    (session_id, row["outbound_message_enc"], now),
                )
            return bool(changed)

    def stop_whatsapp_auto_conversation(
        self, *, contact_ref: str = "", session_id: str = "",
    ) -> dict[str, Any]:
        person_id = ""
        if contact_ref:
            resolved = self.resolve_contact_ref(contact_ref)
            if resolved.get("status") not in {"resolved", "not_allowlisted", "not_configured"}:
                return {"ok": False, "error": "contact_" + str(resolved.get("status") or "unavailable")}
            person_id = str(resolved["person_id"])
        if not person_id and not session_id:
            return {"ok": False, "error": "session_required"}
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if person_id:
                changed = conn.execute(
                    """UPDATE whatsapp_auto_conversations
                       SET status='stopped', stopped_at=?, updated_at=?
                       WHERE person_id=? AND status IN ('active', 'starting')""",
                    (now, now, person_id),
                ).rowcount
            else:
                changed = conn.execute(
                    """UPDATE whatsapp_auto_conversations
                       SET status='stopped', stopped_at=?, updated_at=?
                       WHERE session_id=? AND status IN ('active', 'starting')""",
                    (now, now, session_id),
                ).rowcount
        return {"ok": True, "status": "stopped" if changed else "not_active"}

    def claim_whatsapp_auto_reply(
        self, *, person_id: str, incoming_message_ref: str, content: str,
    ) -> dict[str, Any]:
        if not incoming_message_ref or not content:
            return {"auto_reply_eligible": False}
        fingerprint = hashlib.sha256(
            ("whatsapp\0inbound\0" + incoming_message_ref).encode("utf-8")
        ).hexdigest()
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                """SELECT * FROM whatsapp_auto_conversations
                   WHERE person_id=? AND status='active'
                   ORDER BY created_at DESC LIMIT 1""", (person_id,),
            ).fetchone()
            if session is None:
                return {"auto_reply_eligible": False}
            session_id = str(session["session_id"])
            if session["expires_at"] and str(session["expires_at"]) <= now:
                conn.execute(
                    "UPDATE whatsapp_auto_conversations SET status='expired', updated_at=? WHERE session_id=?",
                    (now, session_id),
                )
                return {"auto_reply_eligible": False}
            if _recipient_opted_out(content):
                conn.execute(
                    """UPDATE whatsapp_auto_conversations
                       SET status='stopped', stopped_at=?, updated_at=? WHERE session_id=?""",
                    (now, now, session_id),
                )
                return {"auto_reply_eligible": False}
            if conn.execute(
                "SELECT 1 FROM whatsapp_auto_reply_attempts WHERE inbound_fingerprint=?",
                (fingerprint,),
            ).fetchone():
                return {"auto_reply_eligible": False}
            reserved = conn.execute(
                "SELECT COUNT(*) FROM whatsapp_auto_reply_attempts WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            reply_limit = int(
                session["max_auto_replies"]
            )

            if (
                reply_limit > 0
                and int(reserved) >= reply_limit
            ):
                conn.execute(
                    "UPDATE whatsapp_auto_conversations SET status='completed', updated_at=? WHERE session_id=?",
                    (now, session_id),
                )
                return {"auto_reply_eligible": False}
            history = conn.execute(
                """SELECT direction, content_enc FROM whatsapp_auto_messages
                   WHERE session_id=? ORDER BY id DESC LIMIT 8""",
                (session_id,),
            ).fetchall()
            attempt_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO whatsapp_auto_reply_attempts
                   (attempt_id, session_id, inbound_fingerprint, inbound_content_enc,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'generation_started', ?, ?)""",
                (attempt_id, session_id, fingerprint, self._enc(content), now, now),
            )
            conn.execute(
                """INSERT INTO whatsapp_auto_messages(session_id, direction, content_enc, created_at)
                   VALUES (?, 'inbound', ?, ?)""",
                (session_id, self._enc(content), now),
            )
            name = conn.execute(
                "SELECT canonical_name_enc FROM people WHERE id=?", (person_id,),
            ).fetchone()
            return {
                "auto_reply_eligible": True,
                "session_id": session_id,
                "attempt_id": attempt_id,
                "contact_name": self._dec(name["canonical_name_enc"]).strip()[:256] if name else "",
                "goal": self._dec(session["goal_enc"])[:2000],
                "history": [
                    {"direction": str(item["direction"]), "content": self._dec(item["content_enc"])[:4000]}
                    for item in reversed(history)
                ],
            }

    def prepare_whatsapp_auto_send(
        self, *, session_id: str, attempt_id: str, message: str,
    ) -> dict[str, Any]:
        clean_message = str(message or "").strip()
        if not clean_message or len(clean_message) > 4000:
            return {"ok": False, "error": "invalid_message"}
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT s.*, a.state FROM whatsapp_auto_reply_attempts a
                   JOIN whatsapp_auto_conversations s ON s.session_id=a.session_id
                   WHERE a.attempt_id=? AND s.session_id=?""",
                (attempt_id, session_id),
            ).fetchone()
            if row is None or row["state"] != "generation_started" or row["status"] != "active":
                return {"ok": False, "error": "auto_reply_not_available"}
            if row["expires_at"] and str(row["expires_at"]) <= now:
                conn.execute(
                    "UPDATE whatsapp_auto_conversations SET status='expired', updated_at=? WHERE session_id=?",
                    (now, session_id),
                )
                return {"ok": False, "error": "session_expired"}
            outreach_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO external_outreach
                   (outreach_id, person_id, provider, provider_contact_ref_enc,
                    outbound_message_enc, status, created_at, updated_at)
                   VALUES (?, ?, 'whatsapp', ?, ?, 'dispatching', ?, ?)""",
                (outreach_id, row["person_id"], row["provider_contact_ref_enc"],
                 self._enc(clean_message), now, now),
            )
            conn.execute(
                """UPDATE whatsapp_auto_reply_attempts
                   SET generated_content_enc=?, state='send_started', outreach_id=?, updated_at=?
                   WHERE attempt_id=?""",
                (self._enc(clean_message), outreach_id, now, attempt_id),
            )
            return {"ok": True, "outreach_id": outreach_id,
                    "provider_contact_ref": self._dec(row["provider_contact_ref_enc"])}

    def finish_whatsapp_auto_send(
        self, *, attempt_id: str, outreach_id: str, status: str,
        provider_message_ref: str = "", error: str = "",
    ) -> None:
        now = _utc_now()
        final_status = status if status in {"sent", "failed", "unknown"} else "unknown"
        if final_status == "sent" and not provider_message_ref:
            final_status = "unknown"
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT a.session_id, a.generated_content_enc, s.auto_reply_count
                   FROM whatsapp_auto_reply_attempts a
                   JOIN whatsapp_auto_conversations s ON s.session_id=a.session_id
                   WHERE a.attempt_id=? AND a.outreach_id=? AND a.state='send_started'""",
                (attempt_id, outreach_id),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE whatsapp_auto_reply_attempts SET state=?, updated_at=? WHERE attempt_id=?",
                (final_status, now, attempt_id),
            )
            if final_status == "sent":
                fingerprint = hashlib.sha256(
                    ("whatsapp\0" + provider_message_ref).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """UPDATE external_outreach SET status='sent', provider_message_ref_enc=?,
                       provider_message_fingerprint=?, updated_at=? WHERE outreach_id=?""",
                    (self._enc(provider_message_ref), fingerprint, now, outreach_id),
                )
                conn.execute(
                    """INSERT INTO whatsapp_auto_messages(session_id, direction, content_enc, created_at)
                       VALUES (?, 'outbound', ?, ?)""",
                    (row["session_id"], row["generated_content_enc"], now),
                )
                count = int(row["auto_reply_count"]) + 1
                conn.execute(
                    """UPDATE whatsapp_auto_conversations
                       SET auto_reply_count=?,
                           status=CASE
                               WHEN max_auto_replies > 0
                                    AND ? >= max_auto_replies
                               THEN 'completed'
                               ELSE status
                           END,
                           updated_at=?
                       WHERE session_id=?""",
                    (count, count, now, row["session_id"]),
                )
            else:
                conn.execute(
                    """UPDATE external_outreach SET status='failed', error_enc=?, updated_at=?
                       WHERE outreach_id=?""",
                    (self._enc(error[:200]), now, outreach_id),
                )

    def create_external_outreach(
        self,
        *,
        outreach_id: str,
        person_id: str,
        provider_contact_ref: str,
        outbound_message: str,
        provider: str = "whatsapp",
    ) -> dict[str, Any]:
        """Create local durable state before any external send occurs."""

        clean_outreach_id = str(
            outreach_id or ""
        ).strip()

        clean_person_id = str(
            person_id or ""
        ).strip()

        clean_provider = str(
            provider or ""
        ).strip()

        clean_contact_ref = str(
            provider_contact_ref or ""
        ).strip()

        clean_message = str(
            outbound_message or ""
        ).strip()

        if not clean_outreach_id:
            raise ValueError(
                "outreach_id_required"
            )

        if not clean_person_id:
            raise ValueError(
                "person_id_required"
            )

        if not clean_provider:
            raise ValueError(
                "provider_required"
            )

        if not clean_contact_ref:
            raise ValueError(
                "provider_contact_ref_required"
            )

        if not clean_message:
            raise ValueError(
                "outbound_message_required"
            )

        now = _utc_now()

        with self._lock, self._connection() as conn:
            person = conn.execute(
                """
                SELECT id
                FROM people
                WHERE id=?
                """,
                (
                    clean_person_id,
                ),
            ).fetchone()

            if person is None:
                raise ValueError(
                    "person_not_found"
                )

            existing = conn.execute(
                """
                SELECT
                    outreach_id,
                    person_id,
                    provider,
                    status
                FROM external_outreach
                WHERE outreach_id=?
                """,
                (
                    clean_outreach_id,
                ),
            ).fetchone()

            if existing is not None:
                if (
                    str(existing["person_id"])
                    != clean_person_id
                    or str(existing["provider"])
                    != clean_provider
                ):
                    raise ValueError(
                        "outreach_id_conflict"
                    )

                return {
                    "outreach_id":
                        str(
                            existing[
                                "outreach_id"
                            ]
                        ),
                    "status":
                        str(
                            existing[
                                "status"
                            ]
                        ),
                    "already_exists":
                        True,
                }

            conn.execute(
                """
                INSERT INTO external_outreach(
                    outreach_id,
                    person_id,
                    provider,
                    provider_contact_ref_enc,
                    outbound_message_enc,
                    status,
                    created_at,
                    updated_at
                )
                VALUES(
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'dispatching',
                    ?,
                    ?
                )
                """,
                (
                    clean_outreach_id,
                    clean_person_id,
                    clean_provider,
                    self._enc(
                        clean_contact_ref
                    ),
                    self._enc(
                        clean_message
                    ),
                    now,
                    now,
                ),
            )

        return {
            "outreach_id":
                clean_outreach_id,
            "status":
                "dispatching",
            "already_exists":
                False,
        }

    def mark_external_outreach_sent(
        self,
        *,
        outreach_id: str,
        provider_message_ref: str,
    ) -> dict[str, Any]:
        clean_outreach_id = str(
            outreach_id or ""
        ).strip()

        clean_message_ref = str(
            provider_message_ref or ""
        ).strip()

        if not clean_outreach_id:
            raise ValueError(
                "outreach_id_required"
            )

        if not clean_message_ref:
            raise ValueError(
                "provider_message_ref_required"
            )

        now = _utc_now()

        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT provider
                FROM external_outreach
                WHERE outreach_id=?
                """,
                (
                    clean_outreach_id,
                ),
            ).fetchone()

            if row is None:
                raise ValueError(
                    "outreach_not_found"
                )

            provider = str(
                row["provider"]
                or ""
            )

            fingerprint = hashlib.sha256(
                (
                    provider
                    + "\0"
                    + clean_message_ref
                ).encode(
                    "utf-8"
                )
            ).hexdigest()

            conn.execute(
                """
                UPDATE external_outreach
                SET
                    status='sent',
                    provider_message_ref_enc=?,
                    provider_message_fingerprint=?,
                    error_enc='',
                    updated_at=?
                WHERE outreach_id=?
                """,
                (
                    self._enc(
                        clean_message_ref
                    ),
                    fingerprint,
                    now,
                    clean_outreach_id,
                ),
            )

        return {
            "outreach_id":
                clean_outreach_id,
            "status":
                "sent",
        }

    def mark_external_outreach_failed(
        self,
        *,
        outreach_id: str,
        error: str,
    ) -> None:
        clean_outreach_id = str(
            outreach_id or ""
        ).strip()

        if not clean_outreach_id:
            return

        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE external_outreach
                SET
                    status='failed',
                    error_enc=?,
                    updated_at=?
                WHERE outreach_id=?
                """,
                (
                    self._enc(
                        str(error or "")[
                            :2000
                        ]
                    ),
                    _utc_now(),
                    clean_outreach_id,
                ),
            )

    def accept_external_outreach_reply(
        self,
        *,
        provider_message_ref: str,
        provider_contact_ref: str,
        content: str,
        provider: str = "whatsapp",
        confidence: float = 0.65,
    ) -> dict[str, Any]:
        """Correlate one reply and store it as non-authoritative evidence."""

        clean_provider = str(
            provider or ""
        ).strip()

        clean_message_ref = str(
            provider_message_ref or ""
        ).strip()

        clean_contact_ref = str(
            provider_contact_ref or ""
        ).strip()

        clean_content = str(
            content or ""
        ).strip()

        if (
            not clean_provider
            or not clean_message_ref
        ):
            return {
                "status":
                    "unmatched",
            }

        if not clean_content:
            return {
                "status":
                    "empty_reply",
            }

        fingerprint = hashlib.sha256(
            (
                clean_provider
                + "\0"
                + clean_message_ref
            ).encode(
                "utf-8"
            )
        ).hexdigest()

        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    outreach_id,
                    person_id,
                    provider_contact_ref_enc,
                    status,
                    reply_memory_id
                FROM external_outreach
                WHERE
                    provider=?
                    AND provider_message_fingerprint=?
                """,
                (
                    clean_provider,
                    fingerprint,
                ),
            ).fetchone()

        if row is None:
            return {
                "status":
                    "unmatched",
            }

        expected_contact_ref = self._dec(
            row[
                "provider_contact_ref_enc"
            ]
        )

        # The reply must belong to the same provider-side contact
        # the original message was sent to.
        if (
            not clean_contact_ref
            or clean_contact_ref
            != expected_contact_ref
        ):
            return {
                "status":
                    "sender_mismatch",
            }

        outreach_id = str(
            row["outreach_id"]
        )

        person_id = str(
            row["person_id"]
        )

        if (
            str(row["status"])
            == "replied"
        ):
            return {
                "status":
                    "duplicate",

                "outreach_id":
                    outreach_id,

                "person_id":
                    person_id,

                "memory_id":
                    str(
                        row[
                            "reply_memory_id"
                        ]
                        or ""
                    ),
            }

        if (
            str(row["status"])
            != "sent"
        ):
            return {
                "status":
                    "not_pending",

                "outreach_id":
                    outreach_id,
            }

        stored = (
            self.record_external_perspective(
                person_id=
                    person_id,

                content=
                    clean_content,

                confidence=
                    confidence,

                outreach_id=
                    outreach_id,

                provider=
                    clean_provider,
            )
        )

        memory_id = str(
            stored["memory_id"]
        )

        now = _utc_now()

        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE external_outreach
                SET
                    status='replied',
                    reply_memory_id=?,
                    replied_at=?,
                    updated_at=?
                WHERE
                    outreach_id=?
                    AND status='sent'
                """,
                (
                    memory_id,
                    now,
                    now,
                    outreach_id,
                ),
            )

        return {
            "status":
                "accepted",

            "outreach_id":
                outreach_id,

            "person_id":
                person_id,

            "memory_id":
                memory_id,

            "confidence":
                float(
                    stored[
                        "confidence"
                    ]
                ),

            "user_authoritative":
                False,
        }

    def accept_external_outreach_reply_by_contact(
        self,
        *,
        incoming_message_ref: str,
        provider_contact_ref: str,
        content: str,
        provider: str = "whatsapp",
        confidence: float = 0.65,
        max_age_hours: int = 24,
    ) -> dict[str, Any]:
        clean_provider = str(
            provider or ""
        ).strip()

        clean_incoming_ref = str(
            incoming_message_ref or ""
        ).strip()

        clean_contact_ref = str(
            provider_contact_ref or ""
        ).strip()

        clean_content = str(
            content or ""
        ).strip()

        if (
            not clean_provider
            or not clean_incoming_ref
            or not clean_contact_ref
        ):
            return {
                "status": "unmatched",
            }

        if not clean_content:
            return {
                "status": "empty_reply",
            }

        hours = max(
            1,
            min(
                int(max_age_hours),
                168,
            ),
        )

        inbound_fingerprint = hashlib.sha256(
            (
                clean_provider
                + "\0inbound\0"
                + clean_incoming_ref
            ).encode("utf-8")
        ).hexdigest()

        with self._lock:
            with self._connection() as conn:
                receipt = conn.execute(
                    """
                    SELECT
                        status,
                        outreach_id,
                        memory_id
                    FROM external_outreach_reply_receipts
                    WHERE
                        provider=?
                        AND inbound_message_fingerprint=?
                    """,
                    (
                        clean_provider,
                        inbound_fingerprint,
                    ),
                ).fetchone()

            if receipt is not None:
                status = str(
                    receipt["status"]
                    or ""
                )

                if status == "accepted":
                    return {
                        "status": "duplicate",
                        "outreach_id": str(
                            receipt["outreach_id"]
                            or ""
                        ),
                        "memory_id": str(
                            receipt["memory_id"]
                            or ""
                        ),
                    }

                if status == "ambiguous":
                    return {
                        "status": "ambiguous",
                    }

            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        outreach_id,
                        provider_contact_ref_enc,
                        provider_message_ref_enc
                    FROM external_outreach
                    WHERE
                        provider=?
                        AND status='sent'
                        AND julianday(created_at)
                            >= julianday(
                                'now',
                                ?
                            )
                    ORDER BY created_at DESC
                    """,
                    (
                        clean_provider,
                        f"-{hours} hours",
                    ),
                ).fetchall()

            matches = []

            for row in rows:
                expected = self._dec(
                    row[
                        "provider_contact_ref_enc"
                    ]
                )

                if expected == clean_contact_ref:
                    matches.append(row)

            if not matches:
                return {
                    "status": "unmatched",
                }

            if len(matches) > 1:
                with self._connection() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO
                            external_outreach_reply_receipts(
                                provider,
                                inbound_message_fingerprint,
                                status,
                                created_at
                            )
                        VALUES(
                            ?,
                            ?,
                            'ambiguous',
                            ?
                        )
                        """,
                        (
                            clean_provider,
                            inbound_fingerprint,
                            _utc_now(),
                        ),
                    )

                return {
                    "status": "ambiguous",
                    "candidate_count":
                        len(matches),
                }

            selected = matches[0]

            provider_message_ref = self._dec(
                selected[
                    "provider_message_ref_enc"
                ]
            )

            if not provider_message_ref:
                return {
                    "status": "unmatched",
                }

            result = (
                self.accept_external_outreach_reply(
                    provider_message_ref=
                        provider_message_ref,
                    provider_contact_ref=
                        clean_contact_ref,
                    content=
                        clean_content,
                    provider=
                        clean_provider,
                    confidence=
                        confidence,
                )
            )

            if (
                result.get("status")
                in {
                    "accepted",
                    "duplicate",
                }
            ):
                outreach_id = str(
                    result.get(
                        "outreach_id"
                    )
                    or selected[
                        "outreach_id"
                    ]
                    or ""
                )

                memory_id = str(
                    result.get(
                        "memory_id"
                    )
                    or ""
                )

                with self._connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO
                            external_outreach_reply_receipts(
                                provider,
                                inbound_message_fingerprint,
                                status,
                                outreach_id,
                                memory_id,
                                created_at
                            )
                        VALUES(
                            ?,
                            ?,
                            'accepted',
                            ?,
                            ?,
                            ?
                        )
                        ON CONFLICT(
                            provider,
                            inbound_message_fingerprint
                        )
                        DO UPDATE SET
                            status='accepted',
                            outreach_id=
                                excluded.outreach_id,
                            memory_id=
                                excluded.memory_id
                        """,
                        (
                            clean_provider,
                            inbound_fingerprint,
                            outreach_id,
                            memory_id,
                            _utc_now(),
                        ),
                    )

            return result

    def resolve_contact_ref(
        self,
        contact_ref: str,
    ) -> dict[str, Any]:
        """Resolve a human-facing name/alias to one trusted WhatsApp contact.

        Transport identifiers stay opaque. This method never returns a phone
        number or JID; only the provider-owned contact_ref stored in local
        encrypted/private memory metadata.
        """

        def fold(value: str) -> str:
            table = str.maketrans({
                "ç": "c",
                "ğ": "g",
                "ı": "i",
                "ö": "o",
                "ş": "s",
                "ü": "u",
                "Ç": "c",
                "Ğ": "g",
                "İ": "i",
                "I": "i",
                "Ö": "o",
                "Ş": "s",
                "Ü": "u",
            })

            return (
                str(value or "")
                .translate(table)
                .casefold()
                .strip()
            )

        wanted = fold(contact_ref)

        if not wanted:
            return {
                "status": "not_found",
            }

        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    canonical_name_enc,
                    aliases_enc,
                    metadata_json
                FROM people
                ORDER BY updated_at DESC
                """
            ).fetchall()

        matches = []

        for row in rows:
            canonical_name = self._dec(
                row["canonical_name_enc"]
            )

            try:
                aliases = json.loads(
                    self._dec(
                        row["aliases_enc"]
                    )
                    or "[]"
                )
            except json.JSONDecodeError:
                aliases = []

            if not isinstance(
                aliases,
                list,
            ):
                aliases = []

            names = [
                canonical_name,
                *[
                    str(alias)
                    for alias in aliases
                    if isinstance(
                        alias,
                        str,
                    )
                ],
            ]

            if not any(
                fold(name) == wanted
                for name in names
            ):
                continue

            try:
                metadata = json.loads(
                    row["metadata_json"]
                    or "{}"
                )
            except json.JSONDecodeError:
                metadata = {}

            if not isinstance(
                metadata,
                dict,
            ):
                metadata = {}

            whatsapp = metadata.get(
                "whatsapp"
            )

            if not isinstance(
                whatsapp,
                dict,
            ):
                whatsapp = {}

            matches.append({
                "person_id":
                    str(row["id"]),

                "canonical_name":
                    canonical_name,

                "allowlisted":
                    whatsapp.get(
                        "allowlisted"
                    )
                    is True,

                "provider_contact_ref":
                    str(
                        whatsapp.get(
                            "contact_ref"
                        )
                        or ""
                    ).strip(),
            })

        if not matches:
            return {
                "status": "not_found",
            }

        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "candidate_count":
                    len(matches),
            }

        match = matches[0]

        if not match["allowlisted"]:
            return {
                "status":
                    "not_allowlisted",

                "person_id":
                    match["person_id"],

                "canonical_name":
                    match[
                        "canonical_name"
                    ],
            }

        if not match[
            "provider_contact_ref"
        ]:
            return {
                "status":
                    "not_configured",

                "person_id":
                    match["person_id"],

                "canonical_name":
                    match[
                        "canonical_name"
                    ],
            }

        return {
            "status": "resolved",

            "person_id":
                match["person_id"],

            "canonical_name":
                match[
                    "canonical_name"
                ],

            # Opaque provider-owned identifier.
            # Never a raw number/JID.
            "provider_contact_ref":
                match[
                    "provider_contact_ref"
                ],
        }

    def record_external_perspective(
        self,
        *,
        person_id: str,
        content: str,
        confidence: float = 0.5,
        outreach_id: str | None = None,
        source_day: str | None = None,
        provider: str = "whatsapp",
    ) -> dict[str, Any]:
        """Store third-party evidence without granting it self-authority."""

        clean_person_id = str(
            person_id or ""
        ).strip()

        clean_content = str(
            content or ""
        ).strip()

        if not clean_person_id:
            raise ValueError(
                "person_id_required"
            )

        if not clean_content:
            raise ValueError(
                "external_perspective_content_required"
            )

        # Third-party-only evidence can never exceed 0.75.
        capped_confidence = max(
            0.0,
            min(
                0.75,
                float(confidence),
            ),
        )

        now = _utc_now()

        day_id = (
            str(source_day).strip()
            if source_day
            else now[:10]
        )

        with self._lock, self._connection() as conn:
            person = conn.execute(
                """
                SELECT id
                FROM people
                WHERE id=?
                """,
                (
                    clean_person_id,
                ),
            ).fetchone()

            if person is None:
                raise ValueError(
                    "person_not_found"
                )

            subject = (
                "person:"
                + clean_person_id
            )

            fingerprint = (
                _content_fingerprint(
                    "external_perspective",
                    subject,
                    clean_content,
                )
            )

            memory_id = (
                uuid.uuid4().hex
            )

            metadata = {
                "provider":
                    str(provider or ""),
            }

            if outreach_id:
                metadata[
                    "outreach_id"
                ] = str(outreach_id)

            conn.execute(
                """
                INSERT OR IGNORE INTO memories
                    (
                        id,
                        kind,
                        subject,
                        content_fingerprint,
                        content_enc,
                        confidence,
                        source_day,
                        provenance,
                        valid_from,
                        user_authoritative,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                VALUES(
                    ?,
                    'external_perspective',
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'external_perspective',
                    ?,
                    0,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    memory_id,
                    subject,
                    fingerprint,
                    self._enc(
                        clean_content
                    ),
                    capped_confidence,
                    day_id,
                    day_id,
                    _json(metadata),
                    now,
                    now,
                ),
            )

            row = conn.execute(
                """
                SELECT
                    id,
                    confidence,
                    provenance,
                    user_authoritative
                FROM memories
                WHERE
                    kind='external_perspective'
                    AND subject=?
                    AND content_fingerprint=?
                    AND source_day=?
                """,
                (
                    subject,
                    fingerprint,
                    day_id,
                ),
            ).fetchone()

        if row is None:
            raise RuntimeError(
                "external_perspective_write_failed"
            )

        return {
            "memory_id":
                str(row["id"]),

            "kind":
                "external_perspective",

            "confidence":
                float(
                    row["confidence"]
                    or 0.0
                ),

            "provenance":
                str(
                    row["provenance"]
                    or ""
                ),

            "user_authoritative":
                bool(
                    row[
                        "user_authoritative"
                    ]
                ),
        }

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
