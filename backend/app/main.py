from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from openai import DefaultAsyncHttpxClient

from . import admin, agents, auth, chat, observability, provider_catalog, sessions, signaling
from .db import engine
from .tts_cache import TTSAudioCache


@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.configure_tracing()
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
            await sessions.recover_interrupted_sessions()
            yield
        finally:
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
