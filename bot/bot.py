"""The agent: one pipeline, two transports, three swappable layers.

Run it with the Pipecat development runner:

    python -m bot.bot -t webrtc     # browser client at http://localhost:7860/client
    python -m bot.bot -t eval       # the eval harness connects to /ws

**The pipeline.** transport.input -> STT -> user aggregator -> LLM ->
language routing -> TTS -> transport.output -> assistant aggregator.
The composition lives in ``bot.runtime.pipeline_builder`` so the local
runner and the authorized runtime build the same pipeline; this module
adds only the local runner's observer and app-message wiring.

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
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from pipecat.frames.frames import (
    STTUpdateSettingsFrame,
)
from pipecat.runner.utils import create_transport
from pipecat.transports.base_output import TransportParams

from bot.config import Settings
from bot.control import apply_mode, mount_control
from bot.language import parse_language_request
from bot.observers import PrometheusObserver, mount_metrics
from bot.runtime.pipeline_builder import (
    build_turn_strategies,
    build_vad_analyzer,
    build_voice_pipeline,
)
from bot.services import build_stt_language_router
from bot.session_settings import resolve_session_settings
from bot.transcripts import queue_client_utterance

# The dev runner's audio rates. 16 kHz in for Silero VAD's sake, 24 kHz
# out for the TTS engines.
AUDIO_IN_SAMPLE_RATE = 16000
AUDIO_OUT_SAMPLE_RATE = 24000

# Both STT providers are clients of the same local server and the same
# checkpoint; they differ in transport only. Anything that asks "does the
# STT need the server" must list them together.
MLX_AUDIO_STT_PROVIDERS = ("mlx-audio", "mlx-audio-realtime")

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


# Re-exported for the turn benchmark and the settings tests; the
# implementations live with the pipeline they configure.
__all__ = [
    "build_turn_strategies",
    "build_vad_analyzer",
    "bot",
    "build_model_resolver",
    "build_voice_pipeline",
    "get_prometheus_observer",
    "mount_api",
    "mount_observability",
    "needs_mlx_audio_server",
    "run_bot",
    "start_mlx_audio_server",
    "transport_params",
]


def _server_answers(root: str) -> bool:
    """Whether the local mlx-audio server is serving."""
    try:
        with urllib.request.urlopen(f"{root}/v1/models", timeout=3) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def needs_mlx_audio_server(settings: Settings) -> bool:
    """Whether the local mlx-audio server is needed by any layer.

    Both STT providers talk to it, one over HTTP and one over a WebSocket,
    and so does the mlx-audio TTS backend. A deployment that uses none of
    them does not need it at all, which is why this is a question about
    the configuration rather than a constant.
    """
    return settings.stt_provider in MLX_AUDIO_STT_PROVIDERS or settings.tts_provider == "mlx-audio"


def _server_clients(settings: Settings) -> list[str]:
    """The layers that talk to the local mlx-audio server."""
    clients = []
    if settings.stt_provider in MLX_AUDIO_STT_PROVIDERS:
        clients.append("stt")
    if settings.tts_provider == "mlx-audio":
        clients.append("tts")
    return clients


def _server_root(settings: Settings) -> str:
    """The root URL of the local mlx-audio server.

    Both layers point at the same server; the STT's URL is the canonical
    one when it is the STT that uses it.
    """
    url = settings.stt_base_url if settings.stt_provider in MLX_AUDIO_STT_PROVIDERS else settings.tts_base_url
    port = urlparse(url).port or 8020
    return f"http://127.0.0.1:{port}"


async def start_mlx_audio_server(settings: Settings) -> subprocess.Popen | None:
    """Make sure the local mlx-audio server is up, and own it if we start it.

    The mlx-audio STT and TTS backends both talk to a local
    ``mlx_audio.server``. `stt_autostart` is off by default so the server
    can be restarted independently of the agent; with it on, the agent
    owns the server's lifetime and stops it when it exits.

    A layer that *needs* the server is a different matter. The agent
    does not spawn a server the operator did not ask for — that would
    take the port away from whoever is managing it — but a bot with no
    voice is worse than a clear failure, so when the server is required
    and is not answering, the agent says which layer needs it and stops.

    Returns the process it started, or None if there was nothing to do.
    """
    clients = _server_clients(settings)
    required_by = " and ".join(clients)

    root = _server_root(settings)
    port = urlparse(root).port or 8020

    if _server_answers(root):
        logger.info(f"mlx-audio server already up on {root}")
        return None

    if not settings.stt_autostart:
        if clients:
            raise RuntimeError(
                f"the mlx-audio server is required by {required_by} "
                f"but is not answering on {root}"
            )
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
    if clients:
        raise RuntimeError(
            f"the mlx-audio server is required by {required_by} "
            f"but is not answering on {root}"
        )
    raise RuntimeError(f"the mlx-audio server did not come up on {root}")


async def run_bot(transport, settings: Settings, runner_args=None) -> None:
    """Build and run the pipeline on the given transport."""
    server = await start_mlx_audio_server(settings)
    try:
        await _run_pipeline(transport, settings, runner_args)
    finally:
        if server is not None:
            server.terminate()


async def _run_pipeline(transport, settings: Settings, runner_args=None) -> None:
    """Build the one voice pipeline and run it on the given transport.

    The composition lives in ``bot.runtime.pipeline_builder`` so the local
    entrypoint and the authorized runtime build the same pipeline. This
    function owns only what is specific to the local runner: the
    process-wide Prometheus observer (so ``/metrics`` and the pipeline
    share a registry) and the browser client's app-message channel.
    """
    assembly = build_voice_pipeline(
        transport,
        settings,
        instruments=get_prometheus_observer(settings),
        runner_args=runner_args,
    )

    # The client's language switch and text box ride on the transport's
    # app-message channel. Wire it before the runner starts the worker.
    _register_app_message_handler(transport, assembly.pipeline, settings)

    await assembly.run()


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
            try:
                delta = build_stt_language_router(settings).delta_for(language)
            except ValueError as exc:
                # A backend with no language switch (the realtime one)
                # says so here. The session stays on its configured mode;
                # the request is dropped and logged, not turned into a
                # broken turn.
                return
            await pipeline.queue_frame(STTUpdateSettingsFrame(delta=delta))
            logger.info(f"STT language set to {language or 'auto'}")
            return

        await queue_client_utterance(payload, pipeline)


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
    # Only Studio sessions that explicitly send an agent id receive a
    # profile. The prebuilt runner stays on the measured .env configuration.
    settings = Settings()
    from bot.agents.service import apply_active_agent_to_settings
    settings = await resolve_session_settings(
        settings, getattr(runner_args, "body", None), apply_active_agent_to_settings
    )
    # An explicit process-wide control-page STT mode override takes
    # precedence over a selected profile's STT provider.
    settings = apply_mode(settings)
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
        "LocalMlxRealtimeSTTService": settings.stt_model,
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

def mount_api(app, settings: Settings) -> None:
    """Mount the multi-agent REST API and CORS middleware on the runner's app."""
    from fastapi.middleware.cors import CORSMiddleware
    from bot.api.router import create_api_router

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{settings.web_port}",
            f"http://127.0.0.1:{settings.web_port}",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_api_router(settings), prefix="/api")
    logger.info("multi-agent REST API mounted at /api")


if __name__ == "__main__":
    import pipecat.runner.run as runner

    settings = Settings()

    # Tracing must be installed before the pipeline is constructed:
    # Pipecat's tracing decorators capture the tracer at decoration time.
    if settings.enable_tracing:
        from bot.tracing import setup_tracing

        setup_tracing(settings)

    # The STT switch owns "/" and "/settings". It is registered before
    # the runner mounts its own "/" -> "/client/" redirect, so route
    # order puts this page first.
    mount_control(runner.app, settings)
    # Mount the multi-agent REST API for web/
    mount_api(runner.app, settings)


    # Mount the metrics endpoint before the runner starts serving.
    mount_observability(settings)

    # The runner takes its host and port from its module globals, and
    # registers --host/--port itself, so a custom argparse parser would
    # collide with them.
    runner.RUNNER_HOST = settings.host
    runner.RUNNER_PORT = settings.port

    runner.main()
