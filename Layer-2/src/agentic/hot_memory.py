"""Short-term conversational continuity for EPIS.

This is intentionally separate from daily/weekly/long-term memory.

Purpose:
- Preserve recent user <-> EPIS conversation across process restarts.
- Restore up to the last N hours of ordinary conversation.
- Never store raw tool payloads/results here.
- /new creates a conversation boundary without deleting long-term memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path


DEFAULT_HOURS = 5.0
DEFAULT_MAX_MESSAGES = 240
DEFAULT_MAX_CHARS = 120_000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


class HotConversationStore:
    """Append-only local JSONL store for recent conversational continuity."""

    def __init__(
        self,
        path,
        *,
        hours: float | None = None,
        max_messages: int | None = None,
        max_chars: int | None = None,
    ):
        self.path = Path(path)

        self.hours = (
            float(os.getenv("EPIS_HOT_MEMORY_HOURS", str(DEFAULT_HOURS)))
            if hours is None
            else float(hours)
        )

        self.max_messages = (
            int(
                os.getenv(
                    "EPIS_HOT_MEMORY_MAX_MESSAGES",
                    str(DEFAULT_MAX_MESSAGES),
                )
            )
            if max_messages is None
            else int(max_messages)
        )

        self.max_chars = (
            int(
                os.getenv(
                    "EPIS_HOT_MEMORY_MAX_CHARS",
                    str(DEFAULT_MAX_CHARS),
                )
            )
            if max_chars is None
            else int(max_chars)
        )

        if self.hours <= 0:
            raise ValueError("Hot-memory hours must be positive")

        if self.max_messages <= 0:
            raise ValueError("Hot-memory max_messages must be positive")

        if self.max_chars <= 0:
            raise ValueError("Hot-memory max_chars must be positive")

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    def _append(self, row: dict) -> None:
        row = dict(row)

        if "ts" not in row:
            row["ts"] = _now_utc().isoformat()

        encoded = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self.path.open(
            "a",
            encoding="utf-8",
        ) as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def append_turn(
        self,
        user_message: str,
        assistant_message: str,
    ) -> None:
        user_message = (
            user_message or ""
        ).strip()

        assistant_message = (
            assistant_message or ""
        ).strip()

        if user_message:
            self._append(
                {
                    "type": "message",
                    "role": "user",
                    "content": user_message,
                }
            )

        if assistant_message:
            self._append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": assistant_message,
                }
            )

    def mark_new_conversation(self) -> None:
        """Start a new hot conversation without deleting historical logs."""
        self._append(
            {
                "type": "boundary",
                "reason": "new_conversation",
            }
        )

    def load_recent(self) -> list[dict]:
        """Return recent messages in OpenAI-style role/content format."""
        if not self.path.exists():
            return []

        cutoff = (
            _now_utc()
            - timedelta(hours=self.hours)
        )

        rows: list[dict] = []

        try:
            with self.path.open(
                "r",
                encoding="utf-8",
            ) as stream:
                for line in stream:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(row, dict):
                        continue

                    ts = _parse_timestamp(
                        row.get("ts")
                    )

                    if ts is None:
                        continue

                    rows.append(
                        {
                            **row,
                            "_parsed_ts": ts,
                        }
                    )

        except OSError:
            return []

        # /new boundary should prevent older turns from returning even if
        # they're still inside the five-hour time window.
        boundary_index = -1

        for index, row in enumerate(rows):
            if (
                row.get("type")
                == "boundary"
                and row.get("reason")
                == "new_conversation"
            ):
                boundary_index = index

        if boundary_index >= 0:
            rows = rows[
                boundary_index + 1 :
            ]

        candidates: list[dict] = []

        for row in rows:
            if row["_parsed_ts"] < cutoff:
                continue

            if row.get("type") != "message":
                continue

            role = row.get("role")

            if role not in {
                "user",
                "assistant",
            }:
                continue

            content = (
                row.get("content")
                or ""
            ).strip()

            if not content:
                continue

            candidates.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        # En yeni mesajları tercih et.
        if len(candidates) > self.max_messages:
            candidates = candidates[
                -self.max_messages :
            ]

        # Context guard: sondan başlayıp max_chars içine sığanı al.
        selected_reversed = []
        used_chars = 0

        for message in reversed(candidates):
            cost = (
                len(message["content"])
                + 32
            )

            if (
                selected_reversed
                and used_chars + cost
                > self.max_chars
            ):
                break

            # Tek bir mesaj max_chars'tan büyükse kırpma yapmıyoruz;
            # en azından en yeni mesajı koruyoruz.
            selected_reversed.append(
                message
            )

            used_chars += cost

        return list(
            reversed(
                selected_reversed
            )
        )