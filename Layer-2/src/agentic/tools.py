"""Tool metadata, validation, and constrained Windows integrations."""

from __future__ import annotations

from dataclasses import dataclass
import os
import platform
from typing import Callable

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

    def openai_schemas(self) -> list[dict]:
        return [spec.openai_schema() for spec, _ in self._tools.values()]

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
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
            if expected == "string" and not isinstance(value, str):
                return f"{key} must be a string"
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                return f"{key} must be an integer"
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
    return {"ok": True, "message": f"{app} launch requested"}


def _close_app(arguments: dict) -> dict:
    import psutil
    app = str(arguments.get("app", "")).strip().lower()
    executable = _APPS.get(app)
    if not executable:
        return {"ok": False, "error": "Supported apps: spotify, notepad, calculator"}
    stopped = []
    for process in psutil.process_iter(["name", "pid"]):
        if (process.info.get("name") or "").lower() == executable:
            process.terminate()
            stopped.append(process.info["pid"])
    return {"ok": bool(stopped), "message": f"{app} close requested", "pids": stopped}


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
        interface = AudioUtilities.GetSpeakers().Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        endpoint.SetMasterVolumeLevelScalar(level / 100.0, None)
    except ImportError:
        return {"ok": False, "error": "pycaw is not installed; install requirements.txt"}
    except Exception as exc:
        return {"ok": False, "error": f"Unable to set volume: {exc}"}
    return {"ok": True, "message": f"volume set to {level}", "level": level}


def _media_play_pause(arguments: dict) -> dict:
    if os.name != "nt":
        return {"ok": False, "error": "media_play_pause is currently Windows-only"}
    import win32api
    import win32con
    win32api.keybd_event(win32con.VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MEDIA_PLAY_PAUSE, 0, win32con.KEYEVENTF_KEYUP, 0)
    return {"ok": True, "message": "media play/pause signal sent"}


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


def build_local_registry() -> ToolRegistry:
    registry = ToolRegistry()
    device_property = {"device_id": {"type": "string", "description": "Optional registered target device id; omit for this computer."}}
    app_schema = {"type": "object", "properties": {"app": {"type": "string", "enum": sorted(_APPS)}, **device_property}, "required": ["app"], "additionalProperties": False}
    registry.register(ToolSpec("open_app", "Open an approved local app.", app_schema, "app.open"), _open_app)
    registry.register(ToolSpec("close_app", "Close an approved local app after confirmation.", app_schema, "app.close", RiskClass.YELLOW.value, True), _close_app)
    registry.register(ToolSpec("set_volume", "Set system volume to an integer percentage.", {"type": "object", "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}, **device_property}, "required": ["level"], "additionalProperties": False}, "audio.volume"), _set_volume)
    registry.register(ToolSpec("media_play_pause", "Toggle system media playback.", {"type": "object", "properties": device_property, "additionalProperties": False}, "media.play_pause"), _media_play_pause)
    registry.register(ToolSpec("get_system_info", "Read non-sensitive local system status.", {"type": "object", "properties": device_property, "additionalProperties": False}, "system.info"), _system_info)
    return registry
