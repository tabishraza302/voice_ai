"""Health and readiness check endpoints.

Follows master spec III.2-3 and auth contract:
- Liveness: returns status without calling providers or databases.
- Readiness: verifies expected schema from app.state.engine, suppresses exception secrets,
  fails closed absent an enrolled active admin with active MFA, and verifies dependencies
  required by enabled flags. Missing dependencies return 503 with Retry-After header.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from platform_api.catalog import ModelCatalog, load_catalog
from platform_api.config import PlatformSettings, get_settings
from platform_api.db.schema import check_schema_readiness
from platform_api.models.auth import AuthMfa, User

router = APIRouter(tags=["health"])


async def check_enrolled_admin_exists(
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Check that at least one active administrator with enrolled MFA exists."""
    try:
        async with session_factory() as session:
            stmt = (
                select(User.id)
                .join(AuthMfa, AuthMfa.user_id == User.id)
                .where(
                    User.role == "admin",
                    User.status == "active",
                    AuthMfa.enabled_at.is_not(None),
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
            "error": "No enrolled administrator with active MFA",
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
async def get_catalog(request: Request) -> ModelCatalog:
    """Fake-safe model catalog loaded from JSON without GPU dependencies."""
    settings: PlatformSettings = getattr(request.app.state, "settings", None) or get_settings()
    return load_catalog(settings.catalog_path)
