#!/usr/bin/env python3
"""Compatibility entrypoint for the unified EPIS server.

The WhatsApp webhook no longer creates its own Layer1Engine/Router/MemoryManager
session.  Both the legacy ``uvicorn whatsapp_webhook:app`` command and the
normal cloud Procfile exposes the same ``server.app`` / shared AgentCore.
"""

from server.app import app


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(
        "server.app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
