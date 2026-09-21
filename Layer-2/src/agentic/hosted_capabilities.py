"""OpenAI-hosted semantic capability provider.

This provider is read-only with respect to the user's devices. Interactive
desktop control, connectors with side effects, voice, image artifacts and
automations are separate provider families and plug into CapabilityBroker.
"""

from __future__ import annotations

import logging
import os
import re
from time import perf_counter
from urllib.parse import urlsplit


logger = logging.getLogger("EPIS.AGENT")


_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$",
    re.IGNORECASE,
)


class OpenAIHostedProvider:
    provider_id = "openai-hosted"
    display_name = "OpenAI hosted tools"
    priority = 100

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        usage_repository=None,
        client_factory=None,
        vector_store_ids: list[str] | None = None,
    ):
        self.api_key = (
            api_key
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("LUNA_API_KEY")
        )
        self.model = (
            model
            or os.getenv(
                "EPIS_HOSTED_CAPABILITY_MODEL",
                "gpt-5.6-luna",
            )
        )
        self.code_model = os.getenv(
            "EPIS_CODE_CAPABILITY_MODEL",
            "gpt-5.6-sol",
        )
        self.usage_repository = usage_repository
        self.client_factory = client_factory

        if vector_store_ids is None:
            raw = os.getenv(
                "EPIS_VECTOR_STORE_IDS",
                "",
            )
            vector_store_ids = [
                item.strip()
                for item in raw.split(",")
                if item.strip()
            ]
        self.vector_store_ids = tuple(
            vector_store_ids
        )

    def available(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> set[str]:
        if not self.available():
            return set()

        result = {
            "web.research",
            "code.analyze",
            "vision.analyze",
        }
        if self.vector_store_ids:
            result.add("files.hosted_search")
        return result

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
    def _response_dict(response) -> dict:
        if isinstance(response, dict):
            return response
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            try:
                value = dump()
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}
        return {}

    @classmethod
    def _sources(cls, response) -> list[dict]:
        root = cls._response_dict(response)
        found = []
        seen = set()

        def visit(value):
            if isinstance(value, dict):
                url = value.get("url")
                if isinstance(url, str) and url.startswith(
                    ("https://", "http://")
                ):
                    if url not in seen:
                        seen.add(url)
                        found.append({
                            "url": url,
                            "title": (
                                value.get("title")
                                if isinstance(
                                    value.get("title"),
                                    str,
                                )
                                else ""
                            ),
                        })
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(root)
        return found[:50]

    @staticmethod
    def _output_text(response) -> str:
        return str(
            getattr(response, "output_text", "")
            or ""
        ).strip()

    def _record_usage(
        self,
        capability: str,
        model: str,
        response,
        latency_ms: int,
    ) -> None:
        if self.usage_repository is None:
            return
        try:
            self.usage_repository.record_openai_response(
                request_kind=f"capability:{capability}",
                model=model,
                usage=getattr(response, "usage", None),
                latency_ms=latency_ms,
            )
        except Exception as exc:
            logger.warning(
                "Hosted capability usage recording failed: %s",
                type(exc).__name__,
            )

    def _create_response(
        self,
        capability: str,
        *,
        model: str,
        **kwargs,
    ):
        started = perf_counter()
        response = (
            self._client()
            .responses.create(
                model=model,
                store=False,
                **kwargs,
            )
        )
        self._record_usage(
            capability,
            model,
            response,
            round(
                (perf_counter() - started)
                * 1000
            ),
        )
        return response

    @staticmethod
    def _safe_domains(
        raw_domains,
    ) -> tuple[list[str], str | None]:
        domains = []
        for raw in raw_domains or []:
            if not isinstance(raw, str):
                return [], "allowed_domains items must be strings"
            candidate = raw.strip().lower().rstrip(".")
            if "://" in candidate:
                parsed = urlsplit(candidate)
                candidate = (
                    parsed.hostname or ""
                ).lower()
            if (
                not candidate
                or not _DOMAIN_RE.fullmatch(candidate)
            ):
                return [], (
                    "allowed_domains must contain domain names only"
                )
            if candidate not in domains:
                domains.append(candidate)

        return domains[:20], None

    def execute(
        self,
        capability: str,
        arguments: dict,
    ) -> dict:
        if not self.available():
            return {
                "ok": False,
                "error": "provider_unavailable",
            }

        try:
            if capability == "web.research":
                return self._web(arguments)
            if capability == "code.analyze":
                return self._code(arguments)
            if capability == "vision.analyze":
                return self._vision(arguments)
            if capability == "files.hosted_search":
                return self._file_search(arguments)
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    "hosted_capability_failed:"
                    + type(exc).__name__
                ),
            }

        return {
            "ok": False,
            "error": "capability_not_supported",
        }

    def _web(self, arguments: dict) -> dict:
        query = arguments["query"].strip()
        if not query:
            return {
                "ok": False,
                "error": "web query is empty",
            }

        context_size = arguments.get(
            "context_size",
            "medium",
        )
        domains, error = self._safe_domains(
            arguments.get("allowed_domains")
        )
        if error:
            return {
                "ok": False,
                "error": error,
            }

        tool = {
            "type": "web_search",
            "search_context_size": context_size,
        }
        if domains:
            tool["filters"] = {
                "allowed_domains": domains,
            }

        response = self._create_response(
            "web.research",
            model=self.model,
            tools=[tool],
            tool_choice="auto",
            include=[
                "web_search_call.action.sources",
            ],
            input=query,
        )
        text = self._output_text(response)

        return {
            "ok": bool(text),
            "status": (
                "web_research_completed"
                if text
                else "web_research_empty"
            ),
            "answer": text,
            "sources": self._sources(response),
            "grounded_by": "web_search",
        }

    def _code(self, arguments: dict) -> dict:
        task = arguments["task"].strip()
        if not task:
            return {
                "ok": False,
                "error": "code analysis task is empty",
            }

        response = self._create_response(
            "code.analyze",
            model=self.code_model,
            tools=[
                {
                    "type": "code_interpreter",
                    "container": {
                        "type": "auto",
                        "memory_limit": os.getenv(
                            "EPIS_CODE_INTERPRETER_MEMORY",
                            "1g",
                        ),
                    },
                }
            ],
            instructions=(
                "Use the python tool when computation or data processing "
                "helps. Work only inside the hosted sandbox. Do not claim "
                "access to the user's local computer or files."
            ),
            input=task,
        )
        text = self._output_text(response)

        return {
            "ok": bool(text),
            "status": (
                "code_analysis_completed"
                if text
                else "code_analysis_empty"
            ),
            "answer": text,
            "executed_in": "openai_hosted_container",
        }

    def _vision(self, arguments: dict) -> dict:
        image_url = arguments["image_url"].strip()
        prompt = arguments["prompt"].strip()

        if not (
            image_url.startswith("https://")
            or image_url.startswith("data:image/")
        ):
            return {
                "ok": False,
                "error": (
                    "vision image_url must be HTTPS "
                    "or a data:image URL"
                ),
            }

        response = self._create_response(
            "vision.analyze",
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        },
                        {
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": "auto",
                        },
                    ],
                }
            ],
        )
        text = self._output_text(response)

        return {
            "ok": bool(text),
            "status": (
                "vision_analysis_completed"
                if text
                else "vision_analysis_empty"
            ),
            "answer": text,
        }

    def _file_search(self, arguments: dict) -> dict:
        if not self.vector_store_ids:
            return {
                "ok": False,
                "error": "provider_unavailable",
            }

        query = arguments["query"].strip()
        if not query:
            return {
                "ok": False,
                "error": "file search query is empty",
            }

        response = self._create_response(
            "files.hosted_search",
            model=self.model,
            tools=[
                {
                    "type": "file_search",
                    "vector_store_ids": list(
                        self.vector_store_ids
                    ),
                }
            ],
            include=[
                "file_search_call.results",
            ],
            input=query,
        )
        text = self._output_text(response)

        return {
            "ok": bool(text),
            "status": (
                "hosted_file_search_completed"
                if text
                else "hosted_file_search_empty"
            ),
            "answer": text,
            "vector_store_count": len(
                self.vector_store_ids
            ),
        }
