"""Production session admission routes for the loopback voice runner."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from bot.config import Settings
from bot.runtime.session_task import SessionAdmissionError, SessionTaskManager
from bot.runtime.transport_auth import RuntimeConfigurationError
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
    adapter_report_hash = getattr(app.state, "installed_adapter_report_hash", None)
    try:
        manager = SessionTaskManager.from_settings(
            settings,
            event_store=event_store,
            usage_meter_factory=usage_meter_factory,
            installed_adapter_report_hash=adapter_report_hash,
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
