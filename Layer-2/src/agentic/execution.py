"""Deterministic execution contracts and ephemeral world state.

Luna chooses semantic tools. Core owns supported technical prerequisites,
recovery, device-local runtime facts and fail-closed behavior.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import time
from typing import Any, Callable


@dataclass(frozen=True)
class WorldFact:
    value: Any
    source: str
    observed_at: float
    expires_at: float


class WorldState:
    """Short-lived verified runtime facts. Never persisted as user memory."""

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._facts: dict[tuple[str, str], WorldFact] = {}

    def set(
        self,
        device_id: str,
        key: str,
        value: Any,
        *,
        source: str,
        ttl_seconds: float = 120,
    ) -> None:
        now = float(self.clock())
        self._facts[(device_id, key)] = WorldFact(
            value=deepcopy(value),
            source=source,
            observed_at=now,
            expires_at=now + max(0.1, float(ttl_seconds)),
        )

    def get(self, device_id: str, key: str, default=None):
        fact = self._facts.get((device_id, key))
        if fact is None:
            return default
        if fact.expires_at <= float(self.clock()):
            self._facts.pop((device_id, key), None)
            return default
        return deepcopy(fact.value)

    def clear(self, device_id: str, key: str) -> None:
        self._facts.pop((device_id, key), None)

    def clear_device(self, device_id: str) -> None:
        for item in [item for item in self._facts if item[0] == device_id]:
            self._facts.pop(item, None)

    def snapshot(self, device_id: str) -> dict:
        result = {}
        for (candidate, key), _fact in list(self._facts.items()):
            if candidate != device_id:
                continue
            value = self.get(device_id, key)
            if value is not None:
                result[key] = value
        return result


@dataclass
class Preparation:
    ok: bool
    recoveries: list[dict]
    blocked_result: dict | None = None


RecoveryRunner = Callable[[str, dict], dict]


class ExecutionEngine:
    """Resolve deterministic prerequisites without teaching Luna OS recipes."""

    def __init__(self, world: WorldState | None = None):
        self.world = world or WorldState()
        self._resolvers = {
            "foreground.latest_app": self._ensure_latest_app_foreground,
            "spotify.authenticated": self._ensure_spotify_authenticated,
        }

    @staticmethod
    def _recovery_summary(tool: str, result: dict) -> dict:
        summary = {
            "tool": tool,
            "ok": bool(result.get("ok")),
        }
        for key in ("status", "error", "outcome"):
            if key in result:
                summary[key] = result[key]
        return summary

    def observe(
        self,
        spec,
        arguments: dict,
        result: dict,
        device_id: str,
    ) -> None:
        """Update only facts that a tool contract explicitly says it can produce."""
        if not isinstance(result, dict):
            return

        effects = set(getattr(spec, "effects", ()) or ())

        if result.get("outcome") == "unknown":
            for effect in effects:
                self.world.clear(device_id, effect)
            return

        if "app.latest_window" in effects:
            window = result.get("window")
            if (
                result.get("ok")
                and result.get("visible_window_verified") is True
                and isinstance(window, dict)
                and window.get("window_id")
                and window.get("app_name")
            ):
                self.world.set(
                    device_id,
                    "app.latest_window",
                    {
                        "window_id": window["window_id"],
                        "app_name": window["app_name"],
                    },
                    source=spec.name,
                    ttl_seconds=float(
                        result.get("selection_ttl_seconds", 120)
                    ),
                )
            elif result.get("ok") is False:
                self.world.clear(device_id, "app.latest_window")

        if "window.foreground" in effects:
            if (
                result.get("ok")
                and result.get("state_verified") is True
                and arguments.get("window_id")
                and arguments.get("app_name")
            ):
                self.world.set(
                    device_id,
                    "window.foreground",
                    {
                        "window_id": arguments["window_id"],
                        "app_name": arguments["app_name"],
                    },
                    source=spec.name,
                    ttl_seconds=120,
                )
            elif result.get("ok") is False:
                self.world.clear(device_id, "window.foreground")

        if "spotify.authenticated" in effects:
            if result.get("ok") and result.get("status") == "connected":
                self.world.set(
                    device_id,
                    "spotify.authenticated",
                    True,
                    source=spec.name,
                    ttl_seconds=300,
                )
            elif result.get("status") in {
                "not_connected",
                "setup_required",
                "awaiting_user_authorization",
            }:
                self.world.clear(device_id, "spotify.authenticated")

        if (
            "spotify.authenticated"
            in set(getattr(spec, "preconditions", ()) or ())
            and result.get("error") == "spotify_not_connected"
        ):
            self.world.clear(device_id, "spotify.authenticated")

    def prepare(
        self,
        spec,
        arguments: dict,
        device_id: str,
        run_recovery: RecoveryRunner,
    ) -> Preparation:
        recoveries: list[dict] = []

        for requirement in getattr(spec, "preconditions", ()) or ():
            resolver = self._resolvers.get(requirement)
            if resolver is None:
                return Preparation(
                    False,
                    recoveries,
                    {
                        "ok": False,
                        "error": "execution_precondition_resolver_missing",
                        "precondition": requirement,
                    },
                )

            ok, blocked, attempted = resolver(
                device_id,
                arguments,
                run_recovery,
            )
            recoveries.extend(attempted)
            if not ok:
                return Preparation(False, recoveries, blocked)

        return Preparation(True, recoveries)

    def _ensure_latest_app_foreground(
        self,
        device_id: str,
        _arguments: dict,
        run_recovery: RecoveryRunner,
    ):
        latest = self.world.get(device_id, "app.latest_window")
        if not isinstance(latest, dict):
            # Soft prerequisite: a user may already have focused a browser by hand.
            # The device-side navigation adapter still validates the foreground app.
            return True, None, []

        current = self.world.get(device_id, "window.foreground")
        if (
            isinstance(current, dict)
            and current.get("window_id") == latest.get("window_id")
        ):
            return True, None, []

        result = run_recovery(
            "focus_window",
            {
                "window_id": latest["window_id"],
                "app_name": latest["app_name"],
                "device_id": device_id,
            },
        )
        attempted = [self._recovery_summary("focus_window", result)]

        if result.get("outcome") == "unknown":
            return (
                False,
                {
                    "ok": False,
                    "outcome": "unknown",
                    "error": "execution_recovery_outcome_unknown",
                    "precondition": "foreground.latest_app",
                },
                attempted,
            )

        if not (
            result.get("ok")
            and result.get("state_verified") is True
        ):
            return (
                False,
                {
                    "ok": False,
                    "error": "execution_precondition_failed",
                    "precondition": "foreground.latest_app",
                    "recovery_tool": "focus_window",
                },
                attempted,
            )

        return True, None, attempted

    def _ensure_spotify_authenticated(
        self,
        device_id: str,
        _arguments: dict,
        run_recovery: RecoveryRunner,
    ):
        if self.world.get(device_id, "spotify.authenticated") is True:
            return True, None, []

        result = run_recovery(
            "spotify_connection_status",
            {"device_id": device_id},
        )
        attempted = [
            self._recovery_summary(
                "spotify_connection_status",
                result,
            )
        ]

        if result.get("outcome") == "unknown":
            return (
                False,
                {
                    "ok": False,
                    "outcome": "unknown",
                    "error": "execution_recovery_outcome_unknown",
                    "precondition": "spotify.authenticated",
                },
                attempted,
            )

        if self.world.get(device_id, "spotify.authenticated") is True:
            return True, None, attempted

        return (
            False,
            {
                "ok": False,
                "error": "execution_precondition_missing",
                "precondition": "spotify.authenticated",
                "recovery_tool": "spotify_connect",
                "observed_status": result.get("status"),
            },
            attempted,
        )
