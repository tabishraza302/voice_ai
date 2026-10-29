"""The runtime pipeline seam.

**One composition, two callers.** The local entrypoint (``bot.bot``) and
the later authorized runtime build the same voice pipeline here:

    transport input -> STT -> user aggregator -> context shaping ->
    [flow processors] -> LLM -> language routing -> TTS ->
    transport output -> assistant aggregator

The assistant aggregator stays *after* ``transport.output()`` so the
context records what was actually spoken: if the user interrupts
mid-sentence, the words that never reached the speaker are not in the
LLM's history.

**No concrete backend is named here.** STT, LLM and TTS come from the
registries in ``bot.services``; the voice router comes from the protocol
in ``bot.language``. Swapping a layer changes the registry, not this
file. ``bot/pipeline.py`` stays free of backend imports; the test suite
checks both properties mechanically.

**The optional seams.** ``flow_hooks`` is where a later runtime installs
provider-neutral flow processors, between context shaping and the LLM;
the local path passes ``None``. ``instruments`` is the Prometheus
observer that owns the process-wide instrument set; the local entrypoint
passes its singleton so ``/metrics`` and the pipeline share one registry.
When it is absent the builder creates a private observer, which is useful
for a test or a one-off pipeline but must not be used by a process that
serves metrics. Neither seam invents a platform session, admission or
billing type.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from bot.config import Settings
from bot.context import ContextShapingProcessor
from bot.observers import LatencyBridge, PrometheusObserver
from bot.pipeline import LanguageRoutingProcessor
from bot.services import build_llm, build_stt, build_tts, build_voice_router
from bot.transcripts import BotTranscriptForwarder, UserTranscriptForwarder


@runtime_checkable
class FlowHooks(Protocol):
    """Provider-neutral flow processors at the pipeline's frame boundaries.

    ``processors`` runs after context shaping and before the LLM. Optional
    hook methods can observe finalized user input, bind model output, observe
    TTS/audio frames, and close a flow runtime at pipeline teardown.
    """

    def processors(self) -> Sequence[FrameProcessor]:
        """Return processors inserted after context shaping and before the LLM."""
        ...

@dataclass(frozen=True)
class VoicePipelineAssembly:
    """The constructed pipeline and the objects that run it.

    Building does no I/O: ``build_voice_pipeline`` constructs services,
    processors and workers but starts no task, opens no connection and
    touches no network. ``run()`` owns the lifecycle the local entrypoint
    had before the seam existed: register the worker with the runner,
    start memory sampling, run the worker, stop sampling in ``finally``.
    """

    pipeline: Pipeline
    worker: PipelineWorker
    runner: WorkerRunner
    prometheus_observer: PrometheusObserver
    latency_bridge: LatencyBridge
    context: LLMContext
    flow_hooks: FlowHooks | None
    instruments: PrometheusObserver | None

    @property
    def processors(self) -> Sequence[FrameProcessor]:
        """The pipeline's processors, in order."""
        return self.pipeline.processors

    @property
    def observers(self) -> tuple[Any, ...]:
        """The observers attached to the worker, in order."""
        return (self.prometheus_observer, self.latency_bridge.observer)

    async def run(self) -> None:
        """Register the worker, then run it, sampling memory for its lifetime."""
        # ``WorkerRunner.run()`` runs the workers that were *added* to it.
        # A constructed-but-unregistered worker is never started, and the
        # runner then waits for a shutdown that no worker can produce.
        await self.runner.add_workers(self.worker)
        try:
            await self.prometheus_observer.start_memory_sampling()
            await self.runner.run()
        finally:
            try:
                close_flow = getattr(self.flow_hooks, "close", None)
                if callable(close_flow):
                    await close_flow()
            finally:
                await self.prometheus_observer.stop_memory_sampling()


def build_turn_strategies() -> UserTurnStrategies:
    """Explicit turn strategies, rather than Pipecat's defaults.

    ``MinWordsUserTurnStartStrategy(min_words=2)`` is the anti-echo
    guard: a single stray word — a cough, a laptop speaker leaking one
    syllable — cannot interrupt the bot. It only applies while the bot is
    speaking; one word still starts a turn when the bot is idle.

    The stop strategy is Smart Turn v3, which decides the turn has ended
    from the *meaning* of the audio rather than from a silence timer.
    It runs the bundled `smart-turn-v3.2-cpu.onnx` through onnxruntime,
    so it is local, needs no download, and covers Hindi.
    """
    return UserTurnStrategies(
        start=[MinWordsUserTurnStartStrategy(min_words=2)],
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
    )


def build_vad_analyzer(settings: Settings) -> SileroVADAnalyzer:
    """The VAD that segments user turns, configured from settings.

    The stop window is what decides whether a pause ends the turn.
    Pipecat's 0.2 s default closed a real question at its first "uh";
    ``Settings.vad_stop_secs`` is the one place that window is set.
    """
    return SileroVADAnalyzer(
        params=VADParams(
            stop_secs=settings.vad_stop_secs,
            confidence=settings.vad_confidence,
            min_volume=settings.vad_min_volume,
        )
    )


def build_voice_pipeline(
    transport,
    settings: Settings,
    flow_hooks: FlowHooks | None = None,
    instruments: PrometheusObserver | None = None,
    runner_args: Any = None,
) -> VoicePipelineAssembly:
    """Compose the voice pipeline for one transport.

    ``transport`` is already constructed: this function does not create
    it, so the local runner and the later authorized runtime can supply
    their own. ``runner_args`` is only read for ``handle_sigint``; it is
    optional so a caller that has no Pipecat runner args can still build
    the pipeline.
    """
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=build_vad_analyzer(settings),
            user_turn_strategies=build_turn_strategies(),
        ),
    )

    stt = build_stt(settings)
    llm = build_llm(settings)
    tts = build_tts(settings)

    context_shaper = ContextShapingProcessor()

    router = LanguageRoutingProcessor(
        voice_router=build_voice_router(settings),
        deadline_seconds=settings.first_chunk_deadline_seconds,
    )

    flow_processors = _flow_processors(flow_hooks)
    flow_input_processor = _optional_flow_processor(flow_hooks, "input_processor")
    flow_output_processors = _optional_flow_processors(flow_hooks, "output_processors")
    flow_audio_processor = _optional_flow_processor(flow_hooks, "audio_processor")
    # The Prometheus observer turns Pipecat's metrics frames into the
    # series that observability/prometheus.yml, alerts.yml and the
    # dashboard all reference; the latency bridge feeds it the
    # end-to-end user->bot number, which is the SLO metric.
    #
    # A caller that serves /metrics must pass the process-wide observer,
    # or the endpoint would serve a different, empty registry.
    prometheus_observer = instruments if instruments is not None else PrometheusObserver()
    latency_bridge = LatencyBridge(prometheus_observer)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            UserTranscriptForwarder(transport),
            *((flow_input_processor,) if flow_input_processor is not None else ()),
            user_aggregator,
            # Between the aggregator and the LLM: the history is rewritten
            # here, so the model never sees two user messages in a row.
            context_shaper,
            *flow_processors,
            llm,
            *flow_output_processors,
            router,
            tts,
            *((flow_audio_processor,) if flow_audio_processor is not None else ()),
            transport.output(),
            BotTranscriptForwarder(transport),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        enable_tracing=settings.enable_tracing,
        enable_turn_tracking=True,
        conversation_id=uuid.uuid4().hex,
        params=PipelineParams(
            enable_metrics=settings.enable_metrics,
            enable_usage_metrics=settings.enable_metrics,
        ),
        observers=[prometheus_observer, latency_bridge.observer],
    )

    # WorkerRunner owns the task manager the pipeline and its processors
    # share. Calling worker.run() directly would mean constructing the
    # WorkerParams yourself, and the transport's task manager would be
    # read before it is set.
    runner = WorkerRunner(handle_sigint=getattr(runner_args, "handle_sigint", True))

    return VoicePipelineAssembly(
        pipeline=pipeline,
        worker=worker,
        runner=runner,
        prometheus_observer=prometheus_observer,
        latency_bridge=latency_bridge,
        context=context,
        flow_hooks=flow_hooks,
        instruments=instruments,
    )


def _flow_processors(flow_hooks: FlowHooks | None) -> tuple[FrameProcessor, ...]:
    """The flow processors to install, validating the seam's contract."""
    if flow_hooks is None:
        return ()
    processors = tuple(flow_hooks.processors())
    for processor in processors:
        if not isinstance(processor, FrameProcessor):
            raise TypeError(
                "flow_hooks.processors() must return FrameProcessor instances; "
                f"got {type(processor).__name__}"
            )
    return processors


def _optional_flow_processor(
    flow_hooks: FlowHooks | None, method_name: str
) -> FrameProcessor | None:
    """Read and validate one optional flow processor hook."""
    if flow_hooks is None:
        return None
    method = getattr(flow_hooks, method_name, None)
    if method is None:
        return None
    processor = method()
    if processor is None:
        return None
    if not isinstance(processor, FrameProcessor):
        raise TypeError(
            f"flow_hooks.{method_name}() must return a FrameProcessor or None; "
            f"got {type(processor).__name__}"
        )
    return processor


def _optional_flow_processors(
    flow_hooks: FlowHooks | None, method_name: str
) -> tuple[FrameProcessor, ...]:
    """Read and validate an optional ordered flow processor hook."""
    if flow_hooks is None:
        return ()
    method = getattr(flow_hooks, method_name, None)
    if method is None:
        return ()
    processors = tuple(method())
    for processor in processors:
        if not isinstance(processor, FrameProcessor):
            raise TypeError(
                f"flow_hooks.{method_name}() must return FrameProcessor instances; "
                f"got {type(processor).__name__}"
            )
    return processors
