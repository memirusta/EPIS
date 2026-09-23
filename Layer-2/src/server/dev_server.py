"""Developer-only hot-reload server for EPIS."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn


DEFAULT_KEYS_FILE = Path(__file__).resolve().parents[3] / "Layer-3" / "keys.env"
KEYS_FILE = Path(os.getenv("EPIS_KEYS_FILE") or DEFAULT_KEYS_FILE)

if KEYS_FILE.is_file():
    load_dotenv(KEYS_FILE, override=False)

if not (os.getenv("LUNA_API_KEY") or os.getenv("OPENAI_API_KEY")):
    raise RuntimeError("LUNA_API_KEY veya OPENAI_API_KEY yuklenemedi.")

if __name__ == "__main__":
    os.environ.setdefault("EPIS_DEPLOYMENT", "local")
    print("EPIS model API key: OK")
    print(
        "OPENAI_ADMIN_KEY: "
        + ("OK" if os.getenv("OPENAI_ADMIN_KEY") else "NOT SET (local usage only)")
    )

    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=int(os.getenv("EPIS_SERVER_PORT", "8000")),
        reload=True,
    )
