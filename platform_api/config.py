"""Configuration for the platform control plane.

Owns validated environment variables, connection URLs, secret keys,
cookie policies, lifetime policies, and runtime boundaries.

Explicitly rejects SQLite URLs (no fallback), forbids pointing
PLATFORM_DATABASE_URL at the local default database (localhost:5439/voice_ai)
or configured local DATABASE_URL, and never imports bot.db.
"""

from __future__ import annotations

import base64
from functools import lru_cache
import ipaddress
from typing import Any
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def _decode_b64url_32(v: str) -> bytes:
    """Decode a base64url secret (padded or unpadded) and verify 32-byte length."""
    raw = v.strip()
    pad = len(raw) % 4
    if pad:
        raw += "=" * (4 - pad)
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as e:
        raise ValueError(f"Invalid base64url encoding: {e}") from e
    if len(decoded) != 32:
        raise ValueError(f"Key must decode to exactly 32 bytes (got {len(decoded)})")
    return decoded


def _validate_fernet_key(v: str) -> str:
    """Validate that the secret is a 32-byte Fernet key."""
    raw = v.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as e:
        raise ValueError(f"Invalid Fernet key encoding: {e}") from e
    if len(decoded) != 32:
        raise ValueError(f"Fernet key must decode to exactly 32 bytes (got {len(decoded)})")
    return raw


def _is_loopback_host(host: str) -> bool:
    """Check if host is a loopback address (127.0.0.1, localhost, ::1)."""
    h = host.strip().lower()
    if h in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback
    except ValueError:
        return False


class PlatformSettings(BaseSettings):
    """Platform API control plane settings.

    Never inherits the root .env implicitly; explicitly reads only OS environment
    variables or programmatic kwargs.
    """

    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Database -------------------------------------------------------------
    database_url: str = Field(
        ...,
        validation_alias=AliasChoices("PLATFORM_DATABASE_URL", "database_url"),
        description="PostgreSQL asyncpg connection URL for platform database",
    )
    local_database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DATABASE_URL",
            "LOCAL_DATABASE_URL",
            "PLATFORM_LOCAL_DATABASE_URL",
            "local_database_url",
        ),
        description="Local bot database URL to verify database isolation",
    )

    # --- Server and Networking ------------------------------------------------
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("PLATFORM_HOST", "host"),
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("PLATFORM_PORT", "port"),
    )
    env: str = Field(
        default="development",
        validation_alias=AliasChoices("PLATFORM_ENV", "env"),
    )
    trusted_proxy_cidrs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("PLATFORM_TRUSTED_PROXY_CIDRS", "trusted_proxy_cidrs"),
    )
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost"],
        validation_alias=AliasChoices("PLATFORM_ALLOWED_HOSTS", "allowed_hosts"),
    )

    # --- Web and CORS ---------------------------------------------------------
    web_origin: str = Field(
        default="http://127.0.0.1:3001",
        validation_alias=AliasChoices("PLATFORM_WEB_ORIGIN", "web_origin"),
    )
    cors_origins: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("PLATFORM_CORS_ORIGINS", "cors_origins"),
    )

    # --- Cookies and CSRF -----------------------------------------------------
    cookie_secure: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_COOKIE_SECURE", "cookie_secure"),
    )
    cookie_domain: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_COOKIE_DOMAIN", "cookie_domain"),
    )
    session_cookie_name: str = Field(
        default="platform_dev_session",
        validation_alias=AliasChoices("PLATFORM_SESSION_COOKIE_NAME", "session_cookie_name"),
    )
    csrf_cookie_name: str = Field(
        default="platform_csrf",
        validation_alias=AliasChoices("PLATFORM_CSRF_COOKIE_NAME", "csrf_cookie_name"),
    )
    csrf_header_name: str = Field(
        default="X-CSRF-Token",
        validation_alias=AliasChoices("PLATFORM_CSRF_HEADER_NAME", "csrf_header_name"),
    )

    # --- Secrets and Cryptography (Required SaaS values in all modes) ---------
    auth_csrf_key: str = Field(
        ...,
        validation_alias=AliasChoices("PLATFORM_AUTH_CSRF_KEY", "auth_csrf_key"),
        description="32-byte base64url secret for CSRF HMAC tokens",
    )
    auth_rate_key: str = Field(
        ...,
        validation_alias=AliasChoices("PLATFORM_AUTH_RATE_KEY", "auth_rate_key"),
        description="32-byte base64url secret for rate limit key hashing",
    )
    auth_mfa_key: str = Field(
        ...,
        validation_alias=AliasChoices("PLATFORM_AUTH_MFA_KEY", "auth_mfa_key"),
        description="32-byte Fernet key for encrypting TOTP secrets",
    )

    # --- Auth Lifetime Policies (exact contract names) ------------------------
    auth_anonymous_ttl_seconds: int = Field(
        default=600,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_ANONYMOUS_TTL_SECONDS", "auth_anonymous_ttl_seconds"),
    )
    auth_mfa_ttl_seconds: int = Field(
        default=300,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MFA_TTL_SECONDS", "auth_mfa_ttl_seconds"),
    )
    auth_session_ttl_seconds: int = Field(
        default=43200,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_SESSION_TTL_SECONDS", "auth_session_ttl_seconds"),
    )
    auth_idle_ttl_seconds: int = Field(
        default=1800,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_IDLE_TTL_SECONDS", "auth_idle_ttl_seconds"),
    )
    auth_idle_touch_seconds: int = Field(
        default=60,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_IDLE_TOUCH_SECONDS", "auth_idle_touch_seconds"),
    )
    auth_max_sessions: int = Field(
        default=5,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAX_SESSIONS", "auth_max_sessions"),
    )
    auth_step_up_seconds: int = Field(
        default=300,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_STEP_UP_SECONDS", "auth_step_up_seconds"),
    )
    auth_register_ttl_seconds: int = Field(
        default=86400,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_REGISTER_TTL_SECONDS", "auth_register_ttl_seconds"),
    )
    auth_reset_ttl_seconds: int = Field(
        default=1800,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_RESET_TTL_SECONDS", "auth_reset_ttl_seconds"),
    )
    auth_hash_concurrency: int = Field(
        default=2,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_HASH_CONCURRENCY", "auth_hash_concurrency"),
    )

    # --- Auth Rate Limits -----------------------------------------------------
    auth_rate_global_per_minute: int = Field(
        default=120,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_RATE_GLOBAL_PER_MINUTE", "auth_rate_global_per_minute"),
    )
    auth_rate_bootstrap_per_minute: int = Field(
        default=30,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_RATE_BOOTSTRAP_PER_MINUTE", "auth_rate_bootstrap_per_minute"),
    )
    auth_rate_login_email_per_15_minutes: int = Field(
        default=5,
        gt=0,
        validation_alias=AliasChoices(
            "PLATFORM_AUTH_RATE_LOGIN_EMAIL_PER_15_MINUTES", "auth_rate_login_email_per_15_minutes"
        ),
    )
    auth_rate_login_ip_per_15_minutes: int = Field(
        default=30,
        gt=0,
        validation_alias=AliasChoices(
            "PLATFORM_AUTH_RATE_LOGIN_IP_PER_15_MINUTES", "auth_rate_login_ip_per_15_minutes"
        ),
    )
    auth_rate_mail_email_per_hour: int = Field(
        default=3,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_RATE_MAIL_EMAIL_PER_HOUR", "auth_rate_mail_email_per_hour"),
    )
    auth_rate_mail_ip_per_hour: int = Field(
        default=20,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_RATE_MAIL_IP_PER_HOUR", "auth_rate_mail_ip_per_hour"),
    )
    auth_mail_resend_seconds: int = Field(
        default=60,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_RESEND_SECONDS", "auth_mail_resend_seconds"),
    )
    auth_rate_mfa_session_per_5_minutes: int = Field(
        default=5,
        gt=0,
        validation_alias=AliasChoices(
            "PLATFORM_AUTH_RATE_MFA_SESSION_PER_5_MINUTES", "auth_rate_mfa_session_per_5_minutes"
        ),
    )
    auth_rate_mfa_ip_per_15_minutes: int = Field(
        default=30,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_RATE_MFA_IP_PER_15_MINUTES", "auth_rate_mfa_ip_per_15_minutes"),
    )
    auth_rate_confirm_ip_per_15_minutes: int = Field(
        default=10,
        gt=0,
        validation_alias=AliasChoices(
            "PLATFORM_AUTH_RATE_CONFIRM_IP_PER_15_MINUTES", "auth_rate_confirm_ip_per_15_minutes"
        ),
    )

    # --- Auth Mail Configuration ----------------------------------------------
    auth_mail_mode: str = Field(
        default="file",
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_MODE", "auth_mail_mode"),
    )
    auth_mail_dir: str = Field(
        default="data/platform-auth-mail",
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_DIR", "auth_mail_dir"),
    )
    auth_mail_queue_max: int = Field(
        default=10000,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_QUEUE_MAX", "auth_mail_queue_max"),
    )
    auth_mail_batch_size: int = Field(
        default=25,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_BATCH_SIZE", "auth_mail_batch_size"),
    )
    auth_mail_lease_seconds: int = Field(
        default=60,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_LEASE_SECONDS", "auth_mail_lease_seconds"),
    )
    auth_mail_attempts: int = Field(
        default=5,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_ATTEMPTS", "auth_mail_attempts"),
    )
    auth_smtp_timeout_seconds: int = Field(
        default=10,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_AUTH_SMTP_TIMEOUT_SECONDS", "auth_smtp_timeout_seconds"),
    )
    auth_smtp_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AUTH_SMTP_HOST", "auth_smtp_host"),
    )
    auth_smtp_port: int | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AUTH_SMTP_PORT", "auth_smtp_port"),
    )
    auth_smtp_username: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AUTH_SMTP_USERNAME", "auth_smtp_username"),
    )
    auth_smtp_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AUTH_SMTP_PASSWORD", "auth_smtp_password"),
    )
    auth_mail_from: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AUTH_MAIL_FROM", "auth_mail_from"),
    )
    auth_smtp_tls_mode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AUTH_SMTP_TLS_MODE", "auth_smtp_tls_mode"),
    )

    # --- Feature Flags (defaults must NOT enable unsafe capabilities) ----------
    enable_generation: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_ENABLE_GENERATION", "enable_generation"),
    )
    enable_chat_test: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_ENABLE_CHAT_TEST", "enable_chat_test"),
    )
    enable_voice_test: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_ENABLE_VOICE_TEST", "enable_voice_test"),
    )
    enable_public_calls: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_ENABLE_PUBLIC_CALLS", "enable_public_calls"),
    )
    enable_real_paid_calls: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "PLATFORM_ENABLE_REAL_PAID_CALLS", "enable_real_paid_calls"
        ),
    )
    enable_qa: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_ENABLE_QA", "enable_qa"),
    )
    enable_gateway: bool = Field(
        default=False,
        validation_alias=AliasChoices("PLATFORM_ENABLE_GATEWAY", "enable_gateway"),
    )

    # --- Operator-owned OpenAI-compatible model endpoints -------------------
    runtime_llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PLATFORM_RUNTIME_LLM_BASE_URL", "runtime_llm_base_url"
        ),
    )
    runtime_llm_api_key: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices(
            "PLATFORM_RUNTIME_LLM_API_KEY", "runtime_llm_api_key"
        ),
    )
    generation_llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PLATFORM_GENERATION_LLM_BASE_URL", "generation_llm_base_url"
        ),
    )
    generation_llm_api_key: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices(
            "PLATFORM_GENERATION_LLM_API_KEY", "generation_llm_api_key"
        ),
    )
    runtime_llm_timeout_seconds: int = Field(default=30, ge=1, le=300)
    generation_llm_timeout_seconds: int = Field(default=90, ge=1, le=300)
    runtime_llm_max_tokens: int = Field(default=256, ge=1, le=4096)
    runtime_llm_temperature: float = Field(default=0.2, ge=0, le=2)
    runtime_llm_reasoning_effort: str | None = "none"
    runtime_llm_max_tokens_parameter: str = "max_tokens"
    generation_llm_temperature: float = Field(default=0.2, ge=0, le=2)
    generation_llm_reasoning_effort: str | None = None
    generation_llm_max_tokens_parameter: str = "max_tokens"
    generation_max_token_budget: int = Field(default=4096, ge=64, le=4096)
    generation_daily_token_budget: int = Field(default=16384, ge=64)
    generation_max_queued_jobs: int = Field(default=64, ge=1, le=10000)
    generation_max_running_jobs: int = Field(default=2, ge=1, le=64)
    generation_max_active_jobs_per_user: int = Field(default=2, ge=1, le=100)
    generation_job_timeout_seconds: int = Field(default=120, ge=1, le=900)
    generation_job_max_age_seconds: int = Field(default=3600, ge=60, le=86400)
    generation_worker_poll_seconds: float = Field(default=0.5, ge=0.05, le=10)
    generation_poll_after_seconds: int = Field(default=2, ge=1, le=30)


    # --- Schema and Catalog ---------------------------------------------------
    schema_version: str = Field(
        default="platform-v1",
        validation_alias=AliasChoices("PLATFORM_SCHEMA_VERSION", "schema_version"),
    )
    catalog_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_CATALOG_PATH", "catalog_path"),
    )
    adapter_capability_report_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PLATFORM_ADAPTER_CAPABILITY_REPORT_PATH",
            "adapter_capability_report_path",
        ),
    )
    adapter_capability_report_hash: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PLATFORM_ADAPTER_CAPABILITY_REPORT_HASH",
            "adapter_capability_report_hash",
        ),
    )
    deployment_adapter_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices(
            "PLATFORM_DEPLOYMENT_ADAPTER_ID", "deployment_adapter_id"
        ),
    )
    deployment_adapter_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices(
            "PLATFORM_DEPLOYMENT_ADAPTER_VERSION", "deployment_adapter_version"
        ),
    )
    deployment_model_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices(
            "PLATFORM_DEPLOYMENT_MODEL_REVISION", "deployment_model_revision"
        ),
    )
    deployment_profile_hash: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices(
            "PLATFORM_DEPLOYMENT_PROFILE_HASH", "deployment_profile_hash"
        ),
    )
    deployment_source_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices(
            "PLATFORM_DEPLOYMENT_SOURCE_REVISION", "deployment_source_revision"
        ),
    )


    # --- Accounting and Events ------------------------------------------------
    billing_slice_ms: int = Field(
        default=1000,
        gt=0,
        validation_alias=AliasChoices("PLATFORM_BILLING_SLICE_MS", "billing_slice_ms"),
    )
    amqp_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLATFORM_AMQP_URL", "amqp_url"),
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("PLATFORM_DATABASE_URL is required and cannot be empty.")
        url_str = v.strip()
        try:
            parsed = make_url(url_str)
        except Exception as e:
            raise ValueError(f"Invalid PLATFORM_DATABASE_URL: {e}") from e

        backend = parsed.get_backend_name()
        if backend == "sqlite":
            raise ValueError(
                "SQLite is not supported for platform database; PostgreSQL is required."
            )
        if backend != "postgresql":
            raise ValueError(
                f"Unsupported platform database backend: {backend}. PostgreSQL is required."
            )

        driver = parsed.get_driver_name()
        if driver and driver != "asyncpg":
            raise ValueError(
                f"Platform database must use asyncpg driver (postgresql+asyncpg://), got: {driver}"
            )
        return url_str

    @field_validator("env")
    @classmethod
    def validate_env_profile(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"development", "staging", "production"}:
            raise ValueError(
                f"Invalid PLATFORM_ENV '{v}'. Allowed profiles: 'development', 'staging', 'production'."
            )
        return normalized

    @field_validator("cors_origins", "allowed_hosts", "trusted_proxy_cidrs", mode="before")
    @classmethod
    def parse_comma_separated_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, (list, tuple)):
            return [str(item).strip() for item in v if str(item).strip()]
        return v

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_trusted_proxies(cls, v: list[str]) -> list[str]:
        validated = []
        for item in v:
            c = item.strip()
            if not c:
                continue
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError:
                try:
                    ipaddress.ip_address(c)
                except ValueError as e:
                    raise ValueError(
                        f"Invalid CIDR or IP address in trusted_proxy_cidrs: '{item}'"
                    ) from e
            validated.append(c)
        return validated

    @field_validator("web_origin")
    @classmethod
    def validate_web_origin_format(cls, v: str) -> str:
        origin = v.strip()
        if "*" in origin:
            raise ValueError(f"PLATFORM_WEB_ORIGIN cannot contain wildcards: '{origin}'")
        parsed = urlsplit(origin)
        if not parsed.scheme or parsed.scheme not in {"http", "https"}:
            raise ValueError(f"PLATFORM_WEB_ORIGIN must have http or https scheme: '{origin}'")
        if not parsed.netloc:
            raise ValueError(f"PLATFORM_WEB_ORIGIN must include a valid host: '{origin}'")
        if parsed.path and parsed.path != "/":
            raise ValueError(f"PLATFORM_WEB_ORIGIN must not contain a path component: '{origin}'")
        if parsed.query or parsed.fragment:
            raise ValueError(f"PLATFORM_WEB_ORIGIN must not contain query or fragment: '{origin}'")
        return origin.rstrip("/")

    @field_validator("auth_csrf_key", "auth_rate_key")
    @classmethod
    def validate_b64url_32_keys(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Cryptographic key is required and cannot be empty.")
        _decode_b64url_32(v)
        return v.strip()

    @field_validator("auth_mfa_key")
    @classmethod
    def validate_fernet_key(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("MFA key is required and cannot be empty.")
        return _validate_fernet_key(v)

    @field_validator("auth_mail_mode")
    @classmethod
    def validate_mail_mode(cls, v: str) -> str:
        mode = v.strip().lower()
        if mode not in {"file", "smtp"}:
            raise ValueError(
                f"Invalid PLATFORM_AUTH_MAIL_MODE '{v}'. Must be 'file' or 'smtp'."
            )
        return mode

    @field_validator(
        "adapter_capability_report_path",
        "adapter_capability_report_hash",
        mode="before",
    )
    @classmethod
    def normalize_optional_capability_source(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("adapter_capability_report_hash")
    @classmethod
    def validate_capability_report_hash(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError(
                "PLATFORM_ADAPTER_CAPABILITY_REPORT_HASH must be a lowercase "
                "SHA-256 digest."
            )
        return value

    @model_validator(mode="after")
    def validate_capability_report_source(self) -> PlatformSettings:
        if (self.adapter_capability_report_path is None) != (
            self.adapter_capability_report_hash is None
        ):
            raise ValueError(
                "PLATFORM_ADAPTER_CAPABILITY_REPORT_PATH and "
                "PLATFORM_ADAPTER_CAPABILITY_REPORT_HASH must be configured together."
            )
        return self

    @field_validator(
        "deployment_adapter_id",
        "deployment_adapter_version",
        "deployment_model_revision",
        "deployment_profile_hash",
        "deployment_source_revision",
        mode="before",
    )
    @classmethod
    def normalize_optional_deployment_identity(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_deployment_identity(self) -> PlatformSettings:
        identity_values = (
            self.deployment_adapter_id,
            self.deployment_adapter_version,
            self.deployment_model_revision,
            self.deployment_profile_hash,
            self.deployment_source_revision,
        )
        if any(value is not None for value in identity_values) and not all(
            value is not None for value in identity_values
        ):
            raise ValueError(
                "All PLATFORM_DEPLOYMENT_* identity fields must be configured together."
            )
        return self


    @model_validator(mode="after")
    def validate_key_separation(self) -> PlatformSettings:
        if self.auth_csrf_key == self.auth_rate_key:
            raise ValueError(
                "PLATFORM_AUTH_CSRF_KEY and PLATFORM_AUTH_RATE_KEY must be distinct keys."
            )
        if self.auth_csrf_key == self.auth_mfa_key or self.auth_rate_key == self.auth_mfa_key:
            raise ValueError(
                "All cryptographic keys (CSRF, rate limit, and MFA) must be distinct keys."
            )
        return self

    @model_validator(mode="after")
    def resolve_and_validate_cors(self) -> PlatformSettings:
        # Default CORS origins if unspecified
        if not self.cors_origins:
            if self.env == "development":
                # For split-origin development, CORS allows precisely the web origin
                self.cors_origins = [self.web_origin]
            else:
                # Production same-origin requires no cross-origin allowance
                self.cors_origins = []
        else:
            # Validate custom CORS origins
            for origin in self.cors_origins:
                if "*" in origin:
                    raise ValueError(f"cors_origins cannot contain wildcards: '{origin}'")
                parsed = urlsplit(origin)
                if not parsed.scheme or parsed.scheme not in {"http", "https"}:
                    raise ValueError(f"cors_origin must have http or https scheme: '{origin}'")
                if not parsed.netloc:
                    raise ValueError(f"cors_origin must include a valid host: '{origin}'")
                if parsed.path and parsed.path != "/":
                    raise ValueError(f"cors_origin must not contain a path component: '{origin}'")
                if parsed.query or parsed.fragment:
                    raise ValueError(f"cors_origin must not contain query or fragment: '{origin}'")
                if self.env in {"staging", "production"} and parsed.scheme != "https":
                    raise ValueError(
                        f"cors_origin must use HTTPS in '{self.env}' environment (got: {origin})"
                    )
        return self

    @model_validator(mode="after")
    def validate_lifetime_invariants(self) -> PlatformSettings:
        if self.auth_idle_ttl_seconds > self.auth_session_ttl_seconds:
            raise ValueError(
                f"auth_idle_ttl_seconds ({self.auth_idle_ttl_seconds}) cannot exceed "
                f"auth_session_ttl_seconds ({self.auth_session_ttl_seconds})."
            )
        if self.auth_idle_touch_seconds > self.auth_session_ttl_seconds:
            raise ValueError(
                f"auth_idle_touch_seconds ({self.auth_idle_touch_seconds}) cannot exceed "
                f"auth_session_ttl_seconds ({self.auth_session_ttl_seconds})."
            )
        if self.auth_step_up_seconds > self.auth_session_ttl_seconds:
            raise ValueError(
                f"auth_step_up_seconds ({self.auth_step_up_seconds}) cannot exceed "
                f"auth_session_ttl_seconds ({self.auth_session_ttl_seconds})."
            )
        if self.auth_mail_lease_seconds <= self.auth_smtp_timeout_seconds:
            raise ValueError(
                f"auth_mail_lease_seconds ({self.auth_mail_lease_seconds}) must exceed "
                f"auth_smtp_timeout_seconds ({self.auth_smtp_timeout_seconds})."
            )
        return self

    @model_validator(mode="after")
    def validate_smtp_configuration(self) -> PlatformSettings:
        if self.auth_mail_mode == "smtp":
            if not self.auth_smtp_host:
                raise ValueError("PLATFORM_AUTH_SMTP_HOST is required when auth_mail_mode is 'smtp'.")
            if not self.auth_smtp_port or not (1 <= self.auth_smtp_port <= 65535):
                raise ValueError("Valid PLATFORM_AUTH_SMTP_PORT (1-65535) is required when auth_mail_mode is 'smtp'.")
            if not self.auth_smtp_username:
                raise ValueError("PLATFORM_AUTH_SMTP_USERNAME is required when auth_mail_mode is 'smtp'.")
            if not self.auth_smtp_password:
                raise ValueError("PLATFORM_AUTH_SMTP_PASSWORD is required when auth_mail_mode is 'smtp'.")
            if not self.auth_mail_from:
                raise ValueError("PLATFORM_AUTH_MAIL_FROM is required when auth_mail_mode is 'smtp'.")
            if not self.auth_smtp_tls_mode or self.auth_smtp_tls_mode.lower() not in {"starttls", "tls"}:
                raise ValueError("PLATFORM_AUTH_SMTP_TLS_MODE must be 'starttls' or 'tls' when auth_mail_mode is 'smtp'.")
        return self

    @field_validator("runtime_llm_base_url", "generation_llm_base_url")
    @classmethod
    def validate_model_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        raw = value.strip().rstrip("/")
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("Model endpoint must be a valid HTTP(S) base URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Model endpoint must be an HTTP(S) base URL without credentials, "
                "query, or fragment"
            )
        if raw.endswith("/chat/completions"):
            raise ValueError("Model endpoint must be a base URL, not a completion path")
        return raw

    @field_validator("runtime_llm_api_key", "generation_llm_api_key")
    @classmethod
    def normalize_model_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        key = value.strip()
        if not key:
            return None
        if len(key) > 4096:
            raise ValueError("Model API key exceeds the configured length limit")
        return key

    @model_validator(mode="after")
    def validate_model_execution_configuration(self) -> PlatformSettings:
        if self.enable_chat_test and self.runtime_llm_base_url is None:
            raise ValueError(
                "PLATFORM_RUNTIME_LLM_BASE_URL is required when chat testing is enabled."
            )
        if self.enable_generation and self.generation_llm_base_url is None:
            raise ValueError(
                "PLATFORM_GENERATION_LLM_BASE_URL is required when generation is enabled."
            )
        if self.generation_llm_timeout_seconds > self.generation_job_timeout_seconds:
            raise ValueError(
                "generation_llm_timeout_seconds cannot exceed generation_job_timeout_seconds."
            )
        if self.runtime_llm_max_tokens_parameter not in {
            "max_tokens",
            "max_completion_tokens",
        }:
            raise ValueError(
                "runtime_llm_max_tokens_parameter must be max_tokens or max_completion_tokens."
            )
        if self.generation_llm_max_tokens_parameter not in {
            "max_tokens",
            "max_completion_tokens",
        }:
            raise ValueError(
                "generation_llm_max_tokens_parameter must be max_tokens or max_completion_tokens."
            )
        reasoning_levels = {None, "none", "low", "medium", "high"}
        if self.runtime_llm_reasoning_effort not in reasoning_levels:
            raise ValueError(
                "runtime_llm_reasoning_effort must be none, low, medium, high, or unset."
            )
        if self.generation_llm_reasoning_effort not in reasoning_levels:
            raise ValueError(
                "generation_llm_reasoning_effort must be none, low, medium, high, or unset."
            )
        return self

    @model_validator(mode="after")
    def validate_security_and_cookie_policy(self) -> PlatformSettings:
        origin_parsed = urlsplit(self.web_origin)
        origin_host = (origin_parsed.hostname or "").lower()

        if self.cookie_secure:
            if self.cookie_domain is not None:
                raise ValueError(
                    "Production cookies must not set cookie_domain (contract requires no Domain for __Host- cookies)."
                )
            if self.session_cookie_name != "__Host-platform_session":
                raise ValueError(
                    "Secure session cookie name must be '__Host-platform_session' (contract requirement)."
                )
        else:
            # Insecure cookie policy is strictly for development on loopback
            if self.env != "development":
                raise ValueError(
                    f"Insecure cookie policy (cookie_secure=False) is only allowed in 'development' environment (got '{self.env}')."
                )
            if self.session_cookie_name != "platform_dev_session":
                raise ValueError(
                    f"Insecure session cookie name must be 'platform_dev_session' (got '{self.session_cookie_name}')."
                )
            if self.cookie_domain is not None:
                raise ValueError(
                    "cookie_domain must be None for development platform cookies."
                )
            if not _is_loopback_host(self.host):
                raise ValueError(
                    f"Insecure development policy requires loopback bind host, got: {self.host}."
                )
            if not _is_loopback_host(origin_host):
                raise ValueError(
                    f"Insecure development policy requires loopback web_origin host, got: {self.web_origin}."
                )

        if self.env in {"staging", "production"}:
            if not self.cookie_secure:
                raise ValueError(
                    f"cookie_secure must be True in '{self.env}' environment."
                )
            if not self.web_origin.startswith("https://"):
                raise ValueError(
                    f"web_origin must use HTTPS in '{self.env}' environment (got: {self.web_origin})."
                )
            if not self.trusted_proxy_cidrs:
                raise ValueError(
                    f"trusted_proxy_cidrs must be configured in '{self.env}' environment."
                )
            if not self.allowed_hosts or "*" in self.allowed_hosts:
                raise ValueError(
                    f"allowed_hosts must be explicitly configured without wildcards in '{self.env}' environment."
                )
            if self.auth_mail_mode != "smtp":
                raise ValueError(f"auth_mail_mode must be 'smtp' in '{self.env}' environment.")
        return self

    @model_validator(mode="after")
    def validate_database_separation(self) -> PlatformSettings:
        if not self.database_url:
            return self

        plat_url = make_url(self.database_url)
        plat_host = (plat_url.host or "localhost").lower()
        plat_port = plat_url.port or 5432
        plat_db = (plat_url.database or "").lstrip("/")

        # Check collision with local default DB (localhost:5439/voice_ai or 127.0.0.1:5439/voice_ai)
        if plat_host in {"127.0.0.1", "localhost"} and plat_port == 5439 and plat_db == "voice_ai":
            raise ValueError(
                "PLATFORM_DATABASE_URL points to the default local bot database "
                "(localhost:5439/voice_ai). The platform database must be a separate database."
            )

        # Check collision with configured local_database_url (fail-closed on malformed URL)
        if self.local_database_url:
            try:
                loc_url = make_url(self.local_database_url)
            except Exception as e:
                raise ValueError(
                    f"Invalid local_database_url: {e}. Refusing to start with malformed local DB URL."
                ) from e

            loc_host = (loc_url.host or "localhost").lower()
            loc_port = loc_url.port or 5432
            loc_db = (loc_url.database or "").lstrip("/")

            # Normalize 127.0.0.1 and localhost equivalence
            host_equiv = {plat_host, loc_host} <= {"127.0.0.1", "localhost"} or plat_host == loc_host

            if (
                plat_url.get_backend_name() == loc_url.get_backend_name()
                and host_equiv
                and plat_port == loc_port
                and plat_db == loc_db
            ):
                raise ValueError(
                    f"PLATFORM_DATABASE_URL points to the same database as local DATABASE_URL ({plat_db}). "
                    "The platform database must be a separate database."
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> PlatformSettings:
    """Return cached platform settings loaded from environment."""
    return PlatformSettings()
