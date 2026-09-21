"""Local action receipts, not a replay queue. No arguments or results on disk."""
from datetime import datetime, timezone
import sqlite3
import uuid
import os


class TaskStore:
    def __init__(self, path=":memory:"):
        self._lease = None
        if str(path) != ":memory:":
            self._lease = open(str(path) + ".lock", "a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.path.getsize(str(path) + ".lock") == 0:
                        self._lease.write(b"0")
                        self._lease.flush()
                    self._lease.seek(0)
                    msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self._lease.close()
                self._lease = None
                raise RuntimeError("Another EPIS session owns this action journal") from exc
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS actions (
            task_id TEXT PRIMARY KEY, tool TEXT NOT NULL, device TEXT NOT NULL,
            state TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        # Restart cannot establish whether a dispatched OS operation completed.
        self.db.execute("UPDATE actions SET state='unknown' WHERE state='running'")
        self.db.execute("UPDATE actions SET state='cancelled' WHERE state='awaiting_confirmation'")
        self.db.commit()

    def create(self, tool, device, state="awaiting_confirmation"):
        task_id = str(uuid.uuid4())
        self.db.execute("INSERT INTO actions VALUES (?, ?, ?, ?, ?)",
                        (task_id, tool, device, state, self._now()))
        self.db.commit()
        return task_id

    def claim(self, task_id):
        result = self.db.execute("""UPDATE actions SET state='running', updated_at=?
            WHERE task_id=? AND state='awaiting_confirmation'""", (self._now(), task_id))
        self.db.commit()
        return result.rowcount == 1

    def finish(self, task_id, state):
        if state not in {"succeeded", "failed", "unknown", "cancelled"}:
            raise ValueError("Invalid terminal action state")
        self.db.execute("""UPDATE actions SET state=?, updated_at=? WHERE task_id=?
            AND state IN ('running', 'awaiting_confirmation')""", (state, self._now(), task_id))
        self.db.commit()

    def recent(self, limit=10):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM actions ORDER BY rowid DESC LIMIT ?", (limit,))]

    def close(self):
        self.db.close()
        if self._lease:
            self._lease.close()
            self._lease = None

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()
