"""Bounded application discovery and snapshot-bound Win32 window operations."""
from dataclasses import dataclass
import hashlib
import os
import stat
from pathlib import Path
import time
import uuid


# Discovery is not an arbitrary command/scripting interface.
BLOCKED_EXECUTABLES = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "windowsterminal.exe",
    "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe",
    "reg.exe", "msiexec.exe", "schtasks.exe", "wmic.exe", "wsl.exe", "bash.exe",
    "python.exe", "pythonw.exe", "node.exe", "java.exe", "javaw.exe",
    "shutdown.exe", "diskpart.exe", "format.com", "control.exe", "mmc.exe",
}


@dataclass(frozen=True)
class AppEntry:
    name: str
    path: str
    app_id: str


def app_entry(name, target):
    if not isinstance(target, str) or not target or any(ord(c) < 32 for c in target):
        return None
    target = os.path.expandvars(target.strip().strip('"'))
    if target.startswith(("\\\\", "//")) or '"' in target:
        return None
    path = Path(target)
    if not path.is_absolute() or path.suffix.lower() != ".exe" or path.name.lower() in BLOCKED_EXECUTABLES:
        return None
    try:
        path = path.resolve(strict=True)
        if (not path.is_file() or path.suffix.lower() != ".exe" or str(path).startswith("\\\\")
                or ":" in str(path)[2:] or path.name.lower() in BLOCKED_EXECUTABLES):
            return None
        stat = path.stat()
    except OSError:
        return None
    identity = f"{path!s}|{stat.st_size}|{stat.st_mtime_ns}"
    app_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return AppEntry(str(name)[:100], str(path), app_id)


def registered_apps():
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as root:
                    for index in range(min(winreg.QueryInfoKey(root)[0], 512)):
                        try:
                            name = winreg.EnumKey(root, index)
                            with winreg.OpenKey(root, name) as key:
                                target, _ = winreg.QueryValueEx(key, None)
                            yield Path(name).stem, target
                        except OSError:
                            continue
            except OSError:
                continue


def start_menu_apps():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    shell = shortcut = None
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        count = 0
        for variable in ("APPDATA", "PROGRAMDATA"):
            base = os.getenv(variable)
            if not base:
                continue
            root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if str(root).startswith("\\\\") or not root.exists() or os.lstat(root).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            # Do not follow directory junctions/symlinks or scan arbitrary user folders.
            for directory, folders, files in os.walk(root, followlinks=False):
                folders[:] = [f for f in folders if not (os.lstat(Path(directory, f)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)]
                for name in sorted(files):
                    if not name.lower().endswith(".lnk"):
                        continue
                    count += 1
                    if count > 512:
                        return
                    try:
                        if os.lstat(Path(directory, name)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        shortcut = shell.CreateShortcut(str(Path(directory, name)))
                        # Read shortcuts; never execute the shortcut or its command-line arguments.
                        if not shortcut.Arguments:
                            yield Path(name).stem, shortcut.TargetPath
                    except Exception:
                        continue
    finally:
        shortcut = None
        shell = None
        pythoncom.CoUninitialize()


class AppCatalog:
    def __init__(self, sources=None):
        self.sources = sources or (registered_apps, start_menu_apps)

    def entries(self):
        entries = {}
        for source in self.sources:
            for name, target in source():
                entry = app_entry(name, target)
                if entry:
                    entries.setdefault(entry.app_id, entry)
        return list(entries.values())

    def discover(self, arguments):
        query = arguments.get("query", "").casefold().strip()
        matches = [entry for entry in self.entries() if query in entry.name.casefold()]
        matches.sort(key=lambda item: item.name.casefold())
        return {"ok": True, "apps": [{"app_id": entry.app_id, "app_name": entry.name} for entry in matches[:50]],
                "truncated": len(matches) > 50,
                "limitations": "Registry and argument-free Start Menu EXEs only; not all Store/portable apps. No filesystem paths returned."}

    def launch(self, arguments):
        entry = next((entry for entry in self.entries() if entry.app_id == arguments["app_id"]), None)
        if not entry or entry.name != arguments["app_name"]:
            return {"ok": False, "error": "Application selection expired or mismatched; discover again"}
        os.startfile(entry.path)
        return {"ok": True, "message": f"{entry.name} launch requested; visible window not yet verified"}


class Win32Windows:
    def enumerate(self):
        import psutil
        import win32gui
        import win32process
        rows = []
        def collect(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, 4):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                process = psutil.Process(pid)
                rows.append({"hwnd": hwnd, "pid": pid, "created": process.create_time(),
                             "process": process.name(), "class": win32gui.GetClassName(hwnd),
                             "minimized": bool(win32gui.IsIconic(hwnd))})
            except (psutil.Error, OSError):
                return
        win32gui.EnumWindows(collect, None)
        return rows

    def apply(self, row, action):
        import win32con
        import win32gui
        hwnd = row["hwnd"]
        if action == "close":
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return {"ok": True, "message": "Normal window close requested; save dialog may remain"}
        if action == "focus":
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            observed = win32gui.GetForegroundWindow() == hwnd
        else:
            code = {"minimize": win32con.SW_MINIMIZE, "maximize": win32con.SW_MAXIMIZE,
                    "restore": win32con.SW_RESTORE}[action]
            win32gui.ShowWindow(hwnd, code)
            show = win32gui.GetWindowPlacement(hwnd)[1]
            expected = win32con.SW_SHOWNORMAL if action == "restore" else code
            observed = bool(win32gui.IsIconic(hwnd)) if action == "minimize" else show == expected
        return {"ok": observed, "action": action,
                "message": "Window state verified" if observed else "Windows did not confirm the requested state; no focus restrictions bypassed"}


class WindowController:
    def __init__(self, backend=None):
        self.backend = backend or Win32Windows()
        self.snapshot = {}

    def list_windows(self, arguments):
        query = arguments.get("app_name", "").casefold().strip()
        rows = [row for row in self.backend.enumerate() if query in row["process"].casefold()
                and row["process"].lower() not in {"explorer.exe", "dwm.exe", "winlogon.exe", "lockapp.exe"}]
        self.snapshot = {uuid.uuid4().hex: (row, time.monotonic() + 120) for row in rows[:64]}
        return {"ok": True, "windows": [{"window_id": token, "app_name": row["process"], "minimized": row["minimized"]}
                for token, (row, _) in self.snapshot.items()], "truncated": len(rows) > 64,
                "message": "No window titles/content collected. Tokens expire in 120 seconds; do not guess between multiple windows."}

    def act(self, arguments, action):
        saved = self.snapshot.get(arguments["window_id"])
        if not saved or saved[1] < time.monotonic():
            return {"ok": False, "error": "Window selection expired; list windows again"}
        expected = saved[0]
        if arguments["app_name"] != expected["process"]:
            return {"ok": False, "error": "Window application mismatch"}
        current = self.backend.enumerate()
        # Names alone cannot disambiguate two documents/windows from the same app.
        # Enforce this here, not just in a model instruction.
        if sum(row["process"].casefold() == expected["process"].casefold() for row in current) > 1:
            return {"ok": False, "error": "Multiple windows for this app; selection is ambiguous. No action taken."}
        actual = next((row for row in current if row["hwnd"] == expected["hwnd"]), None)
        keys = ("hwnd", "pid", "created", "process", "class")
        if not actual or any(actual[key] != expected[key] for key in keys):
            return {"ok": False, "error": "Window identity changed; not executed"}
        return self.backend.apply(actual, action)


def register_desktop_tools(registry):
    from .tools import ToolSpec
    catalog, windows = AppCatalog(), WindowController()
    device = {"device_id": {"type": "string"}}
    def schema(properties, required=()):
        return {"type": "object", "properties": {**properties, **device}, "required": list(required), "additionalProperties": False}
    registry.register(ToolSpec("discover_apps", "FIRST STEP to open any app not in open_app, e.g. Nebula. Call this function immediately; Core pauses it to ask approval. Do not ask approval in chat. Returns exact app_id/app_name; no paths.",
        schema({"query": {"type": "string", "maxLength": 100}}), "apps.discover", "yellow", True,
        confirmation_notice="Eşleşen uygulama adları model API'sine gönderilecek; dosya yolları gönderilmez."), catalog.discover)
    registry.register(ToolSpec("launch_discovered_app", "Launch an exact discovered app ID/name after approval. No paths, arguments, shell or scripts accepted.",
        schema({"app_id": {"type": "string", "maxLength": 24}, "app_name": {"type": "string", "maxLength": 100}}, ("app_id", "app_name")),
        "apps.launch", "yellow", True), catalog.launch)
    registry.register(ToolSpec("list_windows", "FIRST STEP to minimize/maximize/restore/focus/close a window. Call this function immediately; Core asks approval before collecting process names. Do not ask approval in chat. No titles/content.",
        schema({"app_name": {"type": "string", "maxLength": 100}}), "windows.list", "yellow", True,
        confirmation_notice="Eşleşen açık uygulama adları model API'sine gönderilecek; pencere başlıkları ve içerikleri okunmaz."), windows.list_windows)
    selection = schema({"window_id": {"type": "string", "maxLength": 32}, "app_name": {"type": "string", "maxLength": 100}}, ("window_id", "app_name"))
    for action in ("focus", "minimize", "maximize", "restore", "close"):
        registry.register(ToolSpec(f"{action}_window", f"{action.title()} an exact window from list_windows. Never invent IDs; ask if selection is ambiguous.",
            selection, f"windows.{action}", "yellow" if action == "close" else "green", action == "close"),
            lambda args, action=action: windows.act(args, action))
