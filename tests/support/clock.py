"""Deterministic monotonic fake clock for verification and testing.

Implements the contract specified in verification-plan.md Section 4.
"""

from __future__ import annotations

import asyncio
import pytest


class FakeClock:
    def __init__(self, start_ns: int = 1_000_000_000_000) -> None:
        if start_ns < 0:
            raise ValueError("start_ns must be non-negative")
        self._current_ns = start_ns
        self._waiters: list[tuple[int, asyncio.Event]] = []

    def now_ns(self) -> int:
        return self._current_ns

    def advance_ns(self, delta: int) -> None:
        if delta < 0:
            raise ValueError(f"negative advance_ns: {delta}")
        self._current_ns += delta

        # Wake waiters whose deadline has arrived
        pending: list[tuple[int, asyncio.Event]] = []
        for deadline, event in self._waiters:
            if deadline <= self._current_ns:
                event.set()
            else:
                pending.append((deadline, event))
        self._waiters = pending

    async def sleep_until_ns(self, deadline: int) -> None:
        if deadline <= self._current_ns:
            return
        event = asyncio.Event()
        self._waiters.append((deadline, event))
        await event.wait()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
