"""Deterministic permission policy; models never grant permissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RiskClass(str, Enum):
    GREEN = "green"       # safe, automatic
    YELLOW = "yellow"     # context-dependent, explicit confirmation
    RED = "red"           # destructive/privileged, explicit confirmation only


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
        if risk is RiskClass.GREEN and not tool.confirmation_required:
            return PermissionDecision(True, False, "green tool")
        if risk is RiskClass.YELLOW:
            return PermissionDecision(True, True, "yellow tool requires confirmation")
        # Red tools remain available only behind a user-owned confirmation step.
        return PermissionDecision(True, True, "red tool requires explicit confirmation")
