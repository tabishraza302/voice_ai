from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError, ResponseError, TimeoutError as RedisTimeoutError

from .redis import RedisResources

logger = logging.getLogger(__name__)

_RATE_WINDOW_SECONDS = 60
_IP_ALLOWANCE = 60
_AGENT_ALLOWANCE = 120
_MAX_TRACKED_KEYS = 10_000

# Lua scripts for atomic operations
_RATE_CHECK_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local rate_key = KEYS[1]
local tracked_keys_key = KEYS[2]
local limit = tonumber(ARGV[1])
local member_id = ARGV[2]

-- 1. Prune expired requests in sliding window
redis.call('ZREMRANGEBYSCORE', rate_key, '-inf', now - 60)
local count = redis.call('ZCARD', rate_key)

if count >= limit then
    local oldest = redis.call('ZRANGE', rate_key, 0, 0, 'WITHSCORES')
    local retry_after = 60
    if #oldest >= 2 then
        retry_after = math.max(1, math.ceil(tonumber(oldest[2]) + 60 - now))
    end
    return {0, retry_after}
end

-- 2. Prune expired tracked keys
redis.call('ZREMRANGEBYSCORE', tracked_keys_key, '-inf', now)

-- 3. Check if rate_key is already tracked; if not, check cardinality cap
local is_tracked = redis.call('ZSCORE', tracked_keys_key, rate_key)
if not is_tracked then
    local tracked_count = redis.call('ZCARD', tracked_keys_key)
    if tracked_count >= 10000 then
        return {-1, 60} -- Capacity exhausted
    end
end
redis.call('ZADD', tracked_keys_key, now + 60, rate_key)
redis.call('EXPIRE', tracked_keys_key, 120)

-- 4. Record new request
redis.call('ZADD', rate_key, now, member_id)
redis.call('EXPIRE', rate_key, 60)
return {1, 0}
"""

_RESERVE_CREATED_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local lease_key = KEYS[1]
local agent_zset = KEYS[2]
local global_zset = KEYS[3]

local session_id = ARGV[1]
local agent_id = ARGV[2]
local call_type = ARGV[3]
local generation = ARGV[4]
local per_agent_limit = tonumber(ARGV[5])
local overall_limit = tonumber(ARGV[6])

-- Prune expired members from ZSETs
redis.call('ZREMRANGEBYSCORE', agent_zset, '-inf', now)
redis.call('ZREMRANGEBYSCORE', global_zset, '-inf', now)

-- Check capacity
local agent_count = redis.call('ZCARD', agent_zset)
if agent_count >= per_agent_limit then
    return {0, "limit", "Concurrent call limit reached for this agent."}
end

local global_count = redis.call('ZCARD', global_zset)
if global_count >= overall_limit then
    return {0, "limit", "Concurrent call limit reached overall."}
end

-- Check if lease already exists
local existing_expires = redis.call('HGET', lease_key, 'lease_expires_at')
if existing_expires and tonumber(existing_expires) > now then
    return {0, "conflict", "This call already has a signaling connection."}
end

local lease_deadline = now + 120.0
redis.call('HSET', lease_key,
    'session_id', session_id,
    'agent_id', agent_id,
    'call_type', call_type,
    'generation', generation,
    'worker_id', '',
    'phase', 'created',
    'lease_expires_at', tostring(lease_deadline)
)
redis.call('EXPIRE', lease_key, 150) -- 120s deadline + 30s metadata retention

redis.call('ZADD', agent_zset, lease_deadline, session_id)
redis.call('ZADD', global_zset, lease_deadline, session_id)

return {1, tostring(lease_deadline), 120.0}
"""

_BEGIN_SETUP_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local lease_key = KEYS[1]
local agent_zset = KEYS[2]
local global_zset = KEYS[3]

local session_id = ARGV[1]
local expected_gen = ARGV[2]
local new_gen = ARGV[3]
local worker_id = ARGV[4]

local current_gen = redis.call('HGET', lease_key, 'generation')
local current_phase = redis.call('HGET', lease_key, 'phase')
local current_expires = redis.call('HGET', lease_key, 'lease_expires_at')

if not current_gen or current_gen ~= expected_gen or current_phase ~= 'created' or not current_expires or tonumber(current_expires) <= now then
    return {0, "invalid"}
end

local new_deadline = now + 45.0 -- 45s setup lease
redis.call('HSET', lease_key,
    'generation', new_gen,
    'worker_id', worker_id,
    'phase', 'starting',
    'lease_expires_at', tostring(new_deadline)
)
redis.call('EXPIRE', lease_key, 75) -- 45s + 30s grace

redis.call('ZADD', agent_zset, new_deadline, session_id)
redis.call('ZADD', global_zset, new_deadline, session_id)

return {1, tostring(new_deadline), 45.0}
"""

_ACTIVATE_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local lease_key = KEYS[1]
local routing_key = KEYS[2]
local agent_zset = KEYS[3]
local global_zset = KEYS[4]

local session_id = ARGV[1]
local expected_gen = ARGV[2]
local worker_id = ARGV[3]
local pc_id = ARGV[4]
local agent_id = ARGV[5]
local call_type = ARGV[6]

local current_gen = redis.call('HGET', lease_key, 'generation')
local current_worker = redis.call('HGET', lease_key, 'worker_id')
local current_phase = redis.call('HGET', lease_key, 'phase')
local current_expires = redis.call('HGET', lease_key, 'lease_expires_at')

if not current_gen or current_gen ~= expected_gen or current_worker ~= worker_id or current_phase ~= 'starting' or not current_expires or tonumber(current_expires) <= now then
    return {0, "invalid"}
end

local new_deadline = now + 15.0 -- 15s active lease
redis.call('HSET', lease_key,
    'phase', 'active',
    'lease_expires_at', tostring(new_deadline)
)
redis.call('EXPIRE', lease_key, 45) -- 15s + 30s grace

redis.call('ZADD', agent_zset, new_deadline, session_id)
redis.call('ZADD', global_zset, new_deadline, session_id)

-- Publish routing: worker_id:generation:agent_id:session_id:call_type
redis.call('SET', routing_key, worker_id .. ':' .. expected_gen .. ':' .. agent_id .. ':' .. session_id .. ':' .. call_type, 'EX', 20)

return {1, tostring(new_deadline), 15.0}
"""

_RENEW_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local lease_key = KEYS[1]
local routing_key = KEYS[2]
local agent_zset = KEYS[3]
local global_zset = KEYS[4]

local session_id = ARGV[1]
local expected_gen = ARGV[2]
local worker_id = ARGV[3]
local expected_phase = ARGV[4]
local agent_id = ARGV[5]

local current_gen = redis.call('HGET', lease_key, 'generation')
local current_worker = redis.call('HGET', lease_key, 'worker_id')
local current_phase = redis.call('HGET', lease_key, 'phase')
local current_expires = redis.call('HGET', lease_key, 'lease_expires_at')

if not current_gen or current_gen ~= expected_gen or current_worker ~= worker_id or current_phase ~= expected_phase or not current_expires or tonumber(current_expires) <= now then
    return {0, "invalid"}
end

local ttl = 15.0
if current_phase == 'starting' then
    ttl = 45.0
end
local new_deadline = now + ttl

redis.call('HSET', lease_key, 'lease_expires_at', tostring(new_deadline))
redis.call('EXPIRE', lease_key, math.ceil(ttl + 30))

redis.call('ZADD', agent_zset, new_deadline, session_id)
redis.call('ZADD', global_zset, new_deadline, session_id)

if routing_key and routing_key ~= '' then
    local current_route = redis.call('GET', routing_key)
    if current_route then
        local parts = {}
        for p in string.gmatch(current_route, "[^:]+") do
            table.insert(parts, p)
        end
        if #parts >= 2 and parts[2] == expected_gen and parts[1] == worker_id then
            redis.call('EXPIRE', routing_key, math.ceil(ttl + 5))
        end
    end
end

return {1, tostring(new_deadline), ttl}
"""

_RELEASE_LUA = """
local lease_key = KEYS[1]
local routing_key = KEYS[2]
local agent_zset = KEYS[3]
local global_zset = KEYS[4]

local session_id = ARGV[1]
local expected_gen = ARGV[2]
local expected_worker = ARGV[3]

local current_gen = redis.call('HGET', lease_key, 'generation')
local current_worker = redis.call('HGET', lease_key, 'worker_id')

if not current_gen or current_gen ~= expected_gen then
    return 0 -- Stale generation, do not delete replacement!
end
if expected_worker and expected_worker ~= '' and current_worker and current_worker ~= '' and current_worker ~= expected_worker then
    return 0 -- Worker mismatch, do not delete other worker's lease!
end

redis.call('DEL', lease_key)
redis.call('ZREM', agent_zset, session_id)
redis.call('ZREM', global_zset, session_id)

if routing_key and routing_key ~= '' then
    local current_route = redis.call('GET', routing_key)
    if current_route then
        local parts = {}
        for p in string.gmatch(current_route, "[^:]+") do
            table.insert(parts, p)
        end
        if #parts >= 2 and parts[2] == expected_gen then
            redis.call('DEL', routing_key)
        end
    end
end
return 1
"""

_ISSUE_TOKEN_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local token_key = KEYS[1]
local lease_key = KEYS[2]

local session_id = ARGV[1]
local agent_id = ARGV[2]
local call_type = ARGV[3]
local generation = ARGV[4]
local max_remaining_seconds = tonumber(ARGV[5])

local stored_gen = redis.call('HGET', lease_key, 'generation')
local stored_phase = redis.call('HGET', lease_key, 'phase')
local lease_expires = tonumber(redis.call('HGET', lease_key, 'lease_expires_at') or '0')

if not stored_gen or stored_gen ~= generation or stored_phase ~= 'created' then
    return {0, "invalid_lease", "Call lease is no longer valid."}
end

local remaining = lease_expires - now
if remaining <= 0 then
    return {0, "expired", "Call lease has expired."}
end

local ttl = math.min(max_remaining_seconds, remaining)
if ttl <= 0 then
    return {0, "expired", "Call deadline has expired."}
end

local payload = cjson.encode({
    session_id = session_id,
    agent_id = agent_id,
    call_type = call_type,
    generation = generation,
    expires_at = now + ttl,
})
redis.call('SET', token_key, payload, 'EX', math.ceil(ttl))
return {1, tostring(now + ttl), ttl}
"""

_CONSUME_TOKEN_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local token_key = KEYS[1]

local payload = redis.call('GETDEL', token_key)
if not payload then
    return {0, "not_found", "Signaling token is invalid or expired."}
end

local ok, data = pcall(cjson.decode, payload)
if not ok or not data then
    return {0, "corrupt", "Signaling token is invalid or expired."}
end

local expires_at = tonumber(data.expires_at or 0)
if expires_at <= now then
    return {0, "expired", "Signaling token is invalid or expired."}
end

return {1, data.session_id, data.agent_id, data.call_type, data.generation}
"""


@dataclass(frozen=True, slots=True)
class CallClaim:
    session_id: uuid.UUID
    agent_id: uuid.UUID
    call_type: Literal["test", "public"]
    generation: str
    worker_id: str | None
    phase: Literal["created", "starting", "active"]


@dataclass(frozen=True, slots=True)
class LeaseObservation:
    claim: CallClaim
    monotonic_expires_at: float

    def __init__(
        self,
        claim: CallClaim,
        monotonic_expires_at: float | None = None,
        *,
        remaining_seconds: float | None = None,
    ) -> None:
        object.__setattr__(self, "claim", claim)
        if monotonic_expires_at is not None:
            object.__setattr__(self, "monotonic_expires_at", float(monotonic_expires_at))
        elif remaining_seconds is not None:
            object.__setattr__(
                self,
                "monotonic_expires_at",
                time.monotonic() + max(0.0, float(remaining_seconds)),
            )
        else:
            object.__setattr__(self, "monotonic_expires_at", time.monotonic())

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.monotonic_expires_at - time.monotonic())


@dataclass(frozen=True, slots=True)
class LiveRoute:
    pc_id: str
    claim: CallClaim


class Coordination:
    def __init__(self, resources: RedisResources, worker_id: str):
        self.resources = resources
        self.worker_id = worker_id

    async def _eval_lua(self, script: str, numkeys: int, *keys_and_args: Any, use_control: bool = False) -> Any:
        client = self.resources.control if use_control else self.resources.commands
        try:
            return await client.eval(script, numkeys, *keys_and_args)
        except (RedisConnectionError, RedisTimeoutError, ResponseError) as exc:
            logger.error("Redis coordination error: %s", exc)
            raise HTTPException(status_code=503, detail="Coordination service unavailable.") from exc

    async def check_ip(self, client_ip: str | None) -> None:
        key = self.resources.key(f"ratelimit:ip:{client_ip or 'unknown'}")
        tracked_keys = self.resources.key("ratelimit:tracked_keys")
        member_id = f"{secrets.token_hex(8)}"
        res = await self._eval_lua(_RATE_CHECK_LUA, 2, key, tracked_keys, _IP_ALLOWANCE, member_id)
        status, retry_after = int(res[0]), int(res[1])
        if status == -1:
            raise HTTPException(
                status_code=429,
                detail="Signaling rate limiter is at capacity.",
                headers={"Retry-After": str(retry_after)},
            )
        if status == 0:
            raise HTTPException(
                status_code=429,
                detail="Signaling request rate limit exceeded.",
                headers={"Retry-After": str(retry_after)},
            )

    async def check_agent(self, agent_id: uuid.UUID) -> None:
        key = self.resources.key(f"ratelimit:agent:{agent_id}")
        tracked_keys = self.resources.key("ratelimit:tracked_keys")
        member_id = f"{secrets.token_hex(8)}"
        res = await self._eval_lua(_RATE_CHECK_LUA, 2, key, tracked_keys, _AGENT_ALLOWANCE, member_id)
        status, retry_after = int(res[0]), int(res[1])
        if status == -1:
            raise HTTPException(
                status_code=429,
                detail="Signaling rate limiter is at capacity.",
                headers={"Retry-After": str(retry_after)},
            )
        if status == 0:
            raise HTTPException(
                status_code=429,
                detail="Signaling request rate limit exceeded.",
                headers={"Retry-After": str(retry_after)},
            )

    async def reserve_created(
        self,
        session_id: uuid.UUID,
        agent_id: uuid.UUID,
        call_type: Literal["test", "public"],
        per_agent_limit: int,
        overall_limit: int,
    ) -> LeaseObservation:
        generation = secrets.token_urlsafe(16)
        lease_key = self.resources.key(f"call:lease:{session_id}")
        agent_zset = self.resources.key(f"leases:active:agent:{agent_id}")
        global_zset = self.resources.key("leases:active:global")

        start_mono = time.monotonic()
        res = await self._eval_lua(
            _RESERVE_CREATED_LUA,
            3,
            lease_key,
            agent_zset,
            global_zset,
            str(session_id),
            str(agent_id),
            call_type,
            generation,
            per_agent_limit,
            overall_limit,
            use_control=True,
        )
        elapsed = time.monotonic() - start_mono
        success = int(res[0])
        if success == 1:
            remaining = max(0.0, float(res[2]) - elapsed)
            claim = CallClaim(
                session_id=session_id,
                agent_id=agent_id,
                call_type=call_type,
                generation=generation,
                worker_id=None,
                phase="created",
            )
            return LeaseObservation(claim=claim, remaining_seconds=remaining)
        error_type, detail = str(res[1]), str(res[2])
        if error_type == "limit":
            raise HTTPException(status_code=429, detail=detail)
        raise HTTPException(status_code=409, detail=detail)

    async def issue_token(self, claim: CallClaim, max_remaining_seconds: float) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_key = self.resources.key(f"token:sig:{token_hash}")
        lease_key = self.resources.key(f"call:lease:{claim.session_id}")

        res = await self._eval_lua(
            _ISSUE_TOKEN_LUA,
            2,
            token_key,
            lease_key,
            str(claim.session_id),
            str(claim.agent_id),
            claim.call_type,
            claim.generation,
            max_remaining_seconds,
            use_control=True,
        )
        if int(res[0]) != 1:
            err = str(res[1])
            if err == "expired":
                raise HTTPException(status_code=409, detail="Call lease has expired.")
            raise HTTPException(status_code=409, detail="Call lease is no longer valid.")
        return token

    async def consume_token(self, token: str) -> CallClaim:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_key = self.resources.key(f"token:sig:{token_hash}")

        res = await self._eval_lua(_CONSUME_TOKEN_LUA, 1, token_key, use_control=True)
        if int(res[0]) != 1:
            raise HTTPException(status_code=401, detail="Signaling token is invalid or expired.")

        session_id = uuid.UUID(str(res[1]))
        agent_id = uuid.UUID(str(res[2]))
        call_type = str(res[3])
        generation = str(res[4])

        return CallClaim(
            session_id=session_id,
            agent_id=agent_id,
            call_type=call_type,  # type: ignore[arg-type]
            generation=generation,
            worker_id=None,
            phase="created",
        )

    async def inspect(self, session_id: uuid.UUID) -> LeaseObservation | None:
        lease_key = self.resources.key(f"call:lease:{session_id}")
        start_mono = time.monotonic()
        try:
            pipe = self.resources.control.pipeline()
            pipe.time()
            pipe.hgetall(lease_key)
            clock_res, data = await pipe.execute()
        except (RedisConnectionError, RedisTimeoutError, ResponseError) as exc:
            raise HTTPException(status_code=503, detail="Coordination service unavailable.") from exc

        if not data:
            return None

        elapsed = time.monotonic() - start_mono
        now = float(clock_res[0]) + float(clock_res[1]) / 1_000_000.0
        try:
            expires_at = float(data["lease_expires_at"])
            remaining = (expires_at - now) - elapsed
            if remaining <= 0:
                return None
            worker_id = data.get("worker_id") or None
            claim = CallClaim(
                session_id=uuid.UUID(data["session_id"]),
                agent_id=uuid.UUID(data["agent_id"]),
                call_type=data["call_type"],
                generation=data["generation"],
                worker_id=worker_id if worker_id else None,
                phase=data["phase"],
            )
            return LeaseObservation(claim=claim, remaining_seconds=max(0.0, remaining))
        except (KeyError, ValueError, TypeError):
            return None

    async def begin_setup(self, claim: CallClaim) -> LeaseObservation:
        new_gen = secrets.token_urlsafe(16)
        lease_key = self.resources.key(f"call:lease:{claim.session_id}")
        agent_zset = self.resources.key(f"leases:active:agent:{claim.agent_id}")
        global_zset = self.resources.key("leases:active:global")

        start_mono = time.monotonic()
        res = await self._eval_lua(
            _BEGIN_SETUP_LUA,
            3,
            lease_key,
            agent_zset,
            global_zset,
            str(claim.session_id),
            claim.generation,
            new_gen,
            self.worker_id,
            use_control=True,
        )
        elapsed = time.monotonic() - start_mono
        if int(res[0]) != 1:
            raise HTTPException(status_code=409, detail="Call lease is no longer valid for setup.")

        remaining = max(0.0, float(res[2]) - elapsed)
        new_claim = CallClaim(
            session_id=claim.session_id,
            agent_id=claim.agent_id,
            call_type=claim.call_type,
            generation=new_gen,
            worker_id=self.worker_id,
            phase="starting",
        )
        return LeaseObservation(claim=new_claim, remaining_seconds=remaining)

    async def activate(self, claim: CallClaim, pc_id: str) -> LeaseObservation:
        lease_key = self.resources.key(f"call:lease:{claim.session_id}")
        routing_key = self.resources.key(f"call:routing:{pc_id}")
        agent_zset = self.resources.key(f"leases:active:agent:{claim.agent_id}")
        global_zset = self.resources.key("leases:active:global")

        start_mono = time.monotonic()
        res = await self._eval_lua(
            _ACTIVATE_LUA,
            4,
            lease_key,
            routing_key,
            agent_zset,
            global_zset,
            str(claim.session_id),
            claim.generation,
            self.worker_id,
            pc_id,
            str(claim.agent_id),
            claim.call_type,
            use_control=True,
        )
        elapsed = time.monotonic() - start_mono
        if int(res[0]) != 1:
            raise HTTPException(status_code=409, detail="Call lease is no longer valid for activation.")

        remaining = max(0.0, float(res[2]) - elapsed)
        new_claim = CallClaim(
            session_id=claim.session_id,
            agent_id=claim.agent_id,
            call_type=claim.call_type,
            generation=claim.generation,
            worker_id=self.worker_id,
            phase="active",
        )
        return LeaseObservation(claim=new_claim, remaining_seconds=remaining)

    async def renew(self, claim: CallClaim, pc_id: str | None) -> LeaseObservation:
        lease_key = self.resources.key(f"call:lease:{claim.session_id}")
        routing_key = self.resources.key(f"call:routing:{pc_id}") if pc_id else ""
        agent_zset = self.resources.key(f"leases:active:agent:{claim.agent_id}")
        global_zset = self.resources.key("leases:active:global")

        start_mono = time.monotonic()
        res = await self._eval_lua(
            _RENEW_LUA,
            4,
            lease_key,
            routing_key,
            agent_zset,
            global_zset,
            str(claim.session_id),
            claim.generation,
            self.worker_id,
            claim.phase,
            str(claim.agent_id),
            use_control=True,
        )
        elapsed = time.monotonic() - start_mono
        if int(res[0]) != 1:
            raise RuntimeError("Lease renewal failed: lease missing or expired.")

        remaining = max(0.0, float(res[2]) - elapsed)
        return LeaseObservation(claim=claim, remaining_seconds=remaining)

    async def release(self, claim: CallClaim, pc_id: str | None) -> bool:
        lease_key = self.resources.key(f"call:lease:{claim.session_id}")
        routing_key = self.resources.key(f"call:routing:{pc_id}") if pc_id else ""
        agent_zset = self.resources.key(f"leases:active:agent:{claim.agent_id}")
        global_zset = self.resources.key("leases:active:global")

        res = await self._eval_lua(
            _RELEASE_LUA,
            4,
            lease_key,
            routing_key,
            agent_zset,
            global_zset,
            str(claim.session_id),
            claim.generation,
            claim.worker_id or "",
            use_control=True,
        )
        return int(res) == 1

    async def route(self, pc_id: str) -> LiveRoute | None:
        routing_key = self.resources.key(f"call:routing:{pc_id}")
        try:
            val = await self.resources.commands.get(routing_key)
        except (RedisConnectionError, RedisTimeoutError, ResponseError) as exc:
            raise HTTPException(status_code=503, detail="Coordination service unavailable.") from exc

        if not val:
            return None

        # Value format: "worker_id:generation:agent_id:session_id:call_type"
        parts = val.split(":")
        if len(parts) != 5:
            return None

        worker_id, generation, raw_agent, raw_session, call_type = parts
        if call_type not in ("test", "public"):
            return None
        try:
            claim = CallClaim(
                session_id=uuid.UUID(raw_session),
                agent_id=uuid.UUID(raw_agent),
                call_type=call_type,  # type: ignore[arg-type]
                generation=generation,
                worker_id=worker_id,
                phase="active",
            )
            return LiveRoute(pc_id=pc_id, claim=claim)
        except (ValueError, TypeError):
            return None
