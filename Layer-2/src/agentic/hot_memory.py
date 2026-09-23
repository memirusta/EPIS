"""Short-term conversational continuity for EPIS.

This store is intentionally separate from daily/weekly/long-term memory.

Purpose:
- Preserve the complete latest user <-> EPIS session across process restarts.
- A session ends only when an explicit conversation boundary is written.
- Never trim the latest session by age, message count, or character count.
- Never store raw tool payloads/results here.
- /new creates a conversation boundary without deleting older raw history.

Older sessions may remain in the append-only JSONL file for nightly/daily
processing, but only the latest session is restored into active chat context.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class HotConversationStore:
    """Append-only JSONL store whose active view is the latest session."""

    def __init__(
        self,
        path,
        *,
        hours: float | None = None,
        max_messages: int | None = None,
        max_chars: int | None = None,
    ):
        self.path = Path(path)
        self._lock = threading.RLock()

        # Backward-compatible constructor parameters from the old rolling-window
        # implementation. Session memory no longer trims by these limits.
        self.hours = hours
        self.max_messages = max_messages
        self.max_chars = max_chars

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

        with self._lock:
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

        with self._lock:
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

    def append_assistant_event(
        self,
        assistant_message: str,
        *,
        source: str = "internal_event",
    ) -> None:
        """Append an assistant-only event to the active shared session.

        Proactive/Kairos events are machine-originated context, not user text.
        Keeping only the generated EPIS message prevents internal trigger
        instructions from polluting later conversation history.
        """
        content = (assistant_message or "").strip()
        if not content:
            return
        self._append(
            {
                "type": "message",
                "role": "assistant",
                "content": content,
                "source": str(source or "internal_event")[:80],
            }
        )

    def mark_new_conversation(self) -> None:
        """End the active session without deleting older raw session logs."""
        self._append(
            {
                "type": "boundary",
                "reason": "new_conversation",
            }
        )

    def _read_rows(self) -> list[dict]:
        if not self.path.exists():
            return []

        rows: list[dict] = []

        try:
            with self._lock:
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

                        if isinstance(row, dict):
                            rows.append(row)

        except OSError:
            return []

        return rows

    def load_latest_session(self) -> list[dict]:
        """Return every user/assistant message after the newest boundary.

        Unlike the old rolling hot-memory window, this method intentionally
        does not apply a time cutoff, message-count limit, or character limit.
        The active unit is the latest session, not an arbitrary number of turns.
        """
        rows = self._read_rows()

        boundary_index = -1

        for index, row in enumerate(rows):
            if (
                row.get("type") == "boundary"
                and row.get("reason") == "new_conversation"
            ):
                boundary_index = index

        if boundary_index >= 0:
            rows = rows[boundary_index + 1 :]

        messages: list[dict] = []

        for row in rows:
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

            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        return messages

    def load_recent(self) -> list[dict]:
        """Backward-compatible alias for the latest-session active view."""
        return self.load_latest_session()
