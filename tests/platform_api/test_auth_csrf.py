"""Contract tests for Origin validation, CSRF verification, and body limits.

Follows auth-contract.md Task 2:
- Missing, null, attacker suffix domain, or wrong port Origin cannot mutate (403 ORIGIN_FORBIDDEN).
- Missing or wrong X-CSRF-Token cannot mutate (403 CSRF_INVALID).
- Non-JSON content types rejected on mutations (415 UNSUPPORTED_MEDIA_TYPE).
- Request bodies exceeding 8 KiB on auth routes rejected (413 REQUEST_TOO_LARGE).
- Preflights (OPTIONS) work only for configured web origin with specific headers.
- Production cookie uses __Host- prefix, Path=/, Secure, HttpOnly, SameSite=Lax.
- GET links with URL fragments never consume tokens.
"""

from __future__ import annotations

import httpx
import pytest

from platform_api.agents.config import DEFAULT_AGENT_CONFIG_MAX_BYTES
from platform_api.config import PlatformSettings
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_origin_header_validation_on_mutations(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
):
    """Mutations without exact matching Origin header are rejected with 403 ORIGIN_FORBIDDEN."""
    # 1. Missing Origin header
    res1 = await app_client.post(
        "/api/v1/auth/logout",
        headers={"Content-Type": "application/json", "X-CSRF-Token": "some-token"},
    )
    assert res1.status_code == 403
    assert res1.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    # 2. Origin: null (e.g. sandboxed iframe or file URL)
    res2 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "null",
            "Content-Type": "application/json",
            "X-CSRF-Token": "some-token",
        },
    )
    assert res2.status_code == 403
    assert res2.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    # 3. Attacker suffix domain
    res3 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "http://127.0.0.1:3001.attacker.com",
            "Content-Type": "application/json",
            "X-CSRF-Token": "some-token",
        },
    )
    assert res3.status_code == 403
    assert res3.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    # 4. Wrong port
    res4 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "http://127.0.0.1:3002",
            "Content-Type": "application/json",
            "X-CSRF-Token": "some-token",
        },
    )
    assert res4.status_code == 403
    assert res4.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    # 5. Trailing slash is not an exact match
    res5 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": test_platform_settings.web_origin + "/",
            "Content-Type": "application/json",
            "X-CSRF-Token": "some-token",
        },
    )
    assert res5.status_code == 403
    assert res5.json()["error"]["code"] == "ORIGIN_FORBIDDEN"
    # Untrusted origin must never be reflected in error message
    assert "attacker" not in res3.json()["error"]["message"]
    assert test_platform_settings.web_origin not in res5.json()["error"]["message"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_csrf_token_validation_on_mutations(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
):
    """Mutations with missing or invalid CSRF token are rejected with 403 CSRF_INVALID."""
    # First bootstrap an anonymous session to get cookie and valid token
    bootstrap_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    assert bootstrap_res.status_code == 200
    valid_csrf = bootstrap_res.json()["csrf_token"]
    cookie_header = bootstrap_res.headers.get("set-cookie")

    # 1. Valid origin, but missing X-CSRF-Token
    res1 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": test_platform_settings.web_origin,
            "Content-Type": "application/json",
            "Cookie": cookie_header,
        },
    )
    assert res1.status_code == 403
    assert res1.json()["error"]["code"] == "CSRF_INVALID"

    # 2. Valid origin, but tampered/wrong X-CSRF-Token
    res2 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": test_platform_settings.web_origin,
            "Content-Type": "application/json",
            "X-CSRF-Token": "wrong-csrf-token-value",
            "Cookie": cookie_header,
        },
    )
    assert res2.status_code == 403
    assert res2.json()["error"]["code"] == "CSRF_INVALID"

    # 3. Valid origin and valid CSRF token succeeds
    res3 = await app_client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": test_platform_settings.web_origin,
            "Content-Type": "application/json",
            "X-CSRF-Token": valid_csrf,
            "Cookie": cookie_header,
        },
    )
    assert res3.status_code == 204


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_content_type_enforcement(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
):
    """Mutations without application/json Content-Type return 415 UNSUPPORTED_MEDIA_TYPE."""
    bootstrap_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    valid_csrf = bootstrap_res.json()["csrf_token"]
    cookie_header = bootstrap_res.headers.get("set-cookie")

    # text/plain Content-Type
    res = await app_client.post(
        "/api/v1/auth/logout",
        content=b"{}",
        headers={
            "Origin": test_platform_settings.web_origin,
            "Content-Type": "text/plain",
            "X-CSRF-Token": valid_csrf,
            "Cookie": cookie_header,
        },
    )
    assert res.status_code == 415
    assert res.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_delete_requires_json_content_type(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """DELETE mutations without application/json Content-Type return 415 UNSUPPORTED_MEDIA_TYPE."""
    from platform_api.auth.passwords import get_password_hasher
    from platform_api.auth.sessions import create_authenticated_session
    from platform_api.models.auth import User
    from datetime import datetime, timezone
    import uuid

    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("password-for-delete-ct-123")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="delete-ct@example.com",
            email_key="delete-ct@example.com",
            display_name="Delete Tester",
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        _, secret = await create_authenticated_session(
            session, test_platform_settings, user=user, client_ip="127.0.0.1", user_agent="Device", now=now
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header},
    )
    valid_csrf = boot_res.json()["csrf_token"]

    fake_session_id = str(uuid.uuid4())
    # 1. DELETE without Content-Type returns 415
    res1 = await app_client.delete(
        f"/api/v1/auth/sessions/{fake_session_id}",
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": valid_csrf,
            "Cookie": cookie_header,
        },
    )
    assert res1.status_code == 415
    assert res1.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    # 2. DELETE with Content-Type: application/json passes media check and reaches route logic
    res2 = await app_client.delete(
        f"/api/v1/auth/sessions/{fake_session_id}",
        headers={
            "Origin": test_platform_settings.web_origin,
            "Content-Type": "application/json",
            "X-CSRF-Token": valid_csrf,
            "Cookie": cookie_header,
        },
    )
    assert res2.status_code == 404
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_auth_route_body_limit(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
):
    """Auth route requests exceeding 8 KiB are rejected with 413 REQUEST_TOO_LARGE before parsing."""
    bootstrap_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    valid_csrf = bootstrap_res.json()["csrf_token"]
    cookie_header = bootstrap_res.headers.get("set-cookie")

    # Body larger than 8192 bytes
    large_payload = b'{"email":"' + (b"a" * 8200) + b'@example.com"}'
    res = await app_client.post(
        "/api/v1/auth/register",
        content=large_payload,
        headers={
            "Origin": test_platform_settings.web_origin,
            "Content-Type": "application/json",
            "X-CSRF-Token": valid_csrf,
            "Cookie": cookie_header,
        },
    )
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.parametrize(
    ("method", "endpoint"),
    (
        ("POST", "/api/v1/agents"),
        ("PATCH", "/api/v1/agents/00000000-0000-0000-0000-000000000001"),
        ("PATCH", "/api/v1/agents/00000000-0000-0000-0000-000000000001/draft"),
        ("POST", "/api/v1/agents/00000000-0000-0000-0000-000000000001/validate"),
        ("POST", "/api/v1/agents/00000000-0000-0000-0000-000000000001/publish"),
        ("POST", "/api/v1/agents/00000000-0000-0000-0000-000000000001/restore-draft"),
        ("POST", "/api/v1/agents/00000000-0000-0000-0000-000000000001/duplicate"),
    ),
)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_request_body_limit_checks_stream_before_parse(
    app_client: httpx.AsyncClient,
    method: str,
    endpoint: str,
):
    """All body-bearing agent requests are bounded before JSON parsing."""
    request_limit = DEFAULT_AGENT_CONFIG_MAX_BYTES + 1024

    async def oversized_body():
        remaining = request_limit + 1
        while remaining:
            chunk_size = min(16_384, remaining)
            yield b"x" * chunk_size
            remaining -= chunk_size

    response = await app_client.request(
        method,
        endpoint,
        content=oversized_body(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_get_request_never_consumes_token(
    app_client: httpx.AsyncClient,
):
    """GET requests with URL fragment or token query parameters cannot trigger mutation."""
    # Attempting to GET /api/v1/auth/verify-email returns 405 Method Not Allowed
    res = await app_client.get("/api/v1/auth/verify-email?token=some-token")
    assert res.status_code == 405
