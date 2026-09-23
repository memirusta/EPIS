#!/usr/bin/env python3
"""EPIS proactive delivery: Kairos -> shared AgentCore -> delivery channels.

The local worker decides *when* a proactive event is warranted and keeps raw
sensor/memory state local.  Message generation belongs to the single shared
AgentCore; this module never starts a Layer1Engine or a second chat session.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from band_client import BandClient
from shared_runtime_client import SharedRuntimeClient
from whatsapp_client import WhatsAppClient


logger = logging.getLogger("EPIS.PROACTIVE")


# ---------------------------------------------------------------------------
# Legacy Kairos prompt compatibility
# ---------------------------------------------------------------------------
#
# These are pure formatting helpers kept for diagnostics/tests and old Kairos
# tooling. They MUST NOT start Layer1Engine or create a second conversational
# session. Production proactive generation belongs to SharedRuntimeClient /
# the shared AgentCore.

TRIGGER_HINTS = {
    "anomaly": "Endişeli ama sakin, kısa.",
    "intervention": "Nazik hatırlatma, baskı yok.",
    "morning": "Kısa günaydın; gerekirse dünün özeti.",
    "end_of_day": "Hafif check-in, baskı yok.",
    "pre_sleep": "Sakin, kısa.",
    "deadline": "Net, kısa.",
    "nightly_error": "Sade dille.",
    "drift": "Fark ettir, yargılama.",
    "idle": "'Nasılsın' tarzı, hafif.",
}

TRIGGER_SITUATION = {
    "anomaly": "Nabız verilerinde olağandışı bir yükseliş var.",
    "intervention": "Ekran süresi limitine yaklaşıldı veya aşıldı.",
    "morning": "Sabah oldu.",
    "end_of_day": "Gün sonuna yaklaşıyoruz.",
    "pre_sleep": "Uyku saatine yaklaşıyoruz.",
    "deadline": "Yaklaşan bir deadline var.",
    "nightly_error": "Gece analizi tamamlanamadı.",
    "drift": "Son günlerde bir alışkanlıkta sapma görünüyor.",
    "idle": "Bir süredir kullanıcıdan mesaj gelmedi.",
}

PROACTIVE_SYSTEM = """Sen EPIS'sin: kullanıcının yakin, sicak ve durust kisisel yapay zekasi.

Gorevin: kullanıcıya kisa proaktif mesaj yazmak (en fazla 2-3 cumle).

Kurallar:
- Yalnizca akici Turkce; Ingilizce kelime veya karisik dil kullanma
- kullanıcıya 'sen' de; 'siz' deme
- Sadece mesaj metnini yaz — baslik, imza ('EPIS'), JSON, talimat yok
- Verilen detay disinda bilgi uydurma
- 'modern asistan', 'yardimci olmak icin tasarlandim', 'Sen EPIS' deme
- Arkadas gibi konus; robot veya resmi asistan gibi degil"""


def build_kairos_user_message(trigger_type: str, context: str) -> str:
    """Build the legacy diagnostic prompt without invoking any model."""
    situation = TRIGGER_SITUATION.get(
        trigger_type,
        "EPIS'in kullanıcıya ulasmak istedigi bir an.",
    )
    hint = TRIGGER_HINTS.get(
        trigger_type,
        "Kısa ve samimi.",
    )
    detail = context.strip() if context and context.strip() else situation

    return (
        f"Durum: {situation}\n"
        f"Detay: {detail}\n"
        f"Ton: {hint}"
    )


def build_kairos_prompt(trigger_type: str, context: str) -> str:
    """Backward-compatible alias for diagnostic tooling."""
    return build_kairos_user_message(trigger_type, context)


DEFAULT_COOLDOWNS_MIN = {
    "idle": 120,
    "anomaly": 30,
    "intervention": 1440,
    "morning": 1440,
    "end_of_day": 1440,
    "pre_sleep": 1440,
    "deadline": 60,
    "nightly_error": 360,
    "drift": 720,
}


class ProactiveDelivery:
    """Generate through the shared brain, then mirror to local channels."""

    def __init__(self, memory=None, habits: dict | None = None):
        self.memory = memory
        self.habits = habits or {}
        self.wa = WhatsAppClient()
        self.band = BandClient()
        self.runtime = SharedRuntimeClient()
        self._push_cooldowns: dict[str, datetime] = {}

    def _channel(self) -> str:
        return os.getenv("PROACTIVE_CHANNEL") or self._cfg().get("channel", "ui")

    def reload_habits(self, habits: dict):
        self.habits = habits or {}

    def _cfg(self) -> dict:
        return self.habits.get("proactive", {})

    def _in_quiet_hours(self) -> bool:
        if not self._cfg().get("quiet_hours_enabled", True):
            return False
        sched = self.habits.get("schedule", {})
        start_s = self._cfg().get("quiet_start") or sched.get("sleep_time", "23:30")
        end_s = self._cfg().get("quiet_end") or sched.get("wake_time", "08:00")
        try:
            now = datetime.now().time()
            start = datetime.strptime(start_s, "%H:%M").time()
            end = datetime.strptime(end_s, "%H:%M").time()
        except ValueError:
            return False
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    def _cooldown_minutes(self, trigger_type: str) -> int:
        custom = self._cfg().get("cooldowns", {})
        if trigger_type in custom:
            return int(custom[trigger_type])
        return DEFAULT_COOLDOWNS_MIN.get(trigger_type, 15)

    def should_push(self, trigger_type: str, priority: str) -> tuple[bool, str]:
        if not self._cfg().get("instant_push", True):
            return False, "instant_push_kapali"

        if self._in_quiet_hours():
            night_ok = trigger_type in ("anomaly", "deadline") and priority in ("high", "urgent")
            if not night_ok:
                return False, "quiet_hours"

        last = self._push_cooldowns.get(trigger_type)
        cd = self._cooldown_minutes(trigger_type)
        if last and (datetime.now() - last).total_seconds() / 60 < cd:
            return False, "cooldown"

        # Even WA-only messages are generated by the shared brain.  If the
        # runtime is down, the pending item remains pending instead of falling
        # back to a second local model/session.
        if not self.runtime.is_ready():
            return False, "shared_runtime_unavailable"

        channel = self._channel()
        if channel not in ("ui", "wa", "both"):
            return False, "invalid_channel"
        if channel == "wa" and not self.wa.is_ready():
            return False, "whatsapp_unavailable"
        return True, "ok"

    def _send_channels(
        self,
        epis_msg: str,
        trigger_type: str,
        priority: str,
        runtime_result: dict,
    ) -> tuple[bool, str]:
        channel = self._channel()
        sent = False
        via: list[str] = []

        # /internal/proactive already persists the assistant event in the
        # shared hot session and broadcasts it to connected Desktop/Mobile.
        if channel in ("ui", "both") and runtime_result.get("ok"):
            sent = True
            via.append("shared-session")

        if channel in ("wa", "both") and self.wa.is_ready():
            if self.wa.send(epis_msg):
                sent = True
                via.append("wa")

        if self.band.is_ready() and self.band.notify(
            epis_msg,
            respect_quiet_hours=True,
        ):
            sent = True
            via.append("band")

        return sent, "+".join(via) if via else ""

    def push(self, trigger_type: str, context: str, priority: str = "medium") -> dict:
        ok, reason = self.should_push(trigger_type, priority)
        if not ok:
            logger.info("Push atlandi [%s]: %s", trigger_type, reason)
            return {"pushed": False, "reason": reason}

        runtime_result = self.runtime.proactive(trigger_type, context, priority)
        if not runtime_result.get("ok"):
            reason = runtime_result.get("error") or "shared_runtime_failed"
            logger.warning("Shared proactive failed [%s]: %s", trigger_type, reason)
            return {"pushed": False, "reason": reason}

        epis_msg = str(runtime_result.get("message") or "").strip()
        if not epis_msg:
            return {"pushed": False, "reason": "empty_message"}

        sent, via = self._send_channels(
            epis_msg,
            trigger_type,
            priority,
            runtime_result,
        )
        if sent:
            self._push_cooldowns[trigger_type] = datetime.now()
            if self.memory:
                self.memory.update_current_state({
                    "last_proactive_push_at": datetime.now().isoformat(),
                    "last_proactive_trigger": trigger_type,
                })
            logger.info("Push [%s] via %s: %s", trigger_type, via, epis_msg[:80])
        else:
            logger.warning("Push basarisiz [%s]", trigger_type)

        return {
            "pushed": sent,
            "message": epis_msg,
            "via": via,
            "reason": "ok" if sent else "send_fail",
            "shared_request_id": runtime_result.get("request_id"),
        }

    def apply_push_to_item(self, item: dict) -> dict:
        context = item.get("context") or item.get("message") or ""
        result = self.push(
            item.get("trigger_type", ""),
            context,
            item.get("priority", "medium"),
        )
        if result.get("pushed"):
            item["status"] = "delivered"
            item["delivered_at"] = datetime.now().isoformat()
            item["delivery"] = result.get("via") or "shared-session"
            item["epis_message"] = result.get("message")
        return result
