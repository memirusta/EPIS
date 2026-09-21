"""Spotify Web API tools using Authorization Code with PKCE.

No client secret is stored. OAuth tokens are encrypted with Windows DPAPI and
stay on the device agent. Search/device selections are exposed to Luna only as
short-lived opaque references.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import uuid

import requests


ACCOUNTS_AUTHORIZE = "https://accounts.spotify.com/authorize"
ACCOUNTS_TOKEN = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
SCOPES = (
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
)
SELECTION_TTL_SECONDS = 120
AUTH_TTL_SECONDS = 300


class SpotifyTokenStore:
    """Device-local DPAPI-protected OAuth token storage."""

    def __init__(self, path: str | None = None):
        root = Path(os.getenv("LOCALAPPDATA") or Path.home())
        self.path = Path(path) if path else root / "EPIS" / "spotify-oauth.bin"

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            import win32crypt
            encrypted = self.path.read_bytes()
            _, plain = win32crypt.CryptUnprotectData(
                encrypted, None, None, None, 0
            )
            value = json.loads(plain.decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def save(self, value: dict) -> None:
        import win32crypt
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        _, encrypted = win32crypt.CryptProtectData(
            payload,
            "EPIS Spotify OAuth",
            None,
            None,
            None,
            0,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(encrypted)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class PendingAuthorization:
    state: str
    verifier: str
    redirect_uri: str
    expires_at: float
    callback: dict | None = None


class SpotifyController:
    def __init__(
        self,
        *,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        token_store=None,
        session=None,
        opener=None,
        clock=None,
    ):
        self.client_id = (
            client_id
            if client_id is not None
            else os.getenv("EPIS_SPOTIFY_CLIENT_ID", "")
        ).strip()
        self.redirect_uri = (
            redirect_uri
            if redirect_uri is not None
            else os.getenv(
                "EPIS_SPOTIFY_REDIRECT_URI",
                DEFAULT_REDIRECT_URI,
            )
        ).strip()
        self.store = token_store or SpotifyTokenStore()
        self.session = session or requests.Session()
        self.opener = opener or os.startfile
        self.clock = clock or time.time
        self.pending_auth: PendingAuthorization | None = None
        self.track_refs: dict[str, tuple[float, dict]] = {}
        self.device_refs: dict[str, tuple[float, dict]] = {}

    def _now(self) -> float:
        return float(self.clock())

    def _prune(self) -> None:
        now = self._now()
        self.track_refs = {
            key: value
            for key, value in self.track_refs.items()
            if value[0] > now
        }
        self.device_refs = {
            key: value
            for key, value in self.device_refs.items()
            if value[0] > now
        }

    def _validate_redirect_uri(self) -> tuple[str, int, str] | None:
        try:
            parsed = urlparse(self.redirect_uri)
        except ValueError:
            return None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
        ):
            return None
        return parsed.hostname, parsed.port, parsed.path or "/"

    def _callback_handler(self, pending: PendingAuthorization, path: str):
        controller = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != path:
                    self.send_response(404)
                    self.end_headers()
                    return
                values = parse_qs(parsed.query)
                state = (values.get("state") or [""])[0]
                code = (values.get("code") or [""])[0]
                error = (values.get("error") or [""])[0]
                if state != pending.state:
                    pending.callback = {"error": "state_mismatch"}
                elif error:
                    pending.callback = {"error": error}
                elif code:
                    pending.callback = {"code": code}
                else:
                    pending.callback = {"error": "missing_code"}

                body = (
                    b"EPIS Spotify authorization received. "
                    b"You can return to EPIS."
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        return Handler

    def begin_auth(self, _arguments: dict) -> dict:
        if not self.client_id:
            return {
                "ok": False,
                "error": "spotify_client_id_missing",
                "setup_required": True,
            }

        endpoint = self._validate_redirect_uri()
        if endpoint is None:
            return {
                "ok": False,
                "error": "invalid_spotify_redirect_uri",
            }

        now = self._now()
        if (
            self.pending_auth is not None
            and self.pending_auth.expires_at > now
            and self.pending_auth.callback is None
        ):
            return {
                "ok": True,
                "status": "authorization_in_progress",
                "redirect_uri": self.pending_auth.redirect_uri,
            }

        host, port, path = endpoint
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        pending = PendingAuthorization(
            state=secrets.token_urlsafe(24),
            verifier=verifier,
            redirect_uri=self.redirect_uri,
            expires_at=now + AUTH_TTL_SECONDS,
        )

        try:
            server = HTTPServer(
                (host, port),
                self._callback_handler(pending, path),
            )
            server.timeout = AUTH_TTL_SECONDS
        except OSError:
            return {
                "ok": False,
                "error": "spotify_callback_port_unavailable",
            }

        def wait_for_callback():
            try:
                server.handle_request()
            finally:
                server.server_close()

        threading.Thread(
            target=wait_for_callback,
            name="epis-spotify-oauth",
            daemon=True,
        ).start()

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": pending.state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        self.pending_auth = pending
        self.opener(f"{ACCOUNTS_AUTHORIZE}?{urlencode(params)}")
        return {
            "ok": True,
            "status": "authorization_requested",
            "redirect_uri": self.redirect_uri,
            "scopes": list(SCOPES),
        }

    def _token_payload(self, response) -> dict | None:
        if not 200 <= response.status_code < 300:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            return None
        payload = dict(payload)
        payload["expires_at"] = (
            self._now() + int(payload.get("expires_in", 3600)) - 30
        )
        return payload

    def auth_status(self, _arguments: dict) -> dict:
        token = self.store.load()
        if token and (
            token.get("refresh_token")
            or float(token.get("expires_at", 0)) > self._now()
        ):
            refreshed = self._access_token()
            if refreshed:
                return {"ok": True, "status": "connected"}

        pending = self.pending_auth
        if pending is None:
            return {
                "ok": True,
                "status": (
                    "setup_required"
                    if not self.client_id
                    else "not_connected"
                ),
                "client_id_configured": bool(self.client_id),
            }
        if pending.expires_at <= self._now():
            self.pending_auth = None
            return {
                "ok": False,
                "error": "spotify_authorization_expired",
            }
        if pending.callback is None:
            return {
                "ok": True,
                "status": "awaiting_user_authorization",
            }
        callback = pending.callback
        self.pending_auth = None
        if callback.get("error"):
            return {
                "ok": False,
                "error": f"spotify_authorization_{callback['error']}",
            }

        try:
            response = self.session.post(
                ACCOUNTS_TOKEN,
                data={
                    "client_id": self.client_id,
                    "grant_type": "authorization_code",
                    "code": callback["code"],
                    "redirect_uri": pending.redirect_uri,
                    "code_verifier": pending.verifier,
                },
                timeout=10,
            )
        except requests.RequestException:
            return {
                "ok": False,
                "error": "spotify_token_exchange_failed",
            }

        payload = self._token_payload(response)
        if payload is None:
            return {
                "ok": False,
                "error": f"spotify_token_http_{response.status_code}",
            }
        self.store.save(payload)
        return {"ok": True, "status": "connected"}

    def _refresh(self, token: dict) -> dict | None:
        refresh_token = token.get("refresh_token")
        if not refresh_token or not self.client_id:
            return None
        try:
            response = self.session.post(
                ACCOUNTS_TOKEN,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                },
                timeout=10,
            )
        except requests.RequestException:
            return None
        payload = self._token_payload(response)
        if payload is None:
            return None
        payload.setdefault("refresh_token", refresh_token)
        self.store.save(payload)
        return payload

    def _access_token(self) -> str | None:
        token = self.store.load()
        if not token:
            return None
        if float(token.get("expires_at", 0)) <= self._now():
            token = self._refresh(token)
            if token is None:
                return None
        value = token.get("access_token")
        return value if isinstance(value, str) and value else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json_body=None,
    ):
        token = self._access_token()
        if not token:
            return None, {
                "ok": False,
                "error": "spotify_not_connected",
                "connect_required": True,
            }
        url = API_BASE + path
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except requests.RequestException:
            return None, {
                "ok": False,
                "error": "spotify_request_failed",
            }

        if response.status_code == 401:
            current = self.store.load() or {}
            refreshed = self._refresh(current)
            if refreshed:
                try:
                    response = self.session.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers={
                            "Authorization": (
                                f"Bearer {refreshed['access_token']}"
                            )
                        },
                        timeout=10,
                    )
                except requests.RequestException:
                    return None, {
                        "ok": False,
                        "error": "spotify_request_failed",
                    }

        if response.status_code == 204:
            return None, {"ok": True, "http_status": 204}
        if not 200 <= response.status_code < 300:
            result = {
                "ok": False,
                "error": f"spotify_http_{response.status_code}",
            }
            if response.status_code == 429:
                retry = response.headers.get("Retry-After")
                if retry and retry.isdigit():
                    result["retry_after_seconds"] = int(retry)
            return None, result
        try:
            data = response.json()
        except ValueError:
            return None, {
                "ok": False,
                "error": "spotify_invalid_response",
            }
        return data, None

    def search_tracks(self, arguments: dict) -> dict:
        query = arguments["query"].strip()
        limit = int(arguments.get("limit", 5))
        data, error = self._request(
            "GET",
            "/search",
            params={
                "q": query,
                "type": "track",
                "limit": limit,
            },
        )
        if error:
            return error
        items = ((data or {}).get("tracks") or {}).get("items") or []
        self._prune()
        results = []
        for item in items[:limit]:
            uri = item.get("uri")
            if not isinstance(uri, str) or not uri.startswith("spotify:track:"):
                continue
            ref = uuid.uuid4().hex
            public = {
                "track_ref": ref,
                "name": str(item.get("name") or ""),
                "artists": [
                    str(artist.get("name") or "")
                    for artist in item.get("artists") or []
                    if isinstance(artist, dict)
                ][:5],
                "album": str((item.get("album") or {}).get("name") or ""),
                "duration_ms": item.get("duration_ms"),
            }
            self.track_refs[ref] = (
                self._now() + SELECTION_TTL_SECONDS,
                {"uri": uri, "public": public},
            )
            results.append(public)
        return {
            "ok": True,
            "tracks": results,
            "selection_ttl_seconds": SELECTION_TTL_SECONDS,
        }

    def devices(self, _arguments: dict) -> dict:
        data, error = self._request("GET", "/me/player/devices")
        if error:
            return error
        self._prune()
        results = []
        for item in (data or {}).get("devices") or []:
            raw_id = item.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                continue
            ref = uuid.uuid4().hex
            public = {
                "device_ref": ref,
                "name": str(item.get("name") or ""),
                "type": str(item.get("type") or ""),
                "is_active": bool(item.get("is_active")),
                "volume_percent": item.get("volume_percent"),
            }
            self.device_refs[ref] = (
                self._now() + SELECTION_TTL_SECONDS,
                {"id": raw_id, "public": public},
            )
            results.append(public)
        return {
            "ok": True,
            "devices": results,
            "selection_ttl_seconds": SELECTION_TTL_SECONDS,
        }

    def current(self, _arguments: dict) -> dict:
        data, error = self._request("GET", "/me/player")
        if error:
            if error.get("http_status") == 204:
                return {"ok": True, "status": "no_active_playback"}
            return error
        if not data:
            return {"ok": True, "status": "no_active_playback"}
        item = data.get("item") or {}
        device = data.get("device") or {}
        return {
            "ok": True,
            "status": "playback_state_read",
            "is_playing": bool(data.get("is_playing")),
            "track": {
                "name": str(item.get("name") or ""),
                "artists": [
                    str(artist.get("name") or "")
                    for artist in item.get("artists") or []
                    if isinstance(artist, dict)
                ][:5],
            } if item else None,
            "device": {
                "name": str(device.get("name") or ""),
                "type": str(device.get("type") or ""),
            } if device else None,
        }

    def _resolve_track(self, ref: str) -> dict | None:
        self._prune()
        saved = self.track_refs.get(ref)
        return saved[1] if saved else None

    def _resolve_device(self, ref: str | None) -> dict | None:
        if ref is None:
            return {}
        self._prune()
        saved = self.device_refs.get(ref)
        return saved[1] if saved else None

    def play_track(self, arguments: dict) -> dict:
        track = self._resolve_track(arguments["track_ref"])
        if track is None:
            return {
                "ok": False,
                "error": "spotify_track_selection_expired",
            }
        device = self._resolve_device(arguments.get("device_ref"))
        if device is None:
            return {
                "ok": False,
                "error": "spotify_device_selection_expired",
            }
        params = {"device_id": device["id"]} if device.get("id") else None
        _, error = self._request(
            "PUT",
            "/me/player/play",
            params=params,
            json_body={"uris": [track["uri"]]},
        )
        if error and not error.get("ok"):
            return error

        verified = False
        observed = None
        for _ in range(4):
            time.sleep(0.25)
            data, read_error = self._request("GET", "/me/player")
            if read_error:
                break
            if not data:
                continue
            item = data.get("item") or {}
            observed = {
                "is_playing": bool(data.get("is_playing")),
                "uri": item.get("uri"),
            }
            if (
                observed["is_playing"]
                and observed["uri"] == track["uri"]
            ):
                verified = True
                break

        return {
            "ok": True,
            "status": (
                "playback_verified"
                if verified
                else "playback_requested_unverified"
            ),
            "track": track["public"],
            "state_verified": verified,
        }

    def pause(self, arguments: dict) -> dict:
        device = self._resolve_device(arguments.get("device_ref"))
        if device is None:
            return {
                "ok": False,
                "error": "spotify_device_selection_expired",
            }
        params = {"device_id": device["id"]} if device.get("id") else None
        _, error = self._request(
            "PUT",
            "/me/player/pause",
            params=params,
        )
        if error and not error.get("ok"):
            return error

        verified = False
        for _ in range(4):
            time.sleep(0.25)
            data, read_error = self._request("GET", "/me/player")
            if read_error:
                break
            if not data or data.get("is_playing") is False:
                verified = True
                break
        return {
            "ok": True,
            "status": (
                "pause_verified"
                if verified
                else "pause_requested_unverified"
            ),
            "state_verified": verified,
        }


def register_spotify_tools(registry):
    from .tools import ToolSpec

    controller = SpotifyController()
    device = {"device_id": {"type": "string"}}

    def schema(properties=None, required=()):
        return {
            "type": "object",
            "properties": {**(properties or {}), **device},
            "required": list(required),
            "additionalProperties": False,
        }

    registry.register(
        ToolSpec(
            "spotify_connect",
            (
                "Begin Spotify Authorization Code with PKCE on this device. "
                "Use when Spotify API tools report connect_required/setup_required."
            ),
            schema(),
            "spotify.auth",
            "yellow",
            True,
            confirmation_notice=(
                "Spotify yetkilendirme sayfası tarayıcıda açılacak. "
                "İzinleri Spotify ekranında sen vereceksin."
            ),
        ),
        controller.begin_auth,
    )
    registry.register(
        ToolSpec(
            "spotify_connection_status",
            (
                "Check whether Spotify OAuth is connected or whether the "
                "authorization callback is still pending."
            ),
            schema(),
            "spotify.auth_status",
            effects=("spotify.authenticated",),
        ),
        controller.auth_status,
    )
    registry.register(
        ToolSpec(
            "spotify_search_tracks",
            (
                "Search Spotify tracks by text. Returns short-lived opaque "
                "track_ref values plus track/artist/album metadata. Use these "
                "results before spotify_play_track; never invent track_ref."
            ),
            schema(
                {
                    "query": {"type": "string", "maxLength": 200},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                    },
                },
                ("query",),
            ),
            "spotify.search",
            "yellow",
            True,
            confirmation_notice=(
                "Arama metni Spotify API'sine gönderilecek."
            ),
            preconditions=("spotify.authenticated",),
        ),
        controller.search_tracks,
    )
    registry.register(
        ToolSpec(
            "spotify_devices",
            (
                "List Spotify Connect playback devices as short-lived opaque "
                "device_ref values. Raw Spotify device IDs stay local."
            ),
            schema(),
            "spotify.devices",
            preconditions=("spotify.authenticated",),
        ),
        controller.devices,
    )
    registry.register(
        ToolSpec(
            "spotify_current_playback",
            (
                "Read the current Spotify playback state, current track and "
                "active device after the user connected Spotify."
            ),
            schema(),
            "spotify.current",
            preconditions=("spotify.authenticated",),
        ),
        controller.current,
    )
    registry.register(
        ToolSpec(
            "spotify_play_track",
            (
                "Play an exact track_ref returned by spotify_search_tracks, "
                "optionally on a device_ref returned by spotify_devices. "
                "The tool verifies playback when Spotify reports state in time."
            ),
            schema(
                {
                    "track_ref": {"type": "string", "maxLength": 32},
                    "device_ref": {"type": "string", "maxLength": 32},
                },
                ("track_ref",),
            ),
            "spotify.play",
            preconditions=("spotify.authenticated",),
        ),
        controller.play_track,
    )
    registry.register(
        ToolSpec(
            "spotify_pause",
            (
                "Pause Spotify playback, optionally on an exact device_ref "
                "returned by spotify_devices, then verify state when possible."
            ),
            schema(
                {
                    "device_ref": {"type": "string", "maxLength": 32},
                }
            ),
            "spotify.pause",
            preconditions=("spotify.authenticated",),
        ),
        controller.pause,
    )
