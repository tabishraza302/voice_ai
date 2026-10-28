"""Bounded scrypt password hashing and verification.

Follows auth-contract.md:
- 15–128 Unicode codepoints, UTF-8 encoded length at most 512 bytes.
- Spaces and Unicode preserved without trimming, normalization, or composition rules.
- Reject malformed input before hashing; never reflect input in errors.
- Format: scrypt$v1$131072$8$1$<salt-b64url>$<digest-b64url>.
- Bounded thread executor with immediate 429 overflow rejection.
- Fixed dummy hash for constant-time unknown account verification.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import secrets
from typing import Any

from platform_api.auth.errors import InvalidCredentialsError, RateLimitedError, ValidationError

# Fixed bounded scrypt parameters
SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 268435456  # 256 MiB
SALT_BYTES = 16

# Minimum / Maximum password rules
MIN_CODEPOINTS = 15
MAX_CODEPOINTS = 128
MAX_UTF8_BYTES = 512

# Fixed dummy hash for unknown-account login verification (precomputed scrypt$v1$131072$8$1$...)
# Declared as literal constant to prevent CPU and 128 MiB memory allocation at module import time
DUMMY_HASH = "scrypt$v1$131072$8$1$AAAAAAAAAAAAAAAAAAAAAA$2695QJju9cWb1stGevMtMyLfA8lDe3U_DTP_pG9KpXk"

def _b64url_encode(data: bytes) -> str:
    """Encode bytes to unpadded base64url ASCII string."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    """Decode unpadded or padded base64url ASCII string to bytes."""
    s = raw.strip()
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def check_scrypt_availability() -> None:
    """Ensure OpenSSL scrypt is available in hashlib."""
    if not hasattr(hashlib, "scrypt"):
        raise RuntimeError("hashlib.scrypt is unavailable; OpenSSL 1.1+ with scrypt support required")


def validate_password_rules(password: str) -> bytes:
    """Validate codepoint and byte bounds.

    Returns UTF-8 bytes if valid, or raises ValidationError.
    Preserves all spaces and Unicode exactly.
    """
    codepoints = len(password)
    if codepoints < MIN_CODEPOINTS:
        raise ValidationError(
            f"Password must be at least {MIN_CODEPOINTS} characters",
            fields=[{"field": "password", "code": "too_short"}],
        )
    if codepoints > MAX_CODEPOINTS:
        raise ValidationError(
            f"Password cannot exceed {MAX_CODEPOINTS} characters",
            fields=[{"field": "password", "code": "too_long"}],
        )
    try:
        pw_bytes = password.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ValidationError(
            "Password contains invalid characters",
            fields=[{"field": "password", "code": "invalid_encoding"}],
        ) from e

    if len(pw_bytes) > MAX_UTF8_BYTES:
        raise ValidationError(
            f"Password UTF-8 length exceeds {MAX_UTF8_BYTES} bytes",
            fields=[{"field": "password", "code": "too_many_bytes"}],
        )
    return pw_bytes


def _hash_sync(password_bytes: bytes, salt: bytes | None = None) -> str:
    """Synchronous bounded scrypt hash computation."""
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password_bytes,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    salt_b64 = _b64url_encode(salt)
    digest_b64 = _b64url_encode(digest)
    return f"scrypt$v1${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${digest_b64}"


def _verify_sync(password_bytes: bytes, stored_hash: str) -> bool:
    """Synchronous verification against stored hash with strict bounded validation."""
    parts = stored_hash.split("$")
    if len(parts) != 7:
        return False
    algo, version, n_str, r_str, p_str, salt_b64, digest_b64 = parts
    if algo != "scrypt" or version != "v1":
        return False

    try:
        n = int(n_str)
        r = int(r_str)
        p = int(p_str)
    except ValueError:
        return False

    # Enforce strict bounds on parameters to prevent resource exhaustion from crafted hashes
    if n <= 1 or (n & (n - 1)) != 0 or n > SCRYPT_N:
        return False
    if r < 1 or r > SCRYPT_R:
        return False
    if p < 1 or p > SCRYPT_P:
        return False

    try:
        salt = _b64url_decode(salt_b64)
        expected_digest = _b64url_decode(digest_b64)
    except Exception:
        return False

    if len(salt) != SALT_BYTES or len(expected_digest) != SCRYPT_DKLEN:
        return False

    try:
        computed = hashlib.scrypt(
            password_bytes,
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )
    except Exception:
        return False

    return hmac.compare_digest(computed, expected_digest)


class PasswordHasher:
    """Thread-pool executor with bounded concurrency and non-blocking semaphore."""

    def __init__(self, concurrency: int = 2) -> None:
        check_scrypt_availability()
        self._concurrency = max(1, concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=self._concurrency,
            thread_name_prefix="scrypt-hasher",
        )
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._concurrency)
        return self._semaphore

    async def _run_in_thread(self, fn: Any, *args: Any) -> Any:
        """Execute function in thread pool with strict permit retention across cancellation."""
        sem = self._get_semaphore()
        if sem.locked():
            raise RateLimitedError("Password work busy; please retry", retry_after=1)

        await sem.acquire()
        loop = asyncio.get_running_loop()
        try:
            fut = self._executor.submit(fn, *args)
        except Exception:
            sem.release()
            raise

        # Retain permit until worker thread actually completes, preventing cancellation saturation
        fut.add_done_callback(lambda _: loop.call_soon_threadsafe(sem.release))
        return await asyncio.wrap_future(fut, loop=loop)

    async def hash_password(self, password: str) -> str:
        """Validate bounds and compute scrypt hash off the event loop."""
        pw_bytes = validate_password_rules(password)
        return await self._run_in_thread(_hash_sync, pw_bytes)

    async def verify_password(self, password: str, stored_hash: str) -> bool:
        """Validate input format and verify hash off the event loop."""
        try:
            pw_bytes = validate_password_rules(password)
        except ValidationError:
            # If input doesn't satisfy password bounds, run dummy hash to avoid timing leaks
            await self.verify_dummy()
            return False

        return await self._run_in_thread(_verify_sync, pw_bytes, stored_hash)

    async def verify_dummy(self) -> bool:
        """Run dummy scrypt verification for unknown-account constant timing."""
        await self._run_in_thread(
            _verify_sync,
            b"dummy_password_for_timing_safety",
            DUMMY_HASH,
        )
        return False

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


# Module-level default hasher
_DEFAULT_HASHER: PasswordHasher | None = None


def get_password_hasher(concurrency: int = 2) -> PasswordHasher:
    global _DEFAULT_HASHER
    if _DEFAULT_HASHER is None:
        _DEFAULT_HASHER = PasswordHasher(concurrency)
    return _DEFAULT_HASHER
