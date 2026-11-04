from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
import httpx
import pytest
from platform_api.app import _request_path_without_root_path
from cryptography.fernet import Fernet
from sqlalchemy import select

import bot.provider_registry as speech_registry
from bot.provider_registry import SecretFieldMetadata
from platform_api.admin.provider_routes import decrypt_provider_secret, encrypt_provider_secret
from platform_api.admin.model_providers import model_provider_metadata
from platform_api.admin.ollama_model_routes import (
    list_model_entries,
    list_qualified_enabled_model_entries,
)
from platform_api.auth.errors import DependencyUnavailableError
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuditLog, AuthMfa, User
from platform_api.models.provider_secret import ProviderSecret
from tests.support.billing import create_test_user
from tests.support.postgres import FreshPostgres


async def _admin_headers(
    initialized_pg: FreshPostgres,
    settings: PlatformSettings,
    client: httpx.AsyncClient,
    *,
    role: str = "admin",
    step_up: bool = True,
) -> tuple[User, dict[str, str]]:
    now = datetime.now(timezone.utc)
    async with initialized_pg.sessions() as db:
        user = await create_test_user(
            db,
            email=f"provider-{uuid.uuid4().hex}@example.com",
            role=role,
        )
        if role == "admin":
            db.add(
                AuthMfa(
                    user_id=user.id,
                    secret_ciphertext="test-only-ciphertext",
                    enabled_at=now,
                    last_accepted_step=1,
                )
            )
            await db.flush()
        auth_session, secret = await create_authenticated_session(
            db,
            settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="Provider admin regression test",
            now=now,
            mfa_verified=role == "admin",
        )
        if role == "admin":
            auth_session.step_up_at = (
                now
                if step_up
                else now - timedelta(seconds=settings.auth_step_up_seconds + 1)
            )
        await db.commit()

    cookie_header = f"{settings.session_cookie_name}={secret}"
    bootstrap = await client.get(
        "/api/v1/auth/session",
        headers={"Origin": settings.web_origin, "Cookie": cookie_header},
    )
    assert bootstrap.status_code == 200
    return user, {
        "Origin": settings.web_origin,
        "Cookie": cookie_header,
        "X-CSRF-Token": bootstrap.json()["csrf_token"],
        "Content-Type": "application/json",
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_provider_list_requires_admin_mfa_and_fresh_step_up(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    _, user_headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client, role="user"
    )
    user_response = await app_client.get(
        "/api/v1/admin/providers", headers=user_headers
    )
    assert user_response.status_code == 403

    _, stale_headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client, step_up=False
    )
    stale_response = await app_client.get(
        "/api/v1/admin/providers", headers=stale_headers
    )
    assert stale_response.status_code == 403

    _, admin_headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client
    )
    response = await app_client.get("/api/v1/admin/providers", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["model_providers"] == [{"id": "ollama", "name": "Ollama"}]


@pytest.mark.asyncio
async def test_provider_secret_write_body_limit_applies_before_authentication(
    app_client: httpx.AsyncClient,
) -> None:
    response = await app_client.put(
        "/api/v1/admin/providers/tts%3Akokoro/secrets",
        content=b"x" * (8 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.asyncio
async def test_provider_secret_body_limit_applies_under_asgi_root_path(
    app_client: httpx.AsyncClient,
) -> None:
    assert (
        _request_path_without_root_path(
            {
                "path": "/platform/api/v1/admin/providers/tts:kokoro/secrets",
                "root_path": "/platform",
            }
        )
        == "/api/v1/admin/providers/tts:kokoro/secrets"
    )
    assert (
        _request_path_without_root_path(
            {"path": "/platform-else/api/v1/admin/providers", "root_path": "/platform"}
        )
        == "/platform-else/api/v1/admin/providers"
    )
    assert (
        _request_path_without_root_path(
            {
                "path": "/platform//api/v1/admin/providers/tts:kokoro/secrets",
                "root_path": "/platform/",
            }
        )
        == "/api/v1/admin/providers/tts:kokoro/secrets"
    )
    assert (
        _request_path_without_root_path(
            {
                "path": "//api/v1/admin/providers/tts:kokoro/secrets",
                "root_path": "/",
            }
        )
        == "/api/v1/admin/providers/tts:kokoro/secrets"
    )
    mounted_app = FastAPI()
    mounted_app.mount("/platform", app_client._transport.app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mounted_app),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.put(
            "/platform/api/v1/admin/providers/tts%3Akokoro/secrets",
            content=b"x" * (8 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_provider_secret_updates_merge_encrypt_and_audit_names_only(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts_registry = speech_registry.TTS_PROVIDER_REGISTRY
    monkeypatch.setattr(
        speech_registry,
        "TTS_PROVIDER_REGISTRY",
        tuple(
            replace(
                provider,
                secret_fields=(
                    SecretFieldMetadata("API_KEY"),
                    SecretFieldMetadata("REGION"),
                ),
            )
            if provider.id == "kokoro"
            else provider
            for provider in tts_registry
        ),
    )
    user, headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client
    )
    url = "/api/v1/admin/providers/tts%3Akokoro/secrets"

    listed = await app_client.get("/api/v1/admin/providers", headers=headers)
    assert listed.status_code == 200
    provider = next(
        item for item in listed.json()["providers"] if item["id"] == "tts:kokoro"
    )
    assert provider["kind"] == "tts"
    assert provider["secret_fields"] == [
        {"name": "API_KEY", "required": True, "configured": False},
        {"name": "REGION", "required": True, "configured": False},
    ]

    first_secret = "provider-secret-first-value"
    first = await app_client.put(
        url,
        headers=headers,
        json={"secrets": {"API_KEY": first_secret}},
    )
    assert first.status_code == 200
    assert first.json() == {
        "provider_id": "tts:kokoro",
        "configured_secret_fields": ["API_KEY"],
    }
    assert first_secret not in first.text

    second_secret = "provider-secret-second-value"
    second = await app_client.put(
        url,
        headers=headers,
        json={"secrets": {"REGION": second_secret}},
    )
    assert second.status_code == 200
    assert second.json()["configured_secret_fields"] == ["API_KEY", "REGION"]
    assert first_secret not in second.text
    assert second_secret not in second.text

    listed_after = await app_client.get("/api/v1/admin/providers", headers=headers)
    provider_after = next(
        item
        for item in listed_after.json()["providers"]
        if item["id"] == "tts:kokoro"
    )
    assert [field["configured"] for field in provider_after["secret_fields"]] == [
        True,
        True,
    ]

    async with initialized_pg.sessions() as db:
        stored = (
            await db.execute(
                select(ProviderSecret).where(ProviderSecret.provider_id == "tts:kokoro")
            )
        ).scalars().all()
        assert len(stored) == 2
        by_name = {item.field_name: item.secret_ciphertext for item in stored}
        assert first_secret not in by_name["API_KEY"]
        assert second_secret not in by_name["REGION"]
        assert decrypt_provider_secret(
            test_platform_settings, by_name["API_KEY"]
        ) == first_secret
        assert decrypt_provider_secret(
            test_platform_settings, by_name["REGION"]
        ) == second_secret
        audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "provider_credentials_updated",
                    AuditLog.actor_id == user.id,
                )
            )
        ).scalars().all()
        assert len(audits) == 2
        audit_text = json.dumps([audit.change_summary for audit in audits])
        assert "API_KEY" in audit_text and "REGION" in audit_text
        assert first_secret not in audit_text
        assert second_secret not in audit_text

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_provider_secret_write_requires_csrf_and_never_echoes_unknown_values(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts_registry = speech_registry.TTS_PROVIDER_REGISTRY
    monkeypatch.setattr(
        speech_registry,
        "TTS_PROVIDER_REGISTRY",
        tuple(
            replace(
                provider,
                secret_fields=(SecretFieldMetadata("API_KEY"),),
            )
            if provider.id == "kokoro"
            else provider
            for provider in tts_registry
        ),
    )

    _, headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client
    )
    url = "/api/v1/admin/providers/tts%3Akokoro/secrets"
    missing_csrf_headers = {
        "Origin": headers["Origin"],
        "Cookie": headers["Cookie"],
        "Content-Type": "application/json",
    }
    blocked = await app_client.put(
        url,
        headers=missing_csrf_headers,
        json={"secrets": {"API_KEY": "do-not-return-this-secret"}},
    )
    assert blocked.status_code == 403

    invalid_value = "unknown-field-secret-value"
    invalid = await app_client.put(
        url,
        headers=headers,
        json={"secrets": {"UNKNOWN_FIELD": invalid_value}},
    )
    assert invalid.status_code == 422
    assert invalid_value not in invalid.text


@pytest.mark.parametrize("secret", ["secret-value"])
def test_provider_credential_cipher_requires_dedicated_key(
    test_platform_settings: PlatformSettings,
    secret: str,
) -> None:
    settings_without_key = test_platform_settings.model_copy(
        update={"provider_secret_key": None}
    )
    with pytest.raises(DependencyUnavailableError):
        encrypt_provider_secret(settings_without_key, secret)


def test_provider_credential_cipher_is_authenticated_and_round_trips(
    test_platform_settings: PlatformSettings,
) -> None:
    secret = "non-public-provider-key"
    ciphertext = encrypt_provider_secret(test_platform_settings, secret)
    assert secret not in ciphertext
    assert decrypt_provider_secret(test_platform_settings, ciphertext) == secret

    other_key = Fernet.generate_key().decode("ascii")
    wrong_settings = test_platform_settings.model_copy(
        update={"provider_secret_key": other_key}
    )
    with pytest.raises(DependencyUnavailableError):
        decrypt_provider_secret(wrong_settings, ciphertext)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ollama_model_admin_crud_keeps_flags_independent(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    user, headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client
    )
    collection = "/api/v1/admin/providers/ollama/models"
    model_id = "qwen3.5:4b"

    created = await app_client.post(
        collection,
        headers=headers,
        json={"model_id": model_id, "name": "Qwen 3.5 4B"},
    )
    assert created.status_code == 201
    assert created.json() == {
        "provider_id": "ollama",
        "model_id": model_id,
        "name": "Qwen 3.5 4B",
        "enabled": False,
        "qualified": False,
    }

    item_url = f"{collection}/{model_id}"
    attested = await app_client.patch(
        item_url,
        headers=headers,
        json={"qualified": True},
    )
    assert attested.status_code == 200
    assert attested.json()["qualified"] is True
    assert attested.json()["enabled"] is False

    async with initialized_pg.sessions() as db:
        assert [
            entry.model_id for entry in await list_model_entries(db)
        ] == [model_id]
        assert await list_qualified_enabled_model_entries(db) == []
    enabled = await app_client.patch(
        item_url,
        headers=headers,
        json={"enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["qualified"] is True
    async with initialized_pg.sessions() as db:
        assert [
            entry.model_id
            for entry in await list_qualified_enabled_model_entries(db)
        ] == [model_id]

    unqualified = await app_client.patch(
        item_url,
        headers=headers,
        json={"qualified": False},
    )
    assert unqualified.status_code == 200
    assert unqualified.json()["enabled"] is True
    assert unqualified.json()["qualified"] is False

    async with initialized_pg.sessions() as db:
        assert await list_qualified_enabled_model_entries(db) == []

    attested_again = await app_client.patch(
        item_url,
        headers=headers,
        json={"qualified": True},
    )
    assert attested_again.status_code == 200
    assert attested_again.json()["enabled"] is True

    renamed = await app_client.patch(
        item_url,
        headers=headers,
        json={"name": "Qwen for voice"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["model_id"] == model_id
    assert renamed.json()["name"] == "Qwen for voice"
    assert renamed.json()["enabled"] is True
    assert renamed.json()["qualified"] is True

    listed = await app_client.get(collection, headers=headers)
    assert listed.status_code == 200
    assert listed.json() == {"models": [renamed.json()]}
    audit_response = await app_client.get(
        "/api/v1/admin/audit-logs",
        params={"target_type": "ollama_model"},
        headers=headers,
    )
    assert audit_response.status_code == 200
    assert any(
        row["model_change"]
        and row["model_change"]["provider_id"] == "ollama"
        and row["model_change"]["model_id"] == model_id
        and row["model_change"]["after"]["qualified"] is True
        for row in audit_response.json()["items"]
    )

    async with initialized_pg.sessions() as db:
        audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == user.id,
                    AuditLog.target_type == "ollama_model",
                )
            )
        ).scalars().all()
        assert {audit.action for audit in audits} == {
            "ollama_model_created",
            "ollama_model_updated",
        }

    deleted = await app_client.delete(item_url, headers=headers)
    assert deleted.status_code == 204
    after_delete = await app_client.get(collection, headers=headers)
    assert after_delete.status_code == 200
    assert after_delete.json() == {"models": []}
    async with initialized_pg.sessions() as db:
        delete_audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == user.id,
                    AuditLog.action == "ollama_model_deleted",
                )
            )
        ).scalars().all()
        assert len(delete_audits) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ollama_model_admin_rejects_duplicate_missing_csrf_and_unknown_provider(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    _, headers = await _admin_headers(
        initialized_pg, test_platform_settings, app_client
    )
    collection = "/api/v1/admin/providers/ollama/models"
    payload = {
        "model_id": "llama3.2:3b",
        "name": "Llama 3.2 3B",
        "enabled": False,
        "qualified": False,
    }

    missing_csrf_headers = {
        "Origin": headers["Origin"],
        "Cookie": headers["Cookie"],
        "Content-Type": "application/json",
    }
    blocked = await app_client.post(
        collection,
        headers=missing_csrf_headers,
        json=payload,
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "CSRF_INVALID"

    created = await app_client.post(collection, headers=headers, json=payload)
    assert created.status_code == 201

    duplicate = await app_client.post(collection, headers=headers, json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CONFLICT"

    unknown_provider = await app_client.get(
        "/api/v1/admin/providers/openai-compatible/models",
        headers=headers,
    )
    assert unknown_provider.status_code == 404
    assert unknown_provider.json()["error"]["code"] == "NOT_FOUND"

    missing_model = await app_client.patch(
        f"{collection}/missing:latest",
        headers=headers,
        json={"qualified": True},
    )
    assert missing_model.status_code == 404
    assert missing_model.json()["error"]["code"] == "NOT_FOUND"

@pytest.mark.asyncio
async def test_ollama_model_write_body_limit_applies_before_authentication(
    app_client: httpx.AsyncClient,
) -> None:
    response = await app_client.post(
        "/api/v1/admin/providers/ollama/models",
        content=b" " * (8192 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ollama_model_admin_requires_admin_and_fresh_step_up(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    collection = "/api/v1/admin/providers/ollama/models"
    unauthenticated = await app_client.get(collection)
    assert unauthenticated.status_code == 401

    _, user_headers = await _admin_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        role="user",
    )
    non_admin = await app_client.get(collection, headers=user_headers)
    assert non_admin.status_code == 403

    _, stale_headers = await _admin_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        step_up=False,
    )
    stale_step_up = await app_client.get(collection, headers=stale_headers)
    assert stale_step_up.status_code == 403
    assert stale_step_up.json()["error"]["code"] == "STEP_UP_REQUIRED"


def test_model_provider_metadata_only_registers_ollama() -> None:
    provider = model_provider_metadata("ollama")
    assert provider is not None
    assert provider.id == "ollama"
    assert provider.name == "Ollama"
    assert (
        provider.runtime_adapter
        == "pipecat.services.ollama.llm.OLLamaLLMService"
    )
    assert model_provider_metadata("openai-compatible") is None
