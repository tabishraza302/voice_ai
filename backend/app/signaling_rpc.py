from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from fastapi import HTTPException

from .coordination import CallClaim, LiveRoute
from .redis import RedisResources

logger = logging.getLogger(__name__)

MAX_OFFER_BYTES = 1_048_576
MAX_CANDIDATE_BYTES = 65_536
MAX_SDP_CHARS = 131_072
MAX_CANDIDATES = 64
MAX_REPLY_BYTES = 1_048_576
MAX_ENVELOPE_OVERHEAD_BYTES = 2_048

_MAX_QUEUE_LEN = 128
_MAX_CONCURRENT_DISPATCH = 16
_MAX_REPLY_WAITS = 16
_TOTAL_RPC_DEADLINE_SECONDS = 40.0
_QUEUE_TTL_SECONDS = 50

_ACTION_BODY_LIMITS = {
    "renegotiate": MAX_OFFER_BYTES,
    "candidates": MAX_CANDIDATE_BYTES,
}
_MAX_RETRY_AFTER_CHARS = 10


def _sanitize_headers(headers: Any) -> dict[str, str]:
    """Keep only a well-formed Retry-After header from an RPC reply."""
    if not isinstance(headers, dict):
        raise ValueError("RPC reply headers must be an object.")
    retry_after = headers.get("Retry-After")
    if (
        isinstance(retry_after, str)
        and retry_after.isdecimal()
        and len(retry_after) <= _MAX_RETRY_AFTER_CHARS
    ):
        return {"Retry-After": retry_after}
    return {}


def _is_bounded_string(value: Any, maximum: int, *, minimum: int = 1) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum


def _validate_forwarded_payload(action: str, payload: dict[str, Any]) -> bool:
    """Revalidate a forwarded body against the same caps as HTTP ingress."""
    if action == "renegotiate":
        if set(payload) - {
            "sdp",
            "type",
            "signaling_token",
            "pc_id",
            "renegotiation_token",
            "restart_pc",
        }:
            return False
        return (
            _is_bounded_string(payload.get("sdp"), MAX_SDP_CHARS)
            and payload.get("type") == "offer"
            and payload.get("signaling_token") is None
            and _is_bounded_string(payload.get("pc_id"), 256)
            and _is_bounded_string(payload.get("renegotiation_token"), 256)
            and isinstance(payload.get("restart_pc", False), bool)
        )
    if action == "candidates":
        if set(payload) != {"pc_id", "renegotiation_token", "candidates"}:
            return False
        if not _is_bounded_string(payload.get("pc_id"), 256):
            return False
        if not _is_bounded_string(payload.get("renegotiation_token"), 256):
            return False
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            return False
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {
                "candidate",
                "sdp_mid",
                "sdp_mline_index",
            }:
                return False
            if not _is_bounded_string(candidate.get("candidate"), 4_096, minimum=0):
                return False
            if not _is_bounded_string(candidate.get("sdp_mid"), 128, minimum=0):
                return False
            index = candidate.get("sdp_mline_index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 128:
                return False
        return True
    return False

_ENQUEUE_LUA = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
if now_ms >= tonumber(ARGV[2]) then
    return {-2, now_ms}
end
if redis.call('EXISTS', KEYS[2]) == 0 then
    return {-3, now_ms}
end
if redis.call('LLEN', KEYS[1]) >= 128 then
    return {0, now_ms}
end
redis.call('LPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], 50)
return {1, now_ms}
"""

_PUBLISH_REPLY_LUA = """
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], 50)
return 1
"""


@dataclass(frozen=True, slots=True)
class RpcDeadline:
    """A Redis-clock deadline with a conservative local monotonic bound."""

    expires_at_ms: int
    monotonic_expires_at: float

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.monotonic_expires_at - time.monotonic())


@dataclass(frozen=True, slots=True)
class RpcReply:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]

    def to_bytes(self) -> bytes:
        if not isinstance(self.status_code, int) or not 200 <= self.status_code <= 599:
            raise ValueError("RPC reply has an invalid status code.")
        if not isinstance(self.body, dict):
            raise ValueError("RPC reply body must be an object.")
        headers = _sanitize_headers(self.headers)
        try:
            encoded = json.dumps(
                {
                    "status_code": self.status_code,
                    "body": self.body,
                    "headers": headers,
                },
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("RPC reply cannot be encoded safely.") from exc
        if len(encoded) > MAX_REPLY_BYTES:
            raise ValueError("RPC reply exceeds the maximum size.")
        return encoded


class SignalingRpc:
    def __init__(
        self,
        resources: RedisResources,
        worker_id: str,
        dispatch_local: Callable[[str, dict[str, Any], CallClaim, float], Awaitable[RpcReply]],
    ):
        self.resources = resources
        self.worker_id = worker_id
        self.dispatch_local = dispatch_local
        self._stopping = False
        self._dispatch_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DISPATCH)
        self._reply_slot_lock = asyncio.Lock()
        self._reply_slots = 0
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._active_local_dispatch: set[asyncio.Task[Any]] = set()

    async def _redis_time_ms(self) -> int:
        try:
            seconds, micros = await self.resources.commands.time()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Coordination service unavailable.") from exc
        return int(seconds) * 1_000 + int(micros) // 1_000

    async def new_deadline(self) -> RpcDeadline:
        started = time.monotonic()
        now_ms = await self._redis_time_ms()
        remaining = _TOTAL_RPC_DEADLINE_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise HTTPException(status_code=504, detail="Signaling operation timed out.")
        return RpcDeadline(
            expires_at_ms=now_ms + int(remaining * 1_000),
            monotonic_expires_at=time.monotonic() + remaining,
        )

    async def _deadline_from_remote(self, expires_at_ms: int) -> RpcDeadline | None:
        started = time.monotonic()
        now_ms = await self._redis_time_ms()
        remaining = min(
            _TOTAL_RPC_DEADLINE_SECONDS,
            (expires_at_ms - now_ms) / 1_000 - (time.monotonic() - started),
        )
        if remaining <= 0:
            return None
        return RpcDeadline(
            expires_at_ms=now_ms + int(remaining * 1_000),
            monotonic_expires_at=time.monotonic() + remaining,
        )

    async def _try_acquire_reply_slot(self) -> bool:
        async with self._reply_slot_lock:
            if self._reply_slots >= _MAX_REPLY_WAITS:
                return False
            self._reply_slots += 1
            return True

    async def _release_reply_slot(self) -> None:
        async with self._reply_slot_lock:
            self._reply_slots -= 1

    async def _try_acquire_dispatch_slot(self) -> bool:
        if self._stopping:
            return False
        if self._dispatch_semaphore.locked():
            return False
        await self._dispatch_semaphore.acquire()
        return True

    def _release_dispatch_slot(self) -> None:
        self._dispatch_semaphore.release()

    async def dispatch_local_with_deadline(
        self,
        action: str,
        payload: dict[str, Any],
        claim: CallClaim,
        *,
        deadline: RpcDeadline,
        _admitted: bool = False,
    ) -> RpcReply:
        remaining = deadline.remaining_seconds
        if remaining <= 0:
            return RpcReply(504, {"detail": "Signaling operation timed out."}, {})
        if not _admitted:
            if not await self._try_acquire_dispatch_slot():
                return RpcReply(503, {"detail": "Signaling dispatcher is overloaded."}, {})
            admitted_here = True
        else:
            admitted_here = False
        try:
            task = asyncio.create_task(
                self.dispatch_local(action, payload, claim, remaining)
            )
            self._active_local_dispatch.add(task)
            task.add_done_callback(self._active_local_dispatch.discard)
            try:
                return await asyncio.wait_for(task, timeout=remaining)
            except asyncio.TimeoutError:
                return RpcReply(504, {"detail": "Signaling operation timed out."}, {})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Local signaling dispatch failed")
                return RpcReply(502, {"detail": "WebRTC signaling failed."}, {})
        finally:
            if admitted_here:
                self._release_dispatch_slot()

    async def request(
        self,
        route: LiveRoute,
        action: Literal["candidates", "renegotiate"],
        payload: dict[str, Any],
        *,
        deadline: RpcDeadline,
    ) -> RpcReply:
        if deadline.remaining_seconds <= 0:
            return RpcReply(504, {"detail": "Signaling operation timed out."}, {})
        if not _validate_forwarded_payload(action, payload):
            return RpcReply(422, {"detail": "Invalid signaling payload."}, {})
        if not await self._try_acquire_reply_slot():
            return RpcReply(503, {"detail": "Signaling relay is overloaded."}, {})
        try:
            rpc_id = secrets.token_hex(16)
            reply_queue = self.resources.key(f"queue:rpc:reply:{rpc_id}")
            target_queue = self.resources.key(f"queue:worker:{route.claim.worker_id}:signals")
            heartbeat_key = self.resources.key(f"worker:{route.claim.worker_id}:heartbeat")
            envelope = {
                "rpc_id": rpc_id,
                "action": action,
                "pc_id": route.pc_id,
                "claim": {
                    "session_id": str(route.claim.session_id),
                    "agent_id": str(route.claim.agent_id),
                    "call_type": route.claim.call_type,
                    "generation": route.claim.generation,
                    "worker_id": route.claim.worker_id,
                    "phase": route.claim.phase,
                },
                "payload": payload,
                "expires_at_ms": deadline.expires_at_ms,
            }
            try:
                raw_envelope = json.dumps(envelope, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                return RpcReply(400, {"detail": "Invalid signaling payload."}, {})
            if (
                len(raw_envelope.encode("utf-8"))
                > _ACTION_BODY_LIMITS[action] + MAX_ENVELOPE_OVERHEAD_BYTES
            ):
                return RpcReply(413, {"detail": "Signaling request is too large."}, {})
            try:
                admitted, _ = await self.resources.commands.eval(
                    _ENQUEUE_LUA,
                    2,
                    target_queue,
                    heartbeat_key,
                    raw_envelope,
                    deadline.expires_at_ms,
                )
            except Exception:
                return RpcReply(503, {"detail": "Coordination service unavailable."}, {})
            if int(admitted) == -2:
                return RpcReply(504, {"detail": "Signaling operation timed out."}, {})
            if int(admitted) == -3:
                return RpcReply(410, {"detail": "Call session is no longer active."}, {})
            if int(admitted) != 1:
                return RpcReply(503, {"detail": "Signaling target worker is overloaded."}, {})

            remaining = deadline.remaining_seconds
            if remaining <= 0:
                return RpcReply(504, {"detail": "Signaling operation timed out."}, {})
            try:
                blpop_timeout = max(1, int(math.ceil(remaining)))
                raw_res = await asyncio.wait_for(
                    self.resources.blocking.blpop(reply_queue, timeout=blpop_timeout),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return RpcReply(504, {"detail": "Signaling operation timed out."}, {})
            except Exception:
                return RpcReply(503, {"detail": "Signaling relay connection error."}, {})
            finally:
                try:
                    await self.resources.commands.unlink(reply_queue)
                except Exception:
                    pass
            if raw_res is None:
                return RpcReply(504, {"detail": "Signaling gateway timeout communicating with call worker."}, {})
            _, raw_payload = raw_res
            if isinstance(raw_payload, str):
                raw_payload = raw_payload.encode("utf-8")
            if not isinstance(raw_payload, bytes) or len(raw_payload) > MAX_REPLY_BYTES:
                return RpcReply(502, {"detail": "Invalid signaling reply from worker."}, {})
            try:
                res_data = json.loads(raw_payload)
                reply = RpcReply(
                    status_code=int(res_data["status_code"]),
                    body=res_data["body"],
                    headers=res_data.get("headers", {}),
                )
                reply.to_bytes()
                return RpcReply(
                    status_code=reply.status_code,
                    body=reply.body,
                    headers=_sanitize_headers(reply.headers),
                )
            except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                return RpcReply(502, {"detail": "Invalid signaling reply from worker."}, {})
        finally:
            await self._release_reply_slot()

    async def run(self) -> None:
        queue_key = self.resources.key(f"queue:worker:{self.worker_id}:signals")
        while not self._stopping:
            await self._dispatch_semaphore.acquire()
            if self._stopping:
                self._dispatch_semaphore.release()
                break
            try:
                item = await self.resources.blocking.brpop(queue_key, timeout=1.0)
                if self._stopping:
                    self._dispatch_semaphore.release()
                    break
                if item is None:
                    self._dispatch_semaphore.release()
                    continue
                _, raw_envelope = item
                task = asyncio.create_task(self._process_with_slot(raw_envelope))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
            except asyncio.CancelledError:
                self._dispatch_semaphore.release()
                break
            except Exception:
                self._dispatch_semaphore.release()
                if not self._stopping:
                    logger.exception("Signaling RPC run loop error")

    async def _process_with_slot(self, raw_envelope: str) -> None:
        try:
            await self._process_one(raw_envelope)
        finally:
            self._dispatch_semaphore.release()

    async def _process_one(self, raw_envelope: str) -> None:
        raw_bytes = raw_envelope.encode("utf-8") if isinstance(raw_envelope, str) else raw_envelope
        if not isinstance(raw_bytes, bytes) or len(raw_bytes) > MAX_OFFER_BYTES + MAX_ENVELOPE_OVERHEAD_BYTES:
            logger.warning("Dropping oversized signaling RPC envelope")
            return
        try:
            data = json.loads(raw_bytes)
            rpc_id = str(data["rpc_id"])
            if len(rpc_id) != 32 or not all(c in "0123456789abcdef" for c in rpc_id):
                raise ValueError("invalid rpc id")
            action = data["action"]
            if not isinstance(action, str) or action not in _ACTION_BODY_LIMITS:
                raise ValueError("invalid action")
            if len(raw_bytes) > _ACTION_BODY_LIMITS[action] + MAX_ENVELOPE_OVERHEAD_BYTES:
                logger.warning("Dropping oversized signaling RPC envelope for %s", action)
                return
            payload = data["payload"]
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
            if payload.get("pc_id") != data.get("pc_id"):
                payload = {**payload, "pc_id": data["pc_id"]}
            if not _validate_forwarded_payload(action, payload):
                logger.warning("Dropping signaling RPC payload that exceeds action limits")
                return
            expires_at_ms = int(data["expires_at_ms"])
            claim_data = data["claim"]
            if not isinstance(claim_data, dict):
                raise ValueError("invalid claim")
            claim = CallClaim(
                session_id=uuid.UUID(str(claim_data["session_id"])),
                agent_id=uuid.UUID(str(claim_data["agent_id"])),
                call_type=claim_data["call_type"],
                generation=str(claim_data["generation"]),
                worker_id=claim_data["worker_id"],
                phase=claim_data["phase"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Dropping invalid signaling RPC envelope")
            return
        try:
            deadline = await self._deadline_from_remote(expires_at_ms)
        except HTTPException:
            return
        if deadline is None:
            return
        reply = await self.dispatch_local_with_deadline(
            action, payload, claim, deadline=deadline, _admitted=True
        )
        try:
            reply_bytes = reply.to_bytes()
        except ValueError:
            reply_bytes = RpcReply(
                502,
                {"detail": "Invalid signaling reply from worker."},
                {},
            ).to_bytes()
        reply_queue = self.resources.key(f"queue:rpc:reply:{rpc_id}")
        try:
            await self.resources.commands.eval(_PUBLISH_REPLY_LUA, 1, reply_queue, reply_bytes)
        except Exception:
            logger.exception("Failed to publish signaling RPC reply")

    def begin_shutdown(self) -> None:
        """Stop accepting new RPC work while in-flight dispatch may finish."""
        self._stopping = True

    async def drain(self, timeout: float = 5.0) -> bool:
        """Wait for in-flight remote and local dispatch, bounded by timeout.

        Returns True when every in-flight task finished before the timeout.
        """
        pending = set(self._active_tasks) | set(self._active_local_dispatch)
        if not pending:
            return True
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        return not still_pending

    async def close(self, drain_timeout: float = 5.0) -> None:
        self._stopping = True
        await self.drain(drain_timeout)
        pending = set(self._active_tasks) | set(self._active_local_dispatch)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
