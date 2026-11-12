from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import signaling, voice
from app.coordination import CallClaim, LeaseObservation
from app.voice import CallDefinition, CallRuntime


def make_test_runtime(
    session_id: uuid.UUID | None = None,
    claim: CallClaim | None = None,
    coordination: Any = None,
) -> CallRuntime:
    session_id = session_id or uuid.uuid4()
    agent_id = uuid.uuid4()
    if claim is None and coordination is not None:
        claim = CallClaim(
            session_id=session_id,
            agent_id=agent_id,
            call_type="test",
            generation="gen-1",
            worker_id="test-worker",
            phase="active",
        )
    runtime = object.__new__(CallRuntime)
    runtime.definition = SimpleNamespace(
        session_id=session_id,
        agent_id=agent_id,
        call_type="test",
        max_call_duration_seconds=300,
    )
    runtime.writer = SimpleNamespace(
        flush=AsyncMock(return_value=None),
        drain=AsyncMock(return_value=None),
    )
    runtime.claim = claim
    runtime.coordination = coordination
    runtime.pc_id = f"pc-{session_id.hex[:8]}"
    runtime.lease_lock = asyncio.Lock()
    runtime.lease_deadline = time.monotonic() + 30.0
    runtime.watchdog_task = None
    runtime.connection = None
    runtime.transport = None
    runtime.worker = None
    runtime.worker_task = None
    runtime.duration_task = None
    runtime.flush_task = None
    runtime.ready = None
    runtime.cleanup_task = None
    runtime.closing = False
    runtime.renegotiating = False
    runtime.closed = False
    runtime.termination_status = None
    return runtime


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_lease_renewal_continues_during_slow_media_cleanup(monkeypatch):
    """Proves watchdog keeps renewing lease while slow media worker teardown is in progress."""
    coord = AsyncMock()
    runtime = make_test_runtime(coordination=coord)

    renew_calls = 0
    renew_event = asyncio.Event()

    async def fake_renew(claim, coordination, pc_id):
        nonlocal renew_calls
        renew_calls += 1
        renew_event.set()
        return LeaseObservation(claim=claim, monotonic_expires_at=time.monotonic() + 30.0)

    monkeypatch.setattr("app.signaling.renew_voice_claim", fake_renew)
    monkeypatch.setattr("app.sessions.set_session_status", AsyncMock(return_value=True))

    # Patch watchdog sleep to a very small interval for test speed
    orig_sleep = asyncio.sleep

    async def fast_watchdog_sleep(seconds):
        if seconds == 5.0:
            return await orig_sleep(0.03)
        return await orig_sleep(seconds)

    monkeypatch.setattr("app.signaling.asyncio.sleep", fast_watchdog_sleep)

    # Simulate slow media worker cancellation
    worker_cancelled = False

    async def slow_worker_cancel():
        nonlocal worker_cancelled
        await orig_sleep(0.12)
        worker_cancelled = True
        if runtime.worker_task and not runtime.worker_task.done():
            runtime.worker_task.cancel()

    runtime.worker = SimpleNamespace(cancel=slow_worker_cancel)

    async def worker_loop():
        try:
            await orig_sleep(10.0)
        except asyncio.CancelledError:
            pass

    runtime.worker_task = asyncio.create_task(worker_loop())

    # Start watchdog
    runtime.watchdog_task = asyncio.create_task(signaling._setup_watchdog(runtime, coord))

    # Stop call gracefully as completed
    runtime.request_stop("completed")

    # Await cleanup to finish
    assert runtime.cleanup_task is not None
    await runtime.cleanup_task

    # Verifications:
    # 1. Multiple lease renewals occurred during the slow media worker cancellation
    assert renew_calls >= 2, f"Expected at least 2 renewals during slow teardown, got {renew_calls}"
    assert worker_cancelled is True
    # 2. Watchdog was cancelled only after media worker stopped
    assert runtime.watchdog_task.done() is True
    # 3. Call closed successfully as completed
    assert runtime.closed is True
    assert runtime.termination_status == "completed"


@pytest.mark.anyio
async def test_lease_loss_during_teardown_fences_call_safely(monkeypatch):
    """Proves lease loss/renewal failure during media teardown fences the call to failed."""
    coord = AsyncMock()
    runtime = make_test_runtime(coordination=coord)

    async def failing_renew(claim, coordination, pc_id):
        raise RuntimeError("Durable lease renewal failed: session mismatch or non-active status")

    monkeypatch.setattr("app.signaling.renew_voice_claim", failing_renew)

    orig_sleep = asyncio.sleep

    async def fast_watchdog_sleep(seconds):
        if seconds == 5.0:
            return await orig_sleep(0.02)
        return await orig_sleep(seconds)

    monkeypatch.setattr("app.signaling.asyncio.sleep", fast_watchdog_sleep)

    recorded_statuses = []

    async def fake_set_session_status(session_id, status, claim=None):
        recorded_statuses.append(status)
        return True

    monkeypatch.setattr("app.sessions.set_session_status", fake_set_session_status)

    async def slow_worker_cancel():
        await orig_sleep(0.08)

    runtime.worker = SimpleNamespace(cancel=slow_worker_cancel)

    async def worker_loop():
        try:
            await orig_sleep(10.0)
        except asyncio.CancelledError:
            pass

    runtime.worker_task = asyncio.create_task(worker_loop())
    runtime.watchdog_task = asyncio.create_task(signaling._setup_watchdog(runtime, coord))

    # Stop requested as completed
    runtime.request_stop("completed")

    assert runtime.cleanup_task is not None
    await runtime.cleanup_task

    # Verifications:
    # 1. Terminal status must be fenced to 'failed', never 'completed'
    assert runtime.termination_status == "failed"
    assert "failed" in recorded_statuses
    assert "completed" not in recorded_statuses
    # 2. Worker task was cancelled/fenced immediately
    assert runtime.worker_task.done() is True
    assert runtime.closed is True


@pytest.mark.anyio
async def test_hung_worker_and_disconnect_bounded(monkeypatch):
    """Proves cleanup steps (worker.cancel, disconnect, flush) do not hang indefinitely when stuck."""
    coord = AsyncMock()
    runtime = make_test_runtime(coordination=coord)

    # Use a small step timeout for testing
    monkeypatch.setattr(voice, "_CLEANUP_STEP_TIMEOUT_SECONDS", 0.05)

    async def hung_action():
        await asyncio.Future()  # Never completes

    runtime.worker = SimpleNamespace(cancel=hung_action)
    runtime.connection = SimpleNamespace(
        disconnect=hung_action,
        send_app_message=lambda msg: None,
    )
    runtime.writer = SimpleNamespace(
        flush=hung_action,
        drain=hung_action,
    )

    t_start = time.monotonic()
    runtime.request_stop("completed")
    assert runtime.cleanup_task is not None

    # Wait for cleanup with a generous overall timeout
    await asyncio.wait_for(runtime.cleanup_task, timeout=1.0)
    elapsed = time.monotonic() - t_start

    # Verifications:
    # 1. Cleanup completed in bounded time (~0.15 - 0.25s) despite 3 hung steps
    assert elapsed < 0.8, f"Cleanup took too long: {elapsed:.2f}s"
    assert runtime.closed is True
    # 2. Because steps timed out, the termination status must be 'failed', not 'completed'
    assert runtime.termination_status == "failed"


@pytest.mark.anyio
async def test_stop_all_local_runtimes_timeout_deadline_preserves_semantics(monkeypatch):
    """Proves stop_all_local_runtimes enforces deadline and preserves terminal failure semantics."""
    coord = AsyncMock()
    runtime = make_test_runtime(coordination=coord)

    # Simulate hung cleanup task
    async def hung_cleanup():
        await asyncio.Future()

    runtime.cleanup_task = asyncio.create_task(hung_cleanup())

    # Register in signaling registry
    async with signaling._registry_lock:
        signaling._setup_runtimes_by_session[runtime.session_id] = runtime

    recorded_statuses = []

    async def fake_set_session_status(session_id, status, claim=None):
        recorded_statuses.append(status)
        return True

    monkeypatch.setattr("app.sessions.set_session_status", fake_set_session_status)

    t_start = time.monotonic()
    # Call shutdown with a 0.05s deadline
    await signaling.stop_all_local_runtimes(timeout=0.05)
    elapsed = time.monotonic() - t_start

    # Verifications:
    # 1. Bounded by deadline
    assert elapsed < 0.5, f"Shutdown took too long: {elapsed:.2f}s"
    # 2. Registry was cleared
    async with signaling._registry_lock:
        assert runtime.session_id not in signaling._setup_runtimes_by_session
    # 3. Safe terminal semantics: runtime marked failed, never fake success
    assert runtime.closed is True
    assert runtime.termination_status == "failed"
    assert "failed" in recorded_statuses
    # 4. Capacity was released
    assert coord.release.awaited
