from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import admin, agents, auth, observability, provider_catalog, sessions, signaling, steps


@asynccontextmanager
async def lifespan(_: FastAPI):
    observability.configure_tracing()
    await sessions.recover_interrupted_sessions()
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(steps.router)
app.include_router(admin.router)
app.include_router(provider_catalog.router)
app.include_router(sessions.router)
app.include_router(sessions.public_router)
app.include_router(signaling.router)
