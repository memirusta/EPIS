from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from agentic.desktop import WindowController
from agentic.tools import build_local_registry


class FakeWindows:
    def __init__(self, rows, foreground):
        self.rows = [dict(row) for row in rows]
        self.foreground = foreground

    def enumerate(self):
        return [dict(row) for row in self.rows]

    def foreground_handle(self):
        return self.foreground


def test_existing_foreground_target_can_be_launch_correlated():
    rows = [
        {
            "hwnd": 101,
            "pid": 1001,
            "created": 1.0,
            "process": "Spotify.exe",
            "class": "Chrome_WidgetWin_0",
            "minimized": False,
        },
        {
            "hwnd": 202,
            "pid": 1002,
            "created": 2.0,
            "process": "Spotify.exe",
            "class": "Chrome_WidgetWin_0",
            "minimized": False,
        },
    ]

    controller = WindowController(
        FakeWindows(rows, foreground=202)
    )

    result = controller.correlate_launch(
        {
            "rows": [dict(row) for row in rows],
            "foreground": 202,
        },
        process_name="Spotify.exe",
        timeout_seconds=0,
    )

    assert result["ok"] is True
    assert result["status"] == "window_correlated"
    assert result["window"]["app_name"] == "Spotify.exe"


def test_legacy_spotify_search_registered_but_hidden_from_luna():
    registry = build_local_registry()

    entry = registry.get("search_spotify")

    assert entry is not None
    assert entry[0].model_visible is False

    visible = {
        item["function"]["name"]
        for item in registry.openai_schemas()
    }

    assert "search_spotify" not in visible
    assert "spotify_search_tracks" in visible
    assert "spotify_play_track" in visible


def test_spotify_has_no_special_core_recipe():
    source = (
        ROOT / "Layer-2/src/agentic/core.py"
    ).read_text(encoding="utf-8")

    assert "# SPOTIFY PLAYBACK" not in source


def test_core_fallback_is_valid_utf8():
    source = (
        ROOT / "Layer-2/src/agentic/core.py"
    ).read_text(encoding="utf-8")

    assert "İşlemi tamamlayamadım" in source
    assert "fazla sayıda araç adımı oluştu" in source


def test_existing_unique_window_fallback_exists():
    source = (
        ROOT / "Layer-2/src/agentic/desktop.py"
    ).read_text(encoding="utf-8")

    assert "window_correlated_existing_unique" in source
