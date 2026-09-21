"""Targeted Windows SMTC media controls; never fall back to a global key."""
import asyncio
import os
import time
import uuid
from urllib.parse import quote


class WindowsMedia:
    async def sessions(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        return list(manager.get_sessions())

    def state(self, session):
        return session.get_playback_info().playback_status.name.lower()

    async def command(self, session, action):
        method = {"play": "try_play_async", "pause": "try_pause_async",
                  "next": "try_skip_next_async", "previous": "try_skip_previous_async"}[action]
        return await getattr(session, method)()


class MediaController:
    def __init__(self, backend=None):
        self.backend = backend or WindowsMedia()
        self.snapshot = {}

    def run(self, operation, arguments):
        try:
            return asyncio.run(asyncio.wait_for(self._run(operation, arguments), 3))
        except ImportError:
            return {"ok": False, "error": "Windows media dependencies unavailable; install requirements.txt"}
        except TimeoutError:
            return {"ok": False, "outcome": "unknown", "error": "Media request timed out; do not retry automatically"}

    async def _run(self, operation, arguments):
        if operation == "list":
            sessions = await self.backend.sessions()
            query = arguments.get("app_name", "").casefold()
            self.snapshot = {uuid.uuid4().hex: (s, time.monotonic() + 120)
                             for s in sessions[:32] if query in s.source_app_user_model_id.casefold()}
            return {
                "ok": True,
                "sessions": [
                    {
                        "session_id": token,
                        "app_id": s.source_app_user_model_id,
                        "playback_state": self.backend.state(s),
                    }
                    for token, (s, _) in self.snapshot.items()
                ],
                "selection_ttl_seconds": 120,
                "track_titles_collected": False,
            }
        saved = self.snapshot.get(arguments["session_id"])
        if not saved or saved[1] < time.monotonic():
            return {"ok": False, "error": "Media selection expired; list sessions again"}
        session = saved[0]
        app_id = session.source_app_user_model_id
        if arguments["app_id"] != app_id:
            return {"ok": False, "error": "Media application mismatch"}
        current = await self.backend.sessions()
        if sum(s.source_app_user_model_id == app_id for s in current) != 1:
            return {"ok": False, "error": "Media application missing or ambiguous; not executed"}
        action = arguments["action"]
        expected = {"play": "playing", "pause": "paused"}.get(action)
        if expected and self.backend.state(session) == expected:
            return {
                "ok": True,
                "status": "already_in_requested_state",
                "playback_state": expected,
                "command_sent": False,
            }
        accepted = await self.backend.command(session, action)
        if not accepted:
            return {"ok": False, "error": "Application rejected or does not support this media command"}
        observed = self.backend.state(session)
        if expected:
            for _ in range(4):
                if observed == expected:
                    break
                await asyncio.sleep(0.1)
                observed = self.backend.state(session)
        verified = expected is not None and observed == expected
        return {
            "ok": True,
            "status": (
                "state_verified"
                if verified
                else "command_accepted_unverified"
            ),
            "command_accepted": True,
            "state_verified": verified,
            "playback_state": observed,
        }


def spotify_search(arguments):
    query = arguments["query"].strip()
    if not query or any(ord(c) < 32 for c in query):
        return {"ok": False, "error": "Search text must be non-empty and have no control characters"}
    os.startfile("https://open.spotify.com/search/" + quote(query, safe=""))
    return {
        "ok": True,
        "status": "search_opened_in_browser",
        "track_selected": False,
        "playback_started": False,
    }


def register_media_tools(registry):
    from .tools import ToolSpec
    controller = MediaController()
    device = {"device_id": {"type": "string"}}
    def schema(properties, required=()):
        return {"type": "object", "properties": {**properties, **device}, "required": list(required), "additionalProperties": False}
    registry.register(ToolSpec("list_media_sessions", "Find Windows media sessions by application (e.g. spotify); returns exact app/session IDs and playback state, never song titles.",
        schema({"app_name": {"type": "string", "maxLength": 100}}), "media.sessions", "yellow", True,
        confirmation_notice="Medya uygulamalarının adları ve oynatma durumları model API'sine gönderilecek; parça başlıkları okunmaz."),
        lambda args: controller.run("list", args))
    registry.register(ToolSpec("control_media_session", "Play, pause, next or previous ONLY in an exact session returned by list_media_sessions. Never guess IDs or fall back to global toggle. Report state_verified truthfully.",
        schema({"session_id": {"type": "string", "maxLength": 32}, "app_id": {"type": "string", "maxLength": 512},
                "action": {"type": "string", "enum": ["play", "pause", "next", "previous"]}}, ("session_id", "app_id", "action")),
        "media.control"), lambda args: controller.run("control", args))
    registry.register(ToolSpec("search_spotify", "Open Spotify web search in the browser. Does NOT select a track or start playback. Use for named-song requests and disclose this limit.",
        schema({"query": {"type": "string", "maxLength": 200}}, ("query",)), "media.spotify_search", "yellow", True,
        confirmation_notice="Arama metni Spotify'a gönderilecek; parça seçilmez ve otomatik çalınmaz."), spotify_search)
