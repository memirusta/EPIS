"""Frontline model adapter. It is intentionally replaceable and non-authoritative."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Protocol
import logging
from time import perf_counter

from .usage import UsageRepository


def display_text(text: str) -> str:
    """Read only the old direct-response envelope; never execute text JSON."""
    candidate = text.strip()
    if candidate.startswith("```json\n") and candidate.endswith("```"):
        candidate = candidate[8:-3].strip()
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        return text.strip()
    if isinstance(value, dict) and value.get("type") == "direct" and isinstance(value.get("message"), str):
        return value["message"].strip()
    return text.strip()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict


@dataclass
class LunaReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_assistant_message(self) -> dict:
        message = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                }
                for call in self.tool_calls
            ]
        return message


class LunaClient(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> LunaReply: ...


class OpenAILunaClient:
    """Direct GPT-5.6 Luna adapter using OpenAI Chat Completions."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        usage_repository: UsageRepository | None = None,
    ):
        self.model = model or os.getenv("LUNA_MODEL", "gpt-5.6-luna")
        self.base_url = (base_url or os.getenv("LUNA_BASE_URL") or "").rstrip("/") or None
        self.api_key = api_key or os.getenv("LUNA_API_KEY") or os.getenv("OPENAI_API_KEY")
        # Chat Completions currently rejects function tools for Luna when
        # reasoning_effort is anything other than "none". Heavy reasoning is
        # delegated to Sol, so the no-reasoning default also matches EPIS's
        # cost and latency boundary for frontline turns.
        self.reasoning_effort = os.getenv("LUNA_REASONING_EFFORT", "none")
        self.usage_repository = usage_repository

    def _client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is required for the Luna client") from exc
        kwargs = {"timeout": 90.0, "max_retries": 1}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def complete(self, messages: list[dict], tools: list[dict]) -> LunaReply:
        started = perf_counter()
        request = {
            "model": self.model,
            "messages": messages,
            "store": False,
            "max_completion_tokens": 2048,
            "reasoning_effort": self.reasoning_effort,
        }

        if tools:
            request.update(
                {
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )

        response = self._client().chat.completions.create(**request)
        self._record_usage(
            getattr(response, "usage", None),
            round((perf_counter() - started) * 1000),
        )
        message = response.choices[0].message
        calls = []
        for raw_call in message.tool_calls or []:
            try:
                arguments = json.loads(raw_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = None  # Invalid JSON must fail validation, never execute as an empty call.
            calls.append(ToolCall(raw_call.id, raw_call.function.name, arguments))
        return LunaReply(text=display_text(message.content or ""), tool_calls=calls)

    def _record_usage(self, usage, latency_ms: int) -> None:
        if self.usage_repository is None:
            return
        try:
            self.usage_repository.record_openai_response(
                request_kind="luna",
                model=self.model,
                usage=usage,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            logging.getLogger("EPIS.AGENT").warning(
                "Luna usage recording failed: %s",
                type(exc).__name__,
            )


class OpenAISolClient:
    """Direct GPT-5.6 Sol specialist; privacy filtering stays local."""

    def __init__(
        self,
        privacy_filter,
        model: str | None = None,
        api_key: str | None = None,
        usage_repository: UsageRepository | None = None,
    ):
        self.privacy = privacy_filter
        self.model = model or os.getenv("SOL_MODEL", "gpt-5.6-sol")
        self.api_key = api_key or os.getenv("SOL_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.reasoning_effort = os.getenv("SOL_REASONING_EFFORT", "high")
        self.usage_repository = usage_repository

    def analyze(self, task: str, context: dict | None = None) -> dict:
        try:
            from openai import OpenAI
        except ImportError as exc:
            return {
                "ok": False,
                "error": f"openai package is required for Sol: {exc}",
            }

        safe_task, mapping = self.privacy.anonymize(task)
        reason = (context or {}).get("reason", "analysis")

        try:
            max_output_tokens = int(
                os.getenv(
                    "SOL_MAX_OUTPUT_TOKENS",
                    "16384",
                )
            )

            client = OpenAI(
                api_key=self.api_key,
                timeout=180.0,
                max_retries=1,
            )

            started = perf_counter()
            response = client.responses.create(
                model=self.model,
                reasoning={
                    "effort": self.reasoning_effort,
                },
                store=False,
                max_output_tokens=max_output_tokens,
                instructions=(
                    "You are Sol, EPIS's specialist analysis engine. "
                    "Work on the delegated task only. Return a concise, "
                    "rigorous result for Luna to synthesize; do not imitate "
                    "EPIS's personality and do not claim tool actions you "
                    "did not perform."
                ),
                input=(
                    f"Delegation type: {reason}\n\n"
                    f"Task:\n{safe_task}"
                ),
            )
            self._record_usage(
                getattr(response, "usage", None),
                round((perf_counter() - started) * 1000),
            )

            status = getattr(
                response,
                "status",
                None,
            )

            incomplete_details = getattr(
                response,
                "incomplete_details",
                None,
            )

            incomplete_reason = None

            if incomplete_details is not None:
                incomplete_reason = getattr(
                    incomplete_details,
                    "reason",
                    None,
                )

                if incomplete_reason is None:
                    incomplete_reason = str(
                        incomplete_details
                    )

            usage = getattr(
                response,
                "usage",
                None,
            )

            output_tokens = (
                getattr(
                    usage,
                    "output_tokens",
                    None,
                )
                if usage is not None
                else None
            )

            output_details = (
                getattr(
                    usage,
                    "output_tokens_details",
                    None,
                )
                if usage is not None
                else None
            )

            reasoning_tokens = (
                getattr(
                    output_details,
                    "reasoning_tokens",
                    None,
                )
                if output_details is not None
                else None
            )

            raw_text = (
                getattr(
                    response,
                    "output_text",
                    "",
                )
                or ""
            ).strip()

            result_text = self.privacy.deanonymize(
                raw_text,
                mapping,
            )

            diagnostics = {
                "response_status": status,
                "incomplete_reason": incomplete_reason,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "max_output_tokens": max_output_tokens,
            }

            if status not in {
                None,
                "completed",
            }:
                logging.getLogger(
                    "EPIS.AGENT"
                ).warning(
                    (
                        "Sol response not completed: "
                        "status=%s reason=%s "
                        "output_tokens=%s reasoning_tokens=%s"
                    ),
                    status,
                    incomplete_reason,
                    output_tokens,
                    reasoning_tokens,
                )

                result = {
                    "ok": False,
                    "model_used": self.model,
                    "error": "Sol response incomplete",
                    **diagnostics,
                }

                if result_text:
                    result["partial_result"] = result_text

                return result

            if not result_text:
                logging.getLogger(
                    "EPIS.AGENT"
                ).warning(
                    (
                        "Sol returned empty output_text: "
                        "status=%s output_tokens=%s "
                        "reasoning_tokens=%s max_output_tokens=%s"
                    ),
                    status,
                    output_tokens,
                    reasoning_tokens,
                    max_output_tokens,
                )

                return {
                    "ok": False,
                    "model_used": self.model,
                    "error": "Sol returned empty output_text",
                    **diagnostics,
                }

            return {
                "ok": True,
                "model_used": self.model,
                "result": result_text,
                **diagnostics,
            }

        except Exception as exc:
            logging.getLogger(
                "EPIS.AGENT"
            ).warning(
                "Sol request failed: %s",
                type(exc).__name__,
            )

            return {
                "ok": False,
                "model_used": self.model,
                "error": type(exc).__name__,
            }

    def _record_usage(self, usage, latency_ms: int) -> None:
        if self.usage_repository is None:
            return
        try:
            self.usage_repository.record_openai_response(
                request_kind="sol",
                model=self.model,
                usage=usage,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            logging.getLogger("EPIS.AGENT").warning(
                "Sol usage recording failed: %s",
                type(exc).__name__,
            )



# Compatibility for imports from the first 0.1 implementation.
OpenAICompatibleLunaClient = OpenAILunaClient
