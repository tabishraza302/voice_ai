"""Production session admission routes for the loopback voice runner."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from bot.config import Settings
from bot.runtime.flow_storage import (
    DurablePostgresFlowStore,
    PostgresVerifiedPlaybackUsageMeter,
)
from bot.runtime.session_task import SessionAdmissionError, SessionTaskManager
from bot.runtime.transport_auth import RuntimeConfigurationError
from platform_api.db.session import create_engine as create_platform_engine
from platform_api.sessions.schemas import (
    RuntimeRegistrationRequest,
    RuntimeReleaseAcknowledgement,
    RuntimeReleaseRequest,
    RuntimeReservation,
)

logger = logging.getLogger(__name__)
_default_manager: SessionTaskManager | None = None


def mount_saas_runtime(app, settings: Settings) -> None:
    """Install internal registration/release routes without changing local startup."""
    global _default_manager
    if getattr(app.state, "session_runtime_mounted", False):
        return
    app.state.session_runtime_mounted = True
    event_store = getattr(app.state, "session_flow_event_store", None)
    usage_meter_factory = getattr(app.state, "session_usage_meter_factory", None)
    flow_store_engine = None
    if (
        event_store is None
        and usage_meter_factory is None
        and settings.platform_database_url is not None
    ):
        try:
            flow_store_engine = create_platform_engine(
                settings.platform_database_url,
                schema=settings.platform_postgres_schema,
            )
            event_store = DurablePostgresFlowStore(
                flow_store_engine,
                schema=settings.platform_postgres_schema,
            )

            def usage_meter_factory(session_id, verifier):
                return PostgresVerifiedPlaybackUsageMeter(
                    event_store,
                    session_id,
                    verifier,
                )

            async def dispose_flow_store_engine() -> None:
                await flow_store_engine.dispose()

            app.add_event_handler("shutdown", dispose_flow_store_engine)
        except Exception:
            event_store = None
            usage_meter_factory = None
            if flow_store_engine is not None:
                # Engine construction can fail before it is registered for shutdown.
                flow_store_engine.sync_engine.dispose()
            logger.warning(
                "Platform flow storage could not be configured; managed admission will fail closed"
            )
            flow_store_engine = None

    app.state.session_flow_event_store = event_store
    app.state.session_usage_meter_factory = usage_meter_factory
    managed_runtime_configured = _managed_runtime_is_configured(settings)
    adapter_report = (
        _load_verified_adapter_report(app)
        if managed_runtime_configured
        else None
    )
    if managed_runtime_configured and adapter_report is None:
        logger.warning(
            "Platform adapter qualification is unavailable; managed admission will fail closed"
        )
        manager = None
    else:
        try:
            manager = SessionTaskManager.from_settings(
                settings,
                event_store=event_store,
                usage_meter_factory=usage_meter_factory,
                installed_adapter_report=adapter_report,
            )
        except RuntimeConfigurationError as exc:
            logger.info("Platform session bridge is not configured: %s", exc)
            manager = None
    app.state.voice_session_manager = manager
    _default_manager = manager

    router = APIRouter(prefix="/internal/runtime/sessions")

    @router.post("/{session_id}/register", response_model=RuntimeReservation)
    async def register_session(
        session_id: UUID,
        body: RuntimeRegistrationRequest,
        request: Request,
    ) -> RuntimeReservation:
        current = _require_manager(request, request.headers.get("Authorization"))
        await _require_durable_flow_store(request)
        if body.session_id != session_id:
            raise HTTPException(status_code=400, detail="Registration session ID does not match its path")
        try:
            return await current.register(body)
        except SessionAdmissionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.post("/{session_id}/release", response_model=RuntimeReleaseAcknowledgement)
    async def release_session(
        session_id: UUID,
        body: RuntimeReleaseRequest,
        request: Request,
    ) -> RuntimeReleaseAcknowledgement:
        current = _require_manager(request, request.headers.get("Authorization"))
        await _require_durable_flow_store(request)
        if body.session_id != session_id:
            raise HTTPException(status_code=400, detail="Release session ID does not match its path")
        try:
            return await current.release_registration(body)
        except SessionAdmissionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    app.include_router(router)


def get_session_manager(app=None) -> SessionTaskManager | None:
    """Return only the manager mounted for this runner process."""
    if app is not None:
        return getattr(app.state, "voice_session_manager", None)
    try:
        from pipecat.runner.run import app as runner_app
    except Exception:
        return _default_manager
    return getattr(runner_app.state, "voice_session_manager", _default_manager)


def _require_manager(request: Request, authorization: str | None) -> SessionTaskManager:
    manager = get_session_manager(request.app)
    if manager is None:
        raise HTTPException(status_code=503, detail="Platform session runtime is unavailable")
    if not manager.authenticate_internal(authorization, request.headers.get("X-Runner-ID")):
        raise HTTPException(status_code=401, detail="Runner authentication failed")
    return manager


__all__ = ["get_session_manager", "mount_saas_runtime"]


async def _require_durable_flow_store(request: Request) -> None:
    store = getattr(request.app.state, "session_flow_event_store", None)
    ensure_ready = getattr(store, "ensure_ready", None)
    if not callable(ensure_ready):
        raise HTTPException(
            status_code=503,
            detail="Durable PostgreSQL flow storage is unavailable",
        )
    try:
        await ensure_ready()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Durable PostgreSQL flow storage is unavailable",
        ) from None


def _managed_runtime_is_configured(settings: Settings) -> bool:
    return any(
        getattr(settings, name, None)
        for name in (
            "platform_api_base_url",
            "platform_session_runner_id",
            "platform_runtime_service_token",
            "platform_web_origin",
        )
    )


def _load_verified_adapter_report(app):
    """Load and validate the operator-pinned report used by managed admission."""
    try:
        from platform_api.config import get_settings
        from platform_api.qualification import (
            has_qualified_voice_playback,
            is_trusted_deployment_report,
            load_deployment_capability_report,
        )

        deployment_settings = get_settings()
        report = load_deployment_capability_report(
            deployment_settings.adapter_capability_report_path,
            deployment_settings.adapter_capability_report_hash,
        )
        if (
            not is_trusted_deployment_report(report, deployment_settings)
            or not has_qualified_voice_playback(report)
        ):
            return None
        app.state.adapter_capability_report = report
        return report
    except Exception as exc:
        logger.warning(
            "Platform adapter qualification could not be independently verified (%s)",
            type(exc).__name__,
        )
        return None
