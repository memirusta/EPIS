"""Trusted local personal-context capability.

This module runs inside the Windows device agent.  It resolves the canonical
EPIS repository locally, imports the existing MemoryManager/ContextBuilder only
on that device, and returns a privacy-filtered bounded context string.  Raw
Layer-1 memory, reverse pseudonym mappings and sensor databases never become
cloud filesystem state.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from .permissions import RiskClass
from .repository_context import RepositoryBindingStore
from .tools import ToolRegistry, ToolSpec


MAX_SAFE_CONTEXT_CHARS = 30000


def _repo_root() -> Path:
    configured = (os.getenv("EPIS_LOCAL_REPO") or "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        bound = RepositoryBindingStore().load()
        if not bound:
            raise RuntimeError("personal_context_repository_not_bound")
        root = Path(bound)

    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("personal_context_repository_unavailable") from exc

    src = root / "Layer-2" / "src"
    if not (src / "memory_manager.py").is_file() or not (src / "context_builder.py").is_file():
        raise RuntimeError("personal_context_modules_unavailable")
    return root


def _load_legacy_context_modules():
    root = _repo_root()
    src = str(root / "Layer-2" / "src")
    if src not in sys.path:
        # Device-local import path only.  No repository path is sent to Luna.
        sys.path.insert(0, src)

    from context_builder import ContextBuilder
    from memory_manager import MemoryManager
    from privacy import PrivacyFilter

    return MemoryManager, ContextBuilder, PrivacyFilter


def _personal_context(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query_required"}

    try:
        MemoryManager, ContextBuilder, PrivacyFilter = _load_legacy_context_modules()
        memory = MemoryManager()
        builder = ContextBuilder(memory)
        build_trusted_packet = getattr(builder, "build_trusted_packet", None)
        if callable(build_trusted_packet):
            raw_context = build_trusted_packet(query)
        else:
            # Compatibility for legacy/test ContextBuilder implementations.
            # Production ContextBuilder has build_trusted_packet(), which keeps
            # raw session archives/thinking logs out of the cloud packet.
            raw_context = builder.build(query)
        safe_context, mapping = PrivacyFilter().anonymize(raw_context)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {
            "ok": False,
            "error": "personal_context_unavailable",
            "detail": type(exc).__name__,
        }

    safe_context = (safe_context or "").strip()
    truncated = len(safe_context) > MAX_SAFE_CONTEXT_CHARS
    if truncated:
        safe_context = safe_context[:MAX_SAFE_CONTEXT_CHARS]

    return {
        "ok": True,
        "safe_context": safe_context,
        "privacy": "local_pseudonymization",
        "identity_tokens": sorted(mapping.keys()),
        "reverse_mapping_exported": False,
        "truncated": truncated,
        "guidance": (
            "[KNOWN_USER] kullanici demektir; yanitta tokeni tekrar etmek yerine 'sen' de. "
            "[KISI_n]/[YER_n] icin gercek isim uydurma."
        ),
    }


def register_trusted_context_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_personal_context",
            (
                "Read only the relevant personal memory/sensor/screen context from the "
                "trusted Windows device. The device pseudonymizes identities and PII "
                "before any context leaves the device. Use only when the user's request "
                "actually depends on personal/local context; never for generic questions."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 4000,
                        "description": "The user's current question/context need.",
                    },
                    "device_id": {
                        "type": "string",
                        "description": "Optional registered target device id.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "personal.context.read",
            RiskClass.GREEN.value,
            False,
            effects=("read",),
        ),
        _personal_context,
    )
