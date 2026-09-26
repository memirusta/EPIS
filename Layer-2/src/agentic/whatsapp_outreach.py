"""Trusted local WhatsApp outreach primitives for EPIS.

The model sees only the semantic capability. Contact resolution and the real
transport mapping remain on the trusted device. This module contains no raw
phone-number/JID API surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

import requests


def _safe_contact_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()[:256]
    if (
        not name or "@" in name
        or "whatsapp.net" in name.casefold()
        or re.search(r"\d[\d\s()+-]{6,}\d", name)
        or any(ord(character) < 32 for character in name)
    ):
        return ""
    return name


class ContactResolver(Protocol):
    def resolve_contact(
        self,
        contact_ref: str,
    ) -> dict: ...


class OutreachTracker(Protocol):
    def begin(
        self,
        *,
        outreach_id: str,
        person_id: str,
        provider_contact_ref: str,
        message: str,
    ) -> dict: ...

    def mark_sent(
        self,
        *,
        outreach_id: str,
        provider_message_ref: str,
    ) -> dict: ...

    def mark_failed(
        self,
        *,
        outreach_id: str,
        error: str,
    ) -> None: ...

    def accept_reply(
        self,
        *,
        provider_message_ref: str,
        provider_contact_ref: str,
        content: str,
        confidence: float = 0.65,
    ) -> dict: ...


class WhatsappBridge(Protocol):
    def available(self) -> bool: ...

    def send(
        self,
        provider_contact_ref: str,
        message: str,
        *,
        outreach_id: str,
    ) -> dict: ...

    def close(self) -> None: ...


class VaultContactResolver:
    """Thin trusted wrapper around LocalMemoryVault."""

    def __init__(
        self,
        vault,
    ):
        self.vault = vault

    def resolve_contact(
        self,
        contact_ref: str,
    ) -> dict:
        return (
            self.vault
            .resolve_contact_ref(
                contact_ref
            )
        )

    def display_name_for_person(
        self,
        person_id: str,
    ) -> str:
        return self.vault.display_name_for_person(
            person_id
        )


class VaultOutreachState:
    """Durable trusted-device correlation state."""

    def __init__(
        self,
        vault,
    ):
        self.vault = vault

    def begin(
        self,
        *,
        outreach_id: str,
        person_id: str,
        provider_contact_ref: str,
        message: str,
    ) -> dict:
        return (
            self.vault
            .create_external_outreach(
                outreach_id=
                    outreach_id,

                person_id=
                    person_id,

                provider_contact_ref=
                    provider_contact_ref,

                outbound_message=
                    message,

                provider=
                    "whatsapp",
            )
        )

    def mark_sent(
        self,
        *,
        outreach_id: str,
        provider_message_ref: str,
    ) -> dict:
        return (
            self.vault
            .mark_external_outreach_sent(
                outreach_id=
                    outreach_id,

                provider_message_ref=
                    provider_message_ref,
            )
        )

    def mark_failed(
        self,
        *,
        outreach_id: str,
        error: str,
    ) -> None:
        self.vault.mark_external_outreach_failed(
            outreach_id=
                outreach_id,
            error=
                error,
        )

    def accept_reply(
        self,
        *,
        provider_message_ref: str,
        provider_contact_ref: str,
        content: str,
        confidence: float = 0.65,
    ) -> dict:
        return (
            self.vault
            .accept_external_outreach_reply(
                provider_message_ref=
                    provider_message_ref,

                provider_contact_ref=
                    provider_contact_ref,

                content=
                    content,

                confidence=
                    confidence,

                provider=
                    "whatsapp",
            )
        )

    def accept_reply_by_contact(
        self,
        *,
        incoming_message_ref: str,
        provider_contact_ref: str,
        content: str,
        confidence: float = 0.65,
    ) -> dict:
        return (
            self.vault
            .accept_external_outreach_reply_by_contact(
                incoming_message_ref=
                    incoming_message_ref,

                provider_contact_ref=
                    provider_contact_ref,

                content=
                    content,

                confidence=
                    confidence,

                provider=
                    "whatsapp",
            )
        )



_WHATSAPP_BRIDGE_CONFIG_KEYS = frozenset({
    "EPIS_WHATSAPP_BRIDGE_URL",
    "EPIS_WHATSAPP_BRIDGE_TOKEN",
})


def default_whatsapp_bridge_config_path() -> Path:
    override = str(
        os.getenv(
            "EPIS_WHATSAPP_BRIDGE_CONFIG"
        )
        or ""
    ).strip()

    if override:
        return (
            Path(override)
            .expanduser()
            .resolve()
        )

    base = Path(
        os.getenv("LOCALAPPDATA")
        or Path.home()
    )

    return (
        base
        / "EPIS"
        / "whatsapp-bridge.env"
    )


def load_whatsapp_bridge_config() -> dict[str, str]:
    """Load only the two values Python needs.

    The same private file may contain Heroku ingress credentials used by
    the Node process; those values are intentionally never imported into
    the Python device-agent environment here.
    """

    values: dict[str, str] = {}

    path = (
        default_whatsapp_bridge_config_path()
    )

    try:
        lines = path.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        lines = []

    for raw in lines:
        line = raw.strip()

        if (
            not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip()

        if (
            key
            in _WHATSAPP_BRIDGE_CONFIG_KEYS
        ):
            values[key] = (
                value.strip()
            )

    # Explicit process env always wins.
    for key in (
        _WHATSAPP_BRIDGE_CONFIG_KEYS
    ):
        value = str(
            os.getenv(key)
            or ""
        ).strip()

        if value:
            values[key] = value

    return values


class LocalWhatsappBridge:
    """Authenticated fixed localhost adapter for the Baileys process."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 5.0,
    ):
        parsed = urlsplit(
            str(base_url or "").strip()
        )

        if (
            parsed.scheme != "http"
            or parsed.hostname
            != "127.0.0.1"
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {
                "",
                "/",
            }
        ):
            raise ValueError(
                "whatsapp_bridge_must_be_loopback_http"
            )

        clean_token = str(
            token or ""
        ).strip()

        if len(clean_token) < 32:
            raise ValueError(
                "whatsapp_bridge_token_too_short"
            )

        self.base_url = (
            "http://127.0.0.1:"
            + str(parsed.port)
        )

        self.token = clean_token
        self.timeout = float(timeout)

    @property
    def _headers(
        self,
    ) -> dict[str, str]:
        return {
            "Authorization":
                "Bearer "
                + self.token,

            "Content-Type":
                "application/json",
        }

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> tuple[int, dict]:
        response = requests.request(
            method,
            self.base_url + path,
            headers=self._headers,
            json=body,
            timeout=self.timeout,
        )

        try:
            payload = response.json()
        except Exception:
            payload = {}

        if not isinstance(
            payload,
            dict,
        ):
            payload = {}

        return (
            int(
                response.status_code
            ),
            payload,
        )

    def available(
        self,
    ) -> bool:
        try:
            status, payload = (
                self._json_request(
                    "GET",
                    "/health",
                )
            )
        except Exception:
            return False

        return bool(
            status == 200
            and payload.get("ok")
            is True
            and payload.get(
                "whatsapp_connected"
            )
            is True
        )

    def enroll_contact(
        self,
        contact_ref: str,
        phone_number: str,
    ) -> dict:
        try:
            status, payload = (
                self._json_request(
                    "POST",
                    "/contacts/enroll",
                    body={
                        "contact_ref":
                            str(
                                contact_ref
                            ),

                        "phone_number":
                            str(
                                phone_number
                            ),
                    },
                )
            )
        except Exception:
            return {
                "ok": False,
                "error":
                    "whatsapp_bridge_unreachable",
            }

        if status >= 400:
            return {
                "ok": False,
                "error": str(
                    payload.get(
                        "error"
                    )
                    or
                    (
                        "whatsapp_bridge_http_"
                        + str(status)
                    )
                )[:160],
            }

        if payload.get("ok") is not True:
            return {
                "ok": False,
                "error": str(
                    payload.get(
                        "error"
                    )
                    or
                    "contact_enrollment_failed"
                )[:160],
            }

        # Never return the resolved JID/phone.
        return {
            "ok": True,
            "status": str(
                payload.get(
                    "status"
                )
                or "enrolled"
            ),
            "contact_ref": str(
                payload.get(
                    "contact_ref"
                )
                or contact_ref
            ),
        }

    def remove_contact(
        self,
        contact_ref: str,
    ) -> dict:
        try:
            status, payload = (
                self._json_request(
                    "POST",
                    "/contacts/remove",
                    body={
                        "contact_ref":
                            str(
                                contact_ref
                            ),
                    },
                )
            )
        except Exception:
            return {
                "ok": False,
                "error":
                    "whatsapp_bridge_unreachable",
            }

        if status >= 400:
            return {
                "ok": False,
                "error": str(
                    payload.get(
                        "error"
                    )
                    or
                    (
                        "whatsapp_bridge_http_"
                        + str(status)
                    )
                )[:160],
            }

        return {
            "ok":
                payload.get("ok")
                is True,

            "status": str(
                payload.get(
                    "status"
                )
                or "removed"
            ),
        }

    def send(
        self,
        provider_contact_ref: str,
        message: str,
        *,
        outreach_id: str,
    ) -> dict:
        try:
            status, payload = (
                self._json_request(
                    "POST",
                    "/send",
                    body={
                        "contact_ref":
                            str(
                                provider_contact_ref
                            ),

                        "message":
                            str(message),

                        "outreach_id":
                            str(outreach_id),
                    },
                )
            )
        except Exception:
            return {
                "ok": False,
                "error":
                    "whatsapp_bridge_unreachable",
            }

        if status >= 400:
            error = str(
                payload.get(
                    "error"
                )
                or ""
            ).strip()

            # Disclosure-policy failures are intentionally safe to
            # surface toward EPIS so it can rewrite its own message.
            # Arbitrary bridge HTTP errors remain opaque.
            if error in {
                "recipient_identity_context_missing",
                "recipient_identity_deception",
            }:
                result = {
                    "ok": False,
                    "error":
                        error,
                }

                if type(
                    payload.get(
                        "retryable"
                    )
                ) is bool:
                    result["retryable"] = (
                        payload[
                            "retryable"
                        ]
                    )

                required = (
                    payload.get(
                        "required"
                    )
                )

                if isinstance(
                    required,
                    list,
                ):
                    safe_required = [
                        item
                        for item in required
                        if item in {
                            "ai_identity",
                            "emir_context",
                        }
                    ]

                    if safe_required:
                        result["required"] = (
                            safe_required
                        )

                return result

            return {
                "ok": False,
                "error":
                    "whatsapp_bridge_http_"
                    + str(status),
            }

        if payload.get("ok") is not True:
            return {
                "ok": False,
                "error":
                    str(
                        payload.get(
                            "error"
                        )
                        or
                        "whatsapp_bridge_failed"
                    )[:160],
            }

        provider_message_ref = str(
            payload.get(
                "provider_message_ref"
            )
            or ""
        ).strip()

        if not provider_message_ref:
            return {
                "ok": False,
                "error":
                    "provider_message_ref_missing",
            }

        # Explicit safe output allowlist.
        return {
            "ok": True,
            "status": str(
                payload.get("status")
                or "sent"
            ),
            "provider_message_ref":
                provider_message_ref,
        }

    def close(
        self,
    ) -> None:
        return None


def configure_whatsapp_device_runtime_from_env(
    *,
    vault=None,
) -> bool:
    """Wire the private local controller from the dedicated config file."""

    config = (
        load_whatsapp_bridge_config()
    )

    base_url = str(
        config.get(
            "EPIS_WHATSAPP_BRIDGE_URL"
        )
        or ""
    ).strip()

    token = str(
        config.get(
            "EPIS_WHATSAPP_BRIDGE_TOKEN"
        )
        or ""
    ).strip()

    if not base_url or not token:
        configure_default_whatsapp_device_controller(
            None
        )

        return False

    try:
        bridge = LocalWhatsappBridge(
            base_url,
            token,
        )

        if vault is None:
            from memory_vault import (
                LocalMemoryVault,
            )

            vault = (
                LocalMemoryVault()
            )

        controller = (
            WhatsappDeviceController(
                vault,
                bridge,
            )
        )

    except Exception:
        configure_default_whatsapp_device_controller(
            None
        )

        return False

    configure_default_whatsapp_device_controller(
        controller
    )

    return True


    def accept_reply_by_contact(
        self,
        *,
        incoming_message_ref: str,
        provider_contact_ref: str,
        content: str,
        confidence: float = 0.65,
    ) -> dict:
        return (
            self.vault
            .accept_external_outreach_reply_by_contact(
                incoming_message_ref=
                    incoming_message_ref,
                provider_contact_ref=
                    provider_contact_ref,
                content=
                    content,
                confidence=
                    confidence,
                provider=
                    "whatsapp",
            )
        )


@dataclass
class FakeWhatsappBridge:
    """Test-only bridge. Never talks to WhatsApp."""

    is_available: bool = True

    def __post_init__(
        self,
    ):
        self.calls = []

    def available(self) -> bool:
        return bool(
            self.is_available
        )

    def send(
        self,
        provider_contact_ref: str,
        message: str,
        *,
        outreach_id: str,
    ) -> dict:
        self.calls.append({
            "provider_contact_ref":
                provider_contact_ref,

            "message":
                message,

            "outreach_id":
                outreach_id,
        })

        return {
            "ok": True,
            "status": "sent",
            "provider_message_ref": (
                "fake-message:"
                + outreach_id
            ),
        }

    def close(self) -> None:
        return None


class WhatsappOutreachProvider:
    provider_id = (
        "whatsapp-local-outreach"
    )

    display_name = (
        "Trusted WhatsApp Outreach"
    )

    priority = 20

    def __init__(
        self,
        resolver: ContactResolver,
        bridge: WhatsappBridge,
        tracker: OutreachTracker,
    ):
        self.resolver = resolver
        self.bridge = bridge
        self.tracker = tracker

    def available(self) -> bool:
        try:
            return bool(
                self.bridge.available()
            )
        except Exception:
            return False

    def capabilities(self) -> set[str]:
        return {
            "whatsapp.send_to_contact",
        }

    def execute(
        self,
        capability: str,
        arguments: dict,
    ) -> dict:
        if (
            capability
            != "whatsapp.send_to_contact"
        ):
            return {
                "ok": False,
                "error":
                    "capability_not_supported",
            }

        contact_ref = str(
            arguments.get(
                "contact_ref"
            )
            or ""
        ).strip()

        message = str(
            arguments.get(
                "message"
            )
            or ""
        ).strip()

        if not contact_ref:
            return {
                "ok": False,
                "error":
                    "contact_ref_required",
            }

        if not message:
            return {
                "ok": False,
                "error":
                    "message_required",
            }

        resolved = (
            self.resolver
            .resolve_contact(
                contact_ref
            )
        )

        status = str(
            resolved.get(
                "status"
            )
            or ""
        )

        if status == "not_found":
            return {
                "ok": False,
                "error":
                    "contact_not_found",
            }

        if status == "ambiguous":
            return {
                "ok": False,
                "error":
                    "contact_ambiguous",
                "candidate_count":
                    int(
                        resolved.get(
                            "candidate_count"
                        )
                        or 0
                    ),
            }

        if status == "not_allowlisted":
            return {
                "ok": False,
                "error":
                    "contact_not_allowlisted",
            }

        if status == "not_configured":
            return {
                "ok": False,
                "error":
                    "contact_whatsapp_not_configured",
            }

        if status != "resolved":
            return {
                "ok": False,
                "error":
                    "contact_resolution_failed",
            }

        provider_contact_ref = str(
            resolved.get(
                "provider_contact_ref"
            )
            or ""
        )

        if not provider_contact_ref:
            return {
                "ok": False,
                "error":
                    "contact_whatsapp_not_configured",
            }

        outreach_id = (
            uuid.uuid4().hex
        )

        person_id = str(
            resolved.get(
                "person_id"
            )
            or ""
        )

        try:
            self.tracker.begin(
                outreach_id=
                    outreach_id,

                person_id=
                    person_id,

                provider_contact_ref=
                    provider_contact_ref,

                message=
                    message,
            )
        except Exception:
            # Fail before transport: never send a message that cannot
            # at least be represented in the trusted local audit state.
            return {
                "ok": False,
                "error":
                    "outreach_state_unavailable",
            }

        try:
            transport_result = (
                self.bridge.send(
                    provider_contact_ref,
                    message,
                    outreach_id=
                        outreach_id,
                )
            )
        except Exception as exc:
            self.tracker.mark_failed(
                outreach_id=
                    outreach_id,
                error=
                    type(exc).__name__,
            )

            return {
                "ok": False,
                "error":
                    "whatsapp_send_failed",
            }

        if not isinstance(
            transport_result,
            dict,
        ):
            self.tracker.mark_failed(
                outreach_id=
                    outreach_id,
                error=
                    "invalid_bridge_result",
            )

            return {
                "ok": False,
                "error":
                    "invalid_bridge_result",
            }

        if not transport_result.get(
            "ok"
        ):
            error = str(
                transport_result.get(
                    "error"
                )
                or "whatsapp_send_failed"
            )

            self.tracker.mark_failed(
                outreach_id=
                    outreach_id,
                error=
                    error,
            )

            result = {
                "ok": False,
                "error":
                    error,
            }

            if type(
                transport_result.get(
                    "retryable"
                )
            ) is bool:
                result["retryable"] = (
                    transport_result[
                        "retryable"
                    ]
                )

            required = (
                transport_result.get(
                    "required"
                )
            )

            if isinstance(
                required,
                list,
            ):
                safe_required = [
                    item
                    for item in required
                    if item in {
                        "ai_identity",
                        "emir_context",
                    }
                ]

                if safe_required:
                    result["required"] = (
                        safe_required
                    )

            return result

        provider_message_ref = str(
            transport_result.get(
                "provider_message_ref"
            )
            or ""
        ).strip()

        if not provider_message_ref:
            self.tracker.mark_failed(
                outreach_id=
                    outreach_id,
                error=
                    "provider_message_ref_missing",
            )

            # The transport reported success, so do not invite an
            # automatic retry that could duplicate the real message.
            return {
                "ok": True,
                "status":
                    "sent_untracked",
                "outreach_id":
                    outreach_id,
                "person_id":
                    person_id,
                "contact_name": str(
                    resolved.get(
                        "canonical_name"
                    )
                    or ""
                ),
                "reply_tracking":
                    False,
            }

        try:
            self.tracker.mark_sent(
                outreach_id=
                    outreach_id,

                provider_message_ref=
                    provider_message_ref,
            )

            reply_tracking = True

        except Exception:
            # The message was sent already. Report that truthfully and
            # fail closed only for reply correlation.
            reply_tracking = False

        # Deliberately strip provider_message_ref and contact transport
        # identity from anything flowing back toward Luna/cloud.
        return {
            "ok": True,
            "status": (
                str(
                    transport_result.get(
                        "status"
                    )
                    or "sent"
                )
                if reply_tracking
                else "sent_untracked"
            ),
            "outreach_id":
                outreach_id,
            "person_id":
                person_id,
            "contact_name": str(
                resolved.get(
                    "canonical_name"
                )
                or ""
            ),
            "reply_tracking":
                reply_tracking,
        }

    def close(self) -> None:
        try:
            self.bridge.close()
        except Exception:
            pass


# ============================================================
# TRUSTED DEVICE ROUTING
# ============================================================

WHATSAPP_DEVICE_SEND_CAPABILITY = (
    "whatsapp.outreach.send"
)

WHATSAPP_DEVICE_REPLY_CAPABILITY = (
    "whatsapp.outreach.accept_reply"
)

WHATSAPP_DEVICE_AUTO_START_CAPABILITY = "whatsapp.auto_conversation.start"
WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY = "whatsapp.auto_conversation.stop"
WHATSAPP_DEVICE_AUTO_SEND_CAPABILITY = "whatsapp.auto_conversation.send_reply"

WHATSAPP_DEVICE_CAPABILITIES = frozenset({
    WHATSAPP_DEVICE_SEND_CAPABILITY,
    WHATSAPP_DEVICE_REPLY_CAPABILITY,
    WHATSAPP_DEVICE_AUTO_START_CAPABILITY,
    WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY,
    WHATSAPP_DEVICE_AUTO_SEND_CAPABILITY,
})


_DEFAULT_WHATSAPP_DEVICE_CONTROLLER = None


def configure_default_whatsapp_device_controller(
    controller,
) -> None:
    """Trusted host wiring only.

    Phase 1D deliberately leaves this unset in production.
    The real bridge will configure it before DeviceWorker starts.
    """

    global _DEFAULT_WHATSAPP_DEVICE_CONTROLLER

    _DEFAULT_WHATSAPP_DEVICE_CONTROLLER = (
        controller
    )


def whatsapp_device_runtime_available() -> bool:
    controller = (
        _DEFAULT_WHATSAPP_DEVICE_CONTROLLER
    )

    if controller is None:
        return False

    try:
        return bool(
            controller.available()
        )
    except Exception:
        return False


def whatsapp_auto_stop_runtime_available() -> bool:
    return _DEFAULT_WHATSAPP_DEVICE_CONTROLLER is not None


class UnavailableWhatsappDeviceController:
    def available(self) -> bool:
        return False

    def send(
        self,
        _arguments: dict,
    ) -> dict:
        return {
            "ok": False,
            "error":
                "whatsapp_bridge_unavailable",
        }

    def accept_reply(
        self,
        _arguments: dict,
    ) -> dict:
        return {
            "ok": False,
            "error":
                "whatsapp_bridge_unavailable",
        }

    start_auto_conversation = accept_reply
    stop_auto_conversation = accept_reply
    send_auto_reply = accept_reply


class WhatsappDeviceController:
    """Private Lenovo-side WhatsApp capability controller."""

    def __init__(
        self,
        vault,
        bridge: WhatsappBridge,
    ):
        self.bridge = bridge

        self.state = (
            VaultOutreachState(
                vault
            )
        )

        self.contacts = (
            VaultContactResolver(
                vault
            )
        )

        self.provider = (
            WhatsappOutreachProvider(
                self.contacts,
                bridge,
                self.state,
            )
        )

    def available(self) -> bool:
        try:
            return bool(
                self.bridge.available()
            )
        except Exception:
            return False

    def send(
        self,
        arguments: dict,
    ) -> dict:
        if not self.available():
            return {
                "ok": False,
                "error":
                    "whatsapp_bridge_unavailable",
            }

        return self.provider.execute(
            "whatsapp.send_to_contact",
            arguments,
        )

    def start_auto_conversation(self, arguments: dict) -> dict:
        if not self.available():
            return {"ok": False, "error": "whatsapp_bridge_unavailable"}
        contact_ref = str(arguments.get("contact_ref") or "").strip()
        goal = str(arguments.get("goal") or "").strip()
        initial = str(arguments.get("initial_message") or "").strip()
        if not contact_ref or not goal:
            return {"ok": False, "error": "contact_and_goal_required"}
        try:
            duration = arguments.get(
                "duration_minutes"
            )

            reply_limit = arguments.get(
                "max_auto_replies"
            )

            started = self.state.vault.start_whatsapp_auto_conversation(
                contact_ref=contact_ref,
                goal=goal,
                initial_message=initial,
                duration_minutes=(
                    None
                    if duration is None
                    else int(duration)
                ),
                max_auto_replies=(
                    0
                    if reply_limit is None
                    else int(reply_limit)
                ),
            )
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_auto_conversation_limits"}
        if not started.get("ok") or not initial:
            if started.get("ok"):
                started["contact_name"] = _safe_contact_name(started.get("contact_name"))
            return started

        session_id = str(started["session_id"])
        sent = self.provider.execute(
            "whatsapp.send_to_contact",
            {"contact_ref": contact_ref, "message": initial},
        )
        if not sent.get("ok") or not sent.get("reply_tracking"):
            self.state.vault.stop_whatsapp_auto_conversation(session_id=session_id)
            if sent.get("ok"):
                return {"ok": True, "status": "sent_untracked",
                        "auto_conversation_active": False,
                        "contact_name": _safe_contact_name(started["contact_name"])}
            safe = {"ok": False, "error": str(sent.get("error") or "initial_send_failed")}
            for key in ("retryable", "required"):
                if key in sent:
                    safe[key] = sent[key]
            return safe
        if not self.state.vault.activate_whatsapp_auto_conversation(
            session_id, str(sent.get("outreach_id") or ""),
        ):
            self.state.vault.stop_whatsapp_auto_conversation(session_id=session_id)
            return {"ok": True, "status": "sent_without_active_session",
                    "auto_conversation_active": False}
        return {"ok": True, "status": "active", "session_id": session_id,
                "contact_name": _safe_contact_name(started["contact_name"]),
                "initial_message_sent": True, "auto_conversation_active": True}

    def stop_auto_conversation(self, arguments: dict) -> dict:
        contact_ref = str(arguments.get("contact_ref") or "").strip()
        if not contact_ref:
            return {"ok": False, "error": "contact_ref_required"}
        return self.state.vault.stop_whatsapp_auto_conversation(contact_ref=contact_ref)

    def send_auto_reply(self, arguments: dict) -> dict:
        session_id = str(arguments.get("session_id") or "").strip()
        attempt_id = str(arguments.get("attempt_id") or "").strip()
        message = str(arguments.get("message") or "").strip()
        if not self.available():
            return {"ok": False, "error": "whatsapp_bridge_unavailable"}
        prepared = self.state.vault.prepare_whatsapp_auto_send(
            session_id=session_id, attempt_id=attempt_id, message=message,
        )
        if not prepared.get("ok"):
            return prepared
        outreach_id = str(prepared["outreach_id"])
        try:
            sent = self.bridge.send(
                prepared["provider_contact_ref"], message,
                outreach_id=outreach_id,
            )
        except Exception:
            self.state.vault.finish_whatsapp_auto_send(
                attempt_id=attempt_id, outreach_id=outreach_id,
                status="unknown", error="bridge_exception",
            )
            return {"ok": False, "error": "auto_send_outcome_unknown", "outcome": "unknown"}
        if not isinstance(sent, dict):
            sent = {"ok": False, "error": "invalid_bridge_result", "outcome": "unknown"}
        if sent.get("ok") and sent.get("provider_message_ref"):
            self.state.vault.finish_whatsapp_auto_send(
                attempt_id=attempt_id, outreach_id=outreach_id,
                status="sent", provider_message_ref=str(sent["provider_message_ref"]),
            )
            return {"ok": True, "status": "sent", "session_id": session_id,
                    "outreach_id": outreach_id}
        error = str(sent.get("error") or "auto_send_failed")
        unknown = (
            bool(sent.get("ok"))
            or sent.get("outcome") == "unknown"
            or error not in {
                "recipient_identity_context_missing",
                "recipient_identity_deception",
            }
        )
        self.state.vault.finish_whatsapp_auto_send(
            attempt_id=attempt_id, outreach_id=outreach_id,
            status="unknown" if unknown else "failed", error=error,
        )
        return {"ok": False, "error": error,
                "outcome": "unknown" if unknown else "failed"}

    def accept_reply(
        self,
        arguments: dict,
    ) -> dict:
        quoted_ref = str(
            arguments.get(
                "provider_message_ref"
            )
            or ""
        ).strip()

        incoming_ref = str(
            arguments.get(
                "incoming_message_ref"
            )
            or ""
        ).strip()

        contact_ref = str(
            arguments.get(
                "provider_contact_ref"
            )
            or ""
        ).strip()

        content = str(
            arguments.get(
                "content"
            )
            or ""
        ).strip()

        confidence = float(
            arguments.get(
                "confidence",
                0.65,
            )
        )

        # An authenticated inbound opt-out revokes the local lease even if
        # outreach correlation later reports unmatched or ambiguous.
        self.state.vault.stop_whatsapp_auto_on_opt_out(
            provider_contact_ref=contact_ref,
            content=content,
        )

        if quoted_ref:
            result = (
                self.state.accept_reply(
                    provider_message_ref=
                        quoted_ref,
                    provider_contact_ref=
                        contact_ref,
                    content=
                        content,
                    confidence=
                        confidence,
                )
            )
        else:
            result = (
                self.state
                .accept_reply_by_contact(
                    incoming_message_ref=
                        incoming_ref,
                    provider_contact_ref=
                        contact_ref,
                    content=
                        content,
                    confidence=
                        confidence,
                )
            )

        response = {"ok": True, "status": str(result.get("status") or "")}
        if isinstance(result.get("outreach_id"), str):
            response["outreach_id"] = result["outreach_id"]
        if response["status"] == "ambiguous" and isinstance(result.get("candidate_count"), int):
            response["candidate_count"] = result["candidate_count"]

        # The cloud relay may expose this human-facing name to an already
        # connected EPIS client, but never the person ID or provider contact
        # reference used to obtain it.
        if result.get("status") in {"accepted", "duplicate"}:
            contact_name = _safe_contact_name(self.contacts.display_name_for_person(
                str(result.get("person_id") or "")
            ))
            if contact_name:
                response["contact_name"] = contact_name

        if result.get("status") == "accepted":
            try:
                claimed = self.state.vault.claim_whatsapp_auto_reply(
                    person_id=str(result.get("person_id") or ""),
                    incoming_message_ref=incoming_ref,
                    content=content,
                )
            except Exception:
                claimed = {"auto_reply_eligible": False}
            if claimed.get("auto_reply_eligible") is True:
                claimed["contact_name"] = _safe_contact_name(claimed.get("contact_name"))
                response.update(claimed)

        return response

def register_whatsapp_device_tools(
    registry,
    controller=None,
):
    """Register hidden device primitives.

    These contracts exist in cloud/local registries so device hello
    validation knows them, but they are never shown directly to Luna.
    """

    from .tools import ToolSpec

    controller = (
        controller
        or _DEFAULT_WHATSAPP_DEVICE_CONTROLLER
        or UnavailableWhatsappDeviceController()
    )

    send_schema = {
        "type": "object",
        "properties": {
            "contact_ref": {
                "type": "string",
                "maxLength": 200,
            },
            "message": {
                "type": "string",
                "maxLength": 4000,
            },
        },
        "required": [
            "contact_ref",
            "message",
        ],
        "additionalProperties": False,
    }

    reply_schema = {
        "type": "object",
        "properties": {
            "provider_message_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "incoming_message_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "provider_contact_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "maxLength": 12000,
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": [
            "incoming_message_ref",
            "provider_contact_ref",
            "content",
        ],
        "additionalProperties": False,
    }

    auto_start_schema = {
        "type": "object", "properties": {
            "contact_ref": {"type": "string", "maxLength": 200},
            "goal": {"type": "string", "maxLength": 2000},
            "initial_message": {"type": "string", "maxLength": 4000},
            "duration_minutes": {"type": "integer"},
            "max_auto_replies": {"type": "integer"},
        }, "required": ["contact_ref", "goal"], "additionalProperties": False,
    }
    auto_stop_schema = {
        "type": "object", "properties": {
            "contact_ref": {"type": "string", "maxLength": 200},
        }, "required": ["contact_ref"], "additionalProperties": False,
    }
    auto_send_schema = {
        "type": "object", "properties": {
            "session_id": {"type": "string", "maxLength": 64},
            "attempt_id": {"type": "string", "maxLength": 64},
            "message": {"type": "string", "maxLength": 4000},
        }, "required": ["session_id", "attempt_id", "message"],
        "additionalProperties": False,
    }

    registry.register(
        ToolSpec(
            "whatsapp_outreach_device_send",
            (
                "Internal trusted-device WhatsApp outreach primitive. "
                "Contact resolution, transport identity and durable "
                "correlation remain on the trusted device. "
                "Never expose directly to Luna."
            ),
            send_schema,
            WHATSAPP_DEVICE_SEND_CAPABILITY,
            risk_class="yellow",
            confirmation_required=True,
            platforms=("windows",),
            model_visible=False,
        ),
        controller.send,
    )

    registry.register(
        ToolSpec(
            "whatsapp_outreach_device_accept_reply",
            (
                "Internal trusted-device correlation primitive for "
                "a reply to an existing WhatsApp outreach. "
                "Never expose directly to Luna."
            ),
            reply_schema,
            WHATSAPP_DEVICE_REPLY_CAPABILITY,
            platforms=("windows",),
            model_visible=False,
        ),
        controller.accept_reply,
    )

    for name, capability, schema, handler in (
        ("whatsapp_auto_device_start", WHATSAPP_DEVICE_AUTO_START_CAPABILITY,
         auto_start_schema, controller.start_auto_conversation),
        ("whatsapp_auto_device_stop", WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY,
         auto_stop_schema, controller.stop_auto_conversation),
        ("whatsapp_auto_device_send_reply", WHATSAPP_DEVICE_AUTO_SEND_CAPABILITY,
         auto_send_schema, controller.send_auto_reply),
    ):
        registry.register(
            ToolSpec(name, "Internal trusted-device WhatsApp auto conversation primitive.",
                     schema, capability, risk_class="yellow", confirmation_required=True,
                     platforms=("windows",), model_visible=False),
            handler,
        )

    return controller


class CoreWhatsappOutreachProvider:
    """Cloud semantic provider backed by exactly one trusted device."""

    provider_id = (
        "whatsapp-trusted-device"
    )

    display_name = (
        "Trusted WhatsApp Device"
    )

    priority = 10

    def __init__(
        self,
        core,
    ):
        self.core = core

    def capabilities(self) -> set[str]:
        return {
            "whatsapp.send_to_contact",
            WHATSAPP_DEVICE_AUTO_START_CAPABILITY,
            WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY,
        }

    def _candidate_transports(
        self,
        device_capability: str = WHATSAPP_DEVICE_SEND_CAPABILITY,
    ) -> list:
        result = []

        for transport in list(
            self.core.transports.values()
        ):
            # WhatsApp private state belongs to an attached
            # trusted device, never the cloud placeholder or
            # an in-process generic local adapter.
            if (
                transport
                is self.core.local_agent
            ):
                continue

            try:
                if not transport.refresh():
                    continue
            except Exception:
                continue

            device = getattr(
                transport,
                "device",
                None,
            )

            if (
                device is not None
                and device.online
                and
                device_capability
                in device.capabilities
            ):
                result.append(
                    transport
                )

        return result

    def available(self) -> bool:
        # Fail closed when multiple trusted devices claim the
        # same private WhatsApp capability.
        return (
            len(self._candidate_transports()) == 1
            or len(self._candidate_transports(WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY)) == 1
        )

    def execute(
        self,
        capability: str,
        arguments: dict,
    ) -> dict:
        device_capability = {
            "whatsapp.send_to_contact": WHATSAPP_DEVICE_SEND_CAPABILITY,
            WHATSAPP_DEVICE_AUTO_START_CAPABILITY: WHATSAPP_DEVICE_AUTO_START_CAPABILITY,
            WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY: WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY,
        }.get(capability)
        if device_capability is None:
            return {
                "ok": False,
                "error":
                    "capability_not_supported",
            }

        candidates = (
            self._candidate_transports(device_capability)
        )

        if not candidates:
            return {
                "ok": False,
                "error":
                    "whatsapp_device_unavailable",
            }

        if len(candidates) != 1:
            return {
                "ok": False,
                "error":
                    "whatsapp_device_ambiguous",
            }

        transport = candidates[0]

        result = transport.execute(
            device_capability,
            dict(arguments),
            confirmed=True,
            request_id=(
                "wa-"
                + uuid.uuid4().hex
            ),
        )

        if not isinstance(
            result,
            dict,
        ):
            return {
                "ok": False,
                "error":
                    "invalid_device_response",
            }

        if result.get("ok") is not True:
            error = str(
                result.get("error")
                or
                "whatsapp_device_failed"
            )

            safe = {
                "ok": False,
                "error": error,
            }

            # Only the disclosure policy's deterministic retry
            # contract may cross the trusted-device boundary.
            if error in {
                "recipient_identity_context_missing",
                "recipient_identity_deception",
            }:
                if type(
                    result.get(
                        "retryable"
                    )
                ) is bool:
                    safe["retryable"] = (
                        result[
                            "retryable"
                        ]
                    )

                required = (
                    result.get(
                        "required"
                    )
                )

                if isinstance(
                    required,
                    list,
                ):
                    safe_required = [
                        item
                        for item in required
                        if item in {
                            "ai_identity",
                            "emir_context",
                        }
                    ]

                    if safe_required:
                        safe["required"] = (
                            safe_required
                        )

            return safe

        if capability in {
            WHATSAPP_DEVICE_AUTO_START_CAPABILITY,
            WHATSAPP_DEVICE_AUTO_STOP_CAPABILITY,
        }:
            safe = {"ok": True}
            for key in ("status", "session_id", "contact_name",
                        "initial_message_sent", "auto_conversation_active"):
                if key in result:
                    safe[key] = (
                        _safe_contact_name(result[key])
                        if key == "contact_name" else result[key]
                    )
            return safe

        # Explicit output allowlist. Provider-side transport
        # refs/JIDs/numbers can never escape to the cloud model.
        safe = {
            "ok": True,
        }

        for key in (
            "status",
            "outreach_id",
            "contact_name",
            "reply_tracking",
        ):
            if key in result:
                safe[key] = result[key]

        return safe

    def close(self) -> None:
        return None
