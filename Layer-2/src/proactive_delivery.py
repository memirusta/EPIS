#!/usr/bin/env python3
"""
EPIS -- Anlik proaktif teslim (Kairos -> Layer-1 -> UI / WhatsApp)
"""

import os
import re
import logging
from datetime import datetime

from dotenv import load_dotenv

THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
load_dotenv(dotenv_path=os.path.join(EPIS_ROOT, "Layer-3", "keys.env"))

from whatsapp_client import WhatsAppClient
from ui_client import UIClient
from band_client import BandClient

logger = logging.getLogger("EPIS.PROACTIVE")

TRIGGER_HINTS = {
    "anomaly":       "Endişeli ama sakin, kısa.",
    "intervention":  "Nazik hatırlatma, baskı yok.",
    "morning":       "Kısa günaydın; gerekirse dünün özeti.",
    "end_of_day":    "Hafif check-in, baskı yok.",
    "pre_sleep":     "Sakin, kısa.",
    "deadline":      "Net, kısa.",
    "nightly_error": "Sade dille.",
    "drift":         "Fark ettir, yargılama.",
    "idle":          "'Nasılsın' tarzı, hafif.",
}

# Model icin dogal durum — teknik tetik adi yok
TRIGGER_SITUATION = {
    "anomaly":       "Nabız verilerinde olağandışı bir yükseliş var.",
    "intervention":  "Ekran süresi limitine yaklaşıldı veya aşıldı.",
    "morning":       "Sabah oldu.",
    "end_of_day":    "Gün sonuna yaklaşıyoruz.",
    "pre_sleep":     "Uyku saatine yaklaşıyoruz.",
    "deadline":      "Yaklaşan bir deadline var.",
    "nightly_error": "Gece analizi tamamlanamadı.",
    "drift":         "Son günlerde bir alışkanlıkta sapma görünüyor.",
    "idle":          "Bir süredir kullanıcıdan mesaj gelmedi.",
}


# Proaktif mesajlar icin kisa kimlik — tam system prompt + sensor baglami Qwen'i bozar
PROACTIVE_SYSTEM = """Sen EPIS'sin: kullanıcının yakin, sicak ve durust kisisel yapay zekasi.

Gorevin: kullanıcıya kisa proaktif mesaj yazmak (en fazla 2-3 cumle).

Kurallar:
- Yalnizca akici Turkce; Ingilizce kelime veya karisik dil kullanma
- kullanıcıya 'sen' de; 'siz' deme
- Sadece mesaj metnini yaz — baslik, imza ('EPIS'), JSON, talimat yok
- Verilen detay disinda bilgi uydurma
- 'modern asistan', 'yardimci olmak icin tasarlandim', 'Sen EPIS' deme
- Arkadas gibi konus; robot veya resmi asistan gibi degil"""


def _extract_idle_minutes(text: str) -> int | None:
    m = re.search(r"(\d+)\s*dakika", (text or "").lower())
    return int(m.group(1)) if m else None


def _use_proactive_template_only(engine) -> bool:
    """Fine-tune olmayan Qwen proaktifte guvenilir degil."""
    forced = os.getenv("PROACTIVE_TEMPLATE_ONLY", "").lower()
    if forced in ("1", "true", "yes"):
        return True
    if forced in ("0", "false", "no"):
        return False
    model = (getattr(engine.backend, "model", "") or "").lower()
    if "qwen-epis" in model or "epis" in model:
        return False
    if engine.backend_name == "qwen":
        return True
    return False


def _extract_bpm(text: str) -> int | None:
    m = re.search(r"(\d{2,3})\s*(?:bpm|ppm)", (text or "").lower())
    return int(m.group(1)) if m else None


def fallback_proactive_message(trigger_type: str, context: str) -> str:
    """Model kotu urettiginde guvenilir Turkce sablon."""
    ctx = (context or "").strip()
    bpm = _extract_bpm(ctx)

    if trigger_type == "anomaly" and bpm:
        return (
            f"Nabzın son bir süredir {bpm} civarında — normalinden biraz yüksek görünüyor. "
            "Bir nefes al, acele etme; nasıl hissediyorsun?"
        )
    if trigger_type == "idle":
        mins = _extract_idle_minutes(ctx)
        if mins and mins >= 120:
            h = mins // 60
            return f"Bir süredir ses yok — yaklaşık {h} saattir yazışmadık. Nasılsın?"
        if mins and mins >= 60:
            return f"Bir süredir ses yok — yaklaşık bir saattir yazışmadık. Nasılsın?"
        return "Bir süredir ses yok — nasılsın?"
    if trigger_type == "morning":
        return "Günaydın. Bugün nasıl başlıyorsun?"
    if trigger_type == "end_of_day":
        return "Gün bitiyor. Nasıl geçti, bir şey paylaşmak ister misin?"
    if trigger_type == "pre_sleep":
        return "Uyku saatine yaklaştık. Sakin bir kapanış iyi gelir — nasılsın?"
    if trigger_type == "intervention":
        return "Ekran süresi dolmaya yakın. Bir mola iyi gelebilir."
    if trigger_type == "deadline":
        return f"Yaklaşan bir iş var: {ctx}" if ctx else "Yaklaşan bir deadline var — kontrol etmek ister misin?"
    if trigger_type == "drift":
        return "Son günlerde bir rutinde sapma var gibi — fark ettin mi?"
    if trigger_type == "nightly_error":
        return "Gece analizi tamamlanamadı; bir ara birlikte bakarız."
    if ctx:
        return ctx
    return TRIGGER_SITUATION.get(trigger_type, "Merhaba — kisa bir check-in.")


def _looks_garbled(text: str) -> bool:
    if not text or len(text) < 8:
        return True
    low = text.lower()
    bad_phrases = (
        "sen epis", "modern asistan", "tasarlandim", "size yardim",
        "kairos", "tetik", "dolay", "today", "sleep", "assistant",
        "merhaba epis", "yardimci olmak", "cevap verilmedi", "verilmedigin",
        "umarihm", "suredirgine", "suredigine",
    )
    if any(p in low for p in bad_phrases):
        return True
    if re.search(r"\b[a-z]+'[ıiuü]", low):  # today'in gibi Ingilizce+Turkce ek
        return True
    if re.search(r"\b\w{18,}\b", text):  # uydurma uzun kelimeler
        return True
    return False


def build_kairos_user_message(trigger_type: str, context: str) -> str:
    situation = TRIGGER_SITUATION.get(trigger_type, "EPIS'in kullanıcıya ulasmak istedigi bir an.")
    hint = TRIGGER_HINTS.get(trigger_type, "Kısa ve samimi.")
    detail = context.strip() if context and context.strip() else situation
    return (
        f"Durum: {situation}\n"
        f"Detay: {detail}\n"
        f"Ton: {hint}"
    )


def build_kairos_prompt(trigger_type: str, context: str) -> str:
    """Geriye uyumluluk."""
    return build_kairos_user_message(trigger_type, context)


def _clean_proactive_output(text: str) -> str:
    """Talimat sızıntısı, imza ve çift paragraf temizliği."""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
    parts = [p.strip() for p in t.split("\n\n") if p.strip()]
    if len(parts) > 1:
        meta = ("kairos", "tetik", "durum:", "detay:", "ton:", "epis olarak", "sen epis", "talimat", "---")
        if any(m in parts[0].lower() for m in meta):
            t = parts[-1]
    for junk in ("Sen EPIS", "SenEPIS", "Sen EPIS'dir", "Sen EPISdir"):
        if t.startswith(junk):
            lines = [ln for ln in t.splitlines() if ln.strip() and not ln.strip().startswith("---")]
            if lines:
                t = lines[-1].strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    while lines and re.match(r"^[-—\s]*EPIS\.?$", lines[-1], re.IGNORECASE):
        lines.pop()
    t = "\n".join(lines).strip()
    t = re.sub(r"[\s.,;—-]+\s*EPIS\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def format_kairos_epis_message(engine, trigger_type: str, context: str) -> str:
    """Proaktif mesaj — base Qwen icin sablon, fine-tune'da model."""
    if _use_proactive_template_only(engine):
        return fallback_proactive_message(trigger_type, context)

    situation = build_kairos_user_message(trigger_type, context)
    user_msg = (
        f"{situation}\n\n"
        "kullanıcıya tek mesaj yaz. Neden yazdigini dogal bir cumleyle soyle. "
        "Ornek: 'Nabzin biraz yuksek gorunuyor, iyi misin?' veya 'Bir suredir ses yok, nasilsin?'"
    )
    try:
        raw = engine.backend.generate(
            PROACTIVE_SYSTEM,
            [{"role": "user", "text": user_msg}],
        )
        msg = _clean_proactive_output(epis_message_from_raw(raw))
        if _looks_garbled(msg):
            logger.info(f"Proaktif model ciktisi zayif [{trigger_type}], sablon kullaniliyor")
            return fallback_proactive_message(trigger_type, context)
        return msg or fallback_proactive_message(trigger_type, context)
    except Exception as e:
        logger.error(f"Kairos format hatasi: {e}")
        return fallback_proactive_message(trigger_type, context)


DEFAULT_COOLDOWNS_MIN = {
    "idle":          120,
    "anomaly":       30,
    "intervention":  1440,
    "morning":       1440,
    "end_of_day":    1440,
    "pre_sleep":     1440,
    "deadline":      60,
    "nightly_error": 360,
    "drift":         720,
}


def epis_message_from_raw(raw: str) -> str:
    from epis_core import Layer1Engine
    parsed = Layer1Engine._parse(raw)
    return (parsed.get("message") or raw or "").strip()


class ProactiveDelivery:
    """Kairos pending'ini EPIS sesiyle aninda UI ve/veya WhatsApp'a iletir."""

    def __init__(self, memory=None, habits: dict | None = None):
        self.memory = memory
        self.habits = habits or {}
        self.wa     = WhatsAppClient()
        self.ui     = UIClient()
        self.band   = BandClient()
        self._engine = None
        self._context_builder = None
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
        sched   = self.habits.get("schedule", {})
        start_s = self._cfg().get("quiet_start") or sched.get("sleep_time", "23:30")
        end_s   = self._cfg().get("quiet_end") or sched.get("wake_time", "08:00")
        try:
            now   = datetime.now().time()
            start = datetime.strptime(start_s, "%H:%M").time()
            end   = datetime.strptime(end_s, "%H:%M").time()
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

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        from epis_core import build_system_prompt, Layer1Engine
        from context_builder import ContextBuilder
        if self.memory is None:
            from memory_manager import MemoryManager
            self.memory = MemoryManager()
        self._context_builder = ContextBuilder(self.memory)
        self._engine = Layer1Engine(
            build_system_prompt(),
            context_provider=self._context_builder.build,
        )
        logger.info(f"Proactive Layer-1: {self._engine.backend_name} / {self._engine.model}")
        return self._engine

    def format_message(self, trigger_type: str, context: str) -> str:
        engine = self._get_engine()
        return format_kairos_epis_message(engine, trigger_type, context)

    def should_push(self, trigger_type: str, priority: str) -> tuple[bool, str]:
        if not self._cfg().get("instant_push", True):
            return False, "instant_push_kapali"

        if self._in_quiet_hours():
            night_ok = trigger_type in ("anomaly", "deadline") and priority in ("high", "urgent")
            if not night_ok:
                return False, "quiet_hours"

        last = self._push_cooldowns.get(trigger_type)
        cd   = self._cooldown_minutes(trigger_type)
        if last and (datetime.now() - last).total_seconds() / 60 < cd:
            return False, "cooldown"

        ch = self._channel()
        ui_ok = self.ui.is_ready() if ch in ("ui", "both") else False
        wa_ok = self.wa.is_ready() if ch in ("wa", "both") else False
        if not ui_ok and not wa_ok:
            return False, "no_channel_ready"

        return True, "ok"

    def _send_channels(self, epis_msg: str, trigger_type: str, priority: str) -> tuple[bool, str]:
        ch = self._channel()
        sent = False
        via = []

        if ch in ("ui", "both") and self.ui.is_ready():
            if self.ui.push(epis_msg, trigger_type, priority):
                sent = True
                via.append("ui")

        if ch in ("wa", "both") and self.wa.is_ready():
            if self.wa.send(epis_msg):
                sent = True
                via.append("wa")

        if self.band.is_ready():
            if self.band.notify(epis_msg, respect_quiet_hours=True):
                via.append("band")

        return sent, "+".join(via) if via else ""

    def push(self, trigger_type: str, context: str, priority: str = "medium") -> dict:
        ok, reason = self.should_push(trigger_type, priority)
        if not ok:
            logger.info(f"Push atlandi [{trigger_type}]: {reason}")
            return {"pushed": False, "reason": reason}

        epis_msg = self.format_message(trigger_type, context)
        if not epis_msg.strip():
            return {"pushed": False, "reason": "empty_message"}

        sent, via = self._send_channels(epis_msg, trigger_type, priority)
        if sent:
            self._push_cooldowns[trigger_type] = datetime.now()
            if self.memory:
                self.memory.update_current_state({
                    "last_proactive_push_at": datetime.now().isoformat(),
                    "last_proactive_trigger":   trigger_type,
                })
            logger.info(f"Push [{trigger_type}] via {via}: {epis_msg[:80]}")
        else:
            logger.warning(f"Push basarisiz [{trigger_type}]")

        return {
            "pushed":  sent,
            "message": epis_msg,
            "via":     via,
            "reason":  "ok" if sent else "send_fail",
        }

    def apply_push_to_item(self, item: dict) -> dict:
        ctx = item.get("context") or item.get("message") or ""
        result = self.push(
            item.get("trigger_type", ""),
            ctx,
            item.get("priority", "medium"),
        )
        if result.get("pushed"):
            item["status"]       = "delivered"
            item["delivered_at"] = datetime.now().isoformat()
            item["delivery"]     = result.get("via") or "instant"
            item["epis_message"] = result.get("message")
        return result
