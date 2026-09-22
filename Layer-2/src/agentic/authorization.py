"""Deterministic per-turn and per-session authorization policy.

Models can propose actions, but they never grant themselves permission. Core
decides whether the user's current message already authorizes a semantic action,
whether a previously confirmed session category applies, or whether an approval
card is still required.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


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

    _COMPUTER_ACTION_TERMS = (
        "ac ", "ac,", "acip", "kapat", "tikla", "tiklay", "yaz ", "yazip",
        "sec ", "secip", "gec ", "gecip", "surukle", "kaydir", "odakla",
        "open ", "close ", "click", "type ", "select ", "switch ",
        "scroll", "drag", "focus ",
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

    def reset(self) -> None:
        self._grants.clear()

    def grants(self) -> set[str]:
        return set(self._grants)

    def grant(self, category: str | None) -> bool:
        if category not in self.GRANTABLE:
            return False
        self._grants.add(category)
        return True

    @classmethod
    def classify(cls, capability: str, arguments: dict) -> str | None:
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
        return False

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

        if self._explicit_current_turn(category, user_message):
            return AuthorizationDecision(
                category,
                True,
                "explicit_current_turn",
                category in self.GRANTABLE,
            )

        if category in self._grants and self._session_context_relevant(category, user_message):
            return AuthorizationDecision(
                category, True, "session_category_grant", True
            )

        return AuthorizationDecision(
            category,
            False,
            "assistant_proposed",
            category in self.GRANTABLE,
        )
