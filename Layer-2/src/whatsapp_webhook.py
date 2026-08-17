#!/usr/bin/env python3
"""
EPIS -- WhatsApp Webhook Sunucusu
==================================
whatsapp_bridge (Node.js) buraya POST gönderir.
EPIS mesajı işler, yanıtı döner.
Kairos bildirimleri whatsapp_client.py üzerinden bridge'e gider.

Çalıştırma (Hetzner'de):
  uvicorn whatsapp_webhook:app --host 0.0.0.0 --port 8000

Gerekli bağımlılıklar: fastapi, uvicorn[standard], requests
"""

import os
import sys
import json
import logging
import secrets
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT  = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
sys.path.insert(0, THIS_DIR)

load_dotenv(dotenv_path=os.path.join(EPIS_ROOT, "Layer-3", "keys.env"))

from epis_core import build_system_prompt, Layer1Engine
from router import EpisRouter
from memory_manager import MemoryManager
from context_builder import ContextBuilder
from whatsapp_client import WhatsAppClient
from proactive_delivery import format_kairos_epis_message

MEMORY_DIR   = os.path.join(EPIS_ROOT, "Layer-1", "memory")
PENDING_PATH = os.path.join(MEMORY_DIR, "pending.json")
WEBHOOK_SHARED_SECRET = (os.getenv("WEBHOOK_SHARED_SECRET") or "").strip()

# ---------------------------------------------------------------------------
# Loglama
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - WEBHOOK - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(EPIS_ROOT, "epis.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("EPIS.WEBHOOK")

# ---------------------------------------------------------------------------
# Tek Kullanıcılı Oturum Durumu
# ---------------------------------------------------------------------------
SESSION_TIMEOUT_MINUTES = 30


class EPISSession:
    """kullanıcı ile süregelen tek oturumu yönetir."""

    def __init__(self):
        self.session_log: list[str]         = []
        self.last_activity: datetime | None = None
        self.engine: Layer1Engine | None    = None

    def is_expired(self) -> bool:
        if self.last_activity is None:
            return True
        delta = (datetime.now() - self.last_activity).total_seconds() / 60
        return delta > SESSION_TIMEOUT_MINUTES

    def touch(self):
        self.last_activity = datetime.now()


# ---------------------------------------------------------------------------
# Uygulama Başlangıcı
# ---------------------------------------------------------------------------
app     = FastAPI(title="EPIS WhatsApp Webhook")
session = EPISSession()
router_instance: EpisRouter | None  = None
memory:          MemoryManager | None = None


@app.on_event("startup")
async def startup():
    global router_instance, memory, session

    logger.info("EPIS WhatsApp Webhook başlatılıyor...")

    try:
        router_instance = EpisRouter()
        memory          = MemoryManager()
    except Exception as e:
        logger.error(f"Router/Memory hatası: {e}")
        return

    health          = router_instance.api_client.health_check()
    system_prompt   = build_system_prompt()
    context_builder = ContextBuilder(memory)

    try:
        session.engine = Layer1Engine(
            system_prompt,
            gemini_key=router_instance.api_client.gemini_key,
            context_provider=context_builder.build,
        )
    except Exception as e:
        logger.error(f"Layer-1 engine baslatilmadi: {e}")
        return

    # Gemini backend Layer-1 icin Gemini'ye baglidir.
    if session.engine.backend_name == "gemini" and health.get("gemini") != "OK":
        logger.error("Gemini API erişilemiyor -- Layer-1 (gemini) icin gerekli. Webhook hazir degil.")
        session.engine = None
        return

    if session.engine.backend_name == "qwen":
        try:
            session.engine.backend.generate("Saglik kontrolu. Tek kelime: tamam", [])
        except Exception as e:
            logger.error(f"Qwen erisilemiyor -- Ollama acik mi? {e}")
            session.engine = None
            return

    logger.info("EPIS WhatsApp Webhook hazır.")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _check_webhook_auth(request: Request) -> None:
    """
    Bridge ile paylasilan secret.
    Header: Authorization: Bearer <WEBHOOK_SHARED_SECRET>
    veya X-EPIS-Webhook-Secret: <secret>
    Secret yoksa (gelistirme) istek kabul edilir ama uyarilir.
    """
    if not WEBHOOK_SHARED_SECRET:
        logger.warning(
            "WEBHOOK_SHARED_SECRET bos — webhook auth KAPALI. "
            "keys.env'e ekle; bridge'e ayni degeri ver."
        )
        return

    auth = (request.headers.get("authorization") or "").strip()
    alt  = (request.headers.get("x-epis-webhook-secret") or "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif alt:
        token = alt

    if not token or not secrets.compare_digest(token, WEBHOOK_SHARED_SECRET):
        logger.warning("Webhook auth basarisiz (IP/headers)")
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Ana Endpoint
# ---------------------------------------------------------------------------
@app.post("/whatsapp/incoming")
async def incoming(request: Request):
    _check_webhook_auth(request)

    if session.engine is None:
        raise HTTPException(status_code=503, detail="EPIS başlatılamadı.")

    data       = await request.json()
    user_input = data.get("body", "").strip()
    if not user_input:
        return JSONResponse({"reply": None})

    logger.info(f"Gelen mesaj: {user_input[:80]}")

    # Oturum süresi dolmuşsa sıfırla
    if session.is_expired():
        _close_session()
        session.engine.reset()
        session.session_log.clear()
        memory.update_current_state({"session_active": True})
        _deliver_pending_whatsapp(send_via_wa=True)

    session.touch()

    # Layer-1 çağrısı
    l1_response = session.engine.send(user_input)

    if l1_response.get("type") == "tool_call":
        task_type  = l1_response.get("task_type", "fast_tasks")
        payload    = l1_response.get("payload", user_input)
        bridge_msg = l1_response.get("bridge_message", "Bakıyorum...")

        logger.info(f"Tool call: {task_type}")
        tool_call_data = {"task_type": task_type, "payload": payload}
        router_result  = router_instance.intercept_tool_call(
            tool_call_data=tool_call_data,
            current_context={},
        )
        result_text  = router_result.get("result", "")
        model_used   = router_result.get("model_used", "")

        followup = (
            f"[GOREV SONUCU -- {task_type} / {model_used}]\n"
            f"{result_text}\n\n"
            "Yukaridaki sonucu kendi sesinde, EPIS olarak kullanıcıya ilet."
        )
        l1_final     = session.engine.send(followup)
        epis_message = l1_final.get("message", result_text)

        memory.log_interaction(
            event_type="tool_call",
            raw_text=user_input,
            observation="",
            tags=[task_type, model_used],
        )
    else:
        epis_message = l1_response.get("message", "")

    session.session_log.append(f"kullanıcı: {user_input}")
    session.session_log.append(f"EPIS: {epis_message}")
    memory.update_current_state({
        "last_interaction": user_input[:200],
        "last_message_at": datetime.now().isoformat(),
    })

    logger.info(f"Yanıt: {epis_message[:80]}")
    return JSONResponse({"reply": epis_message})


# ---------------------------------------------------------------------------
# Durum Endpoint
# ---------------------------------------------------------------------------
@app.get("/status")
async def status():
    return {
        "epis_ready":       session.engine is not None,
        "backend":          session.engine.backend_name if session.engine else None,
        "model":            session.engine.model if session.engine else None,
        "session_active":   not session.is_expired(),
        "session_messages": len(session.session_log),
    }


# ---------------------------------------------------------------------------
# Yardımcı Fonksiyonlar
# ---------------------------------------------------------------------------

def _close_session():
    """Süre dolan oturumu lifetime.db'ye yazar."""
    if session.session_log and memory:
        memory.log_interaction(
            event_type="session",
            raw_text="\n".join(session.session_log),
            observation="",
        )
        memory.update_current_state({"session_active": False})
        logger.info("Oturum kapatıldı ve kaydedildi.")


def _deliver_pending_whatsapp(send_via_wa: bool = False) -> list[str]:
    """
    pending.json'daki öğeleri EPIS'in Layer-1'inden geçirip
    istenirse WhatsApp'a gönderir.
    """
    if not os.path.exists(PENDING_PATH):
        return []
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    delivered = False
    sent_msgs = []
    wa = WhatsAppClient() if send_via_wa else None

    for item in data.get("items", []):
        if item.get("status") != "pending":
            continue

        context = item.get("context", item.get("message", ""))
        t_type  = item.get("trigger_type", "")

        if session.engine:
            epis_msg = format_kairos_epis_message(session.engine, t_type, context)
        else:
            epis_msg = context

        sent_msgs.append(epis_msg)
        if wa and wa.is_ready():
            wa.send(epis_msg)

        item["status"]       = "delivered"
        item["delivered_at"] = datetime.now().isoformat()
        delivered = True

    if delivered:
        data["last_updated"] = datetime.now().isoformat()
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return sent_msgs


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("whatsapp_webhook:app", host="0.0.0.0", port=8000, reload=False)
