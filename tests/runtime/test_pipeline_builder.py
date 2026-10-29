"""The runtime pipeline seam tests: composition, boundaries, and lifecycle.

Task 6 moves pipeline composition out of ``bot.bot`` into
``bot.runtime.pipeline_builder`` so the local entrypoint and the authorized
runtime build the identical pipeline without duplicate wiring.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.frames.frames import STTUpdateSettingsFrame
from pipecat.pipeline.pipeline import PipelineSink
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMUserAggregator,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

import bot.bot as local_entrypoint
from bot.config import Settings
from bot.context import ContextShapingProcessor
from bot.observers import PrometheusObserver
from bot.pipeline import LanguageRoutingProcessor
from bot.runtime.pipeline_builder import (
    FlowHooks,
    VoicePipelineAssembly,
    build_turn_strategies,
    build_vad_analyzer,
    build_voice_pipeline,
)
from bot.transcripts import BotTranscriptForwarder, UserTranscriptForwarder

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

FORBIDDEN_BACKENDS = {
    "KokoroTTSService",
    "LocalMlxTTSService",
    "WhisperSTTServiceMLX",
    "LocalMlxSTTService",
    "FunASRSTTService",
    "OLLamaLLMService",
    "OpenAILLMService",
}


def _imported_names(path: pathlib.Path) -> set[str]:
    """Collect all imported symbol names from a Python source file."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
    return names


class FakeTransport:
    """Frame processors and app-message handler storage for testing."""

    def __init__(self) -> None:
        self.input_processor = FrameProcessor(name="fake-input")
        self.output_processor = FrameProcessor(name="fake-output")
        self.handlers: dict[str, Any] = {}

    def input(self) -> FrameProcessor:
        return self.input_processor

    def output(self) -> FrameProcessor:
        return self.output_processor

    def event_handler(self, name: str):
        def register(handler: Any) -> Any:
            self.handlers[name] = handler
            return handler

        return register


class RecordingPipeline:
    """Stands in for ``Pipeline`` where only ``queue_frame`` is needed."""

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def queue_frame(self, frame: Any) -> None:
        self.frames.append(frame)


class FixedFlowHooks:
    """Test implementation of FlowHooks protocol."""

    def __init__(self, *processors: FrameProcessor) -> None:
        self._processors = processors

    def processors(self) -> tuple[FrameProcessor, ...]:
        return self._processors


class RecordingRunner:
    """Stands in for ``WorkerRunner`` to record lifecycle calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def add_workers(self, *workers: Any) -> None:
        self.calls.append(("add_workers", workers))

    async def run(self) -> None:
        self.calls.append(("run", ()))


class RecordingObserver:
    """Stands in for ``PrometheusObserver`` to record memory sampling."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start_memory_sampling(self) -> None:
        self.calls.append("start")

    async def stop_memory_sampling(self) -> None:
        self.calls.append("stop")


def _only_index(processors: list[Any], predicate: Any) -> int:
    matches = [index for index, processor in enumerate(processors) if predicate(processor)]
    assert len(matches) == 1, f"expected exactly one match, got {matches}"
    return matches[0]


def _settings(**overrides: Any) -> Settings:
    """Test settings with tracing disabled to avoid OTel provider setup."""
    return Settings(_env_file=None, enable_tracing=False, **overrides)


class TestBuilderComposition:
    """The builder must compose the exact pipeline stages and order."""

    @pytest.mark.asyncio
    async def test_builder_composes_same_processors_order_for_fake_services(
        self, monkeypatch
    ):
        fake_stt = FrameProcessor(name="fake-stt")
        fake_llm = FrameProcessor(name="fake-llm")
        fake_tts = FrameProcessor(name="fake-tts")

        monkeypatch.setattr("bot.runtime.pipeline_builder.build_stt", lambda s: fake_stt)
        monkeypatch.setattr("bot.runtime.pipeline_builder.build_llm", lambda s: fake_llm)
        monkeypatch.setattr("bot.runtime.pipeline_builder.build_tts", lambda s: fake_tts)

        transport = FakeTransport()
        settings = _settings()
        assembly = build_voice_pipeline(transport, settings)

        processors = list(assembly.processors)
        assert isinstance(processors[-1], PipelineSink)

        # Expected 11 pipeline stages + PipelineSink at end
        stages = {
            "input": _only_index(processors, lambda p: p is transport.input_processor),
            "stt": _only_index(processors, lambda p: p is fake_stt),
            "user_transcript": _only_index(
                processors, lambda p: isinstance(p, UserTranscriptForwarder)
            ),
            "user_aggregator": _only_index(
                processors, lambda p: isinstance(p, LLMUserAggregator)
            ),
            "context_shaping": _only_index(
                processors, lambda p: isinstance(p, ContextShapingProcessor)
            ),
            "llm": _only_index(processors, lambda p: p is fake_llm),
            "router": _only_index(
                processors, lambda p: isinstance(p, LanguageRoutingProcessor)
            ),
            "tts": _only_index(processors, lambda p: p is fake_tts),
            "output": _only_index(processors, lambda p: p is transport.output_processor),
            "bot_transcript": _only_index(
                processors, lambda p: isinstance(p, BotTranscriptForwarder)
            ),
            "assistant_aggregator": _only_index(
                processors, lambda p: isinstance(p, LLMAssistantAggregator)
            ),
        }

        expected_order = [
            "input",
            "stt",
            "user_transcript",
            "user_aggregator",
            "context_shaping",
            "llm",
            "router",
            "tts",
            "output",
            "bot_transcript",
            "assistant_aggregator",
        ]
        expected_indices = list(range(1, 12))
        actual_indices = [stages[name] for name in expected_order]
        assert actual_indices == sorted(actual_indices)
        assert actual_indices == expected_indices
        # Invariant: assistant aggregator strictly follows output
        assert stages["assistant_aggregator"] > stages["output"]

    @pytest.mark.asyncio
    async def test_builder_uses_configured_vad_and_turn_strategies(self):
        settings = _settings(vad_stop_secs=1.5, vad_confidence=0.85)
        vad = build_vad_analyzer(settings)
        assert vad.params.stop_secs == 1.5
        assert vad.params.confidence == 0.85

        strategies = build_turn_strategies()
        assert len(strategies.start) == 1
        assert len(strategies.stop) == 1


class TestModularityBackendIndependence:
    """Neither bot/pipeline.py nor bot/runtime/pipeline_builder.py may import a concrete backend."""

    def test_pipeline_module_imports_no_concrete_backend(self):
        imported = _imported_names(REPO_ROOT / "bot" / "pipeline.py")
        forbidden_imported = imported & FORBIDDEN_BACKENDS
        assert not forbidden_imported, (
            f"bot/pipeline.py must not import concrete backends; found {forbidden_imported}"
        )

    def test_pipeline_builder_imports_no_concrete_backend(self):
        imported = _imported_names(REPO_ROOT / "bot" / "runtime" / "pipeline_builder.py")
        forbidden_imported = imported & FORBIDDEN_BACKENDS
        assert not forbidden_imported, (
            "bot/runtime/pipeline_builder.py must not import concrete backends; "
            f"found {forbidden_imported}"
        )


class TestLocalEntrypointUsesBuilder:
    """The local bot entrypoint must use the builder with no second pipeline construction."""

    def test_bot_py_has_no_duplicate_pipeline_construction(self):
        tree = ast.parse((REPO_ROOT / "bot" / "bot.py").read_text())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        forbidden_constructors = {"Pipeline", "PipelineWorker", "WorkerRunner"}
        assert not (forbidden_constructors & called), (
            f"bot/bot.py must not construct {forbidden_constructors & called}; "
            "bot.runtime.pipeline_builder is the single pipeline composition point"
        )

    @pytest.mark.asyncio
    async def test_local_run_pipeline_invokes_builder_and_runs_assembly(
        self, monkeypatch
    ):
        settings = _settings(stt_provider="mlx-audio")
        transport = FakeTransport()
        observer = PrometheusObserver()
        runner_args = SimpleNamespace(handle_sigint=False)
        builder_calls: list[dict[str, Any]] = []
        assembly_runs: list[bool] = []

        class FakeAssembly:
            def __init__(self) -> None:
                self.pipeline = RecordingPipeline()

            async def run(self) -> None:
                assembly_runs.append(True)

        def spy_builder(
            transport_arg: Any,
            settings_arg: Any,
            flow_hooks: Any = None,
            instruments: Any = None,
            runner_args: Any = None,
        ) -> Any:
            builder_calls.append(
                {
                    "transport": transport_arg,
                    "settings": settings_arg,
                    "flow_hooks": flow_hooks,
                    "instruments": instruments,
                    "runner_args": runner_args,
                }
            )
            return FakeAssembly()

        monkeypatch.setattr(local_entrypoint, "build_voice_pipeline", spy_builder)
        monkeypatch.setattr(
            local_entrypoint, "get_prometheus_observer", lambda s=None: observer
        )

        await local_entrypoint._run_pipeline(transport, settings, runner_args)

        assert len(builder_calls) == 1
        call = builder_calls[0]
        assert call["transport"] is transport
        assert call["settings"] is settings
        assert call["flow_hooks"] is None
        assert call["instruments"] is observer
        assert call["runner_args"] is runner_args
        assert assembly_runs == [True]

    @pytest.mark.asyncio
    async def test_local_entrypoint_preserves_app_message_wiring(self, monkeypatch):
        settings = _settings(stt_provider="mlx-audio")
        transport = FakeTransport()
        recording_pipeline = RecordingPipeline()

        class FakeAssembly:
            def __init__(self) -> None:
                self.pipeline = recording_pipeline

            async def run(self) -> None:
                pass

        monkeypatch.setattr(
            local_entrypoint,
            "build_voice_pipeline",
            lambda *a, **kw: FakeAssembly(),
        )
        monkeypatch.setattr(
            local_entrypoint,
            "get_prometheus_observer",
            lambda s=None: PrometheusObserver(),
        )

        await local_entrypoint._run_pipeline(transport, settings)

        assert "on_app_message" in transport.handlers
        handler = transport.handlers["on_app_message"]

        # Language switch frame
        await handler(transport, {"type": "set_language", "language": "en"}, None)
        assert len(recording_pipeline.frames) == 1
        assert isinstance(recording_pipeline.frames[0], STTUpdateSettingsFrame)

        # Injected user speech frames
        await handler(transport, {"text": "namaste"}, None)
        frame_names = [type(f).__name__ for f in recording_pipeline.frames[1:]]
        assert frame_names == [
            "UserStartedSpeakingFrame",
            "TranscriptionFrame",
            "UserStoppedSpeakingFrame",
        ]


class TestOptionalSeamsFlowHooksAndInstruments:
    """flow_hooks and instruments must be optional and not change behavior when absent."""

    @pytest.mark.asyncio
    async def test_absence_of_flow_hooks_leaves_pipeline_untouched(self):
        transport = FakeTransport()
        settings = _settings()

        baseline_assembly = build_voice_pipeline(
            transport, settings, flow_hooks=None, instruments=None
        )
        baseline_types = [type(p) for p in baseline_assembly.processors]

        # Call with no flow_hooks or instruments at all
        default_assembly = build_voice_pipeline(transport, settings)
        default_types = [type(p) for p in default_assembly.processors]

        assert baseline_types == default_types
        assert baseline_assembly.flow_hooks is None
        assert default_assembly.flow_hooks is None

    @pytest.mark.asyncio
    async def test_flow_hooks_installed_between_context_shaping_and_llm(self):
        flow_p1 = FrameProcessor(name="flow_1")
        flow_p2 = FrameProcessor(name="flow_2")
        hooks = FixedFlowHooks(flow_p1, flow_p2)

        assembly = build_voice_pipeline(
            FakeTransport(), _settings(), flow_hooks=hooks
        )
        processors = list(assembly.processors)

        shaper_idx = _only_index(
            processors, lambda p: isinstance(p, ContextShapingProcessor)
        )
        llm_idx = _only_index(processors, lambda p: isinstance(p, LLMService))
        p1_idx = _only_index(processors, lambda p: p is flow_p1)
        p2_idx = _only_index(processors, lambda p: p is flow_p2)

        assert shaper_idx < p1_idx < p2_idx < llm_idx
        assert assembly.flow_hooks is hooks

    @pytest.mark.asyncio
    async def test_invalid_flow_hooks_raises_type_error(self):
        class NonProcessorHooks:
            def processors(self) -> list[Any]:
                return [object()]

        with pytest.raises(TypeError, match="FrameProcessor"):
            build_voice_pipeline(
                FakeTransport(), _settings(), flow_hooks=NonProcessorHooks()
            )

    @pytest.mark.asyncio
    async def test_instruments_optional_creates_fresh_observer_when_absent(self):
        assembly = build_voice_pipeline(FakeTransport(), _settings(), instruments=None)

        assert assembly.instruments is None
        assert isinstance(assembly.prometheus_observer, PrometheusObserver)
        # LatencyBridge connects to the assembly's prometheus observer
        assert assembly.observers == (
            assembly.prometheus_observer,
            assembly.latency_bridge.observer,
        )

    @pytest.mark.asyncio
    async def test_instruments_injected_shares_exact_observer(self):
        shared_observer = PrometheusObserver()
        assembly = build_voice_pipeline(
            FakeTransport(), _settings(), instruments=shared_observer
        )

        assert assembly.instruments is shared_observer
        assert assembly.prometheus_observer is shared_observer
        assert assembly.observers == (
            shared_observer,
            assembly.latency_bridge.observer,
        )


class TestAssemblyExecutionLifecycle:
    """WorkerRunner must add workers before running; sampling stops on completion or error."""

    @pytest.mark.asyncio
    async def test_run_adds_worker_and_samples_memory(self):
        runner = RecordingRunner()
        observer = RecordingObserver()
        fake_worker = object()

        assembly = VoicePipelineAssembly(
            pipeline=None,  # type: ignore[arg-type]
            worker=fake_worker,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            prometheus_observer=observer,  # type: ignore[arg-type]
            latency_bridge=SimpleNamespace(observer=object()),  # type: ignore[arg-type]
            context=None,  # type: ignore[arg-type]
            flow_hooks=None,
            instruments=None,
        )

        await assembly.run()

        assert runner.calls == [("add_workers", (fake_worker,)), ("run", ())]
        assert observer.calls == ["start", "stop"]

    @pytest.mark.asyncio
    async def test_run_stops_memory_sampling_when_runner_fails(self):
        class FailingRunner(RecordingRunner):
            async def run(self) -> None:
                self.calls.append(("run", ()))
                raise RuntimeError("pipeline worker crashed")

        runner = FailingRunner()
        observer = RecordingObserver()

        assembly = VoicePipelineAssembly(
            pipeline=None,  # type: ignore[arg-type]
            worker=object(),  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            prometheus_observer=observer,  # type: ignore[arg-type]
            latency_bridge=SimpleNamespace(observer=object()),  # type: ignore[arg-type]
            context=None,  # type: ignore[arg-type]
            flow_hooks=None,
            instruments=None,
        )

        with pytest.raises(RuntimeError, match="pipeline worker crashed"):
            await assembly.run()

        assert observer.calls == ["start", "stop"]
