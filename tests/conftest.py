"""Shared root pytest configuration, marker definitions, and fixture imports.

Implements the contract specified in verification-plan.md Section 4 and 6.
"""

from __future__ import annotations

import os
import pytest

from tests.support.clock import fake_clock
from tests.support.models import fake_models
from tests.support.postgres import fresh_pg
from tests.support.broker import rabbitmq_broker

__all__ = [
    "fake_clock",
    "fake_models",
    "fresh_pg",
    "rabbitmq_broker",
]


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers required for strict verification lanes."""
    config.addinivalue_line("markers", "postgres: test requires PostgreSQL database")
    config.addinivalue_line("markers", "browser: test requires browser/Playwright environment")
    config.addinivalue_line("markers", "live_voice: test requires live voice/STT/TTS stack")
    config.addinivalue_line("markers", "qualification: test is a formal qualification gate")
    config.addinivalue_line("markers", "broker: test requires message broker (RabbitMQ)")


@pytest.fixture(autouse=True)
def _auto_broker(request: pytest.FixtureRequest) -> None:
    """Ensure broker tests request rabbitmq_broker fixture, skipping or failing cleanly."""
    if request.node.get_closest_marker("broker"):
        request.getfixturevalue("rabbitmq_broker")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    """Enforce fail-closed policy when VERIFY_REQUIRED=1."""
    outcome = yield
    rep = outcome.get_result()

    if os.environ.get("VERIFY_REQUIRED") == "1":
        # Disallow skips in required qualification mode
        if rep.skipped:
            rep.outcome = "failed"
            if rep.longrepr is None or isinstance(rep.longrepr, tuple):
                rep.longrepr = "Skips and xfails are forbidden when VERIFY_REQUIRED=1"
        # Disallow unexpected passes (XPASS) in required qualification mode
        elif hasattr(rep, "wasxfail") and rep.outcome == "passed":
            rep.outcome = "failed"
            rep.longrepr = "XPASS is forbidden when VERIFY_REQUIRED=1"



def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Enforce fail-closed policy on collection skips when VERIFY_REQUIRED=1."""
    if os.environ.get("VERIFY_REQUIRED") == "1":
        if report.skipped:
            report.outcome = "failed"
            if report.longrepr is None or isinstance(report.longrepr, tuple):
                report.longrepr = "Collection skips are forbidden when VERIFY_REQUIRED=1"

def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Ensure non-zero exit status if forbidden outcomes occurred under VERIFY_REQUIRED=1."""
    if os.environ.get("VERIFY_REQUIRED") == "1":
        # Check if any tests were skipped or xfailed/xpassed
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        if tr:
            stats = tr.stats
            has_skips = bool(stats.get("skipped") or stats.get("xfailed") or stats.get("xpassed"))
            if has_skips and exitstatus == 0:
                session.exitstatus = 1
