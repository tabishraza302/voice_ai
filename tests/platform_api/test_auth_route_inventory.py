"""Route inventory test verifying all endpoints have appropriate authorization guards.

Follows auth-contract.md Task 2 & 4:
- Enumerates all routes mounted on the platform control plane.
- Verifies that only the explicit public/bootstrap/health allowlist can lack a full user/admin guard.
- Asserts that all administrative routes (/api/v1/admin/*) require require_admin or require_step_up.
"""

from __future__ import annotations
import base64
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute

from platform_api.app import create_app
from platform_api.auth.dependencies import (
    require_admin,
    require_csrf,
    require_step_up,
    require_user,
)
from platform_api.config import PlatformSettings
# Explicit allowlist of routes usable without full authenticated user session
EXPLICIT_PUBLIC_ALLOWLIST = {
    "/health/live",
    "/health/ready",
    "/catalog",
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/api/v1/catalog",
    "/api/v1/auth/session",
    "/api/v1/auth/register",
    "/api/v1/auth/verify-email",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/password/reset-request",
    "/api/v1/auth/password/reset-confirm",
}


def _get_route_dependencies(route: APIRoute) -> set:
    """Collect all direct and nested dependencies of an APIRoute."""
    deps = set()

    def _collect(dependant):
        for sub in dependant.dependencies:
            deps.add(sub.call)
            _collect(sub)

    _collect(route.dependant)
    return deps


def test_platform_route_inventory_guards():
    """All non-allowlisted platform routes must enforce require_user or require_admin."""
    settings = PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_test",
        auth_csrf_key=base64.urlsafe_b64encode(b"C" * 32).decode("ascii"),
        auth_rate_key=base64.urlsafe_b64encode(b"R" * 32).decode("ascii"),
        auth_mfa_key=Fernet.generate_key().decode("ascii"),
    )
    app = create_app(settings)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue

        path = route.path
        if path in EXPLICIT_PUBLIC_ALLOWLIST:
            continue

        deps = _get_route_dependencies(route)

        # Non-allowlisted routes MUST enforce require_user, require_admin, or require_step_up
        has_auth_guard = any(
            guard in deps
            for guard in (require_user, require_admin, require_step_up)
        )
        assert (
            has_auth_guard
        ), f"Route '{path}' {route.methods} lacks full user/admin authentication guard"

        # Admin routes MUST enforce require_admin or require_step_up
        if path.startswith("/api/v1/admin"):
            has_admin_guard = any(
                guard in deps for guard in (require_admin, require_step_up)
            )
            assert (
                has_admin_guard
            ), f"Admin route '{path}' lacks require_admin or require_step_up guard"
