"""Cloud-safe runtime storage facade.

The cloud/shared AgentCore needs a writable runtime directory for hot chat,
usage and task metadata, but it must not treat bundled/local Layer-1 personal
memory as cloud-owned state.  This facade deliberately exposes no personal
memory and makes accidental long-term reads return empty values.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


class CloudRuntimeMemory:
    """Minimal memory facade for the cloud control plane.

    It is intentionally *not* a replacement for ``MemoryManager``.  Sensitive
    lifetime/identity/sensor state stays on the trusted local device and is
    requested on demand through a device capability.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None):
        base = Path(
            root
            or os.getenv("EPIS_CLOUD_RUNTIME_DIR")
            or (Path(tempfile.gettempdir()) / "epis-cloud-runtime")
        )
        self.memory_dir = str(base.resolve())
        Path(self.memory_dir).mkdir(parents=True, exist_ok=True)

    # ContextBuilder.build_minimal() never calls these.  Empty implementations
    # keep the boundary fail-closed if a future caller accidentally probes this
    # object instead of the trusted local context capability.
    def get_current_state(self) -> dict:
        return {}

    def get_session_buffer_lines(self) -> list[str]:
        return []

    def get_recent_interactions(self, *args, **kwargs) -> list:
        return []

    def search_interactions(self, *args, **kwargs) -> list:
        return []

    def search_sessions(self, *args, **kwargs) -> list:
        return []

    def search_thinking_log(self, *args, **kwargs) -> list:
        return []

    def get_episodic_context(self) -> dict:
        return {}

    def get_people(self) -> dict:
        return {"people": {}}

    def log_interaction(self, *args, **kwargs) -> None:
        return None
