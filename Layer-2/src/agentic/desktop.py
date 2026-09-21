"""Bounded application discovery and snapshot-bound Win32 window operations."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid


# Discovery is not an arbitrary command/scripting interface.
# Interactive shells are allowed only as argument-free GUI app launches.
# Script/runtime/system-management executables remain blocked here.
BLOCKED_EXECUTABLES = {
    "wscript.exe",
    "cscript.exe",
    "mshta.exe",
    "rundll32.exe",
    "regsvr32.exe",
    "reg.exe",
    "msiexec.exe",
    "schtasks.exe",
    "wmic.exe",
    "wsl.exe",
    "bash.exe",
    "python.exe",
    "pythonw.exe",
    "node.exe",
    "java.exe",
    "javaw.exe",
    "shutdown.exe",
    "diskpart.exe",
    "format.com",
    "control.exe",
    "mmc.exe",
}


@dataclass(frozen=True)
class AppEntry:
    name: str
    path: str
    app_id: str
    launch_kind: str = "exe"
    process_name: str = ""


def app_entry(name, target):
    if not isinstance(target, str) or not target or any(ord(c) < 32 for c in target):
        return None

    target = os.path.expandvars(target.strip().strip('"'))
    if target.startswith(("\\\\", "//")) or '"' in target:
        return None

    path = Path(target)
    if (
        not path.is_absolute()
        or path.suffix.lower() != ".exe"
        or path.name.lower() in BLOCKED_EXECUTABLES
    ):
        return None

    try:
        path = path.resolve(strict=True)
        if (
            not path.is_file()
            or path.suffix.lower() != ".exe"
            or str(path).startswith("\\\\")
            or ":" in str(path)[2:]
            or path.name.lower() in BLOCKED_EXECUTABLES
        ):
            return None
        file_stat = path.stat()
    except OSError:
        return None

    identity = f"{path!s}|{file_stat.st_size}|{file_stat.st_mtime_ns}"
    app_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return AppEntry(
        str(name)[:100],
        str(path),
        app_id,
        process_name=path.name,
    )


def normalized_app_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def app_match_score(query: str, name: str) -> int:
    """Score semantic-ish app-name matches without unsafe substring guessing.

    This intentionally stays deterministic. Luna can infer a likely canonical app
    name (for example, "Brave Browser") while the local catalog ranks installed
    names (for example, "Brave") without exposing paths or raw Windows AppIDs.
    """
    query_words = normalized_app_words(query)
    name_words = normalized_app_words(name)

    if not query_words or not name_words:
        return 0

    query_text = " ".join(query_words)
    name_text = " ".join(name_words)

    # Strongest signal: exact normalized name.
    if query_text == name_text:
        return 1000

    generic = {
        "app",
        "application",
        "browser",
        "windows",
        "microsoft",
    }

    query_meaningful = set(query_words) - generic
    name_meaningful = set(name_words) - generic

    # "Brave Browser" -> "Brave"
    # "Windows Terminal" -> "Terminal"
    if (
        query_meaningful
        and name_meaningful
        and query_meaningful == name_meaningful
    ):
        return 900

    overlap = query_meaningful & name_meaningful
    if not overlap:
        return 0

    coverage = len(overlap) / max(
        len(query_meaningful),
        len(name_meaningful),
    )
    return int(500 * coverage)


def start_app_entry(name, target):
    if not isinstance(name, str) or not isinstance(target, str):
        return None

    name = name.strip()
    target = target.strip()

    if (
        not name
        or not target
        or len(name) > 100
        or len(target) > 512
        or any(ord(c) < 32 for c in name + target)
    ):
        return None

    identity = f"start-app|{target}"
    app_id = hashlib.sha256(identity.encode()).hexdigest()[:24]

    return AppEntry(
        name=name,
        path=target,
        app_id=app_id,
        launch_kind="start_app",
    )


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
            if (
                str(root).startswith("\\\\")
                or not root.exists()
                or os.lstat(root).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                continue

            # Do not follow directory junctions/symlinks or scan arbitrary user folders.
            for directory, folders, files in os.walk(root, followlinks=False):
                folders[:] = [
                    folder
                    for folder in folders
                    if not (
                        os.lstat(Path(directory, folder)).st_file_attributes
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    )
                ]

                for name in sorted(files):
                    if not name.lower().endswith(".lnk"):
                        continue

                    count += 1
                    if count > 512:
                        return

                    try:
                        if (
                            os.lstat(Path(directory, name)).st_file_attributes
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        ):
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


def windows_start_apps():
    """Yield Windows Start Apps using Get-StartApps without leaking AppIDs outward."""
    if os.name != "nt":
        return

    command = (
        "[Console]::OutputEncoding = "
        "[System.Text.UTF8Encoding]::new($false); "
        "Get-StartApps | "
        "Select-Object Name,AppID | "
        "ConvertTo-Json -Compress"
    )

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return

    if completed.returncode != 0:
        return

    raw = completed.stdout.strip()
    if not raw:
        return

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return

    if isinstance(rows, dict):
        rows = [rows]

    if not isinstance(rows, list):
        return

    for row in rows[:1024]:
        if not isinstance(row, dict):
            continue

        name = row.get("Name")
        app_id = row.get("AppID")

        if isinstance(name, str) and isinstance(app_id, str):
            yield name, app_id


class AppCatalog:
    def __init__(self, sources=None, start_sources=None):
        if sources is None:
            self.sources = (
                registered_apps,
                start_menu_apps,
            )
            self.start_sources = (
                start_sources
                if start_sources is not None
                else (windows_start_apps,)
            )
        else:
            # Preserve tests/custom callers that provide only classic EXE sources.
            self.sources = sources
            self.start_sources = start_sources or ()

    def entries(self):
        entries_by_name = {}

        # Prefer Windows' own Start Apps catalog.
        for source in self.start_sources:
            for name, target in source():
                entry = start_app_entry(name, target)
                if entry:
                    entries_by_name.setdefault(entry.name.casefold(), entry)

        # Registry / classic EXE discovery fallback. When the same display name
        # is already represented by a Windows Start App, keep the safer Start
        # launch target but enrich it with the real executable/process name.
        for source in self.sources:
            for name, target in source():
                entry = app_entry(name, target)
                if not entry:
                    continue
                key = entry.name.casefold()
                existing = entries_by_name.get(key)
                if (
                    existing is not None
                    and existing.launch_kind == "start_app"
                    and not existing.process_name
                    and entry.process_name
                ):
                    entries_by_name[key] = AppEntry(
                        name=existing.name,
                        path=existing.path,
                        app_id=existing.app_id,
                        launch_kind=existing.launch_kind,
                        process_name=entry.process_name,
                    )
                else:
                    entries_by_name.setdefault(key, entry)

        return list(entries_by_name.values())

    def resolve(self, arguments):
        return next(
            (
                entry
                for entry in self.entries()
                if entry.app_id == arguments.get("app_id")
                and entry.name == arguments.get("app_name")
            ),
            None,
        )

    def discover(self, arguments):
        query = str(arguments.get("query") or "").strip()
        entries = self.entries()

        if query:
            scored = []

            for entry in entries:
                score = app_match_score(query, entry.name)
                if score > 0:
                    scored.append((score, entry))

            scored.sort(
                key=lambda item: (
                    -item[0],
                    len(item[1].name),
                    item[1].name.casefold(),
                )
            )

            if scored:
                best_score = scored[0][0]
                # Keep only candidates reasonably close to the best match.
                scored = [
                    item
                    for item in scored
                    if item[0] >= best_score - 100
                ]

            matches = [entry for _, entry in scored]
        else:
            # Empty query historically means "list/discover apps".
            matches = entries
            matches.sort(key=lambda item: item.name.casefold())

        return {
            "ok": True,
            "apps": [
                {"app_id": entry.app_id, "app_name": entry.name}
                for entry in matches[:50]
            ],
            "truncated": len(matches) > 50,
            "sources": [
                "windows_start_apps",
                "registry_app_paths",
                "argument_free_start_menu_exes",
            ],
            "filesystem_paths_returned": False,
            "raw_windows_app_ids_returned": False,
        }

    def launch(self, arguments):
        entry = self.resolve(arguments)

        if not entry:
            return {
                "ok": False,
                "error": (
                    "Application selection expired or mismatched; "
                    "discover again"
                ),
            }

        if entry.launch_kind == "start_app":
            try:
                subprocess.Popen(
                    [
                        "explorer.exe",
                        f"shell:AppsFolder\\{entry.path}",
                    ],
                    close_fds=True,
                )
            except OSError as exc:
                return {
                    "ok": False,
                    "error": (
                        "Windows Start app launch failed: "
                        f"{type(exc).__name__}"
                    ),
                }
        else:
            os.startfile(entry.path)

        return {
    "ok": True,
    "status": "launch_requested",
    "app_name": entry.name,
    "visible_window_verified": False,
}


class Win32Windows:
    def foreground_handle(self) -> int:
        import win32gui
        return int(win32gui.GetForegroundWindow())

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
                rows.append(
                    {
                        "hwnd": hwnd,
                        "pid": pid,
                        "created": process.create_time(),
                        "process": process.name(),
                        "class": win32gui.GetClassName(hwnd),
                        "minimized": bool(win32gui.IsIconic(hwnd)),
                    }
                )
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
            return {
                "ok": True,
                "status": "close_requested",
                "save_dialog_may_remain": True,
            }

        if action == "focus":
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            observed = win32gui.GetForegroundWindow() == hwnd
        else:
            code = {
                "minimize": win32con.SW_MINIMIZE,
                "maximize": win32con.SW_MAXIMIZE,
                "restore": win32con.SW_RESTORE,
            }[action]
            win32gui.ShowWindow(hwnd, code)
            show = win32gui.GetWindowPlacement(hwnd)[1]
            expected = win32con.SW_SHOWNORMAL if action == "restore" else code
            observed = (
                bool(win32gui.IsIconic(hwnd))
                if action == "minimize"
                else show == expected
            )

        return {
            "ok": observed,
            "status": (
                "window_state_verified"
                if observed
                else "window_state_unverified"
            ),
            "action": action,
            "state_verified": observed,
            "focus_restrictions_bypassed": False,
        }


class WindowController:
    def __init__(self, backend=None):
        self.backend = backend or Win32Windows()
        self.snapshot = {}
        self.snapshot_sources = {}

    @staticmethod
    def _identity(row):
        return (
            row["hwnd"],
            row["pid"],
            row["created"],
            row["process"],
            row["class"],
        )

    def _visible_rows(self):
        return [
            row
            for row in self.backend.enumerate()
            if row["process"].lower()
            not in {"explorer.exe", "dwm.exe", "winlogon.exe", "lockapp.exe"}
        ]

    def capture_launch_state(self):
        rows = self._visible_rows()
        try:
            foreground = self.backend.foreground_handle()
        except Exception:
            foreground = None
        return {
            "rows": [dict(row) for row in rows],
            "foreground": foreground,
        }

    def correlate_launch(
        self,
        before,
        *,
        process_name="",
        timeout_seconds=8,
    ):
        before_rows = before.get("rows") or []
        before_ids = {
            self._identity(row)
            for row in before_rows
        }
        before_foreground = before.get("foreground")
        deadline = time.monotonic() + timeout_seconds
        process_query = str(process_name or "").casefold()

        while True:
            current = self._visible_rows()
            try:
                foreground = self.backend.foreground_handle()
            except Exception:
                foreground = None

            chosen = None
            exact_process = [
                row
                for row in current
                if process_query
                and row["process"].casefold() == process_query
            ]
            if len(exact_process) == 1:
                candidate = exact_process[0]
                if (
                    self._identity(candidate) not in before_ids
                    or foreground == candidate["hwnd"]
                ):
                    chosen = candidate

            new_rows = [
                row
                for row in current
                if self._identity(row) not in before_ids
            ]
            if chosen is None and new_rows:
                foreground_new = next(
                    (
                        row
                        for row in new_rows
                        if row["hwnd"] == foreground
                    ),
                    None,
                )
                if foreground_new is not None:
                    chosen = foreground_new
                elif len(new_rows) == 1:
                    chosen = new_rows[0]

            if chosen is None and process_query and foreground != before_foreground:
                focused_match = next(
                    (
                        row
                        for row in exact_process
                        if row["hwnd"] == foreground
                    ),
                    None,
                )
                if focused_match is not None:
                    chosen = focused_match

            # Windows Start Apps may not reveal their real process name in the
            # discovery catalog. Launching an already-running app can therefore
            # create no new row at all; Windows simply brings its existing window
            # to the foreground. Bind that concrete foreground transition.
            if (
                chosen is None
                and not process_query
                and foreground
                and foreground != before_foreground
            ):
                focused_row = next(
                    (
                        row
                        for row in current
                        if row["hwnd"] == foreground
                    ),
                    None,
                )
                if focused_row is not None:
                    chosen = focused_row

            if chosen is not None:
                token = uuid.uuid4().hex
                self.snapshot[token] = (
                    chosen,
                    time.monotonic() + 120,
                )
                self.snapshot_sources[token] = "launch"
                return {
                    "ok": True,
                    "status": "window_correlated",
                    "window": {
                        "window_id": token,
                        "app_name": chosen["process"],
                        "minimized": chosen["minimized"],
                    },
                    "selection_ttl_seconds": 120,
                }

            if time.monotonic() >= deadline:
                return {
                    "ok": True,
                    "status": "window_not_correlated",
                    "window": None,
                }

            time.sleep(0.2)

    def list_windows(self, arguments):
        query = arguments.get("app_name", "").casefold().strip()
        rows = [
            row
            for row in self.backend.enumerate()
            if query in row["process"].casefold()
            and row["process"].lower()
            not in {"explorer.exe", "dwm.exe", "winlogon.exe", "lockapp.exe"}
        ]

        self.snapshot = {
            uuid.uuid4().hex: (row, time.monotonic() + 120)
            for row in rows[:64]
        }
        self.snapshot_sources = {
            token: "list"
            for token in self.snapshot
        }

        return {
            "ok": True,
            "windows": [
                {
                    "window_id": token,
                    "app_name": row["process"],
                    "minimized": row["minimized"],
                }
                for token, (row, _) in self.snapshot.items()
            ],
            "truncated": len(rows) > 64,
            "selection_ttl_seconds": 120,
            "window_titles_collected": False,
            "window_content_collected": False,
        }

    def wait_for_window(self, arguments):
        """Poll for a newly launched app window instead of racing app startup."""
        timeout = int(arguments.get("timeout_seconds", 8))
        deadline = time.monotonic() + timeout

        while True:
            result = self.list_windows({
                "app_name": arguments.get("app_name", ""),
            })
            count = len(result["windows"])

            if count == 1:
                result["status"] = "window_found"
                return result

            if count > 1:
                result["status"] = "window_ambiguous"
                return result

            if time.monotonic() >= deadline:
                result["status"] = "window_wait_timeout"
                return result

            time.sleep(0.2)

    def resolve_selection(
        self,
        arguments,
        *,
        refresh_ttl=False,
    ):
        """Resolve an opaque window token back to the same verified OS window."""
        saved = self.snapshot.get(arguments["window_id"])
        source = self.snapshot_sources.get(arguments["window_id"], "list")
        if not saved or saved[1] < time.monotonic():
            return None, {
                "ok": False,
                "error": "Window selection expired; list windows again",
            }

        expected = saved[0]
        if arguments["app_name"] != expected["process"]:
            return None, {"ok": False, "error": "Window application mismatch"}

        current = self.backend.enumerate()
        if (
            source != "launch"
            and sum(
                row["process"].casefold() == expected["process"].casefold()
                for row in current
            ) > 1
        ):
            return None, {
                "ok": False,
                "error": (
                    "Multiple windows for this app; selection is ambiguous. "
                    "No action taken."
                ),
            }

        actual = next(
            (row for row in current if row["hwnd"] == expected["hwnd"]),
            None,
        )
        keys = ("hwnd", "pid", "created", "process", "class")
        if not actual or any(actual[key] != expected[key] for key in keys):
            return None, {
                "ok": False,
                "error": "Window identity changed; not executed",
            }

        if refresh_ttl:
            self.snapshot[arguments["window_id"]] = (
                expected,
                time.monotonic() + 120,
            )

        return actual, None

    def act(self, arguments, action):
        actual, error = self.resolve_selection(arguments)
        if error:
            return error
        return self.backend.apply(actual, action)


def register_desktop_tools(registry):
    from .tools import ToolSpec

    catalog, windows = AppCatalog(), WindowController()
    device = {"device_id": {"type": "string"}}

    def schema(properties, required=()):
        return {
            "type": "object",
            "properties": {**properties, **device},
            "required": list(required),
            "additionalProperties": False,
        }

    def launch_with_correlation(arguments):
        entry = catalog.resolve(arguments)
        before = (
            windows.capture_launch_state()
            if entry is not None
            else None
        )
        result = catalog.launch(arguments)
        if (
            not result.get("ok")
            or result.get("status") != "launch_requested"
            or before is None
        ):
            return result

        correlation = windows.correlate_launch(
            before,
            process_name=entry.process_name,
            timeout_seconds=8,
        )
        window = correlation.get("window")
        result = dict(result)
        result["window_correlation"] = correlation.get("status")
        result["window"] = window
        result["visible_window_verified"] = bool(window)
        if window:
            result["selection_ttl_seconds"] = correlation.get(
                "selection_ttl_seconds",
                120,
            )
        return result

    registry.register(
        ToolSpec(
            "discover_apps",
            (
                "Discover installed apps by a model-chosen canonical search term. "
                "Use this for apps outside open_app and when the user describes "
                "an app indirectly (appearance, category, nickname, abbreviation "
                "or command name). Infer a likely canonical app name, search it, "
                "broaden once if needed, then use an exact returned app_id/app_name. "
                "No paths or shortcut arguments are returned."
            ),
            schema({"query": {"type": "string", "maxLength": 100}}),
            "apps.discover",
            "yellow",
            True,
            confirmation_notice=(
                "Eşleşen uygulama adları model API'sine gönderilecek; "
                "dosya yolları gönderilmez."
            ),
        ),
        catalog.discover,
    )

    registry.register(
        ToolSpec(
            "launch_discovered_app",
            (
                "Launch an exact discovered app ID/name. This launches only the "
                "stored executable with no arguments and never elevates. No paths, "
                "arguments, shell commands or scripts are accepted."
            ),
            schema(
                {
                    "app_id": {"type": "string", "maxLength": 24},
                    "app_name": {"type": "string", "maxLength": 100},
                },
                ("app_id", "app_name"),
            ),
            "apps.launch",
            "yellow",
            True,
            effects=("app.latest_window",),
        ),
        launch_with_correlation,
    )

    registry.register(
        ToolSpec(
            "list_windows",
            (
                "FIRST STEP to minimize/maximize/restore/focus/close a window. "
                "Returns opaque window IDs plus application names only; no titles "
                "or content."
            ),
            schema({"app_name": {"type": "string", "maxLength": 100}}),
            "windows.list",
            "yellow",
            True,
            confirmation_notice=(
                "Eşleşen açık uygulama adları model API'sine gönderilecek; "
                "pencere başlıkları ve içerikleri okunmaz."
            ),
        ),
        windows.list_windows,
    )

    registry.register(
        ToolSpec(
            "wait_for_window",
            (
                "After launching an app, wait briefly for exactly one matching "
                "visible window to appear. Use this instead of calling list_windows "
                "immediately after launch. Returns the same opaque window selection "
                "shape as list_windows and never reads window titles/content."
            ),
            schema({
                "app_name": {"type": "string", "maxLength": 100},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 15,
                },
            }, ("app_name",)),
            "windows.wait",
            model_visible=False,
        ),
        windows.wait_for_window,
    )

    selection = schema(
        {
            "window_id": {"type": "string", "maxLength": 32},
            "app_name": {"type": "string", "maxLength": 100},
        },
        ("window_id", "app_name"),
    )

    for action in ("focus", "minimize", "maximize", "restore", "close"):
        registry.register(
            ToolSpec(
                f"{action}_window",
                (
                    f"{action.title()} an exact window from list_windows. "
                    "Never invent IDs; ask if selection is ambiguous."
                ),
                selection,
                f"windows.{action}",
                "yellow" if action == "close" else "green",
                action == "close",
                effects=(
                    ("window.foreground",)
                    if action == "focus"
                    else ()
                ),
            ),
            lambda args, action=action: windows.act(args, action),
        )

    # Shared with hidden Computer Use primitives.
    return windows
