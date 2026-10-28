"""Independent first-release accounting examples. No application imports or I/O."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING


def reference_cost(rate: int, cumulative_ms: int) -> int:
    if rate < 0 or cumulative_ms < 0:
        raise ValueError("negative rate/time")
    return int((Decimal(rate) * Decimal(cumulative_ms) / Decimal(60000)).to_integral_value(rounding=ROUND_CEILING))


@dataclass
class Budget:
    rate: int
    proven: int = 0
    authorized: int = 0
    charged: int = 0
    held: int = 0
    closed: bool = False


class Wallet:
    def __init__(self, credited_minor: int):
        if credited_minor < 0:
            raise ValueError("negative credit")
        self.posted = credited_minor
        self.ledger = [credited_minor]
        self.budgets: dict[str, Budget] = {}
        self.operations: dict[tuple, tuple] = {}

    @property
    def held(self):
        return sum(b.held for b in self.budgets.values())

    def invariant(self):
        assert self.posted >= self.held >= 0
        assert self.posted == sum(self.ledger)
        for b in self.budgets.values():
            assert b.charged == reference_cost(b.rate, b.proven)
            assert 0 <= b.proven <= b.authorized
            assert b.held == (0 if b.closed else reference_cost(b.rate, b.authorized) - b.charged)

    def checkpoint(self, sid, op, rate, proven, future):
        request = (rate, proven, future)
        key = (sid, op)
        if key in self.operations:
            old_request, result = self.operations[key]
            if request != old_request:
                raise ValueError("conflicting replay")
            return result
        if min(rate, proven, future) < 0:
            raise ValueError("negative input")
        b = self.budgets.setdefault(sid, Budget(rate))
        if b.closed or rate != b.rate or not b.proven <= proven <= b.authorized:
            raise ValueError("closed, repriced, or unauthorized proof")
        delta = reference_cost(rate, proven) - b.charged
        assert delta <= b.held
        self.posted -= delta
        b.held -= delta
        b.proven = proven
        b.charged += delta
        if delta:
            self.ledger.append(-delta)
        desired = max(b.authorized, proven + future)
        # Deliberately search by reference cost, independently of the production integer bound.
        available = self.posted - self.held
        lo, hi = b.authorized, desired
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if reference_cost(rate, mid) - b.charged <= b.held + available:
                lo = mid
            else:
                hi = mid - 1
        b.authorized = lo
        b.held = reference_cost(rate, lo) - b.charged
        result = (b.proven, b.authorized, b.charged, b.held)
        self.operations[key] = (request, result)
        self.invariant()
        return result

    def close_after_stop(self, sid, proven):
        b = self.budgets[sid]
        if b.closed:
            if proven != b.proven:
                raise ValueError("late charge after closure")
            return
        self.checkpoint(sid, "close", b.rate, proven, 0)
        b.held = 0
        b.closed = True
        self.invariant()


def verify_examples():
    assert reference_cost(200, 10000) == 34
    assert reference_cost(200, 30000) == 100
    assert reference_cost(200, 300) == 1
    assert reference_cost(0, 999999) == 0
    w = Wallet(100)
    first = w.checkpoint("a", "grant", 200, 0, 10000)
    assert first == w.checkpoint("a", "grant", 200, 0, 10000)
    assert w.held == 34
    try:
        w.checkpoint("a", "grant", 200, 0, 11000)
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting replay accepted")
    w.checkpoint("b", "grant", 200, 0, 10000)
    assert w.checkpoint("c", "grant", 200, 0, 10000)[1] == 9600
    assert w.held == 100
    w.close_after_stop("a", 10000)
    w.close_after_stop("b", 10000)
    w.close_after_stop("c", 9600)
    assert w.posted == 0 and w.held == 0
    partial = Wallet(100)
    partial.checkpoint("p", "grant", 200, 0, 1000)
    partial.close_after_stop("p", 300)
    partial.close_after_stop("p", 300)
    assert partial.posted == 99 and partial.held == 0
    crash = Wallet(100)
    crash.checkpoint("x", "grant", 200, 0, 1000)
    crash.close_after_stop("x", 0)  # called only after stop/expiry proof in the service contract
    assert crash.posted == 100 and crash.held == 0
    mixed = Wallet(200)
    mixed.checkpoint("low", "grant", 100, 0, 10000)
    mixed.checkpoint("high", "grant", 300, 0, 10000)
    mixed.close_after_stop("low", 10000)
    mixed.close_after_stop("high", 10000)
    assert mixed.posted == 133
    cumulative = Wallet(1000)
    cumulative.checkpoint("one", "start", 200, 0, 1000)
    for tick in range(1, 31):
        cumulative.checkpoint("one", str(tick), 200, tick * 1000, 1000)
    cumulative.close_after_stop("one", 30000)
    assert cumulative.posted == 900
    for rate in (0, 1, 200, 333, 10000):
        for duration in (0, 1, 299, 1000, 10001, 30000, 60000):
            exact = (rate * duration + 59999) // 60000
            assert exact == reference_cost(rate, duration)
    print("Accounting specification examples passed: pricing, cumulative rounding, shared funds, mixed rates, replay, partial close and crash write-off.")


if __name__ == "__main__":
    verify_examples()
