#!/usr/bin/env python3
"""
EPIS -- Gizlilik / Anonimleştirme Katmanı (Layer-2)
====================================================
Layer-3'e (harici API) giden metinden kişisel kimliği soyar.

İlkeler:
- Dinamik sözlük: bilinen varlıklar people.json + identity_self.json'dan gelir
  (asla hardcode değil). EPIS yeni isim öğrenip people.json'a ekleyince
  otomatik gizlenir.
- Stabil takma adlar: her varlık tutarlı bir token'a eşlenir
    * Birincil kullanıcı       -> [KNOWN_USER]
    * Diğer kişiler            -> [KISI_1], [KISI_2], ...
    * Yerler                   -> [YER_1], [YER_2], ...
  Tek bir [KNOWN_USER] yerine ayrı tokenlar, "kim kime ne yaptı" ilişkisini
  korur -> Layer-3 analiz kalitesi düşmez.
- Türkçe ek desteği: proper noun'lar kesme işaretiyle çekimlenir
  (kullanıcının, OrnekSehir'de) -> İsim + opsiyonel ['ek] eşleştirilir. Temkinli.
- PII desenleri: e-posta, telefon, IBAN, TC kimlik.
- Reverse map: {token -> gerçek} bellekte tutulur; Layer-3 yanıtı yerelde
  geri çevrilir. Harita CİHAZDAN ÇIKMAZ.
"""

import os
import re
import json
import logging

from crypto_layer import load_json_file

THIS_DIR     = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT    = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
IDENTITY_DIR = os.path.join(EPIS_ROOT, "Layer-1", "identity")
MEMORY_DIR   = os.path.join(EPIS_ROOT, "Layer-1", "memory")

PEOPLE_PATH        = os.path.join(MEMORY_DIR, "people.json")
IDENTITY_SELF_PATH = os.path.join(IDENTITY_DIR, "identity_self.json")

logger = logging.getLogger("EPIS.PRIVACY")

# PII desenleri (sira onemli: telefon, TC'den once)
_PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\bTR\d{2}[\dA-Z]{5,26}\b"), "[IBAN]"),
    (re.compile(r"(?:\+90[\s-]?|0)?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b"), "[TELEFON]"),
    (re.compile(r"\b[1-9]\d{10}\b"), "[TC_KIMLIK]"),
]


class PrivacyFilter:
    """Dinamik sözlük tabanlı anonimleştirici + stabil takma ad + de-anonimleştirme."""

    def __init__(self):
        self._entities: list = []          # (compiled_regex, token, display, name_len)
        self._known_user_display = "[KNOWN_USER]"
        self._mtimes: dict = {}
        self._load_entities()

    # ------------------------------------------------------------------
    # Sözlük yükleme (mtime ile tembel yeniden yükleme)
    # ------------------------------------------------------------------

    def _file_mtime(self, path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _maybe_reload(self):
        current = {
            "people":   self._file_mtime(PEOPLE_PATH),
            "identity": self._file_mtime(IDENTITY_SELF_PATH),
        }
        if current != self._mtimes:
            self._load_entities()

    def _load_entities(self):
        self._mtimes = {
            "people":   self._file_mtime(PEOPLE_PATH),
            "identity": self._file_mtime(IDENTITY_SELF_PATH),
        }

        identity   = self._read_identity_self()
        people_doc = load_json_file(PEOPLE_PATH) or {}

        primary_names: set = set()
        locations:     set = set()

        # identity_self.json -> birincil kullanıcı adı + konum
        basic = identity.get("basic", {}) if isinstance(identity, dict) else {}
        if basic.get("name"):
            primary_names.update(self._name_parts(basic["name"]))
        if basic.get("location"):
            for piece in re.split(r"[,/]", basic["location"]):
                piece = piece.strip()
                if len(piece) > 2:
                    locations.add(piece)

        # people.json -> kişiler
        person_entities = []  # (canonical, [names])
        people = people_doc.get("people", {}) if isinstance(people_doc, dict) else {}
        primary_display = None
        for canonical, info in sorted(people.items()):
            info  = info or {}
            names = [canonical] + list(info.get("aliases", []) or [])
            if info.get("primary"):
                primary_names.update(self._flatten_names(names))
                primary_display = primary_display or canonical
            else:
                person_entities.append((canonical, names))

        for place in (people_doc.get("places", []) or []):
            if place:
                locations.add(place)

        if primary_display:
            self._known_user_display = primary_display

        # Token atama -> entity listesi (regex, token, display)
        entities = []

        for nm in primary_names:
            entities.append((nm, "[KNOWN_USER]", self._known_user_display))

        for idx, (canonical, names) in enumerate(person_entities, start=1):
            token = f"[KISI_{idx}]"
            for nm in set(self._flatten_names(names)):
                entities.append((nm, token, canonical))

        for idx, loc in enumerate(sorted(locations, key=len, reverse=True), start=1):
            token = f"[YER_{idx}]"
            entities.append((loc, token, loc))

        # Derle + uzun isimden kısaya sırala (kismi eşleşmeyi önler)
        compiled = []
        for name, token, display in entities:
            if not name or len(name) < 2:
                continue
            compiled.append((self._make_regex(name), token, display, len(name)))
        compiled.sort(key=lambda x: x[3], reverse=True)
        self._entities = compiled
        logger.info(f"Gizlilik sözlüğü yüklendi: {len(self._entities)} varlık deseni.")

    # ------------------------------------------------------------------
    # Ana API
    # ------------------------------------------------------------------

    def anonymize(self, payload) -> tuple:
        """
        (safe_text, mapping) döner.
        mapping = {token: gerçek_görünen_ad} -- yalnızca yerelde kullanılır.
        """
        self._maybe_reload()

        if not isinstance(payload, str):
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except Exception:
                text = str(payload)
        else:
            text = payload

        if not text:
            return "", {}

        mapping: dict = {}
        for regex, token, display, _ in self._entities:
            if regex.search(text):
                text = regex.sub(token, text)
                mapping.setdefault(token, display)

        for pat, repl in _PII_PATTERNS:
            text = pat.sub(repl, text)

        return text, mapping

    def deanonymize(self, text: str, mapping: dict) -> str:
        """Token'ları gerçek adlarla geri çevirir (yerel gösterim/saklama için)."""
        if not text or not mapping:
            return text
        # Token'lar köşeli parantezli olduğu için [KISI_1], [KISI_10] çakışmaz.
        for token, real in mapping.items():
            text = text.replace(token, real)
        return text

    def reload(self):
        self._load_entities()

    # ------------------------------------------------------------------
    # Yardımcılar
    # ------------------------------------------------------------------

    @staticmethod
    def _make_regex(name: str):
        """
        İsim + opsiyonel Türkçe kesme-işareti eki.
        - 'kullanıcı'   -> 'kullanıcı', 'kullanıcı.', 'kullanıcı '
        - "kullanıcının" -> yakalanır
        - 'Aliağa' içindeki 'Ali' -> yakalanmaz (sınır koruması)
        """
        escaped = re.escape(name)
        pattern = r"(?<!\w)" + escaped + r"(?:['’]\w+|(?!\w))"
        return re.compile(pattern, re.IGNORECASE)

    @staticmethod
    def _name_parts(full_name: str) -> set:
        """'Example User' -> {'Example User', 'Example', 'User'} (3+ harfli parçalar)."""
        parts = {full_name.strip()}
        for p in full_name.split():
            p = p.strip()
            if len(p) >= 3:
                parts.add(p)
        return parts

    @classmethod
    def _flatten_names(cls, names: list) -> list:
        out = []
        for n in names:
            if not n:
                continue
            out.extend(cls._name_parts(n))
        return out

    @staticmethod
    def _read_identity_self() -> dict:
        # identity_self.json şu an düz metin (sadece gitignore'da).
        if not os.path.exists(IDENTITY_SELF_PATH):
            return {}
        try:
            with open(IDENTITY_SELF_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"identity_self.json okunamadı: {e}")
            return {}
