#!/usr/bin/env python3
"""
EPIS -- Kairos: Proaktif Motor
================================
Zamanlar habits.json'dan okunur.
Uyku Mi Band 6 (Gadgetbridge) ile tespit edilir.
Cikti pending.json'a yazilir; mesaj tek shared AgentCore uzerinden uretilip teslim edilir.

Calistirma : python kairos.py
"""

import os
import sys
import json
import uuid
import logging
import time
from datetime import datetime, date, timedelta

import schedule

THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
sys.path.insert(0, THIS_DIR)

from memory_manager import MemoryManager
from sensors import GadgetbridgeReader, ScreenMonitor
from phone_monitor import PhoneScreenMonitor, merge_category_minutes
from proactive_delivery import ProactiveDelivery

MEMORY_DIR     = os.path.join(EPIS_ROOT, "Layer-1", "memory")
IDENTITY_DIR   = os.path.join(EPIS_ROOT, "Layer-1", "identity")
HABITS_PATH    = os.path.join(EPIS_ROOT, "Layer-1", "habits", "habits.json")
PENDING_PATH   = os.path.join(MEMORY_DIR, "pending.json")
DEADLINES_PATH = os.path.join(MEMORY_DIR, "deadlines.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - KAIROS - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(EPIS_ROOT, "kairos.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("EPIS.KAIROS")


class Kairos:

    def __init__(self):
        self.memory       = MemoryManager()
        self.habits       = self._load_habits()
        self.gadgetbridge = GadgetbridgeReader()
        self.screen       = ScreenMonitor()
        self.phone        = PhoneScreenMonitor()
        self.proactive    = ProactiveDelivery(self.memory, self.habits)

        self._sleep_triggered_today = False
        self._nightly_running       = False

        logger.info(
            f"Kairos baslatildi. "
            f"Gadgetbridge: {'aktif' if self.gadgetbridge.is_available() else 'DB yok'}. "
            f"PC ekran: {'aktif' if self.screen.is_available() else 'pywin32 eksik'}. "
            f"Telefon: {'aktif' if self.phone.is_available() else 'ADB kapali'}. "
            f"Shared runtime: {'aktif' if self.proactive.runtime.is_ready() else 'bagli degil'} "
            f"(kanal: {self.proactive._channel()})"
        )

    def _load_habits(self) -> dict:
        if not os.path.exists(HABITS_PATH):
            logger.warning("habits.json bulunamadi -- varsayilan zamanlar.")
            return {
                "schedule": {"wake_time": "08:00", "end_of_day_time": "22:00", "sleep_time": "23:30"},
                "screen_limits": {"reels_minutes": 30, "youtube_minutes": 60, "tiktok_minutes": 20},
                "stress_threshold_bpm": 130,
            }
        try:
            with open(HABITS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"habits.json okunamadi: {e}")
            return {}

    def _reload_habits(self):
        self.habits = self._load_habits()
        self.proactive.reload_habits(self.habits)
        schedule.clear()
        self._schedule_jobs()
        logger.info("habits.json yeniden yuklendi.")

    def run(self):
        self._schedule_jobs()
        logger.info("Kairos aktif -- bekliyor.")
        while True:
            schedule.run_pending()
            self._sensor_loop()
            time.sleep(30)

    def _schedule_jobs(self):
        sched      = self.habits.get("schedule", {})
        wake       = sched.get("wake_time",       "08:00")
        end_of_day = sched.get("end_of_day_time", "22:00")
        sleep_time = sched.get("sleep_time",      "23:30")
        pre_sleep  = self._subtract_minutes(sleep_time, 30)

        schedule.every().day.at(wake).do(self._trigger_morning)
        schedule.every().day.at(end_of_day).do(self._trigger_end_of_day)
        schedule.every().day.at(pre_sleep).do(self._trigger_pre_sleep)
        schedule.every().day.at(sleep_time).do(self._trigger_fallback_nightly)
        schedule.every(1).hours.do(self._trigger_pending_check)
        schedule.every(1).hours.do(self._trigger_deadline_check)

        idle_iv = self.habits.get("proactive", {}).get("idle_check_interval_minutes", 10)
        if idle_iv > 0:
            schedule.every(idle_iv).minutes.do(self._check_idle)

        logger.info(
            f"Zamanlar: sabah={wake} | gun_sonu={end_of_day} | "
            f"uyku_oncesi={pre_sleep} | gece_yedek={sleep_time}"
        )

    # ------------------------------------------------------------------
    # Sensor Döngüsü
    # ------------------------------------------------------------------

    def _sensor_loop(self):
        if self.screen.is_available():
            self.screen.tick()
        if self.phone.is_available():
            self.phone.tick()
        self._check_sleep_onset()
        self._check_stress_anomaly()
        self._check_screen_limit()
        self._update_screen_state()

        if datetime.now().hour == 0 and datetime.now().minute < 1:
            self._sleep_triggered_today = False
            self.screen.reset_daily_if_needed()
            self.phone.reset_daily_if_needed()
            self.memory.update_current_state({"screen_limit_alerted_today": False})

    def _check_sleep_onset(self):
        if self._sleep_triggered_today or self._nightly_running:
            return
        if not self.gadgetbridge.is_available():
            return
        if self.gadgetbridge.get_current_sleep_state() == "sleeping":
            logger.info("Uyku tespit edildi -- NR tetikleniyor.")
            self._sleep_triggered_today = True
            self._trigger_nightly()

    def _check_stress_anomaly(self):
        if not self.gadgetbridge.is_available():
            return
        threshold = self.habits.get("stress_threshold_bpm", 100)
        if not self.gadgetbridge.is_stress_anomaly(threshold_bpm=threshold):
            return

        current    = self.memory.get_current_state()
        last_alert = current.get("last_stress_alert")
        if last_alert:
            mins = (datetime.now() - datetime.fromisoformat(last_alert)).total_seconds() / 60
            if mins < 30:
                return

        hr      = self.gadgetbridge.get_heart_rate_summary(hours=0.25)
        avg_bpm = hr.get("avg", "?")
        self._write_pending(
            trigger_type="anomaly",
            context=f"Kalp hızı son 15 dakikadır {avg_bpm} bpm.",
            priority="high",
        )
        self.memory.update_current_state({"last_stress_alert": datetime.now().isoformat()})
        logger.info(f"Stres anomalisi: {avg_bpm} bpm")

    def _merged_distraction_minutes(self) -> dict:
        pc = self.screen.get_category_minutes() if self.screen.is_available() else {}
        ph = self.phone.get_category_minutes() if self.phone.is_available() else {}
        return merge_category_minutes(pc, ph)

    def _update_screen_state(self):
        if not self.screen.is_available() and not self.phone.is_available():
            return
        merged = self._merged_distraction_minutes()
        pc_active = self.screen.get_daily_summary().get("active", {}) if self.screen.is_available() else {}
        ph_active = self.phone.get_daily_summary().get("active", {}) if self.phone.is_available() else {}
        self.memory.update_current_state({
            "screen_usage_today":      merged,
            "screen_usage_pc":         self.screen.get_category_minutes() if self.screen.is_available() else {},
            "screen_usage_phone":      self.phone.get_category_minutes() if self.phone.is_available() else {},
            "screen_active_app":       pc_active.get("process") or ph_active.get("package"),
            "screen_active_title":     (pc_active.get("title") or ph_active.get("label") or "")[:80],
            "phone_active_package":    ph_active.get("package"),
            "screen_updated_at":       datetime.now().isoformat(),
        })

    def _check_screen_limit(self):
        if not self.screen.is_available() and not self.phone.is_available():
            return
        cats = self._merged_distraction_minutes()
        exceeded = PhoneScreenMonitor.check_limit_exceeded(cats, self.habits)
        if not exceeded:
            return
        current = self.memory.get_current_state()
        if current.get("screen_limit_alerted_today"):
            return
        limits = self.habits.get("screen_limits", {})
        used   = cats.get(exceeded, 0)
        cap    = limits.get(f"{exceeded}_minutes", 30)
        labels = {"youtube": "YouTube", "reels": "Reels/Instagram", "tiktok": "TikTok"}
        self._write_pending(
            trigger_type="intervention",
            context=(
                f"Bugun {labels.get(exceeded, exceeded)} icin toplam {used:.0f} dk gectin "
                f"(PC + telefon, limit {cap} dk)."
            ),
            priority="medium",
        )
        self.memory.update_current_state({"screen_limit_alerted_today": True})

    def _check_idle(self):
        """Aktif saatlerde X dakika mesaj yoksa hafif check-in."""
        cfg = self.habits.get("proactive", {})
        idle_min = int(cfg.get("idle_minutes", 60))
        if idle_min <= 0:
            return

        active = self.habits.get("active_hours", {})
        if active:
            try:
                now_t = datetime.now().time()
                a0 = datetime.strptime(active.get("start", "09:00"), "%H:%M").time()
                a1 = datetime.strptime(active.get("end", "21:00"), "%H:%M").time()
                if a0 <= a1:
                    if not (a0 <= now_t <= a1):
                        return
                elif not (now_t >= a0 or now_t <= a1):
                    return
            except ValueError:
                pass

        state = self.memory.get_current_state()
        if state.get("session_active"):
            return

        last_at = state.get("last_message_at")
        if not last_at:
            return

        try:
            mins = (datetime.now() - datetime.fromisoformat(last_at)).total_seconds() / 60
        except ValueError:
            return

        if mins < idle_min:
            return

        logger.info(f"Trigger: idle ({int(mins)} dk)")
        self._write_pending(
            trigger_type="idle",
            context=f"kullanıcı yaklasik {int(mins)} dakikadir EPIS'e yazmadi.",
            priority="low",
        )

    # ------------------------------------------------------------------
    # Nightly Recalculation
    # ------------------------------------------------------------------

    def _trigger_nightly(self):
        if self._nightly_running:
            return
        self._nightly_running = True
        logger.info("Trigger: nightly_recalculation")
        try:
            sensor_data = {}
            if self.gadgetbridge.is_available():
                sensor_data["biometrics"] = self.gadgetbridge.get_today_summary()
            if self.screen.is_available():
                sensor_data["screen_usage_pc"] = self.screen.get_usage_minutes()
            if self.phone.is_available():
                sensor_data["screen_usage_phone"] = self.phone.get_package_minutes()
            merged = self._merged_distraction_minutes()
            if merged:
                sensor_data["screen_distraction"] = merged

            from nightly_recalculation import NightlyRecalculation
            report = NightlyRecalculation().run(sensor_data=sensor_data)

            if report.get("status") == "failed":
                self._write_pending(
                    trigger_type="nightly_error",
                    context=f"Gece analizi başarısız. Hatalar: {report.get('errors', [])}",
                    priority="high",
                )
            else:
                logger.info(f"NR tamamlandi: {report.get('status')}")
                self._reload_habits()

        except Exception as e:
            logger.error(f"NR trigger hatasi: {e}")
            self._write_pending(
                trigger_type="nightly_error",
                context=f"Gece analizi başlatılamadı: {e}",
                priority="high",
            )
        finally:
            self._nightly_running = False

    def _trigger_fallback_nightly(self):
        if self._sleep_triggered_today:
            logger.info("NR zaten uyku sensoru ile tetiklendi -- fallback atlandi.")
            return
        logger.info("Trigger: fallback_nightly")
        self._sleep_triggered_today = True
        self._trigger_nightly()

    # ------------------------------------------------------------------
    # Günlük Triggerlar
    # ------------------------------------------------------------------

    def _trigger_morning(self):
        logger.info("Trigger: morning")
        report_path = os.path.join(MEMORY_DIR, "morning_report.json")
        context     = "kullanıcı sabah uyandı."

        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                highlights   = report.get("highlights", [])
                tomorrow_ctx = report.get("tomorrow_context", "")
                epis_voice   = (report.get("epis_voice") or "").strip()
                if epis_voice:
                    context = (
                        "kullanıcı sabah uyandı. EPIS'in gece yorumu (kendi sesi): "
                        + epis_voice
                    )
                else:
                    if highlights:
                        context += f" Dünün öne çıkanları: {' | '.join(highlights[:3])}."
                    if tomorrow_ctx:
                        context += f" Bugün için bağlam: {tomorrow_ctx}."
            except Exception as e:
                logger.error(f"Morning report okunamadi: {e}")

        self._write_pending(trigger_type="morning", context=context, priority="medium")

    def _trigger_end_of_day(self):
        logger.info("Trigger: end_of_day")
        if self.memory.get_current_state().get("session_active", False):
            return
        self._write_pending(
            trigger_type="end_of_day",
            context="Gün bitmek üzere, kullanıcı henüz yazmadı.",
            priority="low",
        )

    def _trigger_pre_sleep(self):
        logger.info("Trigger: pre_sleep")
        self._write_pending(
            trigger_type="pre_sleep",
            context="kullanıcının uyku saatine 30 dakika kaldı.",
            priority="low",
        )

    def _trigger_deadline_check(self):
        if not os.path.exists(DEADLINES_PATH):
            return
        try:
            with open(DEADLINES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        now      = datetime.now()
        modified = False

        for dl in data.get("deadlines", []):
            if dl.get("status") != "active":
                continue
            due_str  = dl.get("due_date", "")
            due_time = dl.get("due_time", "23:59")
            try:
                due_dt = datetime.strptime(f"{due_str} {due_time}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            hours_left = (due_dt - now).total_seconds() / 3600
            if hours_left < 0:
                continue

            alerted_at = dl.get("alerted_at", [])
            for threshold in dl.get("notify_before_hours", [24, 2]):
                key = str(threshold)
                if key in alerted_at:
                    continue
                if hours_left <= threshold:
                    label = f"{int(hours_left)} saat" if hours_left >= 1 else "az bir süre"
                    self._write_pending(
                        trigger_type="deadline",
                        context=f"'{dl.get('title')}' deadline'ına {label} kaldı.",
                        priority="high" if threshold <= 2 else "medium",
                    )
                    alerted_at.append(key)
                    dl["alerted_at"] = alerted_at
                    modified = True
                    logger.info(f"Deadline uyarisi: {dl.get('title')} -- {label} kaldi")

        if modified:
            with open(DEADLINES_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def _trigger_pending_check(self):
        data  = self._read_pending()
        items = data.get("items", [])
        now   = datetime.now()

        overdue = [
            it for it in items
            if it.get("status") == "pending"
            and it.get("priority") in ("high", "urgent")
            and self._hours_since(it.get("created_at", ""), now) > 12
        ]
        if overdue:
            logger.warning(f"Suresi gecmis {len(overdue)} yuksek oncelikli pending oge.")

        data["items"] = [
            it for it in items
            if not (
                it.get("status") == "delivered"
                and self._hours_since(it.get("created_at", ""), now) > 168
            )
        ]
        pruned = len(items) - len(data["items"])
        if pruned:
            logger.info(f"Pending temizlendi: {pruned} eski oge silindi.")

        data["last_checked"] = now.isoformat()
        self._save_pending(data)

    # ------------------------------------------------------------------
    # Pending Yaz / Oku
    # ------------------------------------------------------------------

    def _write_pending(self, trigger_type: str, context: str, priority: str = "medium"):
        """Ham olguyu pending'e yazar; mumkunse aninda EPIS sesiyle WA push."""
        data = self._read_pending()
        item = {
            "id":           str(uuid.uuid4()),
            "created_at":   datetime.now().isoformat(),
            "trigger_type": trigger_type,
            "context":      context,
            "priority":     priority,
            "status":       "pending",
        }
        data["items"].append(item)
        data["last_updated"] = datetime.now().isoformat()
        self._save_pending(data)
        logger.info(f"Pending: [{priority}] {trigger_type} -- {context[:60]}")

        result = self.proactive.apply_push_to_item(item)
        if result.get("pushed"):
            data = self._read_pending()
            for i, it in enumerate(data.get("items", [])):
                if it.get("id") == item["id"]:
                    data["items"][i] = item
                    break
            data["last_updated"] = datetime.now().isoformat()
            self._save_pending(data)
        else:
            logger.info(f"Aninda push yok ({result.get('reason')}) -- pending bekliyor")

    def _read_pending(self) -> dict:
        if not os.path.exists(PENDING_PATH):
            return {"items": [], "last_updated": datetime.now().isoformat()}
        try:
            with open(PENDING_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"items": [], "last_updated": datetime.now().isoformat()}

    def _save_pending(self, data: dict):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Yardımcılar
    # ------------------------------------------------------------------

    @staticmethod
    def _hours_since(iso_str: str, now: datetime) -> float:
        try:
            return (now - datetime.fromisoformat(iso_str)).total_seconds() / 3600
        except Exception:
            return 0.0

    @staticmethod
    def _subtract_minutes(time_str: str, minutes: int) -> str:
        try:
            base = datetime.strptime(time_str, "%H:%M")
            return (base - timedelta(minutes=minutes)).strftime("%H:%M")
        except Exception:
            return time_str


if __name__ == "__main__":
    print("EPIS Kairos -- Baslatiliyor")
    print("=" * 40)
    Kairos().run()
