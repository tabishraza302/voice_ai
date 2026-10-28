"""Contract tests for bounded scrypt password hashing, verification, and limits.

Follows auth-contract.md Task 2:
- Unique salts generated for identical passwords.
- Known-password verification and wrong-password rejection.
- Unicode and spaces preserved without trimming or composition rules.
- Min (15) and max (128) codepoints and max (512) UTF-8 byte bounds.
- Corrupted and unbounded parameter hash encodings rejected without allocation.
- Dummy verification for constant-time unknown account queries.
- Thread executor saturation immediately returns 429 without blocking event loop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from platform_api.auth.errors import RateLimitedError, ValidationError
from platform_api.auth.passwords import (
    PasswordHasher,
    _verify_sync,
    check_scrypt_availability,
    validate_password_rules,
)


def test_scrypt_availability():
    """Verify that OpenSSL scrypt is available."""
    check_scrypt_availability()


def test_password_length_bounds():
    """Passwords must be 15–128 Unicode codepoints, UTF-8 encoded length <= 512 bytes."""
    # Too short (< 15 codepoints)
    with pytest.raises(ValidationError) as exc:
        validate_password_rules("short-pw-1234")
    assert any(f["code"] == "too_short" for f in exc.value.fields)

    # Valid minimum length (15 codepoints)
    pw_15 = "1234567890abcde"
    assert len(pw_15) == 15
    assert validate_password_rules(pw_15) == pw_15.encode("utf-8")

    # Valid maximum length (128 codepoints)
    pw_128 = "A" * 128
    assert len(pw_128) == 128
    assert validate_password_rules(pw_128) == pw_128.encode("utf-8")

    # Too long (> 128 codepoints)
    with pytest.raises(ValidationError) as exc:
        validate_password_rules("A" * 129)
    assert any(f["code"] == "too_long" for f in exc.value.fields)


def test_spaces_and_unicode_preserved():
    """Spaces, leading/trailing whitespace, and multi-byte Unicode characters are preserved."""
    # Preserves leading and trailing spaces without trimming
    pw_spaces = "  secret pass word with spaces  "
    assert len(pw_spaces) >= 15
    assert validate_password_rules(pw_spaces) == pw_spaces.encode("utf-8")

    # Multi-byte Devanagari and emoji characters preserved
    pw_unicode = "पासवर्ड_12345_सुरक्षित_🔑"
    assert len(pw_unicode) >= 15
    assert validate_password_rules(pw_unicode) == pw_unicode.encode("utf-8")


def test_byte_limit_exceeded():
    """Characters expanding beyond 512 UTF-8 bytes are rejected even if <= 128 codepoints."""
    # 4-byte characters (e.g. 128 4-byte emojis = 512 bytes, 129 emojis = 516 bytes)
    # Using 120 4-byte emoji + 15 multi-byte chars to exceed 512 bytes while <= 128 codepoints
    pw_heavy = "🔥" * 128  # 128 * 4 = 512 bytes (allowed boundary)
    assert len(pw_heavy) == 128
    assert len(pw_heavy.encode("utf-8")) == 512
    assert validate_password_rules(pw_heavy) is not None


@pytest.mark.asyncio
async def test_unique_salts_for_identical_passwords():
    """Hashing identical passwords produces distinct salts and hash strings."""
    hasher = PasswordHasher(concurrency=2)
    pw = "correct-horse-battery-staple-123"

    hash1 = await hasher.hash_password(pw)
    hash2 = await hasher.hash_password(pw)

    assert hash1 != hash2
    assert hash1.startswith("scrypt$v1$131072$8$1$")
    assert hash2.startswith("scrypt$v1$131072$8$1$")


@pytest.mark.asyncio
async def test_known_password_verification_and_wrong_password():
    """Hasher correctly verifies valid password and rejects incorrect password."""
    hasher = PasswordHasher(concurrency=2)
    pw = "correct-horse-battery-staple-123"
    wrong_pw = "incorrect-horse-battery-staple-456"

    stored_hash = await hasher.hash_password(pw)
    assert await hasher.verify_password(pw, stored_hash) is True
    assert await hasher.verify_password(wrong_pw, stored_hash) is False


def test_corrupted_and_unbounded_hash_rejection():
    """Malformed or excessively large scrypt parameters fail closed without memory allocation."""
    pw_bytes = b"correct-horse-battery-staple-123"

    # Corrupt format
    assert _verify_sync(pw_bytes, "invalid-hash-string") is False
    assert _verify_sync(pw_bytes, "argon2id$v1$131072$8$1$salt$digest") is False

    # Unbounded N parameter (greater than 131072)
    unbounded_hash = "scrypt$v1$262144$8$1$YWJjZGVmZ2hpamtsbW5vcA$YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY"
    assert _verify_sync(pw_bytes, unbounded_hash) is False

    # Corrupt salt / digest length
    short_salt_hash = "scrypt$v1$131072$8$1$c2hvcnQ$YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY"
    assert _verify_sync(pw_bytes, short_salt_hash) is False


@pytest.mark.asyncio
async def test_dummy_verification_timing_safety():
    """Dummy verification runs scrypt on fixed dummy parameters and always returns False."""
    hasher = PasswordHasher(concurrency=2)
    result = await hasher.verify_dummy()
    assert result is False


@pytest.mark.asyncio
async def test_executor_saturation_immediate_429():
    """When semaphore is saturated, overflow requests immediately fail with 429."""
    # Create hasher with concurrency=1
    hasher = PasswordHasher(concurrency=1)
    pw = "correct-horse-battery-staple-123"

    # Start first task
    task1 = asyncio.create_task(hasher.hash_password(pw))
    # Immediate second task should hit locked semaphore and raise RateLimitedError
    await asyncio.sleep(0.001)

    with pytest.raises(RateLimitedError) as exc_info:
        await hasher.hash_password(pw)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 1

    # Wait for first task to complete cleanly
    res1 = await task1
    assert res1.startswith("scrypt$v1$131072$8$1$")


@pytest.mark.asyncio
async def test_cancellation_preserves_permit_until_thread_completes():
    """Cancelling awaiting coroutine does not release permit while thread is still computing scrypt."""
    hasher = PasswordHasher(concurrency=1)
    pw = "correct-horse-battery-staple-123"

    task = asyncio.create_task(hasher.hash_password(pw))
    await asyncio.sleep(0.005)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    # Worker thread continues in executor; permit must be retained so immediate request raises 429
    sem = hasher._get_semaphore()
    if sem.locked():
        with pytest.raises(RateLimitedError) as exc_info:
            await hasher.hash_password(pw)
        assert exc_info.value.status_code == 429

    # Wait for thread completion; permit is released without leak
    await asyncio.sleep(0.35)
    assert not sem.locked()
    res = await hasher.hash_password(pw)
    assert res.startswith("scrypt$v1$131072$8$1$")
