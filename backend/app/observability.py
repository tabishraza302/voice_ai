"""Privacy-preserving Phoenix tracing and scoped trace reads.

The exporter intentionally redacts prompts, transcripts, answers, tool
payloads, provider settings and credentials before spans leave the process.
Only timing, lifecycle, model/provider identity and error type fields are
exported. The owner-facing trace route reads a single session's spans from
Phoenix and exposes no span attributes.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger as _logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status

_SENSITIVE_KEYS = frozenset(
    {
        "input",
        "output",
        "messages",
        "message",
        "tools",
        "tool_choice",
        "system_instructions",
        "gen_ai.system_instructions",
        "transcript",
        "text",
        "prompt",
        "prompt_text",
        "answer",
        "answers",
        "session_properties",
        "request.body",
        "response.body",
    }
)
_SENSITIVE_PARTS = (
    "prompt",
    "transcript",
    "message",
    "stacktrace",
    "answer",
    "completion",
    "arguments",
    "result",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "credential",
    "password",
    "secret",
)
_SAFE_ERROR_TYPES = ("error.type", "exception.type", "error_type")

_PRIVATE_LLM_LOG_MODULE = "pipecat.services.openai.base_llm"
_PRIVATE_LLM_LOG_MARKERS = {
    "DEBUG": "Using system instruction:",
    "WARNING": "Failed to parse function call arguments:",
}
_log_privacy_configured = False
_configured = False


def _redact_pipecat_record(record: dict[str, Any]) -> None:
    if record.get("name") != _PRIVATE_LLM_LOG_MODULE:
        return
    marker = _PRIVATE_LLM_LOG_MARKERS.get(getattr(record.get("level"), "name", None))
    message = record.get("message")
    if marker and isinstance(message, str):
        prefix, found, _ = message.partition(marker)
        if found:
            record["message"] = f"{prefix}{found} [redacted]"


def _is_sensitive(key: str) -> bool:
    lowered = key.casefold()
    if lowered in _SENSITIVE_KEYS:
        return True
    return any(part in lowered for part in _SENSITIVE_PARTS)


def _safe_attributes(attributes: Any) -> dict[str, Any]:
    if not isinstance(attributes, MutableMapping):
        return {}
    return {str(key): value for key, value in attributes.items() if not _is_sensitive(str(key))}


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """Return a copy of an ended span with private payloads removed.

    Ended ``ReadableSpan`` instances hold immutable attributes, so the
    exporter delegates a rebuilt span instead of mutating the original.
    """

    attributes = _safe_attributes(getattr(span, "_attributes", None))
    conversation_id = attributes.get("conversation.id")
    if isinstance(conversation_id, str) and conversation_id and "session.id" not in attributes:
        attributes["session.id"] = conversation_id

    events: list[Event] = []
    for event in getattr(span, "events", ()):
        event_attributes = _safe_attributes(getattr(event, "_attributes", None))
        if not event_attributes:
            continue
        events.append(
            Event(
                name=getattr(event, "name", "event"),
                attributes=event_attributes,
                timestamp=getattr(event, "timestamp", None),
            )
        )

    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=events,
        links=(),
        kind=span.kind,
        instrumentation_scope=getattr(span, "instrumentation_scope", None),
        # Keep the status code but drop its description: providers and
        # automatic instrumentation can put prompts or transcripts there.
        status=Status(span.status.status_code) if span.status is not None else None,
        start_time=span.start_time,
        end_time=span.end_time,
    )


class RedactingExporter(SpanExporter):
    """Delegate exporter that strips private attributes first."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        return self._delegate.export([redact_span(span) for span in spans])

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def _environment(env: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def configure_tracing(env: dict[str, str] | None = None) -> bool:
    """Configure privacy-safe Pipecat logging and Phoenix tracing when enabled.

    Returns True when tracing is active. Calling this more than once is a
    no-op, which keeps FastAPI startup and tests predictable.
    """

    global _configured, _log_privacy_configured
    if not _log_privacy_configured:
        _logger.configure(patcher=_redact_pipecat_record)
        _log_privacy_configured = True
    if _configured:
        return True
    settings = _environment(env)
    endpoint = (settings.get("PHOENIX_OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        return False
    if not endpoint.rstrip("/").endswith("/v1/traces"):
        endpoint = endpoint.rstrip("/") + "/v1/traces"
    project = (settings.get("PHOENIX_PROJECT_NAME") or "voice-agent").strip() or "voice-agent"
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={"x-project-name": project},
        timeout=5,
    )
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "voice-agent-builder",
                "deployment.environment": settings.get("ENVIRONMENT", "development"),
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(RedactingExporter(exporter)))
    trace.set_tracer_provider(provider)
    _configured = True
    return True


def tracing_enabled() -> bool:
    return _configured


class TraceUnavailableError(RuntimeError):
    """Phoenix is not configured or cannot answer a trace query."""


def _phoenix_base(settings: dict[str, str]) -> str:
    configured = (settings.get("PHOENIX_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    endpoint = (settings.get("PHOENIX_OTLP_ENDPOINT") or "").strip().rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint[: -len("/v1/traces")]
    return endpoint


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_ms(start: Any, end: Any) -> int | None:
    started = _parse_time(start)
    ended = _parse_time(end)
    if started is None or ended is None:
        return None
    return max(0, round((ended - started).total_seconds() * 1000))


def _sort_key(span: dict[str, Any]) -> tuple[datetime, str]:
    return (
        _parse_time(span.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc),
        str(span.get("name") or ""),
    )


def _error_type(span: dict[str, Any]) -> str | None:
    attributes = span.get("attributes")
    if not isinstance(attributes, dict):
        return None
    for key in _SAFE_ERROR_TYPES:
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


def public_trace_span(span: dict[str, Any]) -> dict[str, Any]:
    """Project a Phoenix span onto the small owner-safe contract."""

    start = span.get("start_time")
    end = span.get("end_time")
    return {
        "name": str(span.get("name") or "span")[:200],
        "started_at": start if isinstance(start, str) else None,
        "ended_at": end if isinstance(end, str) else None,
        "duration_ms": _duration_ms(start, end),
        "status": str(span.get("status_code") or "UNSET")[:32],
        "error_type": _error_type(span),
    }


async def fetch_session_trace(
    session_id: str,
    *,
    env: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Read one owned session's spans from Phoenix without exposing attributes."""

    settings = _environment(env)
    base = _phoenix_base(settings)
    if not base:
        raise TraceUnavailableError("Phoenix is not configured on the server.")
    project = (settings.get("PHOENIX_PROJECT_NAME") or "voice-agent").strip() or "voice-agent"
    endpoint = f"{base}/v1/projects/{quote(project, safe='')}/spans"

    try:
        async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
            response = await client.get(
                endpoint,
                params=[
                    ("attribute", f"session.id:{session_id}"),
                    ("limit", "1000"),
                    ("sort", "start_time"),
                    ("order", "asc"),
                ],
            )
            if response.status_code != 200:
                # Phoenix returns 404 until the project has received its first
                # export. A session with no spans is an empty trace, not an
                # unavailable observability backend.
                if response.status_code == 404 and "project" in response.text.casefold():
                    return []
                raise TraceUnavailableError("Phoenix did not return a trace for this session.")
            roots = response.json().get("data", [])
            trace_ids: list[str] = []
            for span in roots if isinstance(roots, list) else []:
                context = span.get("context") if isinstance(span, dict) else None
                trace_id = context.get("trace_id") if isinstance(context, dict) else None
                if isinstance(trace_id, str) and trace_id and trace_id not in trace_ids:
                    trace_ids.append(trace_id)
            if not trace_ids:
                return []
            spans = list(roots)
            seen = {
                (span.get("context") or {}).get("span_id")
                for span in spans
                if isinstance(span, dict)
            }
            for trace_id in trace_ids[:20]:
                trace_response = await client.get(
                    endpoint,
                    params=[
                        ("trace_id", trace_id),
                        ("limit", "1000"),
                        ("sort", "start_time"),
                        ("order", "asc"),
                    ],
                )
                if trace_response.status_code != 200:
                    raise TraceUnavailableError("Phoenix did not return the complete trace.")
                for span in trace_response.json().get("data", []):
                    if not isinstance(span, dict):
                        continue
                    span_id = (span.get("context") or {}).get("span_id")
                    if span_id in seen:
                        continue
                    seen.add(span_id)
                    spans.append(span)
    except TraceUnavailableError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise TraceUnavailableError("Phoenix is unavailable.") from error

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for span in spans:
        if not isinstance(span, dict):
            continue
        context = span.get("context") if isinstance(span.get("context"), dict) else {}
        unique[(str(context.get("trace_id", "")), str(context.get("span_id", "")))] = span
    ordered = sorted(unique.values(), key=_sort_key)
    return [public_trace_span(span) for span in ordered]
