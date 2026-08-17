"""
EPIS -- Mi Band bildirim istemcisi (Gadgetbridge / ADB veya HTTP webhook)

Kurulum: scripts/kurulum_band_bildirim.txt
"""

import os
import re
import json
import shutil
import logging
import subprocess

from dotenv import load_dotenv

THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
load_dotenv(dotenv_path=os.path.join(EPIS_ROOT, "Layer-3", "keys.env"))

import requests

logger = logging.getLogger("EPIS.BAND")

_DEFAULT_PACKAGE = "nodomain.freeyourgadget.gadgetbridge"
_INTENT_DEBUG    = "nodomain.freeyourgadget.gadgetbridge.command.DEBUG_SEND_NOTIFICATION"
_INTENT_PEBBLE   = "com.getpebble.action.SEND_NOTIFICATION"
_NOTIFY_TAG      = "epis_notify"


def _enabled() -> bool:
    return os.getenv("BAND_NOTIFICATIONS", "").lower() in ("1", "true", "yes")


def _truncate(text: str, max_len: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def _shell_quote(value: str) -> str:
    """ADB shell tek arguman olarak gecsin diye (bosluklu metin kesilmesin)."""
    return "'" + (value or "").replace("'", "'\"'\"'") + "'"


class BandClient:
    """EPIS mesajlarini Mi Band'e Gadgetbridge uzerinden iletir."""

    def __init__(self):
        self.mode    = (os.getenv("BAND_NOTIFY_MODE") or "adb").lower()
        self.package = os.getenv("GADGETBRIDGE_PACKAGE", _DEFAULT_PACKAGE)
        self.adb     = os.getenv("ADB_PATH") or shutil.which("adb") or "adb"
        self.device  = (os.getenv("ADB_DEVICE") or "").strip()
        self.url     = (os.getenv("BAND_NOTIFY_URL") or "").strip()
        self.max_len = int(os.getenv("BAND_MESSAGE_MAX_LEN", "250"))
        self.title   = os.getenv("BAND_NOTIFY_TITLE", "EPIS")
        # shell = guvenilir, Shell ikonu | epis = once GB intent (EPIS gonderen)
        self.style   = (os.getenv("BAND_NOTIFY_STYLE") or "shell").lower()

    def is_ready(self) -> bool:
        if not _enabled():
            return False
        if self.mode == "http":
            return bool(self.url)
        if self.mode == "adb":
            return self._adb_ok()
        logger.warning(f"Bilinmeyen BAND_NOTIFY_MODE: {self.mode}")
        return False

    def _adb_ok(self) -> bool:
        try:
            cmd = [self.adb]
            if self.device:
                cmd.extend(["-s", self.device])
            cmd.append("get-state")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return r.returncode == 0 and "device" in (r.stdout or "").lower()
        except Exception as e:
            logger.debug(f"ADB hazir degil: {e}")
            return False

    def notify(self, message: str, title: str | None = None, *, respect_quiet_hours: bool = False) -> bool:
        if not _enabled():
            return False
        if not message or not message.strip():
            return False
        if respect_quiet_hours and self._in_quiet_hours():
            logger.info("Band bildirimi atlandi: quiet_hours")
            return False

        body  = _truncate(message, self.max_len)
        label = title or self.title

        if self.mode == "http":
            return self._notify_http(label, body)
        return self._notify_adb(label, body)

    def _adb_cmd(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [self.adb]
        if self.device:
            cmd.extend(["-s", self.device])
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)

    def _adb_shell(self, script: str) -> subprocess.CompletedProcess:
        """Tek shell komutu — adb arguman birlestirme yuzunden metin kesilmesin."""
        return self._adb_cmd("shell", script)

    def _notify_adb(self, title: str, body: str) -> bool:
        """shell: telefon bildirimi. epis: once EPIS adli GB intent, sonra shell yedek."""
        if self.style == "epis":
            methods = (
                self._notify_via_gadgetbridge_debug,
                self._notify_via_pebblekit,
                self._notify_via_phone_notification,
            )
        else:
            methods = (
                self._notify_via_phone_notification,
                self._notify_via_gadgetbridge_debug,
                self._notify_via_pebblekit,
            )
        try:
            for fn in methods:
                if fn(title, body):
                    return True
            logger.warning("Band bildirimi: tum yontemler basarisiz")
            return False
        except FileNotFoundError:
            logger.warning("adb bulunamadi -- PATH'e ekle veya ADB_PATH ayarla.")
            return False
        except Exception as e:
            logger.error(f"Band bildirimi hatasi: {e}")
            return False

    def _notify_via_phone_notification(self, title: str, body: str) -> bool:
        """Telefona gercek bildirim — Gadgetbridge Mi Band'e yansitir."""
        script = (
            f"cmd notification post -t {_shell_quote(title)} "
            f"{_shell_quote(_NOTIFY_TAG)} {_shell_quote(body)}"
        )
        r = self._adb_shell(script)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "posting for user" in out.lower():
            logger.info(f"Band bildirimi (telefon) [{title}]: {body[:60]}")
            return True
        return False

    def _notify_via_gadgetbridge_debug(self, title: str, body: str) -> bool:
        script = (
            f"am broadcast -a {_INTENT_DEBUG} -p {_shell_quote(self.package)} "
            f"-e type GENERIC_SMS "
            f"-e sender {_shell_quote(title)} "
            f"-e subject {_shell_quote(title)} "
            f"-e body {_shell_quote(body)}"
        )
        r = self._adb_shell(script)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "broadcast completed" in out.lower():
            logger.info(f"Band bildirimi (GB debug) [{title}]: {body[:60]}")
            return True
        return False

    def _notify_via_pebblekit(self, title: str, body: str) -> bool:
        data = json.dumps([{"title": title, "body": body}], ensure_ascii=False)
        script = (
            f"am broadcast -a {_INTENT_PEBBLE} -p {_shell_quote(self.package)} "
            f"-e messageType PEBBLE_ALERT "
            f"-e notificationData {_shell_quote(data)}"
        )
        r = self._adb_shell(script)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "broadcast completed" in out.lower():
            logger.info(f"Band bildirimi (PebbleKit) [{title}]: {body[:60]}")
            return True
        return False

    def _in_quiet_hours(self) -> bool:
        try:
            from datetime import datetime
            from pathlib import Path

            habits_path = Path(__file__).resolve().parents[2] / "Layer-1" / "habits" / "habits.json"
            if not habits_path.exists():
                return False
            with open(habits_path, encoding="utf-8") as f:
                habits = json.load(f)
            cfg   = habits.get("proactive", {})
            if not cfg.get("quiet_hours_enabled", True):
                return False
            sched = habits.get("schedule", {})
            start_s = cfg.get("quiet_start") or sched.get("sleep_time", "23:30")
            end_s   = cfg.get("quiet_end") or sched.get("wake_time", "08:00")
            now   = datetime.now().time()
            start = datetime.strptime(start_s, "%H:%M").time()
            end   = datetime.strptime(end_s, "%H:%M").time()
            if start <= end:
                return start <= now <= end
            return now >= start or now <= end
        except Exception:
            return False

    def _notify_http(self, title: str, body: str) -> bool:
        try:
            r = requests.post(
                self.url,
                json={"title": title, "body": body, "sender": title},
                timeout=15,
            )
            if r.status_code < 300:
                logger.info(f"Band HTTP bildirimi [{title}]: {body[:60]}")
                return True
            logger.warning(f"Band HTTP {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.ConnectionError:
            logger.warning("Band webhook erisilemedi.")
            return False
        except Exception as e:
            logger.error(f"Band HTTP hatasi: {e}")
            return False
