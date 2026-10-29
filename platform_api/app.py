"""FastAPI application factory for the platform control plane.

Owns DB pool lifespan, route mounting, security middleware, and error mapping.
Never imports GPU models or bot.db.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
import asyncio
import uuid

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from platform_api.accounts.admin_routes import router as admin_router
from platform_api.accounts.routes import router as account_router
from platform_api.agents.config import DEFAULT_AGENT_CONFIG_MAX_BYTES
from platform_api.agents.routes import router as agents_router
from platform_api.generation.routes import router as generation_router
from platform_api.generation.service import generation_worker_loop
from platform_api.model_client import OpenAICompatibleModelClient
from platform_api.auth.errors import (
    PlatformError,
    RequestTooLargeError,
    platform_error_response,
)
from platform_api.auth.routes import router as auth_router
from platform_api.billing.routes import (
    admin_router as admin_billing_router,
    internal_router as internal_billing_router,
    router as billing_router,
)
from platform_api.catalog import load_catalog
from platform_api.config import PlatformSettings, get_settings
from platform_api.testing.routes import router as chat_test_router
from platform_api.db.base import Base
from platform_api.db.schema import check_schema_readiness
from platform_api.db.session import create_engine, get_session_factory
from platform_api.health import router as health_router
from platform_api.qualification import (
    DeploymentIdentity,
    load_deployment_capability_report,
)
import platform_api.models  # Register complete platform models onto Base.metadata

AUTH_ROUTE_BODY_LIMIT = 8192  # 8 KiB
AGENT_REQUEST_BODY_LIMIT = DEFAULT_AGENT_CONFIG_MAX_BYTES + 1024


class RequestContextMiddleware:
    """Enforces request IDs and bounded request bodies before FastAPI parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        req_id_header = request.headers.get("x-request-id")
        try:
            req_id = uuid.UUID(req_id_header) if req_id_header else uuid.uuid4()
        except ValueError:
            req_id = uuid.uuid4()
        scope.setdefault("state", {})["request_id"] = req_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", str(req_id).encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        path = request.url.path
        is_agent_body_request = (
            (path == "/api/v1/agents" or path.startswith("/api/v1/agents/"))
            and request.method in {"POST", "PATCH", "PUT", "DELETE"}
        )
        if path.startswith("/api/v1/auth"):
            body_limit = AUTH_ROUTE_BODY_LIMIT
            too_large_message = "Request body exceeds 8 KiB limit"
        elif is_agent_body_request:
            body_limit = AGENT_REQUEST_BODY_LIMIT
            too_large_message = (
                "Agent request body exceeds "
                f"{AGENT_REQUEST_BODY_LIMIT} byte limit"
            )
        else:
            body_limit = None
            too_large_message = ""
        original_receive = receive
        if body_limit is not None:
            settings = getattr(request.app.state, "settings", None) or get_settings()
            content_length = request.headers.get("content-length")
            try:
                declared_length = int(content_length) if content_length else None
            except ValueError:
                declared_length = None

            if declared_length is not None and declared_length > body_limit:
                response = platform_error_response(
                    RequestTooLargeError(too_large_message),
                    req_id,
                    settings,
                )
                await response(scope, original_receive, send_with_request_id)
                return

            request_messages: list[Message] = []
            received_bytes = 0
            while True:
                message = await original_receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                received_bytes += len(message.get("body", b""))
                if received_bytes > body_limit:
                    response = platform_error_response(
                        RequestTooLargeError(too_large_message),
                        req_id,
                        settings,
                    )
                    await response(scope, original_receive, send_with_request_id)
                    return
                request_messages.append(message)
                if not message.get("more_body", False):
                    break

            request_message_index = 0

            async def replay_request_body() -> Message:
                nonlocal request_message_index
                if request_message_index < len(request_messages):
                    message = request_messages[request_message_index]
                    request_message_index += 1
                    return message
                return await original_receive()

            receive = replay_request_body

        await self.app(scope, receive, send_with_request_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager owning DB pools, model clients, and workers."""
    settings: PlatformSettings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings
    app.state.deployment_identity = DeploymentIdentity.from_settings(settings)
    app.state.adapter_capability_report = None
    app.state.catalog = None
    app.state.generation_worker_task = None
    app.state.generation_job_tasks = {}

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory
    created_model_client = False
    worker_task: asyncio.Task[None] | None = None

    try:
        app.state.catalog = load_catalog(settings.catalog_path)
        app.state.adapter_capability_report = load_deployment_capability_report(
            settings.adapter_capability_report_path,
            settings.adapter_capability_report_hash,
        )

        try:
            app.state.schema_ready = await check_schema_readiness(
                engine,
                schema=settings.postgres_schema,
                expected_version=settings.schema_version,
            )
        except Exception:
            app.state.schema_ready = False

        if getattr(app.state, "model_client", None) is None and (
            settings.runtime_llm_base_url is not None
            or settings.generation_llm_base_url is not None
        ):
            app.state.model_client = OpenAICompatibleModelClient(settings)
            created_model_client = True

        if (
            settings.enable_generation
            and app.state.schema_ready
            and not getattr(app.state, "disable_generation_worker", False)
        ):
            worker_task = asyncio.create_task(
                generation_worker_loop(app),
                name="platform-generation-worker",
            )
            app.state.generation_worker_task = worker_task

        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        app.state.generation_worker_task = None
        app.state.generation_job_tasks = {}

        if created_model_client:
            await app.state.model_client.aclose()
            app.state.model_client = None

        app.state.adapter_capability_report = None
        app.state.deployment_identity = None
        app.state.catalog = None

        if hasattr(app.state, "engine") and app.state.engine is not None:
            await app.state.engine.dispose()

def create_app(settings: PlatformSettings | None = None) -> FastAPI:
    """Create and configure FastAPI platform application instance."""
    resolved_settings = settings or get_settings()

    app = FastAPI(
        title="Voice AI Platform Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.adapter_capability_report = None
    app.state.model_client = None
    app.state.generation_worker_task = None
    app.state.generation_job_tasks = {}
    app.state.deployment_identity = None
    app.state.catalog = None

    # Request ID and body limit middleware
    app.add_middleware(RequestContextMiddleware)

    # Allowed Host headers
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved_settings.allowed_hosts,
    )

    # CORS middleware for development web origin
    if resolved_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "X-CSRF-Token",
                "Idempotency-Key",
                "X-Request-ID",
            ],
        )

    # Exception Handlers -------------------------------------------------------

    @app.exception_handler(PlatformError)
    async def handle_platform_error(request: Request, exc: PlatformError):
        req_id = getattr(request.state, "request_id", uuid.uuid4())
        settings = getattr(request.app.state, "settings", None) or get_settings()
        return platform_error_response(exc, req_id, settings)
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        req_id = getattr(request.state, "request_id", uuid.uuid4())
        fields = []
        for err in exc.errors():
            loc = err.get("loc", ())
            field_name = str(loc[-1]) if loc else "body"
            # Never reflect rejected input values or secrets
            fields.append({"field": field_name, "code": err.get("type", "invalid")})

        err_obj = PlatformError(
            message="Validation error",
            fields=fields,
        )
        err_obj.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        err_obj.code = "VALIDATION"
        return platform_error_response(err_obj, req_id)

    @app.exception_handler(Exception)
    async def handle_generic_exception(request: Request, exc: Exception):
        req_id = getattr(request.state, "request_id", uuid.uuid4())
        err_obj = PlatformError(
            message="An unexpected internal error occurred",
            fields=[],
        )
        err_obj.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        err_obj.code = "INTERNAL_ERROR"
        return platform_error_response(err_obj, req_id)

    # Mount platform routers under /api/v1
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(auth_router)
    api_v1.include_router(account_router)
    api_v1.include_router(admin_router)
    api_v1.include_router(admin_billing_router)
    api_v1.include_router(billing_router)
    api_v1.include_router(health_router)
    api_v1.include_router(agents_router)
    api_v1.include_router(generation_router)
    api_v1.include_router(chat_test_router)

    app.include_router(api_v1)
    # Mount internal routes under /internal
    internal = APIRouter(prefix="/internal")
    internal.include_router(internal_billing_router)
    app.include_router(internal)

    # Also include health_router at root for /health/live, /health/ready, /catalog
    app.include_router(health_router)
    return app
