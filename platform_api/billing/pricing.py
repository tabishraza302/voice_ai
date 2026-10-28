"""Pure integer pricing formula for prepaid Voice AI sessions.

Follows accounting-contract.md:
- Currency is INR; amounts are integer paise.
- charge(t, r) = (r * t + 59_999) // 60_000
- Pinned nonnegative rate r paise/minute and cumulative billable milliseconds t.
- Rounding ceiling is applied to cumulative cost, never separately to each checkpoint.
- Rate zero is explicitly free.
- Duration zero is free.
- Rejects negative or overflowing values beyond signed 64-bit integer bounds.
"""

from __future__ import annotations

MAX_BIGINT = (1 << 63) - 1
MIN_BIGINT = -(1 << 63)


def charge_minor(rate_minor_per_minute: int, cumulative_billable_ms: int) -> int:
    """Calculate cumulative customer charge in integer paise.

    Args:
        rate_minor_per_minute: Retail price in paise/minute (pinned rate).
        cumulative_billable_ms: Cumulative billable active audio duration in milliseconds.

    Returns:
        Cumulative integer paise charged.

    Raises:
        TypeError: If arguments are boolean or not integer.
        ValueError: If arguments are negative or exceed signed 64-bit bounds.
    """
    if isinstance(rate_minor_per_minute, bool) or isinstance(cumulative_billable_ms, bool):
        raise TypeError("rate_minor_per_minute and cumulative_billable_ms must be integers, not booleans")

    if not isinstance(rate_minor_per_minute, int) or not isinstance(cumulative_billable_ms, int):
        raise TypeError("rate_minor_per_minute and cumulative_billable_ms must be integers")

    if rate_minor_per_minute < 0 or cumulative_billable_ms < 0:
        raise ValueError("rate_minor_per_minute and cumulative_billable_ms must be non-negative")

    if rate_minor_per_minute > MAX_BIGINT or cumulative_billable_ms > MAX_BIGINT:
        raise ValueError("Inputs exceed signed 64-bit integer maximum")

    if rate_minor_per_minute == 0 or cumulative_billable_ms == 0:
        return 0

    charge = (rate_minor_per_minute * cumulative_billable_ms + 59_999) // 60_000

    if charge > MAX_BIGINT:
        raise ValueError("Calculated charge exceeds signed 64-bit integer maximum")

    return charge
