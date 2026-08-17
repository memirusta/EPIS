#!/usr/bin/env python3
"""
EPIS -- Bağlam Oluşturucu (Layer-2 RAG / Context Injection)
===========================================================
Spec §2.2: Layer-2'nin birincil görevi her istek için ilgili hafıza
segmentlerini, kişi kayıtlarını ve anlık bağlamı toplayıp Layer-1
prompt'una enjekte etmektir.

Bu modül her konuşma turunda CANLI bir bağlam bloğu üretir:
  - Zaman / gün-döngüsü (sabah/akşam tonu için)
  - current_state.json anlık durum
  - lifetime.db'den ilgili (yakın + anahtar kelime eşleşen) anılar
  - people.json'dan mesajda geçen kişilerin kayıtları

Çıktı, Layer1Engine tarafından system prompt'a eklenir (history'ye DEĞİL),
böylece her turda taze kalır ve geçmişi şişirmez.
"""

import re
import logging
from datetime import datetime
from typing import Callable, Optional

from memory_manager import _STALE_STATE_KEYS, MemoryManager

logger = logging.getLogger("EPIS.CONTEXT")

# Türkçe gün ve zaman dilimi etiketleri
_WEEKDAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# Bağlamı kalabalıklaştırmamak için anlık durumdan gizlenecek teknik alanlar
_STATE_HIDE = {
    "system_status", "last_update", "session_active",
    "screen_limit_alerted_today", "last_stress_alert",
    "screen_usage_today", "screen_usage_pc", "screen_usage_phone",
    "screen_active_app", "screen_active_title", "phone_active_package", "screen_updated_at",
    *_STALE_STATE_KEYS,
}

_RECALL_HINTS = (
    "dün", "dun", "geçen", "gecen", "konuş", "konus", "hatırl", "hatirl",
    "dedin", "düşün", "dusun", "dusundun", "ne demiş", "ne demist",
    "cevap", "sordum", "önceki", "onceki", "geçmiş", "gecmis", "o mesaj",
    "o konu", "ne dedi", "ne söyle", "ne soyle",
)


def _time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "sabah"
    if 12 <= hour < 18:
        return "öğleden sonra"
    if 18 <= hour < 23:
        return "akşam"
    return "gece"


class ContextBuilder:
    """Her turda Layer-1'e enjekte edilecek anlık bağlam bloğunu üretir."""

    def __init__(
        self,
        memory,
        recent_limit: int = 3,
        search_limit: int = 3,
        live_session_provider: Optional[Callable[[], list[str]]] = None,
    ):
        self.memory       = memory
        self.recent_limit = recent_limit
        self.search_limit = search_limit
        self._live_session_provider = live_session_provider
        self._gadgetbridge = None

    def _is_recall_question(self, msg_low: str) -> bool:
        return any(h in msg_low for h in _RECALL_HINTS)

    def _get_gadgetbridge(self):
        if self._gadgetbridge is None:
            try:
                from sensors import GadgetbridgeReader
                self._gadgetbridge = GadgetbridgeReader()
            except Exception as e:
                logger.warning(f"GadgetbridgeReader: {e}")
                self._gadgetbridge = False
        return self._gadgetbridge if self._gadgetbridge is not False else None

    # ------------------------------------------------------------------

    def _is_screen_question(self, msg_low: str) -> bool:
        # "su an ne hissediyorsun" gibi duygusal sorular ekran degil
        if any(
            k in msg_low
            for k in (
                "hissediyor", "üzgün", "uzgun", "yalnız", "yalniz",
                "bunal", "korkuyor", "nasılsın", "nasilsin", "duygu",
            )
        ):
            return False
        return self._wants_screen_daily(msg_low) or self._wants_screen_live(msg_low)

    def build(self, user_message: str = "") -> str:
        """Bağlam bloğunu string olarak döner. Hata olursa en azından zamanı verir."""
        msg_low = (user_message or "").lower()
        screen_q = self._is_screen_question(msg_low)
        parts = []

        try:
            from epis_core import decide_think_budget
            _, _, intent = decide_think_budget(user_message or "")
        except Exception:
            intent = "default"

        try:
            parts.append(self._time_context())
        except Exception as e:
            logger.warning(f"time_context: {e}")

        try:
            state = self._state_context()
            if state:
                parts.append(state)
        except Exception as e:
            logger.warning(f"state_context: {e}")

        # Ekran sorusunda eski DB/hafiza karismasin.
        # factual: thinking_log gurultusu olmasin (hatirlama degilse).
        # meta/dusunme: canli oturumdaki eski yanlis EPIS cevaplarini tekrar besleme.
        meta_q = any(
            k in msg_low
            for k in (
                "düşünme", "dusunme", "mekanik", "rubrik", "intent",
                "kendi kod", "kodlarına bak", "kodlarina bak", "bakabiliyor musun",
                "kairos", "decide_think",
            )
        )
        skip_thinking = screen_q or meta_q or (
            intent == "factual" and not self._is_recall_question(msg_low)
        )

        if not screen_q:
            if not meta_q:
                try:
                    live = self._live_session_context()
                    if live:
                        parts.append(live)
                except Exception as e:
                    logger.warning(f"live_session_context: {e}")

            try:
                mem = self._memory_context(user_message)
                if mem:
                    parts.append(mem)
            except Exception as e:
                logger.warning(f"memory_context: {e}")

            if not skip_thinking:
                try:
                    think = self._thinking_context(user_message)
                    if think:
                        parts.append(think)
                except Exception as e:
                    logger.warning(f"thinking_context: {e}")

            try:
                ep = self._episodic_context()
                if ep:
                    parts.append(ep)
            except Exception as e:
                logger.warning(f"episodic_context: {e}")

        try:
            ppl = self._people_context(user_message)
            if ppl:
                parts.append(ppl)
        except Exception as e:
            logger.warning(f"people_context: {e}")

        try:
            sensor = self._sensor_context(user_message)
            if sensor:
                parts.append(sensor)
        except Exception as e:
            logger.warning(f"sensor_context: {e}")

        try:
            scr = self._screen_context(user_message)
            if scr:
                parts.append(scr)
        except Exception as e:
            logger.warning(f"screen_context: {e}")

        if intent == "factual":
            parts.append(
                "## FACTUAL KURAL\n"
                "Bu tur olgu sorusu. Once bu bloktaki sensor/ekran/hafiza rakamlarini kullan. "
                "Yoksa uydurma; bilmiyorum de veya (dis bilgiyse) tool_call/fast_tasks."
            )

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Zaman / gün döngüsü
    # ------------------------------------------------------------------

    def _time_context(self) -> str:
        now     = datetime.now()
        weekday = _WEEKDAYS_TR[now.weekday()]
        tod     = _time_of_day(now.hour)
        return (
            "## ZAMAN\n"
            f"Şu an: {now.strftime('%d.%m.%Y %H:%M')} ({weekday}, {tod}). "
            "Tonunu ve selamlamanı güne/saate göre ayarla."
        )

    # ------------------------------------------------------------------
    # Anlık durum
    # ------------------------------------------------------------------

    def _state_context(self) -> str:
        state = self.memory.get_current_state()
        if not state:
            return ""
        visible = {k: v for k, v in state.items() if k not in _STATE_HIDE and v not in (None, "", [], {})}
        if not visible:
            return ""
        lines = [f"- {k}: {v}" for k, v in visible.items()]
        return "## ANLIK DURUM\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # İlgili hafıza (yakın + anahtar kelime)
    # ------------------------------------------------------------------

    def _live_session_context(self) -> str:
        """Bu oturumdaki son mesajlar — model gecmisi sifirlanmis olsa bile."""
        lines_src: list[str] = []
        if self._live_session_provider:
            try:
                lines_src = list(self._live_session_provider() or [])
            except Exception:
                lines_src = []
        if not lines_src:
            lines_src = self.memory.get_session_buffer_lines()
        if not lines_src:
            return ""

        msgs = MemoryManager.parse_conversation_text("\n".join(lines_src))
        if not msgs:
            return ""

        tail = msgs[-12:]
        out = []
        for m in tail:
            role = "kullanıcı" if m.get("role") == "user" else "EPIS"
            text = (m.get("text") or "").strip().replace("\n", " ")
            if len(text) > 300:
                text = text[:300] + "..."
            out.append(f"- {role}: {text}")

        return (
            "## BU OTURUM (canli — az once konusulanlar)\n"
            + "\n".join(out)
            + "\nkullanıcı gecmise atifta bulunursa once buraya bak; ayni konuyu sifirdan uydurma."
        )

    def _memory_context(self, user_message: str) -> str:
        msg_low = (user_message or "").lower()
        recall = self._is_recall_question(msg_low)
        recent_n = 8 if recall else self.recent_limit
        search_n = 6 if recall else self.search_limit

        recent = self.memory.get_recent_interactions(limit=recent_n)
        keywords = self._keywords(user_message)
        searched = self.memory.search_interactions(keywords, limit=search_n) if keywords else []
        sessions = self.memory.search_sessions(keywords, limit=3) if keywords else []

        seen, merged = set(), []
        for item in searched + recent:
            ts = item.get("timestamp", "")
            if ts in seen:
                continue
            seen.add(ts)
            merged.append(item)

        lines = []
        for it in merged[-(recent_n + search_n):]:
            ts   = (it.get("timestamp", "") or "")[:16].replace("T", " ")
            etype = it.get("event_type", "")
            text = (it.get("raw_text", "") or "").strip().replace("\n", " | ")
            if etype == "session" and len(text) > 400:
                text = text[:400] + "..."
            elif len(text) > 220:
                text = text[:220] + "..."
            obs = (it.get("observation", "") or "").strip()
            entry = f"- [{ts}] ({etype}) {text}"
            if obs:
                entry += f" (EPIS notu: {obs[:120]})"
            lines.append(entry)

        for sess in sessions:
            ts = (sess.get("timestamp") or "")[:16].replace("T", " ")
            title = sess.get("title", "Sohbet")
            snippet = self._session_snippet(sess.get("messages") or [], max_turns=4)
            lines.append(f"- [{ts}] Oturum «{title}»: {snippet}")

        if not lines:
            return ""

        header = "## İLGİLİ GEÇMİŞ"
        if recall:
            header += " (kullanıcı gecmisi soruyor — DB + oturum kayitlari)"
        return header + "\n" + "\n".join(lines)

    def _thinking_context(self, user_message: str) -> str:
        """thinking_log.jsonl — ic dusunce + verilen cevap ozeti."""
        msg_low = (user_message or "").lower()
        # Meta/dusunme/kendi-kod sorularinda eski yanlis anlatilari tekrar besleme
        meta_skip = any(
            k in msg_low
            for k in (
                "düşünme", "dusunme", "mekanik", "rubrik", "intent",
                "kendi kod", "kodlarına bak", "kodlarina bak", "bakabiliyor musun",
                "kairos", "decide_think",
            )
        )
        if meta_skip:
            return ""

        keywords = self._keywords(user_message)
        recall = self._is_recall_question(msg_low)
        if not recall and not keywords:
            return ""

        limit = 5 if recall else 2
        rows = self.memory.search_thinking_log(keywords, limit=limit) if keywords else []
        if recall and not rows:
            rows = self.memory.search_thinking_log(
                self._keywords("uyku egzersiz saglik"), limit=3
            )

        if not rows:
            return ""

        # Kairos=dusunme miti tasiyan satirlari ele
        filtered = []
        for row in rows:
            blob = f"{row.get('reasoning', '')} {row.get('content', '')}".lower()
            if "kairos" in blob and any(
                x in blob for x in ("düşün", "dusun", "mekanik", "think")
            ):
                continue
            filtered.append(row)
        rows = filtered
        if not rows:
            return ""

        lines = [
            "## EPIS İÇ DÜŞÜNCESİ (thinking_log — kullanıcıya ham metin olarak verme)",
            "kullanıcı 'ne dusundun / neden oyle dedin' derse buradaki ozetleri kullan.",
            "NOT: Bu ozetler yanlis olabilir; TUR YONLENDIRMESI ile celisirse yonlendirmeyi tercih et.",
        ]
        for row in rows:
            lines.append("- " + MemoryManager.summarize_thinking_row(row))
        return "\n".join(lines)

    def _episodic_context(self) -> str:
        """Gece analizi / haftalik ozet (nightly_recalculation ciktisi)."""
        data = self.memory.get_episodic_context()
        if not data:
            return ""

        lines = ["## EPİSODİK HAFIZA (gece/haftalik analiz)"]

        calc = data.get("identity_calculated") or {}
        traits = calc.get("trait_history") or []
        if traits:
            last = traits[-1]
            lines.append(
                "- Son kullanici profili: "
                f"ruh hali={last.get('mood_estimate', '?')}, "
                f"enerji={last.get('energy_level', '?')}, "
                f"stres={last.get('stress_signal', '?')}"
            )
            insights = last.get("behavioral_insights") or []
            for ins in insights[:2]:
                lines.append(f"  - {ins}")

        epis_self = data.get("epis_self") or {}
        conclusions = epis_self.get("conclusions") or []
        for c in conclusions[-3:]:
            if isinstance(c, str):
                lines.append(f"- EPIS ogrenimi: {c[:160]}")

        report = data.get("morning_report") or {}
        if report.get("tomorrow_context"):
            lines.append(f"- Sabah notu: {str(report['tomorrow_context'])[:200]}")

        weekly = data.get("weekly") or {}
        days = weekly.get("days") or []
        if days:
            last_day = days[-1]
            analysis = last_day.get("analysis") or ""
            if isinstance(analysis, dict):
                bits = [
                    analysis.get("mood_estimate"),
                    analysis.get("energy_level"),
                    analysis.get("tomorrow_context"),
                ]
                insights = analysis.get("behavioral_insights") or []
                if insights:
                    bits.append(insights[0] if isinstance(insights[0], str) else str(insights[0]))
                analysis = " | ".join(str(b) for b in bits if b)
            else:
                analysis = str(analysis).strip()
            if analysis:
                lines.append(f"- Dun ozeti: {analysis[:220]}")
            voice = (last_day.get("epis_voice") or "").strip()
            if voice:
                lines.append(f"- EPIS gece yorumu: {voice[:220]}")

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _session_snippet(messages: list, max_turns: int = 4) -> str:
        tail = messages[-max_turns * 2:]
        parts = []
        for m in tail:
            role = "kullanıcı" if m.get("role") == "user" else "EPIS"
            text = (m.get("text") or "").strip().replace("\n", " ")
            if len(text) > 140:
                text = text[:140] + "..."
            parts.append(f"{role}: {text}")
        return " // ".join(parts)

    # ------------------------------------------------------------------
    # İlgili kişiler (people.json)
    # ------------------------------------------------------------------

    def _people_context(self, user_message: str) -> str:
        people = self.memory.get_people().get("people", {})
        if not people or not user_message:
            return ""

        msg_low = user_message.lower()
        hits    = []
        for name, info in people.items():
            names = [name] + (info.get("aliases", []) if isinstance(info, dict) else [])
            if any(n and n.lower() in msg_low for n in names):
                hits.append((name, info))

        if not hits:
            return ""

        lines = []
        for name, info in hits:
            if isinstance(info, dict):
                rel  = info.get("relation", "")
                note = info.get("notes", "")
                desc = " / ".join(x for x in [rel, note] if x)
                lines.append(f"- {name}: {desc}" if desc else f"- {name}")
            else:
                lines.append(f"- {name}: {info}")

        return "## İLGİLİ KİŞİLER\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Sensörler (Gadgetbridge / Mi Band)
    # ------------------------------------------------------------------

    def _sensor_context(self, user_message: str) -> str:
        gb = self._get_gadgetbridge()
        if not gb:
            return ""

        msg_low = (user_message or "").lower()
        want_hr = not user_message or any(
            k in msg_low for k in ("nabız", "nabiz", "kalp", "stres", "bpm")
        )

        summary = gb.get_today_summary()
        if not summary.get("available"):
            return ""

        lines = ["## SENSÖR VERİSİ (Mi Band / Gadgetbridge — gerçek ölçüm, tahmin değil)"]

        onset = summary.get("sleep_onset")
        wake  = summary.get("wake_time")
        mins  = summary.get("sleep_minutes")
        if onset or wake or mins is not None:
            parts = []
            if onset:
                parts.append(f"uyku başlangıcı: {onset[:16].replace('T', ' ')}")
            if wake:
                parts.append(f"uyanma: {wake[:16].replace('T', ' ')}")
            if mins is not None:
                h, m = divmod(int(mins), 60)
                parts.append(f"süre: {h} sa {m} dk")
            lines.append("- Gece uykusu: " + ", ".join(parts))
        else:
            lines.append("- Gece uykusu: henüz tespit edilemedi (veri yetersiz)")

        avg_hr = summary.get("avg_heart_rate")
        if avg_hr:
            lines.append(f"- Bugün ortalama nabız: {avg_hr} bpm")

        cov = summary.get("hr_coverage_pct")
        if cov is not None:
            lines.append(f"- Gece nabız kapsamı: %{cov}")

        if want_hr:
            recent = gb.get_heart_rate_summary(hours=0.25)
            if recent.get("available"):
                lines.append(
                    f"- Son 15 dk nabız: ort {recent['avg']} bpm "
                    f"(min {recent['min']}, max {recent['max']})"
                )

        if len(lines) <= 1:
            return ""

        warn = summary.get("sleep_data_warning")
        if warn:
            lines.append(f"- NOT: {warn}")

        lines.append(
            "kullanıcı uyku/nabız sorarsa bu rakamları kullan; bilmediğini uydurma, "
            "'siz' diye hitap etme, 'sen' de."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Ekran kullanimi (Kairos / ScreenMonitor + canli okuma)
    # ------------------------------------------------------------------

    _SCREEN_DAILY_KEYS = (
        "ekran", "youtube", "reels", "tiktok", "instagram", "telefon",
        "sure", "dakika", "ne izliyorum", "ne oynuyorum",
    )
    _SCREEN_LIVE_KEYS = (
        "şu an", "su an", "şu anda", "su anda", "şimdi", "simdi",
        "ne var", "neler var", "neler", "açık", "acik",
        "hangi uygulama", "ne izliyorum", "ne oynuyorum",
        "monitör", "monitor", "sekme", "sekmeler", "ekranlarım", "ekranlarim",
    )

    def _wants_screen_daily(self, msg_low: str) -> bool:
        if not msg_low:
            return True
        return any(k in msg_low for k in self._SCREEN_DAILY_KEYS)

    def _wants_screen_live(self, msg_low: str) -> bool:
        if any(k in msg_low for k in self._SCREEN_LIVE_KEYS):
            return True
        # "neler var" / "ne var" — kelime siniri
        return bool(re.search(r"\bne(ler)?\s+var", msg_low))

    def _live_screen_snapshot(self) -> list[str]:
        """Mesaj aninda tum monitorler + telefon on plani (ADB)."""
        from sensors import ScreenMonitor
        from phone_monitor import PhoneScreenMonitor

        lines: list[str] = []

        try:
            sm = ScreenMonitor()
            if sm.is_available():
                snap = sm.get_live_desktop_snapshot()
                lines.extend(ScreenMonitor.format_live_snapshot_lines(snap))
        except Exception as e:
            logger.debug(f"canli PC ekran: {e}")

        try:
            pm = PhoneScreenMonitor()
            if pm.adb_connected():
                if not pm.phone_screen_on():
                    lines.append("- Telefon: ekran kapali veya kilitli")
                else:
                    pkg = pm.peek_foreground()
                    if pkg:
                        label = PhoneScreenMonitor.package_label(pkg)
                        cat = PhoneScreenMonitor.distraction_from_package(pkg)
                        distract_labels = {"youtube": "YouTube", "reels": "Reels/IG", "tiktok": "TikTok"}
                        tag = f" [{distract_labels.get(cat, cat)}]" if cat else ""
                        lines.append(f"- Telefon: {label} ({pkg}){tag}")
                    else:
                        lines.append("- Telefon: ana ekran / bildirim paneli")
        except Exception as e:
            logger.debug(f"canli telefon ekran: {e}")

        return lines

    def _screen_context(self, user_message: str) -> str:
        from sensors import ScreenMonitor
        from phone_monitor import PhoneScreenMonitor, merge_category_minutes

        msg_low = (user_message or "").lower()
        want_daily = self._wants_screen_daily(msg_low)
        want_live  = self._wants_screen_live(msg_low) or want_daily
        if not want_daily and not want_live:
            return ""

        parts: list[str] = []

        if want_live:
            live = self._live_screen_snapshot()
            if live:
                parts.append(
                    "## ŞU AN EKRAN (canlı — mesaj anında okundu)\n"
                    + "\n".join(live)
                    + "\n* = odak penceresi. Önceki sohbet turlarındaki ekran/telefon tahminlerini yok say; "
                    "sadece bu listedeki satırları kullan. Uydurma."
                )

        pc = ScreenMonitor.load_saved_summary() if want_daily else {}
        ph = PhoneScreenMonitor.load_saved_summary() if want_daily else {}

        if want_daily and (pc or ph):
            merged = merge_category_minutes(
                pc.get("categories") if pc else {},
                ph.get("categories") if ph else {},
            )

            daily = ["## BUGÜN EKRAN (PC + telefon — Kairos)"]
            labels = {"youtube": "YouTube", "reels": "Reels/IG", "tiktok": "TikTok"}
            for key, label in labels.items():
                if key in merged and merged[key] > 0:
                    pc_v = (pc.get("categories") or {}).get(key, 0) if pc else 0
                    ph_v = (ph.get("categories") or {}).get(key, 0) if ph else 0
                    if pc_v and ph_v:
                        daily.append(f"- {label}: {merged[key]} dk (PC {pc_v} + tel {ph_v})")
                    elif ph_v:
                        daily.append(f"- {label}: {merged[key]} dk (telefon)")
                    else:
                        daily.append(f"- {label}: {merged[key]} dk (PC)")

            if len(daily) > 1:
                daily.append("kullanıcı günlük süre sorarsa bu rakamları kullan; uydurma.")
                parts.append("\n".join(daily))

        return "\n\n".join(parts)

    # ------------------------------------------------------------------

    @staticmethod
    def _keywords(text: str) -> list:
        """Mesajdan 4+ harfli anlamlı kelimeleri çıkarır (basit, API'siz)."""
        if not text:
            return []
        words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
        stop  = {
            "için", "ama", "ile", "bir", "bu", "şu", "ben", "sen", "biz",
            "evet", "hayır", "tamam", "nasıl", "nedir", "çok", "daha", "gibi",
        }
        return [w for w in words if len(w) >= 4 and w not in stop][:8]
