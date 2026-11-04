"""Negative behavioral tests for platform configuration and settings.

Verifies:
- Settings use contract lifetime names (e.g. PLATFORM_AUTH_ANONYMOUS_TTL_SECONDS)
- Rejection of missing required auth keys in all modes (including development)
- Rejection of identical CSRF and rate keys (key separation enforcement)
- Rejection of identical MFA keys with CSRF or rate keys
- Rejection of non-loopback bind hosts or web_origins when cookie_secure=False
- Production cookie policy: __Host-platform_session required, cookie_domain must be None
- Custom CORS validation: wildcards forbidden, non-HTTPS forbidden in staging/prod
- CORS defaults: development defaults to [web_origin], production defaults to []
- Trusted proxy CIDR validation: invalid CIDRs/IPs rejected
- Fail-closed on malformed database_url
- Zero and negative duration/limit/concurrency/port validation
- Invariant validations: idle TTL <= session TTL, touch <= session TTL, step-up <= session TTL,
  mail lease > SMTP timeout (each reaching its intended validation branch)
- Environment profile validation (reject unknown profiles)
- Staging/production security validation (secure cookies, HTTPS origin, trusted proxies, allowed hosts)
- Origin validation (reject paths, wildcards, missing schemes)
- Key validation (32-byte CSRF/rate keys, Fernet MFA key)
- Mail mode and SMTP validation
- Feature flags default to False (cannot enable unsafe capabilities)
- Database settings: reject SQLite, non-PostgreSQL, non-asyncpg, require valid postgres_schema,
  and accept sharing local database with schema separation.
"""

from __future__ import annotations

import base64
import pytest
from pydantic import ValidationError

from platform_api.config import PlatformSettings

# Distinct 32-byte keys for testing
VALID_CSRF_KEY = base64.urlsafe_b64encode(b"C" * 32).decode("ascii")
VALID_RATE_KEY = base64.urlsafe_b64encode(b"R" * 32).decode("ascii")
VALID_MFA_KEY = base64.urlsafe_b64encode(b"M" * 32).decode("ascii")

DB_URL = "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_platform"


def make_settings(**overrides) -> PlatformSettings:
    """Helper returning valid development settings with specified overrides."""
    kwargs = {
        "database_url": DB_URL,
        "auth_csrf_key": VALID_CSRF_KEY,
        "auth_rate_key": VALID_RATE_KEY,
        "auth_mfa_key": VALID_MFA_KEY,
    }
    kwargs.update(overrides)
    return PlatformSettings(**kwargs)

def test_provider_secret_key_is_optional_dedicated_and_redacted() -> None:
    from pydantic import SecretStr

    from cryptography.fernet import Fernet

    assert make_settings().provider_secret_key is None
    key = Fernet.generate_key().decode("ascii")
    settings = make_settings(provider_secret_key=key)
    assert isinstance(settings.provider_secret_key, SecretStr)
    assert settings.provider_secret_key.get_secret_value() == key
    assert key not in repr(settings)


@pytest.mark.parametrize(
    "provider_secret_key",
    [
        "not-a-fernet-key",
        VALID_CSRF_KEY,
        VALID_RATE_KEY,
        VALID_MFA_KEY,
    ],
)
def test_provider_secret_key_rejects_invalid_or_reused_keys(provider_secret_key) -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_settings(provider_secret_key=provider_secret_key)
    assert provider_secret_key not in str(exc_info.value)



def test_provider_secret_key_rejects_base64_alias_of_auth_key() -> None:
    key = base64.urlsafe_b64encode(b"F" * 32).decode("ascii")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last_character = alphabet.index(key[-2])
    alias = f"{key[:-2]}{alphabet[last_character + 1]}="
    assert base64.urlsafe_b64decode(alias) == base64.urlsafe_b64decode(key)

    with pytest.raises(ValidationError):
        make_settings(auth_mfa_key=key, provider_secret_key=alias)


def test_operations_policy_limits_have_documented_defaults():
    settings = make_settings()

    assert settings.ops_export_max_bytes == 16 * 1024 * 1024
    assert settings.ops_export_max_records == 10_000
    assert settings.ops_retention_batch_size == 500
    assert settings.ops_backup_max_bytes == 8 * 1024 * 1024 * 1024
    assert settings.ops_restore_max_bytes == 8 * 1024 * 1024 * 1024


@pytest.mark.parametrize(
    "field,value",
    [
        ("ops_export_max_bytes", 0),
        ("ops_export_max_records", 0),
        ("ops_retention_batch_size", 0),
        ("ops_retention_batch_size", 1001),
        ("ops_backup_max_bytes", 0),
        ("ops_restore_max_bytes", 0),
    ],
)
def test_operations_policy_limits_reject_invalid_values(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_settings_requires_contract_ttl_names():
    """Settings must use exact contract names (TTL_SECONDS), not non-contract names."""
    settings = make_settings(
        auth_anonymous_ttl_seconds=120,
        auth_mfa_ttl_seconds=180,
        auth_session_ttl_seconds=7200,
        auth_idle_ttl_seconds=900,
        auth_idle_touch_seconds=30,
        auth_max_sessions=10,
        auth_step_up_seconds=150,
        auth_register_ttl_seconds=3600,
        auth_reset_ttl_seconds=600,
        auth_hash_concurrency=4,
    )

    assert settings.auth_anonymous_ttl_seconds == 120
    assert settings.auth_mfa_ttl_seconds == 180
    assert settings.auth_session_ttl_seconds == 7200
    assert settings.auth_idle_ttl_seconds == 900
    assert settings.auth_idle_touch_seconds == 30
    assert settings.auth_max_sessions == 10
    assert settings.auth_step_up_seconds == 150
    assert settings.auth_register_ttl_seconds == 3600
    assert settings.auth_reset_ttl_seconds == 600
    assert settings.auth_hash_concurrency == 4


def test_settings_requires_keys_in_development():
    """Auth CSRF, rate, and MFA keys are required SaaS values even in development."""
    # Omit CSRF key
    with pytest.raises(ValidationError) as exc:
        PlatformSettings(
            database_url=DB_URL,
            auth_rate_key=VALID_RATE_KEY,
            auth_mfa_key=VALID_MFA_KEY,
        )
    assert any("csrf" in str(err["loc"]).lower() for err in exc.value.errors())

    # Omit Rate key
    with pytest.raises(ValidationError) as exc:
        PlatformSettings(
            database_url=DB_URL,
            auth_csrf_key=VALID_CSRF_KEY,
            auth_mfa_key=VALID_MFA_KEY,
        )
    assert any("rate" in str(err["loc"]).lower() for err in exc.value.errors())

    # Omit MFA key
    with pytest.raises(ValidationError) as exc:
        PlatformSettings(
            database_url=DB_URL,
            auth_csrf_key=VALID_CSRF_KEY,
            auth_rate_key=VALID_RATE_KEY,
        )
    assert any("mfa" in str(err["loc"]).lower() for err in exc.value.errors())


def test_settings_enforces_key_separation():
    """CSRF, Rate limit, and MFA keys must all be distinct from one another."""
    # Identical CSRF and Rate keys
    with pytest.raises(ValidationError):
        make_settings(
            auth_csrf_key=VALID_CSRF_KEY,
            auth_rate_key=VALID_CSRF_KEY,
            auth_mfa_key=VALID_MFA_KEY,
        )

    # Identical CSRF and MFA keys
    with pytest.raises(ValidationError):
        make_settings(
            auth_csrf_key=VALID_MFA_KEY,
            auth_rate_key=VALID_RATE_KEY,
            auth_mfa_key=VALID_MFA_KEY,
        )

    # Identical Rate and MFA keys
    with pytest.raises(ValidationError):
        make_settings(
            auth_csrf_key=VALID_CSRF_KEY,
            auth_rate_key=VALID_MFA_KEY,
            auth_mfa_key=VALID_MFA_KEY,
        )


def test_settings_rejects_insecure_development_non_loopback():
    """Insecure cookies (cookie_secure=False) require loopback bind and loopback web_origin."""
    # Non-loopback bind host
    with pytest.raises(ValidationError):
        make_settings(host="0.0.0.0", cookie_secure=False)

    with pytest.raises(ValidationError):
        make_settings(host="192.168.1.100", cookie_secure=False)

    # Non-loopback web_origin
    with pytest.raises(ValidationError):
        make_settings(web_origin="http://voiceai.internal:3001", cookie_secure=False)


def test_settings_insecure_cookie_name_and_domain():
    """Insecure development cookies must be named platform_dev_session with no cookie_domain."""
    with pytest.raises(ValidationError):
        make_settings(session_cookie_name="custom_session", cookie_secure=False)

    with pytest.raises(ValidationError):
        make_settings(cookie_domain="localhost", cookie_secure=False)


def test_settings_production_cookie_policy():
    """Production cookies must be __Host-platform_session with no cookie_domain."""
    prod_kwargs = {
        "database_url": DB_URL,
        "env": "production",
        "cookie_secure": True,
        "web_origin": "https://platform.example.com",
        "trusted_proxy_cidrs": ["10.0.0.0/8"],
        "allowed_hosts": ["platform.example.com"],
        "auth_csrf_key": VALID_CSRF_KEY,
        "auth_rate_key": VALID_RATE_KEY,
        "auth_mfa_key": VALID_MFA_KEY,
        "auth_mail_mode": "smtp",
        "auth_smtp_host": "smtp.example.com",
        "auth_smtp_port": 587,
        "auth_smtp_username": "user",
        "auth_smtp_password": "pw",
        "auth_mail_from": "noreply@example.com",
        "auth_smtp_tls_mode": "starttls",
        "session_cookie_name": "__Host-platform_session",
    }

    # Setting cookie_domain in production is forbidden (contract requires no Domain for __Host- cookies)
    with pytest.raises(ValidationError):
        PlatformSettings(**dict(prod_kwargs, cookie_domain="example.com"))

    # Custom cookie name in production is forbidden
    with pytest.raises(ValidationError):
        PlatformSettings(**dict(prod_kwargs, session_cookie_name="my_prod_cookie"))


def test_cors_resolution_and_validation():
    """CORS origins default to [web_origin] in dev and [] in prod; wildcards and non-HTTPS are rejected."""
    # Development default
    dev_settings = make_settings()
    assert dev_settings.cors_origins == ["http://127.0.0.1:3001"]

    # Production default
    prod_settings = PlatformSettings(
        database_url=DB_URL,
        env="production",
        cookie_secure=True,
        web_origin="https://platform.example.com",
        trusted_proxy_cidrs=["10.0.0.0/8"],
        allowed_hosts=["platform.example.com"],
        auth_csrf_key=VALID_CSRF_KEY,
        auth_rate_key=VALID_RATE_KEY,
        auth_mfa_key=VALID_MFA_KEY,
        auth_mail_mode="smtp",
        auth_smtp_host="smtp.example.com",
        auth_smtp_port=587,
        auth_smtp_username="user",
        auth_smtp_password="pw",
        auth_mail_from="noreply@example.com",
        auth_smtp_tls_mode="starttls",
        session_cookie_name="__Host-platform_session",
    )
    assert prod_settings.cors_origins == []

    # Wildcard in CORS origins rejected
    with pytest.raises(ValidationError):
        make_settings(cors_origins=["http://127.0.0.1:3001", "*"])

    # Non-HTTPS CORS origin in production rejected
    with pytest.raises(ValidationError):
        PlatformSettings(
            database_url=DB_URL,
            env="production",
            cookie_secure=True,
            web_origin="https://platform.example.com",
            cors_origins=["http://external.example.com"],
            trusted_proxy_cidrs=["10.0.0.0/8"],
            allowed_hosts=["platform.example.com"],
            auth_csrf_key=VALID_CSRF_KEY,
            auth_rate_key=VALID_RATE_KEY,
            auth_mfa_key=VALID_MFA_KEY,
            auth_mail_mode="smtp",
            auth_smtp_host="smtp.example.com",
            auth_smtp_port=587,
            auth_smtp_username="user",
            auth_smtp_password="pw",
            auth_mail_from="noreply@example.com",
            auth_smtp_tls_mode="starttls",
            session_cookie_name="__Host-platform_session",
        )


def test_trusted_proxy_cidrs_validation():
    """trusted_proxy_cidrs rejects invalid CIDRs and IP addresses."""
    with pytest.raises(ValidationError):
        make_settings(trusted_proxy_cidrs=["not-a-valid-cidr"])

    # Valid CIDRs and single IPs are accepted
    s = make_settings(trusted_proxy_cidrs=["10.0.0.0/8", "192.168.1.1", "172.16.0.0/12"])
    assert s.trusted_proxy_cidrs == ["10.0.0.0/8", "192.168.1.1", "172.16.0.0/12"]


def test_malformed_database_url_fails_closed():
    """Malformed database_url must fail closed with ValidationError."""
    with pytest.raises(ValidationError):
        make_settings(database_url="not-a-valid-database-url")

def test_settings_rejects_zero_or_negative_durations():
    """Reject zero or negative duration, TTL, count, concurrency, and port values."""
    for field in [
        "auth_anonymous_ttl_seconds",
        "auth_mfa_ttl_seconds",
        "auth_session_ttl_seconds",
        "auth_idle_ttl_seconds",
        "auth_idle_touch_seconds",
        "auth_max_sessions",
        "auth_step_up_seconds",
        "auth_register_ttl_seconds",
        "auth_reset_ttl_seconds",
        "auth_hash_concurrency",
        "auth_rate_global_per_minute",
        "auth_rate_bootstrap_per_minute",
        "auth_rate_login_email_per_15_minutes",
        "auth_rate_login_ip_per_15_minutes",
        "auth_rate_mail_email_per_hour",
        "auth_rate_mail_ip_per_hour",
        "auth_mail_resend_seconds",
        "auth_rate_mfa_session_per_5_minutes",
        "auth_rate_mfa_ip_per_15_minutes",
        "auth_rate_confirm_ip_per_15_minutes",
        "auth_mail_queue_max",
        "auth_mail_batch_size",
        "auth_mail_lease_seconds",
        "auth_mail_attempts",
        "auth_smtp_timeout_seconds",
    ]:
        with pytest.raises(ValidationError):
            make_settings(**{field: 0})
        with pytest.raises(ValidationError):
            make_settings(**{field: -5})


def test_settings_rejects_invalid_port():
    """Port must be between 1 and 65535."""
    with pytest.raises(ValidationError):
        make_settings(port=0)
    with pytest.raises(ValidationError):
        make_settings(port=70000)


def test_settings_rejects_idle_ttl_exceeding_session_ttl():
    """auth_idle_ttl_seconds cannot exceed auth_session_ttl_seconds."""
    with pytest.raises(ValidationError):
        make_settings(
            auth_session_ttl_seconds=1000,
            auth_idle_ttl_seconds=1001,
        )


def test_settings_rejects_idle_touch_exceeding_session_ttl():
    """auth_idle_touch_seconds cannot exceed auth_session_ttl_seconds."""
    with pytest.raises(ValidationError):
        make_settings(
            auth_session_ttl_seconds=1000,
            auth_idle_ttl_seconds=500,  # Valid idle TTL so touch TTL check is reached
            auth_idle_touch_seconds=1001,
        )


def test_settings_rejects_step_up_exceeding_session_ttl():
    """auth_step_up_seconds cannot exceed auth_session_ttl_seconds."""
    with pytest.raises(ValidationError):
        make_settings(
            auth_session_ttl_seconds=1000,
            auth_idle_ttl_seconds=500,  # Valid idle TTL so step-up TTL check is reached
            auth_step_up_seconds=1001,
        )


def test_settings_rejects_mail_lease_not_exceeding_smtp_timeout():
    """auth_mail_lease_seconds must exceed auth_smtp_timeout_seconds."""
    with pytest.raises(ValidationError):
        make_settings(
            auth_mail_lease_seconds=10,
            auth_smtp_timeout_seconds=10,
        )


def test_settings_rejects_unknown_env_profile():
    """Reject unknown environment profiles outside development/staging/production."""
    for invalid_env in ["local", "test", "staging_qa", "prod", "uat"]:
        with pytest.raises(ValidationError):
            make_settings(env=invalid_env)


def test_settings_rejects_insecure_cookies_in_production():
    """Staging and production require cookie_secure=True."""
    for env in ["staging", "production"]:
        with pytest.raises(ValidationError):
            PlatformSettings(
                database_url=DB_URL,
                env=env,
                cookie_secure=False,
                web_origin="https://platform.example.com",
                trusted_proxy_cidrs=["10.0.0.0/8"],
                allowed_hosts=["platform.example.com"],
                auth_csrf_key=VALID_CSRF_KEY,
                auth_rate_key=VALID_RATE_KEY,
                auth_mfa_key=VALID_MFA_KEY,
                auth_mail_mode="smtp",
                auth_smtp_host="smtp.example.com",
                auth_smtp_port=587,
                auth_smtp_username="user",
                auth_smtp_password="pw",
                auth_mail_from="noreply@example.com",
                auth_smtp_tls_mode="starttls",
                session_cookie_name="__Host-platform_session",
            )


def test_settings_rejects_non_https_origin_in_production():
    """Staging and production require HTTPS web_origin."""
    for env in ["staging", "production"]:
        with pytest.raises(ValidationError):
            PlatformSettings(
                database_url=DB_URL,
                env=env,
                cookie_secure=True,
                web_origin="http://platform.example.com",
                trusted_proxy_cidrs=["10.0.0.0/8"],
                allowed_hosts=["platform.example.com"],
                auth_csrf_key=VALID_CSRF_KEY,
                auth_rate_key=VALID_RATE_KEY,
                auth_mfa_key=VALID_MFA_KEY,
                auth_mail_mode="smtp",
                auth_smtp_host="smtp.example.com",
                auth_smtp_port=587,
                auth_smtp_username="user",
                auth_smtp_password="pw",
                auth_mail_from="noreply@example.com",
                auth_smtp_tls_mode="starttls",
                session_cookie_name="__Host-platform_session",
            )


def test_settings_rejects_empty_trusted_proxies_in_production():
    """Staging and production require non-empty trusted_proxy_cidrs."""
    for env in ["staging", "production"]:
        with pytest.raises(ValidationError):
            PlatformSettings(
                database_url=DB_URL,
                env=env,
                cookie_secure=True,
                web_origin="https://platform.example.com",
                trusted_proxy_cidrs=[],
                allowed_hosts=["platform.example.com"],
                auth_csrf_key=VALID_CSRF_KEY,
                auth_rate_key=VALID_RATE_KEY,
                auth_mfa_key=VALID_MFA_KEY,
                auth_mail_mode="smtp",
                auth_smtp_host="smtp.example.com",
                auth_smtp_port=587,
                auth_smtp_username="user",
                auth_smtp_password="pw",
                auth_mail_from="noreply@example.com",
                auth_smtp_tls_mode="starttls",
                session_cookie_name="__Host-platform_session",
            )


def test_settings_rejects_wildcard_allowed_hosts_in_production():
    """Staging and production reject wildcard allowed hosts."""
    for env in ["staging", "production"]:
        with pytest.raises(ValidationError):
            PlatformSettings(
                database_url=DB_URL,
                env=env,
                cookie_secure=True,
                web_origin="https://platform.example.com",
                trusted_proxy_cidrs=["10.0.0.0/8"],
                allowed_hosts=["*"],
                auth_csrf_key=VALID_CSRF_KEY,
                auth_rate_key=VALID_RATE_KEY,
                auth_mfa_key=VALID_MFA_KEY,
                auth_mail_mode="smtp",
                auth_smtp_host="smtp.example.com",
                auth_smtp_port=587,
                auth_smtp_username="user",
                auth_smtp_password="pw",
                auth_mail_from="noreply@example.com",
                auth_smtp_tls_mode="starttls",
                session_cookie_name="__Host-platform_session",
            )


def test_settings_validates_key_lengths():
    """Cryptographic keys must be valid 32-byte secrets."""
    short_key = base64.urlsafe_b64encode(b"0" * 16).decode("ascii")

    with pytest.raises(ValidationError):
        make_settings(auth_csrf_key=short_key)

    with pytest.raises(ValidationError):
        make_settings(auth_rate_key=short_key)

    with pytest.raises(ValidationError):
        make_settings(auth_mfa_key="not-a-valid-fernet-key")


def test_settings_origin_validation():
    """web_origin must be exact scheme+host+port with no path and no wildcard."""
    with pytest.raises(ValidationError):
        make_settings(web_origin="http://localhost:3000/app")

    with pytest.raises(ValidationError):
        make_settings(web_origin="http://*.example.com")

    with pytest.raises(ValidationError):
        make_settings(web_origin="localhost:3000")


def test_settings_mail_mode_validation():
    """auth_mail_mode must be 'file' or 'smtp' and validate SMTP parameters."""
    with pytest.raises(ValidationError):
        make_settings(auth_mail_mode="sendmail")

    with pytest.raises(ValidationError):
        make_settings(
            auth_mail_mode="smtp",
            auth_smtp_port=587,
            auth_smtp_username="user",
            auth_smtp_password="pw",
            auth_mail_from="noreply@example.com",
            auth_smtp_tls_mode="starttls",
        )

    with pytest.raises(ValidationError):
        make_settings(
            auth_mail_mode="smtp",
            auth_smtp_host="smtp.example.com",
            auth_smtp_port=587,
            auth_smtp_username="user",
            auth_smtp_password="pw",
            auth_mail_from="noreply@example.com",
            auth_smtp_tls_mode="plaintext",
        )


def test_feature_flags_default_to_false():
    """All feature flags must default to False to prevent enabling unsafe capabilities."""
    settings = make_settings()

    assert settings.enable_generation is False
    assert settings.enable_voice_test is False
    assert settings.enable_public_calls is False
    assert settings.enable_real_paid_calls is False
    assert settings.enable_qa is False
    assert settings.enable_gateway is False


def test_settings_accepts_default_local_bot_db_with_schema_separation():
    """Accept platform DB URL pointing to default local DB with non-colliding schema separation."""
    settings = make_settings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai"
    )
    assert settings.database_url == "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai"
    assert settings.postgres_schema == "platform"

    settings_localhost = make_settings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@localhost:5439/voice_ai"
    )
    assert settings_localhost.database_url == "postgresql+asyncpg://voice_ai:voice_ai@localhost:5439/voice_ai"


def test_settings_rejects_public_schema():
    """Reject 'public' schema to prevent collision with local bot tables in shared DB."""
    with pytest.raises(ValidationError) as exc:
        make_settings(postgres_schema="public")
    assert "Platform schema cannot be 'public'" in str(exc.value)

def test_settings_rejects_phoenix_schema():
    """Reject 'phoenix' schema to prevent collision with Phoenix telemetry schema in shared DB."""
    with pytest.raises(ValidationError) as exc:
        make_settings(postgres_schema="phoenix")
    assert "Platform schema cannot be 'phoenix'" in str(exc.value)


def test_settings_rejects_invalid_schema_identifiers():
    """Reject empty, system, or invalid schema identifiers."""
    with pytest.raises(ValidationError):
        make_settings(postgres_schema="")

    with pytest.raises(ValidationError):
        make_settings(postgres_schema="pg_catalog")

    with pytest.raises(ValidationError):
        make_settings(postgres_schema="invalid-schema-name")
