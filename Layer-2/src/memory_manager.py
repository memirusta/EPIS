import os
import json
import sqlite3
import logging
from datetime import datetime

from crypto_layer import get_cipher, load_json_file, save_json_file

logging.basicConfig(level=logging.INFO, format='%(asctime)s - MEMORY - %(message)s')

# Kaldirilmis place detection alanlari (eski state dosyalarindan temizlenir)
_STALE_STATE_KEYS = frozenset({
    "place", "place_label", "place_confidence", "place_source",
    "place_updated_at", "place_stabilized",
})


class MemoryManager:
    def __init__(self):
        self.current_dir  = os.path.dirname(os.path.abspath(__file__))
        self.layer2_dir   = os.path.dirname(self.current_dir)
        self.epis_root    = os.path.dirname(self.layer2_dir)

        self.identity_dir = os.path.join(self.epis_root, "Layer-1", "identity")
        self.memory_dir   = os.path.join(self.epis_root, "Layer-1", "memory")
        self.db_path      = os.path.join(self.memory_dir, "lifetime.db")
        self.people_path  = os.path.join(self.memory_dir, "people.json")

        self.cipher = get_cipher()

        self._ensure_directories()
        self._init_sqlite_db()

    def _ensure_directories(self):
        os.makedirs(self.identity_dir, exist_ok=True)
        os.makedirs(self.memory_dir, exist_ok=True)

        state_path = os.path.join(self.memory_dir, "current_state.json")
        if not os.path.exists(state_path):
            self.write_json(self.memory_dir, "current_state.json", {
                "system_status": "initialized",
                "last_update": datetime.now().isoformat()
            })

    def _init_sqlite_db(self):
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lifetime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                raw_text TEXT,
                epis_observation TEXT,
                ai_analysis_tags TEXT
            )
        ''')
        conn.commit()
        conn.close()
        logging.info("SQLite lifetime.db baglantisi saglandi.")

    # ------------------------------------------------------------------
    # Genel JSON (duz metin -- current_state gibi sik yazilanlar)
    # ------------------------------------------------------------------

    def read_json(self, folder_path: str, file_name: str) -> dict:
        file_path = os.path.join(folder_path, file_name)
        if not os.path.exists(file_path):
            return {}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error(f"JSON format hatasi: {file_path}")
            return {}

    def write_json(self, folder_path: str, file_name: str, data: dict):
        file_path = os.path.join(folder_path, file_name)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def update_current_state(self, new_data: dict):
        current = self.read_json(self.memory_dir, "current_state.json")
        current.update(new_data)
        for key in _STALE_STATE_KEYS:
            current.pop(key, None)
        current["last_update"] = datetime.now().isoformat()
        self.write_json(self.memory_dir, "current_state.json", current)

    def get_current_state(self) -> dict:
        return self.read_json(self.memory_dir, "current_state.json")

    def prune_stale_state(self) -> int:
        """current_state.json'dan kaldirilmis alanlari siler."""
        current = self.get_current_state()
        removed = [k for k in _STALE_STATE_KEYS if k in current]
        if not removed:
            return 0
        for k in removed:
            del current[k]
        current["last_update"] = datetime.now().isoformat()
        self.write_json(self.memory_dir, "current_state.json", current)
        return len(removed)

    def delete_orphan_chat_rows(self) -> int:
        """Eski donemden kalan tekil chat kayitlarini siler (oturumlar session tipinde)."""
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM lifetime_log WHERE event_type = 'chat'")
            count = cursor.fetchone()[0]
            if count:
                cursor.execute("DELETE FROM lifetime_log WHERE event_type = 'chat'")
                conn.commit()
            conn.close()
            if count:
                logging.info(f"Orphan chat kayitlari silindi: {count}")
            return count
        except Exception as e:
            logging.warning(f"delete_orphan_chat_rows: {e}")
            return 0

    # ------------------------------------------------------------------
    # people.json (sifreli -- AES-256)
    # ------------------------------------------------------------------

    def get_people(self) -> dict:
        """people.json icerigini doner (sifreliyse cozulmus). Yoksa bos sozluk."""
        data = load_json_file(self.people_path)
        return data or {}

    def save_people(self, data: dict):
        save_json_file(self.people_path, data, encrypt=True)

    def get_known_entities(self) -> list:
        people_data = self.get_people()
        if not people_data:
            return ["kullanıcı"]
        return list(people_data.get("people", {}).keys())

    # ------------------------------------------------------------------
    # lifetime.db (alan sifreleme: raw_text + epis_observation)
    # ------------------------------------------------------------------

    def log_interaction(self, event_type: str, raw_text: str, observation: str = "", tags: list = None):
        if tags is None:
            tags = []
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO lifetime_log (timestamp, event_type, raw_text, epis_observation, ai_analysis_tags)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(),
            event_type,
            self.cipher.encrypt_str(raw_text),
            self.cipher.encrypt_str(observation),
            json.dumps(tags, ensure_ascii=False),
        ))
        conn.commit()
        conn.close()

    def get_recent_interactions(self, limit: int = 5) -> list:
        """Son N etkilesimi (eskiden yeniye) cozulmus halde doner."""
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp, event_type, raw_text, epis_observation, ai_analysis_tags "
                "FROM lifetime_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.warning(f"get_recent_interactions: {e}")
            return []

        results = [self._row_to_dict(r) for r in rows]
        results.reverse()  # eskiden yeniye
        return results

    def search_interactions(self, keywords: list, limit: int = 5, scan: int = 300) -> list:
        """
        Anahtar kelimelerle son `scan` kayit icinde arar (cozdukten sonra).
        Alanlar sifreli oldugu icin SQL LIKE kullanilamaz -- bellekte filtrelenir.
        """
        if not keywords:
            return []
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp, event_type, raw_text, epis_observation, ai_analysis_tags "
                "FROM lifetime_log ORDER BY id DESC LIMIT ?",
                (scan,),
            )
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.warning(f"search_interactions: {e}")
            return []

        kws     = [k.lower() for k in keywords if k]
        matches = []
        for r in rows:
            d    = self._row_to_dict(r)
            blob = f"{d['raw_text']} {d['observation']}".lower()
            if any(k in blob for k in kws):
                matches.append(d)
            if len(matches) >= limit:
                break
        matches.reverse()
        return matches

    def _row_to_dict(self, r: tuple) -> dict:
        return {
            "timestamp":   r[0],
            "event_type":  r[1],
            "raw_text":    self.cipher.decrypt_str(r[2] or ""),
            "observation": self.cipher.decrypt_str(r[3] or ""),
            "tags":        r[4] or "[]",
        }

    @staticmethod
    def parse_conversation_text(raw_text: str) -> list[dict]:
        """'kullanıcı: ...' / 'EPIS: ...' satirlarini mesaj listesine cevirir."""
        messages = []
        for line in (raw_text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("kullanıcı: "):
                messages.append({"role": "user", "text": line[6:].strip()})
            elif line.startswith("EPIS: "):
                messages.append({"role": "epis", "text": line[6:].strip()})
        return messages

    @staticmethod
    def _session_title(messages: list[dict], timestamp: str = "") -> str:
        for m in messages:
            if m.get("role") == "user" and m.get("text"):
                t = m["text"].strip()
                return t[:48] + ("…" if len(t) > 48 else "")
        if timestamp:
            return timestamp[:16].replace("T", " ")
        return "Sohbet"

    def list_chat_sessions(self, limit: int = 40) -> list[dict]:
        """lifetime.db'den kapanmis oturumlar (event_type=session)."""
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, timestamp, raw_text FROM lifetime_log "
                "WHERE event_type = 'session' ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.warning(f"list_chat_sessions: {e}")
            return []

        sessions: list[dict] = []
        for rid, ts, raw_enc in rows:
            raw  = self.cipher.decrypt_str(raw_enc or "")
            msgs = self.parse_conversation_text(raw)
            if not msgs:
                continue
            sessions.append({
                "id":        rid,
                "db_id":     rid,
                "timestamp": ts,
                "title":     self._session_title(msgs, ts),
                "preview":   next((m["text"] for m in reversed(msgs) if m["role"] == "epis"), "")[:80],
                "count":     len(msgs),
                "live":      False,
            })
        return sessions

    def get_session_messages(self, session_id: int) -> dict | None:
        """Tek oturumun tum mesajlari (yalnizca session kaydi)."""
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, timestamp, raw_text FROM lifetime_log "
                "WHERE id = ? AND event_type = 'session'",
                (session_id,),
            )
            row = cursor.fetchone()
            conn.close()
            if not row:
                return None

            rid, ts, raw_enc = row
            raw  = self.cipher.decrypt_str(raw_enc or "")
            msgs = self.parse_conversation_text(raw)
            return {
                "id": rid, "timestamp": ts, "live": False,
                "title": self._session_title(msgs, ts),
                "messages": msgs,
            }
        except Exception as e:
            logging.warning(f"get_session_messages: {e}")
            return None

    def get_session_buffer_lines(self) -> list[str]:
        """session_buffer.json — canli oturum satirlari (kullanıcı:/EPIS:)."""
        path = os.path.join(self.memory_dir, "session_buffer.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return list(data.get("lines") or [])
        except Exception as e:
            logging.warning(f"get_session_buffer_lines: {e}")
            return []

    def search_sessions(self, keywords: list, limit: int = 3) -> list:
        """Anahtar kelimeyle kapanmis oturumlarda ara."""
        if not keywords:
            return []
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, timestamp, raw_text FROM lifetime_log "
                "WHERE event_type = 'session' ORDER BY id DESC LIMIT 80"
            )
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.warning(f"search_sessions: {e}")
            return []

        kws = [k.lower() for k in keywords if k]
        hits = []
        for rid, ts, raw_enc in rows:
            raw = self.cipher.decrypt_str(raw_enc or "")
            blob = raw.lower()
            if not any(k in blob for k in kws):
                continue
            msgs = self.parse_conversation_text(raw)
            if not msgs:
                continue
            hits.append({
                "id":        rid,
                "timestamp": ts,
                "title":     self._session_title(msgs, ts),
                "raw_text":  raw,
                "messages":  msgs,
            })
            if len(hits) >= limit:
                break
        return hits

    def search_thinking_log(self, keywords: list, limit: int = 3, scan: int = 80) -> list:
        """thinking_log.jsonl — gecmis tur dusunce + cevap ozetleri."""
        path = os.path.join(self.memory_dir, "thinking_log.jsonl")
        if not os.path.exists(path) or not keywords:
            return []
        kws = [k.lower() for k in keywords if k]
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
        except Exception as e:
            logging.warning(f"search_thinking_log: {e}")
            return []

        matches = []
        for ln in reversed(lines[-scan:]):
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            blob = " ".join([
                row.get("user") or "",
                row.get("reasoning") or "",
                row.get("content") or "",
            ]).lower()
            if not any(k in blob for k in kws):
                continue
            matches.append(row)
            if len(matches) >= limit:
                break
        matches.reverse()
        return matches

    @staticmethod
    def summarize_thinking_row(row: dict, reasoning_max: int = 280, answer_max: int = 200) -> str:
        """Tek thinking_log satirini baglam icin kisa ozet."""
        ts = (row.get("ts") or "")[:16].replace("T", " ")
        user = (row.get("user") or "").strip().replace("\n", " ")
        reasoning = (row.get("reasoning") or "").strip().replace("\n", " ")
        content = (row.get("content") or "").strip()
        answer = content
        if content.startswith("{"):
            try:
                obj = json.loads(content)
                answer = (obj.get("message") or content).strip()
            except json.JSONDecodeError:
                pass
        if len(user) > 120:
            user = user[:120] + "..."
        if len(reasoning) > reasoning_max:
            reasoning = reasoning[:reasoning_max] + "..."
        if len(answer) > answer_max:
            answer = answer[:answer_max] + "..."
        parts = [f"[{ts}] Soru: {user or '(bos)'}"]
        if reasoning:
            parts.append(f"Dusunce: {reasoning}")
        if answer:
            parts.append(f"Cevap: {answer}")
        return " | ".join(parts)

    def get_episodic_context(self) -> dict:
        """Gece analizi / haftalik ozet + identity ogrenimleri (varsa)."""
        out = {}
        identity_names = ("identity_calculated.json", "epis_self.json")
        memory_names = ("morning_report.json", "weekly.json")
        for name in identity_names:
            path = os.path.join(self.identity_dir, name)
            if os.path.exists(path):
                data = self.read_json(self.identity_dir, name)
                if data:
                    out[name.replace(".json", "")] = data
        for name in memory_names:
            path = os.path.join(self.memory_dir, name)
            if os.path.exists(path):
                data = self.read_json(self.memory_dir, name)
                if data:
                    out[name.replace(".json", "")] = data
        return out
