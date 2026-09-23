#!/usr/bin/env python3
"""Compatibility entrypoint for the unified EPIS server.

The historical browser chat server used to instantiate a second Layer1Engine.
That production path is retired: launching ``epis_chat`` now starts the same
shared ``server.app`` used by Desktop, Mobile and WhatsApp.
"""

from server.app import app


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=os.getenv("EPIS_UI_HOST", "127.0.0.1"),
        port=int(os.getenv("EPIS_UI_PORT", "8080")),
        reload=False,
    )
