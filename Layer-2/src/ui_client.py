"""
EPIS -- UI push istemcisi (Kairos -> epis_chat SSE)
"""

import os
import logging
import requests

logger = logging.getLogger("EPIS.UI_CLIENT")

_DEFAULT = "http://localhost:8080"


def _ui_auth_headers() -> dict:
    secret = (
        (os.getenv("EPIS_UI_SHARED_SECRET") or "").strip()
        or (os.getenv("WEBHOOK_SHARED_SECRET") or "").strip()
    )
    if not secret:
        return {}
    return {"X-EPIS-UI-Secret": secret}


class UIClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("EPIS_UI_URL", _DEFAULT)).rstrip("/")

    def is_ready(self) -> bool:
        try:
            r = requests.get(
                f"{self.base_url}/api/status",
                headers=_ui_auth_headers(),
                timeout=3,
            )
            return r.json().get("ready", False)
        except Exception:
            return False

    def push(
        self,
        message: str,
        trigger_type: str = "",
        priority: str = "medium",
    ) -> bool:
        if not message.strip():
            return False
        try:
            r = requests.post(
                f"{self.base_url}/api/push",
                headers=_ui_auth_headers(),
                json={
                    "message":      message,
                    "trigger_type": trigger_type,
                    "priority":     priority,
                },
                timeout=15,
            )
            if r.status_code == 200:
                logger.info(f"UI push [{trigger_type}]: {message[:60]}")
                return True
            logger.warning(f"UI push HTTP {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.ConnectionError:
            logger.warning("EPIS UI erisilemedi -- push atlandi.")
            return False
        except Exception as e:
            logger.error(f"UI push hatasi: {e}")
            return False
