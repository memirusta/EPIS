"""The EPIS 0.1 text-agent loop and deterministic execution boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import os
import time
import uuid
from typing import Any

from .devices import DeviceRegistry
from .luna import LunaClient, ToolCall
from .permissions import PermissionEngine
from .tasks import TaskStore
from .tools import ToolRegistry
from .transport import DeviceTransport


logger = logging.getLogger("EPIS.AGENT")

_SESSION_READ_CAPABILITIES = frozenset({
    "files.list",
    "files.info",
    "files.read_text",
})


@dataclass
class PendingAction:
    tool_call: ToolCall
    messages: list[dict]
    user_message: str
    remaining_calls: list[ToolCall] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    model_steps: int = 0
    sol_delegations: int = 0
    approval_id: str = ""
    approval_message: str = ""
    expires_at: float = field(
        default_factory=lambda: time.monotonic() + 120
    )


@dataclass
class AgentTurn:
    message: str
    tool_results: list[dict]
    confirmation_required: bool = False
    approval: dict[str, Any] | None = None


class SolDelegator:
    """Explicit heavy-reasoning boundary; not part of ordinary tool dispatch."""

    def __init__(self, router=None):
        self.router = router

    def analyze(
        self,
        task: str,
        context: dict | None = None,
    ) -> dict:
        if self.router is None:
            return {
                "ok": False,
                "error": "Sol is not configured",
            }

        return self.router.intercept_tool_call(
            {
                "task_type": "deep_analysis",
                "payload": task,
            },
            context or {},
        )


SOL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_to_sol",
        "description": (
            "Delegate complex analysis, coding review, planning or repository "
            "review to Sol. For repository work, first inspect the repository "
            "with local tools, then provide the repository path and relevant "
            "file contents in context. Sol cannot read the local filesystem itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The analysis request Luna writes for Sol. "
                        "Describe exactly what the user wants reviewed."
                    ),
                },
                "reason": {
                    "type": "string",
                    "enum": [
                        "analysis",
                        "coding",
                        "planning",
                        "repository_review",
                    ],
                },
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Optional absolute local repository path, for identity "
                        "and file-reference context only. Sol cannot open it directly."
                    ),
                },
                "context": {
                    "type": "string",
                    "maxLength": 60000,
                    "description": (
                        "Relevant repository tree, exact file contents, excerpts, "
                        "tool observations and test information already gathered "
                        "by Luna. Never invent file contents."
                    ),
                },
            },
            "required": [
                "task",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
}


CORE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
    for name, description in (
        (
            "get_devices",
            (
                "List registered devices, exact device IDs, "
                "current availability and capabilities. "
                "Use IDs from this result, never invent device IDs."
            ),
        ),
        (
            "get_task_status",
            (
                "Read recent EPIS device-command receipt states. "
                "Unknown means an action may have run; "
                "never retry it automatically."
            ),
        ),
    )
]


class AgentCore:
    """Coordinates Luna, local context, device routing, and controlled tools.

    Hot memory is intentionally separate from daily/weekly/long-term memory.
    It exists only for recent conversational continuity.
    """

    def __init__(
        self,
        luna: LunaClient,
        system_prompt: str,
        context_builder,
        memory,
        registry: ToolRegistry,
        devices: DeviceRegistry,
        local_agent: DeviceTransport,
        permissions: PermissionEngine | None = None,
        sol: SolDelegator | None = None,
        tasks: TaskStore | None = None,
        hot_memory=None,
        usage_repository=None,
    ):
        self.luna = luna
        self.system_prompt = system_prompt
        self.context_builder = context_builder
        self.memory = memory
        self.registry = registry
        self.devices = devices
        self.local_agent = local_agent
        self.permissions = permissions or PermissionEngine()
        self.sol = sol or SolDelegator()

        self.hot_memory = hot_memory
        self.usage_repository = usage_repository
        self.history: list[dict] = []
        self.restored_hot_messages = 0

        self.pending: PendingAction | None = None

        self.tasks = (
            tasks
            if tasks is not None
            else TaskStore()
        )

        self.transports: dict[str, DeviceTransport] = {
            local_agent.device.device_id: local_agent
        }

        self._call_tasks: dict[str, str] = {}

        # Salt-okuma izinleri yalnizca bu EPIS processinde yasar.
        # EPIS kapaninca otomatik olarak unutulur.
        self._session_read_grants: set[tuple[str, str]] = set()

        self._restore_hot_history()

    def _restore_hot_history(
        self,
    ) -> int:
        """Reload recent user/assistant conversation from local hot memory."""

        if self.hot_memory is None:
            return 0

        try:
            self.history = self._clean_hot_messages(
                self.hot_memory.load_recent()
            )

            self.restored_hot_messages = len(
                self.history
            )

            return self.restored_hot_messages

        except Exception as exc:
            self._log(
                "hot_memory_restore_failed",
                error=type(exc).__name__,
            )

            self.history = []
            self.restored_hot_messages = 0

            return 0

    def _refresh_hot_history(
        self,
    ) -> None:
        """Refresh recent conversation before each new user turn."""

        if self.hot_memory is None:
            return

        try:
            self.history = self._clean_hot_messages(
                self.hot_memory.load_recent()
            )

        except Exception as exc:
            self._log(
                "hot_memory_refresh_failed",
                error=type(exc).__name__,
            )

    @staticmethod
    def _clean_hot_messages(
        messages: list[dict],
    ) -> list[dict]:
        cleaned = []

        for message in messages or []:
            role = message.get(
                "role"
            )

            content = message.get(
                "content"
            )

            if role not in {
                "user",
                "assistant",
            }:
                continue

            if (
                not isinstance(
                    content,
                    str,
                )
                or not content.strip()
            ):
                continue

            cleaned.append(
                {
                    "role": role,
                    "content": content.strip(),
                }
            )

        return cleaned

    @staticmethod
    def _approval_safe_arguments(arguments: dict) -> dict:
        """Bound approval-copy context without echoing file/message bodies."""
        safe: dict[str, Any] = {}
        hidden_keys = {
            "content", "body", "message", "text", "token",
            "password", "secret", "api_key", "authorization",
        }

        for key, value in (arguments or {}).items():
            if key == "device_id":
                continue
            if key.casefold() in hidden_keys:
                length = len(value) if isinstance(value, str) else None
                safe[key] = (
                    f"<content omitted; {length} chars>"
                    if length is not None
                    else "<content omitted>"
                )
                continue
            if isinstance(value, str) and len(value) > 500:
                safe[key] = value[:500] + "…"
            else:
                safe[key] = value

        return safe

    def _approval_message(
        self,
        approval: dict,
        user_message: str,
    ) -> str:
        """Ask Luna for approval-card copy; policy remains deterministic."""
        payload = {
            "requested_by_user": user_message,
            "tool": approval.get("tool"),
            "capability": approval.get("capability"),
            "risk": approval.get("risk"),
            "target": approval.get("target"),
            "arguments": self._approval_safe_arguments(
                approval.get("arguments") or {}
            ),
            "policy_reason": approval.get("reason"),
            "safety_note": approval.get("notice"),
        }

        try:
            reply = self.luna.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Sen EPIS'in Luna sesisin. Aşağıdaki bekleyen işlem "
                            "için onay kartında gösterilecek 1-2 kısa, doğal Türkçe "
                            "cümle yaz. Neyi yapacağını ve neden kullanıcı onayı "
                            "gerektiğini somut söyle. Evet/Hayır, buton, JSON, başlık "
                            "veya madde işareti yazma. İşlem yapılmış gibi konuşma. "
                            "Verilmeyen ayrıntıyı uydurma. Teknik policy metnini "
                            "kelimesi kelimesine tekrar etme; EPIS'in doğal sesiyle yaz."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                        ),
                    },
                ],
                [],
            )
            text = (reply.text or "").strip()
            if text:
                return text
        except Exception as exc:
            self._log(
                "approval_copy_failed",
                error=type(exc).__name__,
            )

        # Fail-safe only when Luna is unavailable. The normal UI copy is model-owned.
        return (
            str(approval.get("notice") or "").strip()
            or "Bu işlem devam etmeden önce onayını gerektiriyor."
        )

    def handle(
        self,
        user_message: str,
    ) -> AgentTurn:
        # A pending approval is independent from ordinary conversation.
        # The user may keep chatting while the approval card remains visible.
        # Keep the original task receipt mapping alive until that approval resolves.
        if self.pending is None:
            self._call_tasks = {}

        self._refresh_hot_history()

        mode = os.getenv(
            "EPIS_LUNA_CONTEXT_MODE",
            "minimal",
        ).lower()

        if mode not in {
            "minimal",
            "local",
        }:
            raise ValueError(
                "EPIS_LUNA_CONTEXT_MODE must be minimal or local"
            )

        if mode == "minimal":
            context = (
                self.context_builder.build_minimal()
            )
        else:
            context = (
                self.context_builder.build(
                    user_message
                )
            )

        system = (
            self.system_prompt
            + "\n\n# AGENT SINIRI\n"
            + (
                "Tool sonucu gÃ¶rmeden iÅŸlem yapÄ±lmÄ±ÅŸ gibi konuÅŸma. "
                "Tool Ã§aÄŸrÄ±larÄ± yalnÄ±zca Ã¶neridir; Core izin ve cihaz "
                "kontrolÃ¼nden geÃ§irir. Tool sonucu geldikten sonra "
                "kullanÄ±cÄ±ya doÄŸal, kÄ±sa ve tek EPIS sesiyle yanÄ±t ver. "

                "KarmaÅŸÄ±k analiz, coding, planning veya repository inceleme "
                "gerekiyorsa delegate_to_sol kullan; normal sohbet ve yerel "
                "cihaz araÃ§larÄ± iÃ§in Sol'u Ã§aÄŸÄ±rma. "

                "Sol sonucunu doÄŸrudan yapÄ±ÅŸtÄ±rma, EPIS'in tutarlÄ± sesiyle "
                "sentezle. "

                "Repository incelemesinde Luna orkestratÃ¶rdÃ¼r, Sol uzman "
                "analiz katmanÄ±dÄ±r. "

                "KullanÄ±cÄ± repo yolunu verdiyse veya yakÄ±n sohbet baÄŸlamÄ±ndan "
                "biliniyorsa tekrar isteme. "

                "Ã–nce list_folder, get_file_info ve read_text_file gibi yerel "
                "araÃ§larla gÃ¶reve iliÅŸkin repository yapÄ±sÄ±nÄ± ve ilgili "
                "dosyalarÄ± gerÃ§ekten incele. "

                "ArdÄ±ndan delegate_to_sol Ã§aÄŸrÄ±sÄ±nda repo_path alanÄ±na repo "
                "yolunu, context alanÄ±na yalnÄ±zca araÃ§larla gerÃ§ekten gÃ¶rdÃ¼ÄŸÃ¼n "
                "ilgili dosya yollarÄ±nÄ±, kod iÃ§eriklerini, testleri ve "
                "gÃ¶zlemleri koy. "

                "Sadece repo yolunu verip Sol'dan diski aÃ§masÄ±nÄ± isteme; "
                "Sol yerel dosya sistemine doÄŸrudan eriÅŸemez. "

                "KullanÄ±cÄ±nÄ±n isteÄŸine gÃ¶re Sol iÃ§in aÃ§Ä±k ve teknik bir "
                "gÃ¶rev yaz. "

                "Sol bir deÄŸiÅŸiklik Ã¶nerirse bunu otomatik uygulama; "
                "kullanÄ±cÄ±ya hangi dosya veya fonksiyonda ne Ã¶nerdiÄŸini, "
                "nedenini, riskini ve gereken testi EPIS'in kendi aÄŸzÄ±yla "
                "anlat ve uygulamak isteyip istemediÄŸini sor. "

                "KullanÄ±cÄ± daha sonraki bir mesajda aÃ§Ä±kÃ§a onay verirse "
                "deÄŸiÅŸiklikten Ã¶nce ilgili dosyalarÄ± yeniden oku ve gÃ¼ncel "
                "olduklarÄ±nÄ± doÄŸrula. "

                "KullanÄ±cÄ± desteklenen bir iÅŸlemi istediyse uygun tool "
                "Ã§aÄŸrÄ±sÄ±nÄ± Ã¶ner. "

                "Onay gereken bir araÃ§ iÃ§in tool Ã§aÄŸrÄ±sÄ±nÄ± normal ÅŸekilde Ã¼ret; "
                "onay kararÄ±nÄ± model verme. Core iÅŸlemi durdurur, Luna'dan ayrÄ± "
                "bir doÄŸal onay aÃ§Ä±klamasÄ± Ã¼rettirir ve UI Evet/HayÄ±r kartÄ± gÃ¶sterir. "
                "Onay gelmeden iÅŸlemi yapÄ±lmÄ±ÅŸ sayma. "

                "Uygulama açma isteklerinde kullanıcı uygulamayı takma ad, renk, "
                "kategori, kısaltma veya komut adıyla tarif edebilir. Bunu semantik "
                "olarak muhtemel kanonik uygulama adına çevirip discover_apps ile ara; "
                "sonuç yoksa bir kez daha genelleştirerek ara. Yalnızca gerçekten "
                "dönen app_id/app_name çiftini launch_discovered_app ile aç; yol veya "
                "uygulama kimliği uydurma. Komut istemcileri de uygulama olarak açılabilir, "
                "ama bu onların içinde komut çalıştırma izni vermez. "

                "AraÃ§ argÃ¼manlarÄ± belirsizse aÃ§Ä±klayÄ±cÄ± soru sor. "
                "Desteklenmeyen iÅŸlemi desteklenmiÅŸ sayma."
            )
        )

        if context:
            system += (
                "\n\n# ANLIK BAGLAM\n"
                + context
            )

        if self.pending is not None:
            system += (
                "\n\n# BEKLEYEN ONAY\n"
                "UI'da ayrı bir işlem onay bekliyor. Kullanıcı bu sırada "
                "normal sohbet etmeye devam edebilir. Yeni mesajı eski işlemin "
                "onayı veya reddi sayma; eski işlemi yapılmış da sayma. "
                "Evet/Hayır butonları Core tarafından ayrı yönetilir."
            )

        messages = [
            {
                "role": "system",
                "content": system,
            },
            *self.history,
            {
                "role": "user",
                "content": user_message,
            },
        ]

        return self._run(
            messages,
            user_message,
        )


    @staticmethod
    def _normalize_session_read_path(
        value: Any,
    ) -> str | None:
        if (
            not isinstance(value, str)
            or not value.strip()
        ):
            return None

        raw = value.strip()

        # Network / UNC path session grant alamaz.
        if (
            raw.startswith("\\")
            or not os.path.isabs(raw)
        ):
            return None

        normalized = os.path.normcase(
            os.path.abspath(
                os.path.normpath(raw)
            )
        )

        drive, _ = os.path.splitdrive(
            normalized
        )

        if not drive:
            return None

        return normalized

    def _session_read_root_for_call(
        self,
        capability: str,
        arguments: dict,
    ) -> str | None:
        if (
            capability
            not in _SESSION_READ_CAPABILITIES
        ):
            return None

        path = (
            self._normalize_session_read_path(
                arguments.get("path")
            )
        )

        if not path:
            return None

        # list_folder zaten klasor alir.
        # info/read dosya alir, bu durumda parent root olur.
        if capability == "files.list":
            root = path
        else:
            root = os.path.dirname(
                path
            )

        drive, _ = os.path.splitdrive(
            root
        )

        # D:\ gibi tum diski tek onayla acma.
        if (
            root.rstrip("/\\")
            == drive.rstrip("/\\")
        ):
            return None

        return root

    @staticmethod
    def _path_is_inside_root(
        path: str,
        root: str,
    ) -> bool:
        try:
            return (
                os.path.commonpath(
                    [path, root]
                )
                == root
            )

        except (
            ValueError,
            OSError,
        ):
            return False

    def _has_session_read_grant(
        self,
        capability: str,
        arguments: dict,
        device_id: str,
    ) -> bool:
        if (
            capability
            not in _SESSION_READ_CAPABILITIES
        ):
            return False

        path = (
            self._normalize_session_read_path(
                arguments.get("path")
            )
        )

        if not path:
            return False

        return any(
            (
                granted_device
                == device_id
                and self._path_is_inside_root(
                    path,
                    root,
                )
            )
            for (
                granted_device,
                root,
            )
            in self._session_read_grants
        )

    def _grant_session_read_for_call(
        self,
        capability: str,
        arguments: dict,
        device_id: str,
    ) -> str | None:
        root = (
            self._session_read_root_for_call(
                capability,
                arguments,
            )
        )

        if not root:
            return None

        self._session_read_grants.add(
            (
                device_id,
                root,
            )
        )

        self._log(
            "session_read_granted",
            device=device_id,
            root=root,
        )

        return root

    def _supersede_pending(self) -> None:
        """Cancel one older pending action when a newer approval replaces it."""
        if self.pending is None:
            return

        pending, self.pending = self.pending, None
        for call in [pending.tool_call, *pending.remaining_calls]:
            task_id = self._call_tasks.get(call.call_id)
            if task_id:
                self.tasks.finish(task_id, "cancelled")

        self._log(
            "pending_approval_superseded",
            tool=pending.tool_call.name,
        )

    def confirm_pending(
        self,
        approval_id: str | None = None,
    ) -> AgentTurn:
        if not self.pending:
            return AgentTurn(
                "Onay bekleyen bir iÅŸlem yok.",
                [],
            )

        if (
            approval_id is not None
            and approval_id != self.pending.approval_id
        ):
            return AgentTurn(
                "Bu onay isteği artık geçerli değil.",
                [],
            )

        pending, self.pending = (
            self.pending,
            None,
        )

        if (
            time.monotonic()
            > pending.expires_at
        ):
            self.pending = pending

            turn = self.reject_pending()

            turn.message = (
                "Onayın süresi doldu; işlem yapılmadı. "
                "İstersen yeniden iste."
            )

            return turn

        entry = self.registry.get(
            pending.tool_call.name
        )

        if entry:
            pending_spec, _ = entry

            pending_device = (
                pending.tool_call.arguments.get(
                    "device_id"
                )
                or self.local_agent.device.device_id
            )

            self._grant_session_read_for_call(
                pending_spec.capability,
                pending.tool_call.arguments,
                pending_device,
            )

        turn = self._dispatch(
            pending.tool_call,
            confirmed=True,
        )

        pending.results.extend(
            turn.tool_results
        )

        self._append_result(
            pending.messages,
            pending.tool_call,
            turn.tool_results[0],
        )

        return self._run(
            pending.messages,
            pending.user_message,
            pending.results,
            pending.model_steps,
            pending.sol_delegations,
            pending.remaining_calls,
        )

    def reject_pending(
        self,
        approval_id: str | None = None,
    ) -> AgentTurn:
        if not self.pending:
            return AgentTurn(
                "Onay bekleyen bir iÅŸlem yok.",
                [],
            )

        if (
            approval_id is not None
            and approval_id != self.pending.approval_id
        ):
            return AgentTurn(
                "Bu onay isteği artık geçerli değil.",
                [],
            )

        pending, self.pending = (
            self.pending,
            None,
        )

        for call in [
            pending.tool_call,
            *pending.remaining_calls,
        ]:
            if (
                call.call_id
                in self._call_tasks
            ):
                self.tasks.finish(
                    self._call_tasks[
                        call.call_id
                    ],
                    "cancelled",
                )

            self._append_result(
                pending.messages,
                call,
                {
                    "ok": False,
                    "error": (
                        "Cancelled by user; "
                        "not executed"
                    ),
                },
            )

        text = (
            "Tamam, bekleyen iÅŸlemleri iptal ettim."
        )

        pending.messages.append(
            {
                "role": "assistant",
                "content": text,
            }
        )

        self._remember(
            pending.messages,
            pending.user_message,
            text,
        )

        return AgentTurn(
            text,
            pending.results,
        )

    def delegate_to_sol(
        self,
        task: str,
        context: dict | None = None,
    ) -> dict:
        """Delegate one bounded specialist analysis to Sol."""

        return self.sol.analyze(
            task,
            context,
        )

    @staticmethod
    def _append_result(
        messages,
        call,
        result,
    ):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": (
                    call.call_id
                ),
                "content": json.dumps(
                    result,
                    ensure_ascii=False,
                ),
            }
        )

    def _run(
        self,
        messages: list[dict],
        user_message: str,
        results=None,
        model_steps=0,
        sol_delegations=0,
        remaining_calls=None,
    ) -> AgentTurn:
        results = (
            results
            if results is not None
            else []
        )

        calls = list(
            remaining_calls or []
        )

        while (
            calls
            or model_steps < 32
        ):
            if not calls:
                try:
                    reply = self.luna.complete(
                        messages,
                        self._model_tools(),
                    )

                except Exception as exc:
                    self._log(
                        "model_failed",
                        error=type(exc).__name__,
                    )

                    text = (
                        "AraÃ§ sonuÃ§larÄ± alÄ±ndÄ± ama yanÄ±t baÄŸlantÄ±sÄ± kesildi. "
                        "Ä°ÅŸlemleri otomatik tekrarlamadÄ±m."
                        if results
                        else (
                            "Model baÄŸlantÄ±sÄ± kurulamadÄ±. "
                            "Anahtar, bakiye ve baÄŸlantÄ±yÄ± "
                            "kontrol edebilirsin."
                        )
                    )

                    messages.append(
                        {
                            "role": "assistant",
                            "content": text,
                        }
                    )

                    self._remember(
                        messages,
                        user_message,
                        text,
                    )

                    return AgentTurn(
                        text,
                        results,
                    )

                model_steps += 1

                messages.append(
                    reply.as_assistant_message()
                )

                if not reply.tool_calls:
                    text = (
                        reply.text
                        or (
                            "YanÄ±t tamamlanamadÄ±; "
                            "tekrar dener misin?"
                        )
                    )

                    self._remember(
                        messages,
                        user_message,
                        text,
                    )

                    return AgentTurn(
                        text,
                        results,
                    )

                calls = list(
                    reply.tool_calls
                )

            batch, calls = (
                calls,
                [],
            )

            for index, call in enumerate(
                batch
            ):
                if any(
                    result.get("outcome")
                    == "unknown"
                    for result in results
                ):
                    result = {
                        "ok": False,
                        "error": (
                            "Previous execution outcome unknown; "
                            "no further actions this turn. "
                            "Ask user before any retry."
                        ),
                    }

                    results.append(
                        result
                    )

                    self._append_result(
                        messages,
                        call,
                        result,
                    )

                    continue

                if (
                    model_steps >= 32
                    or len(results) >= 64
                ):
                    result = {
                        "ok": False,
                        "error": (
                            "Tool budget exhausted; "
                            "not executed"
                        ),
                    }

                    results.append(
                        result
                    )

                    self._append_result(
                        messages,
                        call,
                        result,
                    )

                    continue

                if (
                    call.name
                    == "delegate_to_sol"
                ):
                    result = (
                        self._run_sol_delegation(
                            call,
                            sol_delegations=(
                                sol_delegations
                            ),
                        )
                    )

                    if result.pop(
                        "_delegation_used",
                        False,
                    ):
                        sol_delegations += 1

                    self._log(
                        "sol_delegated",
                        ok=result.get(
                            "ok"
                        ),
                    )

                    results.append(
                        result
                    )

                    self._append_result(
                        messages,
                        call,
                        result,
                    )

                    continue

                turn = self._dispatch(
                    call,
                    confirmed=False,
                )

                results.extend(
                    turn.tool_results
                )

                if (
                    turn.confirmation_required
                ):
                    approval = dict(turn.approval or {})
                    approval_id = uuid.uuid4().hex
                    approval_message = self._approval_message(
                        approval,
                        user_message,
                    )

                    if self.pending is not None:
                        self._supersede_pending()

                    self.pending = (
                        PendingAction(
                            deepcopy(call),
                            list(messages),
                            user_message,
                            batch[
                                index + 1 :
                            ],
                            results,
                            model_steps,
                            sol_delegations,
                        )
                    )

                    approval_payload = {
                        "id": approval_id,
                        "message": approval_message,
                        "tool": approval.get("tool", call.name),
                        "capability": approval.get("capability"),
                        "risk": approval.get("risk"),
                    }
                    # Store UI identity alongside the pending action without
                    # exposing it to device execution arguments.
                    self.pending.approval_id = approval_id
                    self.pending.approval_message = approval_message

                    return AgentTurn(
                        "",
                        results,
                        True,
                        approval_payload,
                    )

                for result in (
                    turn.tool_results
                ):
                    self._append_result(
                        messages,
                        call,
                        result,
                    )

        fallback = (
            "Ä°ÅŸlemi tamamlayamadÄ±m; "
            "fazla sayÄ±da araÃ§ adÄ±mÄ± oluÅŸtu."
        )

        messages.append(
            {
                "role": "assistant",
                "content": fallback,
            }
        )

        self._remember(
            messages,
            user_message,
            fallback,
        )

        return AgentTurn(
            fallback,
            results,
        )

    def _run_sol_delegation(
        self,
        call: ToolCall,
        *,
        sol_delegations: int,
    ) -> dict:
        error = self.registry._validate(
            SOL_TOOL_SCHEMA[
                "function"
            ][
                "parameters"
            ],
            call.arguments,
        )

        if error:
            return {
                "ok": False,
                "error": error,
            }

        if sol_delegations >= 1:
            return {
                "ok": False,
                "error": (
                    "Sol delegation limit reached "
                    "for this turn"
                ),
            }

        task = call.arguments.get(
            "task",
            "",
        )

        reason = call.arguments.get(
            "reason"
        )

        repo_path = (
            call.arguments.get(
                "repo_path",
                "",
            )
            or ""
        ).strip()

        repo_context = (
            call.arguments.get(
                "context",
                "",
            )
            or ""
        ).strip()

        if (
            not isinstance(
                task,
                str,
            )
            or not task.strip()
        ):
            return {
                "ok": False,
                "error": (
                    "Sol task must be non-empty"
                ),
            }

        if len(
            repo_context
        ) > 60000:
            repo_context = (
                repo_context[:60000]
                + "\n\n[EPIS: repository context truncated]"
            )

        sol_prompt_parts = [
            (
                "You are Sol, EPIS's specialist reasoning layer. "
                "You are not the user-facing assistant; Luna/EPIS "
                "will communicate your findings to the user."
            ),
            (
                "Analyze only the evidence supplied below. "
                "Do not claim to have opened local files yourself."
            ),
            (
                "IMPORTANT CHANGE POLICY:\n"
                "- Do not modify anything.\n"
                "- Do not claim that a modification was applied.\n"
                "- If you recommend a code change, clearly state:\n"
                "  1. affected file/function\n"
                "  2. proposed change\n"
                "  3. concrete reason\n"
                "  4. possible risk/side effect\n"
                "  5. tests that should verify it\n"
                "- Luna will explain proposed changes to the user and "
                "ask before any modification is attempted."
            ),
        ]

        if repo_path:
            sol_prompt_parts.append(
                "REPOSITORY:\n"
                + repo_path
            )

        sol_prompt_parts.append(
            "USER REQUEST / LUNA TASK:\n"
            + task.strip()
        )

        if repo_context:
            sol_prompt_parts.append(
                "REPOSITORY CONTEXT COLLECTED BY LUNA:\n"
                + repo_context
            )

        else:
            sol_prompt_parts.append(
                (
                    "REPOSITORY CONTEXT:\n"
                    "No source contents were supplied. "
                    "If the task requires code inspection, "
                    "say that the supplied evidence is "
                    "insufficient instead of guessing."
                )
            )

        sol_prompt = (
            "\n\n==============================\n\n".join(
                sol_prompt_parts
            )
        )

        try:
            result = self.delegate_to_sol(
                sol_prompt,
                {
                    "reason": reason,
                    "repo_path": (
                        repo_path
                        or None
                    ),
                    "source": (
                        "luna_repository_context"
                        if repo_context
                        else "luna_task_only"
                    ),
                },
            )

        except Exception as exc:
            result = {
                "ok": False,
                "error": (
                    type(exc).__name__
                ),
            }

        if not isinstance(
            result,
            dict,
        ):
            result = {
                "ok": True,
                "result": result,
            }

        result[
            "_delegation_used"
        ] = True

        return result

    def _dispatch(
        self,
        call: ToolCall,
        confirmed: bool,
    ) -> AgentTurn:
        if call.name in {
            "get_devices",
            "get_task_status",
        }:
            error = (
                self.registry._validate(
                    {
                        "properties": {},
                        "additionalProperties": False,
                    },
                    call.arguments,
                )
            )

            if error:
                return AgentTurn(
                    "GeÃ§ersiz istek.",
                    [
                        {
                            "ok": False,
                            "error": error,
                        }
                    ],
                )

            if (
                call.name
                == "get_devices"
            ):
                for transport in (
                    self.transports.values()
                ):
                    transport.refresh()

                result = {
                    "ok": True,
                    "devices": (
                        self.devices.list_public()
                    ),
                }

            else:
                result = {
                    "ok": True,
                    "tasks": (
                        self.tasks.recent()
                    ),
                }

            return AgentTurn(
                "",
                [result],
            )

        entry = self.registry.get(
            call.name
        )

        if not entry:
            result = {
                "ok": False,
                "error": (
                    f"Unknown tool: {call.name}"
                ),
            }

            return AgentTurn(
                "Bu iÅŸlemi desteklemiyorum.",
                [result],
            )

        spec, _ = entry

        error = (
            self.registry._validate(
                spec.schema,
                call.arguments,
            )
        )

        if error:
            return AgentTurn(
                "GeÃ§ersiz araÃ§ isteÄŸi.",
                [
                    {
                        "ok": False,
                        "error": error,
                    }
                ],
            )

        decision = (
            self.permissions.decide(
                spec,
                call.arguments,
            )
        )

        if not decision.allowed:
            return AgentTurn(
                (
                    "Bu iÅŸlem izin politikasÄ± "
                    "tarafÄ±ndan engellendi."
                ),
                [
                    {
                        "ok": False,
                        "error": (
                            decision.reason
                        ),
                    }
                ],
            )

        requested_device = call.arguments.get(
            "device_id"
        )
        local_device = self.local_agent.device
        preferred_device = requested_device
        if (
            preferred_device is None
            and local_device.online
            and spec.capability in local_device.capabilities
        ):
            preferred_device = local_device.device_id

        if preferred_device:
            preferred_transport = self.transports.get(
                preferred_device
            )
            if preferred_transport:
                preferred_transport.refresh()

        device = self.devices.find_capable(
            spec.capability,
            preferred_device=preferred_device,
        )
        target_device = (
            device.device_id
            if device is not None
            else (
                requested_device
                or local_device.device_id
            )
        )

        if (
            call.call_id
            not in self._call_tasks
        ):
            self._call_tasks[
                call.call_id
            ] = self.tasks.create(
                call.name,
                target_device,
            )

        task_id = (
            self._call_tasks[
                call.call_id
            ]
        )

        session_read_target_device = target_device

        session_read_root = (
            self._session_read_root_for_call(
                spec.capability,
                call.arguments,
            )
        )

        session_read_granted = (
            self._has_session_read_grant(
                spec.capability,
                call.arguments,
                session_read_target_device,
            )
        )

        if (
            decision.requires_confirmation
            and not confirmed
            and not session_read_granted
        ):
            target = (
                device.display_name
                if device is not None
                else target_device
            )
            reason = decision.reason
            notice = spec.confirmation_notice

            if session_read_root:
                reason = "session read access requires confirmation"
                notice = (
                    "Bu klasör altında salt-okuma erişimi verilecek; "
                    "okunan veriler model API'sine gönderilebilir. "
                    "Yazma, kopyalama, taşıma ve shell dahil değildir."
                )

            return AgentTurn(
                "",
                [],
                True,
                {
                    "tool": call.name,
                    "capability": spec.capability,
                    "risk": spec.risk_class,
                    "target": target,
                    "arguments": deepcopy(call.arguments),
                    "reason": reason,
                    "notice": notice,
                },
            )

        if not device:
            self.tasks.finish(
                task_id,
                "failed",
            )

            return AgentTurn(
                (
                    "Bu iÅŸlemi yapabilecek "
                    "Ã§evrimiÃ§i bir cihaz yok."
                ),
                [
                    {
                        "ok": False,
                        "error": (
                            "No device for "
                            f"{spec.capability}"
                        ),
                    }
                ],
            )

        transport = self.transports.get(
            device.device_id
        )

        if not transport:
            self.tasks.finish(
                task_id,
                "failed",
            )

            return AgentTurn(
                (
                    "Uzak cihaz ajanÄ± henÃ¼z "
                    "bu oturumda baÄŸlÄ± deÄŸil."
                ),
                [
                    {
                        "ok": False,
                        "error": (
                            "remote agent unavailable"
                        ),
                    }
                ],
            )

        if not self.tasks.claim(
            task_id
        ):
            return AgentTurn(
                (
                    "Bu iÅŸlem tekrar "
                    "Ã§alÄ±ÅŸtÄ±rÄ±lmadÄ±."
                ),
                [
                    {
                        "ok": False,
                        "task_id": task_id,
                        "error": (
                            "Action already claimed; "
                            "not replayed"
                        ),
                    }
                ],
            )

        started_at = time.perf_counter()
        try:
            result = (
                transport.execute(
                    spec.capability,
                    call.arguments,
                    confirmed=(
                        confirmed
                        or session_read_granted
                    ),
                    request_id=task_id,
                )
            )

        except Exception as exc:
            result = {
                "ok": False,
                "outcome": "unknown",
                "error": (
                    type(exc).__name__
                ),
            }

        if not isinstance(result, dict):
            result = {
                "ok": False,
                "outcome": "unknown",
                "error": "Invalid device result",
            }

        state = (
            "unknown"
            if (
                result.get("outcome")
                == "unknown"
            )
            else (
                "succeeded"
                if result.get("ok")
                else "failed"
            )
        )

        self.tasks.finish(
            task_id,
            state,
        )

        self._record_tool_usage(
            operation=call.name,
            capability=spec.capability,
            status=state,
            latency_ms=round(
                (time.perf_counter() - started_at) * 1000
            ),
            device_id=device.device_id,
        )

        self._log(
            "tool_dispatched",
            tool=call.name,
            capability=spec.capability,
            device=device.device_id,
            ok=result.get("ok"),
        )

        return AgentTurn(
            "",
            [result],
        )

    def _remember(
        self,
        messages: list[dict],
        user_message: str,
        response: str,
    ) -> None:
        """Persist completed conversation turn."""

        if self.hot_memory is not None:
            try:
                self.hot_memory.append_turn(
                    user_message,
                    response,
                )

            except Exception as exc:
                self._log(
                    "hot_memory_write_failed",
                    error=type(exc).__name__,
                )

        if self.hot_memory is not None:
            try:
                self.history = (
                    self._clean_hot_messages(
                        self.hot_memory.load_recent()
                    )
                )

            except Exception as exc:
                self._log(
                    "hot_memory_refresh_failed",
                    error=type(exc).__name__,
                )

                self.history = (
                    self._conversation_only(
                        messages
                    )
                )

        else:
            self.history = (
                self._conversation_only(
                    messages
                )
            )

        if os.getenv("EPIS_DEPLOYMENT", "local").lower() != "cloud":
            try:
                self.memory.log_interaction(
                    "agent_turn",
                    (
                        f"kullanÄ±cÄ±: {user_message}\n"
                        f"EPIS: {response}"
                    ),
                    tags=[
                        "agentic-0.1",
                    ],
                )

            except Exception as exc:
                self._log(
                    "memory_write_failed",
                    error=type(exc).__name__,
                )

    @staticmethod
    def _conversation_only(
        messages: list[dict],
    ) -> list[dict]:
        result = []

        for message in messages:
            role = message.get(
                "role"
            )

            content = message.get(
                "content"
            )

            if role not in {
                "user",
                "assistant",
            }:
                continue

            if not isinstance(
                content,
                str,
            ):
                continue

            content = content.strip()

            if not content:
                continue

            result.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        return result

    def new_conversation(
        self,
    ) -> int:
        """Clear only active/hot conversation context.

        Daily, weekly, identity, personality and long-term memories remain.
        """

        if self.pending:
            self.reject_pending()

        cleared = len(
            self.history
        )

        self.history = []
        self.restored_hot_messages = 0
        self._call_tasks = {}

        if self.hot_memory is not None:
            try:
                self.hot_memory.mark_new_conversation()

            except Exception as exc:
                self._log(
                    "hot_memory_boundary_failed",
                    error=type(exc).__name__,
                )

        return cleared

    def _model_tools(
        self,
    ) -> list[dict]:
        return [
            *self.registry.openai_schemas(),
            *CORE_TOOL_SCHEMAS,
            SOL_TOOL_SCHEMA,
        ]

    def attach_transport(
        self,
        transport: DeviceTransport,
    ):
        """Trusted host wiring only. Never exposed to the model as a tool."""

        device_id = (
            transport.device.device_id
        )

        if (
            device_id
            in self.transports
        ):
            raise ValueError(
                "Device already attached"
            )

        self.devices.register(
            transport.device
        )

        self.transports[
            device_id
        ] = transport

    def detach_transport(
        self,
        device_id: str,
        transport: DeviceTransport | None = None,
    ) -> bool:
        """Detach only the expected live connection and mark it offline."""

        current = self.transports.get(device_id)
        if current is None or (
            transport is not None
            and current is not transport
        ):
            return False
        if current is self.local_agent:
            return False
        self.transports.pop(device_id, None)
        current.close()
        device = self.devices.get(device_id)
        if device is not None:
            device.online = False
        return True

    def _record_tool_usage(
        self,
        *,
        operation: str,
        capability: str,
        status: str,
        latency_ms: int,
        device_id: str,
    ) -> None:
        if self.usage_repository is None:
            return
        prefix = capability.split(".", 1)[0]
        external_providers = {
            "spotify",
            "whatsapp",
            "gmail",
            "outlook",
            "browser",
            "codex",
        }
        provider = prefix if prefix in external_providers else "local-device"
        try:
            self.usage_repository.record_tool_call(
                provider=provider,
                operation=operation,
                capability=capability,
                status=status,
                latency_ms=latency_ms,
                api_requests=1 if provider in external_providers else 0,
                estimated_cost=None,
                currency=None,
                device_id=device_id,
            )
        except Exception as exc:
            self._log(
                "tool_usage_write_failed",
                error=type(exc).__name__,
            )

    def close(
        self,
    ):
        if self.pending:
            self.reject_pending()

        for transport in (
            self.transports.values()
        ):
            transport.close()

        self.tasks.close()

        if self.usage_repository is not None:
            self.usage_repository.close()

    @staticmethod
    def _log(
        event: str,
        **data: Any,
    ) -> None:
        logger.info(
            json.dumps(
                {
                    "event": event,
                    **data,
                },
                ensure_ascii=False,
                default=str,
            )
        )
