"""Window-bound screenshot and input primitives for Computer Use.

These tools are intentionally hidden from Luna. The semantic Computer Use provider
owns the loop; the device runtime only exposes frame-bound, validated primitives.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import math
import os
import time
import uuid


_ALLOWED_ACTIONS = frozenset({
    "click",
    "double_click",
    "scroll",
    "type",
    "wait",
    "keypress",
    "drag",
    "move",
    "screenshot",
})

_MODIFIERS = frozenset({
    "CTRL",
    "CONTROL",
    "ALT",
    "OPTION",
    "SHIFT",
    "META",
    "CMD",
    "COMMAND",
})

_SENSITIVE_PROCESSES = frozenset({
    "consent.exe",
    "credentialuibroker.exe",
    "logonui.exe",
    "winlogon.exe",
})

_KEY_ALIASES = {
    "RETURN": "ENTER",
    "ESCAPE": "ESC",
    "DEL": "DELETE",
    "UP": "ARROWUP",
    "DOWN": "ARROWDOWN",
    "LEFT": "ARROWLEFT",
    "RIGHT": "ARROWRIGHT",
    "CONTROL": "CTRL",
    "OPTION": "ALT",
    "CMD": "META",
    "COMMAND": "META",
}


@dataclass
class FrameSnapshot:
    frame_id: str
    window_id: str
    app_name: str
    pid: int
    rect: tuple[int, int, int, int]
    width: int
    height: int
    expires_at: float


class WindowsComputerBackend:
    """Small Win32/Pillow adapter; policy stays in ComputerDeviceController."""

    MAX_IMAGE_BYTES = 900_000

    @staticmethod
    def window_rect(row) -> tuple[int, int, int, int]:
        import win32gui

        rect = tuple(int(v) for v in win32gui.GetWindowRect(row["hwnd"]))
        if len(rect) != 4:
            raise RuntimeError("invalid_window_rect")
        left, top, right, bottom = rect
        if right - left < 64 or bottom - top < 64:
            raise RuntimeError("window_too_small")
        return rect

    def capture_window(self, row) -> dict:
        from io import BytesIO

        from PIL import ImageGrab

        rect = self.window_rect(row)
        left, top, right, bottom = rect
        original_width = right - left
        original_height = bottom - top

        image = ImageGrab.grab(
            bbox=rect,
            all_screens=True,
        ).convert("RGB")

        image.thumbnail((1280, 900))

        payload = b""
        for quality in (65, 55, 45):
            buffer = BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
            )
            payload = buffer.getvalue()
            if len(payload) <= self.MAX_IMAGE_BYTES:
                break

        if len(payload) > self.MAX_IMAGE_BYTES:
            image.thumbnail((960, 720))
            buffer = BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=42,
                optimize=True,
            )
            payload = buffer.getvalue()

        if len(payload) > self.MAX_IMAGE_BYTES:
            raise RuntimeError("computer_screenshot_too_large")

        return {
            "bytes": payload,
            "mime_type": "image/jpeg",
            "width": int(image.width),
            "height": int(image.height),
            "window_width": original_width,
            "window_height": original_height,
            "rect": rect,
        }

    @staticmethod
    def foreground_info() -> dict:
        import psutil
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow() or 0)
        if not hwnd:
            return {
                "hwnd": 0,
                "pid": 0,
                "process": "",
            }

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            return {
                "hwnd": hwnd,
                "pid": 0,
                "process": "",
            }

        return {
            "hwnd": hwnd,
            "pid": int(pid),
            "process": str(process),
        }

    @staticmethod
    def password_field_focused() -> bool | None:
        try:
            import uiautomation as auto

            control = auto.GetFocusedControl()
            if control is None:
                return None
            return bool(
                getattr(control, "IsPassword", False)
            )
        except Exception:
            return None

    @staticmethod
    def _mouse_button(button: str):
        import win32con

        table = {
            "left": (
                win32con.MOUSEEVENTF_LEFTDOWN,
                win32con.MOUSEEVENTF_LEFTUP,
            ),
            "right": (
                win32con.MOUSEEVENTF_RIGHTDOWN,
                win32con.MOUSEEVENTF_RIGHTUP,
            ),
            "wheel": (
                win32con.MOUSEEVENTF_MIDDLEDOWN,
                win32con.MOUSEEVENTF_MIDDLEUP,
            ),
        }
        return table[button]

    @staticmethod
    def _vk(name: str) -> int:
        import win32con

        aliases = {
            "CTRL": win32con.VK_CONTROL,
            "ALT": win32con.VK_MENU,
            "SHIFT": win32con.VK_SHIFT,
            "META": win32con.VK_LWIN,
            "ENTER": win32con.VK_RETURN,
            "ESC": win32con.VK_ESCAPE,
            "TAB": win32con.VK_TAB,
            "SPACE": win32con.VK_SPACE,
            "BACKSPACE": win32con.VK_BACK,
            "DELETE": win32con.VK_DELETE,
            "HOME": win32con.VK_HOME,
            "END": win32con.VK_END,
            "PAGEUP": win32con.VK_PRIOR,
            "PAGEDOWN": win32con.VK_NEXT,
            "ARROWUP": win32con.VK_UP,
            "ARROWDOWN": win32con.VK_DOWN,
            "ARROWLEFT": win32con.VK_LEFT,
            "ARROWRIGHT": win32con.VK_RIGHT,
        }
        if name in aliases:
            return aliases[name]
        if len(name) == 1 and name.isascii() and name.isalnum():
            return ord(name.upper())
        if (
            name.startswith("F")
            and name[1:].isdigit()
            and 1 <= int(name[1:]) <= 24
        ):
            return win32con.VK_F1 + int(name[1:]) - 1
        raise ValueError(f"unsupported_key:{name}")

    def hold_modifiers(self, keys: list[str]):
        import win32api

        pressed = []
        try:
            for key in keys:
                vk = self._vk(key)
                win32api.keybd_event(vk, 0, 0, 0)
                pressed.append(vk)
            yield
        finally:
            import win32con

            for vk in reversed(pressed):
                win32api.keybd_event(
                    vk,
                    0,
                    win32con.KEYEVENTF_KEYUP,
                    0,
                )

    @staticmethod
    def move(x: int, y: int) -> None:
        import win32api

        win32api.SetCursorPos((int(x), int(y)))

    def click(
        self,
        x: int,
        y: int,
        button: str,
        *,
        double: bool = False,
        modifiers: list[str] | None = None,
    ) -> None:
        import win32api

        self.move(x, y)
        down, up = self._mouse_button(button)
        keys = list(modifiers or [])

        from contextlib import contextmanager

        @contextmanager
        def held():
            import win32con

            pressed = []
            try:
                for key in keys:
                    vk = self._vk(key)
                    win32api.keybd_event(vk, 0, 0, 0)
                    pressed.append(vk)
                yield
            finally:
                for vk in reversed(pressed):
                    win32api.keybd_event(
                        vk,
                        0,
                        win32con.KEYEVENTF_KEYUP,
                        0,
                    )

        with held():
            repeats = 2 if double else 1
            for _ in range(repeats):
                win32api.mouse_event(down, 0, 0, 0, 0)
                win32api.mouse_event(up, 0, 0, 0, 0)
                if double:
                    time.sleep(0.05)

    def drag(
        self,
        path: list[tuple[int, int]],
        *,
        modifiers: list[str] | None = None,
    ) -> None:
        import win32api
        import win32con
        from contextlib import contextmanager

        keys = list(modifiers or [])

        @contextmanager
        def held():
            pressed = []
            try:
                for key in keys:
                    vk = self._vk(key)
                    win32api.keybd_event(vk, 0, 0, 0)
                    pressed.append(vk)
                yield
            finally:
                for vk in reversed(pressed):
                    win32api.keybd_event(
                        vk,
                        0,
                        win32con.KEYEVENTF_KEYUP,
                        0,
                    )

        with held():
            self.move(*path[0])
            win32api.mouse_event(
                win32con.MOUSEEVENTF_LEFTDOWN,
                0,
                0,
                0,
                0,
            )
            try:
                for x, y in path[1:]:
                    self.move(x, y)
                    time.sleep(0.01)
            finally:
                win32api.mouse_event(
                    win32con.MOUSEEVENTF_LEFTUP,
                    0,
                    0,
                    0,
                    0,
                )

    def scroll(
        self,
        x: int,
        y: int,
        scroll_x: int,
        scroll_y: int,
        *,
        modifiers: list[str] | None = None,
    ) -> None:
        import win32api
        import win32con
        from contextlib import contextmanager

        self.move(x, y)
        keys = list(modifiers or [])

        @contextmanager
        def held():
            pressed = []
            try:
                for key in keys:
                    vk = self._vk(key)
                    win32api.keybd_event(vk, 0, 0, 0)
                    pressed.append(vk)
                yield
            finally:
                for vk in reversed(pressed):
                    win32api.keybd_event(
                        vk,
                        0,
                        win32con.KEYEVENTF_KEYUP,
                        0,
                    )

        def wheel_delta(value: int) -> int:
            if not value:
                return 0
            clicks = max(1, abs(round(value / 100)))
            return clicks * 120 * (1 if value > 0 else -1)

        with held():
            if scroll_y:
                # Computer-tool positive Y means down; Win32 positive wheel is up.
                win32api.mouse_event(
                    win32con.MOUSEEVENTF_WHEEL,
                    0,
                    0,
                    -wheel_delta(scroll_y),
                    0,
                )
            if scroll_x:
                win32api.mouse_event(
                    getattr(win32con, "MOUSEEVENTF_HWHEEL", 0x01000),
                    0,
                    0,
                    wheel_delta(scroll_x),
                    0,
                )

    def keypress(self, keys: list[str]) -> None:
        import win32api
        import win32con

        pressed = []
        try:
            for key in keys:
                vk = self._vk(key)
                win32api.keybd_event(vk, 0, 0, 0)
                pressed.append(vk)
        finally:
            for vk in reversed(pressed):
                win32api.keybd_event(
                    vk,
                    0,
                    win32con.KEYEVENTF_KEYUP,
                    0,
                )

    @staticmethod
    def type_text(text: str) -> None:
        """Type Unicode without using or reading the clipboard."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        ULONG_PTR = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [
                ("type", wintypes.DWORD),
                ("u", _INPUTUNION),
            ]

        units = text.encode(
            "utf-16-le",
            "surrogatepass",
        )
        for index in range(0, len(units), 2):
            code = int.from_bytes(
                units[index:index + 2],
                "little",
            )
            down = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(
                    0,
                    code,
                    KEYEVENTF_UNICODE,
                    0,
                    0,
                ),
            )
            up = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(
                    0,
                    code,
                    KEYEVENTF_UNICODE
                    | KEYEVENTF_KEYUP,
                    0,
                    0,
                ),
            )
            sent = user32.SendInput(
                2,
                (INPUT * 2)(down, up),
                ctypes.sizeof(INPUT),
            )
            if sent != 2:
                raise OSError("SendInput failed")


class ComputerDeviceController:
    FRAME_TTL_SECONDS = 300.0

    def __init__(
        self,
        windows,
        backend=None,
        *,
        clock=time.monotonic,
    ):
        self.windows = windows
        self.backend = backend or WindowsComputerBackend()
        self.clock = clock
        self.frames: dict[str, FrameSnapshot] = {}

    @staticmethod
    def _normalize_key(value) -> str:
        if not isinstance(value, str):
            raise ValueError("key names must be strings")
        name = value.strip().upper()
        name = _KEY_ALIASES.get(name, name)
        if not name or len(name) > 32:
            raise ValueError("invalid key name")
        if (
            name in {
                "CTRL",
                "ALT",
                "SHIFT",
                "META",
                "ENTER",
                "ESC",
                "TAB",
                "SPACE",
                "BACKSPACE",
                "DELETE",
                "HOME",
                "END",
                "PAGEUP",
                "PAGEDOWN",
                "ARROWUP",
                "ARROWDOWN",
                "ARROWLEFT",
                "ARROWRIGHT",
            }
            or (
                len(name) == 1
                and name.isascii()
                and name.isalnum()
            )
            or (
                name.startswith("F")
                and name[1:].isdigit()
                and 1 <= int(name[1:]) <= 24
            )
        ):
            return name
        raise ValueError(f"unsupported key: {value}")

    def _modifiers(self, raw) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 4:
            raise ValueError("invalid modifier keys")
        result = []
        for value in raw:
            key = self._normalize_key(value)
            if key not in {"CTRL", "ALT", "SHIFT", "META"}:
                raise ValueError("mouse modifiers must be CTRL/ALT/SHIFT/META")
            if key not in result:
                result.append(key)
        return result

    @staticmethod
    def _number(value, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"{name} must be a finite number")
        return float(value)

    def _point(
        self,
        raw_x,
        raw_y,
        frame: FrameSnapshot,
    ) -> tuple[int, int]:
        x = self._number(raw_x, "x")
        y = self._number(raw_y, "y")
        if not (
            0 <= x < frame.width
            and 0 <= y < frame.height
        ):
            raise ValueError("computer coordinate outside captured frame")

        left, top, right, bottom = frame.rect
        window_width = right - left
        window_height = bottom - top
        screen_x = left + round(
            x * window_width / frame.width
        )
        screen_y = top + round(
            y * window_height / frame.height
        )
        return screen_x, screen_y

    def _normalize_actions(
        self,
        raw_actions,
        frame: FrameSnapshot,
    ) -> list[dict]:
        if (
            not isinstance(raw_actions, list)
            or not raw_actions
            or len(raw_actions) > 64
        ):
            raise ValueError("invalid computer action batch")

        normalized = []
        for raw in raw_actions:
            if not isinstance(raw, dict):
                raise ValueError("computer actions must be objects")

            action_type = raw.get("type")
            if action_type not in _ALLOWED_ACTIONS:
                raise ValueError("unsupported computer action")

            item = {"type": action_type}

            if action_type in {
                "click",
                "double_click",
                "move",
                "scroll",
            }:
                item["point"] = self._point(
                    raw.get("x"),
                    raw.get("y"),
                    frame,
                )
                item["modifiers"] = self._modifiers(
                    raw.get("keys")
                )

            if action_type in {
                "click",
                "double_click",
            }:
                button = str(
                    raw.get("button", "left")
                ).lower()
                if button not in {
                    "left",
                    "right",
                    "wheel",
                }:
                    raise ValueError("unsupported mouse button")
                item["button"] = button

            elif action_type == "scroll":
                scroll_x = self._number(
                    raw.get("scroll_x", 0),
                    "scroll_x",
                )
                scroll_y = self._number(
                    raw.get("scroll_y", 0),
                    "scroll_y",
                )
                if (
                    abs(scroll_x) > 5000
                    or abs(scroll_y) > 5000
                ):
                    raise ValueError("scroll delta too large")
                item["scroll_x"] = int(scroll_x)
                item["scroll_y"] = int(scroll_y)

            elif action_type == "keypress":
                keys = raw.get("keys")
                if (
                    not isinstance(keys, list)
                    or not keys
                    or len(keys) > 8
                ):
                    raise ValueError("invalid keypress")
                item["keys"] = [
                    self._normalize_key(key)
                    for key in keys
                ]

            elif action_type == "type":
                text = raw.get("text")
                if (
                    not isinstance(text, str)
                    or len(text) > 16000
                    or "\x00" in text
                ):
                    raise ValueError("invalid type action")
                item["text"] = text

            elif action_type == "drag":
                path = raw.get("path")
                if (
                    not isinstance(path, list)
                    or len(path) < 2
                    or len(path) > 128
                ):
                    raise ValueError("invalid drag path")
                points = []
                for point in path:
                    if (
                        isinstance(point, dict)
                        and "x" in point
                        and "y" in point
                    ):
                        x, y = point["x"], point["y"]
                    elif (
                        isinstance(point, (list, tuple))
                        and len(point) >= 2
                    ):
                        x, y = point[0], point[1]
                    else:
                        raise ValueError("invalid drag point")
                    points.append(
                        self._point(
                            x,
                            y,
                            frame,
                        )
                    )
                item["path"] = points
                item["modifiers"] = self._modifiers(
                    raw.get("keys")
                )

            normalized.append(item)

        return normalized

    def _resolve(
        self,
        arguments: dict,
        *,
        refresh_ttl: bool = True,
    ):
        return self.windows.resolve_selection(
            {
                "window_id": arguments["window_id"],
                "app_name": arguments["app_name"],
            },
            refresh_ttl=refresh_ttl,
        )

    def capture(self, arguments: dict) -> dict:
        row, error = self._resolve(arguments)
        if error:
            return error
        if row.get("minimized"):
            return {
                "ok": False,
                "error": "computer_target_window_is_minimized",
            }

        try:
            captured = self.backend.capture_window(row)
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    "computer_screenshot_failed:"
                    + type(exc).__name__
                ),
            }

        payload = captured["bytes"]
        digest = hashlib.sha256(payload).hexdigest()
        frame_id = uuid.uuid4().hex
        snapshot = FrameSnapshot(
            frame_id=frame_id,
            window_id=arguments["window_id"],
            app_name=arguments["app_name"],
            pid=int(row["pid"]),
            rect=tuple(captured["rect"]),
            width=int(captured["width"]),
            height=int(captured["height"]),
            expires_at=(
                self.clock()
                + self.FRAME_TTL_SECONDS
            ),
        )

        # Keep only a tiny recent frame cache; frame IDs are one-session
        # coordination tokens, not persistent screenshot history.
        self.frames = {
            key: value
            for key, value in self.frames.items()
            if value.expires_at >= self.clock()
        }
        self.frames[frame_id] = snapshot
        if len(self.frames) > 8:
            oldest = sorted(
                self.frames.values(),
                key=lambda item: item.expires_at,
            )[:-8]
            for item in oldest:
                self.frames.pop(item.frame_id, None)

        return {
            "ok": True,
            "status": "computer_screenshot_captured",
            "frame_id": frame_id,
            "mime_type": captured["mime_type"],
            "image_base64": base64.b64encode(
                payload
            ).decode("ascii"),
            "width": snapshot.width,
            "height": snapshot.height,
            "sha256": digest,
        }

    def _foreground_guard(
        self,
        row: dict,
    ) -> dict | None:
        try:
            foreground = self.backend.foreground_info()
        except Exception:
            return {
                "ok": False,
                "error": "computer_foreground_unavailable",
            }

        process_name = str(
            foreground.get("process")
            or ""
        ).casefold()
        if process_name in _SENSITIVE_PROCESSES:
            return {
                "ok": False,
                "error": "computer_sensitive_system_surface_blocked",
            }

        if int(foreground.get("pid") or 0) != int(row["pid"]):
            return {
                "ok": False,
                "error": "computer_target_window_lost_foreground",
            }

        return None

    def apply_actions(
        self,
        arguments: dict,
    ) -> dict:
        frame_id = arguments["frame_id"]
        frame = self.frames.get(frame_id)
        if (
            frame is None
            or frame.expires_at < self.clock()
        ):
            return {
                "ok": False,
                "error": "computer_frame_expired",
            }
        if (
            frame.window_id != arguments["window_id"]
            or frame.app_name != arguments["app_name"]
        ):
            return {
                "ok": False,
                "error": "computer_frame_window_mismatch",
            }

        row, error = self._resolve(arguments)
        if error:
            return error
        if int(row["pid"]) != frame.pid:
            return {
                "ok": False,
                "error": "computer_target_identity_changed",
            }

        try:
            if tuple(
                self.backend.window_rect(row)
            ) != frame.rect:
                return {
                    "ok": False,
                    "error": "computer_frame_geometry_changed",
                }
            actions = self._normalize_actions(
                arguments["actions"],
                frame,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    "computer_action_validation_failed:"
                    + type(exc).__name__
                ),
            }

        executed = 0
        try:
            for action in actions:
                action_type = action["type"]

                if action_type == "screenshot":
                    continue

                if action_type == "wait":
                    time.sleep(2.0)
                    executed += 1
                    continue

                guard = self._foreground_guard(row)
                if guard:
                    return {
                        **guard,
                        "actions_executed": executed,
                    }

                if action_type in {
                    "type",
                    "keypress",
                }:
                    password_state = (
                        self.backend.password_field_focused()
                    )
                    if password_state is not False:
                        return {
                            "ok": False,
                            "error": (
                                "computer_password_field_blocked"
                                if password_state is True
                                else "computer_password_field_state_unknown"
                            ),
                            "actions_executed": executed,
                        }

                if action_type == "click":
                    self.backend.click(
                        *action["point"],
                        action["button"],
                        modifiers=action["modifiers"],
                    )
                elif action_type == "double_click":
                    self.backend.click(
                        *action["point"],
                        action["button"],
                        double=True,
                        modifiers=action["modifiers"],
                    )
                elif action_type == "move":
                    self.backend.move(
                        *action["point"]
                    )
                elif action_type == "scroll":
                    self.backend.scroll(
                        *action["point"],
                        action["scroll_x"],
                        action["scroll_y"],
                        modifiers=action["modifiers"],
                    )
                elif action_type == "drag":
                    self.backend.drag(
                        action["path"],
                        modifiers=action["modifiers"],
                    )
                elif action_type == "keypress":
                    self.backend.keypress(
                        action["keys"]
                    )
                elif action_type == "type":
                    self.backend.type_text(
                        action["text"]
                    )
                else:
                    return {
                        "ok": False,
                        "error": "unsupported computer action",
                        "actions_executed": executed,
                    }

                executed += 1

        except Exception as exc:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": (
                    "computer_input_failed:"
                    + type(exc).__name__
                ),
                "actions_executed": executed,
            }

        return {
            "ok": True,
            "status": "computer_actions_applied",
            "actions_executed": executed,
            "screenshot_required": True,
        }


def register_computer_device_tools(
    registry,
    windows,
):
    from .tools import ToolSpec

    controller = ComputerDeviceController(
        windows
    )
    device = {
        "device_id": {
            "type": "string",
        }
    }

    capture_schema = {
        "type": "object",
        "properties": {
            "window_id": {
                "type": "string",
                "maxLength": 32,
            },
            "app_name": {
                "type": "string",
                "maxLength": 100,
            },
            **device,
        },
        "required": [
            "window_id",
            "app_name",
        ],
        "additionalProperties": False,
    }

    action_schema = {
        "type": "object",
        "properties": {
            "window_id": {
                "type": "string",
                "maxLength": 32,
            },
            "app_name": {
                "type": "string",
                "maxLength": 100,
            },
            "frame_id": {
                "type": "string",
                "maxLength": 32,
            },
            "actions": {
                "type": "array",
                "maxItems": 64,
            },
            **device,
        },
        "required": [
            "window_id",
            "app_name",
            "frame_id",
            "actions",
        ],
        "additionalProperties": False,
    }

    registry.register(
        ToolSpec(
            "computer_capture_screen",
            (
                "Internal Computer Use screenshot primitive. "
                "Never expose directly to Luna."
            ),
            capture_schema,
            "computer.capture",
            model_visible=False,
        ),
        controller.capture,
    )
    registry.register(
        ToolSpec(
            "computer_apply_actions",
            (
                "Internal frame-bound Computer Use input primitive. "
                "Never expose directly to Luna."
            ),
            action_schema,
            "computer.actions",
            model_visible=False,
        ),
        controller.apply_actions,
    )

    return controller
