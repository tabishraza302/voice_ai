"""Observers: turn latency, and the Prometheus metrics contract.

**The metric names here are a contract.**
``observability/prometheus.yml``, ``observability/alerts.yml`` and
``observability/grafana/dashboards/voice-ai.json`` all reference them by
name.
Renaming one here breaks all three, silently, at 3 a.m. They are
therefore listed in ``METRIC_NAMES`` and asserted by the test suite.

**Why the observers are backend-agnostic.** The Prometheus observer reads
Pipecat's ``MetricsFrame`` payload types — ``TTFBMetricsData``,
``STTUsageMetricsData`` and so on — which every service produces in the
same shape regardless of which model is behind it. That is what makes
swapping the STT, TTS or LLM change no observable metric, and it is the
property ``tests/test_modularity.py`` checks.

**Label cardinality is bounded.** Every label this module emits comes
from a fixed set: ``processor`` and ``model`` on the pipeline metrics,
``kind`` on the token counter, ``component`` on the memory gauge. Never a
session id, a user id or a call id: those are unbounded, and Prometheus is
the wrong place to store them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psutil
from loguru import logger
from pipecat.frames.frames import InterruptionFrame, MetricsFrame
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    ProcessingMetricsData,
    STTUsageMetricsData,
    TextAggregationMetricsData,
    TTFAMetricsData,
    TTFATMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# The names below are referenced by observability/prometheus.yml,
# observability/alerts.yml and the dashboard. Changing one breaks the
# other three.
METRIC_NAMES = {
    "voice_turn_ttfb_seconds",
    "voice_turn_ttfa_seconds",
    "voice_turn_ttfat_seconds",
    "voice_service_processing_seconds",
    "voice_text_aggregation_seconds",
    "voice_user_bot_latency_seconds",
    "voice_llm_tokens_total",
    "voice_tts_characters_total",
    "voice_stt_audio_seconds_total",
    "voice_turn_interrupted_total",
    "voice_process_resident_memory_bytes",
}

# Process-name needles for the components sampled for the memory gauge,
# mapped to the label they report under.
_MEMORY_NEEDLES = {
    "ollama": "ollama",
    "phoenix": "phoenix",
    "prometheus": "prometheus",
    "grafana": "grafana",
    "otelcol": "otel_collector",
    "mlx_audio": "mlx_audio",
}

# Every component the memory gauge can report. "python" is the agent's own
# process, measured in-process; the rest are sampled by process name.
MEMORY_COMPONENTS = ("python", *_MEMORY_NEEDLES.values())


class PrometheusObserver(BaseObserver):
    """Turns Pipecat's metrics frames into Prometheus series.

    Constructed with ``observe_every_push=False`` so a frame is counted
    once, not once per hop through the pipeline.
    """

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        model_resolver: "Callable[[str], str | None] | None" = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(observe_every_push=False, **kwargs)
        registry = registry or CollectorRegistry()
        # Pipecat's STT and TTS metrics frames carry no model name, so
        # they would be labelled "unknown" and the dashboard could not
        # say which model served a turn. The resolver maps a processor
        # name to the model id configured for it.
        self._model_resolver = model_resolver

        labels = ("processor", "model")

        # Per-service latency. This is the diagnosis metric: which stage
        # of the pipeline is slow.
        self._ttfb = Histogram(
            "voice_turn_ttfb_seconds",
            "Time to first byte per service",
            labels,
            registry=registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
        )
        self._ttfa = Histogram(
            "voice_turn_ttfa_seconds",
            "Time to first audio per service",
            labels,
            registry=registry,
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
        )
        self._ttfat = Histogram(
            "voice_turn_ttfat_seconds",
            "Time to first answer token per service",
            labels,
            registry=registry,
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
        )
        self._processing = Histogram(
            "voice_service_processing_seconds",
            "Service processing time",
            labels,
            registry=registry,
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
        )
        self._text_aggregation = Histogram(
            "voice_text_aggregation_seconds",
            "Time from the first LLM token to the first complete sentence",
            labels,
            registry=registry,
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
        )

        # End-to-end user->bot latency. This is the SLO metric: the
        # number a user actually feels.
        self._user_bot_latency = Histogram(
            "voice_user_bot_latency_seconds",
            "End-to-end latency from the user stopping speaking to the bot starting",
            registry=registry,
            buckets=(0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 15.0),
        )

        # Usage counters.
        self._llm_tokens = Counter(
            "voice_llm_tokens_total",
            "LLM tokens by kind",
            ("processor", "model", "kind"),
            registry=registry,
        )
        self._tts_characters = Counter(
            "voice_tts_characters_total",
            "Characters synthesized by TTS",
            ("processor", "model"),
            registry=registry,
        )
        self._stt_audio_seconds = Counter(
            "voice_stt_audio_seconds_total",
            "Audio seconds submitted to STT",
            ("processor", "model"),
            registry=registry,
        )
        self._interrupted = Counter(
            "voice_turn_interrupted_total",
            "Turns interrupted by the user",
            registry=registry,
        )

        self._memory = Gauge(
            "voice_process_resident_memory_bytes",
            "Resident memory by component",
            ("component",),
            registry=registry,
            multiprocess_mode="livesum",
        )

        self._registry = registry
        self._memory_task: asyncio.Task | None = None

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry

    # -- frame handling ---------------------------------------------------

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        if isinstance(frame, MetricsFrame):
            self._record_metrics(frame)
        elif isinstance(frame, InterruptionFrame):
            self._interrupted.inc()

    def _record_metrics(self, frame: MetricsFrame) -> None:
        # A MetricsFrame carries a *list* of payloads: one processor can
        # report several measurements in one frame.
        for metrics in frame.data:
            self._record_one(metrics)

    def _record_one(self, metrics) -> None:
        processor = metrics.processor
        model = metrics.model or self._resolve_model(processor) or "unknown"

        if isinstance(metrics, TTFBMetricsData):
            self._ttfb.labels(processor=processor, model=model).observe(metrics.value)
        elif isinstance(metrics, TTFAMetricsData):
            self._ttfa.labels(processor=processor, model=model).observe(metrics.ttfa)
        elif isinstance(metrics, TTFATMetricsData):
            self._ttfat.labels(processor=processor, model=model).observe(metrics.ttfat)
        elif isinstance(metrics, ProcessingMetricsData):
            self._processing.labels(processor=processor, model=model).observe(metrics.value)
        elif isinstance(metrics, TextAggregationMetricsData):
            self._text_aggregation.labels(processor=processor, model=model).observe(
                metrics.value
            )
        elif isinstance(metrics, LLMUsageMetricsData):
            usage = metrics.value
            self._llm_tokens.labels(processor=processor, model=model, kind="prompt").inc(
                usage.prompt_tokens
            )
            self._llm_tokens.labels(processor=processor, model=model, kind="completion").inc(
                usage.completion_tokens
            )
            if usage.reasoning_tokens:
                self._llm_tokens.labels(
                    processor=processor, model=model, kind="reasoning"
                ).inc(usage.reasoning_tokens)
        elif isinstance(metrics, TTSUsageMetricsData):
            self._tts_characters.labels(processor=processor, model=model).inc(
                metrics.value
            )
        elif isinstance(metrics, STTUsageMetricsData):
            self._stt_audio_seconds.labels(processor=processor, model=model).inc(
                metrics.value.audio_seconds
            )

    # -- end-to-end latency ------------------------------------------------

    def _resolve_model(self, processor: str) -> str | None:
        """Which model is behind this processor, if we know."""
        if self._model_resolver is None:
            return None
        try:
            return self._model_resolver(processor)
        except Exception:  # noqa: BLE001
            return None

    def observe_user_bot_latency(self, seconds: float) -> None:
        """Record one end-to-end user->bot latency sample."""
        self._user_bot_latency.observe(seconds)

    # -- memory ------------------------------------------------------------

    async def start_memory_sampling(self, interval: float = 5.0) -> None:
        """Sample resident memory by component, in the background."""
        if self._memory_task is None:
            self._memory_task = asyncio.create_task(self._sample_memory(interval))

    async def stop_memory_sampling(self) -> None:
        if self._memory_task is not None:
            self._memory_task.cancel()
            try:
                await self._memory_task
            except asyncio.CancelledError:
                pass
            self._memory_task = None

    async def _sample_memory(self, interval: float) -> None:
        while True:
            try:
                self._memory.labels(component="python").set(
                    psutil.Process().memory_info().rss
                )
                for name, rss in _external_memory().items():
                    self._memory.labels(component=name).set(rss)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"memory sampling failed: {exc}")
            await asyncio.sleep(interval)


def _external_memory() -> dict[str, int]:
    """Resident memory of the other components, by process name.

    Matched loosely: the observability services run inside containers
    whose host-side process names are not stable, so this reports what it
    can and skips the rest rather than guessing.
    """
    found: dict[str, int] = {}
    wanted = _MEMORY_NEEDLES
    try:
        for process in psutil.process_iter(["name", "memory_info"]):
            try:
                name = (process.info["name"] or "").lower()
                for needle, component in wanted.items():
                    if needle in name and component not in found:
                        found[component] = process.info["memory_info"].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"could not enumerate processes: {exc}")
    return found


def mount_metrics(app, registry: CollectorRegistry, path: str = "/metrics") -> None:
    """Mount the Prometheus endpoint on the runner's FastAPI app.

    On prometheus_client 0.26.0 ``add_route`` raises TypeError, so the
    ASGI app is mounted instead. It serves at ``/metrics/`` and
    307-redirects from ``/metrics``, which is why
    ``observability/prometheus.yml`` uses ``metrics_path: "/metrics/"``.

    The app is the one the Pipecat dev runner creates, imported as
    ``from pipecat.runner.run import app``. Mounting on it rather than
    starting a second HTTP server means the metrics live on the same port
    as the agent and the browser client.
    """
    from prometheus_client import make_asgi_app

    app.mount(path, make_asgi_app(registry))


class LatencyBridge:
    """Feeds the end-to-end user->bot latency into the Prometheus histogram.

    Pipecat ships ``UserBotLatencyObserver``, which measures the time from
    the user stopping speaking to the bot starting to speak. That is the
    number a user actually feels, and it is the SLO metric — the per-
    service TTFB is for diagnosis, not for alerting.

    This bridge subscribes to its ``on_latency_measured`` event so the
    measurement is not re-implemented here.
    """

    def __init__(self, prometheus_observer: PrometheusObserver) -> None:
        self._prometheus = prometheus_observer
        self._observer = UserBotLatencyObserver()
        self._observer.add_event_handler("on_latency_measured", self._on_latency)
        self._observer.add_event_handler("on_latency_breakdown", self._on_breakdown)

    @property
    def observer(self) -> UserBotLatencyObserver:
        """The Pipecat observer to attach to the pipeline."""
        return self._observer

    def _on_latency(self, observer: UserBotLatencyObserver, latency_seconds: float) -> None:
        self._prometheus.observe_user_bot_latency(latency_seconds)

    def _on_breakdown(self, observer: UserBotLatencyObserver, breakdown: Any) -> None:
        # The breakdown attributes the turn's time across LLM TTFB, TTS
        # aggregation, VAD silence and turn detection. It is attached to
        # the trace rather than to a metric, because it is per-turn
        # detail, not a series.
        try:
            lines = breakdown.turn_contribution_lines()
        except Exception:  # noqa: BLE001
            return
        for line in lines:
            logger.debug(f"turn breakdown: {line}")
