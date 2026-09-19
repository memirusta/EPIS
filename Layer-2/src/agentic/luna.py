"""Frontline model adapter. It is intentionally replaceable and non-authoritative."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Protocol


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


class OpenAICompatibleLunaClient:
    """Works with Ollama/vLLM or a hosted OpenAI-compatible endpoint via env vars.

    LUNA_BASE_URL defaults to Ollama's OpenAI-compatible endpoint. No endpoint,
    API key, conversation, or tool result is persisted here.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("LUNA_MODEL", os.getenv("QWEN_MODEL", "qwen3.5:9b"))
        self.base_url = (base_url or os.getenv("LUNA_BASE_URL") or os.getenv("QWEN_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
        self.api_key = api_key or os.getenv("LUNA_API_KEY") or os.getenv("QWEN_API_KEY") or "not-needed"

    def complete(self, messages: list[dict], tools: list[dict]) -> LunaReply:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is required for the Luna client") from exc
        client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=90.0, max_retries=1)
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.35,
        )
        message = response.choices[0].message
        calls = []
        for raw_call in message.tool_calls or []:
            try:
                arguments = json.loads(raw_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append(ToolCall(raw_call.id, raw_call.function.name, arguments))
        return LunaReply(text=(message.content or "").strip(), tool_calls=calls)
