"""Semantic OpenAI Computer Use provider for EPIS.

The provider owns the screenshot/action loop. Luna supplies only a goal and an
optional target app name; Core owns app discovery, launch, focus, routing and
the hidden device primitives.
"""

from __future__ import annotations

import json
import logging
import os
from time import perf_counter
import uuid


logger = logging.getLogger("EPIS.AGENT")


class CoreComputerBridge:
    """Bind a Computer Use session to one verified EPIS device/window."""

    REQUIRED_CAPABILITIES = frozenset({
        "computer.capture",
        "computer.actions",
        "apps.discover",
        "apps.launch",
        "windows.focus",
    })

    def __init__(self, core):
        self.core = core

    def _capable_devices(self):
        result = []
        for transport in list(
            self.core.transports.values()
        ):
            try:
                transport.refresh()
            except Exception:
                continue
            device = transport.device
            if (
                device.online
                and self.REQUIRED_CAPABILITIES.issubset(
                    device.capabilities
                )
            ):
                result.append(device)
        return result

    def available(self) -> bool:
        return bool(self._capable_devices())

    def _select_device(self):
        candidates = self._capable_devices()
        if not candidates:
            return None, {
                "ok": False,
                "error": "computer_device_unavailable",
            }

        with_window = [
            device
            for device in candidates
            if self.core.execution.world.get(
                device.device_id,
                "app.latest_window",
            )
            is not None
        ]
        if len(with_window) == 1:
            return with_window[0], None

        local = self.core.local_agent.device
        if (
            local.online
            and any(
                item.device_id == local.device_id
                for item in candidates
            )
        ):
            return local, None

        if len(candidates) == 1:
            return candidates[0], None

        return None, {
            "ok": False,
            "error": "computer_device_ambiguous",
            "devices": [
                {
                    "device_id": item.device_id,
                    "name": item.display_name,
                }
                for item in candidates
            ],
        }

    def _run_tool(
        self,
        name: str,
        arguments: dict,
    ) -> dict:
        from .luna import ToolCall

        turn = self.core._dispatch(
            ToolCall(
                (
                    "computer-internal-"
                    + uuid.uuid4().hex
                ),
                name,
                arguments,
            ),
            confirmed=False,
            allow_recovery=False,
        )
        if turn.confirmation_required:
            return {
                "ok": False,
                "error": (
                    "computer_internal_action_"
                    "unexpectedly_requires_confirmation"
                ),
            }
        if not turn.tool_results:
            return {
                "ok": False,
                "error": "computer_internal_action_no_result",
            }
        return dict(turn.tool_results[-1])

    @staticmethod
    def _choose_app(
        apps: list[dict],
        target_app: str,
    ):
        if len(apps) == 1:
            return apps[0], None

        exact = [
            item
            for item in apps
            if str(
                item.get("app_name")
                or ""
            ).casefold()
            == target_app.casefold()
        ]
        if len(exact) == 1:
            return exact[0], None

        return None, {
            "ok": False,
            "error": "computer_target_app_ambiguous",
            "candidates": [
                str(
                    item.get("app_name")
                    or ""
                )
                for item in apps[:10]
            ],
        }

    def prepare(
        self,
        target_app: str | None,
    ):
        device, error = self._select_device()
        if error:
            return None, error

        device_id = device.device_id
        target_app = str(
            target_app or ""
        ).strip()

        if target_app:
            discovered = self._run_tool(
                "discover_apps",
                {
                    "query": target_app,
                    "device_id": device_id,
                },
            )
            if not discovered.get("ok"):
                return None, discovered

            apps = discovered.get("apps")
            if not isinstance(apps, list) or not apps:
                return None, {
                    "ok": False,
                    "error": "computer_target_app_not_found",
                }

            chosen, error = self._choose_app(
                apps,
                target_app,
            )
            if error:
                return None, error

            launched = self._run_tool(
                "launch_discovered_app",
                {
                    "app_id": chosen["app_id"],
                    "app_name": chosen["app_name"],
                    "device_id": device_id,
                },
            )
            if not launched.get("ok"):
                return None, launched

            if (
                launched.get(
                    "visible_window_verified"
                )
                is not True
            ):
                return None, {
                    "ok": False,
                    "error": (
                        "computer_target_window_"
                        "not_verified_after_launch"
                    ),
                }

        latest = self.core.execution.world.get(
            device_id,
            "app.latest_window",
        )
        if not isinstance(latest, dict):
            return None, {
                "ok": False,
                "error": "computer_target_window_unavailable",
            }

        focused = self._run_tool(
            "focus_window",
            {
                "window_id": latest["window_id"],
                "app_name": latest["app_name"],
                "device_id": device_id,
            },
        )
        if not (
            focused.get("ok")
            and focused.get(
                "state_verified"
            )
            is True
        ):
            return None, {
                "ok": False,
                "error": "computer_target_focus_failed",
            }

        return {
            "device_id": device_id,
            "window_id": latest["window_id"],
            "app_name": latest["app_name"],
            "target_app": target_app or None,
        }, None

    def capture(
        self,
        session: dict,
    ) -> dict:
        return self._run_tool(
            "computer_capture_screen",
            {
                "window_id": session["window_id"],
                "app_name": session["app_name"],
                "device_id": session["device_id"],
            },
        )

    def apply_actions(
        self,
        session: dict,
        frame: dict,
        actions: list[dict],
    ) -> dict:
        return self._run_tool(
            "computer_apply_actions",
            {
                "window_id": session["window_id"],
                "app_name": session["app_name"],
                "frame_id": frame["frame_id"],
                "actions": actions,
                "device_id": session["device_id"],
            },
        )


class OpenAIComputerUseProvider:
    provider_id = "openai-computer"
    display_name = "OpenAI Computer Use"
    priority = 50

    def __init__(
        self,
        bridge: CoreComputerBridge,
        *,
        api_key: str | None = None,
        model: str | None = None,
        usage_repository=None,
        client_factory=None,
    ):
        self.bridge = bridge
        self.api_key = (
            api_key
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("LUNA_API_KEY")
        )
        self.model = (
            model
            or os.getenv(
                "EPIS_COMPUTER_MODEL",
                "gpt-5.6-sol",
            )
        )
        self.usage_repository = usage_repository
        self.client_factory = client_factory

    def available(self) -> bool:
        return bool(
            self.api_key
            and self.bridge.available()
        )

    def capabilities(self) -> set[str]:
        return (
            {"computer.execute"}
            if self.available()
            else set()
        )

    def close(self) -> None:
        return None

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory()

        from openai import OpenAI

        return OpenAI(
            api_key=self.api_key,
            timeout=180.0,
            max_retries=1,
        )

    @staticmethod
    def _dump_item(item):
        if isinstance(item, dict):
            return dict(item)
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            return dump(
                exclude_none=True
            )
        raise TypeError("unsupported response output item")

    @classmethod
    def _computer_call(cls, response):
        for item in getattr(
            response,
            "output",
            [],
        ) or []:
            data = cls._dump_item(item)
            if data.get("type") == "computer_call":
                return data
        return None

    @staticmethod
    def _output_text(response) -> str:
        return str(
            getattr(
                response,
                "output_text",
                "",
            )
            or ""
        ).strip()

    def _record_usage(
        self,
        response,
        latency_ms: int,
    ) -> None:
        if self.usage_repository is None:
            return
        try:
            self.usage_repository.record_openai_response(
                request_kind=(
                    "capability:computer.execute"
                ),
                model=self.model,
                usage=getattr(
                    response,
                    "usage",
                    None,
                ),
                latency_ms=latency_ms,
            )
        except Exception as exc:
            logger.warning(
                "Computer Use usage recording failed: %s",
                type(exc).__name__,
            )

    def _response(
        self,
        client,
        history: list,
    ):
        started = perf_counter()
        response = client.responses.create(
            model=self.model,
            store=False,
            tools=[
                {
                    "type": "computer",
                }
            ],
            include=[
                "reasoning.encrypted_content",
            ],
            instructions=(
                "You are the visual execution sub-agent inside EPIS. "
                "Operate only toward the explicit goal. Request a screenshot "
                "before your first input action. Treat the screenshot coordinate "
                "space as the only clickable region. Do not enter passwords, "
                "approve UAC/elevation, change security settings, purchase, "
                "delete data, or take a consequential action that is not "
                "explicitly requested. If the task becomes blocked, stop and "
                "briefly explain why. When the goal is visually complete, stop "
                "calling the computer tool."
            ),
            input=history,
        )
        self._record_usage(
            response,
            round(
                (perf_counter() - started)
                * 1000
            ),
        )
        return response

    @staticmethod
    def _safe_actions(call: dict):
        checks = call.get(
            "pending_safety_checks"
        )
        if checks:
            return None, {
                "ok": False,
                "error": "computer_provider_safety_check_required",
                "safety_checks": checks,
            }

        actions = call.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
        ):
            return None, {
                "ok": False,
                "error": "computer_call_has_no_actions",
            }

        if len(actions) > 64:
            return None, {
                "ok": False,
                "error": "computer_action_batch_too_large",
            }

        normalized = []
        for action in actions:
            if not isinstance(action, dict):
                return None, {
                    "ok": False,
                    "error": "invalid_computer_action",
                }
            normalized.append(dict(action))

        return normalized, None

    @staticmethod
    def _screenshot_output(
        call_id: str,
        frame: dict,
    ) -> dict:
        return {
            "type": "computer_call_output",
            "call_id": call_id,
            "output": {
                "type": "computer_screenshot",
                "image_url": (
                    "data:"
                    + frame["mime_type"]
                    + ";base64,"
                    + frame["image_base64"]
                ),
                "detail": "original",
            },
        }

    def execute(
        self,
        capability: str,
        arguments: dict,
    ) -> dict:
        if capability != "computer.execute":
            return {
                "ok": False,
                "error": "capability_not_supported",
            }
        if not self.available():
            return {
                "ok": False,
                "error": "provider_unavailable",
            }

        goal = str(
            arguments.get("goal")
            or ""
        ).strip()
        target_app = str(
            arguments.get("target_app")
            or ""
        ).strip() or None
        if not goal:
            return {
                "ok": False,
                "error": "computer_goal_is_empty",
            }

        session, error = self.bridge.prepare(
            target_app
        )
        if error:
            return error

        client = self._client()
        history = [
            {
                "role": "user",
                "content": (
                    "Complete this GUI goal on the already selected target "
                    "window. Do not leave that application unless the goal "
                    "explicitly requires it.\n\nGoal:\n"
                    + goal
                ),
            }
        ]

        current_frame = None
        previous_digest = None
        previous_signature = None
        no_progress_repeats = 0
        action_batches = 0

        try:
            response = self._response(
                client,
                history,
            )

            while True:
                call = self._computer_call(
                    response
                )
                if call is None:
                    status = getattr(
                        response,
                        "status",
                        None,
                    )
                    text = self._output_text(
                        response
                    )
                    if status not in {
                        None,
                        "completed",
                    }:
                        return {
                            "ok": False,
                            "error": (
                                "computer_model_response_"
                                "not_completed"
                            ),
                            "response_status": status,
                        }

                    return {
                        "ok": True,
                        "status": "computer_goal_completed",
                        "answer": text,
                        "model_used": self.model,
                        "target_app": target_app,
                        "action_batches": action_batches,
                        "visual_completion_reported": True,
                    }

                actions, error = self._safe_actions(
                    call
                )
                if error:
                    return error

                call_id = call.get("call_id")
                if not isinstance(
                    call_id,
                    str,
                ) or not call_id:
                    return {
                        "ok": False,
                        "error": "computer_call_missing_id",
                    }

                # Stateless Responses loop: preserve every output item,
                # including encrypted reasoning and the computer_call itself.
                history.extend(
                    self._dump_item(item)
                    for item in (
                        getattr(
                            response,
                            "output",
                            [],
                        )
                        or []
                    )
                )

                mutating = [
                    action
                    for action in actions
                    if action.get("type")
                    != "screenshot"
                ]

                if current_frame is None:
                    if mutating:
                        return {
                            "ok": False,
                            "error": (
                                "computer_initial_"
                                "screenshot_required"
                            ),
                        }
                elif mutating:
                    applied = (
                        self.bridge.apply_actions(
                            session,
                            current_frame,
                            actions,
                        )
                    )
                    if not applied.get("ok"):
                        return applied
                    action_batches += 1

                frame = self.bridge.capture(
                    session
                )
                if not frame.get("ok"):
                    return frame

                signature = json.dumps(
                    actions,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                digest = frame.get("sha256")

                if (
                    digest
                    and digest == previous_digest
                    and signature
                    == previous_signature
                ):
                    no_progress_repeats += 1
                else:
                    no_progress_repeats = 0

                if no_progress_repeats >= 3:
                    return {
                        "ok": False,
                        "error": "computer_no_progress_detected",
                        "action_batches": action_batches,
                    }

                previous_digest = digest
                previous_signature = signature
                current_frame = frame

                history.append(
                    self._screenshot_output(
                        call_id,
                        frame,
                    )
                )

                response = self._response(
                    client,
                    history,
                )

        except Exception as exc:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": (
                    "computer_provider_failed:"
                    + type(exc).__name__
                ),
            }
