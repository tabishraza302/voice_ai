from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from openai import DefaultAsyncHttpxClient

from . import admin, agents, auth, chat, observability, provider_catalog, sessions, signaling
from .db import engine
from .redis import open_redis_resources


@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.configure_tracing()

    redis_url = os.environ.get("REDIS_URL") or "redis://localhost:6379"
    redis_namespace = getattr(app.state, "redis_namespace", os.environ.get("REDIS_NAMESPACE", "voice-ai"))

    redis_resources = getattr(app.state, "redis", None)
    own_resources = False
    if redis_resources is None:
        redis_resources = await open_redis_resources(redis_url, namespace=redis_namespace)
        app.state.redis = redis_resources
        own_resources = True

    async with DefaultAsyncHttpxClient() as openai_http_client:
        app.state.openai_http_client = openai_http_client
        try:
            await sessions.recover_interrupted_sessions()
            yield
        finally:
            if own_resources and redis_resources is not None:
                await redis_resources.close()
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
