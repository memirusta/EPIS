"""Non-reloading local server entry point managed by the Tauri desktop app."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _load_local_keys() -> None:
    candidates = [
        os.getenv("EPIS_KEYS_FILE"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "EPIS" / "keys.env"),
        r"D:\EPIS\Layer-3\keys.env",
        str(Path(__file__).resolve().parents[3] / "Layer-3" / "keys.env"),
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

    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=int(os.getenv("EPIS_SERVER_PORT", "8000")),
        reload=False,
        log_level=os.getenv("EPIS_SERVER_LOG_LEVEL", "info"),
    )
