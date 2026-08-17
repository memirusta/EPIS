"""
EPIS -- Telefon ekran izleme (ADB Wi-Fi)

Aktif uygulama paketi dumpsys ile okunur; ekran goruntusu alinmaz.
Gunluk sureler phone_daily.json'a yazilir; PC ScreenMonitor ile birlestirilir.

Kurulum: scripts/kurulum_phone_monitor.txt
"""

import os
import re
import json
import shutil
import logging
import subprocess
from datetime import datetime, date
from typing import Optional

from dotenv import load_dotenv

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
load_dotenv(dotenv_path=os.path.join(EPIS_ROOT, "Layer-3", "keys.env"))

logger = logging.getLogger(__name__)

PHONE_DAILY_PATH = os.path.join(EPIS_ROOT, "Layer-1", "memory", "phone_daily.json")

# Dikkat dagitici uygulamalar (paket -> limit kategorisi)
DISTRACT_PACKAGES = {
    "com.google.android.youtube": "youtube",
    "com.instagram.android": "reels",
    "com.zhiliaoapp.musically": "tiktok",
    "com.ss.android.ugc.trill": "tiktok",
    "com.ss.android.ugc.aweme": "tiktok",
}

IGNORE_FOCUS = frozenset({
    "NotificationShade",
    "Keyguard",
    "InputMethod",
    "com.android.systemui",
    "com.sec.android.app.launcher",
    "com.google.android.apps.nexuslauncher",
})

DISTRACT_LIMIT_KEYS = ("youtube", "reels", "tiktok")

_FOCUS_RE = re.compile(
    r"mCurrentFocus=Window\{[^}]+\s+u\d+\s+([^/\s}]+)",
    re.IGNORECASE,
)
_ACTIVITY_RE = re.compile(r"ACTIVITY\s+([\w.]+)/", re.IGNORECASE)


def _enabled() -> bool:
    return os.getenv("PHONE_MONITORING", "").lower() in ("1", "true", "yes")


def merge_category_minutes(*parts: dict) -> dict:
    merged: dict[str, float] = {}
    for part in parts:
        for key, val in (part or {}).items():
            merged[key] = round(merged.get(key, 0.0) + float(val), 1)
    return merged


class PhoneScreenMonitor:
    """Samsung/Android telefon: ADB ile on plandaki uygulamayi izler."""

    def __init__(self):
        self.adb = os.getenv("ADB_PATH") or shutil.which("adb") or "adb"
        self.device = (os.getenv("ADB_DEVICE") or "").strip()
        self._usage: dict[str, float] = {}
        self._categories: dict[str, float] = {}
        self._last_check = datetime.now()
        self._last_active: dict = {}
        self._today = date.today().isoformat()
        self._adb_failures = 0
        self._load_daily()

    def is_available(self) -> bool:
        if not _enabled():
            return False
        return self._adb_ok()

    def _adb_cmd(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [self.adb]
        if self.device:
            cmd.extend(["-s", self.device])
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    def _adb_shell(self, script: str) -> subprocess.CompletedProcess:
        return self._adb_cmd("shell", script)

    def _adb_ok(self) -> bool:
        try:
            r = self._adb_cmd("get-state")
            return r.returncode == 0 and "device" in (r.stdout or "").lower()
        except Exception:
            return False

    def _load_daily(self):
        self._reset_if_new_day()
        if not os.path.exists(PHONE_DAILY_PATH):
            return
        try:
            with open(PHONE_DAILY_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != self._today:
                return
            self._usage = {k: float(v) for k, v in (data.get("by_package") or {}).items()}
            self._categories = {k: float(v) for k, v in (data.get("by_category") or {}).items()}
            self._last_active = data.get("last_active") or {}
        except Exception as e:
            logger.warning(f"[PhoneMonitor] phone_daily.json okunamadi: {e}")

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(PHONE_DAILY_PATH), exist_ok=True)
            with open(PHONE_DAILY_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "date":        self._today,
                    "by_package":  {k: round(v, 1) for k, v in self._usage.items()},
                    "by_category": {k: round(v, 1) for k, v in self._categories.items()},
                    "last_active": self._last_active,
                    "updated_at":  datetime.now().isoformat(),
                    "source":      "adb",
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[PhoneMonitor] kayit hatasi: {e}")

    def reset_daily_if_needed(self):
        if date.today().isoformat() != self._today:
            self._usage = {}
            self._categories = {}
            self._last_active = {}
            self._today = date.today().isoformat()
            self._persist()
            logger.info("[PhoneMonitor] Yeni gun — sayaclar sifirlandi.")

    def _reset_if_new_day(self):
        self._today = date.today().isoformat()

    def _screen_on(self) -> bool:
        try:
            r = self._adb_shell("dumpsys power")
            text = (r.stdout or "") + (r.stderr or "")
            if "Display Power: state=OFF" in text:
                return False
            if "mWakefulness=Asleep" in text:
                return False
            if "mWakefulness=Awake" in text:
                return True
            if "Display Power: state=ON" in text:
                return True
            return "mHoldingDisplaySuspendBlocker=true" in text
        except Exception:
            return True

    def _foreground_from_window(self) -> Optional[str]:
        r = self._adb_shell("dumpsys window | grep mCurrentFocus")
        text = (r.stdout or "") + (r.stderr or "")
        m = _FOCUS_RE.search(text)
        if not m:
            return None
        token = m.group(1)
        if token in IGNORE_FOCUS:
            return None
        if "." in token:
            return token
        return None

    def _foreground_from_activity_top(self) -> Optional[str]:
        r = self._adb_shell("dumpsys activity top | head -n 5")
        text = (r.stdout or "") + (r.stderr or "")
        m = _ACTIVITY_RE.search(text)
        if not m:
            return None
        pkg = m.group(1)
        if pkg in IGNORE_FOCUS:
            return None
        return pkg

    def adb_connected(self) -> bool:
        return self._adb_ok()

    def phone_screen_on(self) -> bool:
        return self._screen_on()

    def peek_foreground(self) -> Optional[str]:
        """Canli okuma (sohbet aninda); PHONE_MONITORING sart degil, ADB yeterli."""
        try:
            if not self._adb_ok():
                return None
            if not self._screen_on():
                return None
            pkg = self._foreground_from_window() or self._foreground_from_activity_top()
            if pkg:
                self._adb_failures = 0
            return pkg
        except Exception as e:
            self._adb_failures += 1
            if self._adb_failures <= 3:
                logger.warning(f"[PhoneMonitor] Canli ADB okuma hatasi: {e}")
            return None

    def get_foreground_package(self) -> Optional[str]:
        if not self.is_available():
            return None
        return self.peek_foreground()

    @classmethod
    def distraction_from_package(cls, package: Optional[str]) -> Optional[str]:
        if not package:
            return None
        return DISTRACT_PACKAGES.get(package)

    @classmethod
    def package_label(cls, package: str) -> str:
        labels = {
            "com.google.android.youtube": "YouTube",
            "com.instagram.android": "Instagram",
            "com.zhiliaoapp.musically": "TikTok",
            "com.ss.android.ugc.trill": "TikTok",
        }
        return labels.get(package, package.rsplit(".", 1)[-1])

    def tick(self):
        if not self.is_available():
            return

        now = datetime.now()
        elapsed = (now - self._last_check).total_seconds()
        self._last_check = now

        if date.today().isoformat() != self._today:
            self.reset_daily_if_needed()

        if not self._screen_on():
            return

        pkg = self.get_foreground_package()
        if not pkg:
            return

        self._usage[pkg] = self._usage.get(pkg, 0.0) + elapsed

        distract = self.distraction_from_package(pkg)
        if distract:
            self._categories[distract] = self._categories.get(distract, 0.0) + elapsed

        self._last_active = {
            "package":  pkg,
            "label":    self.package_label(pkg),
            "category": distract or "other",
        }
        self._persist()

    def get_category_minutes(self) -> dict[str, float]:
        return {k: round(v / 60, 1) for k, v in self._categories.items()}

    def get_package_minutes(self) -> dict[str, float]:
        return {k: round(v / 60, 1) for k, v in self._usage.items()}

    def get_daily_summary(self) -> dict:
        cats = self.get_category_minutes()
        return {
            "date":       self._today,
            "categories": cats,
            "by_package": self.get_package_minutes(),
            "active":     self._last_active,
            "total_distraction_min": round(sum(cats.get(k, 0) for k in DISTRACT_LIMIT_KEYS), 1),
        }

    @staticmethod
    def load_saved_summary() -> dict:
        if not os.path.exists(PHONE_DAILY_PATH):
            return {}
        try:
            with open(PHONE_DAILY_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != date.today().isoformat():
                return {}
            cats = {
                k: round(float(v) / 60, 1)
                for k, v in (data.get("by_category") or {}).items()
            }
            pkgs = {
                k: round(float(v) / 60, 1)
                for k, v in (data.get("by_package") or {}).items()
            }
            return {
                "date":       data.get("date"),
                "categories": cats,
                "by_package": pkgs,
                "active":     data.get("last_active") or {},
            }
        except Exception:
            return {}

    @staticmethod
    def check_limit_exceeded(categories: dict, habits: dict) -> Optional[str]:
        limits = habits.get("screen_limits", {})
        for key in DISTRACT_LIMIT_KEYS:
            used = categories.get(key, 0)
            cap = limits.get(f"{key}_minutes", 60 if key == "youtube" else 30)
            if used >= cap:
                return key
        return None
