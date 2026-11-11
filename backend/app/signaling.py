from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from aiortc import RTCIceServer
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    IceCandidate,
    SmallWebRTCRequest,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequestHandler,
)

from .voice import CallRuntime, VoiceUnavailableError, create_call_runtime


router = APIRouter(prefix="/signaling", tags=["signaling"])
_TOKEN_SIZE_BYTES = 32
_TOKEN_SECONDS = 120
_MAX_OFFER_BYTES = 1_048_576
_MAX_CANDIDATE_BYTES = 65_536
_MAX_SDP_CHARS = 131_072
_MAX_CANDIDATES = 64
_RATE_WINDOW_SECONDS = 60
_IP_REQUESTS_PER_WINDOW = 60
_AGENT_REQUESTS_PER_WINDOW = 120
_CONNECTION_START_TIMEOUT_SECONDS = 35


@dataclass(frozen=True, slots=True)
class SignalingGrant:
    session_id: uuid.UUID
    agent_id: uuid.UUID
    call_type: Literal["test", "public"]
    expires_at: float


@dataclass(slots=True)
class _Reservation:
    session_id: uuid.UUID
    agent_id: uuid.UUID


@dataclass(slots=True)
class _LiveCall:
    runtime: CallRuntime
    pc_id: str
    renegotiation_token_hash: bytes
    handler: SmallWebRTCRequestHandler
    renegotiation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _RateLimiter:
    def __init__(self) -> None:
        self._requests: dict[tuple[str, str], deque[float]] = {}

    def check_ip(self, client_ip: str | None) -> None:
        self._check_key(("ip", client_ip or "unknown"), time.monotonic(), _IP_REQUESTS_PER_WINDOW)

    def check_agent(self, agent_id: uuid.UUID) -> None:
        self._check_key(("agent", str(agent_id)), time.monotonic(), _AGENT_REQUESTS_PER_WINDOW)

    def check(self, client_ip: str | None, agent_id: uuid.UUID | None = None) -> None:
        self.check_ip(client_ip)
        if agent_id is not None:
            self.check_agent(agent_id)

    def _check_key(self, key: tuple[str, str], now: float, maximum: int) -> None:
        cutoff = now - _RATE_WINDOW_SECONDS
        events = self._requests.get(key)
        if events is None:
            if len(self._requests) >= 10_000:
                for old_key, old_events in tuple(self._requests.items()):
                    while old_events and old_events[0] <= cutoff:
                        old_events.popleft()
                    if not old_events:
                        self._requests.pop(old_key, None)
                if len(self._requests) >= 10_000:
                    raise HTTPException(
                        status_code=429,
                        detail="Signaling rate limiter is at capacity.",
                        headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
                    )
            events = deque()
            self._requests[key] = events
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= maximum:
            raise HTTPException(
                status_code=429,
                detail="Signaling request rate limit exceeded.",
                headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
            )
        events.append(now)

class _OfferBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sdp: str = Field(min_length=1, max_length=_MAX_SDP_CHARS)
    type: Literal["offer"]
    signaling_token: str | None = Field(default=None, min_length=1, max_length=256)
    pc_id: str | None = Field(default=None, min_length=1, max_length=256)
    renegotiation_token: str | None = Field(default=None, min_length=1, max_length=256)
    restart_pc: bool = False

    @model_validator(mode="after")
    def credentials_match_request(self) -> _OfferBody:
        if self.pc_id is None:
            if self.signaling_token is None or self.renegotiation_token is not None or self.restart_pc:
                raise ValueError("An initial offer requires a signaling token and no peer connection ID.")
        elif self.renegotiation_token is None or self.signaling_token is not None:
            raise ValueError("A renegotiation requires its live connection credential.")
        return self


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: str = Field(max_length=4_096)
    sdp_mid: str = Field(max_length=128)
    sdp_mline_index: int = Field(ge=0, le=128)


class _CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pc_id: str = Field(min_length=1, max_length=256)
    renegotiation_token: str = Field(min_length=1, max_length=256)
    candidates: list[_Candidate] = Field(max_length=_MAX_CANDIDATES)


_tokens: dict[bytes, SignalingGrant] = {}
_token_tasks: dict[bytes, asyncio.Task[None]] = {}
_token_lock = asyncio.Lock()
_registry_lock = asyncio.Lock()
_rate_limiter = _RateLimiter()
_reservations: dict[uuid.UUID, _Reservation] = {}
_live_calls_by_pc: dict[str, _LiveCall] = {}
_live_calls_by_session: dict[uuid.UUID, _LiveCall] = {}


def issue_signaling_token(
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    call_type: Literal["test", "public"],
) -> str:
    if call_type not in {"test", "public"}:
        raise ValueError("Unsupported call type")
    from .sessions import SIGNALING_TOKEN_TTL_SECONDS

    token = secrets.token_urlsafe(_TOKEN_SIZE_BYTES)
    key = hashlib.sha256(token.encode("utf-8")).digest()
    token_ttl = min(_TOKEN_SECONDS, SIGNALING_TOKEN_TTL_SECONDS)
    expires_at = time.monotonic() + token_ttl
    _tokens[key] = SignalingGrant(session_id, agent_id, call_type, expires_at)
    try:
        task = asyncio.get_running_loop().create_task(_expire_unused_token(key, token_ttl))
    except RuntimeError:
        task = None
    if task is not None:
        _token_tasks[key] = task
    return token


async def _expire_unused_token(key: bytes, token_ttl: int) -> None:
    try:
        await asyncio.sleep(token_ttl)
    except asyncio.CancelledError:
        return
    async with _token_lock:
        grant = _tokens.pop(key, None)
        _token_tasks.pop(key, None)
    if grant is not None:
        try:
            from .sessions import set_session_status

            await set_session_status(grant.session_id, "expired")
        except Exception:
            return

async def _consume_signaling_token(token: str) -> SignalingGrant:
    key = hashlib.sha256(token.encode("utf-8")).digest()
    async with _token_lock:
        grant = _tokens.pop(key, None)
        task = _token_tasks.pop(key, None)
        if task is not None:
            task.cancel()
    if grant is None or grant.expires_at <= time.monotonic():
        if grant is not None:
            try:
                from .sessions import set_session_status

                await set_session_status(grant.session_id, "expired")
            except Exception:
                pass
        raise HTTPException(status_code=401, detail="Signaling token is invalid or expired.")
    return grant


def check_call_creation_rate_limit(client_ip: str | None, agent_id: uuid.UUID) -> None:
    """Shared process-local IP and agent limit for session creation and signaling."""
    _rate_limiter.check(client_ip, agent_id)


def _validate_ice_url(value: Any, allowed_schemes: frozenset[str]) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(character.isspace() or ord(character) < 33 for character in value)
        or "#" in value
        or "@" in value
    ):
        raise ValueError("invalid ICE URL")
    scheme, separator, address = value.partition(":")
    authority = address.split("?", 1)[0]
    if (
        not separator
        or scheme.casefold() not in allowed_schemes
        or not authority
        or authority.startswith("/")
        or "/" in authority
        or authority.endswith(":")
    ):
        raise ValueError("invalid ICE URL")
    return value


def _split_configured_urls(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    urls = [url.strip() for url in raw.split(",")]
    if any(not url for url in urls):
        raise ValueError(f"{name} contains an empty URL")
    return urls


def _legacy_stun_urls() -> list[str]:
    raw = os.environ.get("SMALLWEBRTC_ICE_SERVERS", "").strip()
    if not raw:
        return []
    entries = json.loads(raw)
    if not isinstance(entries, list) or not entries:
        raise ValueError("SMALLWEBRTC_ICE_SERVERS must be a nonempty JSON array")
    urls: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"urls"}
            or "turn:" in str(entry.get("urls", "")).casefold()
            or "turns:" in str(entry.get("urls", "")).casefold()
        ):
            raise ValueError(
                "SMALLWEBRTC_ICE_SERVERS accepts URL-only STUN entries; "
                "configure TURN REST credentials instead"
            )
        values = entry["urls"]
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or not values:
            raise ValueError("SMALLWEBRTC_ICE_SERVERS contains invalid URLs")
        urls.extend(_validate_ice_url(url, frozenset({"stun", "stuns"})) for url in values)
    return urls


def _ice_server_configs() -> list[dict[str, Any]]:
    try:
        stun_urls = _split_configured_urls(
            "SMALLWEBRTC_STUN_URLS",
            ("stun:stun.l.google.com:19302",),
        )
        stun_urls.extend(_legacy_stun_urls())
        stun_urls = list(dict.fromkeys(
            _validate_ice_url(url, frozenset({"stun", "stuns"}))
            for url in stun_urls
        ))

        turn_raw = os.environ.get("SMALLWEBRTC_TURN_URLS", "").strip()
        turn_secret = os.environ.get("SMALLWEBRTC_TURN_SHARED_SECRET", "")
        turn_urls: list[str] = []
        if turn_raw:
            turn_urls = [url.strip() for url in turn_raw.split(",")]
            if any(not url for url in turn_urls):
                raise ValueError("SMALLWEBRTC_TURN_URLS contains an empty URL")
            turn_urls = [
                _validate_ice_url(url, frozenset({"turn", "turns"}))
                for url in turn_urls
            ]
            if len(turn_secret.encode("utf-8")) < 32:
                raise ValueError("SMALLWEBRTC_TURN_SHARED_SECRET must contain at least 32 bytes")
        elif turn_secret:
            raise ValueError("SMALLWEBRTC_TURN_SHARED_SECRET requires SMALLWEBRTC_TURN_URLS")

        result: list[dict[str, Any]] = [{"urls": stun_urls}]
        if turn_urls:
            ttl_raw = os.environ.get("SMALLWEBRTC_TURN_CREDENTIAL_TTL_SECONDS", "600")
            try:
                ttl = int(ttl_raw)
            except ValueError:
                raise ValueError("SMALLWEBRTC_TURN_CREDENTIAL_TTL_SECONDS must be an integer") from None
            if not 60 <= ttl <= 3600:
                raise ValueError("TURN REST credential lifetime must be between 60 and 3600 seconds")
            username = f"{int(time.time()) + ttl}:{secrets.token_urlsafe(18)}"
            credential = base64.b64encode(
                hmac.new(
                    turn_secret.encode("utf-8"),
                    username.encode("utf-8"),
                    hashlib.sha1,
                ).digest()
            ).decode("ascii")
            result.append({"urls": turn_urls, "username": username, "credential": credential})
        return result
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=503,
            detail=(
                "WebRTC ICE servers are unavailable. Check SMALLWEBRTC_STUN_URLS, "
                "SMALLWEBRTC_TURN_URLS, SMALLWEBRTC_TURN_SHARED_SECRET, and TURN credential TTL."
            ),
        ) from None


def _configured_ice_servers() -> list[RTCIceServer]:
    return [RTCIceServer(**entry) for entry in _ice_server_configs()]


@router.get("/ice-servers")
async def ice_servers(
    request: Request,
    response: Response,
) -> dict[str, list[dict[str, Any]]]:
    client_ip = request.client.host if request.client is not None else None
    _rate_limiter.check_ip(client_ip)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"iceServers": _ice_server_configs()}


def _new_handler() -> SmallWebRTCRequestHandler:
    return SmallWebRTCRequestHandler(
        ice_servers=_configured_ice_servers(),
        connection_mode=ConnectionMode.MULTIPLE,
    )


async def _read_json(request: Request, maximum_bytes: int) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum_bytes:
                raise HTTPException(status_code=413, detail="Signaling request body is too large.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content length.") from None
    if "application/json" not in request.headers.get("content-type", "").casefold():
        raise HTTPException(status_code=415, detail="Signaling requests must use JSON.")
    parts: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum_bytes:
            raise HTTPException(status_code=413, detail="Signaling request body is too large.")
        parts.append(chunk)
    try:
        payload = json.loads(b"".join(parts))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON signaling request.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Signaling request must be an object.")
    return payload


def _validate_model(model: type[BaseModel], payload: dict[str, Any]) -> BaseModel:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        messages = [entry["msg"] for entry in error.errors(include_url=False)]
        raise HTTPException(status_code=422, detail=messages) from None


def _snapshot_policy(snapshot: Mapping[str, Any]) -> tuple[int, int, int]:
    policy = snapshot.get("policy")
    if not isinstance(policy, Mapping):
        raise HTTPException(status_code=503, detail="The frozen call policy is invalid.")
    values: list[int] = []
    for key in (
        "max_call_duration_seconds",
        "max_concurrent_calls_per_agent",
        "max_concurrent_calls_overall",
    ):
        value = policy.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise HTTPException(status_code=503, detail="The frozen call policy is invalid.")
        values.append(value)
    return values[0], values[1], values[2]


async def _reserve_call(
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    per_agent_limit: int,
    overall_limit: int,
) -> None:
    async with _registry_lock:
        if session_id in _reservations or session_id in _live_calls_by_session:
            raise HTTPException(status_code=409, detail="This call already has a signaling connection.")
        agent_count = sum(1 for entry in _reservations.values() if entry.agent_id == agent_id)
        if agent_count >= per_agent_limit or len(_reservations) >= overall_limit:
            raise HTTPException(status_code=429, detail="Concurrent call limit reached.")
        _reservations[session_id] = _Reservation(session_id, agent_id)


async def release_call_runtime(runtime: CallRuntime) -> None:
    async with _registry_lock:
        _reservations.pop(runtime.session_id, None)
        live = _live_calls_by_session.pop(runtime.session_id, None)
        if live is not None:
            _live_calls_by_pc.pop(live.pc_id, None)
    if live is not None:
        try:
            await live.handler.close()
        except Exception:
            return

def _check_initial_agent_status(context: Mapping[str, Any], call_type: str) -> None:
    status = context.get("agent_status")
    if call_type == "public" and status != "published":
        raise HTTPException(status_code=404, detail="Agent not found.")
    if call_type == "test" and status == "suspended":
        raise HTTPException(status_code=409, detail="Suspended agents cannot start calls.")


async def _load_context(session_id: uuid.UUID) -> Mapping[str, Any] | None:
    from .sessions import load_signaling_context

    return await load_signaling_context(session_id)


async def _set_session_status(session_id: uuid.UUID, status: str) -> bool:
    from .sessions import set_session_status

    return await set_session_status(session_id, status)


async def _fail_created_session(session_id: uuid.UUID) -> None:
    try:
        await _set_session_status(session_id, "failed")
    except Exception:
        return


async def _await_runtime_cleanup(runtime: CallRuntime) -> None:
    runtime.request_stop("failed")
    if runtime.cleanup_task is not None:
        try:
            await asyncio.shield(runtime.cleanup_task)
        except Exception:
            pass
    await release_call_runtime(runtime)

async def _close_handler(handler: SmallWebRTCRequestHandler | None) -> None:
    if handler is not None:
        try:
            await handler.close()
        except Exception:
            return


async def _initial_offer(payload: _OfferBody, *, tts_cache: Any = None) -> dict[str, Any]:
    assert payload.signaling_token is not None
    grant = await _consume_signaling_token(payload.signaling_token)
    handler: SmallWebRTCRequestHandler | None = None
    runtime: CallRuntime | None = None
    reserved = False
    try:
        _rate_limiter.check_agent(grant.agent_id)
        context = await _load_context(grant.session_id)
        if context is None:
            raise HTTPException(status_code=410, detail="Call session is unavailable or expired.")
        if context.get("session_status") != "created":
            raise HTTPException(status_code=409, detail="Initial signaling requires a newly created call.")
        if context.get("call_type") != grant.call_type:
            raise HTTPException(status_code=404, detail="Call session not found.")
        snapshot = context.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise HTTPException(status_code=503, detail="The frozen call snapshot is invalid.")
        agent_snapshot = snapshot.get("agent")
        if not isinstance(agent_snapshot, Mapping) or str(agent_snapshot.get("id")) != str(grant.agent_id):
            raise HTTPException(status_code=404, detail="Call session not found.")
        _check_initial_agent_status(context, grant.call_type)
        _duration, per_agent_limit, overall_limit = _snapshot_policy(snapshot)
        await _reserve_call(grant.session_id, grant.agent_id, per_agent_limit, overall_limit)
        reserved = True
        try:
            runtime = create_call_runtime(
                grant.session_id, grant.call_type, snapshot, tts_cache=tts_cache
            )
        except VoiceUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except Exception:
            raise HTTPException(status_code=503, detail="Voice providers are unavailable for this call.") from None
        if runtime.definition.agent_id != grant.agent_id:
            raise HTTPException(status_code=404, detail="Call session not found.")

        callback_error: Exception | None = None

        async def start_call(connection: Any) -> None:
            nonlocal callback_error
            try:
                activated = await _set_session_status(grant.session_id, "active")
                if not activated:
                    raise HTTPException(status_code=409, detail="Call session is no longer active.")
                await runtime.attach(connection)
            except Exception as error:
                callback_error = error
                if runtime.connection is None:
                    runtime.connection = connection
                runtime.request_stop("failed")
                raise

        handler = _new_handler()
        try:
            answer = await asyncio.wait_for(
                handler.handle_web_request(
                    SmallWebRTCRequest(sdp=payload.sdp, type=payload.type),
                    start_call,
                ),
                timeout=_CONNECTION_START_TIMEOUT_SECONDS,
            )
        except Exception:
            await _await_runtime_cleanup(runtime)
            await _close_handler(handler)
            raise HTTPException(status_code=502, detail="WebRTC offer setup failed.") from None
        if callback_error is not None or answer is None or runtime.closing or runtime.connection is None:
            await _await_runtime_cleanup(runtime)
            await _close_handler(handler)
            raise HTTPException(status_code=502, detail="Voice providers could not start this call.")
        pc_id = answer.get("pc_id")
        if not isinstance(pc_id, str) or not pc_id:
            await _await_runtime_cleanup(runtime)
            await _close_handler(handler)
            raise HTTPException(status_code=502, detail="WebRTC did not create a peer connection.")
        connection_token = secrets.token_urlsafe(_TOKEN_SIZE_BYTES)
        live_call = _LiveCall(
            runtime=runtime,
            pc_id=pc_id,
            renegotiation_token_hash=hashlib.sha256(connection_token.encode("utf-8")).digest(),
            handler=handler,
        )
        async with _registry_lock:
            if pc_id in _live_calls_by_pc or grant.session_id in _live_calls_by_session:
                duplicate = True
            else:
                duplicate = False
                _live_calls_by_pc[pc_id] = live_call
                _live_calls_by_session[grant.session_id] = live_call
        if duplicate:
            await _await_runtime_cleanup(runtime)
            await _close_handler(handler)
            raise HTTPException(status_code=409, detail="WebRTC connection is already registered.")
        return {**answer, "renegotiation_token": connection_token}
    except asyncio.CancelledError:
        if runtime is not None:
            runtime.request_stop("cancelled")
            if runtime.cleanup_task is not None:
                await asyncio.shield(runtime.cleanup_task)
        else:
            await _fail_created_session(grant.session_id)
        await _close_handler(handler)
        if reserved:
            async with _registry_lock:
                _reservations.pop(grant.session_id, None)
        raise
    except HTTPException:
        if runtime is not None:
            await _await_runtime_cleanup(runtime)
        else:
            await _fail_created_session(grant.session_id)
        await _close_handler(handler)
        if reserved:
            async with _registry_lock:
                _reservations.pop(grant.session_id, None)
        raise
    except Exception:
        if runtime is not None:
            await _await_runtime_cleanup(runtime)
        else:
            await _fail_created_session(grant.session_id)
        await _close_handler(handler)
        if reserved:
            async with _registry_lock:
                _reservations.pop(grant.session_id, None)
        raise HTTPException(status_code=502, detail="WebRTC signaling failed.") from None


async def _authorize_live_call(
    pc_id: str,
    token: str,
) -> _LiveCall:
    async with _registry_lock:
        live = _live_calls_by_pc.get(pc_id)
    if live is None or live.runtime.closed or live.runtime.closing:
        raise HTTPException(status_code=404, detail="Live peer connection not found.")
    supplied = hashlib.sha256(token.encode("utf-8")).digest()
    if not secrets.compare_digest(live.renegotiation_token_hash, supplied):
        raise HTTPException(status_code=401, detail="Renegotiation credential is invalid.")
    context = await _load_context(live.runtime.session_id)
    snapshot = context.get("snapshot") if isinstance(context, Mapping) else None
    agent_snapshot = snapshot.get("agent") if isinstance(snapshot, Mapping) else None
    if (
        context is None
        or context.get("session_status") != "active"
        or context.get("call_type") != live.runtime.definition.call_type
        or not isinstance(agent_snapshot, Mapping)
        or str(agent_snapshot.get("id")) != str(live.runtime.agent_id)
    ):
        raise HTTPException(status_code=404, detail="Live call session not found.")

    async with _registry_lock:
        if (
            _live_calls_by_pc.get(pc_id) is not live
            or live.runtime.closed
            or live.runtime.closing
        ):
            raise HTTPException(status_code=404, detail="Live peer connection not found.")
    return live


@router.post("/offer")
async def offer(request: Request) -> dict[str, Any]:
    client_ip = request.client.host if request.client is not None else None
    _rate_limiter.check_ip(client_ip)
    payload = _validate_model(_OfferBody, await _read_json(request, _MAX_OFFER_BYTES))
    assert isinstance(payload, _OfferBody)
    if payload.pc_id is None:
        # The optional TTS audio cache is owned by the app; the call runtime
        # receives it so it can replay cached speech instead of synthesizing.
        tts_cache = getattr(request.app.state, "tts_cache", None)
        return await _initial_offer(payload, tts_cache=tts_cache)

    assert payload.renegotiation_token is not None
    live = await _authorize_live_call(payload.pc_id, payload.renegotiation_token)
    _rate_limiter.check_agent(live.runtime.agent_id)
    async with live.renegotiation_lock:
        if live.runtime.closed or live.runtime.closing:
            raise HTTPException(status_code=404, detail="Live peer connection not found.")
        live.runtime.renegotiating = payload.restart_pc
        new_connection_rejected = False

        async def reject_unbound_connection(connection: Any) -> None:
            nonlocal new_connection_rejected
            new_connection_rejected = True
            try:
                await connection.disconnect()
            finally:
                raise HTTPException(status_code=404, detail="Live peer connection not found.")

        try:
            answer = await asyncio.wait_for(
                live.handler.handle_web_request(
                    SmallWebRTCRequest(
                        sdp=payload.sdp,
                        type=payload.type,
                        pc_id=payload.pc_id,
                        restart_pc=payload.restart_pc,
                    ),
                    reject_unbound_connection,
                ),
                timeout=_CONNECTION_START_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            live.runtime.request_stop("cancelled")
            raise
        except Exception:
            live.runtime.request_stop("failed")
            raise HTTPException(status_code=502, detail="WebRTC renegotiation failed.") from None
        finally:
            live.runtime.renegotiating = False
        if (
            new_connection_rejected
            or answer is None
            or answer.get("pc_id") != payload.pc_id
            or live.runtime.closing
        ):
            live.runtime.request_stop("failed")
            raise HTTPException(status_code=502, detail="WebRTC renegotiation failed.")
        return {**answer, "renegotiation_token": payload.renegotiation_token}


@router.patch("/candidates")
async def candidates(request: Request) -> dict[str, bool]:
    client_ip = request.client.host if request.client is not None else None
    _rate_limiter.check_ip(client_ip)
    payload = _validate_model(_CandidateBody, await _read_json(request, _MAX_CANDIDATE_BYTES))
    assert isinstance(payload, _CandidateBody)
    live = await _authorize_live_call(payload.pc_id, payload.renegotiation_token)
    _rate_limiter.check_agent(live.runtime.agent_id)
    try:
        async with live.renegotiation_lock:
            if live.runtime.closed or live.runtime.closing:
                raise HTTPException(status_code=404, detail="Live peer connection not found.")
            await live.handler.handle_patch_request(
                SmallWebRTCPatchRequest(
                    pc_id=payload.pc_id,
                    candidates=[
                        IceCandidate(
                            candidate=candidate.candidate,
                            sdp_mid=candidate.sdp_mid,
                            sdp_mline_index=candidate.sdp_mline_index,
                        )
                        for candidate in payload.candidates
                    ],
                )
            )
    except asyncio.CancelledError:
        live.runtime.request_stop("cancelled")
        raise
    except HTTPException:
        raise
    except Exception:
        live.runtime.request_stop("failed")
        raise HTTPException(status_code=502, detail="WebRTC candidate exchange failed.") from None
    return {"accepted": True}
