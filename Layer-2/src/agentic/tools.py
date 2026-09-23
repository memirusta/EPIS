"""Tool metadata, validation, and constrained Windows integrations."""

from __future__ import annotations

from dataclasses import dataclass
import os
import platform
from typing import Callable
from urllib.parse import urlsplit

from .permissions import RiskClass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    capability: str
    risk_class: str = RiskClass.GREEN.value
    confirmation_required: bool = False
    platforms: tuple[str, ...] = ("windows",)
    confirmation_notice: str = ""
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    model_visible: bool = True

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, tuple[ToolSpec, Callable[[dict], dict]]] = {}

    def register(self, spec: ToolSpec, handler: Callable[[dict], dict]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        self._tools[spec.name] = (spec, handler)

    def get(self, name: str) -> tuple[ToolSpec, Callable[[dict], dict]] | None:
        return self._tools.get(name)

    def openai_schemas(
        self,
        capabilities: set[str] | None = None,
    ) -> list[dict]:
        specs = [
            spec
            for spec in self.specs()
            if spec.model_visible
        ]
        if capabilities is not None:
            specs = [
                spec
                for spec in specs
                if spec.capability in capabilities
            ]
        return [spec.openai_schema() for spec in specs]

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    def capabilities(self, target_platform: str | None = None) -> set[str]:
        target_platform = target_platform or platform.system().lower()
        return {spec.capability for spec in self.specs() if target_platform in spec.platforms}

    def for_capability(self, capability: str):
        return next((entry for entry in self._tools.values() if entry[0].capability == capability), None)

    def dispatch(self, name: str, arguments: dict) -> dict:
        entry = self.get(name)
        if not entry:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        spec, handler = entry
        error = self._validate(spec.schema, arguments)
        if error:
            return {"ok": False, "error": error}
        if platform.system().lower() not in spec.platforms:
            return {"ok": False, "error": f"{name} is not supported on this platform"}
        try:
            return handler(arguments)
        except Exception as exc:  # The model sees an error result, never an unhandled exception.
            return {"ok": False, "outcome": "unknown", "error": type(exc).__name__}

    def dispatch_capability(self, capability: str, arguments: dict) -> dict:
        for spec, _ in self._tools.values():
            if spec.capability == capability:
                return self.dispatch(spec.name, arguments)
        return {"ok": False, "error": f"No local implementation for {capability}"}

    @staticmethod
    def _validate(schema: dict, arguments: dict) -> str | None:
        """Small validator for the deliberately simple 0.1 function schemas."""
        if not isinstance(arguments, dict):
            return "tool arguments must be an object"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(arguments) - set(properties)
            if unknown:
                return f"unexpected argument(s): {', '.join(sorted(unknown))}"
        for key in schema.get("required", []):
            if key not in arguments:
                return f"missing required argument: {key}"
        for key, value in arguments.items():
            definition = properties.get(key, {})
            expected = definition.get("type")
            if expected == "string":
                if not isinstance(value, str):
                    return f"{key} must be a string"
                if len(value) < definition.get("minLength", 0):
                    return f"{key} is too short"
                if len(value) > definition.get("maxLength", 4096):
                    return f"{key} is too long"
            if expected == "boolean" and not isinstance(value, bool):
                return f"{key} must be a boolean"
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                return f"{key} must be an integer"
            if expected == "array":
                if not isinstance(value, list):
                    return f"{key} must be an array"
                if len(value) < definition.get("minItems", 0):
                    return f"{key} has too few items"
                if len(value) > definition.get("maxItems", 10000):
                    return f"{key} has too many items"
                item_definition = definition.get("items", {})
                if item_definition.get("type") == "string":
                    for index, item in enumerate(value):
                        if not isinstance(item, str):
                            return f"{key}[{index}] must be a string"
                        if len(item) > item_definition.get("maxLength", 4096):
                            return f"{key}[{index}] is too long"
            if "enum" in definition and value not in definition["enum"]:
                return f"{key} must be one of: {', '.join(map(str, definition['enum']))}"
            if "minimum" in definition and value < definition["minimum"]:
                return f"{key} must be at least {definition['minimum']}"
            if "maximum" in definition and value > definition["maximum"]:
                return f"{key} must be at most {definition['maximum']}"
        return None


_APPS = {
    "spotify": "spotify.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
}


def _open_app(arguments: dict) -> dict:
    app = str(arguments.get("app", "")).strip().lower()
    executable = _APPS.get(app)
    if not executable:
        return {"ok": False, "error": "Supported apps: spotify, notepad, calculator"}
    if os.name != "nt":
        return {"ok": False, "error": "open_app is currently Windows-only"}
    os.startfile(executable)  # noqa: S606 - fixed allow-list, not model-supplied command.
    return {
        "ok": True,
        "status": "launch_requested",
        "app_name": app,
        "visible_window_verified": False,
    }


def _close_app(arguments: dict) -> dict:
    import psutil
    import win32con
    import win32gui
    import win32process
    app = str(arguments.get("app", "")).strip().lower()
    executable = _APPS.get(app)
    if not executable:
        return {"ok": False, "error": "Supported apps: spotify, notepad, calculator"}
    pids = set()
    for process in psutil.process_iter(["name", "pid"]):
        if (process.info.get("name") or "").lower() == executable:
            pids.add(process.info["pid"])
    requested = []

    def request_close(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids and win32gui.IsWindowVisible(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            requested.append(pid)

    win32gui.EnumWindows(request_close, None)
    return {
        "ok": bool(requested),
        "status": (
            "close_requested"
            if requested
            else "no_visible_windows_found"
        ),
        "app_name": app,
        "pids": sorted(set(requested)),
        "save_dialog_may_remain": bool(requested),
    }


def _set_volume(arguments: dict) -> dict:
    level = arguments.get("level")
    if not isinstance(level, int) or not 0 <= level <= 100:
        return {"ok": False, "error": "level must be an integer from 0 to 100"}
    if os.name != "nt":
        return {"ok": False, "error": "set_volume is currently Windows-only"}
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        speakers = AudioUtilities.GetSpeakers()
        if hasattr(speakers, "EndpointVolume"):
            endpoint = speakers.EndpointVolume
        else:
            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        endpoint.SetMasterVolumeLevelScalar(level / 100.0, None)
        observed = round(endpoint.GetMasterVolumeLevelScalar() * 100)
    except ImportError:
        return {"ok": False, "error": "pycaw is not installed; install requirements.txt"}
    except Exception as exc:
        return {"ok": False, "outcome": "unknown", "error": f"Unable to set volume: {type(exc).__name__}"}
    verified = abs(observed - level) <= 1
    return {
        "ok": verified,
        "status": (
            "volume_verified"
            if verified
            else "volume_mismatch"
        ),
        "requested_level": level,
        "level": observed,
    }


def _media_play_pause(arguments: dict) -> dict:
    if os.name != "nt":
        return {"ok": False, "error": "media_play_pause is currently Windows-only"}
    import win32api
    import win32con
    win32api.keybd_event(win32con.VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MEDIA_PLAY_PAUSE, 0, win32con.KEYEVENTF_KEYUP, 0)
    return {
        "ok": True,
        "status": "media_toggle_sent",
        "target_app": None,
        "playback_state_verified": False,
    }


def _system_info(arguments: dict) -> dict:
    import psutil
    return {
        "ok": True,
        "os": platform.platform(),
        "hostname": platform.node(),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_percent": psutil.virtual_memory().percent,
        "battery": (psutil.sensors_battery().percent if psutil.sensors_battery() else None),
    }


def _battery(arguments: dict) -> dict:
    import psutil
    battery = psutil.sensors_battery()
    return {"ok": True, "present": battery is not None,
            "percent": battery.percent if battery else None,
            "plugged_in": battery.power_plugged if battery else None}


def _media_skip(direction: str) -> dict:
    import win32api
    import win32con
    key = win32con.VK_MEDIA_NEXT_TRACK if direction == "next" else win32con.VK_MEDIA_PREV_TRACK
    win32api.keybd_event(key, 0, 0, 0)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    return {
        "ok": True,
        "status": "media_signal_sent",
        "direction": direction,
        "target_app": None,
        "playback_state_verified": False,
    }




def _remote_phone_call_only(arguments: dict) -> dict:
    return {
        "ok": False,
        "error": "phone.call requires a connected Android device",
    }

def _open_url(arguments: dict) -> dict:
    url = arguments["url"]
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not any(c.isspace() or ord(c) < 32 for c in url)
                 and "\\" not in url and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        return {"ok": False, "error": "Only HTTPS URLs without credentials, whitespace or custom ports are supported"}
    os.startfile(url)
    return {
        "ok": True,
        "status": "navigation_requested",
        "page_load_verified": False,
    }


def build_local_registry() -> ToolRegistry:
    registry = ToolRegistry()
    device_property = {"device_id": {"type": "string", "description": "Optional registered target device id; omit for this computer."}}
    app_schema = {"type": "object", "properties": {"app": {"type": "string", "enum": sorted(_APPS)}, **device_property}, "required": ["app"], "additionalProperties": False}
    registry.register(ToolSpec("open_app", "Open ONLY spotify, notepad or calculator. For ANY other app (including Nebula), call discover_apps immediately; Core handles approval.", app_schema, "app.open"), _open_app)
    registry.register(ToolSpec("close_app", "Close an approved local app after confirmation.", app_schema, "app.close", RiskClass.YELLOW.value, True), _close_app)
    registry.register(ToolSpec("set_volume", "Set system volume to an integer percentage.", {"type": "object", "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}, **device_property}, "required": ["level"], "additionalProperties": False}, "audio.volume"), _set_volume)
    registry.register(ToolSpec("media_play_pause", "Toggle global Windows media playback. Cannot target Spotify, select a song, or guarantee play/pause state.", {"type": "object", "properties": device_property, "additionalProperties": False}, "media.play_pause"), _media_play_pause)
    registry.register(ToolSpec("get_system_info", "Read non-sensitive local system status.", {"type": "object", "properties": device_property, "additionalProperties": False}, "system.info"), _system_info)
    simple = {"type": "object", "properties": device_property, "additionalProperties": False}
    registry.register(ToolSpec("get_battery", "Read battery level and charging status.", simple, "system.battery"), _battery)
    registry.register(ToolSpec("media_next", "Send global Windows next-track signal; target app and playback state are unverified.", simple, "media.next"), lambda args: _media_skip("next"))
    registry.register(ToolSpec("media_previous", "Send global Windows previous-track signal; target app and playback state are unverified.", simple, "media.previous"), lambda args: _media_skip("previous"))
    registry.register(ToolSpec("open_url", "Open an HTTPS URL in the default browser after explicit confirmation.",
                              {"type": "object", "properties": {"url": {"type": "string", "maxLength": 2048}, **device_property},
                               "required": ["url"], "additionalProperties": False},
                              "browser.open_url", RiskClass.YELLOW.value, True), _open_url)
    phone_schema = {
        "type": "object",
        "properties": {
            "number": {
                "type": "string",
                "maxLength": 40,
                "description": "Phone number supplied by the user. Use either number or contact, never both.",
            },
            "contact": {
                "type": "string",
                "maxLength": 160,
                "description": "Contact name to resolve locally on the Android phone. Contact phone numbers never leave the device.",
            },
            "device_id": {
                "type": "string",
                "description": "Optional registered Android phone device id.",
            },
        },
        "additionalProperties": False,
    }
    registry.register(
        ToolSpec(
            "phone_call",
            (
                "Start a normal cellular call on a connected Android phone. "
                "Use exactly one of number or contact. For contact names, resolution "
                "happens locally on the phone and the saved phone number is not sent "
                "to the cloud. Use only when the user explicitly asks to call someone."
            ),
            phone_schema,
            "phone.call",
            RiskClass.YELLOW.value,
            True,
            platforms=("android",),
            confirmation_notice="Telefon araması başlatılacak.",
            effects=("phone.call_requested",),
        ),
        _remote_phone_call_only,
    )
    from .desktop import register_desktop_tools
    windows = register_desktop_tools(registry)
    from .computer_device import register_computer_device_tools
    register_computer_device_tools(registry, windows)
    from .media import register_media_tools
    from .filesystem import register_folder_tools
    from .repository_context import register_repository_tools
    from .system_tools import register_system_tools
    register_media_tools(registry)
    register_folder_tools(registry)
    register_repository_tools(registry)
    register_system_tools(registry)
    from .file_tools import register_file_tools
    from .shell_tools import register_shell_tools
    from .spotify_tools import register_spotify_tools
    from .ui_tools import register_ui_tools
    from .trusted_context import register_trusted_context_tools
    register_file_tools(registry)
    register_shell_tools(registry)
    register_spotify_tools(registry)
    register_ui_tools(registry)
    register_trusted_context_tools(registry)
    return registry
