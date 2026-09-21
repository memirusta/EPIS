"""Bounded Windows UI Automation primitives.

The model receives only short-lived opaque references to controls in the current
foreground window. Routine tools refuse sensitive controls; a separate
approval-gated tool is required for sensitive clicks, text entry or hotkeys.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from urllib.parse import urlsplit


ELEMENT_TTL_SECONDS = 60
ALLOWED_ROLES = frozenset({
    "ButtonControl",
    "EditControl",
    "ComboBoxControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "MenuItemControl",
    "TabItemControl",
    "ListItemControl",
    "HyperlinkControl",
    "DocumentControl",
})
TEXT_ROLES = frozenset({
    "EditControl",
    "ComboBoxControl",
    "DocumentControl",
})
SENSITIVE_WORDS = (
    "send", "submit", "buy", "purchase", "order", "checkout", "pay",
    "delete", "remove", "uninstall", "install", "allow", "authorize",
    "approve", "confirm", "save", "publish", "upload", "run as administrator",
    "gönder", "gonder", "satın", "satin", "öde", "ode", "sil", "kaldır",
    "kaldir", "yükle", "yukle", "kur", "izin ver", "onayla", "kaydet",
    "yayınla", "yayinla",
)
MUTATING_EDITOR_PROCESSES = frozenset({
    "code.exe",
    "cursor.exe",
    "devenv.exe",
    "idea64.exe",
    "pycharm64.exe",
    "webstorm64.exe",
    "rider64.exe",
    "notepad++.exe",
})
ROUTINE_HOTKEYS = {
    "ctrl+l": "{Ctrl}l",
    "ctrl+f": "{Ctrl}f",
    "escape": "{Esc}",
    "tab": "{Tab}",
    "shift+tab": "{Shift}{Tab}",
    "home": "{Home}",
    "end": "{End}",
    "pageup": "{PgUp}",
    "pagedown": "{PgDn}",
    "alt+left": "{Alt}{Left}",
    "alt+right": "{Alt}{Right}",
}
SENSITIVE_HOTKEYS = {
    "enter": "{Enter}",
    "ctrl+enter": "{Ctrl}{Enter}",
    "ctrl+s": "{Ctrl}s",
    "alt+f4": "{Alt}{F4}",
    "delete": "{Delete}",
}

SUPPORTED_BROWSER_PROCESSES = frozenset({
    "brave.exe",
    "chrome.exe",
    "firefox.exe",
    "msedge.exe",
    "nebula.exe",
    "opera.exe",
    "vivaldi.exe",
})


class WindowsUIBackend:
    def inspect_foreground(self, max_depth: int):
        import psutil
        import uiautomation as auto
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow())
        if not hwnd:
            raise RuntimeError("No foreground window")
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = psutil.Process(pid).name()
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise RuntimeError("Foreground window is not exposed to UI Automation")
        controls = []
        for control, depth in auto.WalkControl(root, maxDepth=max_depth):
            if depth == 0:
                continue
            controls.append(control)
        return hwnd, process_name, controls

    def foreground_handle(self) -> int:
        import win32gui
        return int(win32gui.GetForegroundWindow())

    def foreground_process(self) -> str:
        import psutil
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow())
        if not hwnd:
            raise RuntimeError("No foreground window")
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()

    @staticmethod
    def meta(control) -> dict:
        def value(name, default=None):
            try:
                return getattr(control, name)
            except Exception:
                return default

        return {
            "name": str(value("Name", "") or "")[:200],
            "role": str(value("ControlTypeName", "") or ""),
            "automation_id": str(value("AutomationId", "") or "")[:200],
            "enabled": bool(value("IsEnabled", False)),
            "password": bool(value("IsPassword", False)),
        }

    @staticmethod
    def click(control) -> None:
        control.Click()

    @staticmethod
    def set_value(control, text: str) -> None:
        control.GetValuePattern().SetValue(text)

    @staticmethod
    def send_keys(keys: str) -> None:
        import uiautomation as auto
        auto.SendKeys(keys)

    @staticmethod
    def navigate_https(url: str) -> None:
        import uiautomation as auto

        auto.SendKeys("{Ctrl}l")
        time.sleep(0.05)
        focused = auto.GetFocusedControl()
        if focused is None:
            raise RuntimeError("Browser address bar unavailable")
        role = str(getattr(focused, "ControlTypeName", "") or "")
        if role != "EditControl" or bool(getattr(focused, "IsPassword", False)):
            raise RuntimeError("Browser address bar unavailable")
        focused.GetValuePattern().SetValue(url)
        auto.SendKeys("{Enter}")


@dataclass
class ElementSnapshot:
    control: object
    expires_at: float
    hwnd: int
    process_name: str
    name: str
    role: str
    automation_id: str
    enabled: bool
    password: bool
    interaction: str


class UIController:
    def __init__(self, backend=None, clock=None):
        self.backend = backend or WindowsUIBackend()
        self.clock = clock or time.monotonic
        self.window_ref: str | None = None
        self.window_hwnd: int | None = None
        self.window_process: str | None = None
        self.window_expires_at = 0.0
        self.elements: dict[str, ElementSnapshot] = {}

    def _now(self) -> float:
        return float(self.clock())

    @staticmethod
    def _interaction(meta: dict, process_name: str) -> str:
        if meta.get("password"):
            return "sensitive"
        haystack = (
            f"{meta.get('name', '')} {meta.get('automation_id', '')}"
        ).casefold()
        if any(word in haystack for word in SENSITIVE_WORDS):
            return "sensitive"
        if (
            process_name.casefold() in MUTATING_EDITOR_PROCESSES
            and meta.get("role") in TEXT_ROLES
        ):
            return "sensitive"
        return "routine"

    def inspect(self, arguments: dict) -> dict:
        max_depth = int(arguments.get("max_depth", 4))
        max_elements = int(arguments.get("max_elements", 80))
        try:
            hwnd, process_name, controls = self.backend.inspect_foreground(
                max_depth
            )
        except (ImportError, OSError, RuntimeError) as exc:
            return {
                "ok": False,
                "error": f"ui_inspection_unavailable:{type(exc).__name__}",
            }

        now = self._now()
        self.elements = {}
        self.window_ref = uuid.uuid4().hex
        self.window_hwnd = hwnd
        self.window_process = process_name
        self.window_expires_at = now + ELEMENT_TTL_SECONDS
        public = []

        for control in controls:
            if len(public) >= max_elements:
                break
            try:
                meta = self.backend.meta(control)
            except Exception:
                continue
            if meta.get("role") not in ALLOWED_ROLES:
                continue
            if not meta.get("enabled"):
                continue
            if (
                not meta.get("name")
                and meta.get("role") not in TEXT_ROLES
            ):
                continue
            interaction = self._interaction(meta, process_name)
            ref = uuid.uuid4().hex
            snapshot = ElementSnapshot(
                control=control,
                expires_at=now + ELEMENT_TTL_SECONDS,
                hwnd=hwnd,
                process_name=process_name,
                name=meta.get("name", ""),
                role=meta.get("role", ""),
                automation_id=meta.get("automation_id", ""),
                enabled=bool(meta.get("enabled")),
                password=bool(meta.get("password")),
                interaction=interaction,
            )
            self.elements[ref] = snapshot
            public.append({
                "element_ref": ref,
                "name": snapshot.name,
                "role": snapshot.role,
                "interaction": interaction,
                "password": snapshot.password,
            })

        return {
            "ok": True,
            "window_ref": self.window_ref,
            "process_name": process_name,
            "elements": public,
            "truncated": len(controls) > len(public),
            "selection_ttl_seconds": ELEMENT_TTL_SECONDS,
            "values_read": False,
        }

    def _resolve(self, ref: str) -> tuple[ElementSnapshot | None, dict | None]:
        saved = self.elements.get(ref)
        if not saved or saved.expires_at <= self._now():
            return None, {
                "ok": False,
                "error": "ui_element_selection_expired",
            }
        try:
            foreground = self.backend.foreground_handle()
        except Exception:
            return None, {
                "ok": False,
                "error": "ui_foreground_unavailable",
            }
        if foreground != saved.hwnd:
            return None, {
                "ok": False,
                "error": "ui_foreground_window_changed",
            }
        try:
            meta = self.backend.meta(saved.control)
        except Exception:
            return None, {
                "ok": False,
                "error": "ui_element_stale",
            }
        identity = (
            meta.get("name", ""),
            meta.get("role", ""),
            meta.get("automation_id", ""),
        )
        expected = (
            saved.name,
            saved.role,
            saved.automation_id,
        )
        if identity != expected or not meta.get("enabled"):
            return None, {
                "ok": False,
                "error": "ui_element_identity_changed",
            }
        return saved, None

    def _resolve_window(self, ref: str) -> dict | None:
        if (
            not self.window_ref
            or ref != self.window_ref
            or self.window_expires_at <= self._now()
        ):
            return {
                "ok": False,
                "error": "ui_window_selection_expired",
            }
        try:
            if self.backend.foreground_handle() != self.window_hwnd:
                return {
                    "ok": False,
                    "error": "ui_foreground_window_changed",
                }
        except Exception:
            return {
                "ok": False,
                "error": "ui_foreground_unavailable",
            }
        return None

    def click(self, arguments: dict, *, approved_sensitive: bool) -> dict:
        saved, error = self._resolve(arguments["element_ref"])
        if error:
            return error
        if saved.interaction == "sensitive" and not approved_sensitive:
            return {
                "ok": False,
                "error": "ui_sensitive_element_requires_approval",
            }
        try:
            self.backend.click(saved.control)
        except Exception as exc:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": f"ui_click_failed:{type(exc).__name__}",
            }
        return {
            "ok": True,
            "status": "ui_click_sent",
            "interaction": saved.interaction,
            "effect_verified": False,
        }

    def type_text(self, arguments: dict, *, approved_sensitive: bool) -> dict:
        saved, error = self._resolve(arguments["element_ref"])
        if error:
            return error
        if saved.role not in TEXT_ROLES:
            return {
                "ok": False,
                "error": "ui_element_does_not_accept_text",
            }
        if saved.password:
            return {
                "ok": False,
                "error": "ui_password_entry_not_supported",
            }
        if saved.interaction == "sensitive" and not approved_sensitive:
            return {
                "ok": False,
                "error": "ui_sensitive_text_requires_approval",
            }
        try:
            self.backend.set_value(saved.control, arguments["text"])
        except Exception as exc:
            return {
                "ok": False,
                "error": f"ui_text_entry_failed:{type(exc).__name__}",
            }
        return {
            "ok": True,
            "status": "ui_text_set",
            "interaction": saved.interaction,
            "text_length": len(arguments["text"]),
        }

    def hotkey(self, arguments: dict, *, approved_sensitive: bool) -> dict:
        error = self._resolve_window(arguments["window_ref"])
        if error:
            return error
        name = arguments["hotkey"]
        mapping = SENSITIVE_HOTKEYS if approved_sensitive else ROUTINE_HOTKEYS
        keys = mapping.get(name)
        if keys is None:
            return {
                "ok": False,
                "error": (
                    "ui_sensitive_hotkey_requires_approval"
                    if name in SENSITIVE_HOTKEYS
                    else "ui_hotkey_not_allowed"
                ),
            }
        try:
            self.backend.send_keys(keys)
        except Exception as exc:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": f"ui_hotkey_failed:{type(exc).__name__}",
            }
        return {
            "ok": True,
            "status": "ui_hotkey_sent",
            "hotkey": name,
            "effect_verified": False,
        }

    def scroll(self, arguments: dict) -> dict:
        error = self._resolve_window(arguments["window_ref"])
        if error:
            return error
        direction = arguments["direction"]
        amount = int(arguments.get("amount", 1))
        keys = "{PgDn}" if direction == "down" else "{PgUp}"
        try:
            for _ in range(amount):
                self.backend.send_keys(keys)
        except Exception as exc:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": f"ui_scroll_failed:{type(exc).__name__}",
            }
        return {
            "ok": True,
            "status": "ui_scroll_sent",
            "direction": direction,
            "amount": amount,
            "effect_verified": False,
        }

    def navigate_https(self, arguments: dict) -> dict:
        url = str(arguments["url"]).strip()
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and parsed.port in (None, 443)
                and "\\" not in url
                and not any(c.isspace() or ord(c) < 32 for c in url)
            )
        except ValueError:
            valid = False

        if not valid:
            return {
                "ok": False,
                "error": "ui_navigation_requires_safe_https_url",
            }

        try:
            process_name = self.backend.foreground_process()
        except Exception:
            return {
                "ok": False,
                "error": "ui_foreground_unavailable",
            }

        if process_name.casefold() not in SUPPORTED_BROWSER_PROCESSES:
            return {
                "ok": False,
                "error": "ui_foreground_is_not_supported_browser",
                "process_name": process_name,
            }

        try:
            self.backend.navigate_https(url)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"ui_browser_navigation_failed:{type(exc).__name__}",
            }

        return {
            "ok": True,
            "status": "navigation_requested",
            "browser_process": process_name,
            "page_load_verified": False,
        }

    def wait(self, arguments: dict) -> dict:
        query = arguments["name"].casefold().strip()
        role = arguments.get("role")
        timeout = int(arguments.get("timeout_seconds", 3))
        deadline = self._now() + timeout
        while True:
            result = self.inspect({
                "max_depth": arguments.get("max_depth", 4),
                "max_elements": 100,
            })
            if not result.get("ok"):
                return result
            for element in result["elements"]:
                if query not in element["name"].casefold():
                    continue
                if role and element["role"] != role:
                    continue
                return {
                    "ok": True,
                    "status": "ui_element_found",
                    "window_ref": result["window_ref"],
                    "element": element,
                }
            if self._now() >= deadline:
                return {
                    "ok": True,
                    "status": "ui_wait_timeout",
                    "found": False,
                }
            time.sleep(0.2)


def register_ui_tools(registry):
    from .tools import ToolSpec

    controller = UIController()
    device = {"device_id": {"type": "string"}}

    def schema(properties=None, required=()):
        return {
            "type": "object",
            "properties": {**(properties or {}), **device},
            "required": list(required),
            "additionalProperties": False,
        }

    inspect_schema = schema({
        "max_depth": {
            "type": "integer",
            "minimum": 1,
            "maximum": 8,
        },
        "max_elements": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
        },
    })
    registry.register(
        ToolSpec(
            "ui_inspect",
            (
                "Inspect interactive controls in the CURRENT foreground window. "
                "Returns labels/roles and opaque element_ref values, never field "
                "values. Use dedicated tools before UI automation."
            ),
            inspect_schema,
            "ui.inspect",
            "yellow",
            True,
            confirmation_notice=(
                "Öndeki uygulamanın etkileşimli kontrol adları model API'sine "
                "gönderilecek; alanların mevcut değerleri okunmaz."
            ),
        ),
        controller.inspect,
    )

    element = {
        "element_ref": {"type": "string", "maxLength": 32},
    }
    registry.register(
        ToolSpec(
            "ui_click",
            (
                "Click a routine element_ref from ui_inspect. The local worker "
                "rejects send/submit/buy/delete/install/save/admin-like controls."
            ),
            schema(element, ("element_ref",)),
            "ui.click",
        ),
        lambda args: controller.click(args, approved_sensitive=False),
    )
    registry.register(
        ToolSpec(
            "ui_click_sensitive",
            (
                "Click an approval-gated sensitive element_ref from ui_inspect. "
                "Use for send/submit/buy/delete/install/save/authorization-like "
                "controls; never use ui_click to bypass this."
            ),
            schema(element, ("element_ref",)),
            "ui.click_sensitive",
            "yellow",
            True,
            confirmation_notice=(
                "Ekrandaki hassas bir düğmeye tıklanacak. İşlem gönderme, "
                "silme, kurma, kaydetme veya onaylama etkisi yaratabilir."
            ),
        ),
        lambda args: controller.click(args, approved_sensitive=True),
    )

    text_properties = {
        **element,
        "text": {"type": "string", "maxLength": 2000},
    }
    registry.register(
        ToolSpec(
            "ui_type_text",
            (
                "Set text in a routine UI text field selected by ui_inspect. "
                "Passwords and known code-editor/project text surfaces are blocked."
            ),
            schema(text_properties, ("element_ref", "text")),
            "ui.type",
        ),
        lambda args: controller.type_text(args, approved_sensitive=False),
    )
    registry.register(
        ToolSpec(
            "ui_type_text_sensitive",
            (
                "Set text in an approval-gated sensitive text surface. Use this "
                "for project/editor mutation; passwords remain unsupported."
            ),
            schema(text_properties, ("element_ref", "text")),
            "ui.type_sensitive",
            "yellow",
            True,
            confirmation_notice=(
                "Hassas bir metin alanı değiştirilecek. Bu, açık bir proje veya "
                "belge içeriğini değiştirebilir."
            ),
        ),
        lambda args: controller.type_text(args, approved_sensitive=True),
    )

    registry.register(
        ToolSpec(
            "ui_hotkey",
            "Send one allow-listed routine navigation hotkey to the selected foreground window.",
            schema({
                "window_ref": {"type": "string", "maxLength": 32},
                "hotkey": {
                    "type": "string",
                    "enum": sorted(ROUTINE_HOTKEYS),
                },
            }, ("window_ref", "hotkey")),
            "ui.hotkey",
        ),
        lambda args: controller.hotkey(args, approved_sensitive=False),
    )
    registry.register(
        ToolSpec(
            "ui_hotkey_sensitive",
            (
                "Send one approval-gated commit/close hotkey such as Enter, "
                "Ctrl+Enter, Ctrl+S, Delete or Alt+F4."
            ),
            schema({
                "window_ref": {"type": "string", "maxLength": 32},
                "hotkey": {
                    "type": "string",
                    "enum": sorted(SENSITIVE_HOTKEYS),
                },
            }, ("window_ref", "hotkey")),
            "ui.hotkey_sensitive",
            "yellow",
            True,
            confirmation_notice=(
                "Bu kısayol gönderme, kaydetme, silme veya pencere kapatma "
                "gibi kalıcı bir etki oluşturabilir."
            ),
        ),
        lambda args: controller.hotkey(args, approved_sensitive=True),
    )
    registry.register(
        ToolSpec(
            "ui_scroll",
            "Page-scroll the currently selected foreground window up or down.",
            schema({
                "window_ref": {"type": "string", "maxLength": 32},
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                },
            }, ("window_ref", "direction")),
            "ui.scroll",
        ),
        controller.scroll,
    )
    registry.register(
        ToolSpec(
            "ui_navigate_https",
            (
                "Navigate the CURRENT foreground supported browser to an exact safe "
                "HTTPS URL using its address bar. Use after wait_for_window + "
                "focus_window for requests like 'open Nebula and go to YouTube'. "
                "This is routine navigation only; it cannot submit forms or enter "
                "passwords, and page load is not automatically verified."
            ),
            schema({
                "url": {"type": "string", "maxLength": 2048},
            }, ("url",)),
            "browser.navigate_foreground",
        ),
        controller.navigate_https,
    )
    registry.register(
        ToolSpec(
            "ui_wait",
            (
                "Wait briefly for a named interactive element to appear in the "
                "foreground window, returning a fresh opaque element_ref."
            ),
            schema({
                "name": {"type": "string", "maxLength": 200},
                "role": {
                    "type": "string",
                    "enum": sorted(ALLOWED_ROLES),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                },
            }, ("name",)),
            "ui.wait",
        ),
        controller.wait,
    )
