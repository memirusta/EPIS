import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))

from agentic.spotify_tools import SpotifyController


class MemoryStore:
    def __init__(self, value=None):
        self.value = value

    def load(self):
        return dict(self.value) if self.value else None

    def save(self, value):
        self.value = dict(value)

    def clear(self):
        self.value = None


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self):
        self.responses = []
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class SpotifyToolTests(unittest.TestCase):
    def controller(self, session=None):
        return SpotifyController(
            client_id="client",
            redirect_uri="http://127.0.0.1:8765/callback",
            token_store=MemoryStore({
                "access_token": "token",
                "refresh_token": "refresh",
                "expires_at": 999999,
            }),
            session=session or FakeSession(),
            opener=Mock(),
            clock=lambda: 1000,
        )

    def test_missing_client_id_fails_closed(self):
        controller = SpotifyController(
            client_id="",
            token_store=MemoryStore(),
            opener=Mock(),
        )
        result = controller.begin_auth({})
        self.assertFalse(result["ok"])
        self.assertTrue(result["setup_required"])

    def test_search_returns_opaque_track_refs_not_raw_uris(self):
        session = FakeSession()
        session.responses.append(FakeResponse(payload={
            "tracks": {
                "items": [{
                    "uri": "spotify:track:secret-id",
                    "name": "Starboy",
                    "artists": [{"name": "The Weeknd"}],
                    "album": {"name": "Starboy"},
                    "duration_ms": 230000,
                }]
            }
        }))
        controller = self.controller(session)
        result = controller.search_tracks({"query": "Starboy"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["tracks"][0]["name"], "Starboy")
        self.assertNotIn("uri", result["tracks"][0])
        self.assertNotIn("secret-id", str(result["tracks"][0]))
        self.assertIn(
            result["tracks"][0]["track_ref"],
            controller.track_refs,
        )

    def test_play_requires_a_track_ref_from_search(self):
        controller = self.controller()
        result = controller.play_track({"track_ref": "invented"})
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "spotify_track_selection_expired",
        )


if __name__ == "__main__":
    unittest.main()
