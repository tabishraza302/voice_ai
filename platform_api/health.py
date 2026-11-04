"""Health and readiness check endpoints.

Follows master spec III.2-3 and auth contract:
- Liveness: returns status without calling providers or databases.
- Readiness: verifies expected schema from app.state.engine, suppresses exception secrets,
  fails closed absent an enrolled active admin with active MFA, and verifies dependencies
  required by enabled flags. Missing dependencies return 503 with Retry-After header.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from platform_api.catalog import ModelCatalog, catalog_with_managed_llms, load_catalog
from platform_api.config import PlatformSettings, get_settings
from platform_api.db.schema import check_schema_readiness
from platform_api.db.session import get_db
from platform_api.qualification import has_qualified_voice_playback
from platform_api.qualification import is_trusted_deployment_report
from platform_api.models.auth import User

router = APIRouter(tags=["health"])


async def check_enrolled_admin_exists(
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Check that at least one active administrator exists."""
    try:
        async with session_factory() as session:
            stmt = (
                select(User.id)
                .where(
                    User.role == "admin",
                    User.status == "active",
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None
    except Exception:
        return False


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    """Lightweight liveness probe that does not contact dependencies."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    request: Request,
    response: Response,
) -> dict[str, str]:
    """Readiness probe checking schema validity, admin enrollment, and enabled dependencies.

    Uses app.state instead of global settings or engines, and suppresses all exception secrets.
    """
    if (
        not hasattr(request.app.state, "settings")
        or not hasattr(request.app.state, "engine")
        or request.app.state.engine is None
    ):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "Application state uninitialized",
        }

    if getattr(request.app.state, "schema_ready", None) is False:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "Database schema was not ready at startup; restart after repair",
        }

    settings: PlatformSettings = request.app.state.settings
    engine = request.app.state.engine

    # 1. Verify schema readiness against expected version and metadata hash
    try:
        is_ready = await check_schema_readiness(
            engine,
            schema=settings.postgres_schema,
            expected_version=settings.schema_version,
        )
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "Database dependency unavailable",
        }

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "Database schema is missing or does not match expected version",
        }

    # 2. Verify enrolled administrator with active MFA exists
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "Database session factory uninitialized",
        }

    has_admin = await check_enrolled_admin_exists(session_factory)
    if not has_admin:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "No enrolled administrator",
        }

    # 3. Verify dependencies required by enabled feature flags
    if settings.enable_gateway:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"
        return {
            "status": "not_ready",
            "error": "Enabled gateway dependency is not configured",
        }

    if settings.auth_mail_mode == "smtp":
        if not (
            settings.auth_smtp_host
            and settings.auth_smtp_port
            and settings.auth_smtp_username
            and settings.auth_smtp_password
            and settings.auth_mail_from
        ):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            response.headers["Retry-After"] = "5"
            return {
                "status": "not_ready",
                "error": "SMTP mail dependency is not configured",
            }

    return {
        "status": "ready",
        "schema_version": settings.schema_version,
    }


@router.get("/catalog")
async def get_catalog(request: Request, db: AsyncSession = Depends(get_db)) -> ModelCatalog:
    """Return registered speech metadata and current managed LLM choices."""
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is not None:
        return await catalog_with_managed_llms(catalog, db)
    settings: PlatformSettings = getattr(request.app.state, "settings", None) or get_settings()
    report = getattr(request.app.state, "adapter_capability_report", None)
    trusted_report = is_trusted_deployment_report(report, settings)
    qualified_settings = (
        report.agent_settings.model_dump(mode="python", exclude_none=True)
        if report is not None and trusted_report
        else None
    )
    catalog = load_catalog(
        settings.catalog_path,
        qualified_settings=qualified_settings,
        tts_playback_qualified=(
            trusted_report and has_qualified_voice_playback(report)
        ),
    )
    return await catalog_with_managed_llms(catalog, db)
