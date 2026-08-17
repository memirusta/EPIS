#!/usr/bin/env python3
"""
EPIS -- Web Sohbet Arayuzu
==========================
Tarayicida EPIS ile konusma + Kairos anlik push (SSE).

Calistirma: uvicorn epis_chat:app --host 127.0.0.1 --port 8080
(Varsayilan sadece localhost; LAN icin EPIS_UI_HOST=0.0.0.0 + EPIS_UI_SHARED_SECRET)
"""

import os
import sys
import json
import asyncio
import logging
import atexit
import secrets
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT  = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
STATIC_DIR = os.path.join(THIS_DIR, "static")
MEMORY_DIR = os.path.join(EPIS_ROOT, "Layer-1", "memory")
PENDING_PATH = os.path.join(MEMORY_DIR, "pending.json")
SESSION_BUFFER_PATH = os.path.join(MEMORY_DIR, "session_buffer.json")
sys.path.insert(0, THIS_DIR)

load_dotenv(os.path.join(EPIS_ROOT, "Layer-3", "keys.env"))

from epis_core import build_system_prompt, Layer1Engine
from router import EpisRouter
from memory_manager import MemoryManager
from context_builder import ContextBuilder
from proactive_delivery import format_kairos_epis_message
from band_client import BandClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - EPIS.UI - %(message)s",
)
logger = logging.getLogger("EPIS.UI")

# UI secret: ayri degisken yoksa webhook secret ile ayni (tek sir)
EPIS_UI_SHARED_SECRET = (
    (os.getenv("EPIS_UI_SHARED_SECRET") or "").strip()
    or (os.getenv("WEBHOOK_SHARED_SECRET") or "").strip()
)
EPIS_UI_HOST = (os.getenv("EPIS_UI_HOST") or "127.0.0.1").strip()

app = FastAPI(title="EPIS Chat")


def _extract_ui_token(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    alt = (request.headers.get("x-epis-ui-secret") or "").strip()
    if alt:
        return alt
    # EventSource header gonderemez
    return (request.query_params.get("token") or "").strip()


@app.middleware("http")
async def ui_auth_middleware(request: Request, call_next):
    """
    /api/* korumasi. Secret bossa (gelistirme) acik kalir ama uyarir.
    HTML/static serbest; tarayici JS secret'i inject edilen meta ile gonderir.
    """
    path = request.url.path or ""
    if path.startswith("/api/"):
        if not EPIS_UI_SHARED_SECRET:
            if path == "/api/status":
                logger.warning(
                    "EPIS_UI_SHARED_SECRET/WEBHOOK_SHARED_SECRET bos — UI API auth KAPALI"
                )
        else:
            token = _extract_ui_token(request)
            if not token or not secrets.compare_digest(token, EPIS_UI_SHARED_SECRET):
                logger.warning("UI API auth basarisiz path=%s", path)
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

# SSE aboneleri
_event_queues: list[asyncio.Queue] = []
_router: EpisRouter | None = None
_memory: MemoryManager | None = None
_engine: Layer1Engine | None = None
_session_log: list[str] = []
_ready = False
_shutting_down = False
_band = BandClient()
_last_activity: datetime | None = None
SESSION_TIMEOUT_MINUTES = 30
_exit_cleanup_done = False


def _persist_session_buffer():
    """Terminal ani kapanirsa oturum kurtarilsin."""
    try:
        if not _session_log:
            if os.path.exists(SESSION_BUFFER_PATH):
                os.remove(SESSION_BUFFER_PATH)
            return
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(SESSION_BUFFER_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "lines":      _session_log,
                "updated_at": datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"session_buffer yazilamadi: {e}")


def _recover_crashed_session():
    """
    Onceki UI duzgun kapanmadiysa buffer'daki oturumu DB'ye yaz.
    Normal yeniden baslatmada buffer KORUNUR (_restore_live_session).
    """
    if not _memory or not os.path.exists(SESSION_BUFFER_PATH):
        return
    try:
        with open(SESSION_BUFFER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        updated = data.get("updated_at") or ""
        lines = data.get("lines") or []
        if not lines:
            os.remove(SESSION_BUFFER_PATH)
            return
        # Yalnizca cok eski buffer (muhtemel gercek crash) DB'ye arsivlenir
        try:
            age_h = (datetime.now() - datetime.fromisoformat(updated)).total_seconds() / 3600
        except Exception:
            age_h = 0
        if age_h >= 6:
            _memory.log_interaction(
                event_type="session",
                raw_text="\n".join(lines),
                observation="",
            )
            logger.info(f"Eski buffer arsivlendi ({len(lines)} satir, {age_h:.1f} saat once)")
            os.remove(SESSION_BUFFER_PATH)
    except Exception as e:
        logger.warning(f"Oturum kurtarma hatasi: {e}")


def _restore_live_session():
    """Buffer + engine history — sunucu yeniden baslasa bile konusma surer."""
    global _session_log
    if not _memory or not _engine:
        return
    lines = _memory.get_session_buffer_lines()
    if not lines:
        return
    _session_log = list(lines)
    msgs = MemoryManager.parse_conversation_text("\n".join(_session_log))
    n = _engine.load_history(msgs)
    if n:
        logger.info(f"Canli oturum geri yuklendi ({n} tur, {len(_session_log)} satir)")
        _memory.update_current_state({"session_active": True})


def _flush_session_to_db():
    """Oturum gunlugunu lifetime.db'ye yazar (nightly analiz icin)."""
    global _session_log
    if not _memory or not _session_log:
        return
    _memory.log_interaction(
        event_type="session",
        raw_text="\n".join(_session_log),
        observation="",
    )
    logger.info(f"Oturum kaydedildi ({len(_session_log)} satir)")
    _session_log = []
    _persist_session_buffer()


def _close_browser_session():
    """Tarayici sekmesi kapandiginda oturumu DB'ye yazar."""
    global _last_activity
    _flush_session_to_db()
    if _engine:
        _engine.reset()
    if _memory:
        _memory.update_current_state({"session_active": False})
    _last_activity = datetime.now()
    logger.info("Sekme kapandi — oturum kaydedildi.")


def _on_server_stop():
    """Sunucu kapanirken DB'ye yazma; buffer sonraki acilista kurtarilir."""
    global _exit_cleanup_done
    if _exit_cleanup_done:
        return
    _exit_cleanup_done = True
    _persist_session_buffer()
    logger.info("EPIS sunucu durdu — oturum yalnizca buffer'da (sekme kapanmadiysa kurtarilir).")


atexit.register(_on_server_stop)


def _check_session_expiry():
    """Uzun sure mesaj yoksa oturumu kapat ve hafizaya yaz."""
    global _last_activity
    if _last_activity is None:
        return
    idle_min = (datetime.now() - _last_activity).total_seconds() / 60
    if idle_min <= SESSION_TIMEOUT_MINUTES:
        return
    _flush_session_to_db()
    if _engine:
        _engine.reset()
    if _memory:
        _memory.update_current_state({"session_active": False})
    logger.info("Oturum zaman asimi — sifirlandi")


class ChatIn(BaseModel):
    message: str


class PushIn(BaseModel):
    message: str
    trigger_type: str = ""
    priority: str = "medium"


def _broadcast(event: dict):
    for q in list(_event_queues):
        try:
            q.put_nowait(event)
        except Exception:
            pass


def _process_user_message(user_input: str) -> dict:
    global _session_log, _last_activity
    _check_session_expiry()
    _last_activity = datetime.now()

    l1 = _engine.send(user_input)

    if l1.get("type") == "tool_call":
        task_type  = l1.get("task_type", "fast_tasks")
        payload    = l1.get("payload", user_input)
        bridge_msg = l1.get("bridge_message", "Bakiyorum...")
        _broadcast({"type": "typing", "message": bridge_msg})

        router_result = _router.intercept_tool_call(
            tool_call_data={"task_type": task_type, "payload": payload},
            current_context={},
        )
        result_text = router_result.get("result", "")
        model_used  = router_result.get("model_used", "")
        followup = (
            f"[GOREV SONUCU -- {task_type} / {model_used}]\n"
            f"{result_text}\n\n"
            "Yukaridaki sonucu kendi sesinde, EPIS olarak kullanıcıya ilet."
        )
        l1_final     = _engine.send(followup)
        epis_message = l1_final.get("message", result_text)
        _memory.log_interaction(
            event_type="tool_call",
            raw_text=user_input,
            observation="",
            tags=[task_type, model_used],
        )
    else:
        epis_message = l1.get("message", "")

    _session_log.append(f"kullanıcı: {user_input}")
    _session_log.append(f"EPIS: {epis_message}")
    _persist_session_buffer()
    _memory.update_current_state({
        "last_interaction": user_input[:200],
        "last_message_at":  datetime.now().isoformat(),
        "session_active":   True,
    })
    if epis_message.strip():
        _band.notify(epis_message, respect_quiet_hours=False)
    return {"reply": epis_message}


def _deliver_pending_to_ui():
    if not os.path.exists(PENDING_PATH) or not _engine:
        return
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    delivered = False
    for item in data.get("items", []):
        if item.get("status") != "pending":
            continue
        context  = item.get("context", item.get("message", ""))
        t_type   = item.get("trigger_type", "")
        priority = item.get("priority", "medium")
        epis_msg = format_kairos_epis_message(_engine, t_type, context)
        _broadcast({
            "type":         "proactive",
            "message":      epis_msg,
            "trigger_type": t_type,
            "priority":     priority,
        })
        _band.notify(epis_msg, respect_quiet_hours=True)
        item["status"]       = "delivered"
        item["delivered_at"] = datetime.now().isoformat()
        item["delivery"]     = "ui_bootstrap"
        item["epis_message"] = epis_msg
        delivered = True

    if delivered:
        data["last_updated"] = datetime.now().isoformat()
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


@app.on_event("startup")
async def startup():
    global _router, _memory, _engine, _ready

    logger.info("EPIS UI baslatiliyor...")
    try:
        _router = EpisRouter()
        _memory = MemoryManager()
        _recover_crashed_session()

        def _live_lines():
            return list(_session_log)

        ctx     = ContextBuilder(_memory, live_session_provider=_live_lines)
        _engine = Layer1Engine(
            build_system_prompt(),
            gemini_key=_router.api_client.gemini_key,
            context_provider=ctx.build,
        )
        _restore_live_session()
    except Exception as e:
        logger.error(f"Baslatma hatasi: {e}")
        return

    if _engine.backend_name == "qwen":
        try:
            _engine.backend.generate("ping", [])
        except Exception as e:
            logger.error(f"Qwen erisilemiyor: {e}")
            return

    _memory.update_current_state({"session_active": False})
    global _last_activity
    _last_activity = datetime.now()
    _deliver_pending_to_ui()
    _ready = True
    logger.info(f"EPIS UI hazir -- {_engine.backend_name} / {_engine.model}")


@app.on_event("shutdown")
async def shutdown():
    global _shutting_down
    _shutting_down = True
    for q in list(_event_queues):
        try:
            q.put_nowait(None)
        except Exception:
            pass
    _on_server_stop()


@app.get("/")
async def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(404, "index.html yok")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    # Tarayici fetch/EventSource icin secret (sadece 127.0.0.1 dinliyorsa LAN riski yok)
    inj = EPIS_UI_SHARED_SECRET.replace("\\", "\\\\").replace("'", "\\'")
    html = html.replace(
        "/*__EPIS_UI_SECRET__*/",
        f"window.__EPIS_UI_SECRET__ = '{inj}';",
        1,
    )
    return HTMLResponse(html)


@app.get("/api/status")
async def status():
    return {
        "ready":   _ready,
        "backend": _engine.backend_name if _engine else None,
        "model":   _engine.model if _engine else None,
    }


@app.get("/api/sessions")
async def list_sessions():
    if not _memory:
        raise HTTPException(503, "Hafiza hazir degil")
    items = _memory.list_chat_sessions(limit=50)
    # Bellekteki canli oturum DB'den daha yeni olabilir
    if _session_log:
        live_msgs = MemoryManager.parse_conversation_text("\n".join(_session_log))
        if live_msgs:
            live_item = {
                "id":        "live",
                "db_id":     None,
                "timestamp": _memory.get_current_state().get("last_message_at", ""),
                "title":     MemoryManager._session_title(live_msgs),
                "preview":   next((m["text"] for m in reversed(live_msgs) if m["role"] == "epis"), "")[:80],
                "count":     len(live_msgs),
                "live":      True,
            }
            if items and items[0].get("live"):
                items[0] = live_item
            else:
                items.insert(0, live_item)
    return {"sessions": items}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    if not _memory:
        raise HTTPException(503, "Hafiza hazir degil")
    if session_id == "live":
        msgs = MemoryManager.parse_conversation_text("\n".join(_session_log))
        return {
            "id":       "live",
            "live":     True,
            "title":    MemoryManager._session_title(msgs) if msgs else "EPIS",
            "messages": msgs,
        }
    try:
        sid = int(session_id)
    except ValueError:
        raise HTTPException(400, "Gecersiz oturum id")
    data = _memory.get_session_messages(sid)
    if not data:
        raise HTTPException(404, "Oturum bulunamadi")
    return data


@app.post("/api/sessions/close")
async def close_browser_session():
    """Tarayici sekmesi kapaninca — oturumu kaydet."""
    if not _ready:
        return {"ok": True, "saved": False}
    had = bool(_session_log)
    await asyncio.to_thread(_close_browser_session)
    return {"ok": True, "saved": had}


@app.post("/api/sessions/new")
async def new_session():
    if not _ready:
        raise HTTPException(503, "EPIS hazir degil")
    await asyncio.to_thread(_close_browser_session)
    global _last_activity
    _last_activity = datetime.now()
    if _memory:
        _memory.update_current_state({"session_active": True})
    return {"ok": True, "id": "live"}


@app.post("/api/chat")
async def chat(body: ChatIn):
    if not _ready or not _engine:
        raise HTTPException(503, "EPIS hazir degil")
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "Bos mesaj")
    try:
        result = await asyncio.to_thread(_process_user_message, text)
        return result
    except Exception as e:
        logger.error(f"Chat hatasi: {e}")
        raise HTTPException(500, str(e))


@app.post("/api/push")
async def push(body: PushIn):
    if not body.message.strip():
        raise HTTPException(400, "Bos mesaj")
    _broadcast({
        "type":         "proactive",
        "message":      body.message.strip(),
        "trigger_type": body.trigger_type,
        "priority":     body.priority,
        "at":           datetime.now().isoformat(),
    })
    return {"ok": True}


@app.get("/api/events")
async def events():
    if not _ready:
        raise HTTPException(503, "EPIS hazir degil")

    async def stream():
        q = asyncio.Queue()
        _event_queues.append(q)
        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while not _shutting_down:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None or _shutting_down:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if q in _event_queues:
                _event_queues.remove(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("EPIS_UI_PORT", "8080"))
    host = EPIS_UI_HOST or "127.0.0.1"
    logger.info("EPIS UI bind %s:%s (auth=%s)", host, port, "on" if EPIS_UI_SHARED_SECRET else "off")
    uvicorn.run("epis_chat:app", host=host, port=port, reload=False)
