"""Deterministic permission policy; models never grant permissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RiskClass(str, Enum):
    GREEN = "green"       # safe, automatic
    YELLOW = "yellow"     # context-dependent, explicit confirmation
    RED = "red"           # destructive/privileged, explicit confirmation only


# User-facing approval policy.
#
# Routine desktop control and read-only inspection should feel immediate. The
# tool's intrinsic risk metadata is still kept on ToolSpec for auditing, but
# these capabilities are explicitly allowed without a second confirmation when
# the user has already asked EPIS to do the action.
AUTO_CONFIRM_CAPABILITIES = frozenset({
    "app.open",
    "app.close",
    "apps.discover",
    "apps.launch",
    "audio.volume",
    "audio.mute",
    "audio.status",
    "media.play_pause",
    "media.next",
    "media.previous",
    "media.sessions",
    "media.control",
    "media.spotify_search",
    "spotify.auth_status",
    "spotify.search",
    "spotify.devices",
    "spotify.current",
    "spotify.play",
    "spotify.pause",
    "ui.inspect",
    "ui.click",
    "ui.type",
    "ui.hotkey",
    "ui.scroll",
    "ui.wait",
    "system.info",
    "system.battery",
    "system.disk",
    "system.settings",
    "browser.open_url",
    "browser.navigate_foreground",
    "display.brightness_read",
    "display.brightness_set",
    "windows.list",
    "windows.wait",
    "windows.focus",
    "windows.minimize",
    "windows.maximize",
    "windows.restore",
    "windows.close",
    "files.list",
    "files.info",
    "files.read_text",
    "files.open_folder",
})


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    requires_confirmation: bool
    reason: str


class PermissionEngine:
    """Small, auditable policy boundary for every dispatched tool call."""

    def decide(self, tool, arguments: dict) -> PermissionDecision:
        try:
            risk = RiskClass(tool.risk_class)
        except ValueError:
            return PermissionDecision(False, False, "unknown risk class")

        if tool.capability in AUTO_CONFIRM_CAPABILITIES:
            return PermissionDecision(True, False, "routine user-directed capability")

        if risk is RiskClass.GREEN and not tool.confirmation_required:
            return PermissionDecision(True, False, "green tool")
        if risk is RiskClass.YELLOW:
            return PermissionDecision(True, True, "yellow tool requires confirmation")
        # Red tools remain available only behind a user-owned confirmation step.
        return PermissionDecision(True, True, "red tool requires explicit confirmation")
