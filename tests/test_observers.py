"""Tests for the Prometheus observer — the metrics contract.

The metric names here are referenced by ``observability/prometheus.yml``,
``observability/alerts.yml`` and the Grafana dashboard. Renaming one
breaks all three, silently, so the names are asserted as a set.
"""

from __future__ import annotations

import pytest
from pipecat.frames.frames import InterruptionFrame, MetricsFrame
from pipecat.metrics.metrics import (
    LLMTokenUsage,
    LLMUsageMetricsData,
    ProcessingMetricsData,
    STTUsage,
    STTUsageMetricsData,
    TextAggregationMetricsData,
    TTFAMetricsData,
    TTFATMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from prometheus_client import CollectorRegistry, generate_latest

from bot.config import Settings
from bot.observers import METRIC_NAMES, PrometheusObserver


@pytest.fixture
def observer() -> PrometheusObserver:
    return PrometheusObserver(registry=CollectorRegistry())


def _sample(observer: PrometheusObserver) -> str:
    return generate_latest(observer.registry).decode("utf-8")


def _frame(data) -> MetricsFrame:
    # A MetricsFrame carries a *list* of payloads.
    return MetricsFrame(data=data if isinstance(data, list) else [data])


class TestMetricNames:
    def test_the_declared_names_match_the_instruments(self, observer):
        # prometheus_client registers several names per instrument: a
        # histogram contributes _bucket/_sum/_count, every metric
        # contributes a _created series, and a counter is registered under
        # both its base name and its _total name. Compare on the base
        # name, which is what prometheus.yml and the dashboard refer to.
        suffixes = ("_bucket", "_sum", "_count", "_created", "_total")

        def base(name: str) -> str:
            for suffix in suffixes:
                if name.endswith(suffix):
                    return name[: -len(suffix)]
            return name

        declared = {base(name) for name in METRIC_NAMES}
        produced = {
            base(name) for name in observer.registry._names_to_collectors
        }
        assert declared == produced


class TestLatencyMetrics:
    async def test_ttfb_is_recorded(self, observer):
        await observer.on_push_frame(
            _pushed(_frame(TTFBMetricsData(processor="stt", model="nemotron", value=0.25)))
        )
        text = _sample(observer)
        assert 'voice_turn_ttfb_seconds_count{model="nemotron",processor="stt"} 1.0' in text

    async def test_ttfa_is_recorded(self, observer):
        await observer.on_push_frame(
            _pushed(
                _frame(
                    TTFAMetricsData(
                        processor="tts", model="kokoro", ttfa=0.4, ttfb=0.3, leading_silence=0.1
                    )
                )
            )
        )
        assert "voice_turn_ttfa_seconds_count" in _sample(observer)

    async def test_ttfat_is_recorded(self, observer):
        await observer.on_push_frame(
            _pushed(
                _frame(
                    TTFATMetricsData(
                        processor="llm", model="qwen3:4b", ttfat=0.5, ttfb=0.4, thinking_time=0.1
                    )
                )
            )
        )
        assert "voice_turn_ttfat_seconds_count" in _sample(observer)

    async def test_processing_and_text_aggregation(self, observer):
        await observer.on_push_frame(
            _pushed(_frame(ProcessingMetricsData(processor="llm", model="m", value=0.1)))
        )
        await observer.on_push_frame(
            _pushed(_frame(TextAggregationMetricsData(processor="tts", model="m", value=0.2)))
        )
        text = _sample(observer)
        assert "voice_service_processing_seconds_count" in text
        assert "voice_text_aggregation_seconds_count" in text


class TestUsageCounters:
    async def test_llm_tokens_are_split_by_kind(self, observer):
        await observer.on_push_frame(
            _pushed(
                _frame(
                    LLMUsageMetricsData(
                        processor="llm",
                        model="qwen3:4b",
                        value=LLMTokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    )
                )
            )
        )
        text = _sample(observer)
        assert 'voice_llm_tokens_total{kind="prompt",model="qwen3:4b",processor="llm"} 10.0' in text
        assert 'voice_llm_tokens_total{kind="completion",model="qwen3:4b",processor="llm"} 5.0' in text

    async def test_tts_characters(self, observer):
        await observer.on_push_frame(
            _pushed(_frame(TTSUsageMetricsData(processor="tts", model="kokoro", value=42)))
        )
        assert "voice_tts_characters_total" in _sample(observer)

    async def test_stt_audio_seconds(self, observer):
        await observer.on_push_frame(
            _pushed(
                _frame(
                    STTUsageMetricsData(
                        processor="stt", model="nemotron", value=STTUsage(audio_seconds=3.5)
                    )
                )
            )
        )
        assert "voice_stt_audio_seconds_total" in _sample(observer)


class TestMultiplePayloads:
    async def test_one_frame_can_carry_several_measurements(self, observer):
        """A MetricsFrame carries a list; both entries must be recorded."""
        await observer.on_push_frame(
            _pushed(
                MetricsFrame(
                    data=[
                        TTFBMetricsData(processor="stt", model="m", value=0.2),
                        TTFBMetricsData(processor="llm", model="m", value=0.3),
                    ]
                )
            )
        )
        text = _sample(observer)
        assert 'voice_turn_ttfb_seconds_count{model="m",processor="stt"} 1.0' in text
        assert 'voice_turn_ttfb_seconds_count{model="m",processor="llm"} 1.0' in text


class TestModelResolution:
    def test_resolver_maps_processors_to_configured_models(self):
        from bot.bot import build_model_resolver

        settings = Settings(_env_file=None)
        resolve = build_model_resolver(settings)
        assert resolve("LocalMlxSTTService#0") == settings.stt_model
        assert resolve("KokoroTTSService#0") == settings.tts_model
        assert resolve("OLLamaLLMService#0") == settings.llm_model

    def test_resolver_returns_none_for_an_unknown_processor(self):
        from bot.bot import build_model_resolver

        assert build_model_resolver(Settings(_env_file=None))("SomethingElse#0") is None

    async def test_the_model_is_labelled_when_the_frame_carries_none(self, observer):
        from bot.bot import build_model_resolver

        settings = Settings(_env_file=None)
        observer._model_resolver = build_model_resolver(settings)
        await observer.on_push_frame(
            _pushed(_frame(TTFBMetricsData(processor="KokoroTTSService#0", model=None, value=0.1)))
        )
        text = _sample(observer)
        assert f'model="{settings.tts_model}"' in text
        assert 'model="unknown"' not in text


class TestUserBotLatency:
    async def test_end_to_end_latency_is_the_slo_metric(self, observer):
        observer.observe_user_bot_latency(1.25)
        text = _sample(observer)
        assert "voice_user_bot_latency_seconds_count 1.0" in text
        assert 'voice_user_bot_latency_seconds_sum 1.25' in text


class TestInterruptions:
    async def test_interruption_is_counted(self, observer):
        await observer.on_push_frame(_pushed(InterruptionFrame()))
        assert "voice_turn_interrupted_total 1.0" in _sample(observer)


class TestLabelCardinality:
    async def test_no_session_id_label(self, observer):
        await observer.on_push_frame(
            _pushed(_frame(TTFBMetricsData(processor="stt", model="m", value=0.1)))
        )
        text = _sample(observer)
        # The only labels are processor and model.
        for line in text.splitlines():
            if line.startswith("voice_turn_ttfb_seconds_count"):
                assert "session" not in line
                assert "user" not in line
                assert "call" not in line


def _pushed(frame):
    """A minimal FramePushed carrying the frame."""
    from pipecat.observers.base_observer import FramePushed

    return FramePushed(
        source=None,
        destination=None,
        frame=frame,
        direction=None,
        timestamp=0,
        first_push=True,
    )
