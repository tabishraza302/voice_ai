"""Secret-key detection and scrubbing for agent configuration artifacts.

The platform never stores operational credentials in a draft, flow, prompt
artifact or hash.  Draft saves reject forbidden keys; publication scrubs the
same keys again as defence in depth before persisting an immutable snapshot.
"""

from __future__ import annotations

from typing import Any

FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "access_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "service_account",
        "session_token",
        "token",
        "webhook_secret",
    }
)


def _normalize_key(key: str) -> str:
    """Normalize a JSON key so camelCase/kebab/snake variants are caught."""
    return key.strip().lower().replace("-", "_")


def _pointer_escape(token: str) -> str:
    """Escape an RFC 6901 reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def find_secret_paths(value: Any, prefix: str = "") -> list[str]:
    """Return RFC 6901 paths of every forbidden secret key in ``value``."""
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}/{_pointer_escape(key_text)}"
            if _normalize_key(key_text) in FORBIDDEN_SECRET_KEYS:
                paths.append(path)
            else:
                paths.extend(find_secret_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(find_secret_paths(child, f"{prefix}/{index}"))
    return paths


def scrub_secrets(value: Any) -> Any:
    """Recursively drop forbidden secret keys from a JSON-compatible value."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            if _normalize_key(str(key)) in FORBIDDEN_SECRET_KEYS:
                continue
            clean[key] = scrub_secrets(child)
        return clean
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    return value
