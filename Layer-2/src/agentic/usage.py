"""Prompt-free model usage telemetry.

Per-call latency and response usage metadata are stored locally in SQLite.
Organization totals and billed cost come from OpenAI's Admin Usage APIs when an
admin key is configured. The interface stays replaceable for hosted storage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


logger = logging.getLogger(__name__)


def _value(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _first_integer(source: Any, *names: str) -> int | None:
    """Read the first token field exposed by either OpenAI API surface."""
    for name in names:
        value = _integer(_value(source, name))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _empty_totals() -> dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": None,
        "cost_currency": None,
        "cache_hit_percent": None,
    }


@dataclass(frozen=True)
class UsageEvent:
    timestamp: str
    request_kind: str
    model: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    latency_ms: int | None
    estimated_cost: float | None = None


class UsageRepository(Protocol):
    def record_openai_response(
        self,
        *,
        request_kind: str,
        model: str,
        usage: Any,
        latency_ms: int | None,
    ) -> None: ...

    def record_tool_call(
        self,
        *,
        provider: str,
        operation: str,
        capability: str,
        status: str,
        latency_ms: int | None,
        api_requests: int = 0,
        units: float | None = None,
        unit_name: str | None = None,
        estimated_cost: float | None = None,
        currency: str | None = None,
        device_id: str | None = None,
    ) -> None: ...

    def snapshot(self, *, hours: int = 24, limit: int = 20) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SQLiteUsageRepository:
    """Thread-safe local telemetry that stores usage metadata only."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = Lock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_usage (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                request_kind TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                cached_input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                latency_ms INTEGER,
                estimated_cost REAL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS model_usage_timestamp_idx
            ON model_usage (timestamp DESC)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS integration_usage (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                provider TEXT NOT NULL,
                operation TEXT NOT NULL,
                capability TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                api_requests INTEGER NOT NULL DEFAULT 0,
                units REAL,
                unit_name TEXT,
                estimated_cost REAL,
                currency TEXT,
                device_id TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS integration_usage_timestamp_idx
            ON integration_usage (timestamp DESC)
            """
        )
        self._connection.commit()

    def record_openai_response(
        self,
        *,
        request_kind: str,
        model: str,
        usage: Any,
        latency_ms: int | None,
    ) -> None:
        input_details = (
            _value(usage, "input_tokens_details")
            or _value(usage, "prompt_tokens_details")
        )
        output_details = (
            _value(usage, "output_tokens_details")
            or _value(usage, "completion_tokens_details")
        )
        event = UsageEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            request_kind=request_kind,
            model=model,
            input_tokens=_first_integer(
                usage,
                "input_tokens",
                "prompt_tokens",
            ),
            cached_input_tokens=_integer(_value(input_details, "cached_tokens")),
            output_tokens=_first_integer(
                usage,
                "output_tokens",
                "completion_tokens",
            ),
            reasoning_tokens=_integer(_value(output_details, "reasoning_tokens")),
            latency_ms=latency_ms,
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO model_usage (
                    timestamp, request_kind, model, input_tokens,
                    cached_input_tokens, output_tokens, reasoning_tokens,
                    latency_ms, estimated_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.timestamp,
                    event.request_kind,
                    event.model,
                    event.input_tokens,
                    event.cached_input_tokens,
                    event.output_tokens,
                    event.reasoning_tokens,
                    event.latency_ms,
                    event.estimated_cost,
                ),
            )
            self._connection.commit()

    def record_tool_call(
        self,
        *,
        provider: str,
        operation: str,
        capability: str,
        status: str,
        latency_ms: int | None,
        api_requests: int = 0,
        units: float | None = None,
        unit_name: str | None = None,
        estimated_cost: float | None = None,
        currency: str | None = None,
        device_id: str | None = None,
    ) -> None:
        """Store content-free tool telemetry; arguments and results are excluded."""
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid_tool_status")
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO integration_usage (
                    timestamp, provider, operation, capability, status,
                    latency_ms, api_requests, units, unit_name,
                    estimated_cost, currency, device_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    provider,
                    operation,
                    capability,
                    status,
                    latency_ms,
                    max(0, int(api_requests)),
                    units,
                    unit_name,
                    estimated_cost,
                    currency,
                    device_id,
                ),
            )
            self._connection.commit()

    def snapshot(self, *, hours: int = 24, limit: int = 20) -> dict[str, Any]:
        hours = max(1, min(hours, 24 * 31))
        now = datetime.now(timezone.utc)
        return self.snapshot_between(
            since=now - timedelta(hours=hours),
            until=now,
            limit=limit,
            hours=hours,
        )

    def snapshot_between(
        self,
        *,
        since: datetime,
        until: datetime,
        limit: int = 20,
        hours: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        since_text = since.astimezone(timezone.utc).isoformat()
        until_text = until.astimezone(timezone.utc).isoformat()

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT timestamp, request_kind, model, input_tokens,
                       cached_input_tokens, output_tokens, reasoning_tokens,
                       latency_ms, estimated_cost
                FROM model_usage
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (since_text, until_text, limit),
            ).fetchall()
            totals_row = self._connection.execute(
                """
                SELECT COUNT(*), SUM(input_tokens), SUM(cached_input_tokens),
                       SUM(output_tokens), SUM(reasoning_tokens),
                       SUM(estimated_cost)
                FROM model_usage
                WHERE timestamp >= ? AND timestamp <= ?
                """,
                (since_text, until_text),
            ).fetchone()
            models = self._connection.execute(
                """
                SELECT model, request_kind, COUNT(*), SUM(input_tokens),
                       SUM(cached_input_tokens), SUM(output_tokens),
                       SUM(reasoning_tokens), SUM(estimated_cost)
                FROM model_usage
                WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY model, request_kind
                ORDER BY COUNT(*) DESC, model ASC
                """,
                (since_text, until_text),
            ).fetchall()
            tool_rows = self._connection.execute(
                """
                SELECT timestamp, provider, operation, capability, status,
                       latency_ms, api_requests, units, unit_name,
                       estimated_cost, currency, device_id
                FROM integration_usage
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (since_text, until_text, limit),
            ).fetchall()
            tool_totals = self._connection.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END),
                       SUM(api_requests), SUM(estimated_cost)
                FROM integration_usage
                WHERE timestamp >= ? AND timestamp <= ?
                """,
                (since_text, until_text),
            ).fetchone()
            tool_groups = self._connection.execute(
                """
                SELECT provider, operation, capability, COUNT(*),
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END),
                       SUM(api_requests), AVG(latency_ms), SUM(estimated_cost),
                       MIN(currency)
                FROM integration_usage
                WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY provider, operation, capability
                ORDER BY COUNT(*) DESC, provider ASC, operation ASC
                """,
                (since_text, until_text),
            ).fetchall()

        def totals(values: tuple[Any, ...]) -> dict[str, Any]:
            input_tokens = values[1] or 0
            cached_tokens = values[2] or 0
            output_tokens = values[3] or 0
            return {
                "requests": values[0] or 0,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": values[4] or 0,
                "total_tokens": input_tokens + output_tokens,
                "estimated_cost": values[5],
                "cost_currency": None,
                "cache_hit_percent": (
                    round((cached_tokens / input_tokens) * 100, 1)
                    if input_tokens > 0
                    else None
                ),
            }

        return {
            "source": "local",
            "scope": {"usage": "local", "cost": "unavailable"},
            "window": {
                "hours": hours,
                "from": since_text,
                "to": until_text,
                "timezone": "UTC",
            },
            "totals": totals(totals_row),
            "by_model": [
                {
                    "model": row[0],
                    "request_kind": row[1],
                    **totals(row[2:]),
                }
                for row in models
            ],
            "hourly": [],
            "recent_calls": [
                {
                    "timestamp": row[0],
                    "request_kind": row[1],
                    "model": row[2],
                    "input_tokens": row[3],
                    "cached_input_tokens": row[4],
                    "output_tokens": row[5],
                    "reasoning_tokens": row[6],
                    "latency_ms": row[7],
                    "estimated_cost": row[8],
                }
                for row in rows
            ],
            "tool_usage": {
                "totals": {
                    "calls": tool_totals[0] or 0,
                    "succeeded": tool_totals[1] or 0,
                    "failed": tool_totals[2] or 0,
                    "unknown": tool_totals[3] or 0,
                    "api_requests": tool_totals[4] or 0,
                    "estimated_cost": tool_totals[5],
                },
                "by_operation": [
                    {
                        "provider": row[0],
                        "operation": row[1],
                        "capability": row[2],
                        "calls": row[3] or 0,
                        "succeeded": row[4] or 0,
                        "failed": row[5] or 0,
                        "unknown": row[6] or 0,
                        "api_requests": row[7] or 0,
                        "average_latency_ms": (
                            round(row[8]) if row[8] is not None else None
                        ),
                        "estimated_cost": row[9],
                        "currency": row[10],
                    }
                    for row in tool_groups
                ],
                "recent_calls": [
                    {
                        "timestamp": row[0],
                        "provider": row[1],
                        "operation": row[2],
                        "capability": row[3],
                        "status": row[4],
                        "latency_ms": row[5],
                        "api_requests": row[6],
                        "units": row[7],
                        "unit_name": row[8],
                        "estimated_cost": row[9],
                        "currency": row[10],
                        "device_id": row[11],
                    }
                    for row in tool_rows
                ],
            },
            "warning": None,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class OpenAIOrganizationUsageRepository:
    """Merge OpenAI organization usage/costs with prompt-free local details."""

    def __init__(
        self,
        local: SQLiteUsageRepository,
        *,
        admin_api_key: str | None = None,
        luna_model: str | None = None,
        sol_model: str | None = None,
        timezone_name: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 20.0,
        http_get: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.local = local
        self.admin_api_key = admin_api_key or os.getenv("OPENAI_ADMIN_KEY")
        self.luna_model = luna_model or os.getenv("LUNA_MODEL", "gpt-5.6-luna")
        self.sol_model = sol_model or os.getenv("SOL_MODEL", "gpt-5.6-sol")
        self.models = list(dict.fromkeys((self.luna_model, self.sol_model)))
        self.timezone_name = timezone_name or os.getenv(
            "EPIS_USAGE_TIMEZONE",
            "Europe/Istanbul",
        )
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_get = http_get or self._request_json
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def record_openai_response(
        self,
        *,
        request_kind: str,
        model: str,
        usage: Any,
        latency_ms: int | None,
    ) -> None:
        self.local.record_openai_response(
            request_kind=request_kind,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
        )

    def record_tool_call(self, **event: Any) -> None:
        self.local.record_tool_call(**event)

    def snapshot(self, *, hours: int = 24, limit: int = 20) -> dict[str, Any]:
        hours = max(1, min(hours, 168))
        limit = max(1, min(limit, 100))
        now = self._now_factory().astimezone(timezone.utc)
        recent = self.local.snapshot(hours=hours, limit=limit)

        if not self.admin_api_key:
            return self._local_fallback(
                recent,
                "OpenAI Admin API anahtarı bulunamadı; yerel çağrı verileri gösteriliyor.",
            )

        try:
            display_timezone = self._display_timezone()
            local_now = now.astimezone(display_timezone)
            today_start = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).astimezone(timezone.utc)
            local_today = self.local.snapshot_between(
                since=today_start,
                until=now,
                limit=limit,
            )
            today_buckets = self._usage_buckets(
                start=today_start,
                end=now,
                limit=max(
                    1,
                    min(
                        168,
                        math.ceil((now - today_start).total_seconds() / 3600) + 1,
                    ),
                ),
            )
            graph_buckets = self._usage_buckets(
                start=now - timedelta(hours=hours),
                end=now,
                limit=hours + 1,
            )
        except Exception as exc:
            logger.warning(
                "OpenAI organization usage unavailable: %s",
                type(exc).__name__,
            )
            return self._local_fallback(
                recent,
                "OpenAI organizasyon usage verisi şu anda alınamadı; yerel çağrı verileri gösteriliyor.",
            )

        totals, by_model, api_key_ids = self._aggregate_usage(
            today_buckets,
            local_today,
        )
        hourly = self._aggregate_hourly(graph_buckets)
        warnings: list[str] = []

        try:
            cost, currency, cost_scope = self._cost_total(
                start=today_start,
                end=now,
                api_key_ids=api_key_ids,
            )
        except Exception as exc:
            logger.warning(
                "OpenAI organization costs unavailable: %s",
                type(exc).__name__,
            )
            cost = None
            currency = None
            cost_scope = "unavailable"
            warnings.append("OpenAI maliyet verisi şu anda alınamadı.")

        totals["estimated_cost"] = cost
        totals["cost_currency"] = currency

        return {
            "source": "openai",
            "scope": {"usage": "configured_models", "cost": cost_scope},
            "window": {
                "hours": hours,
                "label": "today",
                "from": today_start.isoformat(),
                "to": now.isoformat(),
                "timezone": self.timezone_name,
            },
            "totals": totals,
            "by_model": by_model,
            "hourly": hourly,
            "recent_calls": recent["recent_calls"],
            "tool_usage": local_today["tool_usage"],
            "warning": " ".join(warnings) or None,
        }

    def close(self) -> None:
        self.local.close()

    def _display_timezone(self):
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            if self.timezone_name == "Europe/Istanbul":
                return timezone(timedelta(hours=3), name="Europe/Istanbul")
            raise

    @staticmethod
    def _local_fallback(
        snapshot: dict[str, Any],
        warning: str,
    ) -> dict[str, Any]:
        snapshot["source"] = "local"
        snapshot["warning"] = warning
        return snapshot

    def _usage_buckets(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        return self._request_all(
            "/organization/usage/completions",
            {
                "start_time": int(start.timestamp()),
                "end_time": int(end.timestamp()),
                "bucket_width": "1h",
                "limit": max(1, min(limit, 168)),
                "group_by": ["model", "api_key_id"],
                "models": self.models,
            },
        )

    def _cost_total(
        self,
        *,
        start: datetime,
        end: datetime,
        api_key_ids: set[str],
    ) -> tuple[float | None, str | None, str]:
        params: dict[str, Any] = {
            "start_time": int(start.timestamp()),
            "end_time": int(end.timestamp()),
            "bucket_width": "1d",
            "limit": 2,
        }
        project_id = os.getenv("EPIS_OPENAI_PROJECT_ID")
        if project_id:
            params["project_ids"] = [project_id]
            cost_scope = "project"
        elif api_key_ids:
            params["api_key_ids"] = sorted(api_key_ids)
            cost_scope = "api_keys"
        else:
            cost_scope = "organization"

        buckets = self._request_all("/organization/costs", params)
        total = 0.0
        currencies: set[str] = set()
        found = False
        for bucket in buckets:
            for result in _value(bucket, "results") or []:
                amount = _value(result, "amount")
                value = _number(_value(amount, "value"))
                currency = _value(amount, "currency")
                if value is not None:
                    total += value
                    found = True
                if isinstance(currency, str) and currency:
                    currencies.add(currency.lower())

        if len(currencies) > 1:
            return None, None, cost_scope
        return total if found else 0.0, next(iter(currencies), "usd"), cost_scope

    def _aggregate_usage(
        self,
        buckets: list[Mapping[str, Any]],
        local_today: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
        models: dict[str, dict[str, Any]] = {}
        api_key_ids: set[str] = set()
        totals = _empty_totals()

        for bucket in buckets:
            for result in _value(bucket, "results") or []:
                model = _value(result, "model")
                if not isinstance(model, str) or not model:
                    model = "unknown"
                row = models.setdefault(
                    model,
                    {
                        **_empty_totals(),
                        "model": model,
                        "request_kind": self._request_kind(model),
                    },
                )
                values = {
                    "requests": _integer(_value(result, "num_model_requests")) or 0,
                    "input_tokens": _integer(_value(result, "input_tokens")) or 0,
                    "cached_input_tokens": _integer(_value(result, "input_cached_tokens")) or 0,
                    "output_tokens": _integer(_value(result, "output_tokens")) or 0,
                }
                for name, value in values.items():
                    row[name] += value
                    totals[name] += value
                api_key_id = _value(result, "api_key_id")
                if isinstance(api_key_id, str) and api_key_id:
                    api_key_ids.add(api_key_id)

        local_reasoning = {
            item["model"]: item.get("reasoning_tokens", 0)
            for item in local_today.get("by_model", [])
        }
        for model, row in models.items():
            row["reasoning_tokens"] = local_reasoning.get(model, 0)
            row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
            row["cache_hit_percent"] = self._cache_percent(row)
        totals["reasoning_tokens"] = sum(local_reasoning.values())
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
        totals["cache_hit_percent"] = self._cache_percent(totals)
        return (
            totals,
            sorted(models.values(), key=lambda row: (-row["requests"], row["model"])),
            api_key_ids,
        )

    def _aggregate_hourly(
        self,
        buckets: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        hourly: list[dict[str, Any]] = []
        for bucket in buckets:
            input_tokens = 0
            cached_tokens = 0
            output_tokens = 0
            requests = 0
            for result in _value(bucket, "results") or []:
                input_tokens += _integer(_value(result, "input_tokens")) or 0
                cached_tokens += _integer(_value(result, "input_cached_tokens")) or 0
                output_tokens += _integer(_value(result, "output_tokens")) or 0
                requests += _integer(_value(result, "num_model_requests")) or 0
            start_time = _integer(_value(bucket, "start_time"))
            end_time = _integer(_value(bucket, "end_time"))
            if start_time is None:
                continue
            hourly.append(
                {
                    "start_time": datetime.fromtimestamp(start_time, timezone.utc).isoformat(),
                    "end_time": (
                        datetime.fromtimestamp(end_time, timezone.utc).isoformat()
                        if end_time is not None
                        else None
                    ),
                    "requests": requests,
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            )
        return sorted(hourly, key=lambda row: row["start_time"])

    def _request_kind(self, model: str) -> str:
        if model == self.luna_model:
            return "luna"
        if model == self.sol_model:
            return "sol"
        return "model"

    @staticmethod
    def _cache_percent(values: Mapping[str, Any]) -> float | None:
        input_tokens = values.get("input_tokens", 0)
        if not isinstance(input_tokens, int) or input_tokens <= 0:
            return None
        cached_tokens = values.get("cached_input_tokens", 0)
        if not isinstance(cached_tokens, int):
            cached_tokens = 0
        return round((cached_tokens / input_tokens) * 100, 1)

    def _request_all(
        self,
        path: str,
        params: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        request_params = dict(params)
        items: list[Mapping[str, Any]] = []
        for _ in range(20):
            payload = self._http_get(path, request_params)
            page_items = _value(payload, "data") or []
            items.extend(item for item in page_items if isinstance(item, Mapping))
            next_page = _value(payload, "next_page")
            if not _value(payload, "has_more") or not next_page:
                return items
            request_params["page"] = next_page
        raise RuntimeError("openai_usage_pagination_limit")

    def _request_json(
        self,
        path: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        query_pairs: list[tuple[str, Any]] = []
        for name, value in params.items():
            if isinstance(value, (list, tuple, set)):
                query_pairs.extend((name, item) for item in value)
            elif value is not None:
                query_pairs.append((name, value))
        url = f"{self.base_url}{path}?{urlencode(query_pairs)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.admin_api_key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("openai_usage_invalid_response")
        return payload
