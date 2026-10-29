"""Standalone consumer-behavior tests for platform_api/billing/pricing.py.

Verifies:
- Exact integer ceiling rounding behavior
- Independent calls rounding vs cumulative session rounding
- Zero rate or zero duration edge cases (explicitly free)
- Negative input rejection (ValueError)
- Invalid types (float, string, None, boolean rejection as TypeError)
- Signed 64-bit bounds and overflow rejection
- Parity with an independent Decimal ROUND_CEILING reference oracle

Strictly self-contained: does not import billing models, schemas, fixtures,
or database/service helpers so this test can run with standalone pricing logic.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
import pytest

from platform_api.billing.pricing import MAX_BIGINT, MIN_BIGINT, charge_minor


def decimal_oracle(rate_minor: int, cumulative_ms: int) -> int:
    """Independent oracle computing ceil(rate * ms / 60000) via Decimal arithmetic."""
    if rate_minor < 0 or cumulative_ms < 0:
        raise ValueError("Inputs must be non-negative")
    if rate_minor == 0 or cumulative_ms == 0:
        return 0
    numerator = Decimal(rate_minor) * Decimal(cumulative_ms)
    denominator = Decimal(60000)
    return int((numerator / denominator).to_integral_value(rounding=ROUND_CEILING))


@pytest.mark.parametrize(
    ("rate", "duration_ms", "expected_charge"),
    [
        # Standard disclosed examples:
        (200, 10000, 34),       # 200 * 10_000 / 60_000 = 33.333... -> 34 paise
        (200, 30000, 100),      # 200 * 30_000 / 60_000 = 100 paise exact
        (200, 300, 1),          # 200 * 300 / 60_000 = 1.0 paise exact
        (200, 1, 1),            # 1 ms active audio charges minimum 1 paise ceiling
        (200, 299, 1),          # fractional paise rounded up to 1 paise
        (200, 301, 2),          # 200 * 301 = 60200 -> 1.0033 paise -> 2 paise
        (100, 10000, 17),       # 100 * 10_000 / 60_000 = 16.666... -> 17 paise
        (300, 10000, 50),       # 300 * 10_000 / 60_000 = 50 paise exact
        (1, 60000, 1),          # 1 paise/min for 1 min = 1 paise
        (1, 1, 1),              # minimal nonzero charge
        (60000, 1, 1),          # rate 60000 paise/min for 1 ms = 1 paise
        (60000, 60000, 60000),  # 60000 paise/min for 60s = 60000 paise
    ],
)
def test_exact_integer_rounding(rate: int, duration_ms: int, expected_charge: int):
    """Charge formula applies integer ceil-rounding: (rate * ms + 59_999) // 60_000."""
    actual = charge_minor(rate, duration_ms)
    assert actual == expected_charge
    assert actual == decimal_oracle(rate, duration_ms)


def test_independent_call_vs_cumulative_rounding():
    """Verify difference between separate independent call charges vs single cumulative duration."""
    rate = 200  # 200 paise/min

    # Three separate calls of 10s each:
    # Each 10s call (10,000 ms) costs ceil(200 * 10,000 / 60,000) = 34 paise.
    single_call_charge = charge_minor(rate, 10000)
    assert single_call_charge == 34
    three_independent_calls_total = 3 * single_call_charge
    assert three_independent_calls_total == 102

    # A single continuous or cumulative session of 30s (30,000 ms):
    # ceil(200 * 30,000 / 60,000) = 100 paise.
    cumulative_charge = charge_minor(rate, 30000)
    assert cumulative_charge == 100

    # The cumulative charge is strictly less than 3 separate ceiled calls
    assert three_independent_calls_total > cumulative_charge
    assert three_independent_calls_total - cumulative_charge == 2

    # Progressive checkpoint sequence within one call:
    # Rounding is applied to cumulative cost, delta is billed per checkpoint
    checkpoints = [5000, 10000, 15000, 20000, 25000, 30000]
    cumulative_charges = [charge_minor(rate, t) for t in checkpoints]
    assert cumulative_charges == [17, 34, 50, 67, 84, 100]

    incremental_deltas = [
        cumulative_charges[i] - cumulative_charges[i - 1]
        for i in range(1, len(cumulative_charges))
    ]
    # Sum of incremental deltas from cumulative charges equals total cumulative charge
    assert cumulative_charges[0] + sum(incremental_deltas) == 100


@pytest.mark.parametrize(
    ("rate", "duration_ms"),
    [
        (0, 0),
        (0, 1),
        (0, 10000),
        (0, 60000),
        (0, 999999),
        (0, MAX_BIGINT),
        (1, 0),
        (200, 0),
        (60000, 0),
        (MAX_BIGINT, 0),
    ],
)
def test_zero_rate_or_duration_is_free(rate: int, duration_ms: int):
    """Zero rate or zero duration is strictly free (0 paise)."""
    assert charge_minor(rate, duration_ms) == 0
    assert decimal_oracle(rate, duration_ms) == 0


@pytest.mark.parametrize(
    ("rate", "duration_ms"),
    [
        (-1, 10000),
        (-200, 30000),
        (200, -1),
        (200, -10000),
        (-1, -1),
        (MIN_BIGINT, 1000),
        (1000, MIN_BIGINT),
    ],
)
def test_negative_values_raise_value_error(rate: int, duration_ms: int):
    """Negative rates or durations must be rejected with ValueError."""
    with pytest.raises(ValueError, match="non-negative"):
        charge_minor(rate, duration_ms)


@pytest.mark.parametrize(
    ("rate", "duration_ms", "expected_msg"),
    [
        (True, 10000, "integers, not booleans"),
        (False, 10000, "integers, not booleans"),
        (200, True, "integers, not booleans"),
        (200, False, "integers, not booleans"),
        (True, True, "integers, not booleans"),
        (200.0, 10000, "must be integers"),
        (200, 10000.0, "must be integers"),
        ("200", 10000, "must be integers"),
        (200, "10000", "must be integers"),
        (None, 10000, "must be integers"),
        (200, None, "must be integers"),
        (Decimal("200"), 10000, "must be integers"),
    ],
)
def test_invalid_types_raise_type_error(rate, duration_ms, expected_msg: str):
    """Booleans, floats, strings, None, Decimals must raise TypeError."""
    with pytest.raises(TypeError, match=expected_msg):
        charge_minor(rate, duration_ms)


def test_64_bit_boundaries_and_overflow():
    """Verify behavior at signed 64-bit integer limits (MAX_BIGINT)."""
    # Exceeding MAX_BIGINT in input:
    with pytest.raises(ValueError, match="exceed signed 64-bit"):
        charge_minor(MAX_BIGINT + 1, 1000)

    with pytest.raises(ValueError, match="exceed signed 64-bit"):
        charge_minor(1000, MAX_BIGINT + 1)

    # Valid at MAX_BIGINT with zero duration or zero rate
    assert charge_minor(MAX_BIGINT, 0) == 0
    assert charge_minor(0, MAX_BIGINT) == 0

    # Rate 1 with duration up to 60_000 * MAX_BIGINT would overflow,
    # but within MAX_BIGINT:
    # If rate = 1, ms = MAX_BIGINT:
    # charge = (1 * MAX_BIGINT + 59999) // 60000
    expected_charge = (MAX_BIGINT + 59999) // 60000
    assert expected_charge <= MAX_BIGINT
    assert charge_minor(1, MAX_BIGINT) == expected_charge
    assert charge_minor(1, MAX_BIGINT) == decimal_oracle(1, MAX_BIGINT)

    # At rate = MAX_BIGINT and duration = 60000 ms:
    # charge = (MAX_BIGINT * 60000 + 59999) // 60000 == MAX_BIGINT (exact limit)
    assert charge_minor(MAX_BIGINT, 60000) == MAX_BIGINT

    # For duration = 60001 ms, calculated charge strictly exceeds MAX_BIGINT:
    with pytest.raises(ValueError, match="exceeds signed 64-bit integer maximum"):
        charge_minor(MAX_BIGINT, 60001)
    # Intermediate product does not crash python integers, but result exceeding MAX_BIGINT is rejected:
    with pytest.raises(ValueError, match="exceeds signed 64-bit integer maximum"):
        charge_minor(MAX_BIGINT, MAX_BIGINT)


@pytest.mark.parametrize(
    ("rate", "ms"),
    [
        (1, 1),
        (2, 59999),
        (2, 60000),
        (2, 60001),
        (17, 353),
        (199, 12345),
        (500, 600000),
        (9999, 88888),
        (100000, 500),
    ],
)
def test_property_matches_decimal_oracle(rate: int, ms: int):
    """Systematically verify charge matches independent Decimal oracle across arbitrary values."""
    assert charge_minor(rate, ms) == decimal_oracle(rate, ms)
