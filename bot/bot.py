"""The agent: one pipeline, two transports, three swappable layers.

Run it with the Pipecat development runner:

    python -m bot.bot -t webrtc     # browser client at http://localhost:7860/client
    python -m bot.bot -t eval       # the eval harness connects to /ws

**The pipeline.** transport.input -> STT -> user aggregator -> LLM ->
language routing -> TTS -> transport.output -> assistant aggregator.

The assistant aggregator goes *after* transport.output() so that the
context records what was actually spoken: if the user interrupts
mid-sentence, the words that never reached the speaker should not be in
the LLM's history.

**The transports.** The runner hands the bot a ``runner_args`` object and
expects a ``bot(runner_args)`` coroutine. The transport is built here
from a params factory, so the same agent serves the browser and the eval
harness — there is one pipeline, not two.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import STTUpdateSettingsFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.utils import create_transport
from pipecat.transports.base_output import TransportParams
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner

from bot.config import Settings
from bot.context import ContextShapingProcessor
from bot.language import parse_language_request
from bot.observers import LatencyBridge, PrometheusObserver, mount_metrics
from bot.pipeline import LanguageRoutingProcessor
from bot.services import (
    build_llm,
    build_stt,
    build_stt_language_router,
    build_tts,
    build_voice_router,
    resolve_models,
)

# The dev runner's audio rates. 16 kHz in for Silero VAD's sake, 24 kHz
# out for the TTS engines.
AUDIO_IN_SAMPLE_RATE = 16000
AUDIO_OUT_SAMPLE_RATE = 24000

# The process-wide Prometheus observer. See get_prometheus_observer().
_PROMETHEUS_OBSERVER: PrometheusObserver | None = None


def transport_params() -> dict[str, Callable]:
    """Transport parameter factories, keyed by transport name.

    ``create_transport`` looks up the key that matches the runner
    arguments. "eval" is the harness's WebSocket; it is detected from the
    connection handshake, so the same server serves both the browser and
    the harness.
    """
    from pipecat.evals.transport import EvalTransportParams

    return {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=AUDIO_IN_SAMPLE_RATE,
            audio_out_sample_rate=AUDIO_OUT_SAMPLE_RATE,
        ),
        "eval": lambda: EvalTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=AUDIO_IN_SAMPLE_RATE,
            audio_out_sample_rate=AUDIO_OUT_SAMPLE_RATE,
        ),
    }


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
    return SileroVADAnalyzer(params=VADParams(stop_secs=settings.vad_stop_secs))


def _server_answers(root: str) -> bool:
    """Whether the local mlx-audio server is serving."""
    try:
        with urllib.request.urlopen(f"{root}/v1/models", timeout=3) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


async def start_stt_server(settings: Settings) -> subprocess.Popen | None:
    """Start the local mlx-audio server, when the configuration asks for it.

    The mlx-audio STT and TTS backends both talk to a local
    ``mlx_audio.server``. `stt_autostart` is off by default so the server
    can be restarted independently of the agent; with it on, the agent
    owns the server's lifetime and stops it when it exits.

    Returns the process it started, or None if there was nothing to do.
    """
    if not settings.stt_autostart:
        return None

    port = urlparse(settings.stt_base_url).port or 8020
    root = f"http://127.0.0.1:{port}"
    if _server_answers(root):
        logger.info(f"mlx-audio server already up on {root}")
        return None

    command = shutil.which("mlx_audio.server") or "mlx_audio.server"
    logger.info(f"starting the mlx-audio server on {root}")
    process = subprocess.Popen(
        [command, "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + 180
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("the mlx-audio server exited before it was ready")
        if _server_answers(root):
            return process
        await asyncio.sleep(1.0)

    process.terminate()
    raise RuntimeError(f"the mlx-audio server did not come up on {root}")


async def run_bot(transport, settings: Settings, runner_args=None) -> None:
    """Build and run the pipeline on the given transport."""
    stt_server = await start_stt_server(settings)
    try:
        await _run_pipeline(transport, settings, runner_args)
    finally:
        if stt_server is not None:
            stt_server.terminate()


async def _run_pipeline(transport, settings: Settings, runner_args=None) -> None:
    """Build and run the pipeline on the given transport."""
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

    router = LanguageRoutingProcessor(voice_router=build_voice_router(settings))

    # Observers. The Prometheus observer turns Pipecat's metrics frames
    # into the series that observability/prometheus.yml, alerts.yml and
    # the dashboard all reference; the latency bridge feeds it the
    # end-to-end user->bot number, which is the SLO metric.
    #
    # It is a process-wide singleton: the /metrics endpoint and the
    # pipeline must expose the same registry, or the endpoint would serve
    # an empty set of series.
    prometheus_observer = get_prometheus_observer(settings)
    latency_bridge = LatencyBridge(prometheus_observer)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            # Between the aggregator and the LLM: the history is rewritten
            # here, so the model never sees two user messages in a row.
            context_shaper,
            llm,
            router,
            tts,
            transport.output(),
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

    _register_app_message_handler(transport, pipeline, settings)

    # WorkerRunner owns the task manager the pipeline and its processors
    # share. Calling worker.run() directly would mean constructing the
    # WorkerParams yourself, and the transport's task manager would be
    # read before it is set.
    runner = WorkerRunner(handle_sigint=getattr(runner_args, "handle_sigint", True))
    await runner.add_workers(worker)

    await prometheus_observer.start_memory_sampling()
    try:
        await runner.run()
    finally:
        await prometheus_observer.stop_memory_sampling()


def _register_app_message_handler(transport, pipeline, settings: Settings) -> None:
    """Let the browser client steer the language, and speak as the user.

    The prebuilt client has no language selector, so the override rides
    on the RTVI message channel:

    * ``{"type": "set_language", "language": "en"|"hi"|"auto"}`` forces
      the STT language. It goes through the same router as everything
      else, so the delta is correct for whichever STT is registered.
    * any other message with a ``text`` field is treated as a user
      utterance — that is what the client's message box sends.
    """

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):  # noqa: ANN001
        payload = _parse_app_message(message)
        if not payload:
            return

        kind = payload.get("type")

        if kind == "set_language":
            try:
                language = parse_language_request(payload.get("language"))
            except ValueError as exc:
                logger.warning(f"ignoring set_language: {exc}")
                return
            delta = build_stt_language_router(settings).delta_for(language)
            await pipeline.push_frame(STTUpdateSettingsFrame(delta=delta))
            logger.info(f"STT language set to {language or 'auto'}")
            return

        text = payload.get("text")
        if text:
            await pipeline.push_frame(
                TranscriptionFrame(text, "client", time_now_iso8601())
            )


def _parse_app_message(message: Any) -> dict[str, Any] | None:
    """Normalize an app message into a dict, or None if it is not one."""
    if isinstance(message, dict):
        return message
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8", "replace")
    if isinstance(message, str):
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return {"type": "send-text", "text": message}
        return parsed if isinstance(parsed, dict) else None
    return None


async def bot(runner_args) -> None:
    """The entrypoint the Pipecat dev runner discovers."""
    settings = resolve_models(Settings())
    logger.info(
        f"voice-ai starting: stt={settings.stt_provider}/{settings.stt_model} "
        f"llm={settings.llm_provider}/{settings.llm_model} "
        f"tts={settings.tts_provider}/{settings.tts_model}"
    )

    transport = await create_transport(runner_args, transport_params())
    await run_bot(transport, settings, runner_args)


def build_model_resolver(settings: Settings):
    """Map a processor name to the model id configured behind it.

    Pipecat's STT and TTS metrics frames carry no model name, so without
    this every STT and TTS series would be labelled "unknown" and the
    dashboard could not say which model served a turn.

    The processor names Pipecat uses are the service class names, with a
    "#0" instance suffix, so the match is by prefix.
    """
    table = {
        "LocalMlxSTTService": settings.stt_model,
        "WhisperSTTServiceMLX": settings.stt_model,
        "KokoroTTSService": settings.tts_model,
        "LocalMlxTTSService": settings.tts_model,
        "OLLamaLLMService": settings.llm_model,
        "OpenAILLMService": settings.llm_model,
        # The router measures how long the LLM's tokens took to become a
        # sentence, so the model behind that wait is the LLM's.
        "LanguageRoutingProcessor": settings.llm_model,
    }

    def resolve(processor: str) -> str | None:
        for name, model in table.items():
            if processor.startswith(name):
                return model
        return None

    return resolve


def get_prometheus_observer(settings: Settings | None = None) -> PrometheusObserver:
    """The process-wide Prometheus observer.

    A singleton because the /metrics endpoint and the pipeline must
    expose the same registry. Two registries would mean the endpoint
    serves an empty set of series.
    """
    global _PROMETHEUS_OBSERVER
    if _PROMETHEUS_OBSERVER is None:
        _PROMETHEUS_OBSERVER = PrometheusObserver(
            model_resolver=build_model_resolver(settings) if settings else None
        )
    return _PROMETHEUS_OBSERVER


def mount_observability(settings: Settings) -> None:
    """Mount the Prometheus endpoint on the runner's app.

    Called once at import time, before the runner starts, so that the
    metrics endpoint is live even before the first session connects.
    """
    from pipecat.runner.run import app

    mount_metrics(app, get_prometheus_observer(settings).registry)
    logger.info(f"prometheus metrics mounted at http://localhost:{settings.port}/metrics/")


if __name__ == "__main__":
    import pipecat.runner.run as runner

    settings = Settings()

    # Tracing must be installed before the pipeline is constructed:
    # Pipecat's tracing decorators capture the tracer at decoration time.
    if settings.enable_tracing:
        from bot.tracing import setup_tracing

        setup_tracing(settings)

    # Mount the metrics endpoint before the runner starts serving.
    mount_observability(settings)

    # The runner takes its host and port from its module globals, and
    # registers --host/--port itself, so a custom argparse parser would
    # collide with them.
    runner.RUNNER_HOST = settings.host
    runner.RUNNER_PORT = settings.port

    runner.main()
