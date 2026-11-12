from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
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
from sqlalchemy import func, or_, select, text, update

from .coordination import CallClaim, Coordination, LiveRoute
from .db import Session as DbSession
from .sessions import (
    CallSession,
    acquire_coordination_lock,
    renew_voice_claim,
    set_session_status,
)
from .signaling_rpc import (
    MAX_CANDIDATE_BYTES,
    MAX_CANDIDATES,
    MAX_ENVELOPE_OVERHEAD_BYTES,
    MAX_OFFER_BYTES,
    MAX_REPLY_BYTES,
    MAX_SDP_CHARS,
    RpcDeadline,
    RpcReply,
    SignalingRpc,
)
from .voice import CallRuntime, VoiceUnavailableError, create_call_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signaling", tags=["signaling"])
_TOKEN_SIZE_BYTES = 32
_CONNECTION_START_TIMEOUT_SECONDS = 35
_SHUTDOWN_TIMEOUT_SECONDS: float = 15.0
_SHUTDOWN_STEP_TIMEOUT_SECONDS: float = 5.0


@dataclass(slots=True)
class _LiveCall:
    runtime: CallRuntime
    pc_id: str
    renegotiation_token_hash: bytes
    handler: SmallWebRTCRequestHandler
    renegotiation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _OfferBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sdp: str = Field(min_length=1, max_length=MAX_SDP_CHARS)
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
    candidates: list[_Candidate] = Field(max_length=MAX_CANDIDATES)


_registry_lock = asyncio.Lock()
_live_calls_by_pc: dict[str, _LiveCall] = {}
_live_calls_by_session: dict[uuid.UUID, _LiveCall] = {}
_setup_runtimes_by_session: dict[uuid.UUID, CallRuntime] = {}


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
    coordination = getattr(request.app.state, "coordination", None)
    client_ip = request.client.host if request.client is not None else None
    if coordination is not None:
        await coordination.check_ip(client_ip)
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


async def release_call_runtime(runtime: CallRuntime) -> None:
    async with _registry_lock:
        _setup_runtimes_by_session.pop(runtime.session_id, None)
        live = _live_calls_by_session.pop(runtime.session_id, None)
        if live is not None:
            _live_calls_by_pc.pop(live.pc_id, None)
    if live is not None:
        try:
            await asyncio.wait_for(live.handler.close(), timeout=_SHUTDOWN_STEP_TIMEOUT_SECONDS)
        except Exception:
            return


async def stop_all_local_runtimes(timeout: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
    async with _registry_lock:
        active_calls = list(_live_calls_by_session.values())
        setup_runtimes = list(_setup_runtimes_by_session.values())
        _live_calls_by_session.clear()
        _live_calls_by_pc.clear()
        _setup_runtimes_by_session.clear()

    # 1. Stop both setup and active media/runtimes
    for r in setup_runtimes:
        r.request_stop("cancelled")
    for call in active_calls:
        call.runtime.request_stop("cancelled")

    # 2. Wait for cleanup while watchdogs remain valid
    cleanup_tasks = []
    for r in setup_runtimes:
        cleanup_tasks.append(_await_runtime_cleanup(r, timeout=timeout))
    for call in active_calls:
        cleanup_tasks.append(_await_runtime_cleanup(call.runtime, timeout=timeout))
        cleanup_tasks.append(_close_handler(call.handler, timeout=min(_SHUTDOWN_STEP_TIMEOUT_SECONDS, timeout)))

    if cleanup_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*cleanup_tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("stop_all_local_runtimes timed out after %s seconds", timeout)
            seen_ids: set[uuid.UUID] = set()
            all_runtimes: list[CallRuntime] = []
            for rt in list(setup_runtimes) + [c.runtime for c in active_calls]:
                if rt.session_id not in seen_ids:
                    seen_ids.add(rt.session_id)
                    all_runtimes.append(rt)

            for rt in all_runtimes:
                if not rt.closed:
                    rt.termination_status = "failed"
                    if rt.watchdog_task is not None and not rt.watchdog_task.done():
                        rt.watchdog_task.cancel()
                    if rt.worker_task is not None and not rt.worker_task.done():
                        rt.worker_task.cancel()
                    if rt.cleanup_task is not None and not rt.cleanup_task.done():
                        rt.cleanup_task.cancel()

                    try:
                        from .sessions import set_session_status

                        if rt.coordination is not None and rt.claim is not None:
                            await asyncio.wait_for(
                                set_session_status(rt.session_id, "failed", claim=rt.claim),
                                timeout=2.0,
                            )
                        else:
                            await asyncio.wait_for(
                                set_session_status(rt.session_id, "failed"),
                                timeout=2.0,
                            )
                    except Exception:
                        pass

                    if rt.coordination is not None and rt.claim is not None:
                        try:
                            await asyncio.wait_for(
                                rt.coordination.release(rt.claim, pc_id=rt.pc_id),
                                timeout=2.0,
                            )
                        except Exception:
                            pass

                    await release_call_runtime(rt)
                    rt.closed = True


def _check_initial_agent_status(context: Mapping[str, Any], call_type: str) -> None:
    status = context.get("agent_status")
    if call_type == "public" and status != "published":
        raise HTTPException(status_code=404, detail="Agent not found.")
    if call_type == "test" and status == "suspended":
        raise HTTPException(status_code=409, detail="Suspended agents cannot start calls.")


async def _load_context(session_id: uuid.UUID) -> Mapping[str, Any] | None:
    from .sessions import load_signaling_context

    return await load_signaling_context(session_id)


async def _set_session_status(session_id: uuid.UUID, status: str, *, claim: CallClaim | None = None) -> bool:
    from .sessions import set_session_status

    return await set_session_status(session_id, status, claim=claim)


async def _fail_created_session(session_id: uuid.UUID, *, claim: CallClaim | None = None) -> None:
    try:
        if claim is not None:
            await _set_session_status(session_id, "failed", claim=claim)
        else:
            async with DbSession() as db:
                await acquire_coordination_lock(db)
                db_now = await db.scalar(text("SELECT clock_timestamp()"))
                await db.execute(
                    update(CallSession)
                    .where(
                        CallSession.id == session_id,
                        CallSession.status == "created",
                    )
                    .values(
                        status="failed",
                        ended_at=db_now,
                        updated_at=db_now,
                    )
                )
                await db.commit()
    except Exception as exc:
        logger.debug("Failed to fail created session %s: %s", session_id, exc)


async def _await_runtime_cleanup(runtime: CallRuntime, timeout: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
    if not runtime.closing:
        runtime.request_stop("failed")
    if runtime.cleanup_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(runtime.cleanup_task), timeout=timeout)
        except Exception:
            runtime.termination_status = "failed"
            runtime.closed = True
    await release_call_runtime(runtime)


async def _close_handler(handler: SmallWebRTCRequestHandler | None, timeout: float = _SHUTDOWN_STEP_TIMEOUT_SECONDS) -> None:
    if handler is not None:
        try:
            await asyncio.wait_for(handler.close(), timeout=timeout)
        except Exception:
            return


async def _setup_watchdog(runtime: CallRuntime, coordination: Coordination) -> None:
    while not runtime.closed:
        try:
            await asyncio.sleep(5.0)
            if runtime.closed:
                break
            if time.monotonic() > runtime.lease_deadline:
                logger.warning("Watchdog loop stalled past deadline for session %s", runtime.session_id)
                runtime.request_stop("failed")
                if runtime.worker_task is not None and not runtime.worker_task.done():
                    runtime.worker_task.cancel()
                break
            if runtime.claim is None:
                break
            # Order: runtime lease lock -> DB advisory lock -> row locks
            async with runtime.lease_lock:
                if runtime.closed:
                    break
                if time.monotonic() > runtime.lease_deadline:
                    logger.warning("Watchdog detected expired deadline under lease lock for session %s", runtime.session_id)
                    runtime.request_stop("failed")
                    if runtime.worker_task is not None and not runtime.worker_task.done():
                        runtime.worker_task.cancel()
                    break
                obs = await renew_voice_claim(runtime.claim, coordination, runtime.pc_id)
                runtime.claim = obs.claim
                runtime.lease_deadline = obs.monotonic_expires_at
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Watchdog renewal failed for session %s: %s", runtime.session_id, exc)
            runtime.request_stop("failed")
            if runtime.worker_task is not None and not runtime.worker_task.done():
                runtime.worker_task.cancel()
            break


async def _initial_offer(
    payload: _OfferBody,
    coordination: Coordination,
) -> dict[str, Any]:
    assert payload.signaling_token is not None
    grant = await coordination.consume_token(payload.signaling_token)
    current_claim: CallClaim = grant
    handler: SmallWebRTCRequestHandler | None = None
    runtime: CallRuntime | None = None
    try:
        await coordination.check_agent(grant.agent_id)
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

        # 1. Acquire advisory lock, verify original DB deadline, transition Redis to starting, and CAS DB row
        async with DbSession() as db:
            await acquire_coordination_lock(db)
            db_call = await db.scalar(
                select(CallSession).where(CallSession.id == grant.session_id).with_for_update()
            )
            if db_call is None or db_call.status != "created":
                raise HTTPException(status_code=409, detail="Initial signaling requires a newly created call.")
            if db_call.claim_generation != grant.generation:
                raise HTTPException(status_code=409, detail="Claim generation mismatch.")
            if db_call.worker_id is not None:
                raise HTTPException(status_code=409, detail="Call already claimed by another worker.")

            db_now = await db.scalar(select(func.clock_timestamp()))
            if db_call.signaling_expires_at is not None and db_call.signaling_expires_at <= db_now:
                raise HTTPException(status_code=410, detail="Call signaling token has expired.")

            # Transition Redis lease to starting with 45s setup lease and assigned worker owner
            obs = await coordination.begin_setup(grant)
            current_claim = obs.claim

            t_before_clock = time.monotonic()
            db_now = await db.scalar(select(func.clock_timestamp()))
            rem = max(0.0, obs.remaining_seconds - (time.monotonic() - t_before_clock))

            # CAS Database row (worker_id IS NULL required)
            res = await db.execute(
                update(CallSession)
                .where(
                    CallSession.id == grant.session_id,
                    CallSession.status == "created",
                    CallSession.claim_generation == grant.generation,
                    CallSession.worker_id.is_(None),
                )
                .values(
                    worker_id=coordination.worker_id,
                    claim_generation=obs.claim.generation,
                    lease_expires_at=db_now + timedelta(seconds=rem),
                    updated_at=func.clock_timestamp(),
                )
            )
            if res.rowcount == 0:
                await db.commit()
                await coordination.release(obs.claim, None)
                raise HTTPException(status_code=409, detail="Call session could not be claimed for setup.")
            await db.commit()

            conservative_setup_mono = min(obs.monotonic_expires_at, t_before_clock + rem)
            if conservative_setup_mono <= time.monotonic():
                await db.commit()
                await _fail_created_session(grant.session_id, claim=current_claim)
                try:
                    await coordination.release(current_claim, None)
                except Exception:
                    pass
                raise HTTPException(status_code=504, detail="Setup lease expired during admission commit.")

        # 2. Create runtime with claim and coordination
        try:
            runtime = create_call_runtime(
                grant.session_id,
                grant.call_type,
                snapshot,
                claim=obs.claim,
                coordination=coordination,
            )
        except VoiceUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except Exception:
            raise HTTPException(status_code=503, detail="Voice providers are unavailable for this call.") from None
        if runtime.definition.agent_id != grant.agent_id:
            raise HTTPException(status_code=404, detail="Call session not found.")

        # Seed initial lease deadline with actual conservative observation
        runtime.lease_deadline = conservative_setup_mono

        # Register runtime in setup runtimes immediately
        async with _registry_lock:
            _setup_runtimes_by_session[grant.session_id] = runtime

        # 3. Start setup watchdog before handler setup
        runtime.watchdog_task = asyncio.create_task(_setup_watchdog(runtime, coordination))

        callback_error: Exception | None = None

        async def start_call(connection: Any) -> None:
            nonlocal callback_error
            try:
                async with runtime.lease_lock:
                    if runtime.closed or runtime.closing:
                        raise HTTPException(status_code=409, detail="Call session is closing.")
                    if time.monotonic() > runtime.lease_deadline:
                        raise HTTPException(status_code=504, detail="Setup lease expired before media attach.")
                    if runtime.claim is None:
                        raise HTTPException(status_code=409, detail="Call claim is missing.")
                    live_obs = await coordination.inspect(grant.session_id)
                    if (
                        live_obs is None
                        or live_obs.claim.generation != runtime.claim.generation
                        or live_obs.claim.worker_id != runtime.claim.worker_id
                        or live_obs.claim.phase != "starting"
                        or live_obs.monotonic_expires_at <= time.monotonic()
                    ):
                        raise HTTPException(status_code=409, detail="Live setup claim lost or replaced.")
                    activated = await _set_session_status(grant.session_id, "active", claim=runtime.claim)
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
        except Exception as exc:
            logger.warning("WebRTC offer setup failed: %s", exc)
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

        # 4. Guarded activation under advisory lock in DB and Redis, serialized by runtime.lease_lock
        async with runtime.lease_lock:
            async with DbSession() as db:
                await acquire_coordination_lock(db)
                active_obs = await coordination.activate(runtime.claim, pc_id)
                current_claim = active_obs.claim
                t_before_clock = time.monotonic()
                db_now = await db.scalar(select(func.clock_timestamp()))
                rem = max(0.0, active_obs.remaining_seconds - (time.monotonic() - t_before_clock))
                res = await db.execute(
                    update(CallSession)
                    .where(
                        CallSession.id == grant.session_id,
                        CallSession.claim_generation == runtime.claim.generation,
                        CallSession.worker_id == coordination.worker_id,
                        CallSession.status == "active",
                    )
                    .values(
                        lease_expires_at=db_now + timedelta(seconds=rem),
                        updated_at=func.clock_timestamp(),
                    )
                )
                if res.rowcount == 0:
                    await db.commit()
                    await coordination.release(active_obs.claim, pc_id)
                    raise HTTPException(status_code=409, detail="Call session could not be activated.")
                await db.commit()

            conservative_active_mono = min(active_obs.monotonic_expires_at, t_before_clock + rem)
            if conservative_active_mono <= time.monotonic():
                await coordination.release(active_obs.claim, pc_id)
                raise HTTPException(status_code=504, detail="Active lease expired during activation commit.")

            runtime.claim = active_obs.claim
            runtime.pc_id = pc_id
            runtime.lease_deadline = conservative_active_mono

        connection_token = secrets.token_urlsafe(_TOKEN_SIZE_BYTES)
        live_call = _LiveCall(
            runtime=runtime,
            pc_id=pc_id,
            renegotiation_token_hash=hashlib.sha256(connection_token.encode("utf-8")).digest(),
            handler=handler,
        )
        async with _registry_lock:
            _setup_runtimes_by_session.pop(grant.session_id, None)
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
                try:
                    await asyncio.wait_for(asyncio.shield(runtime.cleanup_task), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                except Exception:
                    pass
            await release_call_runtime(runtime)
        else:
            await _fail_created_session(grant.session_id, claim=current_claim)
            try:
                await coordination.release(current_claim, None)
            except Exception:
                pass
        await _close_handler(handler)
        raise
    except HTTPException:
        if runtime is not None:
            await _await_runtime_cleanup(runtime)
        else:
            await _fail_created_session(grant.session_id, claim=current_claim)
            try:
                await coordination.release(current_claim, None)
            except Exception:
                pass
        await _close_handler(handler)
        raise
    except Exception as exc:
        logger.warning("Initial offer failed: %s", exc)
        if runtime is not None:
            await _await_runtime_cleanup(runtime)
        else:
            await _fail_created_session(grant.session_id, claim=current_claim)
            try:
                await coordination.release(current_claim, None)
            except Exception:
                pass
        await _close_handler(handler)
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


async def _dispatch_local_signal(
    action: str,
    payload: dict[str, Any],
    claim: CallClaim,
    remaining_seconds: float,
) -> RpcReply:
    deadline_end = time.monotonic() + remaining_seconds
    pc_id = payload.get("pc_id")
    token = payload.get("renegotiation_token")
    if not isinstance(pc_id, str) or not isinstance(token, str):
        return RpcReply(status_code=400, body={"detail": "Missing pc_id or renegotiation_token."}, headers={})

    async with _registry_lock:
        live = _live_calls_by_pc.get(pc_id)
    if live is None or live.runtime.closed or live.runtime.closing:
        return RpcReply(status_code=404, body={"detail": "Live peer connection not found."}, headers={})

    lock_budget = deadline_end - time.monotonic()
    if lock_budget <= 0:
        return RpcReply(status_code=504, body={"detail": "Signaling operation timed out waiting for call lock."}, headers={})
    try:
        await asyncio.wait_for(live.renegotiation_lock.acquire(), timeout=lock_budget)
    except asyncio.TimeoutError:
        return RpcReply(status_code=504, body={"detail": "Signaling operation timed out waiting for call lock."}, headers={})
    except asyncio.CancelledError:
        return RpcReply(status_code=499, body={"detail": "Signaling operation cancelled."}, headers={})

    try:
        if live.runtime.closed or live.runtime.closing:
            return RpcReply(status_code=404, body={"detail": "Live peer connection not found."}, headers={})

        # 1. Credential check
        supplied = hashlib.sha256(token.encode("utf-8")).digest()
        if not secrets.compare_digest(live.renegotiation_token_hash, supplied):
            return RpcReply(status_code=401, body={"detail": "Invalid renegotiation token."}, headers={})

        # 2. Complete durable ownership & claim re-check
        rt_claim = live.runtime.claim
        if (
            rt_claim is None
            or rt_claim.session_id != claim.session_id
            or rt_claim.agent_id != claim.agent_id
            or rt_claim.call_type != claim.call_type
            or rt_claim.worker_id != claim.worker_id
            or rt_claim.generation != claim.generation
            or rt_claim.phase != "active"
        ):
            return RpcReply(status_code=409, body={"detail": "Call claim mismatch."}, headers={})

        # 3. Unexpired lease deadline check
        if time.monotonic() > live.runtime.lease_deadline:
            return RpcReply(status_code=410, body={"detail": "Call session lease has expired."}, headers={})

        # 4. Durable context & agent status re-check
        context = await _load_context(live.runtime.session_id)
        if context is None:
            return RpcReply(status_code=410, body={"detail": "Live call session is no longer active or lease has expired."}, headers={})
        if context.get("worker_id") != claim.worker_id:
            return RpcReply(status_code=409, body={"detail": "Call claim worker mismatch."}, headers={})
        if context.get("claim_generation") != claim.generation:
            return RpcReply(status_code=409, body={"detail": "Call claim generation mismatch."}, headers={})

        snapshot = context.get("snapshot") if isinstance(context, Mapping) else None
        agent_snapshot = snapshot.get("agent") if isinstance(snapshot, Mapping) else None
        if (
            context.get("session_status") != "active"
            or context.get("call_type") != live.runtime.definition.call_type
            or not isinstance(agent_snapshot, Mapping)
            or str(agent_snapshot.get("id")) != str(live.runtime.agent_id)
        ):
            return RpcReply(status_code=404, body={"detail": "Live call session not found."}, headers={})

        # 5. Redis durable lease re-check
        if live.runtime.coordination is not None:
            try:
                redis_obs = await live.runtime.coordination.inspect(claim.session_id)
            except Exception:
                return RpcReply(status_code=503, body={"detail": "Coordination service unavailable."}, headers={})
            if (
                redis_obs is None
                or redis_obs.claim.generation != claim.generation
                or redis_obs.claim.worker_id != claim.worker_id
            ):
                return RpcReply(status_code=410, body={"detail": "Call session lease is no longer active in Redis."}, headers={})

        try:
            _check_initial_agent_status(context, live.runtime.definition.call_type)
        except HTTPException as exc:
            return RpcReply(status_code=exc.status_code, body={"detail": exc.detail}, headers=dict(exc.headers or {}))

        # 6. Owner-side agent rate limit check (charged ONCE on owner after credential/auth verification)
        if live.runtime.coordination is not None:
            try:
                await live.runtime.coordination.check_agent(claim.agent_id)
            except HTTPException as exc:
                return RpcReply(status_code=exc.status_code, body={"detail": exc.detail}, headers=dict(exc.headers or {}))

        # Recheck closing, local lease deadline, and claim immediately before invoking handler
        if live.runtime.closed or live.runtime.closing:
            return RpcReply(status_code=410, body={"detail": "Call session is no longer active."}, headers={})
        if time.monotonic() > live.runtime.lease_deadline:
            return RpcReply(status_code=504, body={"detail": "Signaling lease expired."}, headers={})
        if live.runtime.claim is None or live.runtime.claim != claim:
            return RpcReply(status_code=409, body={"detail": "Call claim mismatch."}, headers={})

        remaining_for_handler = deadline_end - time.monotonic()
        if remaining_for_handler <= 0:
            return RpcReply(status_code=504, body={"detail": "Signaling operation timed out."}, headers={})
        timeout_seconds = min(35.0, remaining_for_handler)

        if action == "renegotiate":
            restart_pc = bool(payload.get("restart_pc", False))
            live.runtime.renegotiating = restart_pc
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
                            sdp=str(payload.get("sdp", "")),
                            type=str(payload.get("type", "offer")),
                            pc_id=pc_id,
                            restart_pc=restart_pc,
                        ),
                        reject_unbound_connection,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=504, body={"detail": "Signaling operation timed out."}, headers={})
            except asyncio.CancelledError:
                live.runtime.request_stop("cancelled")
                return RpcReply(status_code=499, body={"detail": "Signaling operation cancelled."}, headers={})
            except HTTPException as exc:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=exc.status_code, body={"detail": exc.detail}, headers=dict(exc.headers or {}))
            except Exception:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=502, body={"detail": "WebRTC renegotiation failed."}, headers={})
            finally:
                live.runtime.renegotiating = False

            if (
                new_connection_rejected
                or answer is None
                or answer.get("pc_id") != pc_id
                or live.runtime.closing
            ):
                live.runtime.request_stop("failed")
                return RpcReply(status_code=502, body={"detail": "WebRTC renegotiation failed."}, headers={})

            reply = RpcReply(
                status_code=200,
                body={**answer, "renegotiation_token": token},
                headers={},
            )
            try:
                reply.to_bytes()
            except ValueError:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=502, body={"detail": "WebRTC renegotiation failed."}, headers={})
            return reply

        elif action == "candidates":
            raw_candidates = payload.get("candidates", [])
            if not isinstance(raw_candidates, list):
                return RpcReply(status_code=422, body={"detail": "Invalid candidates format."}, headers={})
            try:
                parsed_candidates = [
                    IceCandidate(
                        candidate=c["candidate"],
                        sdp_mid=c["sdp_mid"],
                        sdp_mline_index=c["sdp_mline_index"],
                    )
                    for c in raw_candidates
                ]
                await asyncio.wait_for(
                    live.handler.handle_patch_request(
                        SmallWebRTCPatchRequest(
                            pc_id=pc_id,
                            candidates=parsed_candidates,
                        )
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=504, body={"detail": "Signaling operation timed out."}, headers={})
            except asyncio.CancelledError:
                live.runtime.request_stop("cancelled")
                return RpcReply(status_code=499, body={"detail": "Signaling operation cancelled."}, headers={})
            except HTTPException as exc:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=exc.status_code, body={"detail": exc.detail}, headers=dict(exc.headers or {}))
            except Exception:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=502, body={"detail": "WebRTC candidate exchange failed."}, headers={})

            reply = RpcReply(status_code=200, body={"accepted": True}, headers={})
            try:
                reply.to_bytes()
            except ValueError:
                live.runtime.request_stop("failed")
                return RpcReply(status_code=502, body={"detail": "WebRTC candidate exchange failed."}, headers={})
            return reply

        return RpcReply(status_code=400, body={"detail": f"Unknown action: {action}"}, headers={})
    finally:
        live.renegotiation_lock.release()


@router.post("/offer")
async def offer(request: Request) -> Any:
    coordination: Coordination | None = getattr(request.app.state, "coordination", None)
    if coordination is None:
        raise HTTPException(status_code=503, detail="Coordination service unavailable.")

    client_ip = request.client.host if request.client is not None else None
    await coordination.check_ip(client_ip)

    payload = _validate_model(_OfferBody, await _read_json(request, MAX_OFFER_BYTES))
    assert isinstance(payload, _OfferBody)
    if payload.pc_id is None:
        return await _initial_offer(payload, coordination)

    assert payload.renegotiation_token is not None
    signaling_rpc: SignalingRpc = request.app.state.signaling_rpc
    deadline = await signaling_rpc.new_deadline()

    route = await coordination.route(payload.pc_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Live peer connection not found.")

    if route.claim.worker_id == coordination.worker_id:
        reply = await signaling_rpc.dispatch_local_with_deadline(
            "renegotiate", payload.model_dump(), route.claim, deadline=deadline
        )
    else:
        reply = await signaling_rpc.request(
            route, "renegotiate", payload.model_dump(), deadline=deadline
        )

    return Response(
        content=json.dumps(reply.body),
        status_code=reply.status_code,
        media_type="application/json",
        headers=reply.headers,
    )


@router.patch("/candidates")
async def candidates(request: Request) -> Any:
    coordination: Coordination | None = getattr(request.app.state, "coordination", None)
    if coordination is None:
        raise HTTPException(status_code=503, detail="Coordination service unavailable.")

    client_ip = request.client.host if request.client is not None else None
    await coordination.check_ip(client_ip)

    payload = _validate_model(_CandidateBody, await _read_json(request, MAX_CANDIDATE_BYTES))
    assert isinstance(payload, _CandidateBody)

    signaling_rpc: SignalingRpc = request.app.state.signaling_rpc
    deadline = await signaling_rpc.new_deadline()

    route = await coordination.route(payload.pc_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Live peer connection not found.")

    if route.claim.worker_id == coordination.worker_id:
        reply = await signaling_rpc.dispatch_local_with_deadline(
            "candidates", payload.model_dump(), route.claim, deadline=deadline
        )
    else:
        reply = await signaling_rpc.request(
            route, "candidates", payload.model_dump(), deadline=deadline
        )

    return Response(
        content=json.dumps(reply.body),
        status_code=reply.status_code,
        media_type="application/json",
        headers=reply.headers,
    )
