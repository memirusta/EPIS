"""
EPIS -- WhatsApp İstemcisi
==========================
Kairos ve diğer modüller bu sınıfı kullanarak
whatsapp_bridge/server.js üzerinden mesaj gönderir.
"""

import os
import logging
import requests

logger = logging.getLogger("EPIS.WHATSAPP_CLIENT")

_BRIDGE_URL_DEFAULT = "http://localhost:3001"


class WhatsAppClient:
    """whatsapp_bridge (Node.js) ile HTTP üzerinden iletişim kurar."""

    def __init__(self, bridge_url: str | None = None, my_number: str | None = None):
        self.bridge_url = (
            bridge_url
            or os.environ.get("WHATSAPP_BRIDGE_URL", _BRIDGE_URL_DEFAULT)
        )
        self.my_number = (
            my_number
            or os.environ.get("MY_WHATSAPP_NUMBER", "")
        )

    # ------------------------------------------------------------------

    def send(self, message: str, to: str | None = None) -> bool:
        """
        Bridge üzerinden WhatsApp mesajı gönderir.
        `to` verilmezse MY_WHATSAPP_NUMBER kullanılır.
        """
        target = to or self.my_number
        if not target:
            logger.error("Hedef numara bilinmiyor -- MY_WHATSAPP_NUMBER boş.")
            return False

        try:
            r = requests.post(
                f"{self.bridge_url}/send",
                json={"to": target, "message": message},
                timeout=10,
            )
            if r.status_code == 200:
                logger.info(f"WA gönderildi → {target}: {message[:60]}")
                return True
            logger.warning(f"Bridge yanıtı {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.ConnectionError:
            logger.warning("WhatsApp bridge erişilemedi -- mesaj atlandı.")
            return False
        except Exception as e:
            logger.error(f"WA gönderim hatası: {e}")
            return False

    def is_ready(self) -> bool:
        """Bridge'in WhatsApp'a bağlı olup olmadığını kontrol eder."""
        try:
            r = requests.get(f"{self.bridge_url}/status", timeout=5)
            return r.json().get("ready", False)
        except Exception:
            return False
