"""Admin endpoints for encrypted credentials of registered speech adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
import uuid

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.provider_registry import SpeechProviderMetadata, provider_for, provider_registry
from platform_api.audit import record_audit
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_csrf,
    require_step_up,
)
from platform_api.auth.errors import DependencyUnavailableError, NotFoundError, ValidationError
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.config import PlatformSettings
from platform_api.db.session import get_db
from platform_api.admin.ollama_model_routes import router as model_entries_router
from platform_api.admin.model_providers import MODEL_PROVIDER_REGISTRY
from platform_api.models.provider_secret import ProviderSecret


router = APIRouter(prefix="/admin/providers", tags=["admin-providers"])
router.include_router(model_entries_router)
MAX_SECRET_LENGTH = 4096


class ProviderSecretFieldView(BaseModel):
    name: str
    required: bool
    configured: bool


class ProviderView(BaseModel):
    id: str
    kind: Literal["stt", "tts"]
    name: str
    secret_fields: list[ProviderSecretFieldView]


class ModelProviderView(BaseModel):
    id: str
    name: str


class ProviderListResponse(BaseModel):
    providers: list[ProviderView]
    model_providers: list[ModelProviderView]


class ProviderSecretUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secrets: dict[str, SecretStr] = Field(min_length=1)

    @field_validator("secrets")
    @classmethod
    def secret_values_must_be_nonblank_and_bounded(
        cls, values: dict[str, SecretStr]
    ) -> dict[str, SecretStr]:
        for value in values.values():
            secret = value.get_secret_value()
            if not secret.strip():
                raise ValueError("Secret values must not be blank")
            if len(secret) > MAX_SECRET_LENGTH:
                raise ValueError("Secret value exceeds its maximum length")
        return values


class ProviderSecretUpdateResponse(BaseModel):
    provider_id: str
    configured_secret_fields: list[str]


def _admin_provider_id(kind: str, provider_id: str) -> str:
    return f"{kind}:{provider_id}"


def _registered_providers() -> list[tuple[str, SpeechProviderMetadata]]:
    return [
        (kind, provider)
        for kind in ("stt", "tts")
        for provider in provider_registry(kind)
    ]


def _resolve_provider(admin_provider_id: str) -> tuple[str, SpeechProviderMetadata]:
    kind, separator, runtime_id = admin_provider_id.partition(":")
    if not separator or kind not in {"stt", "tts"} or not runtime_id:
        raise NotFoundError("Registered provider not found")
    provider = provider_for(kind, runtime_id)
    if provider is None:
        raise NotFoundError("Registered provider not found")
    return kind, provider


def encrypt_provider_secret(settings: PlatformSettings, value: str) -> str:
    """Encrypt a provider credential with the dedicated deployment key."""
    key = settings.provider_secret_key
    if key is None:
        raise DependencyUnavailableError(
            "Provider credential encryption is not configured"
        )
    return Fernet(key.get_secret_value().encode("ascii")).encrypt(
        value.encode("utf-8")
    ).decode("ascii")


def decrypt_provider_secret(settings: PlatformSettings, ciphertext: str) -> str:
    """Decrypt a stored credential for its registered runtime adapter only."""
    key = settings.provider_secret_key
    if key is None:
        raise DependencyUnavailableError(
            "Provider credential encryption is not configured"
        )
    try:
        return Fernet(key.get_secret_value().encode("ascii")).decrypt(
            ciphertext.encode("ascii")
        ).decode("utf-8")
    except Exception:
        raise DependencyUnavailableError(
            "Stored provider credential cannot be decrypted"
        ) from None


async def read_provider_secrets(
    db: AsyncSession,
    settings: PlatformSettings,
    provider_id: str,
) -> dict[str, str]:
    """Return decrypted configured values to a runtime adapter, never an API route."""
    _, provider = _resolve_provider(provider_id)
    if not provider.secret_fields:
        return {}
    rows = await db.execute(
        select(ProviderSecret.field_name, ProviderSecret.secret_ciphertext).where(
            ProviderSecret.provider_id == provider_id
        )
    )
    declared = {field.name for field in provider.secret_fields}
    return {
        field_name: decrypt_provider_secret(settings, ciphertext)
        for field_name, ciphertext in rows.all()
        if field_name in declared
    }


@router.get("", response_model=ProviderListResponse)
async def list_registered_providers(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_step_up),
) -> ProviderListResponse:
    """List registered providers and credential presence without returning values."""
    registered = _registered_providers()
    provider_ids = [
        _admin_provider_id(kind, provider.id) for kind, provider in registered
    ]
    result = await db.execute(
        select(ProviderSecret.provider_id, ProviderSecret.field_name).where(
            ProviderSecret.provider_id.in_(provider_ids)
        )
    )
    configured = {(row.provider_id, row.field_name) for row in result.all()}
    return ProviderListResponse(
        providers=[
            ProviderView(
                id=_admin_provider_id(kind, provider.id),
                kind=kind,
                name=provider.name,
                secret_fields=[
                    ProviderSecretFieldView(
                        name=field.name,
                        required=field.required,
                        configured=(
                            _admin_provider_id(kind, provider.id), field.name
                        )
                        in configured,
                    )
                    for field in provider.secret_fields
                ],
            )
            for kind, provider in registered
        ],
        model_providers=[
            ModelProviderView(id=provider.id, name=provider.name)
            for provider in MODEL_PROVIDER_REGISTRY
        ],
    )


@router.put(
    "/{provider_id}/secrets",
    response_model=ProviderSecretUpdateResponse,
)
async def set_provider_secrets(
    provider_id: str,
    payload: ProviderSecretUpdate,
    request: Request,
    principal: Principal = Depends(require_step_up),
    csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> ProviderSecretUpdateResponse:
    """Merge supplied credential fields, encrypt, and audit field names only."""
    kind, provider = _resolve_provider(provider_id)
    declared = {field.name: field for field in provider.secret_fields}
    if not declared:
        raise ValidationError("This registered provider does not require credentials")
    unknown = sorted(set(payload.secrets) - set(declared))
    if unknown:
        raise ValidationError(
            "Unknown provider credential field",
            fields=[{"field": name, "code": "UNKNOWN"} for name in unknown],
        )

    settings = get_request_settings(request)
    encrypted_values = {
        name: encrypt_provider_secret(settings, secret.get_secret_value())
        for name, secret in payload.secrets.items()
    }
    now = datetime.now(timezone.utc)
    for field_name, ciphertext in encrypted_values.items():
        statement = pg_insert(ProviderSecret).values(
            provider_id=provider_id,
            field_name=field_name,
            secret_ciphertext=ciphertext,
            updated_at=now,
        )
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[ProviderSecret.provider_id, ProviderSecret.field_name],
                set_={
                    "secret_ciphertext": statement.excluded.secret_ciphertext,
                    "updated_at": now,
                },
            )
        )

    configured_result = await db.execute(
        select(ProviderSecret.field_name).where(
            ProviderSecret.provider_id == provider_id
        )
    )
    configured_names = {row[0] for row in configured_result.all()}
    configured_fields = [
        field.name for field in provider.secret_fields if field.name in configured_names
    ]
    settings_for_audit = get_request_settings(request)
    client_ip = get_client_ip(request, settings_for_audit)
    request_id = getattr(request.state, "request_id", uuid.uuid4())
    await record_audit(
        db,
        actor_kind="user",
        actor_id=principal.user_id,
        session_id=principal.session_id,
        action="provider_credentials_updated",
        target_type="provider_credentials",
        result="success",
        request_id=request_id,
        source_ip=client_ip,
        change_summary={
            "provider_id": provider_id,
            "configured_fields": configured_fields,
            "updated_fields": sorted(encrypted_values),
        },
    )
    await db.commit()
    return ProviderSecretUpdateResponse(
        provider_id=provider_id,
        configured_secret_fields=configured_fields,
    )
