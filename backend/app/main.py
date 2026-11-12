from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import secrets
import uuid

from fastapi import FastAPI
from openai import DefaultAsyncHttpxClient
from sqlalchemy import inspect

from . import admin, agents, auth, chat, observability, provider_catalog, sessions, signaling
from .coordination import Coordination
from .db import Session as DbSession, engine
from .redis import open_redis_resources
from .signaling_rpc import SignalingRpc
from .tts_cache import TTSAudioCache

logger = logging.getLogger(__name__)

_SIGNALING_DRAIN_SECONDS = 5.0
_REAPER_LEADER_TTL_SECONDS = 20
_LEADER_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], 20)
end
return 0
"""
_LEADER_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def _check_schema_columns() -> None:
    async with DbSession() as db:
        connection = await db.connection()

        def check(sync_conn):
            insp = inspect(sync_conn)
            if not insp.has_table("sessions"):
                return False
            cols = {c["name"] for c in insp.get_columns("sessions")}
            required = {"worker_id", "claim_generation", "lease_expires_at"}
            return required.issubset(cols)

        has_cols = await connection.run_sync(check)
        if not has_cols:
            raise RuntimeError(
                "Database schema is missing required coordination columns. "
                "Run 'alembic upgrade head' before starting the backend."
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.configure_tracing()
    await _check_schema_columns()

    worker_id = f"worker-{uuid.uuid4().hex[:12]}"
    app.state.worker_id = worker_id

    redis_url = os.environ.get("REDIS_URL") or "redis://localhost:6379"
    redis_namespace = getattr(app.state, "redis_namespace", os.environ.get("REDIS_NAMESPACE", "voice-ai"))

    # Reuse existing redis resources if fixture provided them (e.g. conftest)
    redis_resources = getattr(app.state, "redis", None)
    own_resources = False
    if redis_resources is None:
        redis_resources = await open_redis_resources(redis_url, namespace=redis_namespace)
        app.state.redis = redis_resources
        own_resources = True

    coordination = Coordination(redis_resources, worker_id)
    app.state.coordination = coordination

    signaling_rpc = SignalingRpc(
        resources=redis_resources,
        worker_id=worker_id,
        dispatch_local=signaling._dispatch_local_signal,
    )
    app.state.signaling_rpc = signaling_rpc

    stopping = asyncio.Event()

    # 1. Worker Heartbeat Loop (uses reserved control pool and SSCAN)
    async def heartbeat_loop() -> None:
        hb_key = redis_resources.key(f"worker:{worker_id}:heartbeat")
        active_set = redis_resources.key("workers:active")
        scan_cursor = 0
        sweep_counter = 0
        while not stopping.is_set():
            try:
                await redis_resources.control.set(hb_key, "1", ex=15)
                await redis_resources.control.sadd(active_set, worker_id)

                sweep_counter += 1
                if sweep_counter >= 3:
                    sweep_counter = 0
                    scan_cursor, members = await redis_resources.control.sscan(active_set, scan_cursor, count=20)
                    stale_candidates = [m for m in members if m != worker_id]
                    if stale_candidates:
                        pipe = redis_resources.control.pipeline()
                        for m in stale_candidates:
                            pipe.exists(redis_resources.key(f"worker:{m}:heartbeat"))
                        results = await pipe.execute()
                        rem_pipe = redis_resources.control.pipeline()
                        for m, alive in zip(stale_candidates, results):
                            if not alive:
                                rem_pipe.srem(active_set, m)
                        await rem_pipe.execute()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Heartbeat error: %s", exc)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    # 2. Reconciler Leader Election & Reaper Loop
    async def reaper_loop() -> None:
        leader_key = redis_resources.key("reconciler:leader")
        while not stopping.is_set():
            leader_nonce = secrets.token_hex(16)
            try:
                # Attempt to acquire leader lease
                acquired = await redis_resources.control.set(
                    leader_key, leader_nonce, nx=True, ex=_REAPER_LEADER_TTL_SECONDS
                )
                if acquired:
                    leadership_lost = asyncio.Event()

                    async def leader_renewal() -> None:
                        while not leadership_lost.is_set() and not stopping.is_set():
                            try:
                                await asyncio.sleep(5.0)
                                if leadership_lost.is_set() or stopping.is_set():
                                    break
                                res = await redis_resources.control.eval(
                                    _LEADER_RENEW_LUA, 1, leader_key, leader_nonce
                                )
                                if int(res or 0) != 1:
                                    logger.warning("Reaper leadership renewal failed; lost leadership")
                                    leadership_lost.set()
                                    break
                            except asyncio.CancelledError:
                                break
                            except Exception as renew_exc:
                                logger.warning("Reaper leadership renewal error: %s", renew_exc)
                                leadership_lost.set()
                                break

                    renew_task = asyncio.create_task(leader_renewal())
                    try:
                        await sessions.reconcile_voice_sessions(
                            coordination,
                            is_leader=lambda: not leadership_lost.is_set(),
                        )
                    finally:
                        renew_task.cancel()
                        try:
                            await renew_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                        if not leadership_lost.is_set():
                            try:
                                await redis_resources.control.eval(
                                    _LEADER_RELEASE_LUA, 1, leader_key, leader_nonce
                                )
                            except Exception:
                                pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Reaper loop error: %s", exc)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    reaper_task = asyncio.create_task(reaper_loop())
    rpc_task = asyncio.create_task(signaling_rpc.run())

    async with DefaultAsyncHttpxClient() as openai_http_client:
        app.state.openai_http_client = openai_http_client
        # The optional TTS audio cache. It is owned by the app, so every call
        # shares one client, and it never keeps the application from starting:
        # a cache that cannot be used simply admits nothing.
        tts_cache = TTSAudioCache(
            redis_url=os.environ.get("TTS_CACHE_REDIS_URL"),
            phrases_json=os.environ.get("TTS_CACHE_PHRASES_JSON"),
        )
        app.state.tts_cache = tts_cache
        try:
            yield
        finally:
            stopping.set()

            # 1. Withdraw worker liveness immediately so other workers cannot enqueue
            # new requests to this worker during shutdown drain.
            heartbeat_task.cancel()
            try:
                hb_key = redis_resources.key(f"worker:{worker_id}:heartbeat")
                active_set = redis_resources.key("workers:active")
                await redis_resources.control.delete(hb_key)
                await redis_resources.control.srem(active_set, worker_id)
            except Exception:
                pass

            # 2. Stop RPC admission. In-flight signaling gets a bounded drain
            # while call media and watchdogs are still valid.
            signaling_rpc.begin_shutdown()
            drained = await signaling_rpc.drain(_SIGNALING_DRAIN_SECONDS)
            if not drained:
                logger.warning("Signaling RPC drain timed out; cancelling remaining dispatch")
            rpc_task.cancel()

            # 3. Stop local runtimes and their media only after the drain bound.
            await signaling.stop_all_local_runtimes()

            # 4. Stop remaining background tasks and close RPC.
            reaper_task.cancel()
            await signaling_rpc.close(drain_timeout=0)

            await asyncio.gather(heartbeat_task, reaper_task, rpc_task, return_exceptions=True)

            if own_resources:
                await redis_resources.close()

            app.state.coordination = None
            app.state.redis = None
            app.state.signaling_rpc = None
            app.state.worker_id = None
            app.state.tts_cache = None

            await tts_cache.close()
            await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(admin.router)
app.include_router(provider_catalog.router)
app.include_router(sessions.router)
app.include_router(sessions.public_router)
app.include_router(signaling.router)
app.include_router(chat.chat_router)
