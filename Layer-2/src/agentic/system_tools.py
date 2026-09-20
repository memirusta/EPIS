"""Fixed OS integrations, never model-provided shell/PowerShell/WMI queries."""
import os

SETTINGS = {"sound": "ms-settings:sound", "display": "ms-settings:display",
            "bluetooth": "ms-settings:bluetooth", "wifi": "ms-settings:network-wifi",
            "battery": "ms-settings:batterysaver", "notifications": "ms-settings:notifications"}


def audio_endpoint():
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    speakers = AudioUtilities.GetSpeakers()
    if hasattr(speakers, "EndpointVolume"):
        return speakers.EndpointVolume
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    return cast(speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))


def audio_status(args):
    endpoint = audio_endpoint()
    return {"ok": True, "level": round(endpoint.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(endpoint.GetMute())}


def mute(args):
    endpoint = audio_endpoint()
    wanted = args["state"] == "muted"
    endpoint.SetMute(int(wanted), None)
    observed = bool(endpoint.GetMute())
    return {"ok": observed == wanted, "muted": observed, "message": "Mute state read back"}


def disk_status(args):
    import shutil
    drive = os.getenv("SystemDrive", "C:") + "\\"
    usage = shutil.disk_usage(drive)
    return {"ok": True, "scope": "Windows system drive", "total_gib": round(usage.total / 2**30, 1),
            "free_gib": round(usage.free / 2**30, 1), "used_percent": round(usage.used / usage.total * 100, 1)}


def open_settings(args):
    os.startfile(SETTINGS[args["page"]])
    return {"ok": True, "message": "Windows settings page requested; no setting changed"}


def lock_screen(args):
    import ctypes
    success = bool(ctypes.windll.user32.LockWorkStation())
    return {"ok": success, "message": "Lock request accepted; lock state not independently verified" if success else "Windows rejected lock request"}


def brightness(args):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    service = monitors = methods = monitor = method = matching = parameters = result = None
    try:
        service = win32com.client.GetObject(r"winmgmts:\\.\root\WMI")
        monitors = list(service.ExecQuery("SELECT * FROM WmiMonitorBrightness WHERE Active=True"))
        if len(monitors) != 1:
            return {"ok": False, "error": "Exactly one WMI-controllable active display required; external monitors may be unsupported"}
        monitor = monitors[0]
        if "level" in args:
            methods = list(service.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods WHERE Active=True"))
            matching = [m for m in methods if m.InstanceName == monitor.InstanceName]
            if len(matching) != 1:
                return {"ok": False, "error": "Brightness control unavailable for this display"}
            method = matching.pop()
            # SWbemObject does not reliably support Python named-argument dispatch.
            # Bind the fixed WMI method's typed input properties explicitly.
            parameters = method.Methods_("WmiSetBrightness").InParameters.SpawnInstance_()
            parameters.Properties_("Timeout").Value = 0
            parameters.Properties_("Brightness").Value = args["level"]
            result = method.ExecMethod_("WmiSetBrightness", parameters)
            if result is not None and int(result.Properties_("ReturnValue").Value) != 0:
                return {"ok": False, "error": "Windows rejected brightness request"}
            monitors = list(service.ExecQuery("SELECT * FROM WmiMonitorBrightness WHERE Active=True"))
            if len(monitors) != 1 or monitors[0].InstanceName != monitor.InstanceName:
                return {"ok": False, "outcome": "unknown", "error": "Display changed during verification"}
            monitor = monitors[0]
        level = int(monitor.CurrentBrightness)
        return {"ok": "level" not in args or level == args["level"], "level": level, "message": "Brightness read from Windows"}
    finally:
        result = parameters = matching = method = monitor = methods = monitors = service = None
        pythoncom.CoUninitialize()


def register_system_tools(registry):
    from .tools import ToolSpec
    device = {"device_id": {"type": "string"}}
    def schema(properties=None, required=()):
        return {"type": "object", "properties": {**(properties or {}), **device}, "required": list(required), "additionalProperties": False}
    registry.register(ToolSpec("get_audio_status", "Read master volume percentage and mute state.", schema(), "audio.status"), audio_status)
    registry.register(ToolSpec("set_mute", "Set master audio mute/unmute explicitly, then verify.",
        schema({"state": {"type": "string", "enum": ["muted", "unmuted"]}}, ["state"]), "audio.mute"), mute)
    registry.register(ToolSpec("get_disk_space", "Read free/total space of the Windows system drive only; no file names.", schema(), "system.disk"), disk_status)
    registry.register(ToolSpec("open_settings", "Open a fixed Windows settings page. Does not change the setting itself.",
        schema({"page": {"type": "string", "enum": list(SETTINGS)}}, ["page"]), "system.settings", "yellow", True), open_settings)
    registry.register(ToolSpec("lock_screen", "Lock this Windows session after explicit confirmation. Never invoke for sleep/shutdown/restart requests.",
        schema(), "system.lock", "yellow", True, confirmation_notice="Windows oturumu kilitlenecek; geri dönmek için kendin giriş yapmalısın."), lock_screen)
    registry.register(ToolSpec("get_brightness", "Read internal display brightness when Windows WMI supports it.", schema(), "display.brightness_read"), brightness)
    registry.register(ToolSpec("set_brightness", "Set brightness 1-100 for one supported internal display and read it back. No external-monitor guarantee.",
        schema({"level": {"type": "integer", "minimum": 1, "maximum": 100}}, ["level"]), "display.brightness_set"), brightness)
