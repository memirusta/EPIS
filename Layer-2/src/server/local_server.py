"""Explicit local-development server entry point.

Production Desktop never falls back here; Tauri only starts this file when
EPIS_DESKTOP_LOCAL_SERVER=1 in a debug build.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _load_local_keys() -> None:
    repo_keys = Path(__file__).resolve().parents[3] / "Layer-3" / "keys.env"
    candidates = [
        os.getenv("EPIS_KEYS_FILE"),
        str(repo_keys),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "EPIS" / "keys.env"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            os.environ.setdefault("EPIS_KEYS_FILE", candidate)
            load_dotenv(candidate, override=False)
            return


_load_local_keys()

if not (os.getenv("LUNA_API_KEY") or os.getenv("OPENAI_API_KEY")):
    raise RuntimeError("EPIS model API key could not be loaded")

if __name__ == "__main__":
    import uvicorn

    os.environ.setdefault("EPIS_DEPLOYMENT", "local")
    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=int(os.getenv("EPIS_SERVER_PORT", "8000")),
        reload=False,
        log_level=os.getenv("EPIS_SERVER_LOG_LEVEL", "info"),
    )
