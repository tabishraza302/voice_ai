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

import argparse
import asyncio
import json
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Awaitable, Callable
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
from bot.services import (
    LlmModelNotAvailableError,
    SttLanguageNotSupportedError,
    build_stt_language_router,
    validate_stt_language,
)
from bot.session_settings import InvalidAgentIdError, resolve_session_settings
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


async def run_bot(
    transport,
    settings: Settings,
    runner_args=None,
    *,
    flow_hooks=None,
    register_local_app_messages: bool = True,
) -> None:
    """Build and run the pipeline on the given transport."""
    server = await start_mlx_audio_server(settings)
    try:
        await _run_pipeline(
            transport,
            settings,
            runner_args,
            flow_hooks=flow_hooks,
            register_local_app_messages=register_local_app_messages,
        )
    finally:
        if server is not None:
            server.terminate()


async def _run_pipeline(
    transport,
    settings,
    runner_args=None,
    *,
    flow_hooks=None,
    register_local_app_messages: bool = True,
) -> None:
    """Build and run the shared local or platform-managed voice pipeline."""
    assembly = build_voice_pipeline(
        transport,
        settings,
        instruments=get_prometheus_observer(settings),
        runner_args=runner_args,
        flow_hooks=flow_hooks,
    )
    if register_local_app_messages:
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


async def close_peer_connection(connection: Any) -> None:
    """Close a failed SmallWebRTC peer connection so the browser is not left connected."""
    if connection is None:
        return
    try:
        if hasattr(connection, "disconnect") and callable(connection.disconnect):
            await connection.disconnect()
        elif hasattr(connection, "_close") and callable(connection._close):
            await connection._close()
        elif hasattr(connection, "pc") and connection.pc and callable(getattr(connection.pc, "close", None)):
            res = connection.pc.close()
            if asyncio.iscoroutine(res):
                await res
    except Exception as exc:
        logger.warning(f"Failed to cleanly disconnect WebRTC peer: {exc}")


class InvalidSessionBodyError(ValueError):
    """The session payload put configuration where the runner cannot read it."""


def extract_session_body(data: Any, *, source: str) -> dict[str, Any] | None:
    """Extract the session body from the route-correct field.

    Pipecat reads ``request_data`` on ``/api/offer`` and ``body`` on
    ``/start``; a top-level ``agent_id`` is accepted by neither. Similarly,
    sending ``agent_id`` in ``request_data`` to ``/start`` or in ``body`` to
    ``/api/offer`` is ignored by the runner and causes the bot to silently
    fall back to the default profile. Reject wrong nested fields as well as
    top-level ``agent_id``.
    """
    if source not in {"offer", "start"}:
        raise ValueError(f"unknown session body source {source!r}")
    if not isinstance(data, dict):
        return None
    if "agent_id" in data:
        raise InvalidSessionBodyError(
            "agent_id must be sent inside request_data for /api/offer "
            "or inside body for /start, not at the top level"
        )
    wrong_field = "body" if source == "offer" else "request_data"
    wrong_nested = data.get(wrong_field)
    if isinstance(wrong_nested, dict) and "agent_id" in wrong_nested:
        correct_field = "request_data" if source == "offer" else "body"
        route_name = "/api/offer" if source == "offer" else "/start"
        raise InvalidSessionBodyError(
            f"agent_id must be sent inside {correct_field} for {route_name}, "
            f"not inside {wrong_field}"
        )
    field = "request_data" if source == "offer" else "body"
    nested = data.get(field)
    return nested if isinstance(nested, dict) else None


async def preflight_session_settings(
    body: object,
    settings: Settings,
    profile_resolver: Callable[..., Awaitable[Settings]] | None = None,
) -> Settings:
    """Preflight validate session settings before WebRTC offer negotiation."""
    if profile_resolver is None:
        from bot.agents.service import apply_active_agent_to_settings

        profile_resolver = apply_active_agent_to_settings

    resolved = await resolve_session_settings(settings, body, profile_resolver)
    resolved = apply_mode(resolved)
    validate_stt_language(resolved)
    return resolved


def mount_offer_guard(
    app: Any,
    settings: Settings,
    profile_resolver: Callable[..., Awaitable[Settings]] | None = None,
) -> None:
    """Mount preflight validation on WebRTC offer routes.

    Validates explicitly requested agent profiles before the Pipecat runner
    negotiates an SDP answer. When an agent ID is missing, malformed, or
    rejected by the database or provider configuration, the offer request
    fails closed immediately with a user-visible HTTP status code (400, 404,
    503, or 500) rather than leaving the browser in a connected state with no
    running backend session. An unreachable PostgreSQL is a 503: the selected
    profile may still exist, so it is not reported as missing.
    """
    if getattr(app.state, "offer_guard_mounted", False):
        return
    app.state.offer_guard_mounted = True
    from starlette.responses import JSONResponse, Response
    from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
    from bot.agents.service import AgentNotFoundError
    from bot.db.session import StorageUnavailableError
    from bot.runtime.session_task import SessionAdmissionError


    class WebRTCOfferPreflightMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            method = request.method.upper()
            origin = request.headers.get("origin")
            if path == "/api/offer" and method == "POST":
                try:
                    body_bytes = await request.body()
                    data = json.loads(body_bytes) if body_bytes else {}
                except json.JSONDecodeError:
                    return JSONResponse(status_code=400, content={"detail": "Offer body must be valid JSON"})
                request_data = _session_request_data(data)
                if _has_session_credentials(request_data):
                    manager = getattr(app.state, "voice_session_manager", None)
                    if manager is None:
                        return JSONResponse(status_code=503, content={"detail": "Platform session runtime is unavailable"})
                    try:
                        pending = await manager.prepare_offer(request_data, origin=origin)
                    except SessionAdmissionError as exc:
                        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
                    try:
                        response = await call_next(request)
                        if not 200 <= response.status_code < 300:
                            await manager.abort_offer(pending.snapshot.session_id, reason="offer_failed")
                            return response
                        response_body = getattr(response, "body", None)
                        if response_body is None:
                            response_body = b"".join(
                                chunk async for chunk in response.body_iterator
                            )
                        answer = json.loads(response_body)
                        pc_id = answer.get("pc_id") if isinstance(answer, dict) else None
                        if not isinstance(pc_id, str):
                            raise SessionAdmissionError("SmallWebRTC answer has no pc_id", status_code=502)
                        await manager.bind_peer_pc(pending.snapshot.session_id, pc_id)
                        response_headers = {
                            key: value
                            for key, value in response.headers.items()
                            if key.lower() not in {"content-length", "transfer-encoding"}
                        }
                        return Response(
                            content=response_body,
                            status_code=response.status_code,
                            headers=response_headers,
                            media_type=response.media_type,
                            background=response.background,
                        )
                    except Exception as exc:
                        try:
                            await manager.abort_offer(pending.snapshot.session_id, reason="offer_failed")
                        except Exception:
                            logger.exception("Failed to release an unbound platform offer")
                        if isinstance(exc, SessionAdmissionError):
                            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
                        logger.exception("Platform offer negotiation failed")
                        return JSONResponse(status_code=502, content={"detail": "WebRTC offer could not be bound to its platform session"})
                if settings.platform_web_origin and origin == settings.platform_web_origin:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Platform browser offers require session_id and join_token"},
                    )

            if path == "/api/offer" and method == "PATCH" and origin == settings.platform_web_origin:
                manager = getattr(app.state, "voice_session_manager", None)
                if manager is None:
                    return JSONResponse(status_code=503, content={"detail": "Platform session runtime is unavailable"})
                try:
                    data = json.loads(await request.body())
                except json.JSONDecodeError:
                    return JSONResponse(status_code=400, content={"detail": "Candidate body must be valid JSON"})
                pc_id = data.get("pc_id") if isinstance(data, dict) else None
                if not isinstance(pc_id, str) or not manager.authorize_patch(pc_id, origin):
                    return JSONResponse(status_code=403, content={"detail": "Candidate update has no active session binding"})

            if method == "POST" and (
                path == "/api/offer"
                or path.endswith("/api/offer")
                or path == "/start"
            ):
                try:
                    body_bytes = await request.body()
                    if body_bytes:
                        data = json.loads(body_bytes)
                        source = "start" if path == "/start" else "offer"
                        session_body = extract_session_body(data, source=source)
                        if session_body and "agent_id" in session_body:
                            await preflight_session_settings(
                                session_body, settings, profile_resolver=profile_resolver
                            )
                except json.JSONDecodeError:
                    pass
                except InvalidSessionBodyError as exc:
                    logger.warning(f"WebRTC offer preflight rejected misplaced session body: {exc}")
                    return JSONResponse(status_code=400, content={"detail": str(exc)})
                except InvalidAgentIdError as exc:
                    logger.warning(f"WebRTC offer preflight rejected invalid agent id: {exc}")
                    return JSONResponse(status_code=400, content={"detail": f"Invalid agent ID: {exc}"})
                except SttLanguageNotSupportedError as exc:
                    logger.warning(f"WebRTC offer preflight rejected incompatible STT settings: {exc}")
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": (
                                "Incompatible speech recognition settings: mlx-audio-realtime does not support "
                                "pinned languages. Set language to auto or switch speech recognition to mlx-audio."
                            )
                        },
                    )
                except LlmModelNotAvailableError as exc:
                    logger.warning(f"WebRTC offer preflight rejected unavailable LLM model: {exc}")
                    return JSONResponse(status_code=503, content={"detail": f"Selected language model is unavailable: {exc}"})
                except (StorageUnavailableError, OperationalError, InterfaceError) as exc:
                    logger.warning(f"WebRTC offer preflight could not reach agent storage: {exc}")
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": (
                                "Agent storage is unavailable: PostgreSQL is not reachable, "
                                "so the requested agent profile cannot be verified. "
                                "Reconnect PostgreSQL and try again."
                            )
                        },
                    )
                except AgentNotFoundError as exc:
                    logger.warning(f"WebRTC offer preflight rejected missing agent: {exc}")
                    return JSONResponse(
                        status_code=404,
                        content={"detail": "The requested agent profile was not found or has been deleted."},
                    )
                except SQLAlchemyError as exc:
                    logger.error(f"WebRTC offer preflight failed on database error: {exc}")
                    return JSONResponse(
                        status_code=500,
                        content={"detail": "Database error: Unable to load agent configuration."},
                    )
                except Exception:
                    logger.exception("WebRTC offer preflight failed unexpectedly")
                    return JSONResponse(
                        status_code=500,
                        content={
                            "detail": (
                                "Voice session initialization failed: The server encountered an unexpected error "
                                "configuring the session."
                            )
                        },
                    )

            return await call_next(request)

    app.add_middleware(WebRTCOfferPreflightMiddleware)
    logger.info("WebRTC offer preflight guard mounted")


async def bot(runner_args) -> None:
    """The entrypoint the Pipecat dev runner discovers."""
    webrtc_connection = getattr(runner_args, "webrtc_connection", None)
    try:
        settings = Settings()
        body = getattr(runner_args, "body", None)
        request_data = _session_request_data(body)
        if _has_session_credentials(request_data):
            from bot.saas_runtime import get_session_manager

            manager = get_session_manager()
            if manager is None:
                raise RuntimeError("Platform session runtime is unavailable")
            await _run_platform_session(
                runner_args,
                manager,
                request_data,
                webrtc_connection,
            )
            return

        # Only Studio sessions that explicitly send an agent id receive a
        # profile. The prebuilt runner stays on the measured .env configuration.
        from bot.agents.service import apply_active_agent_to_settings

        settings = await resolve_session_settings(
            settings, body, apply_active_agent_to_settings
        )
        # An explicit process-wide control-page STT mode override takes
        # precedence over a selected profile's STT provider.
        settings = apply_mode(settings)
        validate_stt_language(settings)
        logger.info(
            f"voice-ai starting: stt={settings.stt_provider}/{settings.stt_model} "
            f"llm={settings.llm_provider}/{settings.llm_model} "
            f"tts={settings.tts_provider}/{settings.tts_model}"
        )

        transport = await create_transport(runner_args, transport_params())
        await run_bot(transport, settings, runner_args)
    except Exception as exc:
        logger.error(f"voice session failed to start or run: {exc}")
        if webrtc_connection is not None:
            await close_peer_connection(webrtc_connection)
        raise


def _session_request_data(body: object) -> object:
    if isinstance(body, dict) and "request_data" in body:
        return body["request_data"]
    return body


def _has_session_credentials(request_data: object) -> bool:
    return isinstance(request_data, dict) and (
        "session_id" in request_data or "join_token" in request_data
    )


async def _run_platform_session(
    runner_args,
    manager,
    request_data: object,
    webrtc_connection,
) -> None:
    """Run only a join admitted by the platform session manager."""
    pending = await manager.claim_pipeline(request_data)
    pending.connection = webrtc_connection
    activation_task = None
    pipeline_task = None
    try:
        settings = await manager.begin_pipeline(pending)
        validate_stt_language(settings)
        logger.info(
            f"platform voice session starting: stt={settings.stt_provider}/{settings.stt_model} "
            f"llm={settings.llm_provider}/{settings.llm_model} "
            f"tts={settings.tts_provider}/{settings.tts_model}"
        )
        transport = await create_transport(runner_args, transport_params())
        await manager.attach_transport(pending, transport, webrtc_connection)
        activation_task = asyncio.create_task(manager.wait_until_active(pending))
        pipeline_task = asyncio.create_task(
            run_bot(
                transport,
                settings,
                runner_args,
                flow_hooks=pending.runtime,
                register_local_app_messages=False,
            )
        )
        done, _ = await asyncio.wait(
            {activation_task, pipeline_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if activation_task in done:
            await activation_task
        if pipeline_task in done:
            await pipeline_task
        else:
            await pipeline_task
    finally:
        for task in (activation_task, pipeline_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await manager.finish_pipeline(pending, "runner_exit")

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

def mount_api(
    app,
    settings: Settings,
    profile_resolver: Callable[..., Awaitable[Settings]] | None = None,
) -> None:
    from fastapi.middleware.cors import CORSMiddleware
    from bot.api.router import create_api_router
    from bot.local_guard import mount_local_state_guard
    from bot.saas_runtime import mount_saas_runtime

    # Mount preflight validation before local state guard so that the local
    # security guard is added last and runs outermost (Starlette LIFO order).
    mount_saas_runtime(app, settings)
    mount_offer_guard(app, settings, profile_resolver=profile_resolver)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{settings.web_port}",
            f"http://127.0.0.1:{settings.web_port}",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_api_router(settings), prefix="/api")
    mount_local_state_guard(app, settings)
    # Guarantee LocalStateGuardMiddleware is outermost (index 0 in user_middleware)
    # even if another component (like mount_control) mounted it earlier.
    if hasattr(app, "user_middleware"):
        for idx, m in enumerate(app.user_middleware):
            if getattr(m.cls, "__name__", "") == "LocalStateGuardMiddleware":
                if idx > 0:
                    guard_m = app.user_middleware.pop(idx)
                    app.user_middleware.insert(0, guard_m)
                break
    logger.info("multi-agent REST API mounted at /api")

def validate_runner_bind_host(host: str) -> str:
    """Validate the final host Pipecat will bind, using Settings' guard.

    The dev runner registers its own ``--host`` flag, so the CLI value is
    the actual bind address and would otherwise bypass the field validator
    on ``Settings.host``. Reusing that validator keeps one definition of
    loopback — including its rejection of wildcards, non-loopback IPs,
    domain names and bracketed IPv6 forms.

    Returns the normalized host so the caller binds the value that was
    validated, not the raw CLI string.
    """
    return Settings(_env_file=None, host=host).host


class _LoopbackBindParser(argparse.ArgumentParser):
    """Argument parser that rejects a non-loopback final ``--host``.

    Pipecat adds its own ``--host``/``--port`` flags to this parser and
    parses them immediately before starting uvicorn, so validating here
    covers the actual value that will be bound, regardless of whether it
    came from ``Settings.host`` or an explicit CLI override.
    """

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        try:
            parsed.host = validate_runner_bind_host(parsed.host)
        except ValueError as exc:
            self.error(str(exc))
        return parsed


if __name__ == "__main__":
    import pipecat.runner.run as runner

    settings = Settings()

    # Tracing must be installed before the pipeline is constructed:
    # Pipecat's tracing decorators capture the tracer at decoration time.
    if settings.enable_tracing:
        from bot.tracing import setup_tracing

        setup_tracing(settings)

    # Mount the multi-agent REST API for web/
    mount_api(runner.app, settings)
    # The STT switch owns "/" and "/settings". It is registered before
    # the runner mounts its own "/" -> "/client/" redirect, so route
    # order puts this page first.
    mount_control(runner.app, settings)
    mount_observability(settings)

    # The runner takes its host and port from its module globals, and
    # registers --host/--port itself. The parser subclass only validates
    # the final --host value; it does not add or replace those flags.
    runner.RUNNER_HOST = settings.host
    runner.RUNNER_PORT = settings.port

    runner.main(parser=_LoopbackBindParser(description="Pipecat Development Runner"))
