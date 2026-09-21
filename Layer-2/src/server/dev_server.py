from __future__ import annotations

from dotenv import load_dotenv
import os
import uvicorn


KEYS_FILE = os.getenv("EPIS_KEYS_FILE", r"D:\EPIS\Layer-3\keys.env")

load_dotenv(KEYS_FILE, override=False)

if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError("OPENAI_API_KEY y?klenemedi.")

if __name__ == "__main__":
    print("OPENAI_API_KEY: OK")
    print(
        "OPENAI_ADMIN_KEY: "
        + ("OK" if os.getenv("OPENAI_ADMIN_KEY") else "NOT SET (local usage only)")
    )

    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
