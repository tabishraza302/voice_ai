from contextlib import asynccontextmanager

from fastapi import FastAPI
from openai import DefaultAsyncHttpxClient

from . import admin, agents, auth, chat, observability, provider_catalog, sessions, signaling
from .db import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.configure_tracing()
    async with DefaultAsyncHttpxClient() as openai_http_client:
        app.state.openai_http_client = openai_http_client
        try:
            await sessions.recover_interrupted_sessions()
            yield
        finally:
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
