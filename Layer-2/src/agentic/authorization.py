"""Deterministic per-turn and per-session authorization policy.

Models can propose actions, but they never grant themselves permission. Core
decides whether the user's current message already authorizes a semantic action,
whether a previously confirmed session category applies, or whether an approval
card is still required.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading


def _fold(value: str) -> str:
    table = str.maketrans({
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
        "Ç": "c", "Ğ": "g", "İ": "i", "I": "i", "Ö": "o", "Ş": "s", "Ü": "u",
    })
    value = str(value or "").translate(table).casefold()
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class AuthorizationDecision:
    category: str | None
    authorized: bool
    source: str
    grantable: bool
    always_confirm: bool = False
    denied: bool = False


class SessionAuthorizationPolicy:
    """Small auditable policy for semantic Computer Use goals."""

    GRANTABLE = frozenset({
        "computer_control",
        "external_communication",
    })

    # File mutation is never granted by session/path state alone.  The user must
    # explicitly ask for a create/modify/patch/copy/move operation in the current
    # conversational turn.  Persistent path grants answer only *where* EPIS may
    # write, never *whether* it may decide to write by itself.
    FILE_MUTATION_CAPABILITIES = frozenset({
        "files.write_text",
        "files.patch_text",
        "files.copy",
        "files.move",
        "repository.context_write",
    })

    REPOSITORY_BIND_CAPABILITY = "repository.bind"

    ALWAYS_CONFIRM = frozenset({
        "credential_entry",
        "financial_transaction",
        "security_privileged",
        "destructive_change",
    })

    _CRITICAL_TERMS = {
        "credential_entry": (
            "password", "passcode", "credential", "otp", "2fa",
            "one time code", "sifre", "parola", "pin gir",
        ),
        "financial_transaction": (
            "purchase", "checkout", "pay ", "buy ", "place order",
            "satin al", "odeme", "ode ", "siparis ver",
        ),
        "security_privileged": (
            "uac", "elevat", "run as administrator", "administrator permission",
            "security setting", "firewall", "defender setting",
            "yonetici olarak", "yonetici izni", "guvenlik ayar",
        ),
        "destructive_change": (
            "delete ", "remove ", "uninstall", "factory reset", "format ",
            "wipe ", "sil ", "kaldir ", "fabrika ayar", "bicimlendir",
        ),
    }

    _EXTERNAL_ACTION_TERMS = (
        "send", "submit", "publish", "post ", "postla", "share ",
        "gonder", "yolla", "ilet ", "paylas", "yayinla", "yorum yaz",
        "comment yaz", "issue ac", "issue olustur", "mail at", "email at",
    )

    _EXTERNAL_DENIAL_TERMS = (
        "don't send", "do not send", "without sending", "dont send",
        "gonderme", "gondermeden", "yollama", "yollamadan", "paylasma",
        "yayinlama", "submit etme",
    )

    _WHATSAPP_ACTION_TERMS = (
        "whatsapp",
        "whatsapptan",
        "whatsapp'tan",
        "mesaj at",
        "mesaj gonder",
        "mesaj yolla",
        "mesaj ilet",
        "sor ",
        "sorsana",
        "sorar misin",
        "sorabilir misin",
        "ask ",
    )

    _WHATSAPP_DENIAL_TERMS = (
        "sorma",
        "sormadan",
        "mesaj atma",
        "mesaj gonderme",
        "mesaj yollama",
        "whatsapptan gonderme",
        "whatsapp'tan gonderme",
    )

    _COMPUTER_ACTION_TERMS = (
        "ac ", "ac,", "acip", "kapat", "tikla", "tiklay", "yaz ", "yazip",
        "sec ", "secip", "gec ", "gecip", "surukle", "kaydir", "odakla",
        "open ", "close ", "click", "type ", "select ", "switch ",
        "scroll", "drag", "focus ",
    )

    _FILE_MUTATION_TERMS = (
        "degistir", "duzelt", "uygula", "guncelle", "kaydet", "save", "patch", "edit",
        "olustur", "dosya yaz", "koda yaz", "ekle", "replace", "modify",
        "update", "fix ", "fixle", "apply", "create", "rewrite",
        "rename", "tasi", "move ", "copy", "kopyala",
    )

    _FILE_MUTATION_DENIAL_TERMS = (
        "degistirme", "duzeltme", "uygulama", "yazma", "olusturma",
        "dokunma", "sadece incele", "yalnizca incele", "only inspect",
        "do not modify", "don't modify", "dont modify", "read only",
        "salt okunur", "salt-okunur",
    )

    _REPOSITORY_BIND_TERMS = (
        "repo artik burada", "repo burada", "bunu ana repo yap",
        "bunu canonical repo yap", "canonical repo", "kanonik repo",
        "repo now here", "make this the repo", "make this canonical repo",
        "set this repo", "set repository",
    )

    _CATEGORY_CONTEXT = {
        "external_communication": (
            "mesaj", "message", "mail", "email", "yorum", "comment",
            "issue", "post", "gonderi", "yayin",
        ),
        "computer_control": (
            "uygulama", "app", "desktop", "pencere", "window", "ekran",
            "screen", "chatgpt", "work mode", "arayuz", "gui",
        ),
    }

    def __init__(self):
        self._grants: set[str] = set()
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self._grants.clear()

    def grants(self) -> set[str]:
        with self._lock:
            return set(self._grants)

    def grant(self, category: str | None) -> bool:
        if category not in self.GRANTABLE:
            return False
        with self._lock:
            self._grants.add(category)
        return True

    @classmethod
    def classify(cls, capability: str, arguments: dict) -> str | None:

        if capability == "whatsapp.send_to_contact":
            return "external_communication"
        if capability in cls.FILE_MUTATION_CAPABILITIES:
            return "file_mutation"

        if capability == cls.REPOSITORY_BIND_CAPABILITY:
            return "repository_binding"

        if capability != "computer.execute":
            return None

        goal = _fold(arguments.get("goal", ""))
        for category, terms in cls._CRITICAL_TERMS.items():
            if any(term in goal for term in terms):
                return category

        if (
            any(term in goal for term in cls._EXTERNAL_ACTION_TERMS)
            and not any(term in goal for term in cls._EXTERNAL_DENIAL_TERMS)
        ):
            return "external_communication"

        return "computer_control"

    @classmethod
    def _denied_by_current_turn(cls, category: str, user_message: str) -> bool:
        text = _fold(user_message)
        if category == "external_communication":
            return any(term in text for term in cls._EXTERNAL_DENIAL_TERMS)
        if category == "file_mutation":
            return any(term in text for term in cls._FILE_MUTATION_DENIAL_TERMS)
        return False

    @classmethod
    def _whatsapp_denied_current_turn(
        cls,
        user_message: str,
    ) -> bool:
        text = _fold(user_message)

        return (
            cls._denied_by_current_turn(
                "external_communication",
                user_message,
            )
            or any(
                term in text
                for term
                in cls._WHATSAPP_DENIAL_TERMS
            )
        )

    @classmethod
    def _explicit_whatsapp_current_turn(
        cls,
        arguments: dict,
        user_message: str,
    ) -> bool:
        text = _fold(user_message)

        if not text:
            return False

        if cls._whatsapp_denied_current_turn(
            user_message
        ):
            return False

        has_action = any(
            term in text
            for term
            in cls._WHATSAPP_ACTION_TERMS
        )

        if not has_action:
            return False

        contact_ref = _fold(
            arguments.get(
                "contact_ref",
                "",
            )
        )

        # "Birine sor istersen" gibi assistant-originated
        # belirsiz hedefler otomatik outreach yetkisi değildir.
        target_is_explicit = (
            bool(
                contact_ref
                and contact_ref in text
            )
            or "whatsapp" in text
        )

        return target_is_explicit

    @classmethod
    def _explicit_current_turn(cls, category: str, user_message: str) -> bool:
        if category in cls.ALWAYS_CONFIRM:
            return False

        text = _fold(user_message)
        if not text or cls._denied_by_current_turn(category, user_message):
            return False

        if category == "external_communication":
            return any(term in text for term in cls._EXTERNAL_ACTION_TERMS)
        if category == "computer_control":
            return any(term in text for term in cls._COMPUTER_ACTION_TERMS)
        if category == "file_mutation":
            return any(term in text for term in cls._FILE_MUTATION_TERMS)
        if category == "repository_binding":
            return any(term in text for term in cls._REPOSITORY_BIND_TERMS)
        return False

    @classmethod
    def _session_context_relevant(cls, category: str, user_message: str) -> bool:
        text = _fold(user_message)
        if not text or cls._denied_by_current_turn(category, user_message):
            return False
        if cls._explicit_current_turn(category, user_message):
            return True
        return any(term in text for term in cls._CATEGORY_CONTEXT.get(category, ()))

    def evaluate(self, capability: str, arguments: dict, user_message: str) -> AuthorizationDecision:

        # WhatsApp outreach is stricter than generic external
        # communication: an old session grant may never silently
        # authorize contacting a third party.
        if capability == "whatsapp.send_to_contact":
            category = "external_communication"

            if self._whatsapp_denied_current_turn(
                user_message
            ):
                return AuthorizationDecision(
                    category,
                    False,
                    "user_denied",
                    False,
                    denied=True,
                )

            if self._explicit_whatsapp_current_turn(
                arguments,
                user_message,
            ):
                return AuthorizationDecision(
                    category,
                    True,
                    "explicit_current_turn",
                    False,
                )

            return AuthorizationDecision(
                category,
                False,
                "assistant_proposed",
                False,
            )
        category = self.classify(capability, arguments)

        if category is None:
            return AuthorizationDecision(None, False, "policy_required", False)

        if self._denied_by_current_turn(category, user_message):
            return AuthorizationDecision(
                category, False, "user_denied", False, denied=True
            )

        if category in self.ALWAYS_CONFIRM:
            return AuthorizationDecision(
                category, False, "always_confirm", False, always_confirm=True
            )

        # File mutation is deliberately non-grantable.  A path grant can remove
        # repeated *filesystem scope* approvals, but the current message still
        # has to authorize mutation every time.
        if category == "file_mutation":
            if self._explicit_current_turn(category, user_message):
                return AuthorizationDecision(
                    category, True, "explicit_current_turn", False
                )
            return AuthorizationDecision(
                category, False, "explicit_mutation_required", False
            )

        # Canonical repository binding is a persistent application setting, not
        # a source edit.  It is still permitted only when the current message
        # explicitly asks to adopt that repository/location.
        if category == "repository_binding":
            if self._explicit_current_turn(category, user_message):
                return AuthorizationDecision(
                    category, True, "explicit_current_turn", False
                )
            return AuthorizationDecision(
                category, False, "explicit_repository_binding_required", False
            )

        if self._explicit_current_turn(category, user_message):
            return AuthorizationDecision(
                category,
                True,
                "explicit_current_turn",
                category in self.GRANTABLE,
            )

        with self._lock:
            session_granted = category in self._grants

        if session_granted and self._session_context_relevant(category, user_message):
            return AuthorizationDecision(
                category, True, "session_category_grant", True
            )

        return AuthorizationDecision(
            category,
            False,
            "assistant_proposed",
            category in self.GRANTABLE,
        )
